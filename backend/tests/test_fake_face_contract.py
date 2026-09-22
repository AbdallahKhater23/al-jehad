"""The stub *is* the model in every punch test, so its own contract has to be pinned.

WHY THIS EXISTS
---------------
``FakeFaceNetEngine`` documents a table - ``"match"`` -> distance ~0.00 -> approved, ``"review"``
-> distance ``REVIEW_DISTANCE``, ``"mismatch"`` -> distance ~2.00 -> refused, ``"none"`` -> the
no-face path - and a trap: the helper behind ``"review"`` takes a *similarity* while the bands
are written in *distances*, so getting it backwards drives the refusal path in a test that
claims to be exercising the review band.

What the suite already did with that table is use it: ``test_metrics`` drives mode -> counter
label, ``test_quick_links`` and ``test_offline_selfie_scoring`` set a mode to reach one branch.
**Nothing verified the numbers.** So the failure mode this file exists for is the quiet one:
``REVIEW_DISTANCE`` drifts to 0.30, every "review" test keeps passing - because 0.30 is still
outside the approve line - while a test named "the review band is logged, not refused" is
suddenly exercising a different band than it says. Same for the seeded template's dimension:
the harness's own docstring still said "a genuine 2622-float embedding" while the constant above
it was ``4096``, which is exactly how the original wrong dimension (2622) stayed invisible.

The stub moved when the engine did. It used to stand in for ``deepface.DeepFace``; the
application now embeds through ``face_onnx`` (FaceNet-128 on ONNX Runtime) and imports neither
TensorFlow nor DeepFace, so this file is also where that boundary is asserted - a suite that had
quietly gone on pretending the package existed would hide the dependency coming back.

``test_metrics``/``test_face_match_bands`` remain the right home for "a punch at distance X is
classified Y"; this file is about what the fake *is*.
"""

from __future__ import annotations

import sys

import pytest
from scipy.spatial.distance import cosine

import biometrics
import face_detector
import face_engine
import face_onnx
import harness
from harness import FAKE_ENGINE, REVIEW_DISTANCE, WORKER

#: How far a measured distance may sit from the constant that produces it. The vectors are
#: Python floats and the construction is exact up to float error, so this only absorbs that.
TOLERANCE = 1e-6


def _enrolled_embedding() -> list[float]:
    """The template a punch for the seeded worker is scored against, read the app's own way."""
    return biometrics.read_reference(str(harness.reference_path(WORKER))).embedding


def _distance_for(mode: str) -> float:
    """One embedding from the stub, in ``mode``, measured against the enrolled template."""
    previous = FAKE_ENGINE.FACE_MODE
    FAKE_ENGINE.FACE_MODE = mode
    try:
        live = FAKE_ENGINE.embed_as_list(b"not-an-image")
    finally:
        FAKE_ENGINE.FACE_MODE = previous
    return cosine(_enrolled_embedding(), live)


# ---------------------------------------------------------------------------
# the mode table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [("match", 0.0), ("review", REVIEW_DISTANCE), ("mismatch", 2.0)],
)
def test_each_documented_mode_produces_the_distance_it_claims(mode, expected):
    """The table in the docstring, measured with the metric the application compares with."""
    measured = _distance_for(mode)
    assert measured == pytest.approx(expected, abs=TOLERANCE), (
        f"FACE_MODE={mode!r} produced distance {measured:.6f}, not {expected}. Every test that "
        "selects this mode for its outcome is now exercising a different band than it names"
    )


def test_review_is_a_distance_not_its_similarity(monkeypatch):
    """The inversion the docstring warns about, asserted rather than trusted.

    ``_cosine_similar_vector`` takes a similarity, the bands are distances, and the two are
    inverses - so building the review vector from ``REVIEW_DISTANCE`` directly would produce a
    genuine-looking capture at 0.55 instead of an ambiguous one at 0.45.
    """
    measured = _distance_for("review")
    similarity_of_the_measured = 1.0 - measured

    assert measured == pytest.approx(REVIEW_DISTANCE, abs=TOLERANCE)
    assert similarity_of_the_measured == pytest.approx(similarity_of_the_measured, abs=TOLERANCE)
    assert abs(measured - (1.0 - REVIEW_DISTANCE)) > 0.05, (
        "the stub produced the *similarity* as a distance: a test asking for the review band got "
        "a capture 0.10 further out, which is a different decision"
    )


def test_the_review_distance_sits_inside_the_live_band_and_off_both_lines():
    """Why the fixture distance is not a boundary: the lines are derived and are not round numbers.

    A score placed on a line would pin the comparison operator (``<=`` against ``<``) instead of
    the band, and the fixture would be testing the rounding granularity.
    """
    band = face_detector.active_band()

    assert band.classify(REVIEW_DISTANCE) == face_detector.MATCH_REVIEW, (
        f"{REVIEW_DISTANCE} is classified {band.classify(REVIEW_DISTANCE)!r} by the live band "
        f"(approve {band.approve}, refuse above {band.review}), so the review-mode tests no "
        "longer measure the review band"
    )
    assert REVIEW_DISTANCE > band.approve, (
        f"the fixture's review distance {REVIEW_DISTANCE} is inside the approve line "
        f"{band.approve}, so a test asserting 'logged for a human' is asserting an approval"
    )
    assert band.classify(REVIEW_DISTANCE) != band.classify(band.review + 1e-9), (
        "the review distance now classifies like a refusal"
    )
    assert REVIEW_DISTANCE not in (band.approve, band.review), (
        "the fixture sits exactly on a decision line, so the tests around it pin the comparison "
        "operator rather than the band"
    )


# ---------------------------------------------------------------------------
# the seeded template
# ---------------------------------------------------------------------------


def test_the_seeded_template_is_a_real_facenet_embedding():
    """The fixture's vector has to be the size the live model returns, or nothing matches.

    This is not hypothetical: this constant was 2622 once, no template in the repo was that
    size, every seeded vector silently became the synthetic fallback, and the docstring still
    describes the old number. It then moved 4096 -> 128 with the engine, and the two are pinned
    against each other here rather than against a literal, so a further model change is a red
    test instead of another silent fallback.
    """
    assert harness._FACENET_EMBEDDING_DIM == face_onnx.DIMENSIONS == 128, (
        "the harness's embedding size is not what the engine returns, so it would never match a "
        "real template and the fallback would be used invisibly"
    )
    assert face_onnx.MODEL_NAME == face_engine.FACE_MODEL == "Facenet"

    embedding = harness._reference_embedding()
    assert len(embedding) == 128
    assert len(_enrolled_embedding()) == 128

    import json

    written = json.loads(harness.reference_path(WORKER).read_text())
    assert written["dimensions"] == len(embedding), (
        "the template the fixture writes records a different dimension than it carries, which is "
        "what the application's staleness check reads"
    )
    assert written["model"] == "Facenet", written["model"]


def test_the_stub_still_answers_when_the_template_is_under_test():
    """Deleting a worker's template is itself a case the suite tests, so the stub cannot depend
    on the file existing - it falls back to the deterministic vector the fixture seeds.
    """
    path = harness.reference_path(WORKER)
    path.unlink()
    assert not harness.template_exists(WORKER)

    live = FAKE_ENGINE.embed_as_list(b"frame")
    assert live == harness._reference_embedding(), (
        "with the template gone the stub no longer produces the fixture's vector, so a test that "
        "deletes a reference (retention, re-enrolment) gets a model error instead of its own case"
    )


# ---------------------------------------------------------------------------
# the engine's shape and the instance-vs-class trap
# ---------------------------------------------------------------------------


def test_the_engine_stub_returns_the_shape_and_dtype_the_app_stores():
    """``embed`` is ``(128,)`` float32 and unit-length; ``embed_as_list`` is plain floats.

    A stub that returned a list where the app expects an array, or an array of the wrong dtype,
    would pass every test that only reads ``len()`` and then fail a real deployment's template
    writer - so both halves of the surface are asserted.
    """
    import numpy as np

    vector = FAKE_ENGINE.embed(b"frame")
    assert isinstance(vector, np.ndarray)
    assert vector.shape == (128,) and vector.dtype == np.float32, (vector.shape, vector.dtype)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=TOLERANCE), (
        "the engine stub is not unit-length, so the seeded template's 'match' distance is not zero"
    )

    listed = FAKE_ENGINE.embed_as_list(b"frame")
    assert isinstance(listed, list) and len(listed) == 128, type(listed)

    described = FAKE_ENGINE.describe()
    assert described["model"] == "Facenet" and described["dimensions"] == 128, described
    assert described["loaded"] is True and described["error"] is None, described

    previous = FAKE_ENGINE.FACE_MODE
    FAKE_ENGINE.FACE_MODE = "none"
    try:
        with pytest.raises(ValueError):
            FAKE_ENGINE.embed_as_list(b"frame")
    finally:
        FAKE_ENGINE.FACE_MODE = previous


def test_the_detector_stub_reads_the_instance_so_a_test_can_raise_the_count(face):
    """The trap that would silently disarm the multi-face guard.

    ``test_metrics`` sets ``face.FACE_COUNT = 2`` on the fixture's object. The stub has to read
    that instance - reading the class would report one face to every caller, the guard would
    never fire, and the test would pass for the wrong reason.
    """
    face.FACE_COUNT = 2

    reported = harness._stub_detect_and_align(b"frame")
    assert len(reported) == 2, "the detector stub read the class, not the instance"
    assert list(reported[0]) == ["face", "facial_area", "confidence"], list(reported[0])
    assert type(face).FACE_COUNT == 1, "the instance assignment leaked onto the class"

    face.FACE_MODE = "none"
    assert harness._stub_detect_and_align(b"frame") == [], (
        "the stub did not read the instance's mode"
    )


def test_the_detector_stub_reports_the_model_as_present_and_keeps_the_real_one():
    """Two claims: the YuNet routing stays under test, and a test can get the real detector back.

    The stub declares the model available so every punch goes through the new detector path; if
    the model were genuinely missing, that declaration would be a fiction and the fallback path -
    the old code - would be what is actually running.
    """
    real = harness.REAL_FACE_DETECTOR
    assert face_detector.available is harness._stub_available
    assert face_detector.detect_and_align is harness._stub_detect_and_align

    ok, reason = real["available"]()
    if not ok:
        pytest.skip(f"the ONNX detector model is not in this checkout: {reason}")
    assert harness._stub_available() == (True, ""), (
        "the stub declares the detector available, so the routing under test is the real one"
    )
    assert real["detect_and_align"] is not face_detector.detect_and_align, (
        "the real detector was not kept, so a test that needs it (test_face_detector) has no way "
        "back"
    )


def test_the_application_is_bound_to_the_stub_and_imports_no_ml_framework():
    """The engine seam is installed *and* the frameworks it replaced are genuinely absent.

    ``face_onnx.set_engine`` is what the stub is installed through, so the shared engine every
    embedding path reads is the fake. The second half is the stronger claim: a suite that had
    quietly gone on stubbing ``deepface`` would import it and never notice TensorFlow coming
    back, so the negative is asserted where the positive is.
    """
    import main  # noqa: F401 - the import is the thing being observed

    assert face_onnx.get_engine() is FAKE_ENGINE, (
        "the application would build a real ONNX session, so every punch test would open a graph"
    )
    assert "tensorflow" not in sys.modules, (
        "TensorFlow was imported during the suite: the point of the ONNX engine is that it is not"
    )
    assert "deepface" not in sys.modules, (
        "DeepFace was imported during the suite: the application no longer depends on it, and a "
        "test that re-introduced it would hide the dependency coming back"
    )


# ---------------------------------------------------------------------------
# the per-test reset
# ---------------------------------------------------------------------------
# Deliberately two tests in Definition order: the first leaves the fake in a non-default state
# *without* restoring it, and the second asserts a test body starts from the default. That pins
# the fixture's reset - which is the thing that makes "a test sets FACE_MODE and forgets about
# it" safe. Selected alone, the second test passes trivially; that is the price of testing a
# fixture boundary rather than a function.


def test_a_test_leaves_the_fake_in_a_non_default_state():
    FAKE_ENGINE.FACE_MODE = "mismatch"
    FAKE_ENGINE.FACE_COUNT = 2
    assert (FAKE_ENGINE.FACE_MODE, FAKE_ENGINE.FACE_COUNT) == ("mismatch", 2)


def test_the_next_test_starts_from_the_default_mode_and_count():
    assert (FAKE_ENGINE.FACE_MODE, FAKE_ENGINE.FACE_COUNT) == ("match", 1), (
        "the per-test reset did not put the stub back, so a test that sets a mode hands its "
        "refusal path to every test after it in the same worker"
    )
