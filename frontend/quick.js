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

    var $ = function (id) { return document.getElementById(id); };

    function say(text, kind) {
        var box = $("message");
        box.className = "msg " + (kind || "");
        box.textContent = text;
    }

    /** The server's refusal, in the server's words, with its code when it has one. */
    function detailOf(body, fallbackKey) {
        var detail = body && body.detail;
        var text = (detail && detail.message) || detail || Capture.t(fallbackKey);
        var code = detail && detail.error_code ? " (" + detail.error_code + ")" : "";
        return text + code;
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
                    say(detailOf(res.body, "link.invalid"), "err");
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
            .catch(function () { say(Capture.t("offline"), "err"); });
    }

    function punch() {
        var problem = camera.problem();
        if (problem) {
            camera.showProblem(problem);
            say(problem, "err");
            return;
        }
        $("btn-punch").disabled = true;
        say(Capture.t("quick.recording"), "ok");
        Capture.locate().then(function (position) {
            if (!position) {
                say(Capture.t("quick.noFix"), "err");
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
            fetch(API + "/q/" + encodeURIComponent(token), { method: "POST", body: form })
                .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
                .then(function (res) {
                    if (!res.ok) {
                        say(detailOf(res.body, "quick.notRecorded"), "err");
                        $("btn-punch").disabled = false;
                        return;
                    }
                    var body = res.body;
                    var hours = Number(body.hours || 0);
                    say(
                        body.action === "Clock Out"
                            ? Capture.t("quick.clockedOut", { site: body.site, hours: hours.toFixed(2) }) +
                              (body.break_hours
                                  ? Capture.t("quick.break", { min: Math.round(body.break_hours * 60) })
                                  : "") + "."
                            : Capture.t("quick.clockedInAt", { site: body.site }),
                        "ok"
                    );
                    camera.clear();
                    load();
                })
                .catch(function () {
                    say(Capture.t("quick.notReached"), "err");
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
            onBlocked: function () { say(Capture.t("camera.blocked.punch"), "err"); }
        });

        $("btn-punch").addEventListener("click", punch);
        $("btn-start").addEventListener("click", function () { camera.start(); });
        $("btn-shoot").addEventListener("click", function () { camera.shoot(); });
        $("btn-retake").addEventListener("click", function () { camera.retake(); });
        $("file").addEventListener("change", function (event) { camera.chooseFromInput(event.target); });

        Capture.applyPolicy();
        load();
    }

    boot();
})();
