"""The live framing hint on the camera card.

WHY THIS EXISTS
---------------
A punch used to be take-it-and-find-out: the card was a camera, a shutter and a static
line of advice, and what the server thought of the photo arrived afterwards as a refusal
about a *score*. This suite is about the part that moved: the card now watches the frame
while it is still open and says the one thing that would help - too dark, no face yet,
move closer - so a worker fixes the photo before taking it.

What is asserted here is the advice's rules (pure numbers in, translation key out), the
line it paints and the tone it paints it in, that every piece of advice exists in all
three languages, and that the hint is inside the card and stops with it. The rules are
deliberately ordered, so the ordering is asserted too: light is the fix for most bad
photos on a site, and being in the dark outranks a face-count complaint.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
const env = boot();

/** One frame's measurements, with sane defaults so a scenario states only its point. */
function frame(overrides) {
    return Object.assign({
        luminance: 120,
        contrast: 30,
        faceDetection: true,
        face: { count: 1, area: 0.4 }
    }, overrides || {});
}

const ADVICE = {
    dark: frame({ luminance: 20 }),
    bright: frame({ luminance: 240 }),
    // A covered lens or a blank wall: nothing in the frame to match a face against.
    flat: frame({ luminance: 120, contrast: 3 }),
    no_face: frame({ face: { count: 0, area: 0 } }),
    many_faces: frame({ face: { count: 2, area: 0.5 } }),
    closer: frame({ face: { count: 1, area: 0.05 } }),
    further: frame({ face: { count: 1, area: 0.9 } }),
    good: frame({}),
    // Dark *and* faceless: the light is the thing to fix first.
    dark_and_faceless: frame({ luminance: 10, face: { count: 0, area: 0 } }),
    // No detector in this browser: say nothing rather than guess about faces.
    no_detector: frame({ faceDetection: false, face: null }),
    no_detector_dark: frame({ luminance: 20, faceDetection: false, face: null })
};

const results = {};

results.advice = {};
Object.keys(ADVICE).forEach((name) => {
    results.advice[name] = env.evaluate('UI.framingAdvice(' + JSON.stringify(ADVICE[name]) + ')');
});

// Every key the rules can produce, and the tone it is painted in.
results.keys = Object.keys(results.advice).map((name) => results.advice[name]).filter(Boolean);
results.tones = env.evaluate('UI.FRAMING_TONES');
results.bad_keys = results.keys.filter((key) => !results.tones[key]);

// A missing translation would silently fall back to English (``I18n.__``), so the
// languages are read straight out of the table instead.
// ``evaluate`` hands back the value the expression evaluated to, so the table is built
// inside the VM and only then serialised for the trip out.
results.translations = JSON.parse(env.evaluate(
    'JSON.stringify(' + JSON.stringify(results.keys) + '.map((key) => ({' +
    '  key: key,' +
    '  en: (TRANSLATIONS.en[key] || ""),' +
    '  ar: (TRANSLATIONS.ar[key] || ""),' +
    '  hi: (TRANSLATIONS.hi[key] || "")' +
    '})))'
));

// 2. what the line says when it is painted, and in which tone
{
    env.evaluate("UI.showFramingHint('framingDark')");
    const dark = env.evaluate(
        "({ hidden: document.getElementById('framingHint').hidden," +
        "   text: document.getElementById('framingHint').textContent," +
        "   tone: document.getElementById('framingHint').dataset.framing })"
    );
    env.evaluate('UI.showFramingHint(null)');
    const cleared = env.evaluate(
        "({ hidden: document.getElementById('framingHint').hidden," +
        "   text: document.getElementById('framingHint').textContent," +
        "   tone: document.getElementById('framingHint').dataset.framing })"
    );
    results.painted = { dark: dark, cleared: cleared, translated: env.evaluate("I18n.__('framingDark')") };
}

// 3. the coach lives inside the card and stops with it
//
// Everything the page owns has to be reached *inside* the VM (``env.evaluate``): the
// app's files declare their objects in the VM's global scope, not in this script's.
{
    // The VM has no getUserMedia, so the two things the card asks of the device are
    // stubbed. What this asserts is the page's own code, not the stub.
    env.evaluate("Object.defineProperty(Camera, 'isSupported', { value: true, configurable: true })");
    env.evaluate("Camera.start = async () => ({ getTracks: () => [] })");
    // The card is painted before the stream is awaited and the coach starts after it, so
    // ``openCamera`` is read mid-flight (what a worker sees the moment the card opens)
    // and then awaited (what the card is doing a moment later).
    const opening = env.evaluate("UI.openCamera('Clock In', '30.05,31.23')");
    const overlays = env.evaluate("(document.body.__children || []).filter((el) => el.id === 'cameraOverlay')");
    const markup = overlays.length ? overlays[0].innerHTML : '';
    await opening;
    const running = env.evaluate('UI._framing.timer !== null');
    env.evaluate('UI.closeCamera()');
    results.card = {
        overlays: overlays.length,
        has_hint: markup.indexOf('id="framingHint"') >= 0,
        starts_hidden: /id="framingHint"[^>]*hidden/.test(markup),
        keeps_advice: markup.indexOf(env.evaluate("I18n.__('captureHint')")) >= 0,
        coach_running: running,
        stopped_with_card: env.evaluate('UI._framing.timer === null')
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_advice_for_a_frame(results):
    """One measurement in, one instruction out - and nothing said when nothing is wrong."""
    assert results["advice"] == {
        "dark": "framingDark",
        "bright": "framingBright",
        "flat": "framingFlat",
        "no_face": "framingNoFace",
        "many_faces": "framingManyFaces",
        "closer": "framingCloser",
        "further": "framingFurther",
        "good": "framingGood",
        "dark_and_faceless": "framingDark",
        "no_detector": None,
        "no_detector_dark": "framingDark",
    }, results["advice"]


def test_every_piece_of_advice_has_a_tone_and_is_translated(results):
    assert results["keys"], "the rules produced no advice at all"
    assert results["bad_keys"] == [], (
        f"advice with no tone - it would paint in the default colour: {results['bad_keys']}"
    )
    missing = [
        entry["key"] for entry in results["translations"]
        if not (entry["en"] and entry["ar"] and entry["hi"])
    ]
    assert missing == [], (
        f"advice that exists in only some of the three languages (__ falls back to "
        f"English, so a worker in Arabic would read English): {missing}"
    )


def test_the_hint_line_says_it_and_hides_when_it_has_nothing_to_say(results):
    painted = results["painted"]
    assert painted["dark"]["text"] == painted["translated"], painted
    assert painted["dark"]["text"] != "framingDark", "the key itself is not a message"
    assert painted["dark"]["tone"] == "bad", painted
    assert painted["cleared"] == {"hidden": True, "text": "", "tone": ""}, (
        "with nothing to say the line must go away, not sit there empty"
    )


def test_the_hint_is_part_of_the_card_and_stops_with_it(results):
    card = results["card"]
    assert card["overlays"] == 1, "the camera card did not open"
    assert card["has_hint"], "the framing hint is not in the card"
    assert card["starts_hidden"], "an empty hint line must not take space before it has advice"
    assert card["keeps_advice"], "the static capture hint must stay under the shutter"
    assert card["coach_running"], "the coach never started"
    assert card["stopped_with_card"], "the coach outlived the card it was watching"
