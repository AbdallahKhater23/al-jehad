"""The detector seam: geometry, degradation, and the pipeline label it hands out.

WHAT THESE TESTS CAN AND CANNOT SEE
-----------------------------------
``harness`` replaces ``face_detector.available`` and ``detect_and_align`` with stubs, so the
rest of the suite drives the *routing* (per-face embedding, the face-count guard, the crop)
without an ONNX model or a real face. This file deliberately does not use those stubs: it
tests the detector's own half - the parts that decide what a worker's photo even is.

What is **not** here is a claim about real detection quality. Proving that YuNet finds a
hard-hatted face in glare better or worse than MTCNN needs a labelled corpus of site photos,
which this repository does not have. The speed difference is measured in
``backend/models/README.md`` under a stated method; a timing assertion in a test would be a
flaky test, not evidence.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

import config
import face_detector
import harness

#: The artefact this code was written and measured against. Pinned by digest rather than by
#: name, because a detector swapped for a differently-trained file of the same name changes
#: every template on disk and nothing else would notice.
SHIPPED_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def _row(*, x=10, y=20, w=40, h=50, landmarks=None) -> np.ndarray:
    """One YuNet-shaped row: box, five (x, y) landmarks, score."""
    points = landmarks
    if points is None:
        points = [(20, 30), (40, 30), (30, 42), (22, 55), (38, 55)]
    flat = [value for point in points for value in point]
    return np.array([x, y, w, h, *flat, 0.93], dtype=np.float32)


@pytest.fixture
def real_detector(monkeypatch):
    """The unstubbed functions, with the model load state forgotten for this test.

    ``harness`` replaces ``available`` and ``detect_and_align`` at import time, so without
    putting the originals back every test in this file would be testing the stub. The
    originals come from ``harness.REAL_FACE_DETECTOR`` rather than from the module, because by
    the time this file is imported the module *is* the stub.

    The load state is cleared on every test so a test that points the model path elsewhere
    gets that path looked at, instead of a detector cached by an earlier test.
    """
    module = sys.modules[face_detector.__name__]
    for name, function in harness.REAL_FACE_DETECTOR.items():
        monkeypatch.setattr(module, name, function, raising=True)
    monkeypatch.setattr(face_detector, "_loaded", False, raising=True)
    monkeypatch.setattr(face_detector, "_detector", None, raising=True)
    monkeypatch.setattr(face_detector, "_load_error", None, raising=True)
    return face_detector


def _png(width: int = 160, height: int = 160) -> bytes:
    import cv2

    frame = np.full((height, width, 3), 60, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", frame)
    assert ok
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# 1. the artefact
# ---------------------------------------------------------------------------
def test_the_model_ships_with_the_code():
    """In the repository, not behind a deploy step.

    The rule this is an exception to - ``backend/models/*.onnx`` is git-ignored - exists for
    large weights. This one is 232 KB and is the difference between a 10 ms and a 308 ms
    detection on every punch, so a checkout that has to fetch it is a checkout that runs the
    slow detector until somebody remembers.
    """
    path = face_detector.DEFAULT_MODEL_PATH
    assert path.exists(), f"the detector model is missing: {path}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == SHIPPED_SHA256, (
        "the committed detector is not the one this code was written against; every stored "
        "face template is invalidated by a change here"
    )


def test_the_gitignore_lets_the_model_through():
    """The exception is stated, not implied by a file that happens to be tracked."""
    text = (harness.BACKEND_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert "backend/models/*.onnx" in text, "the ignore rule this is an exception to"
    assert "!backend/models/face_detection_yunet_2023mar.onnx" in text


# ---------------------------------------------------------------------------
# 2. the crop is the contract
# ---------------------------------------------------------------------------
def test_the_crop_is_the_warp_of_the_landmarks_onto_the_template(real_detector):
    """``align`` is the estimated transform of the landmarks, and the transform is a similarity.

    Two things worth pinning, and neither is the pixels alone:

    * the crop must be the *warp of the landmark estimate* - so the output is compared against
      ``cv2.warpAffine`` driven by the same matrix, which is what makes the crop a function of
      the landmarks rather than of the bounding box;
    * the transform must be a **similarity** - rotation, uniform scale, translation, no shear.
      A shear or a non-uniform scale would stretch the face, and the crop would then depend on
      the pose in a way no other capture reproduces.

    The residual is not zero and must not be asserted as such: five points cannot be fitted
    exactly by a four-degree-of-freedom transform, and the mouth corners are where the miss
    shows up (here ~7 px on the worst corner). That is the expected behaviour of a partial
    affine fit, not a defect - OpenCV's own recogniser sample does the same.
    """
    import cv2

    frame = np.zeros((120, 120, 3), dtype=np.uint8)
    landmarks = np.array(
        [(30.0, 40.0), (80.0, 42.0), (55.0, 70.0), (38.0, 95.0), (72.0, 97.0)], dtype=np.float32
    )
    row = _row(landmarks=[tuple(point) for point in landmarks])

    crop = real_detector.align(frame, row)
    assert crop.shape == (face_detector.ALIGNED_SIZE, face_detector.ALIGNED_SIZE, 3)

    matrix, _inliers = cv2.estimateAffinePartial2D(landmarks, face_detector._ALIGN_TEMPLATE)
    assert matrix is not None, "five points always yield an estimate"

    expected = cv2.warpAffine(
        frame, matrix, (face_detector.ALIGNED_SIZE, face_detector.ALIGNED_SIZE), flags=cv2.INTER_LINEAR
    )
    assert np.array_equal(crop, expected), "the crop is the warp, not a resized box"

    linear = matrix[:, :2]
    first, second = linear[:, 0], linear[:, 1]
    assert abs(float(np.dot(first, second))) < 1e-3, f"a shear would stretch the face: {linear}"
    assert np.isclose(np.linalg.norm(first), np.linalg.norm(second), rtol=1e-3), (
        f"a non-uniform scale would stretch the face: {linear}"
    )

    moved = cv2.transform(landmarks.reshape(1, -1, 2), matrix)[0]
    worst = float(np.abs(moved - face_detector._ALIGN_TEMPLATE).max())
    assert worst < 10.0, f"the landmarks barely reached the template: {worst:.1f}px out"


def test_a_change_of_scale_does_not_change_the_crop(real_detector):
    """Distance from the camera is the commonest difference between two captures of one face.

    Alignment normalises it, so a worker standing one step back does not score differently
    from the same worker one step closer - which is the whole reason a template can be taken
    at the gate and matched at the door.
    """
    import cv2

    frame = np.zeros((400, 400, 3), dtype=np.uint8)
    near = np.array(
        [(120.0, 150.0), (280.0, 150.0), (200.0, 220.0), (144.0, 300.0), (256.0, 300.0)],
        dtype=np.float32,
    )
    centre = near.mean(axis=0)
    matrix = cv2.getRotationMatrix2D((float(centre[0]), float(centre[1])), 0.0, 0.5)
    far = cv2.transform(near.reshape(1, -1, 2), matrix)[0]

    close_crop = real_detector.align(frame, _row(landmarks=[tuple(p) for p in near]))
    far_crop = real_detector.align(frame, _row(landmarks=[tuple(p) for p in far]))

    difference = np.abs(close_crop.astype(int) - far_crop.astype(int)).mean()
    assert difference < 1.0, (
        f"halving the face changed the crop by {difference:.2f} per pixel; scale is not "
        "being normalised"
    )


def test_a_rotation_is_removed_by_the_alignment(real_detector):
    """The point of aligning at all: the same face at an angle crops to the same picture."""
    import cv2

    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    upright = np.array(
        [(60.0, 80.0), (140.0, 80.0), (100.0, 120.0), (72.0, 160.0), (128.0, 160.0)],
        dtype=np.float32,
    )
    centre = upright.mean(axis=0)
    matrix = cv2.getRotationMatrix2D((float(centre[0]), float(centre[1])), 25.0, 1.0)
    tilted = cv2.transform(upright.reshape(1, -1, 2), matrix)[0]

    straight = real_detector.align(frame, _row(landmarks=[tuple(p) for p in upright]))
    rotated = real_detector.align(frame, _row(landmarks=[tuple(p) for p in tilted]))

    difference = np.abs(straight.astype(int) - rotated.astype(int)).mean()
    assert difference < 1.0, (
        f"a 25 degree tilt changed the crop by {difference:.2f} per pixel; alignment is not "
        "removing rotation, so a worker's score depends on how they held the phone"
    )


def test_degenerate_landmarks_fall_back_to_the_box(real_detector, monkeypatch):
    """A bad crop is still a crop.

    Refusing here would take a clock-in away over a landmark the detector was unsure about -
    the model's own threshold is the place for that judgement, not the geometry helper.
    """
    import cv2

    frame = np.full((100, 100, 3), 200, dtype=np.uint8)
    row = _row(x=10, y=10, w=30, h=30, landmarks=[(0, 0)] * 5)

    crop = real_detector.align(frame, row)
    assert crop.shape == (face_detector.ALIGNED_SIZE, face_detector.ALIGNED_SIZE, 3)
    assert crop.mean() == pytest.approx(200, abs=2), "the box crop's pixels, resized"


def test_a_box_outside_the_frame_still_returns_a_usable_crop(real_detector):
    """A detector can report a box that hangs off the edge; the model cannot take a 0x0 image."""
    frame = np.full((60, 60, 3), 120, dtype=np.uint8)
    row = _row(x=50, y=50, w=400, h=400, landmarks=[(1, 1)] * 5)

    crop = real_detector.align(frame, row)
    assert crop.shape == (face_detector.ALIGNED_SIZE, face_detector.ALIGNED_SIZE, 3)


# ---------------------------------------------------------------------------
# 3. the shape every caller reads
# ---------------------------------------------------------------------------
def test_detect_and_align_answers_the_shape_every_caller_reads(real_detector, monkeypatch):
    """``face_engine`` and ``quick_links`` read ``confidence`` and count the entries."""
    monkeypatch.setattr(
        face_detector,
        "detect_raw",
        lambda image: [_row(), _row(x=80, y=20, landmarks=[(0, 0)] * 5)],
    )

    faces = real_detector.detect_and_align(None)

    assert len(faces) == 2, "one entry per detected face"
    for face in faces:
        assert set(face) == {"face", "facial_area", "confidence"}
        assert set(face["facial_area"]) == {"x", "y", "w", "h"}
        assert isinstance(face["confidence"], float)
        assert face["face"].shape == (face_detector.ALIGNED_SIZE, face_detector.ALIGNED_SIZE, 3)


def test_a_frame_with_no_face_is_an_empty_list(real_detector, monkeypatch):
    monkeypatch.setattr(face_detector, "detect_raw", lambda image: [])
    assert real_detector.detect_and_align(None) == []


def test_the_five_landmarks_are_read_as_five_points(real_detector, monkeypatch):
    """Off-by-one here shifts every landmark and silently mis-aligns every crop."""
    captured = {}

    def capture(image):
        return [_row()]

    monkeypatch.setattr(face_detector, "detect_raw", capture)
    monkeypatch.setattr(
        face_detector,
        "align",
        lambda image, row: captured.setdefault("landmarks", tuple(np.round(row[4:14], 2))),
    )
    real_detector.detect_and_align(np.zeros((10, 10, 3), dtype=np.uint8))

    assert captured["landmarks"] == (20.0, 30.0, 40.0, 30.0, 30.0, 42.0, 22.0, 55.0, 38.0, 55.0)


# ---------------------------------------------------------------------------
# 4. what the caller had
# ---------------------------------------------------------------------------
def test_float_pixels_are_not_read_as_bytes(real_detector):
    """A 0..1 image read as 0..255 pixels is a black frame, and \"no face\" is the wrong answer.

    Callers hand round floats around - a normalised crop, a frame from a float pipeline - so
    this is not hypothetical: it is how a detector ends up finding nothing in a perfectly good
    photo.
    """
    frame = np.full((32, 32, 3), 0.5, dtype=np.float32)
    converted = real_detector._to_bgr(frame)
    assert converted.dtype == np.uint8
    assert converted.mean() == pytest.approx(127, abs=2)


def test_byte_pixels_are_left_alone(real_detector):
    frame = np.full((32, 32, 3), 200, dtype=np.uint8)
    assert np.array_equal(real_detector._to_bgr(frame), frame)


def test_a_path_is_read_from_disk(real_detector, tmp_path):
    import cv2

    target = tmp_path / "frame.png"
    target.write_bytes(_png())
    loaded = real_detector._to_bgr(str(target))
    assert loaded is not None and loaded.shape == (160, 160, 3)


def test_a_missing_path_is_no_face_rather_than_a_crash(real_detector, tmp_path):
    """``cv2.imread`` answers ``None`` for a file it cannot read, and every caller after it
    has to survive that - a punch must not 500 because a byte looked wrong."""
    assert real_detector._to_bgr(str(tmp_path / "nope.png")) is None
    assert real_detector.detect_raw(str(tmp_path / "nope.png")) == []


# ---------------------------------------------------------------------------
# 5. which pipeline is actually live
# ---------------------------------------------------------------------------
def test_the_pipeline_names_the_detector_that_would_really_run(real_detector, monkeypatch):
    """The label a template is written with, and the one a punch compares against.

    Recording the *intended* detector on a host that is running the fallback would label an
    MTCNN embedding as a YuNet one and lose the mismatch this exists to catch.
    """
    # ``_ensure``, not ``available``: ``active()`` asks the loader directly, and patching
    # only the public wrapper would leave this test asserting against the shipped model.
    monkeypatch.setattr(face_detector, "_ensure", lambda: (True, ""))
    assert face_detector.active_pipeline() == face_detector.PIPELINE

    monkeypatch.setattr(face_detector, "_ensure", lambda: (False, "no model"))
    assert face_detector.active_pipeline() == face_detector.FALLBACK_PIPELINE
    assert face_detector.active_detector() == face_detector.FALLBACK_DETECTOR
    assert face_detector.FALLBACK_PIPELINE != face_detector.PIPELINE, (
        "the fallback has to be distinguishable, or templates written under it would read "
        "as current once the model arrived"
    )


def test_a_missing_model_is_reported_not_raised(real_detector, monkeypatch, tmp_path):
    """Never raises: a checkout without the model keeps working on the previous path."""
    monkeypatch.setattr(face_detector, "model_path", lambda: tmp_path / "absent.onnx")
    monkeypatch.setattr(face_detector, "_loaded", False)

    ok, reason = face_detector.available()
    assert ok is False and "not found" in reason
    assert face_detector.detect_raw(np.zeros((8, 8, 3), dtype=np.uint8)) == []
    assert face_detector.describe()["available"] is False


def test_a_pinned_digest_refuses_a_different_file(real_detector, monkeypatch):
    """``FACE_DETECTOR_MODEL_SHA256`` is the operator's pin, and it has to be checked."""
    monkeypatch.setattr(face_detector, "_configured_sha256", lambda: "0" * 64)
    monkeypatch.setattr(face_detector, "_loaded", False)
    monkeypatch.setattr(face_detector, "_detector", None)
    monkeypatch.setattr(face_detector, "_load_error", None)

    ok, reason = face_detector.available()
    assert ok is False, "a pinned deployment must not load an unexpected detector"
    assert "SHA256" in reason


def test_describe_reports_enough_to_explain_a_deployment(real_detector, monkeypatch):
    described = face_detector.describe()
    assert set(described) >= {
        "detector",
        "pipeline",
        "active_detector",
        "active_pipeline",
        "available",
        "model",
        "model_fingerprint",
        "error",
    }
    assert described["pipeline"] == face_detector.PIPELINE
    assert described["model_fingerprint"], "the shipped model has a fingerprint"


def test_the_recorded_pipeline_is_in_the_template_a_person_is_enrolled_with():
    """End to end through the writer: what ``write_reference`` records is what is live."""
    path = harness.seed_reference(harness.WORKER)

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["pipeline"] == face_detector.active_pipeline()
    assert payload["dimensions"] == len(payload["embedding"])
    assert payload["model"], "the recognition model is recorded beside the detector"


# ---------------------------------------------------------------------------
# 1b. the close-up regime: a face too large for the detector to answer about
# ---------------------------------------------------------------------------
# Measured on this repository's own corpus (35 single-face photographs, each driven to a chosen
# face width in a 720x1280 portrait frame): at native resolution a face wider than half the frame
# comes back as *pieces* of one face - 10 of 105 close-up frames reported two subject-sized boxes
# while holding one person - and a worker four times closer than the band the pipeline was derived
# for is refused for "more than one face". Reading every frame at a smaller width instead fixes the
# close-ups but costs the far range (2 of 60 at 480 px, 15 of 60 at 320 px), which is the regime
# commit 60bc460 exists to serve. So the second read is conditional, and these pin the condition.

def _fake_passes(monkeypatch, *, native, reduced, seen=None):
    """``_detect_rows`` replaced by a fake that answers per *input width*, and counts its calls."""
    def fake(frame):
        if seen is not None:
            seen.append(frame.shape[1])
        return reduced if frame.shape[1] <= face_detector.CLOSE_FRAME_RETRY_PX else native

    monkeypatch.setattr(face_detector, "_detect_rows", fake)


def test_the_retry_width_is_a_scale_this_detector_was_built_for():
    """Not a round number somebody liked: tied to the input the anchors are laid out for."""
    assert face_detector.CLOSE_FRAME_RETRY_PX > face_detector.DETECTOR_INPUT_SIZE
    assert face_detector.CLOSE_FRAME_RETRY_PX <= 2 * face_detector.DETECTOR_INPUT_SIZE
    assert 0 < face_detector.MAX_SUBJECT_FRAME_FRACTION < 1


def test_a_normal_frame_is_read_once_and_handed_on_untouched(real_detector, monkeypatch):
    seen = []
    _fake_passes(monkeypatch, native=[_row(x=10, y=20, w=80, h=90)], reduced=[], seen=seen)

    rows = face_detector.detect_raw(np.zeros((960, 1280, 3), dtype=np.uint8))

    assert seen == [1280], "one pass: a frame whose faces are face-sized costs one model call"
    assert len(rows) == 1
    assert (float(rows[0][0]), float(rows[0][2])) == (10.0, 80.0), "the row is not rescaled"


def test_a_face_too_close_to_be_one_face_is_read_again_smaller(real_detector, monkeypatch):
    """The refusal the deployment actually produced: one person, two boxes of the same size."""
    seen = []
    native = [_row(x=110, y=150, w=482, h=621), _row(x=111, y=735, w=592, h=615)]
    _fake_passes(monkeypatch, native=native, reduced=[_row(x=60, y=200, w=300, h=380)], seen=seen)

    rows = face_detector.detect_raw(np.zeros((1280, 720, 3), dtype=np.uint8))

    assert seen == [720, face_detector.CLOSE_FRAME_RETRY_PX], (
        "a frame that reports a face too large to be one is read again at the retry width"
    )
    assert len(rows) == 1, "the second read's answer is the one that survives"


def test_the_second_read_answers_in_the_frames_own_pixels(real_detector, monkeypatch):
    """Box *and* landmarks: a crop taken from the reduced frame's coordinates is the wrong crop."""
    _fake_passes(
        monkeypatch,
        native=[_row(x=100, y=100, w=600, h=700)],
        reduced=[_row(x=60, y=200, w=300, h=380, landmarks=[(70, 210)] * 5)],
    )

    rows = face_detector.detect_raw(np.zeros((1280, 960, 3), dtype=np.uint8))

    back = 960 / face_detector.CLOSE_FRAME_RETRY_PX
    assert len(rows) == 1
    assert float(rows[0][2]) == pytest.approx(300 * back, rel=1e-6), "the box is mapped back"
    assert float(rows[0][4]) == pytest.approx(70 * back, rel=1e-6), "so is the first landmark"


def test_a_second_read_that_finds_nothing_keeps_the_first_answer(real_detector, monkeypatch):
    """Turning a close-up into "no face" would be a worse answer than a confused first pass."""
    native = [_row(x=110, y=150, w=482, h=621)]
    _fake_passes(monkeypatch, native=native, reduced=[])

    rows = face_detector.detect_raw(np.zeros((1280, 720, 3), dtype=np.uint8))

    assert len(rows) == 1 and float(rows[0][2]) == 482.0


def test_a_frame_already_at_the_retry_width_is_not_resampled(real_detector, monkeypatch):
    """A small frame is the far-range case by construction: nothing to resample, nothing to gain."""
    seen = []
    width = face_detector.CLOSE_FRAME_RETRY_PX
    _fake_passes(monkeypatch, native=[_row(x=0, y=0, w=width - 10, h=width)], reduced=[], seen=seen)

    face_detector.detect_raw(np.zeros((960, width, 3), dtype=np.uint8))

    assert seen == [width], "a frame no wider than the retry width is read exactly once"


# ---------------------------------------------------------------------------
# the startup warm-up: the spike that should not land on the first punch
# ---------------------------------------------------------------------------
# Opening the graph is ~3 MB; the first *detection* is what costs - ~106 MB peak and ~88 MB
# kept at the ceiling, sized by the frame's area (tools/yunet_memory.py). Nothing ran one
# before a punch, so the first worker of the day paid it, queue and all.
# ---------------------------------------------------------------------------
def test_the_warm_frame_is_the_one_the_chain_would_hand_the_detector():
    """The warm size has to be the size a punch's frame has, or it moves the spike nowhere.

    ``uploads.face_frame`` resizes every frame the models see to ``settings.face_frame_max_px``,
    and the working set follows the frame's *area*. Warming smaller leaves the pool to grow on
    the first punch; warming larger holds memory no punch would use. Pinning the two numbers
    together is what keeps this from drifting away from the chain.
    """
    ceiling = face_detector.frame_ceiling()
    assert ceiling == int(config.settings.face_frame_max_px)

    width, height = face_detector.warm_frame_size()
    assert (width, height) == (ceiling, ceiling * 3 // 4)


def test_the_warm_pass_runs_a_detection_at_that_size(real_detector, monkeypatch):
    """A model *load* is not enough: it is ~3 MB of the ~106 MB the first punch pays."""
    seen = []
    monkeypatch.setattr(
        face_detector, "_detect_rows", lambda frame: seen.append(frame.shape) or []
    )

    report = face_detector.warm()

    width, height = face_detector.warm_frame_size()
    assert report["warmed"] is True
    assert (report["width"], report["height"]) == (width, height)
    assert seen == [(height, width, 3)], "the warm pass did not run a frame at the ceiling"
    assert report["seconds"] >= 0.0


def test_a_warm_pass_without_the_model_reports_it_instead_of_raising(
    real_detector, monkeypatch, tmp_path
):
    """A host without the model still boots; ``warm`` is the last thing that may stop it."""
    monkeypatch.setattr(face_detector, "model_path", lambda: tmp_path / "absent.onnx")
    monkeypatch.setattr(face_detector, "_loaded", False)

    report = face_detector.warm()

    assert report["warmed"] is False and report["available"] is False
    assert "not found" in report["error"]


def test_the_warm_pass_is_refused_when_the_models_run_in_a_child(real_detector, monkeypatch):
    """``FACE_ENGINE_PROCESS`` moves the models out - working set included.

    A warm here would build exactly the detector that mode exists to move out of the API
    process, so the refusal belongs where the mistake would be made. The child, whose own flag
    is cleared, is where the real warm then happens (``face_worker``).
    """
    monkeypatch.setattr(config.settings, "face_engine_process", True, raising=False)
    monkeypatch.setattr(
        face_detector, "_ensure", lambda: pytest.fail("the API process built a detector")
    )

    report = face_detector.warm()

    assert report["warmed"] is False and report["delegated"] is True


def test_the_startup_preload_warms_the_detector_not_just_the_embedding():
    """A preload that opened only the embedding would leave the first punch the bigger spike.

    Parsed rather than grepped: the sentence that explains this in ``main`` names the function
    too, and a scan that counted a comment as a call would pass against code that has none.
    """
    tree = ast.parse((harness.BACKEND_DIR / "main.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "face_detector"
    }
    assert "warm" in called, called


# ---------------------------------------------------------------------------
# 7. the capped working size: ~62 MB of detector, bought with a different crop
# ---------------------------------------------------------------------------
# ``tools/yunet_memory.py`` measures the detector's working set at ~86 MB resident / ~104 MB peak
# at the 1280 px ceiling, and ~24 MB at 640. ``tools/detector_resolution_ab.py`` measures what
# that costs: the 640 crop moves by 5-33x the band's entire headroom (the line sits 0.0036 cosine
# above the genuine ceiling), on faces squarely inside the deployment's own band and not only at
# the far range. So the cap is a *crop* change dressed as a memory change, and the pipeline label
# is the rail that stops it being adopted quietly: a capped build names a pipeline with no band,
# ``band_for`` raises, and the startup gate refuses to open the port until one is measured.

def _capped(monkeypatch, size: int = 640, tiles: int = 2) -> None:
    monkeypatch.setattr(config.settings, "face_detector_input_size", size, raising=True)
    monkeypatch.setattr(config.settings, "face_detector_tiles", tiles, raising=True)


def test_the_pass_runs_at_the_frames_own_size_until_a_cap_is_asked_for(real_detector):
    """Off by default, and the default is the crop the shipped band was measured through."""
    assert face_detector.capped_input_size() == 0
    assert face_detector.capped_pipeline() == face_detector.PIPELINE
    assert face_detector.active_pipeline() == face_detector.PIPELINE
    assert face_detector.PIPELINE in face_detector.BANDS, (
        "the default pass has to have earned a band, or the default build would not boot"
    )


def test_a_cap_names_a_pipeline_of_its_own_and_the_band_does_not_cover_it(
    real_detector, monkeypatch
):
    """The rail: a capped crop is a *different pipeline*, and no line has been derived for it.

    This is the assertion that makes the change safe to have in the code and unsafe to switch on
    without measuring. If a capped build could answer with ``yunet-2023mar``'s lines it would
    score every punch against thresholds derived from a crop the template was not made with,
    and every stored template would have to be re-enrolled anyway - so the failure would arrive
    as silent mis-matches rather than as a build that refuses to serve.
    """
    _capped(monkeypatch)

    name = face_detector.capped_pipeline()
    assert name == f"{face_detector.PIPELINE}-640x2"
    assert name != face_detector.PIPELINE
    assert name not in face_detector.BANDS
    with pytest.raises(face_detector.UnknownPipelineError):
        face_detector.band_for(name)

    # ``readiness`` reads this, and ``face_match_band`` is FATAL: the cap cannot be served
    # until a band exists for exactly this name.
    assert face_detector.active_pipeline() == name


def test_the_tile_grid_is_part_of_the_crop_name(real_detector, monkeypatch):
    """Tiles decide which faces are found, so a band must not carry across a grid change.

    The 2x2 grid is what buys the far range back at a 640 input (17.6 px native, against the
    runtime's own ``MIN_SUBJECT_PX`` of 24), and it is the *same* working set as one tile - but
    a face is found in a different window, at a different effective scale, so the crop of a
    distant face genuinely differs between them. One band cannot cover both.
    """
    _capped(monkeypatch, tiles=1)
    one = face_detector.capped_pipeline()
    _capped(monkeypatch, tiles=2)
    two = face_detector.capped_pipeline()
    _capped(monkeypatch, size=480, tiles=2)
    smaller = face_detector.capped_pipeline()

    assert len({one, two, smaller}) == 3, (one, two, smaller)
    assert face_detector.capped_pipeline(640, 1) != face_detector.capped_pipeline(640, 2)


def test_a_capped_pass_reads_through_the_letterboxed_detector(real_detector, monkeypatch):
    """The capped route is ``detector_640``, and its answer arrives in the frame's own pixels.

    Two things at once: the native detector is never built (that is the memory change), and the
    row shape the rest of this module reads - ``[x, y, w, h, 5x(x,y), score]`` - is preserved, so
    ``align``, ``subject_detections`` and the corpus provenance all keep working untouched.
    """
    import detector_640

    seen: dict[str, object] = {}

    class _Letterboxed:
        input_size = 640

        def detect(self, frame, *, tiles=1, overlap=0.2):
            seen["shape"] = frame.shape[:2]
            seen["tiles"] = tiles
            return [
                detector_640.Detection(
                    box=(100.0, 40.0, 30.0, 30.0),
                    landmarks=np.array(
                        [[110, 50], [130, 50], [120, 60], [112, 68], [128, 68]], dtype=np.float32
                    ),
                    score=0.91,
                    origin="full",
                    scale=0.5,
                )
            ]

    _capped(monkeypatch)
    monkeypatch.setattr(face_detector, "_capped_detector", lambda size, tiles: _Letterboxed())
    monkeypatch.setattr(
        face_detector, "_ensure", lambda: pytest.fail("the capped pass built the native detector")
    )

    rows = face_detector.detect_raw(np.zeros((960, 1280, 3), dtype=np.uint8))

    assert seen == {"shape": (960, 1280), "tiles": 2}
    assert len(rows) == 1
    row = rows[0]
    assert row.shape == (15,), "box + 5 landmarks + score, the layout every caller reads"
    assert list(row[:4]) == [100.0, 40.0, 30.0, 30.0], "already native; nothing rescaled it"
    assert np.allclose(row[4:14], [110, 50, 130, 50, 120, 60, 112, 68, 128, 68])
    assert float(row[14]) == pytest.approx(0.91)
    assert face_detector.landmarks_of(row).shape == (5, 2)


def test_a_capped_pass_does_not_re_read_a_close_up(real_detector, monkeypatch):
    """The cap *is* the close-up retry, so paying for a third crop would be pure cost.

    The retry exists because a face filling a native-resolution frame has no single anchor to
    land on. A capped pass already reads every frame at a scale the anchors were built for, and
    re-reading a close-up at ``CLOSE_FRAME_RETRY_PX`` would *upscale* it into the letterbox - a
    third crop under the same pipeline name, for a face the cap had just answered about.
    """
    seen: list[int] = []
    _fake_passes(monkeypatch, native=[_row(x=0, y=0, w=900, h=800)], reduced=[], seen=seen)

    _capped(monkeypatch)
    rows = face_detector.detect_raw(np.zeros((960, 1280, 3), dtype=np.uint8))
    assert len(rows) == 1 and seen == [1280], "one read, at the frame's own size"

    # The native path still re-reads it, which is the difference being pinned.
    monkeypatch.setattr(config.settings, "face_detector_input_size", 0, raising=True)
    seen.clear()
    face_detector.detect_raw(np.zeros((960, 1280, 3), dtype=np.uint8))
    assert seen == [1280, face_detector.CLOSE_FRAME_RETRY_PX]


def test_describe_names_the_working_size_a_capped_pass_would_use(real_detector, monkeypatch):
    """Readiness has to show which crop is live, or the memory change is invisible in the report."""
    assert face_detector.describe()["working_size"] is None
    assert face_detector.describe()["tiles"] is None

    _capped(monkeypatch)
    described = face_detector.describe()
    assert described["working_size"] == 640
    assert described["tiles"] == 2
    assert described["active_pipeline"] == described["pipeline"] + "-640x2"
