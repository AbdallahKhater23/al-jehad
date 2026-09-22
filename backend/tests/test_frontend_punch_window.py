"""The clock-in window on the punch card, before the shutter.

WHY THIS EXISTS
---------------
The window a punch is judged by is the *site's*, and it was resolved only on the server, on the
punch path - so the answer to "am I early, on time or late" reached an administrator, as a
late-arrival notification, after the record existed. The worker it was about read it never. The
card now asks the same question one tap earlier, while the worker is still framing their face.

What is asserted here:

* the line, in the three verdicts, from the arithmetic the server sends (verdict + minutes) and
  the translations the page owns - three languages, all present;
* **clock-in only**. A clock-out is judged on the hours, not on this window, so nothing is even
  requested for it: printing "late" over somebody who is leaving would be a lie told by the
  screen;
* the failure modes do not escalate. No signal, an older server, a 500 - the line stays hidden
  and the shutter is exactly as usable as it was. This is advice, never a gate, and the punch
  that follows is still judged on the server;
* the site name and window label are values off the wire, so they are written as text. A site
  named ``<img onerror>`` is a name, not markup.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------
//
// One shape, four verdicts: the endpoint the card calls, and the company window a site that has
// none of its own inherits. `answer()` is deliberately the *whole* response - the card is
// allowed to read `on_site`, `site_name`, `window.window`, `verdict` and `minutes_off`, and a
// key it does not get is a field it must survive without (see the shipping half of the suite).

function answer(overrides) {
    return Object.assign({
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
    }, overrides || {});
}

const WORKER = { id: '600', name: 'Moallem', role: 'lead_worker', token: 'tok-600' };

let status = 200;
let payload = answer();

function responders(url) {
    if (url.indexOf('/worker/me/site-window') >= 0) {
        return { status: status, body: payload };
    }
    return { status: 200, body: {} };
}

function workerEnv() {
    status = 200;
    payload = answer();
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(WORKER) + ')');
    return env;
}

function windowRequests(env) {
    return env.requests.filter((r) => r.url.indexOf('/worker/me/site-window') >= 0);
}

/**
 * What the card's line currently says, and how it is toned.
 *
 * ``shown`` rather than ``hidden``: the element starts hidden (the attribute in the card's own
 * markup), and the only thing that unhides it is a successful answer. `hidden === false` is
 * therefore "the page painted this", which is the fact worth asserting - an untouched element
 * has no ``hidden`` property at all in this DOM.
 */
function line(env) {
    return env.evaluate(
        "({ text: document.getElementById('cameraWindow').textContent," +
        "   verdict: document.getElementById('cameraWindow').dataset.verdict," +
        "   shown: document.getElementById('cameraWindow').hidden === false," +
        "   html: document.getElementById('cameraWindow').innerHTML })"
    );
}

async function ask(env, coords, action) {
    await env.evaluate(
        'UI.loadSiteWindow(' + JSON.stringify(coords) + ', ' + JSON.stringify(action) + ')'
    );
    return line(env);
}

const results = {};

// 1. the three verdicts, from the arithmetic the server sent
{
    const onTime = workerEnv();
    results.on_time = await ask(onTime, '30.05,31.23', 'Clock In');

    const early = workerEnv();
    payload = answer({ verdict: 'early', minutes_off: 25 });
    results.early = await ask(early, '30.05,31.23', 'Clock In');

    const late = workerEnv();
    payload = answer({ verdict: 'late', minutes_off: 65 });
    results.late = await ask(late, '30.05,31.23', 'Clock In');

    // An overnight site: the label the server builds, carried through verbatim.
    const night = workerEnv();
    payload = answer({
        site_name: 'New Capital Zone B',
        window: {
            clock_in_window_start: '21:30',
            clock_in_window_end: '05:30',
            site_timezone: 'Africa/Cairo',
            window: '21:30-05:30 (overnight)',
            crosses_midnight: true,
            site_specific: true
        },
        verdict: 'early',
        minutes_off: 30
    });
    results.overnight = await ask(night, '29.98,31.75', 'Clock In');
}

// 2. not inside any site: say where to stand, not a window for somewhere else
{
    const env = workerEnv();
    payload = answer({ on_site: false, site_name: null, verdict: 'late', minutes_off: 120 });
    results.off_site = await ask(env, '51.5074,-0.1278', 'Clock In');
}

// 3. clock-out asks nothing: the window does not judge a departure
{
    const env = workerEnv();
    results.clock_out = await ask(env, '30.05,31.23', 'Clock Out');
    results.clock_out_requests = windowRequests(env).length;
}

// 4. advice, never a gate: a failed look-up leaves the line hidden and the card untouched
{
    const env = workerEnv();
    status = 500;
    results.failed = await ask(env, '30.05,31.23', 'Clock In');
    results.failed_requests = windowRequests(env).length;
}

// 5. a hostile site name is a name, not markup
{
    const env = workerEnv();
    payload = answer({ site_name: '<img src=x onerror=alert(1)>' });
    results.hostile = await ask(env, '30.05,31.23', 'Clock In');
}

// 6. the open card carries the line, and opening it is what asks
//
// The device is overridden rather than installed: this suite is about the window line, not
// about capture, and installing a camera is what a suite about capture does instead (see
// test_frontend_punch_capture). ``openCamera`` is read mid-flight, the way the worker sees it
// the moment the card appears.
{
    const env = workerEnv();
    env.evaluate("Object.defineProperty(Camera, 'isSupported', { value: true, configurable: true })");
    env.evaluate("Camera.start = async () => ({ getTracks: () => [] })");

    const opening = env.evaluate("UI.openCamera('Clock In', '30.05,31.23')");
    const overlays = env.evaluate("(document.body.__children || []).filter((el) => el.id === 'cameraOverlay')");
    results.card_markup = overlays.length ? overlays[0].innerHTML.indexOf('id="cameraWindow"') >= 0 : false;
    await opening;
    await new Promise((resolve) => setTimeout(resolve, 20));
    results.card = { line: line(env), requests: windowRequests(env).length };
    // The fix travels as a query parameter, so it has to survive the trip: read back through
    // ``decodeURIComponent``, which is what the server's parser does with it.
    const asked = windowRequests(env)[0];
    results.card_url = asked ? asked.url : null;
    results.card_coords = asked
        ? decodeURIComponent(String(asked.url).split('location_input=')[1] || '')
        : null;
    env.evaluate('UI.closeCamera()');

    const out = workerEnv();
    out.evaluate("Object.defineProperty(Camera, 'isSupported', { value: true, configurable: true })");
    out.evaluate("Camera.start = async () => ({ getTracks: () => [] })");
    await out.evaluate("UI.openCamera('Clock Out', '30.05,31.23')");
    await new Promise((resolve) => setTimeout(resolve, 20));
    results.card_clock_out_requests = windowRequests(out).length;
    out.evaluate('UI.closeCamera()');
}

// 7. the minutes a gap is read aloud from
{
    const env = workerEnv();
    results.minutes = [0, 1, 59, 60, 65, 600, 1440, undefined, 'nonsense'].map((value) =>
        env.evaluate('UI.windowMinutes(' + JSON.stringify(value) + ')')
    );
}

// 8. every key this line can say exists in all three languages
//
// A missing translation falls back to English silently (``I18n.__``), so the table is read
// directly - the same rule the framing coach is held to.
{
    const env = workerEnv();
    const keys = ['punchWindowSite', 'punchWindowOnTime', 'punchWindowEarly', 'punchWindowLate', 'punchWindowOffSite'];
    results.translations = JSON.parse(env.evaluate(
        'JSON.stringify(' + JSON.stringify(keys) + '.map((key) => ({' +
        '  key: key,' +
        '  en: (TRANSLATIONS.en[key] || ""),' +
        '  ar: (TRANSLATIONS.ar[key] || ""),' +
        '  hi: (TRANSLATIONS.hi[key] || "")' +
        '})))'
    ));
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_card_names_the_site_its_window_and_the_verdict(results):
    on_time = results["on_time"]
    assert on_time["shown"] is True
    assert on_time["verdict"] == "on_time"
    assert on_time["text"] == "Downtown Tower A · clock-in window 04:00-06:30 · on time", on_time["text"]


def test_early_and_late_carry_how_far_outside_the_window_they_are(results):
    assert results["early"]["verdict"] == "early"
    assert results["early"]["text"].endswith("early — opens in 25m"), results["early"]["text"]
    assert results["late"]["verdict"] == "late"
    assert results["late"]["text"].endswith("late — the window closed 1h 5m ago"), results["late"]["text"]


def test_an_overnight_site_says_so_on_the_card(results):
    """The label comes from the server's resolved window, overnight marker included."""
    assert "21:30-05:30 (overnight)" in results["overnight"]["text"], results["overnight"]["text"]
    assert results["overnight"]["text"].startswith("New Capital Zone B")


def test_a_worker_outside_every_site_is_told_where_to_stand(results):
    """The punch is refused there, so a window would explain the wrong decision."""
    off = results["off_site"]
    assert off["verdict"] == "off_site"
    assert "not inside any site" in off["text"], off["text"]
    assert "04:00" not in off["text"], "no window for a site this worker is not standing on"


def test_a_clock_out_does_not_ask_for_the_window_and_says_nothing(results):
    """It is a clock-*in* window: a worker leaving is not late for anything."""
    assert results["clock_out_requests"] == 0
    assert results["clock_out"]["shown"] is False
    assert results["clock_out"]["text"] == ""
    assert results["card_clock_out_requests"] == 0, "opening the card for a clock-out asked anyway"


def test_a_failed_look_up_leaves_the_card_exactly_as_it_was(results):
    """Advice must never become a gate: the shutter works whether or not this answered."""
    assert results["failed"]["shown"] is False
    assert results["failed"]["text"] == ""
    assert results["failed_requests"] == 1, "it was asked, and it answered 500"


def test_a_site_name_off_the_wire_is_written_as_text_never_as_markup(results):
    """``<img src=x onerror=...>`` is a site name here, and a name is not a tag."""
    hostile = results["hostile"]
    assert "<img src=x onerror=alert(1)>" in hostile["text"]
    assert "clock-in window 04:00-06:30" in hostile["text"], "the rest of the line survives it"
    assert hostile["html"] == "", "a site name must never be written as markup"


def test_the_card_carries_the_line_and_opening_it_is_what_asks(results):
    assert results["card_markup"] is True, "the punch card must have somewhere to put the answer"
    assert results["card"]["requests"] == 1
    assert results["card"]["line"]["shown"] is True
    assert "30.05,31.23" in results["card_coords"], (
        "the fix has to arrive as lat,lon, undamaged by the query encoding"
    )


def test_a_gap_in_minutes_reads_the_way_a_person_says_it(results):
    assert results["minutes"] == ["0m", "1m", "59m", "1h 0m", "1h 5m", "10h 0m", "24h 0m", "0m", "0m"]


def test_the_line_exists_in_every_language_the_card_speaks(results):
    for row in results["translations"]:
        assert row["en"], row["key"]
        assert row["ar"], f"{row['key']} has no Arabic"
        assert row["hi"], f"{row['key']} has no Hindi"
    by_key = {row["key"]: row for row in results["translations"]}
    assert "{site}" in by_key["punchWindowSite"]["en"]
    assert "{window}" in by_key["punchWindowSite"]["en"]
    for key in ("punchWindowEarly", "punchWindowLate"):
        assert "{minutes}" in by_key[key]["en"], key
        assert "{minutes}" in by_key[key]["ar"], key
        assert "{minutes}" in by_key[key]["hi"], key
