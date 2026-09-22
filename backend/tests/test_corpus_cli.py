"""The operator's side of the corpus: import, label, inspect, export, erase.

The CLI is where a corpus either acquires a consent basis and a detector fingerprint or quietly does
not, so the tests here are about the fields a command line is easy to lose: what was relied on, which
configuration cropped the images, and whether a purge was asked for or merely previewed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import corpus
import detector_640

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

#: Loaded by *path*, under a name of this suite's choosing, because the tool's own directory goes on
#: ``sys.path`` and a module called ``corpus`` there would shadow the application's store for every
#: later import in the process - see the tool's docstring, which is why it is called
#: ``corpus_admin.py``.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("corpus_cli", _TOOLS_DIR / "corpus_admin.py")
assert _spec and _spec.loader  # the tool has to be importable for its own suite to exist
corpus_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corpus_cli)


@pytest.fixture(autouse=True)
def _isolated_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "ROOT_DIR", str(tmp_path / "corpus"))
    return tmp_path


class _StubDetector:
    def __init__(self, faces: int = 1) -> None:
        self.faces = faces

    def __call__(self, frame):
        return [
            detector_640.Detection(
                box=(140.0, 60.0, 200.0, 200.0),
                landmarks=np.array(
                    [[180.0, 120.0], [300.0, 120.0], [240.0, 170.0], [200.0, 220.0], [280.0, 220.0]],
                    dtype=np.float32,
                ),
                score=0.9,
            )
            for _ in range(self.faces)
        ]


def _install_detector(monkeypatch, *, faces: int = 1) -> None:
    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: _StubDetector(faces))


def _folder(root: Path, *, identities: int, captures: int, size: tuple[int, int] = (480, 320)) -> Path:
    for identity in range(identities):
        folder = root / f"person_{identity}"
        folder.mkdir(parents=True, exist_ok=True)
        for capture in range(captures):
            rng = np.random.default_rng(identity * 10 + capture)
            pixels = rng.integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
            Image.fromarray(pixels, "RGB").save(folder / f"{capture}.jpg", quality=95)
    return root


def test_add_records_a_stated_basis_and_the_detector_that_cropped(tmp_path, monkeypatch, capsys):
    _install_detector(monkeypatch)
    source = _folder(tmp_path / "lab", identities=2, captures=2)
    code = corpus_cli.main(
        ["add", "--source", str(source), "--consent", "staff session, signed 2026-09-22",
         "--actor", "R. Ops", "--input-size", "320", "--tiles", "1", "--overlap", "0.0", "--quiet"]
    )
    assert code == 0
    assert "stored 4 capture(s)" in capsys.readouterr().out

    records = corpus.sidecars()
    assert len(records) == 4
    assert {record.identity for record in records} == {"person_0", "person_1"}
    assert all(record.consent == "staff session, signed 2026-09-22" for record in records)
    assert all(record.detector["input_size"] == 320 for record in records)
    assert all(record.detector["model_fingerprint"] for record in records)
    assert all(record.face_px == 200.0 for record in records)
    assert all(record.native_face_px == 200.0 for record in records), "no downscale at MAX_PX 1280"


def test_add_refuses_an_import_with_no_basis(tmp_path, monkeypatch, capsys):
    """The corpus is a biometric store: the command that fills it has to say what it relied on."""
    _install_detector(monkeypatch)
    source = _folder(tmp_path / "lab", identities=1, captures=1)
    code = corpus_cli.main(["add", "--source", str(source)])
    assert code == 2
    assert "needs --consent" in capsys.readouterr().err
    assert corpus.sidecars() == []

    # An actor alone is a named basis, spelled out in the record rather than left blank.
    code = corpus_cli.main(["add", "--source", str(source), "--actor", "R. Ops", "--quiet"])
    assert code == 0
    assert corpus.sidecars()[0].consent == corpus_cli.IMPORT_CONSENT_TEMPLATE.format(actor="R. Ops")


def test_add_reports_what_it_skipped_and_can_be_told_to_skip_multi_face_frames(
    tmp_path, monkeypatch, capsys
):
    _install_detector(monkeypatch, faces=2)
    source = _folder(tmp_path / "lab", identities=1, captures=1)
    assert corpus_cli.main(
        ["add", "--source", str(source), "--consent", "test", "--strict-single-face", "--quiet"]
    ) == 0
    assert corpus.sidecars() == []
    assert "2 faces detected" in capsys.readouterr().err

    # Without the flag the largest face is used, and a frame nobody can crop is reported instead.
    assert corpus_cli.main(["add", "--source", str(source), "--consent", "test", "--quiet"]) == 0
    assert len(corpus.sidecars()) == 1


def test_unlabelled_imports_are_a_state_a_human_can_resolve(tmp_path, monkeypatch, capsys):
    _install_detector(monkeypatch)
    source = _folder(tmp_path / "pending", identities=1, captures=2)
    assert corpus_cli.main(
        ["add", "--source", str(source), "--unlabelled", "--consent", "field trial", "--quiet"]
    ) == 0
    assert all(record.identity is None for record in corpus.sidecars())

    assert corpus_cli.main(["list", "--unlabelled"]) == 0
    assert "2 capture(s)" in capsys.readouterr().out

    assert corpus_cli.main(["label", "--all-unlabelled", "--identity", "Bilal Khan", "--actor", "ops"]) == 0
    records = corpus.sidecars()
    assert {record.identity for record in records} == {"Bilal Khan"}
    assert all(record.label_history[-1]["from"] is None for record in records)

    assert corpus_cli.main(["label", "--capture", records[0].capture_id, "--clear", "--actor", "ops"]) == 0
    assert len(corpus.sidecars(unlabelled_only=True)) == 1


def test_stats_and_export_report_the_contract_the_tools_read(tmp_path, monkeypatch, capsys):
    _install_detector(monkeypatch)
    source = _folder(tmp_path / "lab", identities=2, captures=2)
    corpus_cli.main(["add", "--source", str(source), "--consent", "test", "--quiet"])

    stats_file = tmp_path / "stats.json"
    assert corpus_cli.main(["stats", "--json", str(stats_file)]) == 0
    printed = capsys.readouterr().out
    # Four captures hold C(4,2) = 6 pairs, of which 2 are same-person: 4 different-people pairs is
    # nowhere near a floor, and the command has to say so rather than printing a number.
    assert "genuine 2, impostor 4" in printed
    assert "can derive    : no" in printed
    assert "not enough different-people pairs for a floor" in printed
    payload = json.loads(stats_file.read_text(encoding="utf-8"))
    assert payload["genuine_pairs"] == 2 and payload["identities"] == 2
    assert payload["can_support_floor"] is False

    destination = tmp_path / "export"
    assert corpus_cli.main(["export", "--destination", str(destination)]) == 0
    assert sorted(path.name for path in destination.iterdir()) == ["person_0", "person_1"]
    for folder in destination.iterdir():
        assert len(list(folder.glob("*.jpg"))) == 2
        assert len(list(folder.glob("*.json"))) == 2


def test_purge_previews_until_it_is_told_to_erase(tmp_path, monkeypatch, capsys):
    _install_detector(monkeypatch)
    source = _folder(tmp_path / "lab", identities=1, captures=2)
    corpus_cli.main(["add", "--source", str(source), "--consent", "test", "--quiet"])
    frames = [Path(corpus.image_path(record.capture_id)) for record in corpus.sidecars()]

    assert corpus_cli.main(["purge", "--older-than-days", "0"]) == 0
    assert "would erase 2 capture(s)" in capsys.readouterr().out
    assert all(frame.exists() for frame in frames), "a preview must not erase a face"

    assert corpus_cli.main(["purge", "--older-than-days", "0", "--apply"]) == 0
    assert "erased 2 capture(s)" in capsys.readouterr().out
    assert not any(frame.exists() for frame in frames)
    assert corpus.sidecars() == []
