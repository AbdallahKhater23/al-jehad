"""Two migration instruments, held to the properties that make them instruments.

WHY THESE TESTS USE A FAKE ENCODER, AND WHY THAT IS NOT A SHORTCUT
------------------------------------------------------------------
``shadow.py`` and ``tools/contract_ab.py`` exist to answer questions about *pairs of
configurations* - 128-D against 512-D, RGB against BGR - and the checkout ships one graph, no
labelled corpus, and no second encoder. So the encoders here are stubs. That is the right tool
for the questions actually under test, because none of them are about FaceNet's accuracy:

* that a shadow cannot change the enforced answer, which is a property of the *control flow*;
* that a comparison refuses to happen across widths, which is a property of the *gallery*;
* that the cutover gate refuses on coverage, volume, health, permissiveness and universal
  agreement, which is arithmetic over counters;
* that the A/B scores all four contracts on one crop set, which is a property of *ordering*.

The one thing stubs cannot prove is that the numbers in the report are the numbers a real
FaceNet would produce. ``facenet_ort`` owns that claim and has its own pins; these tests own the
claims the arithmetic would otherwise be free to get wrong.

WHAT THESE FILES CANNOT SEE
---------------------------
``contract_ab``'s detector is stubbed in the end-to-end run below, so nothing here exercises
``detector_640``'s letterbox (that module has its own measurement, verified against the live
artifact). The A/B's refusal to compare four *different* corpora is asserted directly instead.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

import calibration
import face_align
import shadow
from shadow import (
    Gallery,
    ShadowError,
    ShadowScorer,
    coverage,
    flip_ready,
    paired_confusion,
    rollout_report,
)

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import contract_ab  # noqa: E402 - the path has to be set up first


# ---------------------------------------------------------------------------
# a stand-in encoder: the tensor carries the identity, so the gate's arithmetic has real pairs
# ---------------------------------------------------------------------------
class _FakeEncoder:
    """A deterministic encoder that reads an identity code out of the tensor's first 4 values.

    Real enough to move a threshold: captures of one identity land close together, different
    identities land far apart, and the spread is controlled by ``jitter`` - so ``derive_band`` sees
    a genuine window and an impostor window rather than two clouds on top of each other.
    """

    def __init__(
        self,
        width: int,
        *,
        jitter: float = 0.02,
        fail: bool = False,
        input_size: int = 160,
        salt: str = "a",
    ) -> None:
        self.width = int(width)
        self.jitter = float(jitter)
        self.fail = bool(fail)
        self.input_size = int(input_size)
        self.model_id = f"fake-{salt}-{width}"
        self.calls = 0

    def embed(self, tensor: np.ndarray) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated execution-provider failure")
        flat = np.asarray(tensor, dtype=np.float64).reshape(-1)
        code = flat[:4]
        base = np.zeros(self.width, dtype=np.float64)
        for index, value in enumerate(code):
            base[index * (self.width // 4) % self.width] = value
        noise = np.random.default_rng(int(hashlib.sha256(flat.tobytes()).hexdigest()[:8], 16))
        vector = base + self.jitter * noise.normal(size=self.width)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector


def _signal(identity: int, capture: int, *, salt: float = 0.0) -> np.ndarray:
    """A ``(160, 160, 3)`` tensor whose leading values are the identity code, per the fake above."""
    tensor = np.zeros((160, 160, 3), dtype=np.float32)
    tensor[:, :, 0] = salt  # the part the fake ignores: it makes two arms differ as pixels
    tensor[0, 0, 0] = float(identity)
    tensor[0, 1, 0] = float(capture)
    tensor[0, 2, 0] = 1.0
    return tensor


def _gallery(width: int, *ids: int, band: object | None = None) -> Gallery:
    gallery = Gallery(
        width=width,
        band=band or calibration.Band(
            approve=0.9, review=0.95, genuine_ceiling=0.2, impostor_floor=0.8,
            separation_ratio=4.0, review_window_collapsed=False,
        ),
        name=f"gallery-{width}",
    )
    for identity in ids:
        gallery.add(identity, _unit(_FakeEncoder(width).embed(_signal(identity, 0))))
    return gallery


def _unit(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float64).reshape(-1)
    return vector / float(np.linalg.norm(vector))


def _arm(label: str, *, ratio: float, low: float, ceiling: float, floor: float):
    """An ``Arm`` with fabricated statistics, for the ranking rule alone."""
    contract = face_align.Contract(
        face_align.ChannelOrder.RGB, face_align.ValueRange.ZERO_ONE, label
    )
    report = {
        "raw": {"genuine_ceiling": ceiling, "impostor_floor": floor, "separation_ratio": ratio},
        "ratio_ci": [low, floor / ceiling * 1.05],
        "passes_margin_on_ci": low >= calibration.MARGIN_SQUARED,
        "operating": {"eer": 0.01},
        "review_window_collapsed": False,
    }
    return contract_ab.Arm(
        contract=contract,
        distances=(np.array([ceiling]), np.array([floor])),
        report=report,
        norm_mean=1.0,
        norm_min=1.0,
        norm_max=1.0,
        tensor_stats={"min": 0.0, "max": 1.0, "mean": 0.5, "shape": 1.0},
    )


class _StubDetector:
    """A detector that reports one face where the test says one is, in native coordinates."""

    def __init__(self, *, faces: int = 1, detect_nothing: bool = False) -> None:
        self.faces = faces
        self.detect_nothing = detect_nothing
        self.calls = 0

    def __call__(self, frame: np.ndarray) -> list:
        from detector_640 import Detection

        self.calls += 1
        if self.detect_nothing:
            return []
        landmarks = face_align.template_for(160) + np.array([20.0, 20.0], dtype=np.float32)
        found = [
            Detection(box=(20.0, 20.0, 170.0, 170.0), landmarks=landmarks, score=0.99)
            for _ in range(self.faces)
        ]
        return found


def _corpus(root: Path, *, identities: int = 3, captures: int = 3, size: int = 220) -> Path:
    """A corpus of noise images, one folder per identity - the folder contract, exercised."""
    import cv2

    for identity in range(identities):
        folder = root / f"person_{identity}"
        folder.mkdir(parents=True, exist_ok=True)
        for capture in range(captures):
            rng = np.random.default_rng(identity * 1000 + capture)
            image = rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8)
            assert cv2.imwrite(str(folder / f"{capture}.png"), image)
    return root


def _scorer(tmp_path: Path, **kwargs) -> ShadowScorer:
    enforced = kwargs.pop("enforced", None) or _FakeEncoder(128, salt="enforced")
    shadowed = kwargs.pop("shadow_encoder", None) or _FakeEncoder(512, salt="shadow")
    return ShadowScorer(enforced, shadowed, db_path=tmp_path / "shadow.sqlite", **kwargs)


# ---------------------------------------------------------------------------
# the gallery: never compare across widths, never hold a raw template
# ---------------------------------------------------------------------------
def test_a_gallery_refuses_a_probe_of_another_width():
    """The one mistake that silently accepts the wrong person: comparing 128-D with 512-D.

    Every convenient way of making the shapes agree - truncate, pad, project - produces a number
    that looks like a cosine and is not one, so it has to be an exception rather than a coercion.
    """
    gallery = _gallery(128, 1, 2)
    assert gallery.nearest(_FakeEncoder(128).embed(_signal(1, 0)))[0] == "1"
    with pytest.raises(shadow.DimensionMismatch) as caught:
        gallery.nearest(_FakeEncoder(512).embed(_signal(1, 0)))
    assert "128" in str(caught.value) and "512" in str(caught.value)

    with pytest.raises(shadow.DimensionMismatch):
        gallery.add(3, _unit(np.ones(512)))
    with pytest.raises(shadow.DimensionMismatch):
        gallery.add_normalized(3, np.ones(512))


def test_an_un_normalized_template_cannot_be_stored_silently():
    """A raw FaceNet output has norm ~5.78; compared with a unit probe it reads as distance 0.83.

    That is every worker refused, no exception, and no symptom but a queue that stays empty - so
    the strict door raises and the forgiving door normalizes and says so.
    """
    gallery = _gallery(128, 1)
    raw = _FakeEncoder(128).embed(_signal(2, 0)) * 5.7762
    with pytest.raises(shadow.EmbeddingError) as caught:
        gallery.add(2, raw)
    assert "norm" in str(caught.value)

    gallery.add_normalized(2, raw)
    stored = gallery.templates["2"]
    assert float(np.linalg.norm(stored)) == pytest.approx(1.0, abs=1e-6)
    probe = _FakeEncoder(128).embed(_signal(2, 1))
    assert gallery.nearest(probe)[1] == pytest.approx(
        float(1.0 - _unit(raw) @ _unit(probe)), abs=1e-6
    )
    with pytest.raises(shadow.EmbeddingError):
        gallery.add_normalized(9, np.zeros(128))


def test_the_shadow_must_be_a_different_graph_from_the_enforced_one(tmp_path):
    """A copy-pasted model path makes a shadow that always agrees, and a gate that always passes."""
    with pytest.raises(ShadowError) as caught:
        _scorer(tmp_path, enforced=_FakeEncoder(128), shadow_encoder=_FakeEncoder(128))
    assert "same width" in str(caught.value)

    with pytest.raises(ShadowError):
        _scorer(
            tmp_path,
            enforced_gallery=Gallery(width=512, band=None, name="mismatched"),
        )


def test_coverage_counts_templates_against_the_workforce():
    assert coverage(_gallery(128, 1, 2), total_workers=4) == pytest.approx(0.5)
    assert coverage(_gallery(128), total_workers=4) == 0.0
    assert coverage(_gallery(128, 1), total_workers=0) == 0.0


# ---------------------------------------------------------------------------
# the shadow: it cannot change the product, and it cannot take the product down
# ---------------------------------------------------------------------------
def test_a_failing_shadow_cannot_change_the_enforced_answer_or_raise(tmp_path):
    """The whole reason the shadow is instrument-only: a migration aid must not fail a punch."""
    enforced = _FakeEncoder(128)
    scorer = _scorer(
        tmp_path,
        enforced=enforced,
        shadow_encoder=_FakeEncoder(512, fail=True),
        enforced_gallery=_gallery(128, 1),
        shadow_gallery=_gallery(512, 1),
    )
    embedding, verdict, shadow_distance = scorer.score(_signal(1, 0), worker_id="1")
    assert verdict == "approved"
    assert shadow_distance is None
    assert float(np.linalg.norm(embedding)) == pytest.approx(1.0, abs=1e-6)

    rows = scorer.summary()
    assert rows["events"] == 1
    assert rows["errors"] == 1
    import sqlite3

    with sqlite3.connect(scorer._db_path) as conn:  # noqa: SLF001
        outcome, error, enforced_dist = conn.execute(
            "SELECT outcome, error, enforced_dist FROM shadow_scores"
        ).fetchone()
    assert outcome == "shadow_error"
    assert "simulated execution-provider failure" in error
    assert enforced_dist is not None  # the enforced decision was still made and recorded


def test_a_healthy_shadow_records_both_distances_on_one_row(tmp_path):
    scorer = _scorer(
        tmp_path,
        enforced_gallery=_gallery(128, 1, 2),
        shadow_gallery=_gallery(512, 1, 2),
    )
    _, verdict, shadow_distance = scorer.score(_signal(1, 0), worker_id="1")
    assert verdict == "approved"
    assert shadow_distance is not None
    summary = scorer.summary()
    assert (summary["events"], summary["ok"], summary["errors"]) == (1, 1, 0)
    assert summary["enforced_ms_mean"] >= 0.0 and summary["shadow_ms_mean"] >= 0.0


def test_sampling_thins_the_shadow_and_never_the_enforced_pass(tmp_path):
    """Sampling is the escape hatch for peak load, and it must thin only the instrument."""
    enforced = _FakeEncoder(128)
    scorer = _scorer(
        tmp_path,
        enforced=enforced,
        enforced_gallery=_gallery(128, 1),
        shadow_gallery=_gallery(512, 1),
        sample_every=3,
    )
    for _ in range(6):
        scorer.score(_signal(1, 0))
    summary = scorer.summary()
    assert summary["events"] == 6
    assert (summary["ok"], summary["skipped"]) == (2, 4)
    assert enforced.calls == 6, "the enforced encoder must run on every request"

    with pytest.raises(ShadowError):
        _scorer(tmp_path, sample_every=0)


def test_disabling_the_shadow_leaves_the_scorer_usable(tmp_path):
    scorer = _scorer(
        tmp_path, enforced_gallery=_gallery(128, 1), shadow_gallery=_gallery(512, 1), enabled=False
    )
    _, verdict, shadow_distance = scorer.score(_signal(1, 0))
    assert (verdict, shadow_distance) == ("approved", None)
    assert scorer.summary()["skipped"] == 1


def test_a_failed_write_is_queued_and_flushed_by_the_next_one(tmp_path):
    """An instrument that loses its own evidence when a disk hiccups is worse than none."""
    scorer = _scorer(
        tmp_path, enforced_gallery=_gallery(128, 1), shadow_gallery=_gallery(512, 1)
    )
    real_connect = shadow.sqlite3.connect

    class _Boom:
        def __enter__(self) -> "_Boom":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def execute(self, *args: object, **kwargs: object) -> None:
            raise shadow.sqlite3.Error("disk full")

        def executemany(self, *args: object, **kwargs: object) -> None:
            raise shadow.sqlite3.Error("disk full")

    state = {"fail": True}

    def flaky(path: object, *args: object, **kwargs: object):
        return _Boom() if state["fail"] else real_connect(path, *args, **kwargs)

    scorer.score(_signal(1, 0))  # queued: the write fails
    state["fail"] = False
    scorer.score(_signal(1, 0))  # flushes the backlog in the same transaction

    import sqlite3

    with sqlite3.connect(scorer._db_path) as conn:  # noqa: SLF001
        count = conn.execute("SELECT COUNT(*) FROM shadow_scores").fetchone()[0]
    assert count == 2, "the row lost to the failed write must come back with the next one"


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def test_the_gate_refuses_cutover_for_every_reason_it_has():
    healthy = {"paired_samples": 5000, "agreement": 0.93, "shadow_more_permissive": 0.0}
    summary = {"error_rate": 0.0}
    ready, reason = flip_ready(coverage_fraction=1.0, paired=healthy, summary=summary)
    assert ready and "clear to cut over" in reason

    ready, reason = flip_ready(coverage_fraction=0.42, paired=healthy, summary=summary)
    assert not ready and "42.0%" in reason

    ready, reason = flip_ready(
        coverage_fraction=1.0, paired={**healthy, "paired_samples": 300}, summary=summary
    )
    assert not ready and "300 paired samples" in reason

    ready, reason = flip_ready(
        coverage_fraction=1.0, paired=healthy, summary={"error_rate": 0.4}
    )
    assert not ready and "error rate" in reason

    ready, reason = flip_ready(
        coverage_fraction=1.0, paired={**healthy, "shadow_more_permissive": 0.02}, summary=summary
    )
    assert not ready and "weakening" in reason

    ready, reason = flip_ready(
        coverage_fraction=1.0, paired={**healthy, "agreement": 1.0}, summary=summary
    )
    assert not ready and "different graph" in reason


def test_paired_confusion_ignores_rows_the_shadow_could_not_score(tmp_path):
    """Errors must not be counted as agreement - that is how a broken session passes its own gate.

    Two poisoned rows, because two different filters carry the claim. The realistic one has no
    shadow distance (the forward pass failed); the second carries distances *and* an error outcome,
    which is the row the outcome filter exists for - it recorded two numbers and did not complete a
    comparison, and counting it as agreement is exactly how a broken session passes its own gate.
    """
    scorer = _scorer(
        tmp_path,
        enforced_gallery=_gallery(128, 1, 2),
        shadow_gallery=_gallery(512, 1, 2),
    )
    for _ in range(4):
        scorer.score(_signal(1, 0))
    import sqlite3

    with sqlite3.connect(scorer._db_path) as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO shadow_scores (created_at, enforced_model, enforced_width, "
            "shadow_model, shadow_width, outcome, error, enforced_ms, shadow_ms, total_ms) "
            "VALUES ('2026-09-22 00:00:00', 'e', 128, 's', 512, 'shadow_error', 'boom', 0, 0, 0)"
        )
        conn.execute(
            "INSERT INTO shadow_scores (created_at, enforced_model, enforced_width, "
            "enforced_dist, enforced_verdict, shadow_model, shadow_width, shadow_dist, "
            "shadow_verdict, outcome, error, enforced_ms, shadow_ms, total_ms) "
            "VALUES ('2026-09-22 00:00:01', 'e', 128, 0.20, 'approved', 's', 512, 0.20, "
            "'approved', 'shadow_error', 'band unavailable', 0, 0, 0)"
        )
        conn.commit()
    paired = paired_confusion(
        scorer._db_path, enforced_threshold=0.9, shadow_threshold=0.9
    )
    assert paired["paired_samples"] == 4, "an error row carries no comparison and must be excluded"
    assert paired["agreement"] == 1.0, "only the four real pairs may be compared"


def test_the_rollout_report_says_why_it_is_not_ready(tmp_path):
    """The report is what an operator acts on, so the reason has to be in it - not only in a log."""
    scorer = _scorer(
        tmp_path,
        enforced_gallery=_gallery(128, 1),
        shadow_gallery=_gallery(512, 1, 2),
    )
    for _ in range(3):
        scorer.score(_signal(1, 0))
    report = rollout_report(
        scorer,
        total_workers=8,
        rollout={"enforced_threshold": 0.9, "shadow_threshold": 0.9},
    )
    assert report["coverage"] == pytest.approx(2 / 8)
    assert report["flip_ready"] is False
    assert "covers" in report["reason"]
    assert json.loads(shadow.dump_report(report))["summary"]["events"] == 3


# ---------------------------------------------------------------------------
# the A/B diagnostics
# ---------------------------------------------------------------------------
def test_the_shift_diagnostic_separates_a_global_offset_from_a_per_identity_effect():
    """``floor/ceiling`` moves under a constant offset, so the offset must be measured, not assumed.

    A wrong contract is a global input transform: it moves every pair by about the same amount.
    If it instead moved some pairs and not others, the shift would be a poor summary and the rank
    agreement would show it - which is exactly the distinction this pins.
    """
    base = np.linspace(0.1, 0.9, 200)
    shifted = base + 0.31
    stats = contract_ab.shift_stats(base, shifted)
    assert stats["median"] == pytest.approx(0.31, abs=1e-9)
    assert stats["spread"] == pytest.approx(0.0, abs=1e-9)
    assert contract_ab.spearman(base, shifted) == pytest.approx(1.0)
    assert contract_ab.spearman(base, shifted[::-1]) == pytest.approx(-1.0)

    rng = np.random.default_rng(7)
    noisy = base + rng.normal(0.0, 0.25, size=base.size)
    assert contract_ab.spearman(base, noisy) < 0.9
    assert contract_ab.shift_stats(base, noisy)["spread"] > 0.5


def test_the_verdict_ranks_on_the_confidence_interval_not_the_point_estimate():
    """Two arms this close can swap places on the raw ratio; the interval is what may be gated on.

    Both arms *clear* the rule, which is the only way the two rankings can disagree: with one of
    them failing, the pass flag decides and the tie-break is never reached.
    """
    strong_point = _arm("strong point", ratio=2.40, low=1.90, ceiling=0.40, floor=0.96)
    tight_interval = _arm("tight interval", ratio=2.05, low=2.00, ceiling=0.40, floor=0.82)
    better, reason = contract_ab.verdict([strong_point, tight_interval], required_ratio=1.82)
    assert better is tight_interval, "ranking must not prefer a wide window with a low CI"
    assert "lower end" in reason
    assert tight_interval.ratio < strong_point.ratio  # the point estimate really is smaller


# ---------------------------------------------------------------------------
def test_the_scorer_works_against_the_real_128d_graph(tmp_path):
    """One real session, one real crop, one real 128-D gallery - and a 512-D shadow beside it.

    This is the cross-module contract the fake tests above cannot reach: the graph returns a raw
    vector of norm ~5.78, ``facenet_ort`` normalizes it host-side, and the 128-D gallery accepts it.
    A template stored at its raw norm would be refused by ``Gallery.add`` - which is the failure this
    pins, and the reason ``embed``'s output and a stored template have to be the same kind of object.
    """
    import facenet_ort

    model = Path(__file__).resolve().parent.parent / "models" / "facenet128.onnx"
    if not model.exists():  # pragma: no cover - the graph ships with the checkout
        pytest.skip("facenet128.onnx is not present")

    engine = facenet_ort.FaceNetORT(model, warmup=True)
    assert engine.width == 128
    crop = np.random.default_rng(3).integers(0, 256, size=(160, 160, 3), dtype=np.uint8)
    tensor = face_align.to_tensor(crop, face_align.CONTRACTS[0], size=engine.input_size)
    assert float(np.linalg.norm(engine.embed_raw(tensor))) > 2.0, (
        "the raw graph output is not unit-norm, so host-side normalisation is doing real work"
    )

    band = calibration.Band(
        approve=0.9, review=0.95, genuine_ceiling=0.2, impostor_floor=0.8,
        separation_ratio=4.0, review_window_collapsed=False,
    )
    scorer = ShadowScorer(
        engine,
        _FakeEncoder(512),
        db_path=tmp_path / "shadow.sqlite",
        enforced_gallery=Gallery(width=128, band=band, name="enforced"),
        shadow_gallery=Gallery(width=512, band=band, name="shadow"),
    )
    # The shadow side is pre-seeded (its encoder is a stand-in); the enforced side is not, which is
    # the state a rollout starts in - a gallery backfilled while the instrument already runs.
    scorer.shadow_gallery.add_normalized("7", _FakeEncoder(512).embed(tensor))
    embedding, verdict, shadow_distance = scorer.score(tensor, worker_id="7")
    assert embedding.size == 128
    assert float(np.linalg.norm(embedding)) == pytest.approx(1.0, abs=1e-5)
    assert verdict is None, "an empty gallery must produce no verdict rather than a default one"
    assert shadow_distance == pytest.approx(0.0, abs=1e-6), (
        "the shadow scored the same crop against its own template"
    )

    scorer.enforced_gallery.add_normalized("7", embedding)  # the enrollment path, on a real vector
    assert coverage(scorer.enforced_gallery, total_workers=1) == 1.0
    again, second_verdict, _ = scorer.score(tensor, worker_id="7")
    assert float(np.linalg.norm(again)) == pytest.approx(1.0, abs=1e-5)
    import sqlite3

    with sqlite3.connect(scorer._db_path) as conn:  # noqa: SLF001
        distance = conn.execute(
            "SELECT enforced_dist FROM shadow_scores ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    # The same crop, twice through the same graph against its own template: deterministic, and
    # distance 0 within float32 storage - which is what makes the verdict below a fact and not a
    # coincidence of the band's width.
    assert distance == pytest.approx(0.0, abs=1e-6)
    assert second_verdict == band.classify(distance) == "approved"


# ---------------------------------------------------------------------------
# the evidence layer both instruments read: calibration intervals
# ---------------------------------------------------------------------------
def test_a_duplicated_capture_is_not_an_impostor_pair():
    """The bootstrap draws *people* twice, and a capture drawn twice is one observation, not two.

    Comparing a vector with itself gives a distance of exactly 0, and that 0 landed in the
    *impostor* distribution of every round where a capture was drawn twice - so the floor interval's
    lower bound became the distance between a vector and itself, which is not an impostor
    measurement at all. Three tightly-clustered identities is the worst case for it and the case a
    real corpus approaches: one person's two captures are near-identical by construction.
    """
    rng = np.random.default_rng(11)
    embeddings: list[np.ndarray] = []
    labels: list[str] = []
    for identity in range(3):
        centre = rng.normal(size=64)
        for _ in range(3):
            embeddings.append(centre + 0.05 * rng.normal(size=64))
            labels.append(f"person_{identity}")
    intervals = calibration.cluster_bootstrap(np.stack(embeddings), labels, rounds=60, seed=3)
    assert intervals["floor"].low > 0.0, (
        "the impostor floor interval collapsed to zero: a capture is being compared with itself"
    )
    report = calibration.evaluate(np.stack(embeddings), labels, bootstrap=60, seed=3)
    # The published pair is rounded to four decimals, so it is asserted above the rounding too.
    assert report["floor_ci"][0] > 0.0
    assert report["raw"]["impostor_floor"] > report["raw"]["genuine_ceiling"]


def test_a_self_comparison_is_zero_and_never_negative():
    """``1 - cos`` on unit float64 vectors lands at -2.2e-16 for *this* vector, and that propagates.

    A negative floor under a geometric centre is ``math.sqrt`` of a negative number: the crash lands
    in the one function whose job is to *refuse* with a message, and it reports a domain error
    instead. The vector below is the one found by search whose normalized dot product with itself
    exceeds 1 - the premise is asserted, so the pin cannot silently go stale.
    """
    raw = np.array(
        [-0.7037352358069926, -1.2654214710460525, -0.6232744625373522, 0.0413259793472436,
         -2.3250307746388343, -0.21879166393254573, -1.2459109472530652, -0.7322673547034516]
    )
    unit = raw / np.linalg.norm(raw)
    assert float(unit @ unit) > 1.0, "the premise of this test no longer holds for this vector"
    other = np.zeros(8)
    other[7] = 1.0
    genuine, impostor = calibration.pair_distances(np.stack([unit, unit, other]), ["a", "a", "b"])
    assert genuine.min() == 0.0, "a self-comparison must be exactly 0, not -2.2e-16"
    assert genuine.min() >= 0.0 and impostor.min() >= 0.0

    rng = np.random.default_rng(5)
    for _ in range(20):
        embeddings = rng.normal(size=(12, 32))
        labels = [f"p{index % 4}" for index in range(12)]
        genuine, impostor = calibration.pair_distances(embeddings, labels)
        assert genuine.min() >= 0.0 and impostor.min() >= 0.0
        assert genuine.max() <= 2.0 and impostor.max() <= 2.0


def test_a_band_refuses_a_floor_that_leaves_no_room_rather_than_crashing():
    """No line separates two people who are indistinguishable: that is a refusal, not a traceback."""
    genuine = np.array([0.20, 0.30])
    with pytest.raises(calibration.CalibrationError) as caught:
        calibration.derive_band(genuine, np.array([0.0, 0.50]))
    assert "no line" in str(caught.value)
    with pytest.raises(calibration.CalibrationError):
        calibration.derive_band(genuine, np.array([-1e-16, 0.50]))
    with pytest.raises(calibration.CalibrationError):
        calibration.derive_band(genuine, np.array([0.10, 0.50]),
                                floor_ci=calibration.Interval(low=0.0, high=0.40))


def test_the_intervals_in_a_report_are_pairs_not_objects():
    """``dataclasses.asdict`` recurses, so an ``Interval`` became a dict and ``ci[0]`` a KeyError.

    The A/B tool reads ``ci[0]`` at the far end of a calibration run to rank the arms, which is
    exactly the wrong place to discover that the same band is ``[low, high]`` in one producer and
    ``{"low": ..}`` in another.
    """
    rng = np.random.default_rng(2)
    embeddings = rng.normal(size=(9, 48))
    labels = [f"p{index // 3}" for index in range(9)]
    report = calibration.evaluate(embeddings, labels, bootstrap=25)
    for key in ("ceiling_ci", "floor_ci", "ratio_ci"):
        interval = report[key]
        assert isinstance(interval, list) and len(interval) == 2, f"{key} was {interval!r}"
        assert interval[0] <= interval[1]
    band = calibration.derive_band(
        np.array([0.20, 0.30]), np.array([0.60, 0.80]),
        ceiling_ci=calibration.Interval(low=0.28, high=0.35),
        floor_ci=calibration.Interval(low=0.55, high=0.62),
    )
    published = band.as_dict()
    assert published["ceiling_ci"] == [0.28, 0.35]  # the interval as recorded, as a pair
    assert published["floor_ci"] == [0.55, 0.62]
    # ... and the *conservative* end of each is what the lines were placed on.
    assert band.genuine_ceiling == 0.35
    assert band.impostor_floor == 0.55


# ---------------------------------------------------------------------------
# the A/B end to end, on a real graph, with a stubbed detector
# ---------------------------------------------------------------------------
def test_all_four_contracts_are_scored_on_one_shared_crop_set(tmp_path, monkeypatch, capsys):
    """The controlled experiment itself: one crop per image, four tensor mappings, one graph.

    If an arm could re-detect or re-align, the four ratios would come from four different corpora
    and the ranking would be meaningless - so the shared count is asserted, not assumed.
    """
    import detector_640

    corpus = _corpus(tmp_path / "corpus")
    detector = _StubDetector()
    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: detector)

    model = Path(__file__).resolve().parent.parent / "models" / "facenet128.onnx"
    if not model.exists():  # pragma: no cover - the graph ships with the checkout
        pytest.skip("facenet128.onnx is not present")

    out = tmp_path / "ab.json"
    code = contract_ab.main(
        [
            "--corpus", str(corpus),
            "--model", str(model),
            "--detector-model", str(
                Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
            ),
            "--min-face-pixels", "160",
            "--min-impostor-pairs", "20",  # a 9-image fixture cannot support a 100-pair floor
            "--bootstrap", "25",
            "--json", str(out),
            "--quiet",
        ]
    )
    assert code in (0, 1), f"unexpected exit {code}: {capsys.readouterr()}"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["arms"]) == 4, "all four permutations must be measured"
    assert payload["corpus_stats"]["cropped"] == 9
    assert detector.calls == 9, "detection runs once per image, not once per arm"

    counts = {
        (arm["calibration"]["genuine_pairs"], arm["calibration"]["impostor_pairs"])
        for arm in payload["arms"]
    }
    assert len(counts) == 1, f"the arms were scored on different corpora: {counts}"

    tensors = {arm["contract"]: arm["tensor"] for arm in payload["arms"]}
    digests = {arm["contract"]: arm["tensor_sha256"] for arm in payload["arms"]}
    assert len(set(digests.values())) == 4, (
        "the four arms must feed the graph four different tensors, or the experiment varies nothing"
    )
    assert len({tuple(sorted(stats.items())) for stats in tensors.values()}) == 2, (
        "min/max/mean cannot see a channel swap: only the value range moves a summary statistic, "
        "which is why the digest above - not this - is the proof the arms differed"
    )
    assert all(
        arm["embedding_norms"]["min"] == pytest.approx(1.0, abs=1e-4)
        and arm["embedding_norms"]["max"] == pytest.approx(1.0, abs=1e-4)
        for arm in payload["arms"]
    ), "host-side L2 normalisation must be in force for every arm"
    assert payload["required_ratio"] == pytest.approx(1.82, abs=0.01)
    assert payload["verdict"]["contract"] in tensors


def test_the_comparison_refuses_a_corpus_it_cannot_support(tmp_path, monkeypatch, capsys):
    import detector_640

    model = Path(__file__).resolve().parent.parent / "models" / "facenet128.onnx"
    if not model.exists():  # pragma: no cover
        pytest.skip("facenet128.onnx is not present")
    detector_model = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
    common = ["--model", str(model), "--detector-model", str(detector_model), "--bootstrap", "10"]

    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: _StubDetector())
    code = contract_ab.main(
        ["--corpus", str(_corpus(tmp_path / "one", identities=1)), *common]
    )
    assert code == 2
    assert "at least two" in capsys.readouterr().err

    monkeypatch.setattr(
        detector_640, "build_detector", lambda *a, **k: _StubDetector(detect_nothing=True)
    )
    code = contract_ab.main(["--corpus", str(_corpus(tmp_path / "two")), *common])
    assert code == 2
    assert "nothing can be measured" in capsys.readouterr().err

    code = contract_ab.main(["--corpus", str(tmp_path / "absent"), *common])
    assert code == 2

    monkeypatch.setattr(detector_640, "build_detector", lambda *a, **k: _StubDetector())
    code = contract_ab.main(
        ["--corpus", str(_corpus(tmp_path / "thin")), "--min-impostor-pairs", "1000", *common]
    )
    assert code == 2, "a floor resting on 27 pairs must be refused, not printed"
    assert "impostor pairs" in capsys.readouterr().err
