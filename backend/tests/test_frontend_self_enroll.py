"""The console's "enroll my own face" control, exercised for real.

WHY THIS EXISTS
---------------
The backend has the endpoint (``POST /worker/me/enroll``, see
``test_admin_self_enrollment``); this suite is about the screen. The gap it closes is a
specific one: an administrator who works a site reaches the punch card and is refused with
"Facial reference not registered", and the console - the only account screen in the app -
had no way to give them one. The roster said "Face: None" beside a row that offered nothing,
because a standard administrator may not manage administrators, and their own row is one.

What is pinned here:

1. the button appears on **your own row only**, in the language of what it will do - enroll
   when there is no template, replace when the template is stale - and appears for no other
   role, because the endpoint refuses them;
2. the capture is the app's own camera: the front-facing stream, the same shutter, and the
   photo the server judges;
3. the request carries a photo and nothing else - whose template this is, is the token, and
   there is no account id on the wire to point at somebody else;
4. a refusal is about the photo, so the camera stays open and the shutter comes back; a
   signed-out session takes the camera down with it, because there is nothing left to
   enroll *for*;
5. once it lands, the row says "Enrolled" and the button says "replace" - the answer to
   "did that work" is the roster, not a toast.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answer, faked ------------------------------------------

const ADMIN = { id: '1001', name: 'Ops Admin', role: 'admin' };
const HEAD = { id: '5000', name: 'Head Admin', role: 'head_admin' };

//: The acting administrator's own row, and the two rows a standard admin must not be able
//: to enroll: a worker's (that is not this feature) and another administrator's.
const ROSTER = [
    {
        id: '1', name: 'Seed Worker', role: 'worker', phone: '+200000000001',
        email: 'seed@example.test', status: 'active', face_enrolled: false,
        enrolled_at: null, password_set: true, password_changed_at: null, sessions_revoked: 0
    },
    {
        id: '1000', name: 'Other Admin', role: 'admin', phone: '', email: 'other@example.test',
        status: 'active', face_enrolled: true, enrolled_at: '2026-08-22 09:00:00',
        password_set: true, password_changed_at: null, sessions_revoked: 0
    },
    {
        id: '1001', name: 'Ops Admin', role: 'admin', phone: '', email: 'ops@example.test',
        status: 'active', face_enrolled: false, enrolled_at: null,
        password_set: true, password_changed_at: null, sessions_revoked: 0
    },
    {
        id: '5000', name: 'Head Admin', role: 'head_admin', phone: '', email: 'head@example.test',
        status: 'active', face_enrolled: true, enrolled_at: '2026-08-20 09:00:00',
        password_set: true, password_changed_at: null, sessions_revoked: 0
    }
];

let enrollStatus = 200;
let enrollDetail = 'Enrollment failed.';

//: The fields the endpoint was sent, so "no account id travelled" is an assertion rather
//: than a reading of the source.
let posted = null;
let enrollCalls = 0;

function responders(url, init) {
    if (url.indexOf('/worker/me/enroll') >= 0) {
        enrollCalls += 1;
        posted = { path: url.replace(/^https?:\/\/[^/]*\/api\/v1/, ''), fields: Array.from(init.body.keys()) };
        if (enrollStatus >= 400) return { status: enrollStatus, body: { detail: enrollDetail } };
        // The server writes the template; the roster it answers with next says so.
        ROSTER.filter((u) => u.id === '1001')[0].face_enrolled = true;
        return { status: 200, body: { status: 'success', worker_id: '1001', message: 'Registered.' } };
    }
    if (url.indexOf('/admin/users') >= 0) return { status: 200, body: ROSTER };
    return { status: 200, body: {} };
}

// --- driving it -----------------------------------------------------------

/**
 * The capture step needs what a stub DOM does not have: a drawable video, a canvas that can
 * make a JPEG, and an element whose ``remove()`` actually detaches it. Everything under
 * assertion - which fields the request carries, what a refusal does to the open camera - is
 * the page's own code.
 */
function stubCamera(env) {
    env.evaluate("Object.defineProperty(Camera, 'isSupported', { value: true, configurable: true })");
    env.evaluate("Camera.starts = 0; Camera.start = async () => { Camera.starts += 1; return { getTracks: () => [] }; }");
    env.evaluate("State.stops = 0; State.stopCamera = function () { State.stops += 1; State.mediaStream = null; }");
    env.evaluate(`(function () {
        const make = document.createElement;
        document.createElement = function (tag) {
            const el = make.call(document, tag);
            if (tag === 'canvas') {
                el.getContext = () => ({ drawImage() {} });
                el.toBlob = (done) => done(new Blob(['frame'], { type: 'image/jpeg' }));
            }
            return el;
        };
    })()`);
    return env;
}

function envAs(user) {
    enrollStatus = 200;
    posted = null;
    enrollCalls = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({ ...user, token: 'tok-' + user.id }) + ')');
    return env;
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function overlays(env) {
    return env.evaluate(
        "(document.body.__children || []).filter((el) => el.className === 'camera-overlay').length"
    );
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function usersRequests(env) {
    return env.requests.filter((r) => r.url.indexOf('/admin/users') >= 0).length;
}

/** The enroll control as the roster draws it, per row: which rows offer it, and with what
 *  words. Read off the markup, because that is what the person sees. */
function rowsWithButton(env) {
    const markup = render(env);
    const rows = markup.match(/<tr data-user="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        id: (/data-user="([^"]*)"/.exec(row) || [])[1],
        enrolled: (/data-face="([^"]*)"/.exec(row) || [])[1] === 'enrolled',
        enroll: (/data-enroll-self="([^"]*)"/.exec(row) || [])[1] || null,
        label: (/data-enroll-self="[^"]*"[^>]*>[\s\S]*?<span[^>]*>[\s\S]*?<\/span>|data-enroll-self="[^"]*"[^>]*>([^<]*)</.exec(row) || [])[1] || null,
        protected_hint: row.indexOf('data-protected="true"') >= 0
    }));
}

/** The label as a translation, not as markup: a button whose text is a key is a bug. */
function buttonLabel(env, userId) {
    const markup = render(env);
    const row = (markup.match(/<tr data-user="[^"]*"[\s\S]*?<\/tr>/g) || [])
        .filter((r) => r.indexOf('data-user="' + userId + '"') >= 0)[0] || '';
    return {
        marked: row.indexOf('data-enroll-self="' + userId + '"') >= 0,
        enrol_words: row.indexOf(env.evaluate("UI.escapeHtml(I18n.__('credentialsFaceEnroll'))")) >= 0,
        replace_words: row.indexOf(env.evaluate("UI.escapeHtml(I18n.__('credentialsFaceReplace'))")) >= 0
    };
}

const results = {};

// 1. whose row offers it
{
    const admin = envAs(ADMIN);
    await admin.evaluate("UI.renderAdminTab('Credentials')");
    results.admin_rows = rowsWithButton(admin);

    const head = envAs(HEAD);
    await head.evaluate("UI.renderAdminTab('Credentials')");
    results.head_rows = rowsWithButton(head);
    results.head_can = head.evaluate('UI_MODULES.canSelfEnroll()');
    results.admin_can = admin.evaluate('UI_MODULES.canSelfEnroll()');
}

// 2. the words: enroll when there is no template, replace when there is one
{
    const admin = envAs(ADMIN);
    await admin.evaluate("UI.renderAdminTab('Credentials')");
    const own = ROSTER.filter((u) => u.id === '1001')[0];
    results.labels = {
        missing: buttonLabel(admin, '1001'),
        other_admin: buttonLabel(admin, '1000'),
        worker: buttonLabel(admin, '1')
    };
    // An administrator whose template has gone stale is being offered a replacement - the
    // case the console's readiness screen reports. Repainted from the roster, as a real
    // save would, because the label is read off the row.
    own.face_enrolled = true;
    await admin.evaluate('UI_MODULES.repaintCredentialsFromCache()');
    results.labels.enrolled = buttonLabel(admin, '1001');
    own.face_enrolled = false;
}

// 3. the camera it opens, and the photo it sends
{
    const env = envAs(ADMIN);
    stubCamera(env);
    await env.evaluate("UI.renderAdminTab('Credentials')");

    await env.evaluate('UI_MODULES.enrollSelf()');
    const opened = {
        overlays: overlays(env),
        starts: env.evaluate('Camera.starts'),
        // The overlay is the app's camera chrome, and its own title is on it: a punch card's
        // words over an enrollment capture would tell the reader they are clocking in.
        labelled: env.evaluate(
            "(document.body.__children || []).filter((el) => el.className === 'camera-overlay')" +
            ".map((el) => el.innerHTML.indexOf(I18n.__('credentialsFaceEnrollTitle')) >= 0)[0]"
        ),
        has_shutter: env.evaluate(
            "(document.body.__children || []).filter((el) => el.className === 'camera-overlay')" +
            ".map((el) => el.innerHTML.indexOf('enrollShutter') >= 0)[0]"
        )
    };

    // A second tap while one capture is open must not open a second camera.
    await env.evaluate('UI_MODULES.enrollSelf()');
    opened.after_second_tap = overlays(env);

    const usersBefore = usersRequests(env);
    env.evaluate("document.getElementById('enrollVideo').videoWidth = 640");
    await env.evaluate('UI_MODULES.submitSelfEnroll()');

    results.send = {
        opened,
        posted,
        calls: enrollCalls,
        overlays: overlays(env),
        stops: env.evaluate('State.stops'),
        stream_released: env.evaluate('State.mediaStream === null'),
        roster_refetched: usersRequests(env) > usersBefore,
        toasts: toasts(env).slice(-1),
        // The row is the answer: after a successful capture the roster comes back with the
        // face cell enrolled and the button offering a replacement.
        row_after: rowsWithButton(env).filter((row) => row.id === '1001')[0]
    };
}

// 4. a refusal is about the photo, not about the session
{
    const env = envAs(ADMIN);
    stubCamera(env);
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate('UI_MODULES.enrollSelf()');
    env.evaluate("document.getElementById('enrollVideo').videoWidth = 640");

    enrollStatus = 422;
    enrollDetail = 'Liveness check failed: a printed photo cannot be enrolled.';
    const usersBefore = usersRequests(env);
    await env.evaluate('UI_MODULES.submitSelfEnroll()');

    results.refused = {
        overlays: overlays(env),
        stops: env.evaluate('State.stops'),
        signed_in: env.evaluate('!!State.user'),
        shutter_live: env.evaluate("document.getElementById('enrollShutter').disabled === false"),
        roster_refetched: usersRequests(env) > usersBefore,
        toast: toasts(env).slice(-1)[0] || ''
    };
}

// 5. a dead session takes the camera with it
{
    const env = envAs(ADMIN);
    stubCamera(env);
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate('UI_MODULES.enrollSelf()');
    env.evaluate("document.getElementById('enrollVideo').videoWidth = 640");

    enrollStatus = 401;
    enrollDetail = 'Invalid token';
    await env.evaluate('UI_MODULES.submitSelfEnroll()');

    results.signed_out = {
        overlays: overlays(env),
        token: env.evaluate('!!State.token'),
        signed_in: env.evaluate('!!State.user'),
        toast: toasts(env).slice(-1)[0] || ''
    };
}

// 6. a role the server refuses never gets the camera
{
    const env = envAs(HEAD);
    stubCamera(env);
    await env.evaluate("UI.renderAdminTab('Credentials')");
    await env.evaluate('UI_MODULES.enrollSelf()');
    results.head_capture = { overlays: overlays(env), starts: env.evaluate('Camera.starts'), calls: enrollCalls };
}

// 7. every string this feature added exists in all four tables
{
    const env = envAs(ADMIN);
    const KEYS = [
        'credentialsFaceEnroll', 'credentialsFaceReplace', 'credentialsFaceEnrollTitle',
        'credentialsFaceEnrollHint', 'credentialsFaceEnrollTake', 'credentialsFaceEnrollDone',
        'credentialsFaceEnrollFailed'
    ];
    results.translations = env.evaluate(`(function () {
        const KEYS = ${JSON.stringify(KEYS)};
        return {
            missing_english: KEYS.filter((key) => !(key in TRANSLATIONS.en)),
            missing: Object.keys(TRANSLATIONS).map((lang) => [
                lang,
                KEYS.filter((key) => !(key in TRANSLATIONS[lang])).join(',')
            ]).filter(([, missing]) => missing),
            // A key that resolves to itself is a key with no wording behind it.
            unresolved: KEYS.filter((key) => I18n.__(key) === key)
        };
    })()`);
}
"""


@pytest.fixture(scope="module")
def results():
    return frontend_vm.run(HARNESS)


def _row(rows, user_id):
    return [row for row in rows if row["id"] == user_id][0]


def test_only_your_own_row_offers_it(results):
    """The roster's own rule, applied: your row is the one account you may re-credential.

    A worker's template is not this feature (the company issues it, through an enrollment
    link), and another administrator's is not either - which is the same reason those row's
    management buttons are replaced with a "protected" badge.
    """
    admin = {row["id"]: row for row in results["admin_rows"]}
    assert admin["1001"]["enroll"] == "1001", "your own row must offer the capture"
    assert admin["1001"]["protected_hint"] is True, (
        "the enroll control sits on the row the console otherwise refuses to manage"
    )
    assert admin["1"]["enroll"] is None, "a worker's face is not enrolled from here"
    assert admin["1000"]["enroll"] is None, "another administrator's face is not either"
    assert results["admin_can"] is True


def test_a_role_the_server_refuses_gets_no_button_and_no_camera(results):
    """A button that always answers 403 is a trap, not an affordance."""
    head = {row["id"]: row for row in results["head_rows"]}
    assert all(row["enroll"] is None for row in head.values()), head
    assert results["head_can"] is False
    assert results["head_capture"]["overlays"] == 0, "the camera was opened for a refused role"
    assert results["head_capture"]["starts"] == 0
    assert results["head_capture"]["calls"] == 0, "and nothing was posted"


def test_the_label_says_what_the_button_will_do(results):
    """Two states, two words: an account with no face is being told how to clock in, and an
    account with one is being offered a replacement for a template that may be stale."""
    labels = results["labels"]
    assert labels["missing"]["marked"] is True
    assert labels["missing"]["enrol_words"] is True, labels["missing"]
    assert labels["missing"]["replace_words"] is False, "nothing to replace yet"

    assert labels["enrolled"]["replace_words"] is True, labels["enrolled"]
    assert labels["enrolled"]["enrol_words"] is False

    for absent in ("other_admin", "worker"):
        assert labels[absent]["marked"] is False, absent


def test_the_capture_is_the_apps_own_camera(results):
    """The same front-facing camera the punch card uses, on the device that will punch."""
    opened = results["send"]["opened"]
    assert opened["overlays"] == 1, "the capture has to be a camera, not a file picker"
    assert opened["starts"] == 1, "the stream was requested exactly once"
    assert opened["labelled"] is True, "the overlay has to say which photo this is"
    assert opened["has_shutter"] is True
    assert opened["after_second_tap"] == 1, "a second tap must not open a second camera"


def test_the_photo_is_the_whole_request(results):
    """Whose template this is, is the token: no account id may travel.

    This is what makes the endpoint safe to hand to a role that is not a head administrator -
    there is no field to point at somebody else, and the server ignores one that is sent.
    """
    posted = results["send"]["posted"]
    assert posted is not None, "nothing was posted"
    assert posted["path"] == "/worker/me/enroll", posted["path"]
    assert "photo" in posted["fields"], posted["fields"]
    assert "worker_id" not in posted["fields"] and "user_id" not in posted["fields"], posted["fields"]
    assert results["send"]["calls"] == 1


def test_it_puts_the_camera_away_and_the_row_says_enrolled(results):
    """The answer to "did that work" is the roster, not the toast: the face cell flips and
    the button becomes a replacement."""
    send = results["send"]
    assert send["overlays"] == 0, "the camera was left open over the console"
    assert send["stops"] >= 1, "the stream was never released"
    assert send["stream_released"] is True
    assert send["roster_refetched"] is True, "the row has to be read back from the server"
    assert send["row_after"]["enrolled"] is True
    assert send["row_after"]["enroll"] == "1001"
    assert send["toasts"], "no confirmation"


def test_a_refusal_is_about_the_photo_so_the_camera_stays(results):
    """A liveness refusal is the reader's to fix - better light, alone in the frame - so the
    overlay stays open, the shutter comes back and the sentence is the server's own."""
    refused = results["refused"]
    assert refused["overlays"] == 1, "a refused capture must not close the camera"
    assert refused["stops"] == 0, "the stream was released while the camera is still up"
    assert refused["signed_in"] is True, "a 422 is about the photo, not the session"
    assert refused["shutter_live"] is True, "the shutter has to come back, or the camera is a dead end"
    assert refused["roster_refetched"] is False, "nothing changed on the server"
    assert "Liveness check failed" in refused["toast"], refused["toast"]


def test_a_dead_session_takes_the_camera_with_it(results):
    """A 401 has already signed the reader out and repainted the login screen; a camera over
    that screen, with a shutter that can only fail, is the worst of both."""
    signed_out = results["signed_out"]
    assert signed_out["overlays"] == 0
    assert signed_out["token"] is False and signed_out["signed_in"] is False
    assert signed_out["toast"], "the reader has to be told why"


def test_every_string_is_in_every_table(results):
    translations = results["translations"]
    assert translations["missing_english"] == [], translations["missing_english"]
    assert translations["missing"] == [], translations["missing"]
    assert translations["unresolved"] == [], translations["unresolved"]
