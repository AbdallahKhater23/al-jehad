"""Defects found by driving the real console in a real browser.

They are here because they are all invisible to a backend test - the server answers
correctly in every one of them - and because each one is a *silent* failure of the kind
that survives review:

1. a 401 that is about a *body* credential must not be reported as a dead session: the
   clock prompt used to re-check the worker's password, and mistyping it signed them out
   mid-punch with "your session expired", which reads like a server fault and hides the
   one thing they can fix. That prompt is gone (`/attendance/verify` takes no password),
   so what is left of the rule is the login form and its "Invalid credentials or user
   ID" - and the reverse case below: a 401 on the punch now *is* a dead session, and it
   has to take the camera overlay down with it instead of leaving a shutter over the
   login screen;
1b. the punch itself carries no password - the overlay must not ask for one, and the form
   must not send one, because the bearer token is the credential;
1c. a photo the server could not match answered 401, so the client read it as a dead
   session: the worker was signed out mid-punch, with "your session expired" instead of
   the reason, at the gate, needing the password this app exists to not ask for. It is a
   422 about the frame, and the card stays up with the server's sentence on it;
2. two quick taps on the clock button opened two camera overlays - two live streams,
   two shutters, and a second frame queued as a second punch;
3. a rejected form did nothing at all: the promise rejected into the console, so "Site
   name already exists." never reached the admin. Asserted against the site form and the
   Credentials tab's account-creation form - the second used to be an administrator form
   on the Admin tab, which is gone: accounts, administrators included, are created in one
   place now.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answer, faked ------------------------------------------

let answer = { status: 200, body: {} };
const results = {};

function envWithSession(role) {
    const env = boot();
    env.setResponder(() => answer);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: '1', name: 'Seed Worker', role: role || 'worker', token: 'tok-worker'
    }) + ')');
    return env;
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

// Two overrides, because this suite is about what the page does *with* a punch rather than
// about taking one: it calls ``submitAttendance`` directly, so it never needs a video with a
// frame on it. A suite about the capture itself uses the camera the harness installs instead
// (see ``test_frontend_punch_capture``).
//
// The DOM patch this function used to carry - a canvas that could make a JPEG, an element whose
// ``remove()`` detached it, a ``getElementById`` that found an appended node - is gone: the
// harness models those now, which is what made the camera card coming down assertable at all.
function stubCamera(env) {
    env.evaluate("Location.current = async () => '30.05,31.23'");
    env.evaluate("Object.defineProperty(Camera, 'isSupported', { value: true, configurable: true })");
    env.evaluate("Camera.start = async () => ({ getTracks: () => [] })");

    return env;
}

function overlays(env) {
    return env.evaluate("(document.body.__children || []).filter((el) => el.id === 'cameraOverlay').length");
}

// 1. the punch carries no password, and a refused one takes the overlay with it
{
    const env = envWithSession();
    stubCamera(env);
    let punched = null;
    env.setResponder((url, init) => {
        if (url.indexOf('/attendance/verify') >= 0) {
            punched = Array.from(init.body.keys());
            return { status: 401, body: { detail: 'Credentials changed. Please sign in again.' } };
        }
        return { status: 200, body: {} };
    });

    await env.evaluate("UI.doAttendance('Clock In')");
    results.clock_prompt = {
        overlays: overlays(env),
        markup: env.evaluate(
            "(document.body.__children || []).filter((el) => el.id === 'cameraOverlay').map((el) => el.innerHTML).join('')"
        )
    };

    env.evaluate("document.getElementById('attendanceVideo').videoWidth = 640");
    // A stub element keeps whatever the page assigned, so the capture step is real.

    await env.evaluate("UI.submitAttendance('Clock In', '30.05,31.23')");
    await new Promise((resolve) => setTimeout(resolve, 0));
    results.attendance_401 = {
        fields: punched,
        overlays: overlays(env),
        camera_open_flag: env.evaluate('!!UI._cameraOpen'),
        token: env.evaluate('!!State.token'),
        signed_in: env.evaluate('!!State.user'),
        toasts: toasts(env)
    };
}

// 1d. a photo the server could not match is a fact about the *frame*, so the card stays
// open, the worker keeps their session, and the refusal is a sentence they can act on -
// not a number and not a logout
{
    const env = envWithSession();
    stubCamera(env);
    const sentence = 'We could not confirm that this is you. Fill the frame with your face, '
        + 'in even light, and take it again.';
    env.setResponder((url) => (url.indexOf('/attendance/verify') >= 0
        ? { status: 422, body: { detail: { error_code: 'face_mismatch', score: 0.7589, message: sentence } } }
        : { status: 200, body: {} }));
    await env.evaluate("UI.openCamera('Clock In', '30.05,31.23')");
    env.evaluate("document.getElementById('attendanceVideo').videoWidth = 640");
    await env.evaluate("UI.submitAttendance('Clock In', '30.05,31.23')");
    await new Promise((resolve) => setTimeout(resolve, 0));
    results.attendance_422 = {
        overlays: overlays(env),
        camera_open_flag: env.evaluate('!!UI._cameraOpen'),
        signed_in: env.evaluate('!!State.user'),
        shutter_live: env.evaluate("document.getElementById('captureBtn').disabled === false"),
        toasts: toasts(env)
    };
}

// 1b. a 401 from a token-protected read has always signed the worker out, with its reason
{
    const env = envWithSession();
    answer = { status: 401, body: { detail: 'Invalid token' } };
    let message = null;
    try {
        await env.evaluate("API.request('/worker/me/stats')");
    } catch (err) {
        message = err.message;
    }
    results.token_401 = {
        message: message,
        token: env.evaluate('!!State.token'),
        signed_in: env.evaluate('!!State.user')
    };
}// 1c. the login form keeps reporting a wrong password as a wrong password
{
    const env = boot();
    answer = { status: 401, body: { detail: 'Invalid credentials or user ID' } };
    env.setResponder(() => answer);
    let message = null;
    try {
        await env.evaluate("API.request('/auth/login', { method: 'POST', body: { user_id: '1' } })");
    } catch (err) {
        message = err.message;
    }
    results.login_401 = { message: message };
}

// 2. a double tap opens one camera, and a worker with no session gets told why
{
    const env = envWithSession();
    // The VM has no getUserMedia, so the two things the flow asks of the device are
    // stubbed: a location, and a camera that reports itself usable. Everything the
    // assertion is about - how many overlays a double tap produces - is the page's own
    // code, not the stub.
    env.evaluate("Location.current = async () => '30.05,31.23'");
    env.evaluate("Object.defineProperty(Camera, 'isSupported', { value: true, configurable: true })");
    env.evaluate("Camera.start = async () => ({ getTracks: () => [] })");
    const first = env.evaluate("UI.doAttendance('Clock In')");
    const second = env.evaluate("UI.doAttendance('Clock In')");
    await Promise.all([first, second]);
    results.double_tap = {
        overlays: env.evaluate("(document.body.__children || []).filter((el) => el.id === 'cameraOverlay').length"),
        open_flag: env.evaluate('!!UI._cameraOpen')
    };
}

// 3. a rejected site form reaches the admin instead of an unhandled rejection
{
    const env = envWithSession('head_admin');
    env.setResponder((url) => (url.indexOf('/admin/sites/add') >= 0
        ? { status: 400, body: { detail: 'Site name already exists.' } }
        : { status: 200, body: [] }));
    const content = env.evaluate("document.getElementById('adminContent')");
    await env.evaluate("UI_MODULES.renderSites(document.getElementById('adminContent'))");
    env.evaluate("document.getElementById('siteName').value = 'Downtown Tower A'");
    env.evaluate("document.getElementById('location').value = '30.05,31.23'");
    env.evaluate("document.getElementById('radius').value = '65'");
    let rejected = null;
    try {
        await env.evaluate("document.getElementById('addSiteForm').onsubmit({ preventDefault() {} })");
    } catch (err) {
        rejected = err.message;
    }
    results.site_form = { unhandled: rejected, toasts: toasts(env) };
}

// 3b. and a rejected account-creation form does too. The form this used to drive lived on the
// Admin tab and is gone - accounts, administrators included, are created from the Credentials
// tab - so the same property is asserted against the screen that took the job over: the server
// says no, the reason reaches the administrator, nothing rejects into the console.
{
    const env = envWithSession('head_admin');
    env.setResponder((url) => (url.indexOf('/admin/users/create') >= 0
        ? { status: 409, body: { detail: 'User ID already exists.' } }
        : { status: 200, body: url.indexOf('/admin/users') >= 0 ? [] : {} }));
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate("UI_MODULES.openCredentialsMode('create')");
    env.evaluate("document.getElementById('credentialsNewId').value = '1'");
    env.evaluate("document.getElementById('credentialsNewName').value = 'Duplicate'");
    let rejected = null;
    try {
        await env.evaluate("UI_MODULES.createCredentialsAccount()");
    } catch (err) {
        rejected = err.message;
    }
    results.create_form = { unhandled: rejected, toasts: toasts(env) };
}

// 1e. a short clock-out is refused as a *question*: the card stays up, two buttons appear
// over it, and nothing is sent until one of them is pressed
{
    const env = envWithSession();
    stubCamera(env);
    const punches = [];
    env.setResponder((url, init) => {
        if (url.indexOf('/attendance/verify') >= 0) {
            punches.push(Array.from(init.body.keys()));
            return { status: 409, body: { detail: {
                error_code: 'confirm_early_checkout',
                message: 'You have worked 7.20h of the 8.00h paid day.',
                paid_hours: 7.2, regular_hours: 8.0, short_hours: 0.8
            } } };
        }
        return { status: 200, body: {} };
    });
    await env.evaluate("UI.openCamera('Clock Out', '30.05,31.23')");
    env.evaluate("document.getElementById('attendanceVideo').videoWidth = 640");
    // Deliberately not awaited yet: this promise does not settle until the question is
    // answered, so awaiting it here would deadlock against the answer below.
    const pending = env.evaluate("UI.submitAttendance('Clock Out', '30.05,31.23')");
    await new Promise((resolve) => setTimeout(resolve, 10));

    const modal = env.evaluate("document.getElementById('modalRoot').innerHTML");
    results.early_question = {
        punches: punches.length,
        first_fields: punches[0] || [],
        has_flag_on_first: (punches[0] || []).indexOf('confirm_early_checkout') >= 0,
        over_camera: modal.indexOf('is-over-camera') >= 0,
        says_paid: modal.indexOf('7.20') >= 0,
        says_day: modal.indexOf('8.00') >= 0,
        buttons: env.evaluate(
            "['confirmEarlyOut', 'cancelEarlyOut'].filter((id) => !!document.getElementById(id).__handlers.click).length"
        ),
        camera_open_flag: env.evaluate('!!UI._cameraOpen')
    };

    env.evaluate("document.getElementById('cancelEarlyOut').__handlers.click()");
    await pending;
    results.early_cancelled = {
        punches: punches.length,
        modal_cleared: env.evaluate("document.getElementById('modalRoot').innerHTML") === '',
        camera_open_flag: env.evaluate('!!UI._cameraOpen'),
        shutter_live: env.evaluate("document.getElementById('captureBtn').disabled === false")
    };
}

// 1f. confirming repeats the very same punch, carrying the flag the server asked for
{
    const env = envWithSession();
    stubCamera(env);
    const punches = [];
    env.setResponder((url, init) => {
        if (url.indexOf('/attendance/verify') >= 0) {
            punches.push(Array.from(init.body.keys()));
            if (punches.length === 1) {
                return { status: 409, body: { detail: {
                    error_code: 'confirm_early_checkout', message: 'short',
                    paid_hours: 6.5, regular_hours: 8.0, short_hours: 1.5
                } } };
            }
            return { status: 200, body: { message: 'Clocked Out of Downtown Tower A.' } };
        }
        return { status: 200, body: {} };
    });
    await env.evaluate("UI.openCamera('Clock Out', '30.05,31.23')");
    env.evaluate("document.getElementById('attendanceVideo').videoWidth = 640");
    const pending = env.evaluate("UI.submitAttendance('Clock Out', '30.05,31.23')");
    await new Promise((resolve) => setTimeout(resolve, 10));
    env.evaluate("document.getElementById('confirmEarlyOut').__handlers.click()");
    await pending;
    results.early_confirmed = {
        punches: punches.length,
        second_fields: punches[1] || [],
        has_flag: (punches[1] || []).indexOf('confirm_early_checkout') >= 0,
        modal_cleared: env.evaluate("document.getElementById('modalRoot').innerHTML") === '',
        camera_open_flag: env.evaluate('!!UI._cameraOpen')
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_clock_prompt_asks_for_no_password(results):
    """The bearer token is the credential; a second copy of the password is not asked for."""
    prompt = results["clock_prompt"]
    assert prompt["overlays"] == 1, "the camera opened"
    assert 'type="password"' not in prompt["markup"], (
        "the clock prompt must not ask for the password again - the session already is one"
    )
    assert "confirmPass" not in prompt["markup"], "not even as an untranslated label"


def test_the_punch_carries_identity_action_location_and_a_selfie_only(results):
    outcome = results["attendance_401"]
    assert outcome["fields"] == ["worker_id", "action", "location_input", "selfie"], (
        f"the punch is what the endpoint documents, got {outcome['fields']}"
    )
    assert "password" not in outcome["fields"]


def test_a_refused_punch_ends_the_session_and_closes_the_camera(results):
    """The old failure left a shutter over the login screen with nothing left to work."""
    outcome = results["attendance_401"]
    assert outcome["signed_in"] is False, "a 401 on the punch is a dead session"
    assert outcome["token"] is False
    assert outcome["overlays"] == 0, "the camera must come down with the session"
    assert outcome["camera_open_flag"] is False
    assert outcome["toasts"] and "session expired" in outcome["toasts"][0].lower()


def test_an_unmatchable_photo_keeps_the_card_and_the_session(results):
    """A refused frame must leave the worker able to try again, and be told why."""
    outcome = results["attendance_422"]
    assert outcome["signed_in"] is True, "a mismatched face is not a dead session"
    assert outcome["overlays"] == 1, "the card must stay up for the retry"
    assert outcome["camera_open_flag"] is True
    assert outcome["shutter_live"], "the shutter must be ready for the next attempt"
    assert outcome["toasts"], outcome
    toast = outcome["toasts"][-1].lower()
    assert "could not confirm" in toast, f"the server's sentence must reach the worker, got {toast!r}"
    assert "0.75" not in toast and "score" not in toast, (
        f"a face-match score means nothing on a phone, got {toast!r}"
    )


def test_a_dead_token_still_signs_the_worker_out(results):
    """The other half of the rule: a 401 about the token is a dead session."""
    outcome = results["token_401"]
    assert outcome["token"] is None or outcome["token"] is False
    assert outcome["signed_in"] is False
    assert "session expired" in outcome["message"].lower()


def test_the_login_form_still_reports_a_wrong_password(results):
    assert results["login_401"]["message"] == "Invalid credentials or user ID"


def test_a_double_tap_opens_one_camera_only(results):
    """Two overlays are two live streams, two shutters and two punches."""
    assert results["double_tap"]["overlays"] == 1
    assert results["double_tap"]["open_flag"] is True


def test_a_rejected_site_form_tells_the_admin(results):
    outcome = results["site_form"]
    assert outcome["unhandled"] is None, "the handler must not reject into the console"
    assert outcome["toasts"] == ["Site name already exists."]


def test_a_rejected_create_form_tells_the_admin(results):
    outcome = results["create_form"]
    assert outcome["unhandled"] is None, "the handler must not reject into the console"
    assert outcome["toasts"] == ["User ID already exists."]


def test_a_short_clock_out_asks_before_it_records(results):
    """The refusal is a question: the card stays up, with two buttons and the server's numbers."""
    outcome = results["early_question"]
    assert outcome["punches"] == 1, "only the first attempt is sent until the question is answered"
    assert outcome["has_flag_on_first"] is False, "the phone must not pre-answer the question"
    assert outcome["over_camera"], "asked over the open camera, not hidden behind it"
    assert outcome["says_paid"] and outcome["says_day"], (
        "the sentence has to carry both the hours that will be recorded and the full day"
    )
    assert outcome["buttons"] == 2, "confirm and cancel, and nothing else"
    assert outcome["camera_open_flag"] is True, "the worker is still holding the phone up"


def test_cancelling_the_early_clock_out_sends_nothing_and_keeps_the_camera(results):
    outcome = results["early_cancelled"]
    assert outcome["punches"] == 1, "a cancel must not record a second punch"
    assert outcome["modal_cleared"] is True
    assert outcome["camera_open_flag"] is True
    assert outcome["shutter_live"] is True, "the worker can take the selfie again, or just leave"


def test_confirming_repeats_the_punch_with_the_flag(results):
    outcome = results["early_confirmed"]
    assert outcome["punches"] == 2, "the answer is the same tap a second time"
    assert outcome["has_flag"], (
        f"the repeat must carry confirm_early_checkout, got {outcome['second_fields']}"
    )
    assert outcome["modal_cleared"] is True
