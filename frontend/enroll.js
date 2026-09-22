/**
 * The page an enrollment link opens.
 *
 * The camera, the photo policy and every sentence about a photo are ``capture.js``, shared
 * with the punch page; what is left here is this page's own flow: which link this is
 * (enroll or register), the password the registration link owns, and the upload.
 */
(function () {
    "use strict";

    var API = "/api/v1";
    var token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
    var minPasswordLength = 8;
    var isRegister = false;
    var workerId = "";
    //: The server's answer, kept for the lines that are a sentence about it - the link's
    //: own header. Every other line on this page is read off state that does not change.
    var invite = null;
    //: False until the server has said the link is usable - see the same guard on the punch
    //: page: an unusable link must not be re-armed by a photo arriving.
    var usable = false;
    var camera = null;

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
     * ``Capture.serverMessage`` resolves the ``error_code`` - or the ``status`` of a peek at
     * the invite - against this page's table, so the sentence is the reader's, and the
     * re-say means switching language rewrites it rather than leaving the English behind.
     */
    function sayRefusal(body, fallbackKey) {
        sayAgain(function () { return Capture.serverMessage(body, fallbackKey); }, "err");
    }

    /**
     * Who the link is for, and when it stops working.
     *
     * Assembled from the server's answer rather than left to the ``data-t`` pass: the
     * translation replaces an element's text, so a language switch would otherwise leave
     * this line reading "Checking your link…" over a link that had already been checked.
     * The name and the id are the server's, and are escaped.
     */
    function describeWho() {
        if (!invite) return;
        var verb = Capture.t(isRegister ? "enroll.verb.register" : "enroll.verb.enroll");
        $("who").innerHTML = verb + " <strong>" + Capture.esc(invite.worker_name) + "</strong> (id " +
            Capture.esc(invite.worker_id) + ") · <span class='pill'>" +
            Capture.esc(Capture.t("quick.expires", { date: invite.expires_at })) + "</span>";
    }

    function describeInvite() {
        if (!isRegister) {
            $("title").textContent = Capture.t("enroll.title");
            $("step-intro").classList.remove("hidden");
            $("password-card").classList.add("hidden");
            $("btn-submit").textContent = Capture.t("enroll.submit");
        }
    }

    function loadInvite() {
        fetch(API + "/enroll/" + encodeURIComponent(token))
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (!res.ok) {
                    sayRefusal(res.body && res.body.detail, "link.invalid");
                    $("btn-start").disabled = true;
                    $("btn-submit").disabled = true;
                    $("fallback-label").classList.add("hidden");
                    return;
                }
                usable = true;
                if (res.body.photo_policy) Capture.setPolicy(res.body.photo_policy);
                if (res.body.min_password_length) minPasswordLength = Number(res.body.min_password_length);
                Capture.applyPolicy();

                isRegister = res.body.kind === "register";
                workerId = res.body.worker_id;
                invite = res.body;
                describeWho();

                if (isRegister) {
                    // The password is the visitor's own choice and nobody can reset it for
                    // them, so the rule is stated before they type rather than after they
                    // submit.
                    $("title").textContent = Capture.t("enroll.registerTitle");
                    $("step-intro").classList.add("hidden");
                    $("password-card").classList.remove("hidden");
                    $("password-hint").textContent = Capture.t("enroll.password.hint", {
                        n: minPasswordLength, id: res.body.worker_id
                    });
                    $("btn-submit").textContent = Capture.t("enroll.create");
                } else {
                    describeInvite();
                }

                if (!res.body.usable) {
                    // The peek at the invite reports the same three reasons as the punch
                    // route, as ``status`` rather than as an ``error_code``.
                    sayRefusal(res.body, "enroll.unusable");
                    $("btn-start").disabled = true;
                    $("btn-submit").disabled = true;
                    $("password-card").classList.add("hidden");
                }
            })
            .catch(function () { sayKey("offline", "err"); });
    }

    function submit() {
        var problem = camera.problem();
        if (problem) {
            camera.showProblem(problem);
            // Asked again rather than remembered: the sentence is the policy's, in the
            // language that is chosen when it is written.
            sayAgain(function () { return camera.problem() || problem; }, "err");
            return;
        }
        var form = new FormData();
        form.append("photo", camera.photo(), "photo.jpg");
        form.append("phone", $("phone").value || "");
        form.append("email", $("email").value || "");
        var url = API + "/enroll/" + encodeURIComponent(token);

        if (isRegister) {
            var password = $("password").value || "";
            var again = $("password2").value || "";
            if (password.length < minPasswordLength) {
                sayKey("enroll.passwordShort", "err", { n: minPasswordLength });
                return;
            }
            if (password !== again) {
                sayKey("enroll.passwordMismatch", "err");
                return;
            }
            form.append("password", password);
            url += "/register";
        }

        $("btn-submit").disabled = true;
        $("status-line").textContent = Capture.t("enroll.uploading");
        fetch(url, { method: "POST", body: form })
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (res.ok) {
                    if (isRegister) {
                        sayKey("enroll.done.register", "ok");
                        $("password-card").classList.add("hidden");
                    } else {
                        sayKey("enroll.done.enroll", "ok");
                    }
                    var live = res.body.liveness || {};
                    $("status-line").textContent = Capture.t("enroll.liveness", { verdict: live.verdict || "n/a" });
                    $("btn-retake").classList.add("hidden");
                    return;
                }
                sayRefusal(res.body && res.body.detail, "enroll.failed");
                $("btn-submit").disabled = false;
                $("status-line").textContent = "";
            })
            .catch(function () {
                sayKey("enroll.uploadFailed", "err");
                $("btn-submit").disabled = false;
            });
    }

    function boot() {
        Capture.applyDirection();
        Capture.translate(document);
        document.title = Capture.t("enroll.headTitle");
        $("credit").textContent = Capture.credit();
        Capture.wireLanguagePicker(function () {
            document.title = Capture.t("enroll.headTitle");
            $("credit").textContent = Capture.credit();
            Capture.applyPolicy();
            describeWho();
            // The message box is not a ``data-t`` node, so it is re-said here from whatever
            // wrote it - a refusal in the language it arrived in is the leak this closes.
            resay();
            // The intro, the title and the submit button are read off the same state the
            // first paint read them off - the link's kind, which does not change.
            if (isRegister) {
                $("title").textContent = Capture.t("enroll.registerTitle");
                $("password-hint").textContent = Capture.t("enroll.password.hint", {
                    n: minPasswordLength, id: workerId
                });
                $("btn-submit").textContent = Capture.t("enroll.create");
            } else {
                describeInvite();
            }
        });

        camera = Capture.createCamera({
            primary: "btn-submit",
            family: "photo",
            allow: function () { return usable; },
            onBlocked: function () { sayKey("camera.blocked.enroll", "err"); }
        });

        $("btn-start").addEventListener("click", function () { camera.start(); });
        $("btn-shoot").addEventListener("click", function () { camera.shoot(); });
        $("btn-retake").addEventListener("click", function () { camera.retake(); });
        $("btn-submit").addEventListener("click", submit);
        $("file").addEventListener("change", function (event) { camera.chooseFromInput(event.target); });

        Capture.applyPolicy();
        loadInvite();
    }

    boot();
})();
