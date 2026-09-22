"""A punch, from the card opening to the selfie on the wire, against the modelled device.

WHY THIS EXISTS
---------------
A punch asks the phone for two things - a location fix and a camera frame - and until now every
suite that drove one had to stub both: ``Location.current`` and ``Camera.start`` replaced, and
the page's video element handed a made-up ``videoWidth`` so the shutter would have something to
take. What that left untested is the shipped half of the flow: the code that opens the camera
against a real device, takes a *fresh* fix at the shutter (a punch is a claim about now, and the
reading the card opened with can be stale by the time the worker presses it), turns the frame
into a JPEG, and releases the track afterwards.

``frontend_vm`` models that device now - ``installCameraSupport()``, a stream that carries a
frame, a video that reports the size of the stream on it, a canvas that can encode what it was
given, and an ``installGeolocationSupport()`` fix the suite chooses - so this suite drives the
app's own capture path with nothing stubbed in the middle. What is asserted is the part a worker
cannot check for themselves:

* **the card opens on the modelled camera.** The stream is on the video and the frame has a
  size, which is the condition the shutter checks before it will take a photo at all, and the
  window line the server sent is on the card;
* **the punch carries a fresh fix.** The fix is moved between the card opening and the shutter,
  so a punch that re-sent the card's reading is caught rather than passing by accident - and the
  re-read asks for the same high accuracy the first one did;
* **a re-read that fails still punches.** The card's fix is a fix; a GPS hiccup at the shutter
  must not lose the punch or send the worker back to the start of the flow;
* **the selfie is a JPEG of the frame that came through the device**, posted with the worker,
  the action and the fix - and the session is the credential, so no password is on the wire;
* **the camera comes down.** The track is stopped (the light goes out), the stream is released
  and the card leaves the DOM. That last one used to be untestable, because this stub resolved
  an id to a stand-in rather than to the node the page had appended;
* **an impatient double tap costs one fix, one card and one punch;**
* **a refused camera opens no card and punches nothing;**
* **the framing coach measured real pixels.** The flat frame the camera starts with draws its
  advice and a frame with contrast in it says nothing - which can only be true if the readback
  the coach reasoned about came through the device rather than from the stub's opinion.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the phone, and the server on the other end --------------------------

const WORKER = { id: '712', name: 'Rafiq Hussain', role: 'worker', token: 'tok-712' };

// Two fixes, deliberately different. The card opens on the first and the shutter re-reads, so
// a punch carrying the card's reading looks nothing like one that took a fresh one - which is
// the only way "the punch is a claim about now" can be asserted instead of assumed.
const CARD_FIX = { latitude: 30.05, longitude: 31.23, accuracy: 9 };
const SHUTTER_FIX = { latitude: 30.0502, longitude: 31.2311, accuracy: 6 };

let punched = null;

function responders(url, init) {
    if (url.indexOf('/attendance/verify') >= 0) {
        punched = init.body;
        return { status: 200, body: { message: 'Checked in at Downtown Tower A.' } };
    }
    if (url.indexOf('/worker/me/site-window') >= 0) {
        return {
            status: 200,
            body: {
                on_site: true,
                site_name: 'Downtown Tower A',
                window: {
                    clock_in_window_start: '04:00',
                    clock_in_window_end: '06:30',
                    site_timezone: 'Africa/Cairo',
                    window: '04:00-06:30',
                    crosses_midnight: false,
                    site_specific: false
                },
                verdict: 'on_time',
                minutes_off: 0,
                site_time: '05:12'
            }
        };
    }
    return { status: 200, body: {} };
}

/** A worker on a phone with a camera, a GPS and a session - nothing stubbed by hand. */
function phone() {
    punched = null;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(WORKER) + ')');
    env.installGeolocationSupport();
    env.installCameraSupport();
    env.setGeolocationFix(CARD_FIX);
    return env;
}

/** A picture with detail in it: a wall with light on it rather than a lens cap. */
function detailedFrame(width, height) {
    const data = new Array(width * height * 4);
    for (let at = 0; at < data.length; at += 4) {
        const value = ((at / 4) % 2) ? 30 : 220;
        data[at] = value;
        data[at + 1] = value;
        data[at + 2] = value;
        data[at + 3] = 255;
    }
    return data;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

function overlays(env) {
    return env.evaluate("(document.body.__children || []).filter((el) => el.id === 'cameraOverlay').length");
}

function hint(env) {
    return env.evaluate(
        "({ text: document.getElementById('framingHint').textContent," +
        "   shown: document.getElementById('framingHint').hidden === false," +
        "   tone: document.getElementById('framingHint').dataset.framing })"
    );
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function cameraLight(env) {
    return env.cameraTracks.map((track) => track.stopped);
}

/** Open the card the way the worker does, and let the page finish painting it. */
async function openCard(env) {
    // ``doAttendance`` does not await the camera (a double tap must not open two), so the
    // camera coming up is a later turn of the loop rather than part of this promise.
    await env.evaluate("UI.doAttendance('Clock In')");
    await settle();
}

/**
 * Take the photo the way the card does: the handler the page bound to its own shutter.
 *
 * That handler returns ``submitAttendance``'s promise, so awaiting what the click returned
 * awaits the whole punch - the snapshot, the re-read, the request and the repaint - rather
 * than waiting a while and hoping.
 */
async function pressShutter(env) {
    await env.evaluate("document.getElementById('captureBtn').__handlers.click()");
    await settle();
}

const results = {};

// 1. the whole flow: tap, card, shutter, and the punch that arrives at the server
{
    const env = phone();
    await openCard(env);
    results.card = {
        overlays: overlays(env),
        open_flag: env.evaluate('!!UI._cameraOpen'),
        stream_attached: env.evaluate("!!document.getElementById('attendanceVideo').srcObject"),
        frame: env.evaluate(
            "(function () { const v = document.getElementById('attendanceVideo');"
            + " return v.videoWidth + 'x' + v.videoHeight; })()"
        ),
        // The card is built as markup and appended, so the fix the worker reads at the top of
        // it is in that markup until the shutter writes a fresh one into the node.
        card_markup: env.evaluate(
            "((document.body.__children || []).filter((el) => el.id === 'cameraOverlay')[0]"
            + " || { innerHTML: '' }).innerHTML"
        ),
        window_line: env.evaluate(
            "({ text: document.getElementById('cameraWindow').textContent," +
            "   shown: document.getElementById('cameraWindow').hidden === false," +
            "   verdict: document.getElementById('cameraWindow').dataset.verdict })"
        ),
        hint: hint(env),
        flat_advice: env.evaluate("I18n.__('framingFlat')"),
        fixes_asked: env.geolocationCalls.length,
        tracks: env.cameraTracks.length,
    };

    // The worker walks a few metres between opening the card and pressing the shutter.
    env.setGeolocationFix(SHUTTER_FIX);
    await pressShutter(env);

    const request = env.requests.filter((r) => r.url.indexOf('/attendance/verify') >= 0)[0] || null;
    const selfie = punched ? punched.get('selfie') : null;
    results.punch = {
        requests: env.requests.filter((r) => r.url.indexOf('/attendance/verify') >= 0).length,
        url: request ? request.url : null,
        method: request ? request.method : null,
        authorized: !!(request && request.headers && request.headers.Authorization),
        fields: punched ? Array.from(punched.keys()) : [],
        worker_id: punched ? punched.get('worker_id') : null,
        action: punched ? punched.get('action') : null,
        location_input: punched ? punched.get('location_input') : null,
        selfie_name: selfie ? selfie.name : null,
        selfie_type: selfie ? selfie.type : null,
        selfie_size: selfie ? selfie.size : null,
        selfie_frame: selfie ? await selfie.text() : null,
        fixes_asked: env.geolocationCalls.length,
        // The re-read is the same request as the first one, options and all: a fresh fix is
        // not a cheaper one.
        re_read_options: (env.geolocationCalls[1] || {}).options || null,
        overlays: overlays(env),
        open_flag: env.evaluate('!!UI._cameraOpen'),
        camera_light: cameraLight(env),
        stream_released: env.evaluate('State.mediaStream === null'),
        coords_shown: env.evaluate("document.getElementById('cameraCoords').textContent"),
        stored_coords: env.evaluate('State.gpsCoords'),
        toasts: toasts(env),
    };
}

// 2. a re-read that fails still punches, on the fix the card opened with
{
    const env = phone();
    await openCard(env);
    env.setGeolocationError({ code: 2, message: 'Position unavailable' });
    await pressShutter(env);
    results.stale_fix = {
        punched: !!punched,
        location_input: punched ? punched.get('location_input') : null,
        fixes_asked: env.geolocationCalls.length,
        overlays: overlays(env),
        camera_light: cameraLight(env),
        toasts: toasts(env),
    };
}

// 3. an impatient double tap: one fix, one card, one punch
{
    const env = phone();
    const first = env.evaluate("UI.doAttendance('Clock In')");
    const second = env.evaluate("UI.doAttendance('Clock In')");
    await Promise.all([first, second]);
    await settle();
    results.double_tap = { overlays: overlays(env), fixes_asked: env.geolocationCalls.length };
    await pressShutter(env);
    results.double_tap.punches = env.requests.filter(
        (r) => r.url.indexOf('/attendance/verify') >= 0
    ).length;
}

// 4. a camera the person refused: no card, an explanation, and nothing sent
{
    const env = phone();
    env.setCameraRefusal('NotAllowedError');
    await openCard(env);
    results.refused_camera = {
        overlays: overlays(env),
        open_flag: env.evaluate('!!UI._cameraOpen'),
        explanation: env.evaluate("(document.getElementById('modalRoot').innerHTML || '').length > 0"),
        punched: !!punched,
        fixes_asked: env.geolocationCalls.length,
    };
}

// 5. the coach, on a frame with detail in it
{
    const env = phone();
    env.setCameraFrame(detailedFrame);
    await openCard(env);
    results.coach = { hint: hint(env), flat: results.card.hint };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_card_opens_on_the_modelled_camera(results):
    """The state the shutter checks before it will take a photo, reached through the app."""
    card = results["card"]
    assert card["overlays"] == 1, "tapping clock-in with a working camera must open one card"
    assert card["open_flag"] is True, card
    assert card["stream_attached"] is True, (
        "the camera's stream is not on the video element, so there is nothing to photograph"
    )
    assert card["frame"] == "720x960", (
        f"the video reports {card['frame']!r} for a modelled stream that gave it settings: the "
        f"shutter refuses a frame with no size, and a worker would read 'the camera is not ready "
        f"yet' forever"
    )
    assert card["tracks"] == 1, card
    assert "30.05,31.23" in card["card_markup"], (
        f"the card does not show the fix the punch will be judged by: {card['card_markup'][:200]!r}"
    )
    assert 'id="captureBtn"' in card["card_markup"], "the card has no shutter to press"
    assert 'id="attendanceVideo"' in card["card_markup"], "the card has no camera in it"


def test_the_card_shows_the_window_the_server_sent_it(results):
    """The card is live, not only the camera: its own advice arrived while the stream came up."""
    line = results["card"]["window_line"]
    assert line["shown"] is True, (
        "the clock-in window line stayed hidden, so the worker is framing their face without "
        "the one piece of advice the card exists to give"
    )
    assert "Downtown Tower A" in line["text"], line["text"]
    assert line["verdict"] == "on_time", line


def test_the_punch_carries_a_fresh_fix_rather_than_the_card_s_reading(results):
    """A punch is a claim about now, and the card's fix can be a walk old."""
    punch = results["punch"]
    assert punch["fixes_asked"] == 2, (
        f"the shutter asked for {punch['fixes_asked']} readings: re-sending the fix the card "
        f"opened with is what told a worker standing inside the gate that they were outside it"
    )
    assert punch["location_input"] == "30.0502,31.2311", (
        f"the punch carried {punch['location_input']!r}, which is not the fix taken at the "
        f"shutter: the re-read happened and was thrown away, or never reached the request"
    )
    assert punch["stored_coords"] == "30.0502,31.2311", (
        "the screen's idea of where the punch was made is not where it was made"
    )
    assert "30.0502,31.2311" in punch["coords_shown"], (
        f"the card goes on showing the old reading after a fresh one was sent: {punch['coords_shown']!r}"
    )
    options = punch["re_read_options"]
    assert options["enableHighAccuracy"] is True and options["maximumAge"] == 30000, (
        f"the re-read is cheaper than the first read, which is the one thing it must not be - a "
        f"coarse or cached fix is what fails a geofence the worker is standing inside: {options}"
    )


def test_a_failed_re_read_still_punches_on_the_fix_the_card_had(results):
    """The card's fix is a fix, and the server is the one that judges it."""
    stale = results["stale_fix"]
    assert stale["fixes_asked"] == 2, stale
    assert stale["punched"] is True, (
        "a GPS that could not answer at the shutter lost the punch: the worker walks inside the "
        "gate, presses the button, and is sent back to the start of the flow"
    )
    assert stale["location_input"] == "30.05,31.23", (
        f"the punch fell back to {stale['location_input']!r} rather than the reading the card "
        f"opened with, so the fallback invents a position instead of using the one in hand"
    )


def test_the_selfie_is_a_jpeg_of_the_frame_that_came_through_the_device(results):
    """The bytes the server scores: the app's own snapshot, of the camera's own frame."""
    punch = results["punch"]
    assert punch["requests"] == 1, f"one shutter press sent {punch['requests']} punches"
    assert punch["url"].endswith("/api/v1/attendance/verify"), punch["url"]
    assert punch["method"] == "POST", punch
    assert punch["authorized"] is True, (
        "the punch carried no bearer token, and the session is the only credential it has"
    )
    assert punch["selfie_type"] == "image/jpeg", (
        f"the frame was encoded as {punch['selfie_type']!r}: a PNG of a camera frame is several "
        f"times the upload, over a phone's connection"
    )
    assert punch["selfie_name"] == "selfie.jpg", punch["selfie_name"]
    assert punch["selfie_size"] > 0, punch
    frame = punch["selfie_frame"]
    assert "720x960" in frame, (
        f"the encoded frame is not the one the camera produced: {frame!r} (the stream was "
        f"1280x960-able and the video reported 720x960)"
    )
    assert "q0.9" in frame, (
        f"the capture path no longer compresses the frame it sends: {frame!r}. A full-resolution "
        f"JPEG off a phone's camera is the difference between a punch that uploads at the gate "
        f"and one that does not"
    )


def test_the_punch_names_the_worker_and_the_action_and_carries_no_password(results):
    """The session is the credential; a password photographed under a phone is a worse secret."""
    punch = results["punch"]
    assert punch["worker_id"] == "712", punch
    assert punch["action"] == "Clock In", punch
    for field in ("worker_id", "action", "location_input", "selfie"):
        assert field in punch["fields"], f"the punch does not carry {field}: {punch['fields']}"
    assert "password" not in punch["fields"], (
        f"a password is back on the punch form: {punch['fields']}"
    )


def test_the_camera_comes_down_with_the_card(results):
    """The light goes out, the stream is released and the card leaves the page."""
    punch = results["punch"]
    assert punch["camera_light"] == [True], (
        "the camera track was never stopped, so the light stays on after the punch - on a "
        "phone in a worker's pocket, for the rest of the shift"
    )
    assert punch["stream_released"] is True, "the page still holds a stream it has stopped"
    assert punch["open_flag"] is False, punch
    assert punch["overlays"] == 0, (
        "the card is still on the page after the punch: the shipped ``closeCamera()`` resolves "
        "the card by id, and an id that resolves to a stand-in removes nothing"
    )
    assert punch["toasts"], "the punch succeeded and the worker was told nothing"
    assert "Downtown Tower A" in punch["toasts"][-1], punch["toasts"]


def test_an_impatient_double_tap_opens_one_card_and_punches_once(results):
    """Two overlays mean two live streams on a phone in the sun, and two punches queued."""
    tap = results["double_tap"]
    assert tap["fixes_asked"] == 1, (
        f"the second tap asked for a fix of its own ({tap['fixes_asked']} in total), so the "
        f"guard in front of the camera is not doing its job"
    )
    assert tap["overlays"] == 1, f"{tap['overlays']} cards opened for one tap repeated"
    assert tap["punches"] == 1, f"{tap['punches']} punches from one button"


def test_a_refused_camera_opens_no_card_and_punches_nothing(results):
    """A person who said no gets an explanation, not a card and a shutter over nothing."""
    refused = results["refused_camera"]
    assert refused["overlays"] == 0, "a card was left open over a camera that never came up"
    assert refused["open_flag"] is False, refused
    assert refused["explanation"] is True, (
        "the camera was refused and the worker was told nothing - the old behaviour was a "
        "shutter that did not work and no reason"
    )
    assert refused["punched"] is False, (
        "a punch was sent with no photo from a camera that never opened"
    )
    assert refused["fixes_asked"] == 1, (
        f"the fix was taken {refused['fixes_asked']} times for a card that never appeared"
    )


def test_the_framing_coach_measured_the_frame_through_the_device(results):
    """The coach's rules, run on pixels that came through the modelled camera.

    The two frames are the whole assertion: the flat one the camera starts with draws its
    advice, and a frame with contrast in it says nothing. Neither can be true unless the
    readback - the camera's picture, drawn into a canvas and read back as pixels - reached the
    rules, which is the half of this flow that had no way to run before.
    """
    coach = results["coach"]
    flat = coach["flat"]
    assert flat["shown"] is True, "the coach looked at a flat frame and said nothing at all"
    assert flat["tone"] == "bad", flat
    assert flat["text"] == results["card"]["flat_advice"], flat
    assert coach["hint"]["shown"] is False, (
        f"a frame with plenty of detail in it still drew advice: {coach['hint']}. Either the "
        f"measured pixels are not the ones the camera produced, or a usable frame is being "
        f"reported as a bad one"
    )
