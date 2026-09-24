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
        // Inside the good window by default, so a scenario states only its point: a fixture
        // default that is itself "too close" hides the rule being tested behind another one.
        face: { count: 1, area: 0.05 }
    }, overrides || {});
}

const ADVICE = {
    dark: frame({ luminance: 20 }),
    bright: frame({ luminance: 240 }),
    // A covered lens or a blank wall: nothing in the frame to match a face against.
    flat: frame({ luminance: 120, contrast: 3 }),
    no_face: frame({ face: { count: 0, area: 0 } }),
    // Two people at a sensible distance: the count is the complaint, not the size.
    many_faces: frame({ face: { count: 2, area: 0.05 } }),
    // The deployment's own case, from its own numbers: a face filling the frame comes back
    // from the detector as the *pieces* of one face - two boxes of 0.5 of a frame's area each
    // - and the worker was told to get somebody else out of a frame that held only them.
    too_close_split: frame({ face: { count: 2, area: 0.5 } }),
    // "Move closer" is for a genuinely marginal face only: below 1% of the frame is
    // past ~1.9 m on a 66° phone camera. 0.05 (≈0.7 m) and 0.03 (≈0.9 m) are IN the
    // good window now — the old 12% floor demanded an arm's-length selfie.
    closer: frame({ face: { count: 1, area: 0.005 } }),
    further: frame({ face: { count: 1, area: 0.5 } }),
    good: frame({ face: { count: 1, area: 0.03 } }),
    // The whole working band the pipeline was measured for must read as good framing:
    // 3% ≈ 0.7 m, 1.5% ≈ 1.0 m, 1.1% ≈ 1.2 m, 0.7% ≈ 1.5 m.
    good_far: frame({ face: { count: 1, area: 0.03 } }),
    good_1m: frame({ face: { count: 1, area: 0.015 } }),
    good_1_2m: frame({ face: { count: 1, area: 0.011 } }),
    good_1_5m: frame({ face: { count: 1, area: 0.007 } }),
    // A very close frame (arm's length or nearer) now gets the advice the old rule gave
    // everybody: the face overfills the alignment template's margins above 35% (~0.21 m).
    too_close: frame({ face: { count: 1, area: 0.4 } }),
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

// 1b. who is actually in the frame, from the browser detector's raw boxes
//
// The advice above is only as good as its count, and the count is where this went wrong:
// window.FaceDetector reports a box per detection *response*, so one worker standing alone
// arrives as two overlapping boxes, and a face-shaped patch of wall arrives as a third.
// Both used to read as "Only one person can be in the frame". These cases pin the counting
// rule, and then the advice it produces.
{
    const box = (x, y, w, h) => ({ boundingBox: { x: x, y: y, width: w, height: h } });
    const cases = {
        one_worker: [box(120, 60, 90, 110)],
        // The same face twice: nested (containment is what catches this - the union makes
        // the IoU look moderate) and heavily overlapping.
        duplicated_nested: [box(120, 60, 90, 110), box(126, 66, 82, 100)],
        duplicated_overlapping: [box(120, 60, 90, 110), box(130, 64, 88, 106)],
        // Two people whose boxes touch but are not one face: this must NOT be merged.
        two_overlapping: [box(120, 60, 90, 110), box(170, 60, 90, 110)],
        // A face-shaped speck of poster, 4x5 px of a 320x240 sample.
        worker_and_speck: [box(120, 60, 90, 110), box(300, 20, 4, 5)],
        specks_only: [box(10, 10, 4, 5), box(300, 20, 3, 4)],
        // Two workers standing at the gate.
        two_people: [box(40, 60, 90, 110), box(200, 60, 88, 108)],
        none: []
    };
    results.subjects = {};
    Object.keys(cases).forEach((name) => {
        results.subjects[name] = env.evaluate(
            'UI.framingSubjects(' + JSON.stringify(cases[name]) + ', 320, 240)'
        );
    });
    // What the coach says about the two cases that used to be wrong, end to end.
    results.subject_advice = {};
    ['one_worker', 'duplicated_nested', 'duplicated_overlapping', 'worker_and_speck',
     'specks_only', 'two_people'].forEach((name) => {
        results.subject_advice[name] = env.evaluate(
            'UI.framingAdvice(' +
            JSON.stringify(frame({ face: results.subjects[name] })) +
            ')'
        );
    });
    results.sample_width = env.evaluate('UI.FRAMING_SAMPLE_WIDTH');
}

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
    // The device is overridden rather than installed: this suite drives the card only far
    // enough to see the hint inside it, and the advice itself is asserted from numbers above.
    // A suite about the capture path uses the camera the harness models (see
    // test_frontend_punch_capture).
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
        "too_close_split": "framingFurther",
        "closer": "framingCloser",
        "further": "framingFurther",
        "good": "framingGood",
        "good_far": "framingGood",
        "good_1m": "framingGood",
        "good_1_2m": "framingGood",
        "good_1_5m": "framingGood",
        "too_close": "framingFurther",
        "dark_and_faceless": "framingDark",
        "no_detector": None,
        "no_detector_dark": "framingDark",
    }, results["advice"]


def test_being_too_close_outranks_the_count(results):
    """The deployment's report, end to end: one face at the lens is not two people.

    A worker got "there is multiple faces" while holding one face close to the phone, and the
    server agreed with the phone: it logged ``2 subject-sized face(s) [585, 576]`` on a frame a
    phone captures in portrait. Both were reading the pieces of one face. The advice that is true
    in *both* readings is the distance, so it is decided before the count - and that ordering is
    the thing this pins, not the words.
    """
    assert results["advice"]["too_close_split"] == "framingFurther", (
        "a face that overfills the frame gets the distance advice whatever the count says"
    )
    assert results["advice"]["many_faces"] == "framingManyFaces", (
        "two people at a distance must still be told about the second person"
    )
    assert results["advice"]["too_close"] == "framingFurther"


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


# ---------------------------------------------------------------------------
# who is actually in the frame
# ---------------------------------------------------------------------------
# The coach's count is what produces "Only one person can be in the frame", and the browser's
# detector does not report people - it reports *responses*. One worker standing alone arrives
# as two overlapping boxes often enough that workers were being told to get somebody else out
# of a frame that held only them, and one of them was stopped with a sentence about a person
# who was never there. These pin the counting rule and the advice it feeds.
def test_one_face_reported_twice_is_one_person(results):
    for case in ("duplicated_nested", "duplicated_overlapping"):
        subject = results["subjects"][case]
        assert subject["count"] == 1, f"{case}: {subject}"
        assert subject["merged"] == 1, f"{case}: {subject}"
        assert results["subject_advice"][case] != "framingManyFaces", (
            f"{case}: one face in two boxes is not two people"
        )


def test_a_face_shaped_speck_is_not_a_person(results):
    subject = results["subjects"]["worker_and_speck"]
    assert subject["count"] == 1, subject
    assert subject["rawCount"] == 2 and subject["ignored"] == 1, subject
    assert results["subject_advice"]["worker_and_speck"] == "framingGood", (
        "a speck of poster behind the worker must not be advice about a second person"
    )


def test_specks_alone_are_no_face_at_all(results):
    subject = results["subjects"]["specks_only"]
    assert subject["count"] == 0, subject
    assert results["subject_advice"]["specks_only"] == "framingNoFace", (
        "with only specks in the frame, the honest instruction is that no face is there"
    )


def test_two_people_are_still_two_people(results):
    subject = results["subjects"]["two_people"]
    assert subject["count"] == 2, subject
    assert subject["merged"] == 0, subject
    assert results["subject_advice"]["two_people"] == "framingManyFaces", (
        "the rule that matters must survive the fix: two workers is still two people"
    )


def test_boxes_that_only_touch_are_not_merged(results):
    assert results["subjects"]["two_overlapping"]["count"] == 2, results["subjects"]["two_overlapping"]


def test_the_area_is_the_subject_not_the_sum(results):
    """The area drives "move closer"/"move further", so it must describe the person in front."""
    alone = results["subjects"]["one_worker"]["area"]
    with_speck = results["subjects"]["worker_and_speck"]["area"]
    assert abs(alone - with_speck) < 1e-9, (alone, with_speck)
    assert 0.006 < alone < 0.35, f"the good-window fixture should read as good framing: {alone}"


def test_the_sample_is_wide_enough_for_the_face_detector(results):
    assert results["sample_width"] >= 240, (
        "160 px was too coarse for window.FaceDetector: it produced the overlapping boxes "
        "that made one worker look like two"
    )
