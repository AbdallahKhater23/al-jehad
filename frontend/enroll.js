/**
 * The page an enrollment link opens.
 *
 * The camera, the photo policy and every sentence about a photo are ``capture.js``, shared
 * with the punch page; what is left here is this page's own flow: who the link is for, and
 * the upload.
 *
 * ONE KIND OF LINK, AND THAT IS THE DESIGN. An invite registers the face of an account that
 * already exists - an administrator creates the account in the console, or approves it from
 * the site's registration link, and this page is where its owner takes the photo the gate
 * will later compare them against. The link that used to create the account itself, with the
 * visitor choosing its password, is gone: this page no longer has a password field, and a
 * row one of those links left behind (still live, still valid) is refused here rather than
 * offered a form that has nowhere to submit to. Every account is created by a person who is
 * signed in, or by a reviewer who looked at a face first.
 */
(function () {
    "use strict";

    //: Base URLs are owned by ``api-config.js``, loaded by the page before this file:
    //: production Worker origin, the direct-Railway diagnostics origin, and the normaliser.
    //: Endpoint paths here carry no ``/api/v1`` prefix - this constant owns it, exactly
    //: once, so a request can never become ``/api/v1/api/v1/...``.
    var API = (typeof resolveAPIBase === "function")
        ? resolveAPIBase()
        : (location.origin + "/api/v1");
    var token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
    //: The server's answer, kept for the lines that are a sentence about it - the link's
    //: own header, and the verb it reads with. Every other line on this page is read off
    //: state that does not change.
    var invite = null;
    //: Whether the who-line reads as something the link does ("Enrolling Ahmed") or as a
    //: plain statement of who it is about. A usable link is about to enroll a face; a link
    //: this page refuses is about nobody, so it says whose account it named and nothing
    //: more - "Enrolling" over a refusal is a sentence that contradicts the box below it.
    var whoVerb = true;
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
     * Takes the page out of service: no camera, no submit, no gallery.
     *
     * One shape for every way a link can be refused - unknown, expired, revoked, spent, or
     * the retired kind - because they all mean the same thing to the person holding the
     * phone: there is nothing to do here, and no photo will be taken.
     */
    function disarm() {
        $("btn-start").disabled = true;
        $("btn-submit").disabled = true;
        $("fallback-label").classList.add("hidden");
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
        var account = "<strong>" + Capture.esc(invite.worker_name) + "</strong> (id " +
            Capture.esc(invite.worker_id) + ") · <span class='pill'>" +
            Capture.esc(Capture.t("quick.expires", { date: invite.expires_at })) + "</span>";
        $("who").innerHTML = whoVerb
            ? Capture.t("enroll.verb.enroll") + " " + account
            : account;
    }

    /**
     * The page's own title and the one button that sends a photo.
     *
     * Read off state that does not change, so the language switch can repaint it: there is
     * one link to be on this page at all, which is why there is nothing here to branch on.
     */
    function describeInvite() {
        $("title").textContent = Capture.t("enroll.title");
        $("step-intro").classList.remove("hidden");
        $("btn-submit").textContent = Capture.t("enroll.submit");
    }

    function loadInvite() {
        fetch(API + "/enroll/" + encodeURIComponent(token))
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (!res.ok) {
                    sayRefusal(res.body && res.body.detail, "link.invalid");
                    disarm();
                    return;
                }

                invite = res.body;
                whoVerb = false;
                describeWho();

                if (!res.body.usable) {
                    // The peek at the invite reports the same three reasons as the punch
                    // route, as ``status`` rather than as an ``error_code``.
                    sayRefusal(res.body, "enroll.unusable");
                    disarm();
                    return;
                }
                if (res.body.kind !== "enroll") {
                    // A link issued before the registration flow was removed is still live
                    // and still valid, and its account may never have been created at all -
                    // so it is refused here, before the camera is armed, rather than left
                    // for a submit that cannot work. No photo is taken for it.
                    sayKey("enroll.retired", "err");
                    disarm();
                    return;
                }

                usable = true;
                whoVerb = true;
                describeWho();
                describeInvite();
                if (res.body.photo_policy) Capture.setPolicy(res.body.photo_policy);
                Capture.applyPolicy();
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

        $("btn-submit").disabled = true;
        $("status-line").textContent = Capture.t("enroll.uploading");
        fetch(url, { method: "POST", body: form })
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (res.ok) {
                    sayKey("enroll.done.enroll", "ok");
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
            describeInvite();
            // The message box is not a ``data-t`` node, so it is re-said here from whatever
            // wrote it - a refusal in the language it arrived in is the leak this closes.
            resay();
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
