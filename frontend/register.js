/**
 * The page the console's registration link opens.
 *
 * WHAT IT IS SERVED UNDER
 * -----------------------
 * ``/register/<token>``, where the token is the link an administrator copied out of the
 * Credentials tab. The page reads it out of its own path and sends it with both calls, so the
 * address it can be reached at is the link itself: there is no un-tokened form to open, and a
 * copy of the link that has been replaced stops working the moment the console replaces it.
 * The bare ``/register`` is served as well - deliberately, and this file is why - so that
 * somebody who typed the address from memory, or whose chat client cut the link in half, reads
 * one sentence about asking for a link rather than a browser error page. That is the whole of
 * ``noLink`` below.
 *
 * WHY THIS EXISTS
 * ---------------
 * ``GET /register/<token>`` publishes the policy and ``POST /register/<token>`` takes a
 * submission, creating the account it is for as ``pending_approval``; an administrator's
 * approval is what lets that account record attendance. What this file is is the half a person
 * uses - nobody without a session could fill anything in, so applications could only be filed by
 * hand.
 *
 * WHAT IT PRODUCES
 * ----------------
 * An account. The submission carries a name, a password, a role, contact details, an optional
 * line about the work and a photograph; the server creates the user immediately, quarantines it
 * as ``pending_approval``, and answers with the id - which this page puts on the screen, because
 * it is what the person signs in with. What the account cannot do until an administrator
 * approves it is record attendance, and that is refused at the punch itself rather than here.
 *
 * ONE KIND OF DECIDING
 * --------------------
 * The page refuses only what it can decide alone: a missing name, a password under the floor
 * the server published, two passwords that differ, an unticked consent line, and a photo that
 * is not one. Everything else - the switch, the role, a full queue, a photograph that could not
 * be read - is the server's answer, and each one is spoken in the reader's language off its
 * ``error_code`` (``Capture.serverMessage``) rather than off the English prose it arrives in.
 *
 * The camera, the photo policy and the language tables are ``capture.js``, shared with the
 * two link pages; what is left here is this page's own flow.
 */
(function () {
    "use strict";

    //: Base URLs are owned by ``api-config.js``, loaded by the page before this file.
    //: Endpoint paths here carry no ``/api/v1`` prefix - this constant owns it, exactly once.
    var API = (typeof resolveAPIBase === "function")
        ? resolveAPIBase()
        : (location.origin + "/api/v1");

    var $ = function (id) { return document.getElementById(id); };

    /**
     * The link's token, out of this page's own path: ``/register/<token>``.
     *
     * Empty at ``/register``, which is a page that says so rather than a form. The token is not
     * validated here and could not be: what makes it a link is a signature only the server holds
     * the key to, so this page sends it and reads the answer - a 404 is the server saying the
     * link was replaced or never existed, and it arrives through ``sayRefusal`` like every other
     * refusal.
     */
    var TOKEN = (function () {
        var path = String(location.pathname || "");
        var match = path.match(/\/register\/([^/]+)\/?$/);
        return match ? decodeURIComponent(match[1]) : "";
    }());

    //: Where both calls go: the link's own address, under the API prefix.
    var ENDPOINT = API + "/register/" + encodeURIComponent(TOKEN);

    //: The server's answer to ``GET /register/<token>``: the roles this link may register, the
    //: photo policy, and the shortest password. Null until it arrives, which is why nothing the
    //: applicant types is checked against it before then.
    var policy = null;

    //: Whether intake is open. The camera and the submit both wait on it, so a closed link
    //: cannot be armed by a photo arriving later.
    var open = false;

    var camera = null;

    //: How to write the message box's current sentence again, in whatever language is chosen
    //: now: the box is not a ``data-t`` node, so the switch cannot repaint it on its own.
    var resay = function () {};

    function say(text, kind, again) {
        var box = $("message");
        box.className = "msg " + (kind || "");
        box.textContent = text;
        resay = again || function () {};
    }

    /** Says a sentence the page can produce again - a refusal, or a line about the form. */
    function sayAgain(produce, kind) {
        var again = function () { say(produce(), kind, again); };
        again();
    }

    /** Says a sentence from the page's own table. */
    function sayKey(key, kind, vars) {
        sayAgain(function () { return Capture.t(key, vars); }, kind);
    }

    /** Says a refused answer, from the reason it carries rather than from its prose. */
    function sayRefusal(body, fallbackKey) {
        sayAgain(function () { return Capture.serverMessage(body, fallbackKey); }, "err");
    }

    /**
     * Takes the page out of service: no form to fill, no camera, no submit.
     *
     * One shape for both ways this page can be closed - intake switched off, and a request
     * that has already been sent - because they mean the same thing to the person holding the
     * phone: there is nothing more to do here, and no second submission.
     */
    function disarm() {
        open = false;
        $("identity").classList.add("hidden");
        $("photo").classList.add("hidden");
        $("fallback-label").classList.add("hidden");
        if (camera) camera.arm(false);
    }

    /**
     * The roles this link may register, in the server's order and the reader's words.
     *
     * Built from the published list rather than written into the markup, so a role the server
     * would refuse at submit is never offered here - and so a role added to the business set
     * appears on this page without an edit. The names are words, so the language switch
     * refills them.
     */
    function fillRoles(roles) {
        var select = $("role");
        select.innerHTML = "";
        for (var i = 0; i < roles.length; i += 1) {
            var option = document.createElement("option");
            option.value = roles[i];
            option.textContent = Capture.t("role." + roles[i]);
            select.appendChild(option);
        }
    }

    /**
     * The receipt: the account exists, the number is on the screen, and the form is gone.
     *
     * THE NUMBER IS THE WHOLE POINT
     * -----------------------------
     * ``POST /register`` answers with the id the account was created under, and that id is what
     * this person signs in with for as long as they work here. It is what makes this page
     * different from the one that used to say "we have your request": the applicant is not
     * anonymous to the system any more, so they can check on themselves instead of waiting to be
     * told. It is set in the largest type on the page, because the alternative - a number in a
     * sentence that scrolls away - is somebody who has to telephone an administrator to find out
     * who they are.
     *
     * The form goes away with it (``disarm``), so a second tap cannot become a second account,
     * and so the only thing left on the page is the way to the sign-in screen.
     */
    function showDone(answer) {
        $("done-id").textContent = String((answer && answer.user_id) || "");
        $("step-intro").classList.add("hidden");
        $("send").classList.add("hidden");
        $("done").classList.remove("hidden");
        disarm();
        // The sentence in the message box, and the card beside it, say different things on
        // purpose: the box is the server's answer ("your account is created and waiting"), and
        // the card is what to do with the number - including the part that will refuse them at
        // the gate until an administrator approves.
        sayKey("register.done", "ok");
    }

    function loadPolicy() {
        fetch(ENDPOINT)
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (!res.ok) {
                    // A policy read that fails is not a closed intake: it is a page that
                    // cannot know what it is allowed to ask for, so it asks for nothing.
                    sayKey("offline", "err");
                    disarm();
                    return;
                }
                policy = res.body;
                if (res.body.photo_policy) Capture.setPolicy(res.body.photo_policy);
                Capture.applyPolicy();

                if (!res.body.enabled) {
                    // Off is the default, and the link is permanent: a page that has been
                    // switched off has to be able to say so rather than look broken.
                    sayKey("register.closed", "err");
                    disarm();
                    return;
                }

                fillRoles(res.body.roles || []);
                open = true;
                $("btn-submit").disabled = true;
            })
            .catch(function () {
                sayKey("offline", "err");
                disarm();
            });
    }

    /**
     * What the page refuses on its own, or an empty string when there is nothing to refuse.
     *
     * Asked fresh each time rather than remembered, so a refusal is written in the language
     * that is chosen when it is read - and so fixing one thing cannot leave a stale sentence
     * about the thing next to it.
     */
    function complaint() {
        if (!$("full-name").value.trim()) return Capture.t("register.nameRequired");
        var floor = (policy && policy.min_password_length) || 0;
        if (floor && $("password").value.length < floor) {
            return Capture.t("register.passwordShort", { n: floor });
        }
        if ($("password").value !== $("password2").value) return Capture.t("register.passwordMismatch");
        if (!$("consent").checked) return Capture.t("register.consentRequired");
        return "";
    }

    function submit() {
        var problem = complaint() || camera.problem();
        if (problem) {
            // The photo line belongs to the photo policy and is written where the photo is;
            // every other refusal is the form's and goes in the message box. One sentence in
            // both places would read as two complaints about one tap.
            var photoOnly = camera.problem();
            camera.showProblem(photoOnly || "");
            sayAgain(function () { return complaint() || camera.problem() || problem; }, "err");
            return;
        }

        var form = new FormData();
        form.append("full_name", $("full-name").value.trim());
        form.append("password", $("password").value);
        form.append("role", $("role").value);
        form.append("phone", $("phone").value || "");
        form.append("email", $("email").value || "");
        form.append("work_details", $("work").value || "");
        // Truthy by the server's own list, and a tick is the only thing that sets it.
        form.append("consent", $("consent").checked ? "true" : "");
        form.append("photo", camera.photo(), "photo.jpg");

        if (camera) camera.arm(false);
        $("status-line").textContent = Capture.t("register.sending");

        fetch(ENDPOINT, { method: "POST", body: form })
            .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                $("status-line").textContent = "";
                if (res.ok) {
                    // The account exists now: the form goes away, so a second tap cannot become
                    // a second account, and the id it was created under takes its place.
                    showDone(res.body);
                    return;
                }
                sayRefusal(res.body && res.body.detail, "register.failed");
                if (camera) camera.arm(true);
            })
            .catch(function () {
                sayKey("register.uploadFailed", "err");
                $("status-line").textContent = "";
                if (camera) camera.arm(true);
            });
    }

    function boot() {
        Capture.applyDirection();
        Capture.translate(document);
        document.title = Capture.t("register.headTitle");
        $("credit").textContent = Capture.credit();
        Capture.wireLanguagePicker(function () {
            document.title = Capture.t("register.headTitle");
            $("credit").textContent = Capture.credit();
            Capture.applyPolicy();
            // The role names are words too, so the list is rebuilt in the new language.
            if (policy) fillRoles(policy.roles || []);
            // The message box is not a ``data-t`` node: a refusal written in the language it
            // arrived in is the leak this closes.
            resay();
        });

        camera = Capture.createCamera({
            primary: "btn-submit",
            family: "photo",
            allow: function () { return open; },
            onBlocked: function () { sayKey("camera.blocked.enroll", "err"); }
        });

        $("btn-start").addEventListener("click", function () { camera.start(); });
        $("btn-shoot").addEventListener("click", function () { camera.shoot(); });
        $("btn-retake").addEventListener("click", function () { camera.retake(); });
        $("btn-submit").addEventListener("click", submit);
        $("file").addEventListener("change", function (event) { camera.chooseFromInput(event.target); });

        // No token is not a closed form and not a broken one, it is a person who reached this
        // address without the link - so the page answers with the one thing that is true and
        // useful ("ask your administrator for a link") and asks the server nothing at all. The
        // intro card goes with the form: it explains what a submission does, and there is no
        // submission here to explain.
        if (!TOKEN) {
            sayKey("register.needLink", "err");
            $("step-intro").classList.add("hidden");
            disarm();
            return;
        }

        Capture.applyPolicy();
        loadPolicy();
    }

    boot();
})();
