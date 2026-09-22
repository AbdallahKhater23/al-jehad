"""A corpus of real captures is only worth having if the tools measure *its* crops, not fresh ones.

THE CLAIM THESE TESTS MAKE
--------------------------
Detection is not deterministic across a detector upgrade, an OpenCV version or a machine, so a corpus
whose crop is re-derived every time it is read is a corpus whose numbers move without anybody changing
the model. The stored sidecar exists to stop that: the capture records the points its own detector
measured, and both tools read them - which is why ``--landmarks stored`` must be able to run a full
four-arm A/B **without calling a detector at all**.

So the detector is not merely unused in the runs below, it is replaced by one that raises if it is
called. That is the difference between "the numbers came out" and "the numbers came out of the stored
crops", and it is the property a real-traffic corpus is being built for.

The other half is the refusal. A corpus measured at one detector configuration and read under another
is not a corpus with slightly wrong crops - it is a report carrying one configuration's name over
another's pixels, and it has to stop rather than quietly re-detect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import corpus
import face_detector

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import contract_ab  # noqa: E402 - the path has to be set up first
import derive_facenet_band  # noqa: E402

DETECTOR_MODEL = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
FACENET_MODEL = Path(__file__).resolve().parent.parent / "models" / "facenet128.onnx"


@pytest.fixture(autouse=True)
def _isolated_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "ROOT_DIR", str(tmp_path / "corpus"))
    return tmp_path


def _live_fingerprint() -> dict:
    """Exactly what the live capture path records: the live model's digest and the live settings."""
    return corpus.detector_fingerprint(
        kind=face_detector.DETECTOR_NAME,
        model_fingerprint=face_detector.fingerprint(),
        pipeline=face_detector.active_pipeline(),
        input_size=face_detector.DETECTOR_INPUT_SIZE,
        tiles=1,
        overlap=0.0,
    )


def _face_box() -> list[float]:
    """A 200px face in the middle of a 480x320 frame: above the engine's 160px floor."""
    return [140.0, 60.0, 200.0, 200.0]


def _landmarks() -> np.ndarray:
    return np.array(
        [[180.0, 120.0], [300.0, 120.0], [240.0, 170.0], [200.0, 220.0], [280.0, 220.0]],
        dtype=np.float32,
    )


def _build_corpus(*, identities: int = 3, captures: int = 3, fingerprint: dict | None = None) -> Path:
    """A corpus of real-ish gate frames, stored the way the capture path stores them, then exported."""
    for identity in range(identities):
        for capture in range(captures):
            rng = np.random.default_rng(identity * 100 + capture)
            image = Image.fromarray(rng.integers(0, 256, size=(320, 480, 3), dtype=np.uint8), "RGB")
            prepared = corpus.prepare(image)
            corpus.store(
                prepared,
                detector=fingerprint or _live_fingerprint(),
                identity=f"person_{identity}",
                landmarks=_landmarks(),
                box=_face_box(),
                score=0.93,
                source=corpus.SOURCE_PUNCH,
                consent="test: exported corpus",
                actor="tester",
            )
    destination = Path(corpus.root_dir()).parent / "export"
    corpus.export(destination)
    return destination


class _Forbidden:
    """A detector that fails the run if anything reaches for it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("the detector was called: this run was supposed to measure stored crops")


def _ban_detection(monkeypatch) -> _Forbidden:
    import detector_640

    forbidden = _Forbidden()
    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: forbidden)
    return forbidden


def _require_models() -> None:
    for path in (DETECTOR_MODEL, FACENET_MODEL):
        if not path.exists():  # pragma: no cover - both ship with the checkout
            pytest.skip(f"{path.name} is not present")


def _ab_args(corpus_dir: Path, tmp_path: Path, *, landmarks: str = "stored", **overrides) -> list[str]:
    args = [
        "--corpus", str(corpus_dir),
        "--model", str(FACENET_MODEL),
        "--detector-model", str(DETECTOR_MODEL),
        "--input-size", str(overrides.pop("input_size", face_detector.DETECTOR_INPUT_SIZE)),
        "--tiles", str(overrides.pop("tiles", 1)),
        "--overlap", str(overrides.pop("overlap", 0.0)),
        "--min-face-pixels", "160",
        "--min-impostor-pairs", "1",   # the fixture is 9 images, not a corpus of thousands
        "--bootstrap", "10",
        "--landmarks", landmarks,
        "--json", str(tmp_path / "ab.json"),
        "--quiet",
    ]
    return args


# ---------------------------------------------------------------------------
# the A/B
# ---------------------------------------------------------------------------
def test_the_ab_scores_stored_crops_and_never_runs_a_detector(tmp_path, monkeypatch):
    """The whole point of the format, in one run: real crops, four contracts, no detection."""
    _require_models()
    exported = _build_corpus()
    forbidden = _ban_detection(monkeypatch)

    code = contract_ab.main(_ab_args(exported, tmp_path))
    assert code in (0, 1), f"unexpected exit {code}"
    assert forbidden.calls == 0

    payload = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))
    assert payload["crop_source"] == {"stored": 9, "detected": 0, "mode": "stored"}
    assert len(payload["arms"]) == 4
    assert payload["corpus_stats"]["cropped"] == 9
    assert payload["crop_fingerprint"]["input_size"] == face_detector.DETECTOR_INPUT_SIZE
    counts = {(arm["calibration"]["genuine_pairs"], arm["calibration"]["impostor_pairs"])
              for arm in payload["arms"]}
    assert len(counts) == 1, f"the arms measured different corpora: {counts}"


def test_the_ab_refuses_a_crop_measured_by_another_configuration(tmp_path, monkeypatch, capsys):
    """A corpus from a 320-input detector read under a 640 tiled run is a refusal, not a re-crop."""
    _require_models()
    exported = _build_corpus()
    forbidden = _ban_detection(monkeypatch)

    code = contract_ab.main(_ab_args(exported, tmp_path, input_size=640, tiles=2, overlap=0.2))
    assert code == 2
    assert forbidden.calls == 0, "a refusal must not fall back to detecting"
    message = capsys.readouterr().err
    assert "different detector configuration" in message
    assert "input=320" in message and "input=640" in message

    # An empty corpus folder is a different refusal, and a tool that answered the first one with the
    # second's message would send an operator to re-detect a corpus that simply is not there.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert contract_ab.main(_ab_args(empty, tmp_path)) == 2
    assert "no images under" in capsys.readouterr().err


def test_the_escape_hatch_re_detects_on_purpose(tmp_path, monkeypatch):
    """``--landmarks detect`` means "I am choosing a new crop", and it has to actually crop again."""
    import detector_640

    _require_models()
    exported = _build_corpus()
    seen: list[tuple[int, int]] = []

    def detect(frame):
        seen.append((frame.shape[1], frame.shape[0]))
        return [detector_640.Detection(box=tuple(_face_box()), landmarks=_landmarks(), score=0.91)]

    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: detect)
    code = contract_ab.main(
        _ab_args(exported, tmp_path, landmarks="detect", input_size=640, tiles=1)
    )
    assert code in (0, 1), f"unexpected exit {code}"
    assert len(seen) == 9, "one detection pass per image, and only one"
    payload = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))
    assert payload["crop_source"] == {"stored": 0, "detected": 9, "mode": "detect"}


# ---------------------------------------------------------------------------
# the band derivation
# ---------------------------------------------------------------------------
def test_the_band_tool_reads_the_stored_crop_rather_than_detecting(tmp_path, monkeypatch):
    """The same claim for the tool that writes the threshold, on the same corpus."""
    _require_models()
    exported = _build_corpus()

    def forbidden(image):
        raise AssertionError("the band tool re-detected a corpus that already had its crops stored")

    monkeypatch.setattr(face_detector, "detect_and_align", forbidden)
    out = tmp_path / "band.json"
    code = derive_facenet_band.main(
        [
            "--corpus", str(exported),
            "--pipeline", face_detector.active_pipeline(),
            "--landmarks", "stored",
            "--min-impostor-pairs", "1",
            "--json", str(out),
            "--quiet",
        ]
    )
    # Whether this 9-image fixture yields a band is the *tool's* decision (it refuses a window it
    # cannot place two lines in - and under the suite's embedding stub every distance is 0, so it
    # refuses), and it is not what this test is about. The crops are.
    assert code in (0, 1, 2, 3), f"unexpected exit {code}"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["crop_source"] == {"stored": 9, "detected": 0, "mode": "stored"}
    assert report["images_found"] == 9
    assert not [note for note in report["images_skipped"] if "landmarks" in note]


def test_the_band_tool_refuses_a_crop_from_another_pipeline_configuration(tmp_path, monkeypatch):
    """Stored crops are tied to the *live* configuration, so a corpus from another one is refused."""
    _require_models()
    other = corpus.detector_fingerprint(
        kind=face_detector.DETECTOR_NAME,
        model_fingerprint=face_detector.fingerprint(),
        pipeline=face_detector.active_pipeline(),
        input_size=640,          # the capture ran at the live 320, so this is a different crop
        tiles=1,
        overlap=0.0,
    )
    exported = _build_corpus(fingerprint=other)
    monkeypatch.setattr(
        face_detector, "detect_and_align",
        lambda image: (_ for _ in ()).throw(AssertionError("must not re-detect")),
    )
    out = tmp_path / "band.json"
    code = derive_facenet_band.main(
        ["--corpus", str(exported), "--pipeline", face_detector.active_pipeline(),
         "--landmarks", "stored", "--json", str(out), "--quiet"]
    )
    assert code == 2
    assert not out.exists() or "crop_source" not in json.loads(out.read_text(encoding="utf-8"))


def test_a_capture_with_no_sidecar_is_still_detected_by_default(tmp_path, monkeypatch):
    """``auto`` is not ``stored``: an ordinary folder of photographs keeps working."""
    import cv2

    import detector_640

    _require_models()
    plain = tmp_path / "plain" / "person_a"
    plain.mkdir(parents=True)
    rng = np.random.default_rng(0)
    for index in range(2):
        cv2.imwrite(
            str(plain / f"{index}.jpg"),
            rng.integers(0, 256, size=(320, 480, 3), dtype=np.uint8),
        )
    seen: list[int] = []

    def detect(frame):
        seen.append(frame.shape[0])
        return [detector_640.Detection(box=tuple(_face_box()), landmarks=_landmarks(), score=0.9)]

    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: detect)
    code = contract_ab.main(_ab_args(tmp_path / "plain", tmp_path, landmarks="auto"))
    assert code == 2, "one identity cannot support an impostor distribution"
    assert len(seen) == 2, "an unstored image must still be detected under --landmarks auto"
