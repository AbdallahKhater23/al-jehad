"""The labelled corpus: what a capture keeps, what a reader may assume, and what erasure removes.

WHAT THESE TESTS ARE ACTUALLY ABOUT
-----------------------------------
A corpus is the evidence behind a threshold, so the failure mode here is not a crash - it is a corpus
that *looks* fine and supports a number that is not true. Three properties carry that:

* **the crop is in the stored image's coordinates**, so whoever opens the file months later crops the
  same pixels the detector measured. If the points were native-frame coordinates, the export would
  silently crop the wrong region of a downscaled frame, and the resulting band would describe a crop
  nobody took a picture of;
* **the detector that made the crop travels with it**, and a mismatch is a refusal. This is the same
  rule ``derive_facenet_band`` applies to pipelines, applied to the thing that actually decides which
  pixels a face is;
* **"unlabelled" is a state the store can hold**, because a corpus of real traffic contains captures
  nobody has decided about yet, and a store that cannot represent that forces a guess at import time -
  which is how a wrong label becomes indistinguishable from a right one.

The store is exercised directly, through ``store()`` and a synthetic detector, because the questions
above are about the record rather than about YuNet. The one place a real detector configuration is
used is the refusal, where the point is that two *configurations* cannot be confused.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import corpus


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_corpus(tmp_path, monkeypatch):
    """Every test gets its own corpus root, in the temp directory.

    Repointed here rather than relying on the harness: these tests never import the application, and a
    store that wrote into the checkout - even an empty one - is the leak ``harness.FILE_TREES`` exists
    to prevent.
    """
    monkeypatch.setattr(corpus, "ROOT_DIR", str(tmp_path / "corpus"))
    return tmp_path


#: Five points in a 480x320 frame, deliberately not the alignment template: a test that used the
#: template would pass whichever coordinate space it was written in.
LANDMARKS = np.array(
    [[150.0, 120.0], [230.0, 120.0], [190.0, 160.0], [165.0, 200.0], [215.0, 200.0]], dtype=np.float32
)


def _image(size: tuple[int, int] = (480, 320), colour: tuple[int, int, int] = (120, 130, 140)) -> Image.Image:
    return Image.new("RGB", size, colour)


def _fingerprint(**overrides) -> dict:
    base = dict(
        kind="yunet",
        model_fingerprint="a" * 16,
        pipeline="yunet-2023mar",
        input_size=640,
        square=False,
        tiles=1,
        overlap=0.0,
    )
    base.update(overrides)
    return corpus.detector_fingerprint(**base)


def _capture(image: Image.Image | None = None, *, identity: str | None = "person_a", **overrides) -> corpus.Capture:
    """One capture whose landmarks and box are already in the *stored* frame's coordinates.

    Which is the store's contract, and deliberately not a convenience it provides: the caller is the
    thing that ran the detector, and it ran it on ``prepare(...).frame``.
    """
    return _store(corpus.prepare(image or _image()), identity=identity, **overrides)


def _capture_from_native(
    image: Image.Image, *, native_landmarks: np.ndarray, native_box: list[float], **overrides
) -> corpus.Capture:
    """The larger-frame path, the way a real capture does it: cap, then scale what was measured."""
    prepared = corpus.prepare(image)
    return _store(
        prepared,
        identity=overrides.pop("identity", "person_a"),
        landmarks=corpus.scale_landmarks(native_landmarks, prepared.scale),
        box=[value * prepared.scale[index % 2] for index, value in enumerate(native_box)],
        **overrides,
    )


def _store(prepared: corpus.Prepared, *, identity: str | None, **overrides) -> corpus.Capture:
    return corpus.store(
        prepared,
        detector=overrides.pop("detector", _fingerprint()),
        identity=identity,
        landmarks=overrides.pop("landmarks", LANDMARKS),
        box=overrides.pop("box", [140.0, 110.0, 100.0, 110.0]),
        score=overrides.pop("score", 0.94),
        source=overrides.pop("source", corpus.SOURCE_IMPORT),
        consent=overrides.pop("consent", "test: synthetic frame"),
        actor=overrides.pop("actor", "tester"),
        **overrides,
    )


# ---------------------------------------------------------------------------
# what a capture keeps
# ---------------------------------------------------------------------------
def test_a_capture_keeps_the_frame_the_landmarks_and_the_detector_that_made_them():
    record = _capture()
    frame = Path(corpus.image_path(record.capture_id))
    sidecar = Path(corpus.sidecar_path(record.capture_id))

    assert frame.exists() and sidecar.exists()
    assert record.image == frame.name
    assert record.image_sha256 == __import__("hashlib").sha256(frame.read_bytes()).hexdigest()
    assert record.version == corpus.SIDECAR_VERSION
    assert record.consent and record.actor == "tester"
    assert record.label_history and record.label_history[0]["to"] == "person_a"
    assert record.detector["input_size"] == 640

    stored = json.loads(sidecar.read_text(encoding="utf-8"))
    assert stored["landmarks"] == record.landmarks, "the sidecar is the record, not a summary of it"
    assert stored["version"] == corpus.SIDECAR_VERSION

    # And the reload path sees the same thing.
    assert corpus.load(record.capture_id).landmarks == record.landmarks


def test_the_stored_points_are_in_the_stored_images_coordinates():
    """The property the whole format exists for: a reader crops the same pixels, months later.

    Asserted by re-running the *same* geometry against the file that was kept. Landmarks stored in the
    original frame's coordinates would pass a naive "we stored something" test and fail this one - and
    the export would crop a region the detector never looked at.
    """
    source = _image((1920, 1080))
    native = LANDMARKS * 4.0  # a detection in the 1920-wide frame
    record = _capture_from_native(
        source, native_landmarks=native, native_box=[560.0, 440.0, 400.0, 440.0]
    )

    assert record.source_size == (1920, 1080)
    assert record.stored_size == (corpus.MAX_PX, 720), "the long edge is capped at MAX_PX"
    assert record.points.max() < record.stored_size[0], "points must fit inside the stored image"
    assert np.allclose(record.points, corpus.scale_landmarks(native, record.scale), atol=1e-3)

    stored_frame = Image.open(corpus.image_path(record.capture_id))
    assert stored_frame.size == record.stored_size
    assert record.points.max() <= stored_frame.size[0]

    # And the guard that catches the opposite mistake: native coordinates handed to a capped frame.
    with pytest.raises(corpus.CorpusError) as caught:
        _store(corpus.prepare(source), identity="person_a", landmarks=native,
               box=[560.0, 440.0, 400.0, 440.0])
    assert "outside the stored" in str(caught.value)


def test_both_regimes_are_recorded_so_a_downscaled_corpus_cannot_hide_it():
    """``face_px`` is what a tool will see; ``native_face_px`` is what the camera saw.

    Without the second number a corpus captured at a 1280 cap would look like a corpus of small faces
    and nobody could tell whether that was the gate or the cap - which is precisely the question the
    corpus is being built to answer.
    """
    record = _capture_from_native(
        _image((1920, 1080)), native_landmarks=LANDMARKS * 4.0, native_box=[560.0, 440.0, 400.0, 440.0]
    )
    assert record.face_px == pytest.approx(400.0 * record.scale[0], rel=1e-3)
    assert record.native_face_px == pytest.approx(400.0, rel=1e-2)
    assert record.native_face_px > record.face_px


def test_a_capture_without_a_consent_basis_or_without_landmarks_is_refused():
    """Both refusals are about the same thing: a capture that cannot answer for itself."""
    prepared = corpus.prepare(_image())
    with pytest.raises(corpus.CorpusError) as caught:
        corpus.store(prepared, detector=_fingerprint(), landmarks=LANDMARKS, consent="")
    assert "consent" in str(caught.value)

    with pytest.raises(corpus.CorpusError) as caught:
        corpus.store(prepared, detector=_fingerprint(), consent="test")
    assert "no landmarks" in str(caught.value)

    with pytest.raises(corpus.CorpusError) as caught:
        corpus.store(prepared, detector=_fingerprint(), consent="test",
                     landmarks=[[1.0, 2.0], [3.0, 4.0]])
    assert "five (x, y) points" in str(caught.value)
    with pytest.raises(corpus.CorpusError):
        corpus.store(prepared, detector=_fingerprint(), consent="test", landmarks=LANDMARKS,
                     box=[140.0, 110.0, -3.0, 40.0])
    assert corpus.captures_dir()  # created, and still empty of sidecars
    assert not corpus.sidecars()


def test_a_failed_sidecar_write_leaves_no_orphan_frame():
    """An image with no provenance is an unexplained face - worse than a missing capture."""
    prepared = corpus.prepare(_image())

    def explode(_record):
        raise OSError("the disk filled at the last moment")

    original = corpus._write_sidecar
    corpus._write_sidecar = explode
    try:
        with pytest.raises(OSError):
            corpus.store(prepared, detector=_fingerprint(), landmarks=LANDMARKS, consent="test")
    finally:
        corpus._write_sidecar = original
    # *Anywhere* under the root, partitions included: a store whose layout gained a level would pass a
    # check that only listed the top one while leaving exactly the orphan this test exists for.
    frames = sorted(str(path.relative_to(corpus.root_dir())) for path in Path(corpus.root_dir()).rglob("*.jpg"))
    assert frames == [], f"a frame survived a failed sidecar write: {frames}"


# ---------------------------------------------------------------------------
# the refusal that makes a corpus reusable
# ---------------------------------------------------------------------------
def test_a_crop_from_another_detector_configuration_is_refused_not_reinterpreted():
    record = _capture()
    frame = corpus.image_path(record.capture_id)

    # Same graph, same everything: reusable.
    same = corpus.landmarks_for(frame, expect=_fingerprint())
    assert same is not None and same["capture_id"] == record.capture_id

    for changed in (
        _fingerprint(input_size=320),
        _fingerprint(tiles=2),
        _fingerprint(model_fingerprint="b" * 16),
        _fingerprint(square=True),
    ):
        with pytest.raises(corpus.CorpusError) as caught:
            corpus.landmarks_for(frame, expect=changed)
        message = str(caught.value)
        assert "different detector configuration" in message
        assert "stored:" in message and "requested:" in message

    # A different *name* for the same graph is not a different crop: this is what makes a corpus
    # captured by the live pipeline (pipeline="yunet-2023mar", kind="yunet") readable by a tool that
    # only knows the digest and the four settings.
    renamed = corpus.detector_fingerprint(
        kind="yunet", pipeline="something-else", model_fingerprint="a" * 16, input_size=640, overlap=0.0
    )
    assert corpus.same_crop(record.detector, renamed)


def test_an_unknown_digest_is_a_mismatch_rather_than_a_match():
    """``None == None`` would make two corpora with no provenance look like one crop.

    The second assertion is the one a comparison written field-by-field gets wrong: two records that
    both lack a digest match each other on every *other* field, and they are exactly the pair somebody
    merges without checking - the answer has to be "we cannot tell", not "the same".
    """
    record = _capture()
    nameless = dict(record.detector)
    nameless["model_fingerprint"] = None
    assert not corpus.same_crop(record.detector, nameless)
    assert not corpus.same_crop(record.detector, None)
    assert not corpus.same_crop(None, None)

    blank = {"kind": "yunet", "input_size": 640, "square": False, "tiles": 1, "overlap": 0.0}
    assert not corpus.same_crop(dict(blank), dict(blank))


def test_a_sidecar_this_build_does_not_understand_is_refused():
    record = _capture()
    path = Path(corpus.sidecar_path(record.capture_id))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = corpus.SIDECAR_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(corpus.CorpusError) as caught:
        corpus.load(record.capture_id)
    assert "refusing" in str(caught.value)


# ---------------------------------------------------------------------------
# labelling
# ---------------------------------------------------------------------------
def test_labelling_is_recorded_and_a_wrong_label_can_be_corrected():
    record = _capture(identity=None)
    assert record.identity is None
    assert [item for item in corpus.sidecars(unlabelled_only=True)] == [corpus.load(record.capture_id)]

    first = corpus.label(record.capture_id, "person_b", actor="ops")
    second = corpus.label(record.capture_id, "person_c", actor="ops")
    cleared = corpus.label(record.capture_id, None, actor="ops")

    assert first.identity == "person_b" and second.identity == "person_c"
    assert cleared.identity is None
    history = corpus.load(record.capture_id).label_history
    assert [item["to"] for item in history] == ["person_b", "person_c", None]
    assert [item["from"] for item in history] == [None, "person_b", "person_c"], (
        "the first entry's 'from' is None, which is how 'this was unlabelled' is recorded"
    )
    assert all(item["by"] == "ops" for item in history), "who changed it is part of the record"
    assert [item.identity for item in corpus.sidecars(identity="person_b")] == []


# ---------------------------------------------------------------------------
# statistics: can this corpus carry a measurement at all
# ---------------------------------------------------------------------------
def test_stats_says_whether_the_corpus_can_carry_a_floor():
    for identity in ("a", "b"):
        for _ in range(3):
            _capture(identity=identity)
    payload = corpus.stats()
    assert payload["captures"] == 6 and payload["identities"] == 2
    assert payload["identity_counts"] == {"a": 3, "b": 3}
    assert payload["genuine_pairs"] == 6  # 3 per identity
    assert payload["impostor_pairs"] == 9  # C(6,2) - 6
    assert payload["can_support_floor"] is False, "9 impostor pairs is not a floor"
    assert payload["detectors"] == {corpus.crop_description(_fingerprint()): 6}

    for index in range(12):
        _capture(identity=f"extra_{index}")
    payload = corpus.stats()
    assert payload["can_support_floor"] is True
    assert payload["single_capture_identities"] == sorted(f"extra_{index}" for index in range(12))


def test_stats_reports_the_small_face_regime_and_the_unlabelled_backlog():
    _capture(box=[10.0, 10.0, 40.0, 40.0])          # 40px: below the 64px small-face line
    _capture(box=[10.0, 10.0, 200.0, 200.0])         # large
    _capture(identity=None)
    payload = corpus.stats()
    assert payload["labelled"] == 2 and payload["unlabelled"] == 1
    assert payload["small_face_share"] == pytest.approx(0.5)
    assert payload["face_px"]["32-64"] == 1 and payload["face_px"]["128+"] == 1


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def test_export_writes_the_folder_contract_the_two_tools_read():
    for identity in ("Seed Head Admin", "worker_two"):
        for _ in range(2):
            _capture(identity=identity)
    _capture(identity=None)
    destination = Path(corpus.root_dir()).parent / "export"

    report = corpus.export(destination)
    assert report["images"] == 4 and report["identities"] == 2
    assert report["unlabelled_skipped"] == 1

    folder = destination / "Seed_Head_Admin"
    images = sorted(folder.glob("*.jpg"))
    assert len(images) == 2
    for image in images:
        sidecar = Path(corpus.sidecar_for(image))
        assert sidecar.exists(), "an exported crop without its provenance is a crop nobody can check"
        stored = json.loads(sidecar.read_text(encoding="utf-8"))
        assert stored["identity"] == "Seed Head Admin"
        assert stored["landmarks"]

    # The band tool's own collector reads the export as a corpus - which is the whole point of the
    # folder contract, and the reason the store is not a second format.
    import sys

    tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from derive_facenet_band import collect

    grouped = collect(destination)
    assert set(grouped) == {"Seed_Head_Admin", "worker_two"}
    assert all(len(paths) == 2 for paths in grouped.values())

    with_unlabelled = corpus.export(destination, include_unlabelled=True)
    assert with_unlabelled["images"] == 5
    assert (destination / "unlabelled").is_dir()


def test_export_refuses_to_merge_two_corpora_by_capture_id():
    record = _capture()
    destination = Path(corpus.root_dir()).parent / "merge"
    folder = destination / "person_a"
    folder.mkdir(parents=True)
    (folder / f"{record.capture_id}.jpg").write_bytes(b"a different person entirely")
    with pytest.raises(corpus.CorpusError) as caught:
        corpus.export(destination)
    assert "fresh" in str(caught.value)


# ---------------------------------------------------------------------------
# erasure
# ---------------------------------------------------------------------------
def test_purge_is_a_dry_run_until_it_is_told_otherwise():
    record = _capture()
    frame, sidecar = Path(corpus.image_path(record.capture_id)), Path(corpus.sidecar_path(record.capture_id))

    reported = corpus.purge(capture_ids=[record.capture_id])
    assert reported["dry_run"] is True and reported["captures"] == 1 and reported["bytes"] > 0
    assert frame.exists() and sidecar.exists(), "a dry run must not delete a face"

    applied = corpus.purge(capture_ids=[record.capture_id], dry_run=False)
    assert applied["captures"] == 1
    assert not frame.exists() and not sidecar.exists()
    assert corpus.sidecars() == []
    assert corpus.purge(capture_ids=["nothing"])["captures"] == 0


def test_purge_by_identity_leaves_everybody_else_alone():
    keep = _capture(identity="keeper")
    drop = _capture(identity="leaver")
    # Paths first, because after an erasure there is no longer a *lookup* to ask for one: the capture is
    # gone, and that is the point.
    dropped_frame = Path(corpus.image_path(drop.capture_id))
    kept_frame = Path(corpus.image_path(keep.capture_id))
    applied = corpus.purge(identity="leaver", dry_run=False)
    assert applied["captures"] == 1 and applied["identities"] == ["leaver"]
    assert not dropped_frame.exists()
    assert not corpus.exists(drop.capture_id), "the record went with the frame"
    assert kept_frame.exists()
    assert [record.identity for record in corpus.sidecars()] == ["keeper"]


def test_purge_by_age_uses_the_capture_time_and_needs_a_selector():
    old = _capture(captured_at="2026-01-01 09:00:00")
    fresh = _capture(captured_at="2026-09-01 09:00:00")
    from datetime import datetime

    old_frame = Path(corpus.image_path(old.capture_id))
    report = corpus.purge(older_than_days=180, now=datetime(2026, 9, 22, 12, 0, 0), dry_run=False)
    assert [record.capture_id for record in corpus.sidecars()] == [fresh.capture_id]
    assert report["captures"] == 1
    assert not old_frame.exists() and not corpus.exists(old.capture_id)

    with pytest.raises(corpus.CorpusError):
        corpus.purge()


# ---------------------------------------------------------------------------
# the live capture path
# ---------------------------------------------------------------------------
class _StubDetector:
    """One face, whose geometry the test controls, and a record of what it was shown."""

    def __init__(self, faces: int = 1) -> None:
        self.faces = faces
        self.sizes: list[tuple[int, int]] = []

    def __call__(self, image):
        self.sizes.append((image.width, image.height))
        return [
            {
                "landmarks": LANDMARKS.copy(),
                "facial_area": {"x": 140, "y": 110, "w": 100, "h": 110},
                "confidence": 0.97,
            }
            for _ in range(self.faces)
        ]


def _live(monkeypatch, *, enabled: bool = True, faces: int = 1) -> _StubDetector:
    import face_detector

    from config import settings

    stub = _StubDetector(faces)
    monkeypatch.setattr(settings, "calibration_capture_enabled", enabled)
    monkeypatch.setattr(face_detector, "detect_landmarks", stub)
    monkeypatch.setattr(face_detector, "fingerprint", lambda: "c" * 16)
    monkeypatch.setattr(face_detector, "active_pipeline", lambda: "yunet-2023mar")
    return stub


def test_the_live_hook_labels_a_verified_verdict_and_only_a_verified_verdict(monkeypatch):
    """A label is a claim about who a person is, so only the band's own approval may carry one."""
    _live(monkeypatch)
    approved = corpus.maybe_capture_punch(_image(), worker_id="W-1", verdict="approved")
    flagged = corpus.maybe_capture_punch(_image(), worker_id="W-1", verdict="pending_review")
    refused = corpus.maybe_capture_punch(_image(), worker_id="W-1", verdict="refused")

    assert approved and flagged and refused
    records = {record.capture_id: record for record in corpus.sidecars()}
    assert records[approved].identity == "W-1"
    assert records[flagged].identity is None and records[refused].identity is None
    assert records[approved].source == corpus.SOURCE_PUNCH
    assert "CALIBRATION_CAPTURE_ENABLED" in records[approved].consent
    assert records[approved].note and "verdict=approved" in records[approved].note
    assert len(corpus.sidecars(unlabelled_only=True)) == 2


def test_the_live_hook_is_off_by_default_and_never_fails_a_punch(monkeypatch):
    _live(monkeypatch, enabled=False)
    assert corpus.maybe_capture_punch(_image(), worker_id="W-1", verdict="approved") is None
    assert corpus.sidecars() == []

    import face_detector

    _live(monkeypatch)
    monkeypatch.setattr(
        face_detector, "detect_landmarks", lambda image: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert corpus.maybe_capture_punch(_image(), worker_id="W-1", verdict="approved") is None
    assert corpus.sidecars() == [], "a measurement aid must not be able to leave half a capture"


def test_the_live_hook_detects_on_the_pixels_it_stores(monkeypatch):
    """The crop's coordinates are the stored file's, so the detector must have seen that file."""
    stub = _live(monkeypatch)
    corpus.maybe_capture_punch(_image((1920, 1080)), worker_id="W-1", verdict="approved")
    assert stub.sizes == [(corpus.MAX_PX, 720)], (
        f"the detector was shown {stub.sizes}: landmarks in native coordinates would be meaningless "
        "to a reader of the stored frame"
    )
    record = corpus.sidecars()[0]
    assert record.stored_size == (corpus.MAX_PX, 720) and record.source_size == (1920, 1080)


def test_the_live_hook_skips_a_frame_it_cannot_crop(monkeypatch):
    """Two faces is a choice, and a capture nobody can label is not worth storing."""
    _live(monkeypatch, faces=2)
    assert corpus.maybe_capture_punch(_image(), worker_id="W-1", verdict="approved") is None
    assert corpus.sidecars() == []
