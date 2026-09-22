/**
 * The page a quick link opens, once it is open.
 *
 * The camera, the photo policy and the location fix are ``capture.js``, shared with the
 * enrollment page; what is left here is this page's own flow: what the link is for, what a
 * tap sends, and what the server's answer means.
 */
(function () {
    "use strict";

    var API = "/api/v1";
    var token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
    var info = null;
    var camera = null;
    //: False until the server has said the link is usable. A photo arriving must not arm a
    //: button whose link was just refused: the file is fine, the link is not.
    var usable = false;
    //: The early clock-out figures the server sent with its refusal, while the worker is
    //: being asked. Kept so the question can be rewritten in another language without a
    //: second request - the numbers do not change because the reader did.
    var pendingEarly = null;

    var $ = function (id) { return document.getElementById(id); };

    //: How to write the message box's current sentence again, in whatever language is
    //: chosen now.
    var resay = function () {};

    /**
     * Writes the message box, and keeps the way to write it again.
     *
     * This box is not a ``data-t`` node: its sentence is chosen when the server answers or
     * a photo is refused, so the language switch cannot repaint it on its own. ``again`` is
     * that way back, which is why every writer below hands one over - a worker who reads a
     * refusal and then taps Urdu must not be left reading the English it arrived in.
     */
    function say(text, kind, again) {
        var box = $("message");
        box.className = "msg " + (kind || "");
        box.textContent = text;
        resay = again || function () {};
    }

    /** Says a sentence the page can produce again - a refusal, or a photo policy line. */
    function sayAgain(produce, kind) {
        var again = function () { say(produce(), kind, again); };
        again();
    }

    /** Says a sentence from the page's own table. */
    function sayKey(key, kind, vars) {
        sayAgain(function () { return Capture.t(key, vars); }, kind);
    }

    /**
     * Says a refused answer, from the reason it carries rather than from its prose.
     *
     * ``Capture.serverMessage`` resolves the ``error_code`` against this page's table, so
     * the sentence is the reader's - and the re-say above means switching language rewrites
     * it rather than leaving the server's English behind.
     */
    function sayRefusal(body, fallbackKey) {
        var detail = body && body.detail;
        sayAgain(function () { return Capture.serverMessage(detail, fallbackKey); }, "err");
    }

    function describeState() {
        if (!info) return;
        if (info.clocked_in) {
            $("state").textContent = Capture.t("quick.state.in");
            $("state-detail").textContent = Capture.t("quick.state.since", {
                time: info.clock_in_time,
                site: info.open_shift_site ? Capture.t("quick.state.site", { site: info.open_shift_site }) : ""
            });
        } else {
            $("state").textContent = Capture.t("quick.state.out");
            $("state-detail").textContent = Capture.t("quick.state.closed");
        }
        $("btn-punch").textContent = info.next_action === "Clock Out"
            ? Capture.t("quick.btn.out") : Capture.t("quick.btn.in");
        if (info.remaining_uses !== null && info.remaining_uses !== undefined) {
            $("state-detail").textContent += Capture.t("quick.state.remaining", { n: info.remaining_uses });
        }
    }

    /**
     * Who the link is for, and when it stops working.
     *
     * The name and the id are the server's and are escaped: they are strings an
     * administrator typed, and this line is assembled as markup because of the bold name.
     */
    function describeLink() {
        var who = Capture.esc(info.worker_name);
        var id = Capture.esc(info.worker_id);
        $("who").innerHTML = Capture.t("quick.for", { name: "<strong>" + who + "</strong>", id: id }) +
            " · <span class='pill'>" + Capture.esc(Capture.t("quick.expires", { date: info.expires_at })) + "</span>";
    }

    function load() {
        fetch(API + "/q/" + encodeURIComponent(token))
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (!res.ok) {
                    // A dead link (revoked, expired, used up, or an account that was
                    // deactivated) is a final answer, not a retry: the page says which of
                    // those it is and disables the tap.
                    sayRefusal(res.body, "link.invalid");
                    $("btn-punch").disabled = true;
                    $("btn-start").disabled = true;
                    $("fallback-label").classList.add("hidden");
                    // And the location line goes with it. Nothing is asked for a link the
                    // server has already refused, and the last thing that line would say
                    // is "Allow location and try again" - an instruction that cannot change
                    // the answer, sent to somebody who will go into their phone's settings
                    // and come back to the same refusal.
                    $("location-line").classList.add("hidden");
                    return;
                }
                info = res.body;
                usable = true;
                if (info.photo_policy) Capture.setPolicy(info.photo_policy);
                Capture.applyPolicy();
                describeLink();
                describeState();
                // Asked for only now that the link is known good: the fix is for the punch
                // that is about to be sent, and a refused link never sends one.
                Capture.locate();
            })
            .catch(function () { sayKey("offline", "err"); });
    }

    /** The question, in the reader's language, from the server's two figures. */
    function earlyBodyText(detail) {
        return Capture.t("quick.earlyBody", {
            paid: Number((detail && detail.paid_hours) || 0).toFixed(2),
            regular: Number((detail && detail.regular_hours) || 0).toFixed(2)
        });
    }

    /**
     * Ask before recording a shift that is short of the paid day.
     *
     * The tap was not refused, it was questioned: the server stopped before it wrote the
     * row or closed the shift, so nothing has been recorded at the moment this appears.
     */
    function askEarly(detail) {
        pendingEarly = detail;
        $("early-title").textContent = Capture.t("quick.earlyTitle");
        $("early-body").textContent = earlyBodyText(detail);
        $("btn-early-confirm").textContent = Capture.t("quick.earlyConfirm");
        $("btn-early-cancel").textContent = Capture.t("quick.earlyCancel");
        $("early-confirm").classList.remove("hidden");
        say("", "");
        // The punch button stays disabled while the question is up: the two buttons below
        // are the only way forward, and a live punch button would let a second tap race
        // the answer.
        $("btn-punch").disabled = true;
    }

    function answerEarly(confirmed) {
        $("early-confirm").classList.add("hidden");
        pendingEarly = null;
        if (confirmed) {
            // The selfie is still on the page - only a *recorded* tap clears it - so the
            // answer is the same tap again with the flag the server asked for.
            punch(true);
            return;
        }
        // Cancelled: the shift is exactly as it was, so the button comes back and the
        // worker can take the selfie again or simply leave.
        describeState();
        $("btn-punch").disabled = false;
    }

    function punch(confirmed) {
        var problem = camera.problem();
        if (problem) {
            camera.showProblem(problem);
            // Asked again rather than remembered: the sentence is the policy's, in the
            // language that is chosen when it is written.
            sayAgain(function () { return camera.problem() || problem; }, "err");
            return;
        }
        $("btn-punch").disabled = true;
        sayKey("quick.recording", "ok");
        Capture.locate().then(function (position) {
            if (!position) {
                sayKey("quick.noFix", "err");
                $("btn-punch").disabled = false;
                describeState();
                return;
            }
            var form = new FormData();
            form.append("selfie", camera.photo(), "selfie.jpg");
            form.append("lat", String(position.lat));
            form.append("lon", String(position.lon));
            if (position.accuracy !== null && position.accuracy !== undefined) {
                form.append("accuracy", String(position.accuracy));
            }
            // Only on the second attempt: the server decides whether a shift is short, and
            // this is the page saying the worker was told and meant it.
            if (confirmed) form.append("confirm_early_checkout", "1");
            fetch(API + "/q/" + encodeURIComponent(token), { method: "POST", body: form })
                .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
                .then(function (res) {
                    if (!res.ok) {
                        var detail = res.body && res.body.detail;
                        // The one refusal on this page that is a question rather than an
                        // answer. Everything else is shown as the error it is.
                        if (detail && detail.error_code === "confirm_early_checkout") {
                            askEarly(detail);
                            return;
                        }
                        sayRefusal(res.body, "quick.notRecorded");
                        $("btn-punch").disabled = false;
                        return;
                    }
                    var body = res.body;
                    var hours = Number(body.hours || 0);
                    // Composed rather than a single key, and from the server's own figures,
                    // so it is written again from the same answer when the language changes.
                    sayAgain(function () {
                        return body.action === "Clock Out"
                            ? Capture.t("quick.clockedOut", { site: body.site, hours: hours.toFixed(2) }) +
                              (body.break_hours
                                  ? Capture.t("quick.break", { min: Math.round(body.break_hours * 60) })
                                  : "") + "."
                            : Capture.t("quick.clockedInAt", { site: body.site });
                    }, "ok");
                    camera.clear();
                    load();
                })
                .catch(function () {
                    sayKey("quick.notReached", "err");
                    $("btn-punch").disabled = false;
                });
        });
    }

    function boot() {
        Capture.applyDirection();
        Capture.translate(document);
        document.title = Capture.t("quick.headTitle");
        $("credit").textContent = Capture.credit();
        Capture.wireLanguagePicker(function () {
            // Everything on this screen is a sentence this file just wrote, so the repaint
            // is the same describe() calls that wrote them - and nothing has to be fetched.
            document.title = Capture.t("quick.headTitle");
            $("credit").textContent = Capture.credit();
            Capture.applyPolicy();
            describeState();
            if (info) describeLink();
            // The early clock-out question, if it is on screen, in the other language - from
            // the same figures the server sent, not from a second request.
            if (pendingEarly) $("early-body").textContent = earlyBodyText(pendingEarly);
            // The message box is not a ``data-t`` node, so it is re-said here from whatever
            // wrote it - a refusal in the language it arrived in is the leak this closes.
            resay();
            // The fix, or the reason there is none - said again in the other language. It is
            // not re-requested: the coordinates a punch is sent with do not change because
            // the reader changed language.
            Capture.repaintLocation();
        });

        camera = Capture.createCamera({
            primary: "btn-punch",
            family: "selfie",
            readyKey: "camera.sendSelfie",
            allow: function () { return usable; },
            onBlocked: function () { sayKey("camera.blocked.punch", "err"); }
        });

        $("btn-punch").addEventListener("click", function () { punch(false); });
        $("btn-early-confirm").addEventListener("click", function () { answerEarly(true); });
        $("btn-early-cancel").addEventListener("click", function () { answerEarly(false); });
        $("btn-start").addEventListener("click", function () { camera.start(); });
        $("btn-shoot").addEventListener("click", function () { camera.shoot(); });
        $("btn-retake").addEventListener("click", function () { camera.retake(); });
        $("file").addEventListener("change", function (event) { camera.chooseFromInput(event.target); });

        Capture.applyPolicy();
        load();
    }

    boot();
})();
