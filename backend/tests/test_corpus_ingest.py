"""Ingestion: what is kept, what is flagged, what is thrown away, and what a worker id has to be.

The corpus's whole claim is that a threshold was derived from *real traffic*, and the way that claim
goes wrong is not a crash - it is a pipeline that quietly keeps only the flattering half. So the tests
here are about the two lines in the gate being two different lines, about a hard case surviving
ingestion while staying out of the measurement, and about a label having to name a worker who exists.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import corpus
import corpus_ingest
import detector_640
from face_align import template_for

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("corpus_cli", _TOOLS_DIR / "corpus_admin.py")
assert _spec and _spec.loader
corpus_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corpus_cli)


@pytest.fixture(autouse=True)
def _isolated_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "ROOT_DIR", str(tmp_path / "corpus"))
    return tmp_path


# ---------------------------------------------------------------------------
# a synthetic face, built from the alignment template
# ---------------------------------------------------------------------------
BASE = template_for(160).astype(np.float64)


def _face(
    *,
    interocular: float = 60.0,
    roll: float = 0.0,
    yaw: float = 0.0,
    pitch: float = 1.0,
    centre: tuple[float, float] = (240.0, 160.0),
) -> np.ndarray:
    """Five landmarks for a face with a chosen size, roll, yaw and pitch, in a 480x320 frame.

    Built from the model's own template, so "no flags" is the template's own pose and every deviation is
    measured against the framing the embedder expects rather than against a number invented here.
    """
    points = BASE - BASE[0:2].mean(axis=0)
    points = points * (interocular / float(np.linalg.norm(BASE[1] - BASE[0])))
    eye_middle = points[0:2].mean(axis=0)
    # Pitch: the nose's offset from the eye line changes length, which is what the ratio sees.
    points[2] = eye_middle + (points[2] - eye_middle) * np.array([1.0, pitch])
    points[2, 0] += yaw * interocular
    angle = math.radians(roll)
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    points = points @ rotation.T + np.asarray(centre)
    return points.astype(np.float32)


def _frame(size: tuple[int, int] = (480, 320), seed: int = 0) -> Image.Image:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
    return Image.fromarray(pixels, "RGB")


class _StubDetector:
    """A detector that returns whatever a test tells it to, in the frame's coordinates."""

    def __init__(self, faces: list[dict] | None = None, *, explode: bool = False) -> None:
        self.faces = faces if faces is not None else [{"points": _face(), "size": 96.0, "score": 0.92}]
        self.explode = explode
        self.calls = 0

    def __call__(self, frame):
        self.calls += 1
        if self.explode:
            raise detector_640.DetectorError("inference died on this one")
        found = []
        for face in self.faces:
            points = np.asarray(face["points"], dtype=np.float32)
            size = float(face["size"])
            centre = points.mean(axis=0)
            found.append(
                detector_640.Detection(
                    box=(float(centre[0] - size / 2), float(centre[1] - size / 2), size, size),
                    landmarks=points,
                    score=float(face["score"]),
                )
            )
        return found


def _spec(monkeypatch, detector: _StubDetector, **overrides) -> corpus_ingest.DetectorSpec:
    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: detector)
    base = dict(
        kind="yunet",
        model_path=Path("models/face_detection_yunet_2023mar.onnx"),
        model_fingerprint="b" * 16,
        input_size=640,
        tiles=2,
    )
    base.update(overrides)
    return corpus_ingest.DetectorSpec(**base)


def _frames(*images: Image.Image, camera: str | None = "gate-north") -> corpus_ingest.FrameListSource:
    return corpus_ingest.FrameListSource(
        [
            corpus_ingest.Frame(image=image, name=f"frame_{index}.jpg", camera=camera)
            for index, image in enumerate(images)
        ]
    )


def _sources(*images: Image.Image, camera: str | None = "gate-north"):
    return _frames(*images, camera=camera)


# ---------------------------------------------------------------------------
# pose: a real measurement (roll) and two stated proxies (yaw, pitch)
# ---------------------------------------------------------------------------
def test_the_frontal_reference_is_the_models_own_template():
    """The gate's zero point is the alignment template, so it must move when the template moves.

    This is the test the module's constants are pinned by: they are literals (a gate that recomputed its
    own zero point at import would re-flag a whole stored corpus when the template changed), so the
    derivation is asserted here instead.
    """
    roll, yaw, pitch = corpus_ingest.pose_of(BASE)
    assert roll == pytest.approx(corpus_ingest.REFERENCE_ROLL_DEG, abs=1e-4)
    assert yaw == pytest.approx(corpus_ingest.REFERENCE_YAW, abs=1e-4)
    assert pitch == pytest.approx(corpus_ingest.REFERENCE_PITCH, abs=1e-4)


def test_pose_is_scale_and_translation_invariant():
    """A face across the yard and a face at the gate have the same pose - only the size flag differs."""
    small = corpus_ingest.pose_of(_face(interocular=6.0, centre=(60.0, 40.0)))
    large = corpus_ingest.pose_of(_face(interocular=120.0, centre=(400.0, 280.0)))
    reference = corpus_ingest.pose_of(_face())
    for axis in (0, 1, 2):
        assert small[axis] == pytest.approx(reference[axis], abs=1e-6)
        assert large[axis] == pytest.approx(reference[axis], abs=1e-6)


def test_a_face_too_small_for_a_pose_is_unmeasurable_rather_than_silently_zero():
    degenerate = np.array(
        [[10.0, 10.0], [10.0, 10.0], [11.0, 12.0], [10.0, 20.0], [14.0, 20.0]], dtype=np.float32
    )
    with pytest.raises(ValueError):
        corpus_ingest.pose_of(degenerate)

    quality = corpus_ingest.assess(box=[8.0, 8.0, 4.0, 14.0], score=0.9, landmarks=degenerate)
    assert quality.discard is True and quality.reason == "unmeasurable"
    assert quality.flags == ["unmeasurable"] and quality.pose_error


# ---------------------------------------------------------------------------
# the gate: two lines, not one
# ---------------------------------------------------------------------------
def test_a_typical_capture_is_neither_flagged_nor_discarded():
    quality = corpus_ingest.assess(box=[0.0, 0.0, 96.0, 96.0], score=0.92, landmarks=_face())
    assert quality.flags == [] and quality.hard_case is False and quality.discard is False
    assert quality.face_px == 96.0
    assert quality.gate == corpus_ingest.GateConfig().as_dict()


def test_a_small_capture_is_kept_and_flagged_and_a_tiny_one_is_discarded():
    """The distinction the whole corpus depends on: 20 px is evidence, 10 px is not."""
    flagged = corpus_ingest.assess(box=[0.0, 0.0, 20.0, 20.0], score=0.9, landmarks=_face(interocular=8.0))
    assert flagged.flags == ["small_face"] and flagged.hard_case is True and flagged.discard is False

    tiny = corpus_ingest.assess(box=[0.0, 0.0, 10.0, 10.0], score=0.9, landmarks=_face(interocular=4.0))
    assert tiny.discard is True and tiny.reason == "too_small"


def test_confidence_has_the_same_two_lines():
    weak = corpus_ingest.assess(box=[0.0, 0.0, 96.0, 96.0], score=0.4, landmarks=_face())
    assert weak.flags == ["low_confidence"] and weak.discard is False
    unusable = corpus_ingest.assess(box=[0.0, 0.0, 96.0, 96.0], score=0.1, landmarks=_face())
    assert unusable.discard is True and unusable.reason == "low_score"


@pytest.mark.parametrize(
    "deviation,flag,reason,discard",
    [
        ({"roll": 25.0}, "rolled", None, False),
        ({"roll": 70.0}, "rolled", "extreme_roll", True),
        ({"yaw": 0.25}, "yawed", None, False),
        ({"yaw": 0.70}, "yawed", "extreme_yaw", True),
        ({"pitch": 0.70}, "pitched", None, False),
        ({"pitch": 1.80}, "pitched", "extreme_pitch", True),
    ],
)
def test_pose_flags_and_discards(deviation, flag, reason, discard):
    quality = corpus_ingest.assess(
        box=[0.0, 0.0, 96.0, 96.0], score=0.9, landmarks=_face(**deviation)
    )
    assert flag in quality.flags
    assert quality.discard is discard
    if reason:
        assert quality.reason == reason


def test_the_permissive_gate_is_for_a_controlled_sitting_and_still_keeps_the_frame():
    """``add`` imports frames the operator chose to take; dropping the dim ones would choose for them."""
    hard = dict(box=[0.0, 0.0, 18.0, 18.0], score=0.12, landmarks=_face(interocular=6.0, roll=50.0))
    assert corpus_ingest.assess(**hard).discard is True
    permissive = corpus_ingest.assess(gate=corpus_ingest.GateConfig.permissive(), **hard)
    assert permissive.discard is False and permissive.hard_case is False


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def test_a_folder_source_is_sorted_and_reports_what_it_could_not_read(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    for name in ("b.jpg", "a.jpg", "c.jpg"):
        _frame(seed=hash(name) % 1000).save(folder / name)
    (folder / "broken.jpg").write_bytes(b"not a jpeg at all")
    (folder / "notes.txt").write_text("ignore me")

    source = corpus_ingest.DirectorySource(folder)
    names = [frame.name for frame in source.frames()]

    assert names == ["a.jpg", "b.jpg", "c.jpg"], "deterministic order: a run has to be repeatable"
    assert len(source.skipped) == 1 and "broken.jpg" in source.skipped[0][0]
    assert "unreadable" in source.skipped[0][1]


def test_a_source_over_an_explicit_list_does_not_re_derive_anything(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    for name in ("a.jpg", "b.jpg"):
        _frame().save(folder / name)
    source = corpus_ingest.DirectorySource(folder, paths=[folder / "b.jpg"])
    assert [frame.name for frame in source.frames()] == ["b.jpg"]


def test_a_folder_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(corpus_ingest.IngestError):
        list(corpus_ingest.DirectorySource(tmp_path / "nope").frames())


def test_a_camera_thats_not_there_is_refused_with_the_thing_to_check():
    with pytest.raises(corpus_ingest.IngestError) as caught:
        list(corpus_ingest.CameraSource("rtsp://10.255.255.1/gate", open_timeout_ms=50).frames())
    assert "could not open" in str(caught.value) and "RTSP" in str(caught.value)


class _FakeCapture:
    """A stand-in for ``cv2.VideoCapture``: N good frames, then end-of-stream or failed reads."""

    def __init__(self, frames: int = 3, *, fail_after: int | None = None, opens: bool = True) -> None:
        self.frames = frames
        self.fail_after = fail_after
        self.opens = opens
        self.reads = 0
        self.released = False

    def isOpened(self) -> bool:
        return self.opens

    def read(self):
        self.reads += 1
        if not self.opens:
            return False, None
        if self.fail_after is not None and self.reads > self.fail_after:
            return False, None
        if self.reads > self.frames:
            return False, None
        pixels = np.zeros((32, 48, 3), dtype=np.uint8)
        pixels[:, :] = self.reads
        return True, pixels

    def release(self) -> None:
        self.released = True


def _install_capture(monkeypatch, fake: _FakeCapture) -> None:
    import cv2

    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **k: fake)


def test_a_camera_source_honours_the_limit_the_sampling_and_releases_the_handle(monkeypatch):
    import cv2

    fake = _FakeCapture(frames=10)
    _install_capture(monkeypatch, fake)
    source = corpus_ingest.CameraSource(0, limit=2, every=3)
    frames = list(source.frames())
    assert len(frames) == 2
    assert [frame.name for frame in frames] == ["webcam:0#1", "webcam:0#2"]
    assert fake.released is True, "the handle is released even when the pull ends early"
    assert cv2 is not None


def test_a_camera_that_goes_silent_ends_the_pull_and_says_so(monkeypatch):
    fake = _FakeCapture(frames=1, fail_after=1)
    _install_capture(monkeypatch, fake)
    source = corpus_ingest.CameraSource("rtsp://10.0.0.9/gate", max_consecutive_failures=3)
    frames = list(source.frames())
    assert len(frames) == 1
    assert source.skipped and "failed reads in a row" in source.skipped[0][1]


def test_a_camera_urls_credentials_never_reach_the_record():
    assert corpus_ingest.redact("rtsp://admin:hunter2@10.0.0.9/gate") == "rtsp://10.0.0.9/gate"
    assert corpus_ingest.redact("http://user:pa%40ss@host/path") == "http://host/path"
    assert corpus_ingest.redact("rtsp://10.0.0.9/gate") == "rtsp://10.0.0.9/gate"
    source = corpus_ingest.CameraSource("rtsp://admin:hunter2@10.0.0.9/gate")
    assert source.camera == "rtsp://10.0.0.9/gate"
    assert "hunter2" not in source.camera


def test_the_detector_receives_the_contract_opencv_documents():
    bgr = corpus_ingest.as_bgr(Image.new("RGB", (4, 2), (1, 2, 3)))
    assert bgr.shape == (2, 4, 3) and bgr.dtype == np.uint8
    assert bgr[0, 0].tolist() == [3, 2, 1], "RGB in, BGR out - the one place this is converted"
    with pytest.raises(corpus_ingest.IngestError):
        corpus_ingest.as_bgr(np.zeros((4, 4), dtype=np.uint8))


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------
def test_ingest_files_a_capture_with_its_camera_and_its_quality(monkeypatch):
    detector = _StubDetector([{"points": _face(), "size": 96.0, "score": 0.92}])
    report = corpus_ingest.ingest(
        _sources(_frame(seed=1), _frame(seed=2)),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="deployment notice 2026-08",
        actor="R. Ops",
        camera="gate-north",
    )

    assert report.frames == 2 and len(report.stored) == 2
    assert detector.calls == 2, "one detection pass per frame, on the stored copy"
    record = report.stored[0]
    assert record.identity == "W-1042"
    assert record.camera == "gate-north"
    assert record.source == corpus.SOURCE_DIRECTORY
    assert record.landmark_source == "yunet"
    assert record.quality["flags"] == [] and record.quality["gate"]["min_face_px"] == 30.0
    assert record.detector["tiles"] == 2 and record.detector["model_fingerprint"] == "b" * 16
    assert corpus.find_capture(record.capture_id)[0] == "W-1042"
    assert report.summary().startswith("stored 2 capture(s)")
    assert corpus_ingest.coverage()["captures"] == 2


def test_a_hard_case_is_stored_flagged_and_counted(monkeypatch):
    detector = _StubDetector([{"points": _face(interocular=8.0), "size": 18.0, "score": 0.9}])
    report = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="test",
        camera="gate-far",
    )
    assert report.hard_cases == [report.stored[0].capture_id]
    assert report.counts()["small_face"] == 1
    assert report.stored[0].hard_case is True
    assert corpus.stats()["measured"] == 0, "kept, and not part of the measurement"


def test_a_hard_case_can_be_dropped_instead_of_kept(monkeypatch):
    detector = _StubDetector([{"points": _face(interocular=8.0), "size": 18.0, "score": 0.9}])
    report = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="test",
        keep_hard_cases=False,
    )
    assert report.stored == []
    assert report.skipped[0]["reason"] == "quality:hard_case"


def test_frames_that_are_not_evidence_are_reported_rather_than_stored(monkeypatch):
    no_face = _StubDetector([])
    report = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, no_face),
        identity="W-1042",
        consent="test",
    )
    assert report.stored == [] and report.skipped[0]["reason"] == "no_face"

    unusable = _StubDetector([{"points": _face(interocular=4.0), "size": 10.0, "score": 0.9}])
    report = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, unusable),
        identity="W-1042",
        consent="test",
    )
    assert report.skipped[0]["reason"] == "quality:too_small"

    angry = _StubDetector(explode=True)
    report = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, angry),
        identity="W-1042",
        consent="test",
    )
    assert report.skipped[0]["reason"] == "detector_failed", "one bad frame does not end a session"


def test_a_frame_with_several_faces_obeys_the_stated_policy(monkeypatch):
    faces = [
        {"points": _face(interocular=20.0, centre=(120.0, 100.0)), "size": 30.0, "score": 0.9},
        {"points": _face(interocular=70.0, centre=(320.0, 220.0)), "size": 110.0, "score": 0.9},
    ]
    detector = _StubDetector(faces)
    largest = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="test",
        choose="largest",
    )
    assert len(largest.stored) == 1
    assert largest.stored[0].face_px == 110.0, "the subject at the gate is the larger face"
    assert "faces=2" in (largest.stored[0].note or "")

    skipped = corpus_ingest.ingest(
        _sources(_frame()),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="test",
        choose="skip",
    )
    assert skipped.stored == [] and skipped.skipped[0]["reason"] == "many_faces"


def test_ingest_stops_at_the_limit(monkeypatch):
    detector = _StubDetector()
    report = corpus_ingest.ingest(
        _sources(*[_frame(seed=index) for index in range(5)]),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="test",
        limit=2,
    )
    assert len(report.stored) == 2 and report.frames == 2


def test_a_source_that_could_not_read_a_file_says_so_in_the_report(monkeypatch, tmp_path):
    folder = tmp_path / "camera_dump"
    folder.mkdir()
    _frame(seed=3).save(folder / "ok.jpg")
    (folder / "half.jpg").write_bytes(b"truncated")

    report = corpus_ingest.ingest(
        corpus_ingest.DirectorySource(folder, camera="gate-north"),
        detector=_spec(monkeypatch, _StubDetector()),
        identity="W-1042",
        consent="test",
    )
    assert len(report.stored) == 1
    assert [item["reason"] for item in report.skipped] == ["source"]
    assert "unreadable" in report.skipped[0]["detail"]
    assert report.counts()["source"] == 1


def test_ingestion_without_a_stated_basis_is_refused(monkeypatch):
    with pytest.raises(corpus_ingest.IngestError) as caught:
        corpus_ingest.ingest(_sources(_frame()), detector=_spec(monkeypatch, _StubDetector()), consent="")
    assert "consent" in str(caught.value)
    with pytest.raises(corpus_ingest.IngestError):
        corpus_ingest.ingest(
            _sources(_frame()),
            detector=_spec(monkeypatch, _StubDetector()),
            consent="test",
            choose="whichever",
        )


def test_a_detector_spec_without_a_model_is_refused_rather_than_silently_skipped():
    with pytest.raises(corpus_ingest.IngestError):
        corpus_ingest.DetectorSpec(kind="yunet").build()


# ---------------------------------------------------------------------------
# enrolment
# ---------------------------------------------------------------------------
def test_enrolment_refuses_an_id_that_is_not_in_the_roster(monkeypatch):
    detector = _StubDetector()
    roster = lambda worker_id: {"id": worker_id, "name": "Bilal Khan", "role": "worker"} if worker_id == "W-1042" else None

    with pytest.raises(corpus_ingest.IngestError) as caught:
        corpus_ingest.enroll(
            _sources(_frame()),
            identity="W-9999",
            detector=_spec(monkeypatch, detector),
            consent="signed form 2026-09-22",
            roster=roster,
            check_roster=True,
        )
    assert "roster" in str(caught.value)
    assert corpus.sidecars() == [], "nothing was written before the refusal"

    report = corpus_ingest.enroll(
        _sources(_frame()),
        identity="W-1042",
        detector=_spec(monkeypatch, detector),
        consent="signed form 2026-09-22",
        roster=roster,
        check_roster=True,
    )
    assert [record.identity for record in report.stored] == ["W-1042"]
    assert corpus.stats()["identity_counts"] == {"W-1042": 1}


def test_enrolment_without_a_lookup_cannot_claim_to_have_checked(monkeypatch):
    with pytest.raises(corpus_ingest.IngestError):
        corpus_ingest.enroll(
            _sources(_frame()),
            identity="W-1042",
            detector=_spec(monkeypatch, _StubDetector()),
            consent="test",
            check_roster=True,
        )


def test_the_operator_commands_route_through_this_one_pipeline(monkeypatch, tmp_path, capsys):
    """``ingest`` and ``enroll`` in the CLI are the pipeline, not a second implementation of it."""
    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: _StubDetector())
    monkeypatch.setattr(
        corpus_cli, "_roster_lookup",
        lambda: (lambda worker_id: {"id": worker_id, "name": "Bilal", "role": "worker"}
                 if worker_id == "W-1042" else None),
    )
    session = tmp_path / "session"
    session.mkdir()
    for index in range(3):
        _frame(seed=index).save(session / f"{index}.jpg")

    assert corpus_cli.main(
        ["ingest", "--source", str(session), "--camera-name", "gate-north", "--identity", "W-1042",
         "--consent", "deployment notice", "--actor", "R. Ops", "--coverage",
         "--expected-face-px", "40", "--quiet"]
    ) == 0
    out = capsys.readouterr().out
    assert "stored 3 capture(s)" in out and "coverage verdict" in out
    assert corpus.stats()["identity_counts"] == {"W-1042": 3}

    # and the enrolment refusal, through the command line
    assert corpus_cli.main(
        ["enroll", "--source", str(session), "--identity", "W-9999", "--check-roster",
         "--consent", "test"]
    ) == 2
    assert "no worker" in capsys.readouterr().err


def test_the_coverage_report_tells_an_easy_corpus_from_a_usable_one(monkeypatch):
    detector = _StubDetector([{"points": _face(interocular=70.0), "size": 130.0, "score": 0.9}])
    corpus_ingest.ingest(
        _frames(*[_frame(seed=index) for index in range(3)]),
        detector=_spec(monkeypatch, detector),
        identity="W-1042",
        consent="test",
    )
    easy = corpus_ingest.coverage(expected_face_px=40.0)
    assert easy["captures"] == 3 and easy["below_gate_px"] == 0
    assert easy["verdict"].startswith("thin"), "a corpus of close-up faces cannot model a gate"

    assert corpus_ingest.coverage()["captures"] == 3
    assert corpus_ingest.coverage(expected_face_px=40.0)["at_or_below_expected"] == 0
