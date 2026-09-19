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

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

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
def test_detect_and_align_answers_the_shape_deepface_answers(real_detector, monkeypatch):
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

    DeepFace hands round floats around, so this is not hypothetical: it is how a detector
    ends up finding nothing in a perfectly good photo.
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
