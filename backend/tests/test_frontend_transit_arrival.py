"""The arrival, from the worker's thumb to the site on their own card.

WHY THIS EXISTS
---------------
The server has always had the arrival: ``ACTION_TRANSIT_CHECKPOINT`` is "sent by the phone
when the worker has reached a site", it clears the in-transit flag, replaces the "In Transit"
placeholder with the geofence that confirmed the trip, credits the travel, and leaves an
"Arrived at ..." notice in the worker's inbox. Two things could not be caught by the backend
suites, and both are asserted here:

* **the phone never sent it.** While a shift was on the road the handset's only action was
  Clock Out, which is refused away from a site, so the state the whole feature is built
  around was unreachable from the one device it was written for;
* **it should not cost a second face check.** A travel shift was opened with a full face
  check at the moment it began; what its arrival claims is *where the worker is*, and that is
  a GPS + geofence fact. So the arrival is taken without the camera at all - no overlay, no
  track, no selfie on the wire - and the server refuses a frame-less punch for every other
  action (see ``test_transit_to_site``).

So this suite drives the app's own path with the modelled GPS **and the camera installed but
never used**, and asserts what a worker cannot check for themselves:

* the road's primary action is the arrival, in its own words, with no Clock Out offered;
* the tap takes a fix and posts without a photograph, and the camera is never opened;
* the card changes: the real site in the hero, the road note and its request gone, the
  ordinary Clock Out back;
* the worker is told - the server's sentence as the toast, and the arrival notice as the unread
  the alerts band counts.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the phone, and the server on the other end --------------------------

const WORKER = { id: '712', name: 'Rafiq Hussain', role: 'worker', token: 'tok-712' };
const ARRIVAL_SITE = 'Downtown Tower A';
const ARRIVAL_MESSAGE = 'Arrival confirmed at Downtown Tower A. Travel time is credited to this shift.';
const FIX = { latitude: 30.05, longitude: 31.23, accuracy: 9 };

let punched = null;
let arrived = false;
//: A refusal the next arrival is answered with - ``{status, body}`` - or ``null`` for the ordinary
//: success. The two refusals an arrival can get are the point of scenario 2.
let refusal = null;

function stats() {
    return {
        worker_id: WORKER.id,
        approval_status: 'active',
        total_hours: 0,
        regular_hours: 0,
        pending_hours: 0,
        overtime_hours: 0,
        overtime_notify_hours: 8.1,
        break_minutes: 30,
        break_after_hours: 4,
        paid_day_hours: 8.0,
        on_site_day_hours: 8.5,
        auto_close_at_regular: 1,
        flagged_for_review: false,
        active_session: {
            site_name: arrived ? ARRIVAL_SITE : 'In Transit',
            clock_in_time: '2026-09-28 05:00:00',
            late_flag: null,
            seconds_on_site: 3600,
            in_transit: !arrived
        }
    };
}

function responders(url, init) {
    if (url.indexOf('/attendance/verify') >= 0) {
        punched = init.body;
        if (refusal) return refusal;
        arrived = true;
        return {
            status: 200,
            body: { status: 'arrived', message: ARRIVAL_MESSAGE, site: ARRIVAL_SITE }
        };
    }
    if (url.indexOf('/worker/me/stats') >= 0) return { status: 200, body: stats() };
    if (url.indexOf('/worker/me/notifications') >= 0) {
        return {
            status: 200,
            body: arrived
                ? {
                    unread: 1,
                    notifications: [{
                        id: 1, kind: 'transit_arrived',
                        title: 'Arrived at ' + ARRIVAL_SITE,
                        body: 'Your travel shift was confirmed.'
                    }]
                }
                : { unread: 0, notifications: [] }
        };
    }
    return { status: 200, body: {} };
}

/** A worker on a phone with a GPS, a camera and a session - the camera stays shut. */
function roadPhone() {
    punched = null;
    arrived = false;
    refusal = null;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(WORKER) + ')');
    env.installGeolocationSupport();
    // Installed on purpose: an arrival that opened it would leave a track behind, which is
    // the difference between "no camera was needed" and "the camera stub was missing".
    env.installCameraSupport();
    env.setGeolocationFix(FIX);
    return env;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

function panel(env) {
    return env.evaluate("document.getElementById('workerDashboard').innerHTML");
}

function overlays(env) {
    return env.evaluate(
        "(document.body.__children || []).filter((el) => el.id === 'cameraOverlay').length"
    );
}

function toasts(env) {
    return env.evaluate(
        "(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)"
    );
}

function toastCount(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).length");
}

/** The two arrivals the server refuses, as ``/attendance/verify`` really answers them. */
const OUTSIDE = {
    status: 422,
    body: {
        detail: {
            error_code: 'arrival_outside_geofence',
            message: 'Outside target site geofence. Cannot confirm arrival.'
        }
    }
};
const ALREADY = {
    status: 409,
    body: {
        detail: {
            error_code: 'arrival_already_confirmed',
            message: 'This travel shift has already been confirmed at a site.'
        }
    }
};

const results = {};

// 1. on the road: the arrival is the action, and it is taken with a fix and no camera
{
    const env = roadPhone();
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'))");
    const before = panel(env);

    results.road = {
        arrival_button: before.indexOf("handleClock('Transit Checkpoint')") >= 0,
        arrival_label: env.evaluate("I18n.__('transitArrive')"),
        arrival_labelled: before.indexOf(env.evaluate("I18n.__('transitArrive')")) >= 0,
        no_clock_out: before.indexOf("handleClock('Clock Out')") < 0,
        note: before.indexOf('data-transit-pending') >= 0,
        request_button: before.indexOf('data-request-checkout') >= 0
    };

    // The worker taps the card's own action, the way ``handleClock`` does.
    await env.evaluate("UI.doAttendance('Transit Checkpoint')");
    await settle();

    const request = env.requests.filter((r) => r.url.indexOf('/attendance/verify') >= 0)[0] || null;
    results.punch = {
        requests: env.requests.filter((r) => r.url.indexOf('/attendance/verify') >= 0).length,
        url: request ? request.url : null,
        method: request ? request.method : null,
        authorized: !!(request && request.headers && request.headers.Authorization),
        fields: punched ? Array.from(punched.keys()) : [],
        action: punched ? punched.get('action') : null,
        location_input: punched ? punched.get('location_input') : null,
        // The whole point: no photograph on the wire.
        selfie: punched ? punched.get('selfie') : 'no-request'
    };

    results.face = {
        overlays: overlays(env),
        camera_tracks: env.cameraTracks.length,
        camera_open: env.evaluate('!!UI._cameraOpen'),
        fixes_asked: env.geolocationCalls.length
    };

    const after = panel(env);
    results.after = {
        site_shown: after.indexOf(ARRIVAL_SITE) >= 0,
        note_gone: after.indexOf('data-transit-pending') < 0,
        request_gone: after.indexOf('data-request-checkout') < 0,
        clock_out_back: after.indexOf("handleClock('Clock Out')") >= 0,
        arrival_button_gone: after.indexOf("handleClock('Transit Checkpoint')") < 0
    };
    results.told = {
        toasts: toasts(env),
        server_message: ARRIVAL_MESSAGE,
        // The arrival notice is the unread the band draws, so the badge is not still zero.
        unread: env.evaluate('WORKER_MODULES.alertCount'),
        newest_title: env.evaluate(
            "(WORKER_MODULES._newestUnread && WORKER_MODULES._newestUnread.title) || ''"
        )
    };
}

// 2. refused: the worker reads their own language, not the server's English
{
    const env = roadPhone();
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'))");
    const roadMarkup = panel(env);

    refusal = OUTSIDE;
    await env.evaluate("UI.doAttendance('Transit Checkpoint')");
    await settle();
    results.refused = {
        toasts: toasts(env),
        sentence: env.evaluate("I18n.__('transitArriveOutside')"),
        server_message: OUTSIDE.body.detail.message,
        // The tap still took a fix and still asked the server - the refusal is the server's
        // answer, not something the phone decided.
        requests: env.requests.filter((r) => r.url.indexOf('/attendance/verify') >= 0).length,
        // Nothing was written and nothing changed: the shift is still on the road, which is
        // what "the shift is left exactly as it was" means from the phone's side.
        still_on_road: panel(env) === roadMarkup,
        camera_tracks: env.cameraTracks.length,
        overlays: overlays(env)
    };

    // The same refusal with the app in Arabic: this is the whole point of the change, and the
    // only way to assert it is to read the toast in a language that is not English.
    env.evaluate("I18n.setLang('ar')");
    const before = toastCount(env);
    refusal = OUTSIDE;
    await env.evaluate("UI.doAttendance('Transit Checkpoint')");
    await settle();
    results.refused_arabic = {
        new_toasts: toasts(env).slice(before),
        sentence: env.evaluate("I18n.__('transitArriveOutside')"),
        server_message: OUTSIDE.body.detail.message,
        generic_prefix: env.evaluate("I18n.__('attendanceError')")
    };
    env.evaluate("I18n.setLang('en')");

    // The other refusal the tap can get: somebody - this phone twice, or an administrator -
    // already confirmed the trip. It has its own sentence rather than that one's.
    const beforeAlready = toastCount(env);
    refusal = ALREADY;
    await env.evaluate("UI.doAttendance('Transit Checkpoint')");
    await settle();
    results.refused_already = {
        new_toasts: toasts(env).slice(beforeAlready),
        sentence: env.evaluate("I18n.__('transitArriveConfirmed')"),
        outside_sentence: env.evaluate("I18n.__('transitArriveOutside')"),
        server_message: ALREADY.body.detail.message
    };

    // Both sentences, read straight out of the four tables: a refusal translated in one language
    // and left in English in three is the bug this change exists to close.
    results.languages = env.evaluate(
        "['en','ar','hi','ur'].reduce((out, lang) => { const t = TRANSLATIONS[lang] || {};"
        + " out[lang] = { outside: t.transitArriveOutside, confirmed: t.transitArriveConfirmed };"
        + " return out; }, {})"
    );
}
"""


@pytest.fixture(scope="module")
def arrival() -> dict:
    return frontend_vm.run(HARNESS)


@pytest.mark.regression
def test_the_roads_primary_action_is_the_arrival(arrival):
    """While the shift is unconfirmed the button is the arrival - and Clock Out is not offered.

    An off-site Clock Out is refused, so a card that led with it would invite the one tap that
    cannot succeed. The arrival is the step that changes the state, so it is the step the card
    offers, in its own words rather than the clock-out's.
    """
    road = arrival["road"]
    assert road["arrival_button"] is True, "the road's action has to be the arrival"
    assert road["arrival_labelled"] is True
    assert road["arrival_label"], "the arrival action is not an empty label"
    assert road["no_clock_out"] is True, "Clock Out on the road is a refusal, not an action"
    assert road["note"] is True, "the state is still explained"
    assert road["request_button"] is True, "the way out for a worker who never arrives stays"


@pytest.mark.regression
def test_the_arrival_carries_a_fix_and_no_photograph(arrival):
    """A place, not a person: the punch names the worker, the action and the fix - and no face.

    The face was checked when the shift opened. Re-checking it here would make a worker at a
    gate re-prove an identity verified hours earlier, so the arrival is the one punch with no
    frame on the wire - and that is the shape the endpoint requires, not a client shortcut.
    """
    punch = arrival["punch"]
    assert punch["requests"] == 1, "one tap is one punch"
    assert punch["url"].endswith("/api/v1/attendance/verify"), punch
    assert punch["method"] == "POST"
    assert punch["authorized"] is True, "the session is still the credential"
    assert punch["action"] == "Transit Checkpoint", punch
    assert punch["location_input"], "an arrival is a claim about a place"
    assert punch["selfie"] is None, "an arrival must not send a photograph"
    assert "selfie" not in punch["fields"], punch["fields"]
    assert "worker_id" in punch["fields"] and "action" in punch["fields"]


@pytest.mark.regression
def test_the_arrival_never_opens_the_camera(arrival):
    """No overlay, no track, no open card: the camera is installed and stays unused."""
    face = arrival["face"]
    assert face["overlays"] == 0, "an arrival has no camera card"
    assert face["camera_tracks"] == 0, "no stream was started for an arrival"
    assert face["camera_open"] is False
    assert face["fixes_asked"] >= 1, "the arrival still needs a fix"


@pytest.mark.regression
def test_the_card_changes_from_in_transit_to_the_site(arrival):
    """The visible half: the real site, the road note gone, and the ordinary card back."""
    after = arrival["after"]
    assert after["site_shown"] is True, "the site the worker reached is not on the card"
    assert after["note_gone"] is True, "the road note outlived the road state"
    assert after["request_gone"] is True, "the request button belongs to the road only"
    assert after["clock_out_back"] is True, "a confirmed shift still ends the ordinary way"
    assert after["arrival_button_gone"] is True


@pytest.mark.regression
def test_the_worker_is_told_they_arrived(arrival):
    """The server's own sentence is the toast, and the notice is what the badge counts."""
    told = arrival["told"]
    assert told["server_message"] in told["toasts"], told["toasts"]
    assert told["unread"] == 1, "the arrival notice has to reach the inbox badge"
    assert told["newest_title"], "the band has nothing to show"


@pytest.mark.regression
def test_a_refused_arrival_is_said_in_the_workers_own_language(arrival):
    """The one moment the worker is standing at a gate: an English sentence is no help.

    ``/attendance/verify`` answers English because the API has one language, so a refusal the
    worker has to *read* carries an ``error_code`` beside its sentence and the handset writes its
    own words from it. Asserted on the toast, because the toast is the whole surface: a reader of
    Arabic used to get a button they could read and an answer to it they could not.
    """
    refused = arrival["refused"]
    assert refused["sentence"] in refused["toasts"], refused["toasts"]
    assert refused["server_message"] not in " | ".join(refused["toasts"]), (
        "the worker was handed the server's English instead of the app's own sentence: "
        + repr(refused["toasts"])
    )
    # ...and the tap really was sent, and really was refused - the sentence came from a real answer.
    assert refused["requests"] == 1, refused


@pytest.mark.regression
def test_a_refused_arrival_leaves_the_shift_on_the_road(arrival):
    """A refusal is not an answer: the shift is still open, still in transit, still counting."""
    refused = arrival["refused"]
    assert refused["still_on_road"] is True, "a refused arrival changed the card"
    assert refused["camera_tracks"] == 0
    assert refused["overlays"] == 0


@pytest.mark.regression
def test_the_same_refusal_in_arabic_is_arabic_and_not_the_servers(arrival):
    """The point of the change, read off the toast rather than off the table.

    An assertion that the key exists would pass on a handset that never used it. This drives the
    app in Arabic and reads what the worker would actually see - including that it is not the
    generic "punch failed" toast, which is the other way a translated sentence reaches nobody.
    """
    arabic = arrival["refused_arabic"]
    assert arabic["new_toasts"], "the second tap produced no toast at all"
    assert arabic["sentence"] in arabic["new_toasts"], arabic["new_toasts"]
    assert arabic["sentence"] != arabic["server_message"], (
        "the sentence the handset draws is the server's own: nothing was translated"
    )
    # ...and the app really was in Arabic, so the check above is not two English sentences being
    # compared. Without this the suite would pass on a handset whose language switch did nothing.
    assert arabic["sentence"] != arrival["refused"]["sentence"], (
        "the Arabic tap drew the English sentence: the language never changed"
    )
    assert arabic["server_message"] not in " | ".join(arabic["new_toasts"]), arabic["new_toasts"]
    assert not any(text.startswith(arabic["generic_prefix"]) for text in arabic["new_toasts"]), (
        "a refusal the worker can act on was flattened into the generic punch failure"
    )


@pytest.mark.regression
def test_a_shift_somebody_already_confirmed_says_so_in_its_own_words(arrival):
    """The 409 is a different fact from the 422, so it is a different sentence."""
    already = arrival["refused_already"]
    assert already["sentence"] in already["new_toasts"], already["new_toasts"]
    assert already["outside_sentence"] not in already["new_toasts"], (
        "both refusals were flattened into one sentence: " + repr(already["new_toasts"])
    )
    assert already["server_message"] not in " | ".join(already["new_toasts"]), already["new_toasts"]


@pytest.mark.regression
def test_both_refusals_exist_in_all_four_languages(arrival):
    """A sentence that exists only in English is the bug this change is about."""
    languages = arrival["languages"]
    assert sorted(languages) == ["ar", "en", "hi", "ur"], languages
    for lang in ("ar", "hi", "ur"):
        for field in ("outside", "confirmed"):
            text = languages[lang][field]
            assert text, f"the {field} refusal is missing in {lang}"
            assert text != languages["en"][field], (
                f"the {field} refusal is still English in {lang}: {text!r}"
            )
