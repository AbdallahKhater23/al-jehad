"""The wiring that begins the 512 shadow migration, held to its refusals.

WHY THESE TESTS DO NOT OPEN A GRAPH
-----------------------------------
The migration's own artefact - a ~90 MiB FaceNet export - does not ship, and ``harness`` replaces
the embedding engine before import so no other punch test opens one either. What is left to pin is
everything that decides whether the migration is *sound*: that an absent graph is reported rather
than faked, that a pair which cannot be a controlled comparison is refused, that a gallery is never
read by a graph that did not build it, and that a frame the punch path would refuse is counted by
its reason instead of becoming evidence about an encoder.

The encoders here are therefore stand-ins, and that is not a shortcut for the same reason it is not
one in ``test_shadow_rollout_and_contract_ab``: none of these claims is about FaceNet's accuracy.
They are about control flow and refusal, which stubs exercise exactly as well as weights would.

WHAT IS DELIBERATELY MISSING
----------------------------
A test that a real 512 graph produces a usable band. That is a corpus measurement
(``tools/contract_ab.py``, ``derive_facenet_band.py``), not an assertion, and a test that invented
the line would be the very defect the band table exists to prevent.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest

import face_align
import shadow
import shadow_rollout
from shadow_rollout import CropResult, ShadowUnavailable, validate_pair


# ---------------------------------------------------------------------------
# stand-in encoders: two widths, one canvas
# ---------------------------------------------------------------------------
class _FakeEncoder:
    """Enough of ``FaceNetORT`` for the wiring: a width, a canvas, an id, and ``embed``.

    The vector it returns is deterministic in the tank of the tensor, so a pair of these produces a
    stable distance and the paired log gets real rows rather than nulls.
    """

    def __init__(self, width: int, *, name: str = "graph", input_size: int = 160) -> None:
        self.width = int(width)
        self.input_size = int(input_size)
        self.path = f"/models/{name}_{width}.onnx"
        self.model_id = f"{name}-{width}".ljust(32, "0")

    def embed(self, tensor) -> np.ndarray:
        rng = np.random.default_rng(seed=int(np.asarray(tensor, dtype=np.float64).sum()) % 9973)
        vector = rng.normal(size=self.width)
        return (vector / np.linalg.norm(vector)).astype(np.float32)


class _Band:
    """The one method ``shadow.Gallery`` needs. Lines are beside the point in these tests."""

    def classify(self, distance: float) -> str:
        return "approved" if float(distance) <= 0.5 else "refused"


_BAND = _Band()


def _tensor(value: int = 128) -> np.ndarray:
    crop = np.full((112, 112, 3), value, dtype=np.uint8)
    return face_align.to_tensor(crop, shadow_rollout.INCUMBENT_CONTRACT, size=160)


def _pair(*, enforced_width: int = 128, shadow_width: int = 512) -> shadow_rollout.Encoders:
    return shadow_rollout.Encoders(
        enforced=_FakeEncoder(enforced_width, name="incumbent"),
        shadow=_FakeEncoder(shadow_width, name="candidate"),
        contract=shadow_rollout.INCUMBENT_CONTRACT,
    )


@pytest.fixture
def settings(monkeypatch):
    """A stand-in for ``config.settings`` carrying only the shadow knobs these tests read."""

    class _Settings:
        facenet_shadow_model_path = "backend/models/facenet512.onnx"
        facenet_shadow_model_sha256 = ""
        facenet_shadow_contract = "bgr:0_1"
        shadow_log_path = ""
        database_path = "times.db"

    stub = _Settings()
    monkeypatch.setattr(shadow_rollout, "_settings", lambda: stub)
    return stub


# ---------------------------------------------------------------------------
# 1. an absent graph is reported, not faked
# ---------------------------------------------------------------------------
def test_an_absent_shadow_graph_is_reported_by_path_and_variable(settings, tmp_path, monkeypatch):
    """The shipped state. It has to read as "fetch a file", not as a fault or as a silence."""
    missing = tmp_path / "facenet512.onnx"
    monkeypatch.setattr(shadow_rollout, "shadow_model_path", lambda: missing)

    report = shadow_rollout.status()

    assert report["started"] is False
    assert report["encoders"] is None
    assert str(missing) in report["reason"], report["reason"]
    assert "FACENET_SHADOW_MODEL_PATH" in report["reason"], (
        "the reason must name the variable that moves the path, or an operator has to read the "
        "source to find out how to supply a graph"
    )
    assert report["configured"]["shadow_present"] is False


def test_status_absorbs_its_own_refusals_but_not_a_broken_environment(settings, monkeypatch):
    """``status`` reports the things it knows how to refuse, and does not swallow a real fault.

    The distinction matters: a missing graph or a contract disagreement is a *state* an operator
    reads and acts on, while an unexpected error is a bug that must not be dressed up as one of
    them - a status command that answered "no graph" for every failure would send somebody to
    fetch a file that is already there.
    """

    def explode():
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(shadow_rollout, "shadow_model_path", explode)
    with pytest.raises(RuntimeError):
        shadow_rollout.status()


def test_load_refuses_an_absent_graph_without_opening_anything(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_rollout, "shadow_model_path", lambda: tmp_path / "nope.onnx")
    with pytest.raises(ShadowUnavailable) as raised:
        shadow_rollout.load_encoders()
    assert "nope.onnx" in str(raised.value)


# ---------------------------------------------------------------------------
# 2. the contract, and why a mismatch is refused rather than scored
# ---------------------------------------------------------------------------
def test_an_unknown_contract_id_lists_the_ones_that_exist():
    with pytest.raises(ShadowUnavailable) as raised:
        shadow_rollout.contract_from_id("rgb:9_9")
    message = str(raised.value)
    for contract in face_align.CONTRACTS:
        assert contract.id in message, message


def test_the_incumbent_contract_id_resolves_to_the_incumbent_contract():
    assert shadow_rollout.contract_from_id("bgr:0_1") is shadow_rollout.INCUMBENT_CONTRACT
    assert shadow_rollout.INCUMBENT_CONTRACT is face_align.CONTRACTS[0], (
        "the incumbent is CONTRACTS[0]; a second definition here is a second thing to drift"
    )


def test_a_shadow_contract_that_differs_is_refused_before_a_graph_is_opened(settings):
    """One crop is handed to both encoders, so a disagreement is not a pair at all.

    Refused *before* loading, so this holds even with no graph on the host - which is also what
    makes the failure readable: it is about the tensor, not about a missing file.
    """
    settings.facenet_shadow_contract = "rgb:0_1"

    with pytest.raises(ShadowUnavailable) as raised:
        shadow_rollout.shadow_contract()

    message = str(raised.value)
    assert "contract_ab" in message, (
        "the refusal must name the tool that measures a contract, or the operator's next move is "
        "to change the setting until the error goes away"
    )
    assert "same crop" in message


# ---------------------------------------------------------------------------
# 3. the pair itself
# ---------------------------------------------------------------------------
def test_two_graphs_of_the_same_width_are_refused():
    """The likeliest operator error: pointing the shadow path at the incumbent's file.

    Caught here rather than by the cutover gate's universal-agreement check, which would only
    notice after a day of logging - and would read as "the encoders agree", not as "you copied the
    wrong file".
    """
    with pytest.raises(ShadowUnavailable) as raised:
        validate_pair(_FakeEncoder(128), _FakeEncoder(128))
    assert "same width" in str(raised.value)


def test_graphs_that_declare_different_canvases_are_refused():
    """One tensor goes to both, so different input sizes cannot be fed the same pixels."""
    with pytest.raises(ShadowUnavailable) as raised:
        validate_pair(_FakeEncoder(128, input_size=160), _FakeEncoder(512, input_size=224))
    assert "input canvas" in str(raised.value)
    assert "crop change" in str(raised.value), (
        "the message has to say why resizing is not the fix, or somebody resizes"
    )


def test_a_real_pair_is_accepted():
    validate_pair(_FakeEncoder(128), _FakeEncoder(512))
    pair = _pair()
    assert (pair.enforced_width, pair.shadow_width) == (128, 512)
    described = pair.describe()
    assert described["contract"] == "bgr:0_1"
    assert described["shadow"]["width"] == 512


# ---------------------------------------------------------------------------
# 4. a gallery is never read by a graph that did not build it
# ---------------------------------------------------------------------------
def _gallery(width: int, *, members: int = 2) -> shadow.Gallery:
    gallery = shadow.Gallery(width=width, band=_BAND, name="shadow")
    for index in range(members):
        vector = np.zeros(width, dtype=np.float64)
        vector[index % width] = 1.0
        gallery.add(f"worker-{index}", vector.copy())
    return gallery


def test_a_cached_gallery_round_trips(tmp_path):
    original = _gallery(512)
    path = shadow_rollout.save_gallery(
        tmp_path / "gallery.json", original, model_id="graph-512".ljust(32, "0"), contract="bgr:0_1",
        band=type("B", (), {"model": "Facenet-512", "approve": 0.42, "review": 0.5, "evidence": "e"})(),
    )
    loaded, payload = shadow_rollout.load_gallery(
        path, width=512, model_id="graph-512".ljust(32, "0"),
        band=type("B", (), {"model": "Facenet-512", "approve": 0.42, "review": 0.5, "evidence": "e"})(),
    )
    assert loaded.size == original.size
    assert set(loaded.templates) == set(original.templates)
    assert payload["band"]["evidence"] == "e"


def test_a_gallery_of_another_width_is_refused(tmp_path):
    path = shadow_rollout.save_gallery(
        tmp_path / "gallery.json", _gallery(128), model_id="graph-128".ljust(32, "0"),
        contract="bgr:0_1", band=None,
    )
    with pytest.raises(ShadowUnavailable) as raised:
        shadow_rollout.load_gallery(path, width=512, model_id="graph-128".ljust(32, "0"), band=None)
    assert "128-D" in str(raised.value) and "512-D" in str(raised.value)
    assert "backfill" in str(raised.value)


def test_a_gallery_built_by_another_graph_of_the_same_width_is_refused(tmp_path):
    """The subtler half: same width, differently-trained export - a different space entirely."""
    path = shadow_rollout.save_gallery(
        tmp_path / "gallery.json", _gallery(512), model_id="graph-a".ljust(32, "0"),
        contract="bgr:0_1", band=None,
    )
    with pytest.raises(ShadowUnavailable) as raised:
        shadow_rollout.load_gallery(path, width=512, model_id="graph-b".ljust(32, "0"), band=None)
    assert "another space" in str(raised.value)


def test_an_absent_gallery_says_to_run_backfill(tmp_path):
    with pytest.raises(ShadowUnavailable) as raised:
        shadow_rollout.load_gallery(tmp_path / "none.json", width=512, model_id="x", band=None)
    assert "backfill" in str(raised.value)


# ---------------------------------------------------------------------------
# 5. the backfill reports what it could not do
# ---------------------------------------------------------------------------
def test_the_backfill_names_the_workers_it_could_not_embed(monkeypatch):
    """Coverage is only honest if the people left out are named.

    A gallery that silently omitted everyone it could not process would report a fraction of the
    workforce while describing itself as a complete one - and the cutover gate's first test is that
    fraction.
    """
    import biometrics

    monkeypatch.setattr(shadow_rollout, "worker_ids", lambda conn: ["1", "2", "3"])
    monkeypatch.setattr(
        biometrics,
        "resolve_photo",
        lambda user_id: None if user_id == "1" else f"/photos/{user_id}.jpg",
    )
    monkeypatch.setattr(
        shadow_rollout,
        "tensor_for_path",
        lambda path, **kwargs: CropResult(None, "no face") if path.endswith("2.jpg") else CropResult(_tensor()),
    )

    gallery, report = shadow_rollout.build_shadow_gallery(None, _pair(), band=None)

    assert report["templates"] == 1
    assert gallery.size == 1
    reasons = {row["worker_id"]: row["reason"] for row in report["skipped"]}
    assert reasons == {"1": "no reference photo", "2": "no face"}


def test_the_backfill_embeds_at_the_shadow_width(monkeypatch):
    import biometrics

    monkeypatch.setattr(shadow_rollout, "worker_ids", lambda conn: ["7"])
    monkeypatch.setattr(biometrics, "resolve_photo", lambda user_id: "/photos/7.jpg")
    monkeypatch.setattr(shadow_rollout, "tensor_for_path", lambda path, **kw: CropResult(_tensor()))

    gallery, _report = shadow_rollout.build_shadow_gallery(None, _pair(), band=None)

    assert gallery.width == 512, "the shadow gallery must be the shadow encoder's width"
    assert all(vector.shape == (512,) for vector in gallery.templates.values())


# ---------------------------------------------------------------------------
# 6. the punch path's own crop rule, applied rather than relaxed
# ---------------------------------------------------------------------------
def _detection(width: int, x: int = 0, y: int = 0) -> dict:
    return {
        "face": np.zeros((112, 112, 3), dtype=np.uint8),
        "facial_area": {"x": x, "y": y, "w": width, "h": width},
        "confidence": 0.99,
    }


def test_a_frame_the_punch_path_would_refuse_is_skipped_by_reason(monkeypatch):
    """Two people is the gate's ambiguity, not an encoder's evidence."""
    import face_detector

    monkeypatch.setattr(face_detector, "detect_and_align", lambda frame: [])
    assert shadow_rollout.crop_frame(np.zeros((10, 10, 3), np.uint8), contract=shadow_rollout.INCUMBENT_CONTRACT, input_size=160).reason == "no face"

    monkeypatch.setattr(
        face_detector, "detect_and_align", lambda frame: [_detection(120, 0), _detection(120, 400)]
    )
    many = shadow_rollout.crop_frame(np.zeros((10, 10, 3), np.uint8), contract=shadow_rollout.INCUMBENT_CONTRACT, input_size=160)
    assert many.reason == "many faces", many.reason

    monkeypatch.setattr(face_detector, "detect_and_align", lambda frame: [_detection(4), _detection(5)])
    specks = shadow_rollout.crop_frame(np.zeros((10, 10, 3), np.uint8), contract=shadow_rollout.INCUMBENT_CONTRACT, input_size=160)
    assert specks.reason == "no subject-sized face", specks.reason


def test_one_face_in_two_boxes_is_one_subject_and_crops(monkeypatch):
    """The duplicate-box case the punch path merges; a shadow must merge it too."""
    import face_detector

    monkeypatch.setattr(
        face_detector,
        "detect_and_align",
        lambda frame: [_detection(120, 100, 80), _detection(112, 104, 84)],
    )
    cropped = shadow_rollout.crop_frame(
        np.zeros((10, 10, 3), np.uint8), contract=shadow_rollout.INCUMBENT_CONTRACT, input_size=160
    )
    assert cropped.ok, cropped.reason
    assert cropped.tensor.shape == (1, 160, 160, 3)
    assert cropped.face_px == 120, "the frame's own detected face size is recorded beside the tensor"


def test_an_unreadable_frame_is_a_skip_not_a_crash():
    assert shadow_rollout.crop_frame(None, contract=shadow_rollout.INCUMBENT_CONTRACT, input_size=160).reason == (
        "image could not be read"
    )


# ---------------------------------------------------------------------------
# 7. the paired run
# ---------------------------------------------------------------------------
def _scored(monkeypatch, frames, tmp_path, *, failures=None):
    failures = failures or {}
    monkeypatch.setattr(
        shadow_rollout,
        "tensor_for_path",
        lambda path, **kwargs: CropResult(None, failures[path]) if path in failures else CropResult(_tensor()),
    )
    pair = _pair()
    return shadow_rollout.score_frames(
        pair,
        enforced_gallery=_gallery(128),
        shadow_gallery=_gallery(512),
        frames=frames,
        db_path=tmp_path / "shadow.sqlite",
    )


def test_unusable_frames_are_counted_by_reason_and_not_as_agreement(monkeypatch, tmp_path):
    """A run that read 500 frames and scored 40 has to say so."""
    frames = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    report = _scored(
        monkeypatch, frames, tmp_path, failures={"b.jpg": "no face", "c.jpg": "many faces"}
    )

    assert report["scored"] == 2
    assert report["skipped"] == {"no face": 1, "many faces": 1}
    assert report["summary"]["paired_samples"] == 2, (
        "a skipped frame must not enter the paired log, where it would read as an agreement"
    )


def test_the_paired_rows_carry_both_widths(monkeypatch, tmp_path):
    import sqlite3

    report = _scored(monkeypatch, ["a.jpg", "b.jpg"], tmp_path)
    rows = sqlite3.connect(report["shadow_log"]).execute(
        "SELECT enforced_width, shadow_width, enforced_dist, shadow_dist, outcome FROM shadow_scores"
    ).fetchall()

    assert len(rows) == 2
    for enforced_width, shadow_width, enforced_dist, shadow_dist, outcome in rows:
        assert (enforced_width, shadow_width) == (128, 512), (
            "the log is the audit of which encoder decided what; a row without both widths cannot "
            "answer that after a rollback"
        )
        assert outcome == shadow.OUTCOME_OK
        assert enforced_dist is not None and shadow_dist is not None


def test_a_frame_folder_is_capped_from_the_newest_end(tmp_path):
    for name in ("a.jpg", "b.jpg", "c.jpg", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")

    assert [p.name for p in shadow_rollout.frame_paths(tmp_path, limit=2)] == ["b.jpg", "c.jpg"]
    assert [p.name for p in shadow_rollout.frame_paths(tmp_path)] == ["a.jpg", "b.jpg", "c.jpg"]


def test_a_missing_folder_is_a_refusal(tmp_path):
    with pytest.raises(ShadowUnavailable):
        shadow_rollout.frame_paths(tmp_path / "absent")


# ---------------------------------------------------------------------------
# 8. the band belongs to the space it is a line in
# ---------------------------------------------------------------------------
def test_the_shadow_band_records_what_measured_it(app_module):
    live = shadow_rollout.face_detector.active_band()

    band = shadow_rollout.shadow_band(
        approve=0.42, review=0.5, model="Facenet-512", evidence="measured 2026-09-24"
    )

    assert (band.approve, band.review) == (0.42, 0.5)
    assert band.evidence == "measured 2026-09-24"
    assert band.model == "Facenet-512"
    assert band.genuine_ceiling == live.genuine_ceiling, (
        "the boundaries are the band table's, not this module's: only the lines were replaced"
    )


def test_a_shadow_band_is_one_line_when_no_review_line_was_measured(app_module):
    band = shadow_rollout.shadow_band(approve=0.42, model="Facenet-512", evidence="e")
    assert band.review == band.approve


# ---------------------------------------------------------------------------
# 9. the property the whole design rests on
# ---------------------------------------------------------------------------
#: The modules a punch, a clock link or an enrollment actually runs through. Checked by source
#: because that is where the change would be made: this test's own process has imported the module
#: under test, so "is it in ``sys.modules``" answers a question about pytest, not about the app.
REQUEST_PATH_MODULES = (
    "main.py",
    "face_engine.py",
    "face_onnx.py",
    "quick_links.py",
    "enrollment.py",
    "offline_sync.py",
    "overtime.py",
    "biometrics.py",
)


def test_the_request_path_does_not_import_the_migration(app_module):
    """Zero downtime is structural: nothing the punch path runs can reach this module.

    Pinned because it is the one property that would silently stop being true - a later change that
    imported ``shadow_rollout`` from ``main`` would put a second 90 MiB session, and a possible
    failure, inside a worker's clock-in, and every test above would still pass.
    """
    backend = shadow_rollout.Path(shadow_rollout.__file__).parent
    offenders = []
    for name in REQUEST_PATH_MODULES:
        path = backend / name
        if not path.exists():  # pragma: no cover - a module renamed away
            continue
        text = path.read_text(encoding="utf-8")
        if "shadow_rollout" in text or "ShadowScorer" in text:
            offenders.append(name)
    assert offenders == [], (
        f"{offenders} reach the migration instrument from the request path; the shadow encoder must "
        "stay offline so it cannot fail or slow a punch"
    )


def test_the_migration_never_writes_a_template(app_module):
    """``biometrics.write_reference`` is the only template writer, and this module is not a caller.

    A migration that re-enrolled people would be a migration that had already cut over - and the
    128-D templates are the incumbent's control group until it has. Read off the source rather than
    asserted in prose, because the failure this guards against is a helpful-looking one-line call
    added later by somebody trying to "finish" the backfill.
    """
    text = open(shadow_rollout.__file__, encoding="utf-8").read()
    for forbidden in ("write_reference", "remove_files", "quarantine_legacy"):
        assert forbidden not in text, f"the migration calls {forbidden}"
