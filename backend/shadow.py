"""Shadow scoring: both encoders on the identical crop, one enforced decision, paired evidence.

THE THREE INVARIANTS, IN ORDER OF IMPORTANCE
--------------------------------------------
1. **The shadow can never change the enforced answer.** Every failure in the shadow path is caught,
   stamped as ``shadow_error`` and dropped. A migration instrument that can fail a punch is worse
   than no instrument: the incumbent path's behaviour must be bit-identical with the shadow enabled
   and disabled, and that is a property worth a test rather than a promise.
2. **Never compare across widths.** ``1 - cos`` between a 128-D template and a 512-D probe is
   undefined, and every convenient way of making the shapes agree (truncate, pad, PCA-project) is a
   quiet way to accept the wrong person. ``Gallery`` carries its width and both ``nearest`` and
   ``cosine_distance`` raise ``DimensionMismatch``.
3. **Coverage is measured, not assumed.** The cutover gate is a property of the *gallery* plus a
   *paired* ROC on live traffic - never elapsed time, and never a corpus substitute. Paired means the
   same probe decided twice, which is the only way to compare two encoders without confounding them
   with two different populations.

WHAT THE PAIRED LOG BUYS
------------------------
One row per scoring event: both distances, both verdicts, the outcome, the latency. From that table
the gate question ("is the shadow strictly better on the same traffic?") is a GROUP BY, and after a
rollback the same table answers "what did the old band decide in the hours it was live".
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import numpy as np

from facenet_ort import DimensionMismatch, EmbeddingError, FaceNetORT, cosine_distance

log = logging.getLogger("attendance.shadow")

#: The paired log. Deliberately a *separate* table from attendance: it is migration instrumentation
#: with a defined end (the cutover), and it must be droppable without touching a single shift.
SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS shadow_scores (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    site              TEXT,
    worker_id         TEXT,
    enforced_model    TEXT    NOT NULL,
    enforced_width    INTEGER NOT NULL,
    enforced_contract TEXT,
    enforced_dist     REAL,
    enforced_verdict  TEXT,
    shadow_model      TEXT    NOT NULL,
    shadow_width      INTEGER NOT NULL,
    shadow_contract   TEXT,
    shadow_dist       REAL,
    shadow_verdict    TEXT,
    outcome           TEXT    NOT NULL,
    error             TEXT,
    enforced_ms       REAL,
    shadow_ms         REAL,
    total_ms          REAL
);
CREATE INDEX IF NOT EXISTS idx_shadow_created ON shadow_scores(created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome ON shadow_scores(outcome);
"""

OUTCOME_OK: Final = "shadow_ok"
OUTCOME_ERROR: Final = "shadow_error"
OUTCOME_SKIPPED: Final = "shadow_skipped"


class ShadowError(RuntimeError):
    """A configuration error in the shadow pair - not a per-request failure."""


@dataclass
class ShadowRecord:
    """One scoring event, as it is logged. Field order matches the table."""

    created_at: str
    site: str | None
    worker_id: str | None
    enforced_model: str
    enforced_width: int
    enforced_contract: str | None
    enforced_dist: float | None
    enforced_verdict: str | None
    shadow_model: str
    shadow_width: int
    shadow_contract: str | None
    shadow_dist: float | None
    shadow_verdict: str | None
    outcome: str
    error: str | None
    enforced_ms: float
    shadow_ms: float
    total_ms: float

    def as_tuple(self) -> tuple[Any, ...]:
        return tuple(asdict(self).values())


@dataclass
class Gallery:
    """Templates of **one** width, with the band that was calibrated for that width.

    ``band`` is typed loosely (anything with ``classify``) so this module does not depend on the
    calibration module's internals - but the *width* check is hard, because that is the invariant
    that prevents an accidental cross-encoder comparison.
    """

    width: int
    band: Any
    name: str = "gallery"
    templates: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.templates)

    def add(self, worker_id: str, embedding: np.ndarray) -> None:
        """Store a template that is already unit length. Nothing else gets in.

        The unit-norm check is the whole reason this method is strict: ``nearest`` computes a dot
        product, which *is* the cosine only for unit vectors. A template stored raw (FaceNet's output
        has norm ~5.78) and matched against a unit probe yields a cosine of ~0.17 - a distance of
        0.83, i.e. every worker refused - with no error anywhere and no symptom but a quiet
        collapse. ``add_normalized`` is the forgiving door for a legacy gallery loaded from disk.
        """
        vector = np.asarray(embedding, dtype=np.float64).reshape(-1)
        if vector.size != self.width:
            raise DimensionMismatch(
                f"refusing to add a {vector.size}-D template to the {self.width}-D {self.name}"
            )
        norm = float(np.linalg.norm(vector))
        if abs(norm - 1.0) > 1e-6:
            raise EmbeddingError(
                f"template for {worker_id} has norm {norm:.4f}, not 1.0: an un-normalized template "
                "cannot be compared with a normalized probe. Use add_normalized() to store it."
            )
        self.templates[str(worker_id)] = vector.astype(np.float32)

    def add_normalized(self, worker_id: str, embedding: np.ndarray) -> None:
        """Store a vector of any norm, normalizing it first and saying so in the log."""
        vector = np.asarray(embedding, dtype=np.float64).reshape(-1)
        if vector.size != self.width:
            raise DimensionMismatch(
                f"refusing to add a {vector.size}-D template to the {self.width}-D {self.name}"
            )
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            raise EmbeddingError(f"cannot store a zero-norm template for {worker_id}")
        if abs(norm - 1.0) > 1e-6:
            log.warning("template for %s had norm %.4f; normalized before storage", worker_id, norm)
            vector = vector / norm
        self.templates[str(worker_id)] = vector.astype(np.float32)

    def nearest(self, embedding: np.ndarray) -> tuple[str, float] | None:
        """``(worker_id, 1 - cos)`` for the closest template, or ``None`` for an empty gallery.

        A dot product rather than a loop of ``cosine_distance`` calls: both sides are unit vectors,
        so ``a . b`` *is* the cosine, and the difference between that and a de-normalized
        implementation is precisely the defect this module guards against.
        """
        vector = np.asarray(embedding, dtype=np.float64).reshape(-1)
        if vector.size != self.width:
            raise DimensionMismatch(
                f"probe is {vector.size}-D, the {self.name} is {self.width}-D"
            )
        if not self.templates:
            return None
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            raise EmbeddingError("zero-norm probe cannot be matched")
        unit = vector / norm
        best_id: str | None = None
        best_cos = -1.0
        for worker_id, template in self.templates.items():
            score = float(unit @ template)
            if score > best_cos:
                best_id, best_cos = worker_id, score
        assert best_id is not None
        return best_id, float(1.0 - best_cos)

    def verdict(self, distance: float) -> str:
        return str(self.band.classify(distance))


def coverage(gallery: Gallery, *, total_workers: int) -> float:
    """Fraction of the workforce with a template in this gallery. The cutover's first gate."""
    if total_workers <= 0:
        return 0.0
    return min(1.0, gallery.size / float(total_workers))


class ShadowScorer:
    """The enforced encoder, plus a shadow encoder fed the *same* crop.

    ``min_width_delta`` is not configurable in practice: the two encoders must differ in width or the
    shadow adds a redundant forward pass that proves nothing. It is enforced at construction rather
    than documented, because a copy-paste of the same model path is the likeliest operator error and
    it fails silently (the shadow would simply agree, always, and the gate would pass).
    """

    def __init__(
        self,
        enforced: FaceNetORT,
        shadow: FaceNetORT,
        *,
        db_path: str | Path,
        enforced_gallery: Gallery | None = None,
        shadow_gallery: Gallery | None = None,
        site: str | None = None,
        enabled: bool = True,
        sample_every: int = 1,
        enforced_contract: str | None = None,
        shadow_contract: str | None = None,
        queue_failed_writes: bool = True,
    ) -> None:
        if enforced.width == shadow.width:
            raise ShadowError(
                f"the shadow encoder has the same width as the enforced one ({enforced.width}); "
                "there is nothing to compare"
            )
        if enforced_gallery is not None and enforced_gallery.width != enforced.width:
            raise ShadowError(
                f"enforced gallery is {enforced_gallery.width}-D for a {enforced.width}-D encoder"
            )
        if shadow_gallery is not None and shadow_gallery.width != shadow.width:
            raise ShadowError(
                f"shadow gallery is {shadow_gallery.width}-D for a {shadow.width}-D encoder"
            )
        if sample_every < 1:
            raise ShadowError(f"sample_every must be >= 1, got {sample_every}")
        self.enforced, self.shadow = enforced, shadow
        self.enforced_gallery, self.shadow_gallery = enforced_gallery, shadow_gallery
        self.site, self.enabled, self.sample_every = site, bool(enabled), int(sample_every)
        self.enforced_contract, self.shadow_contract = enforced_contract, shadow_contract
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._counter = 0
        self._queued: list[ShadowRecord] = []
        self.queue_failed_writes = queue_failed_writes
        try:
            with sqlite3.connect(self._db_path, timeout=10.0) as conn:
                conn.executescript(SCHEMA)
        except sqlite3.Error as exc:
            raise ShadowError(f"could not prepare the shadow log at {self._db_path}: {exc}") from exc

    # -- sampling -------------------------------------------------------------
    def _should_shadow(self) -> bool:
        """Deterministic sampling when a full shadow pass is too expensive at peak.

        Sampled by counter rather than by worker id: a hash-of-worker sample is stable across a shift
        and would slowly become a *biased* sample (the same people every time), whereas a counter is
        unbiased over volume - which is what a paired ROC needs.
        """
        if not self.enabled:
            return False
        self._counter += 1
        return self._counter % self.sample_every == 0

    # -- the one public entry point -------------------------------------------
    def score(
        self, tensor: np.ndarray, *, worker_id: str | None = None
    ) -> tuple[np.ndarray, str | None, float | None]:
        """``(enforced embedding, enforced verdict, shadow distance)``.

        The returned verdict is *always* the enforced one. ``shadow distance`` is returned for the
        caller that wants to record it next to the decision it did not take; nothing in the return
        path depends on it.
        """
        started = time.perf_counter()
        enforced_started = time.perf_counter()
        enforced_embedding = self.enforced.embed(tensor)          # any failure here propagates: it is the product
        enforced_elapsed = (time.perf_counter() - enforced_started) * 1000.0
        enforced_reading = self._read(self.enforced_gallery, enforced_embedding)
        record = ShadowRecord(
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            site=self.site,
            worker_id=worker_id,
            enforced_model=self.enforced.model_id,
            enforced_width=self.enforced.width,
            enforced_contract=self.enforced_contract,
            enforced_dist=enforced_reading[1] if enforced_reading else None,
            enforced_verdict=enforced_reading[2] if enforced_reading else None,
            shadow_model=self.shadow.model_id,
            shadow_width=self.shadow.width,
            shadow_contract=self.shadow_contract,
            shadow_dist=None,
            shadow_verdict=None,
            outcome=OUTCOME_SKIPPED,
            error=None,
            enforced_ms=round(enforced_elapsed, 3),
            shadow_ms=0.0,
            total_ms=0.0,
        )
        shadow_distance: float | None = None
        if self._should_shadow():
            shadow_started = time.perf_counter()
            try:
                shadow_embedding = self.shadow.embed(tensor)      # one more forward pass, same crop
                shadow_reading = self._read(self.shadow_gallery, shadow_embedding)
                if shadow_reading is not None:
                    shadow_distance = shadow_reading[1]
                    record.shadow_dist = shadow_reading[1]
                    record.shadow_verdict = shadow_reading[2]
                record.outcome = OUTCOME_OK
            except Exception as exc:  # noqa: BLE001 - isolation IS the feature
                # Deliberately broad: an ORT provider failure, an EP-level allocation error, a
                # shape surprise from a mis-exported graph. None of them may reach the worker.
                record.outcome = OUTCOME_ERROR
                record.error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("shadow scoring failed; enforced decision unaffected: %s", exc)
            record.shadow_ms = round((time.perf_counter() - shadow_started) * 1000.0, 3)
        else:
            record.outcome = OUTCOME_SKIPPED
        record.total_ms = round((time.perf_counter() - started) * 1000.0, 3)
        self._write(record)
        return enforced_embedding, record.enforced_verdict, shadow_distance

    @staticmethod
    def _read(gallery: Gallery | None, embedding: np.ndarray) -> tuple[str, float, str] | None:
        if gallery is None:
            return None
        found = gallery.nearest(embedding)
        if found is None:
            return None
        worker_id, distance = found
        return worker_id, distance, gallery.verdict(distance)

    # -- logging --------------------------------------------------------------
    def _write(self, record: ShadowRecord) -> None:
        columns = ", ".join(asdict(record))
        marks = ", ".join("?" for _ in asdict(record))
        try:
            with self._lock, sqlite3.connect(self._db_path, timeout=5.0) as conn:
                conn.execute(
                    f"INSERT INTO shadow_scores ({columns}) VALUES ({marks})", record.as_tuple()
                )
                if self._queued:  # a previous write failed: flush the backlog in the same transaction
                    backlog = list(self._queued)
                    self._queued.clear()
                    conn.executemany(
                        f"INSERT INTO shadow_scores ({columns}) VALUES ({marks})",
                        [row.as_tuple() for row in backlog],
                    )
        except sqlite3.Error as exc:
            log.warning("shadow row not written (%s); queued for the next write", exc)
            if self.queue_failed_writes:
                self._queued.append(record)

    # -- reporting ------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        try:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS events,
                           SUM(outcome = 'shadow_ok')      AS ok,
                           SUM(outcome = 'shadow_error')   AS errors,
                           SUM(outcome = 'shadow_skipped') AS skipped,
                           AVG(enforced_ms) AS enforced_ms,
                           AVG(shadow_ms)   AS shadow_ms
                      FROM shadow_scores
                    """
                ).fetchone()
                paired = conn.execute(
                    """
                    SELECT COUNT(*) AS n,
                           AVG(CASE WHEN enforced_verdict = shadow_verdict THEN 1.0 ELSE 0.0 END) AS agreement
                      FROM shadow_scores
                     WHERE outcome = 'shadow_ok'
                       AND enforced_dist IS NOT NULL AND shadow_dist IS NOT NULL
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise ShadowError(f"could not read the shadow log: {exc}") from exc
        events = int(row["events"] or 0)
        errors = int(row["errors"] or 0)
        return {
            "events": events,
            "ok": int(row["ok"] or 0),
            "errors": errors,
            "skipped": int(row["skipped"] or 0),
            "error_rate": round(errors / events, 4) if events else 0.0,
            "paired_samples": int(paired["n"] or 0),
            "verdict_agreement": round(float(paired["agreement"] or 0.0), 4),
            "enforced_ms_mean": round(float(row["enforced_ms"] or 0.0), 3),
            "shadow_ms_mean": round(float(row["shadow_ms"] or 0.0), 3),
        }


# ---------------------------------------------------------------------------
# the cutover gate
# ---------------------------------------------------------------------------
def paired_confusion(
    db_path: str | Path,
    *,
    enforced_threshold: float,
    shadow_threshold: float,
    limit: int | None = None,
) -> dict[str, Any]:
    """Both encoders' accept/refuse over the **same** rows, plus each one's acceptance rate.

    This is the honest comparison: identical probes, two thresholds, four counts. Only rows where
    both distances exist enter, because a row the shadow failed to score carries no comparison - and
    counting it as agreement is how a broken shadow session passes its own gate.
    """
    query = (
        "SELECT enforced_dist, shadow_dist FROM shadow_scores "
        "WHERE outcome = 'shadow_ok' AND enforced_dist IS NOT NULL AND shadow_dist IS NOT NULL "
        "ORDER BY id"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    try:
        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
            rows = conn.execute(query).fetchall()
    except sqlite3.Error as exc:
        raise ShadowError(f"could not read paired rows: {exc}") from exc
    if not rows:
        return {"paired_samples": 0}
    enforced = np.asarray([row[0] for row in rows], dtype=np.float64)
    shadow = np.asarray([row[1] for row in rows], dtype=np.float64)
    enforced_accept = enforced <= enforced_threshold
    shadow_accept = shadow <= shadow_threshold
    return {
        "paired_samples": int(enforced.size),
        "enforced_accept_rate": round(float(enforced_accept.mean()), 4),
        "shadow_accept_rate": round(float(shadow_accept.mean()), 4),
        "agreement": round(float((enforced_accept == shadow_accept).mean()), 4),
        "shadow_stricter": round(float((enforced_accept & ~shadow_accept).mean()), 4),
        "shadow_more_permissive": round(float((~enforced_accept & shadow_accept).mean()), 4),
        "enforced_threshold": enforced_threshold,
        "shadow_threshold": shadow_threshold,
    }


def flip_ready(
    *,
    coverage_fraction: float,
    paired: dict[str, Any],
    min_coverage: float = 0.98,
    min_paired_samples: int = 2000,
    max_error_rate: float = 0.005,
    summary: dict[str, Any] | None = None,
    max_permissive_delta: float = 0.0,
) -> tuple[bool, str]:
    """Every gate, in the order they can fail, with the reason returned rather than logged only.

    The gates exist because each one has a plausible way of being skipped:

    * **coverage** - flipping with a 40 % shadow gallery silently splits the workforce into two
      behaviours, and the reports then disagree with each other about the same shift;
    * **paired volume** - 30 paired samples cannot distinguish two encoders, and the confidence
      interval on a 30-sample agreement rate spans most of the useful range;
    * **shadow health** - a shadow that errors is not a shadow that disagrees; a 40 % error rate
      would otherwise read as "the encoders differ, interesting";
    * **strictly better, or at least not more permissive** - a migration that widens acceptance on
      the same traffic is an access-control *weakening* dressed as an upgrade, which is the one
      outcome that must not pass by default.
    """
    if coverage_fraction < min_coverage:
        return False, (
            f"shadow gallery covers {coverage_fraction:.1%} of the workforce; need {min_coverage:.0%}"
        )
    if summary is not None and float(summary.get("error_rate", 0.0)) > max_error_rate:
        return False, (
            f"shadow error rate {float(summary['error_rate']):.2%} exceeds {max_error_rate:.2%}"
        )
    samples = int(paired.get("paired_samples", 0))
    if samples < min_paired_samples:
        return False, f"{samples} paired samples; need {min_paired_samples} before the comparison means anything"
    permissive_delta = float(paired.get("shadow_more_permissive", 0.0))
    if permissive_delta > max_permissive_delta + 1e-9:
        return False, (
            f"the shadow accepts {permissive_delta:.2%} of traffic the incumbent refuses - a wider "
            "acceptance on identical probes is a weakening, not an upgrade"
        )
    if samples and float(paired.get("agreement", 0.0)) >= 1.0:
        return False, (
            "the encoders agree on every paired sample; verify the shadow is really a different graph"
        )
    return True, (
        f"coverage {coverage_fraction:.1%}, {samples} paired samples, errors "
        f"{float((summary or {}).get('error_rate', 0.0)):.2%} - clear to cut over per site"
    )


def rollout_report(scorer: ShadowScorer, *, total_workers: int, rollout: dict[str, Any]) -> dict[str, Any]:
    """One object an operator reads: health, coverage, paired comparison, and the gate's verdict."""
    summary = scorer.summary()
    coverage_fraction = (
        coverage(scorer.shadow_gallery, total_workers=total_workers)
        if scorer.shadow_gallery is not None
        else 0.0
    )
    paired = paired_confusion(
        scorer._db_path,
        enforced_threshold=float(rollout["enforced_threshold"]),
        shadow_threshold=float(rollout["shadow_threshold"]),
    )
    ready, reason = flip_ready(
        coverage_fraction=coverage_fraction,
        paired=paired,
        min_coverage=float(rollout.get("min_coverage", 0.98)),
        min_paired_samples=int(rollout.get("min_paired_samples", 2000)),
        max_error_rate=float(rollout.get("max_error_rate", 0.005)),
        summary=summary,
    )
    return {
        "summary": summary,
        "coverage": round(coverage_fraction, 4),
        "paired": paired,
        "flip_ready": ready,
        "reason": reason,
    }


def dump_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
