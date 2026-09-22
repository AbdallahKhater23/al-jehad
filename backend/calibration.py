"""Derive a decision band, and state how much of it is evidence rather than wishful thinking.

THE RULE (identical to ``face_detector``'s, so a band derived here is comparable with the shipped one)
-----------------------------------------------------------------------------------------------------
    IMPOSTOR_MARGIN = 1.35          # a refusal at the easiest end of the impostors
    need  impostor_floor >= 1.35^2 x genuine_ceiling    (~1.82x)
    approve = sqrt(genuine_ceiling * impostor_floor)    # geometric centre of the measured window
    review  = impostor_floor / IMPOSTOR_MARGIN          # clamped up to approve

Below 1.82x the review line lands *under* the approve line, which is not a tier at all - so it is
clamped and the collapse is **reported** (``review_window_collapsed``) instead of hidden behind a
band that reads two-tier and behaves one-tier.

WHY THE UNCERTAINTY IS PART OF THE RULE, NOT A FOOTNOTE
-------------------------------------------------------
``genuine_ceiling`` is a **maximum** over the sampled genuine pairs and ``impostor_floor`` a
**minimum** over the sampled impostor pairs. Both are extreme statistics: the floor's sample minimum
is an estimate of the lower tail taken from *above*, i.e. it makes the separation look better than it
is. With 196 impostor pairs, the observed minimum sits at roughly the 0.5th percentile - and a band
that consumes it as if it were the population's minimum is optimistic in the direction that admits
impostors.

So a band is derived from the **conservative ends of a cluster bootstrap** (the low end of the floor's
95 % interval, the high end of the ceiling's), and the bootstrap resamples *identities*, not pairs.
Resampling pairs treats 196 impostor pairs as 196 independent observations when they are drawn from
10 identities - which understates the interval width, sometimes by a factor of three.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np

log = logging.getLogger("attendance.calibration")

#: The margin a band rule uses. Kept as one name so a band's provenance is readable.
IMPOSTOR_MARGIN: Final = 1.35
#: What the ratio must reach for a two-tier band to be *possible*: 1.8225.
MARGIN_SQUARED: Final = IMPOSTOR_MARGIN**2
#: Rounding of the published lines. Four decimals is the stored precision of the distances.
DECIMALS: Final = 4
#: Pair-computation budget for the bootstrap (rounds x n^2). Keeps a large corpus from turning a
#: calibration run into an afternoon, with the reduction logged rather than silent.
_BOOTSTRAP_BUDGET: Final = 20_000_000

#: Classification labels, spelled once.
APPROVED: Final = "approved"
REVIEW: Final = "review"
REFUSED: Final = "refused"


class CalibrationError(RuntimeError):
    """Too little evidence, or inconsistent evidence, to derive a band."""


@dataclass(frozen=True)
class Interval:
    low: float
    high: float
    level: float = 0.95

    def as_list(self) -> list[float]:
        return [round(self.low, DECIMALS), round(self.high, DECIMALS)]


@dataclass(frozen=True)
class Band:
    """The two lines, plus the evidence they were derived from and its uncertainty."""

    approve: float
    review: float
    genuine_ceiling: float
    impostor_floor: float
    separation_ratio: float
    review_window_collapsed: bool
    ceiling_ci: Interval | None = None
    floor_ci: Interval | None = None
    ratio_ci: Interval | None = None
    genuine_pairs: int = 0
    impostor_pairs: int = 0
    identities: int = 0

    def classify(self, distance: float) -> str:
        if distance <= self.approve:
            return APPROVED
        if distance <= self.review:
            return REVIEW
        return REFUSED

    def as_dict(self) -> dict[str, Any]:
        """The band as plain data, with the three intervals as ``[low, high]`` - always.

        Built from the fields rather than ``dataclasses.asdict``, which **recurses into nested
        dataclasses**: every ``Interval`` would come out as ``{"low": .., "high": .., "level": ..}``,
        so the ``isinstance`` check below could never fire and the same band would be an object here
        and a pair of brackets in ``registry_entry``. A consumer reading ``ci[0]`` then dies with
        ``KeyError: 0`` at the far end of a calibration run - which is how this was found.
        """
        payload: dict[str, Any] = {
            name: getattr(self, name) for name in self.__dataclass_fields__
        }
        for key in ("ceiling_ci", "floor_ci", "ratio_ci"):
            value = payload.get(key)
            payload[key] = value.as_list() if isinstance(value, Interval) else None
        return payload

    def registry_entry(self, *, pipeline: str, model_id: str, contract_id: str) -> dict[str, Any]:
        """What a band table (or a JSON registry) stores: the lines *and* how they were derived.

        The provenance is not decoration - it is the answer to "which of these numbers may I move,
        and to what" six months from now.
        """
        return {
            "pipeline": pipeline,
            "model_id": model_id,
            "contract": contract_id,
            "approve": self.approve,
            "review": self.review,
            "genuine_ceiling": self.genuine_ceiling,
            "impostor_floor": self.impostor_floor,
            "separation_ratio": round(self.separation_ratio, 4),
            "required_ratio": round(MARGIN_SQUARED, 4),
            "review_window_collapsed": self.review_window_collapsed,
            "impostor_margin": IMPOSTOR_MARGIN,
            "pairs": {"genuine": self.genuine_pairs, "impostor": self.impostor_pairs,
                      "identities": self.identities},
            "ci": {
                "level": 0.95,
                "ceiling": self.ceiling_ci.as_list() if self.ceiling_ci else None,
                "floor": self.floor_ci.as_list() if self.floor_ci else None,
                "ratio": self.ratio_ci.as_list() if self.ratio_ci else None,
            },
        }


# ---------------------------------------------------------------------------
# pair statistics
# ---------------------------------------------------------------------------
def _unit(embeddings: np.ndarray) -> np.ndarray:
    matrix = np.asarray(embeddings, dtype=np.float64)
    if matrix.ndim != 2:
        raise CalibrationError(f"expected an (N, D) matrix, got {matrix.shape}")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 0.0):
        raise CalibrationError("a zero-norm embedding reached calibration")
    return matrix / norms


def pair_distances(
    embeddings: np.ndarray, labels: Sequence[str]
) -> tuple[np.ndarray, np.ndarray]:
    """``(genuine, impostor)`` cosine distances over every pair. ``1 - cos`` on unit vectors.

    Mathematically identical to ``scipy.spatial.distance.cosine`` - which is the metric the running
    deployment uses - and computed as one Gram matrix rather than a Python loop, because a corpus
    large enough to estimate a floor is also large enough that a loop hurts.
    """
    unit = _unit(embeddings)
    ids = np.asarray([str(label) for label in labels])
    if ids.size != unit.shape[0]:
        raise CalibrationError(f"{ids.size} labels for {unit.shape[0]} embeddings")
    if len(set(ids.tolist())) < 2:
        raise CalibrationError("need at least two identities to have an impostor distribution")
    gram = unit @ unit.T
    # Clamped, because ``1 - cos`` is only a distance where ``cos`` is in [-1, 1] and a dot product
    # of unit float64 vectors can land at 1 + 1e-16. The resulting -1e-16 is not a small distance,
    # it is not a distance at all: it goes on to make a *negative* impostor floor, and a negative
    # floor under a geometric centre is ``math.sqrt`` of a negative number - a crash in the one
    # function whose job is to refuse, with a message about the corpus, rather than raise.
    distance = np.clip(1.0 - gram, 0.0, 2.0)
    iu = np.triu_indices(unit.shape[0], k=1)
    same = ids[iu[0]] == ids[iu[1]]
    genuine, impostor = distance[iu][same], distance[iu][~same]
    if genuine.size == 0:
        raise CalibrationError("no genuine pairs: every identity has a single image")
    if impostor.size == 0:
        raise CalibrationError("no impostor pairs")
    return genuine, impostor


def _clusters(labels: Sequence[str]) -> dict[str, list[int]]:
    by_identity: dict[str, list[int]] = {}
    for index, label in enumerate(labels):
        by_identity.setdefault(str(label), []).append(index)
    return by_identity


def cluster_bootstrap(
    embeddings: np.ndarray,
    labels: Sequence[str],
    *,
    rounds: int = 400,
    seed: int = 20260922,
    quantiles: tuple[float, float] = (2.5, 97.5),
) -> dict[str, Interval]:
    """Interval estimates for the ceiling, the floor and the ratio, resampling **identities**.

    Cluster resampling, stated: the unit of resampling is the *person*, not the pair, because a
    person contributes many correlated pairs. When an identity is drawn k times, its k copies are
    given k distinct labels - so a duplicated cluster contributes genuine pairs *within* each copy
    and impostor pairs *between* copies, which is the standard bootstrap for pair statistics and
    the reason this function rebuilds the pair table per round instead of resampling distances.

    **A duplicated capture is one observation drawn twice, not two.** Drawing a person twice puts
    the *same* capture into two copies, and the pair ``(capture i of copy 0, capture i of copy 1)``
    is that vector compared with itself: distance exactly 0, in the impostor distribution. Left in,
    it drives the floor of every such round to 0, and the floor interval's lower bound becomes the
    distance between a vector and itself - a number that is not an impostor measurement at all.
    Masking pairs whose underlying image index coincides keeps the between-copy content (capture i
    against capture j, i != j) and removes the degeneracy.

    Cost is ``rounds x n^2``; on a corpus large enough for that to matter the round count is
    reduced and the reduction is logged, because a silently truncated bootstrap is a confidence
    interval that lies about its own width.
    """
    unit = _unit(embeddings)
    ids = [str(label) for label in labels]
    by_identity = _clusters(ids)
    n = unit.shape[0]
    budget_rounds = max(30, int(_BOOTSTRAP_BUDGET / max(1, n * n)))
    effective = min(int(rounds), budget_rounds)
    if effective < rounds:
        log.warning(
            "cluster bootstrap reduced from %d to %d rounds for %d images (pair budget)",
            rounds, effective, n,
        )
    rng = np.random.default_rng(seed)
    identities = sorted(by_identity)
    ratios: list[float] = []
    ceilings: list[float] = []
    floors: list[float] = []
    for _ in range(effective):
        drawn = rng.integers(0, len(identities), size=len(identities))
        index_list: list[int] = []
        drawn_labels: list[str] = []
        for copy, position in enumerate(drawn.tolist()):
            identity = identities[position]
            for index in by_identity[identity]:
                index_list.append(index)
                drawn_labels.append(f"{identity}#{copy}")
        subset = unit[index_list]
        gram = subset @ subset.T
        distance = np.clip(1.0 - gram, 0.0, 2.0)
        labels_arr = np.asarray(drawn_labels)
        origin = np.asarray(index_list)
        iu = np.triu_indices(len(index_list), k=1)
        # Two *observations*, never one observation twice - see the docstring on duplicated draws.
        distinct = origin[iu[0]] != origin[iu[1]]
        same = labels_arr[iu[0]] == labels_arr[iu[1]]
        genuine, impostor = distance[iu][same & distinct], distance[iu][~same & distinct]
        if genuine.size == 0 or impostor.size == 0:
            continue
        ceiling, floor = float(genuine.max()), float(impostor.min())
        ceilings.append(ceiling)
        floors.append(floor)
        if ceiling > 0.0:
            ratios.append(floor / ceiling)
    if not ceilings:
        raise CalibrationError("cluster bootstrap produced no usable resamples")
    low, high = quantiles
    return {
        "ceiling": Interval(float(np.percentile(ceilings, low)), float(np.percentile(ceilings, high))),
        "floor": Interval(float(np.percentile(floors, low)), float(np.percentile(floors, high))),
        "ratio": Interval(float(np.percentile(ratios, low)), float(np.percentile(ratios, high))),
    }


def operating_curve(genuine: np.ndarray, impostor: np.ndarray) -> dict[str, float | None]:
    """EER and TAR at fixed FAR, computed on the same samples that produced the band.

    The band is a *policy*; this is the *capability*, and reporting capability without the policy
    (or the reverse) is how a system acquires a threshold nobody can defend. FAR is measured as
    ``P(impostor <= t)`` and TAR as ``P(genuine <= t)`` - both monotone in ``t`` for a distance.
    """
    g = np.asarray(genuine, dtype=np.float64)
    i = np.asarray(impostor, dtype=np.float64)
    thresholds = np.unique(np.concatenate([g, i]))
    if thresholds.size == 0:
        return {"eer": None}
    far = np.array([float(np.mean(i <= t)) for t in thresholds])
    fnr = np.array([float(np.mean(g > t)) for t in thresholds])
    gap = np.abs(far - fnr)
    best = int(np.argmin(gap))
    out: dict[str, float | None] = {
        "eer": round(float((far[best] + fnr[best]) / 2.0), 4),
        "eer_threshold": round(float(thresholds[best]), DECIMALS),
    }
    tar = 1.0 - fnr
    for target in (1e-2, 1e-3, 1e-4):
        feasible = far <= target
        out[f"tar_at_far_{target:g}"] = round(float(tar[feasible].max()), 4) if feasible.any() else None
    return out


def min_pair_count_for_floor(quantile: float = 0.005) -> int:
    """How many impostor pairs are needed for the sample minimum to sit near ``quantile``.

    ``P(min <= q) = 1 - (1-q)^n``; solving for n at a 0.5 % tail gives ~1 000 pairs. The live
    corpus has 196, which is why the band must consume the bootstrap's lower bound instead of the
    raw minimum: at 196 pairs the observed floor is an estimate of ~the 0.5th percentile *from
    above*, and treating it as a fact is a 1-in-200 claim made from 196 samples.
    """
    if not 0.0 < quantile < 1.0:
        raise CalibrationError(f"quantile must be in (0, 1), got {quantile}")
    return int(math.ceil(math.log(1.0 - 0.5) / math.log(1.0 - quantile)))


# ---------------------------------------------------------------------------
# the band
# ---------------------------------------------------------------------------
def derive_band(
    genuine: np.ndarray,
    impostor: np.ndarray,
    *,
    ceiling_ci: Interval | None = None,
    floor_ci: Interval | None = None,
    ratio_ci: Interval | None = None,
    identities: int = 0,
) -> Band:
    """The two lines from the evidence, using the conservative end of each interval.

    ``approve`` is the geometric centre of the window (the midpoint in *log* distance, which is the
    scale on which a multiplicative margin is meaningful), and ``review`` sits ``IMPOSTOR_MARGIN``
    below the impostor floor. With intervals supplied, the ceiling is taken at its upper end and the
    floor at its lower end - both directions that make the band stricter, never more permissive.
    """
    g = np.asarray(genuine, dtype=np.float64)
    i = np.asarray(impostor, dtype=np.float64)
    if g.size == 0 or i.size == 0:
        raise CalibrationError("derive_band needs both distributions")
    ceiling = float(g.max())
    floor = float(i.min())
    if ceiling_ci is not None:
        ceiling = max(ceiling, float(ceiling_ci.high))
    if floor_ci is not None:
        floor = min(floor, float(floor_ci.low))
    if floor <= 0.0:
        # Two different people at distance zero is not a narrow window, it is no separation at all:
        # no approve line can sit below the impostor floor and above the genuine ceiling, so the
        # corpus (or the crop) cannot support a band and saying so is the honest output.
        raise CalibrationError(
            f"impostor floor is {floor:.4f} after the interval, so no line can separate the "
            "distributions. Two captures of different people are indistinguishable under this "
            "crop and model: re-crop the corpus, or check the preprocessing contract."
        )
    centre = math.sqrt(ceiling * floor) if ceiling > 0.0 else floor
    approve = round(centre, DECIMALS)
    review = max(round(floor / IMPOSTOR_MARGIN, DECIMALS), approve)
    return Band(
        approve=approve,
        review=review,
        genuine_ceiling=round(ceiling, DECIMALS),
        impostor_floor=round(floor, DECIMALS),
        separation_ratio=round(floor / ceiling, DECIMALS) if ceiling > 0.0 else math.inf,
        review_window_collapsed=bool(review <= approve),
        ceiling_ci=ceiling_ci,
        floor_ci=floor_ci,
        ratio_ci=ratio_ci,
        genuine_pairs=int(g.size),
        impostor_pairs=int(i.size),
        identities=int(identities),
    )


def evaluate(
    embeddings: np.ndarray,
    labels: Sequence[str],
    *,
    bootstrap: int = 400,
    seed: int = 20260922,
) -> dict[str, Any]:
    """The whole calibration report for one (graph, contract, corpus) triple.

    One entry point so every consumer - the A/B tool, the rollout gate, the triage surface - reports
    the same quantities computed the same way. A second implementation of "the ratio" is a second
    number the organisation will argue about.
    """
    genuine, impostor = pair_distances(embeddings, labels)
    intervals = cluster_bootstrap(embeddings, labels, rounds=bootstrap, seed=seed)
    band = derive_band(
        genuine,
        impostor,
        ceiling_ci=intervals["ceiling"],
        floor_ci=intervals["floor"],
        ratio_ci=intervals["ratio"],
        identities=len(set(str(label) for label in labels)),
    )
    raw_ratio = float(impostor.min() / genuine.max()) if genuine.max() > 0 else math.inf
    payload = band.as_dict()
    payload["raw"] = {
        "genuine_ceiling": round(float(genuine.max()), DECIMALS),
        "impostor_floor": round(float(impostor.min()), DECIMALS),
        "separation_ratio": round(raw_ratio, DECIMALS),
    }
    payload["operating"] = operating_curve(genuine, impostor)
    payload["minimum_impostor_pairs_for_tail"] = min_pair_count_for_floor()
    payload["passes_margin"] = bool(raw_ratio >= MARGIN_SQUARED)
    payload["passes_margin_on_ci"] = bool(
        intervals["ratio"].low >= MARGIN_SQUARED if band.ratio_ci else False
    )
    return payload


def require_two_tier(record: dict[str, Any]) -> None:
    """The rollout gate: a two-tier band must be justified, a collapsed one must be *declared*.

    Both directions matter. A collapsed band with a satisfying raw ratio is an inconsistency worth
    an exception; a collapsed band with a poor ratio is a legitimate, honest configuration - and the
    deployment's current one-line band is exactly that, so refusing it outright would be refusing
    the truth.
    """
    if record.get("review_window_collapsed") and record.get("passes_margin"):
        raise CalibrationError(
            "band collapsed although the raw ratio satisfies the margin rule - derivation inconsistent"
        )
    if not record.get("passes_margin_on_ci"):
        log.warning(
            "no two-tier band: ratio %.3fx raw, %.3fx at the 2.5th percentile (need %.2fx)",
            float(record["raw"]["separation_ratio"]),
            float(record["ratio_ci"][0]) if record.get("ratio_ci") else float("nan"),
            MARGIN_SQUARED,
        )


def write_registry(path: str | Path, entries: Sequence[dict[str, Any]]) -> Path:
    """Write/merge a band registry keyed by ``pipeline|model_id|contract``.

    Merging rather than overwriting: a migration keeps the incumbent's band on disk so a rollback
    restores the decisions it made, not a recomputed approximation of them.
    """
    target = Path(path)
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"existing registry {target} is not valid JSON: {exc}") from exc
    for entry in entries:
        key = f"{entry.get('pipeline')}|{entry.get('model_id')}|{entry.get('contract')}"
        existing[key] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    return target


def load_registry(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))
