"""Where a capture lives, and what happens when its label changes.

The layout is not decoration: the partition **is** the label, which is why ``label()`` moves files and
why the version-1 flat store has to keep working. The failures these tests are written against are the
two that a directory store can produce and a table cannot:

* a capture that exists twice, in two identities' folders, after a re-label crashed halfway;
* a frame that outlived its sidecar (or the reverse), which is a face nobody can explain - or a record
  pointing at a picture that moved away.

Both are asserted by *walking the tree* and counting, rather than by trusting the move's own report.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import corpus


@pytest.fixture(autouse=True)
def _isolated_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "ROOT_DIR", str(tmp_path / "corpus"))
    return tmp_path


LANDMARKS = np.array(
    [[150.0, 120.0], [230.0, 120.0], [190.0, 160.0], [165.0, 200.0], [215.0, 200.0]], dtype=np.float32
)


def _fingerprint(**overrides):
    base = dict(
        kind="yunet",
        model_fingerprint="a" * 16,
        input_size=640,
        square=False,
        tiles=1,
        overlap=0.0,
    )
    base.update(overrides)
    return corpus.detector_fingerprint(**base)


def _capture(identity: str | None = "person_a", **overrides) -> corpus.Capture:
    prepared = corpus.prepare(Image.new("RGB", (480, 320), (90, 100, 110)))
    return corpus.store(
        prepared,
        detector=overrides.pop("detector", _fingerprint()),
        identity=identity,
        landmarks=overrides.pop("landmarks", LANDMARKS),
        box=overrides.pop("box", [140.0, 110.0, 100.0, 110.0]),
        score=overrides.pop("score", 0.94),
        consent=overrides.pop("consent", "test: synthetic frame"),
        **overrides,
    )


def _tree(root: Path) -> list[str]:
    """Every file under the corpus root, as ``partition/name``, sorted."""
    return sorted(
        str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*") if path.is_file()
    )


# ---------------------------------------------------------------------------
# the layout
# ---------------------------------------------------------------------------
def test_a_capture_is_filed_under_its_identity_with_a_timestamped_name():
    record = _capture("Bilal Khan")
    root = Path(corpus.root_dir())

    assert _tree(root) == [
        f"Bilal_Khan/{record.capture_id}.jpg",
        f"Bilal_Khan/{record.capture_id}.json",
    ], "identity is the directory, the frame and its provenance sit together"
    # ``<timestamp>_<random>``: sortable, and it does not encode who this is - a countable filename for
    # a face is a directory anybody can enumerate.
    stamp, token = record.capture_id.split("_")
    assert len(stamp) == 15 and stamp[8] == "T" and len(token) == 8
    assert record.capture_id in Path(corpus.image_path(record.capture_id)).name
    assert Path(corpus.sidecar_path(record.capture_id)).parent == Path(corpus.image_path(record.capture_id)).parent


def test_two_captures_in_the_same_second_do_not_collide():
    ids = {corpus.new_capture_id().split("_")[1] for _ in range(50)}
    assert len(ids) == 50, "the random suffix is what keeps a burst of captures apart"


def test_unlabelled_captures_are_a_partition_of_their_own_not_a_missing_field():
    record = _capture(identity=None)
    assert _tree(Path(corpus.root_dir())) == [
        f"_unlabelled/{record.capture_id}.jpg",
        f"_unlabelled/{record.capture_id}.json",
    ]
    assert record.identity is None and record.label_history == []
    assert [item.capture_id for item in corpus.sidecars(unlabelled_only=True)] == [record.capture_id]
    assert corpus.stats()["unlabelled"] == 1


def test_a_relabel_moves_the_capture_rather_than_copying_it():
    record = _capture("person_a")

    moved = corpus.label(record.capture_id, "person_b", actor="ops")

    assert moved.identity == "person_b"
    assert _tree(Path(corpus.root_dir())) == [
        f"person_b/{record.capture_id}.jpg",
        f"person_b/{record.capture_id}.json",
    ], "no duplicate was left behind in the old partition"
    assert corpus.find_capture(record.capture_id)[0] == "person_b"
    assert [item["to"] for item in moved.label_history] == ["person_a", "person_b"]


def test_clearing_a_label_rehomes_the_capture_to_unlabelled():
    record = _capture("person_a")
    cleared = corpus.label(record.capture_id, None, actor="ops")
    assert cleared.identity is None
    assert _tree(Path(corpus.root_dir())) == [
        f"_unlabelled/{record.capture_id}.jpg",
        f"_unlabelled/{record.capture_id}.json",
    ]
    assert [item["from"] for item in cleared.label_history] == [None, "person_a"]


def test_a_capture_that_vanished_is_not_quietly_answered_with_a_path():
    """``image_path`` looks a capture up, so an id nobody can find raises instead of naming a new file.

    The alternative - a pure path join - is how a caller writing to a capture it believes exists creates
    a *second* one in a stale partition.
    """
    assert corpus.exists("20260101T000000_deadbeef") is False
    with pytest.raises(corpus.CorpusError):
        corpus.image_path("20260101T000000_deadbeef")
    record = _capture()
    assert corpus.exists(record.capture_id) is True


def test_a_version_one_flat_store_is_still_read_and_rehomed_by_labelling(tmp_path):
    """The old layout was ``captures/<id>.jpg`` with no ``camera`` and no ``quality``.

    Both additions are *additions* and the layout follows the label, so a v1 record still means exactly
    what it meant - which is the test a version bump has to pass. It is read, it is not written, and the
    first relabel moves it into the current shape.
    """
    legacy = Path(corpus.root_dir()) / "captures"
    legacy.mkdir(parents=True)
    record = _capture("person_a")
    # Paths first: once a second copy of the id exists, a lookup cannot say which one is meant.
    frame = Path(corpus.image_path(record.capture_id))
    sidecar = Path(corpus.sidecar_path(record.capture_id))
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["version"] = 1
    payload.pop("camera")
    payload.pop("quality")
    (legacy / f"{record.capture_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    (legacy / f"{record.capture_id}.jpg").write_bytes(frame.read_bytes())
    frame.unlink()
    sidecar.unlink()
    (Path(corpus.root_dir()) / "person_a").rmdir()

    read = corpus.load(record.capture_id)
    assert read.version == 1 and read.identity == "person_a" and read.camera is None

    moved = corpus.label(record.capture_id, "person_b", actor="ops")
    assert moved.version == corpus.SIDECAR_VERSION
    assert _tree(Path(corpus.root_dir())) == [
        f"person_b/{record.capture_id}.jpg",
        f"person_b/{record.capture_id}.json",
    ], "the legacy pair went with it, leaving nothing behind"


def test_the_camera_and_the_quality_assessment_travel_with_the_capture(tmp_path):
    record = _capture(
        "person_a",
        camera="gate-north",
        quality={"flags": ["small_face"], "hard_case": True, "face_px": 20.0},
    )
    stored = json.loads(Path(corpus.sidecar_path(record.capture_id)).read_text(encoding="utf-8"))
    assert stored["camera"] == "gate-north"
    assert stored["quality"]["flags"] == ["small_face"]
    assert corpus.load(record.capture_id).hard_case is True
    assert corpus.stats()["cameras"] == {"gate-north": 1}


# ---------------------------------------------------------------------------
# hard cases: kept, flagged, and out of the measurement
# ---------------------------------------------------------------------------
def test_a_hard_case_is_kept_but_left_out_of_the_measurement(tmp_path):
    good = _capture("person_a")
    edge = _capture("person_a", quality={"flags": ["small_face"], "hard_case": True, "face_px": 20.0})

    assert corpus.exists(edge.capture_id)
    payload = corpus.stats()
    assert payload["captures"] == 2 and payload["measured"] == 1
    assert payload["quality"]["flags"] == {"small_face": 1}
    assert payload["quality"]["excluded_from_measurement"] == 1

    destination = Path(corpus.root_dir()).parent / "export"
    report = corpus.export(destination)
    assert report["images"] == 1 and report["hard_cases_skipped"] == 1
    assert not (destination / "person_a" / f"{edge.capture_id}.jpg").exists()
    assert (destination / "person_a" / f"{good.capture_id}.jpg").exists()

    with_edges = corpus.export(destination, include_hard_cases=True)
    assert with_edges["images"] == 2
    assert corpus.stats(include_hard_cases=True)["measured"] == 2


# ---------------------------------------------------------------------------
# the one thing a store is expected to refuse
# ---------------------------------------------------------------------------
def test_an_unknown_sidecar_version_is_refused_rather_than_half_read():
    record = _capture()
    path = Path(corpus.sidecar_path(record.capture_id))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = corpus.SIDECAR_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(corpus.CorpusError) as caught:
        corpus.load(record.capture_id)
    assert "refusing" in str(caught.value)
