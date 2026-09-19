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

    function say(text, kind) {
        var box = $("message");
        box.className = "msg " + (kind || "");
        box.textContent = text;
    }

    /** The server's refusal, in the server's words, which are not translated here. */
    function detailOf(body, fallbackKey) {
        var detail = body && body.detail;
        var text = (detail && detail.message) || detail || Capture.t(fallbackKey);
        var code = detail && detail.error_code ? " (" + detail.error_code + ")" : "";
        return text + code;
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
                    say(detailOf(res.body, "link.invalid"), "err");
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
                    say(Capture.t("enroll.unusable", { status: res.body.status }), "err");
                    $("btn-start").disabled = true;
                    $("btn-submit").disabled = true;
                    $("password-card").classList.add("hidden");
                }
            })
            .catch(function () { say(Capture.t("offline"), "err"); });
    }

    function submit() {
        var problem = camera.problem();
        if (problem) {
            camera.showProblem(problem);
            say(problem, "err");
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
                say(Capture.t("enroll.passwordShort", { n: minPasswordLength }), "err");
                return;
            }
            if (password !== again) {
                say(Capture.t("enroll.passwordMismatch"), "err");
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
                        say(Capture.t("enroll.done.register"), "ok");
                        $("password-card").classList.add("hidden");
                    } else {
                        say(Capture.t("enroll.done.enroll"), "ok");
                    }
                    var live = res.body.liveness || {};
                    $("status-line").textContent = Capture.t("enroll.liveness", { verdict: live.verdict || "n/a" });
                    $("btn-retake").classList.add("hidden");
                    return;
                }
                say(detailOf(res.body, "enroll.failed"), "err");
                $("btn-submit").disabled = false;
                $("status-line").textContent = "";
            })
            .catch(function () {
                say(Capture.t("enroll.uploadFailed"), "err");
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
            onBlocked: function () { say(Capture.t("camera.blocked.enroll"), "err"); }
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
