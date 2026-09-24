"""Beginning the 128-to-512 embedder migration, off the punch path.

WHERE THIS SITS
---------------
``docs/RUNBOOK_EMBEDDER_MIGRATION.md`` calls phase 3 a *migration instrument* rather than a fix,
and is explicit that the width is not what buys a two-metre verification: coverage is phase 1 (the
detector's reach) and separation is phase 2 (the input contract and a recalibrated band). What the
width change buys is a second encoder running beside the first, accumulating paired evidence, with
a gate that decides when - or whether - to cut over. That is what this module starts.

Three modules already did the hard part and none of them is wired to anything: ``facenet_ort``
executes any FaceNet graph and reads the embedding width out of it, ``face_align`` produces one
aligned crop and names the input contract as a value, and ``shadow`` owns the paired log and the
cutover gate (its ``ShadowScorer`` cannot fail a punch, refuses to compare across widths, and
refuses an un-normalized template). This module is the missing wiring, and it is deliberately the
*offline* half of it: nothing here runs inside a request, so the zero-downtime property is
structural rather than careful.

WHY THE SHADOW GRAPH IS A DROP-IN
---------------------------------
The 512 graph is a ~90 MiB artefact that does not ship in this repository (``backend/models/README.md``:
large weights are fetched per host). So a deployment without one is the *normal* state, and every
entry point here reports that state honestly - naming the path and the variable that moves it -
rather than failing, warning spuriously, or worse, quietly scoring with the incumbent twice. Put the
file at ``backend/models/facenet512.onnx`` (or point ``FACENET_SHADOW_MODEL_PATH`` at it) and the
migration can start with no code change, because nothing here assumes a width.

THE THREE THINGS THIS REFUSES TO DO
-----------------------------------
1. **Compare across widths.** ``shadow.Gallery`` already raises on a cross-width probe; this module
   refuses earlier and more visibly - two graphs of the same width have nothing to compare, and the
   operator almost certainly pointed the shadow path at the incumbent's file by accident. A same-width
   pair would make the shadow agree with the enforced encoder on every sample, which the cutover gate
   already treats as suspicious ("verify the shadow is really a different graph"); failing at load
   time names the mistake instead of reporting a suspicious agreement a day later.
2. **Feed a graph a tensor it did not ask for.** Both encoders are handed the *same* crop - that is
   the controlled experiment - which means the pair shares one input contract, and the configured
   shadow contract is refused when it differs from the incumbent's. A graph fed the wrong channel
   order returns a vector of the right shape in the right range while recognising nobody: no
   exception, no smoke, just a separation window that has closed. Measuring whose contract is right
   is ``tools/contract_ab.py``'s job, and the refusal names it.
3. **Classify with a line that was not measured for the shadow.** The shadow needs its own approve
   line, because a distance in a 512-D space is not a distance in a 128-D one and the two are not
   even the same units. So scoring refuses without one, and the line is recorded with its evidence
   string. The runbook's rule - "a band cannot be read by a pipeline it was not measured for" - is
   enforced here rather than quoted.

WHAT THIS CANNOT SEE, AND SAYS SO
---------------------------------
Paired evidence from *stored punch frames* is weaker than the runbook's live traffic, in one
specific way: those frames are overwhelmingly punches the incumbent already approved, so both
encoders agree on most of them and ``verdict_agreement`` reads near 1.0 - which the gate treats as
suspicious rather than as success. The gate's coverage, volume, health and permissiveness tests are
all still meaningful on a stored set; a claim that the shadow is *more accurate* is not, and this
module does not make one. The report carries the probe source beside the numbers for that reason.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Iterable

import numpy as np

import face_align
import face_detector

log = logging.getLogger("attendance.shadow_rollout")

#: The contract both encoders are fed, and the one the incumbent 128 graph is measured under. It is
#: ``face_align.CONTRACTS[0]`` rather than a retyped string: the A/B reports the four in this order
#: with the incumbent first, and a second definition here is a second thing to drift.
INCUMBENT_CONTRACT: Final = face_align.CONTRACTS[0]

#: Where a drop-in graph is expected when nothing is configured. Matches
#: ``tools/contract_ab.DEFAULT_MODEL_CANDIDATES``, which already looked here for a 512 export.
DEFAULT_SHADOW_GRAPH: Final = Path("backend/models/facenet512.onnx")

class ShadowUnavailable(RuntimeError):
    """The migration cannot be started or continued, with the reason a reader can act on.

    Distinct from ``shadow.ShadowError``, which is about a pair that was already built: this one is
    raised by the loading and refusal checks below, before any session exists.
    """


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def _settings() -> Any:
    """``config.settings``, or ``None`` in a process that never loaded it.

    Wrapped rather than imported at module scope for the reason ``face_onnx.model_path`` gives:
    a tool or a test must be able to import this module without a configuration.
    """
    try:
        import config

        return config.settings
    except Exception:  # pragma: no cover - config is importable everywhere else
        return None


def enforced_model_path() -> Path:
    """The incumbent's graph path, from the setting that decides it.

    Read from ``config`` rather than ``face_onnx.model_path()`` deliberately, and the reason is a
    rule this repository enforces rather than a preference: ``test_face_engine`` holds that only
    ``face_engine`` resolves the embedding engine, because a direct resolution works perfectly and
    simply does not participate in the capacity policy - which is how the last three unbounded call
    sites came about. This module reads the *path*, and it never resolves the live engine: the
    offline encoders below are its own ``facenet_ort`` sessions, in their own process.
    """
    settings = _settings()
    configured = getattr(settings, "facenet_model_path", None) if settings else None
    if configured:
        return Path(configured)
    import os

    return Path(os.environ.get("FACENET_MODEL_PATH") or "backend/models/facenet128.onnx")


def shadow_model_path() -> Path:
    """The configured drop-in graph. Absent is the shipped state, not an error."""
    settings = _settings()
    configured = getattr(settings, "facenet_shadow_model_path", None) if settings else None
    if configured:
        return Path(configured)
    import os

    return Path(os.environ.get("FACENET_SHADOW_MODEL_PATH") or DEFAULT_SHADOW_GRAPH)


def shadow_model_sha256() -> str:
    settings = _settings()
    return str(getattr(settings, "facenet_shadow_model_sha256", "") or "") if settings else ""


def shadow_log_path() -> Path:
    """The paired log: its own SQLite file, beside the database unless configured.

    Beside the database means on the volume in a deployment, which is what makes the evidence
    survive the container that produced it.
    """
    settings = _settings()
    configured = str(getattr(settings, "shadow_log_path", "") or "") if settings else ""
    if configured:
        return Path(configured)
    database = Path(str(getattr(settings, "database_path", "times.db"))) if settings else Path("times.db")
    return database.parent / "shadow.sqlite"


def contract_from_id(text: str) -> face_align.Contract:
    """The contract a configured id names, or a refusal that lists the ones that exist.

    Ids are ``field:value`` (``bgr:0_1``), which is ``face_align.Contract.id`` - so a contract
    written into a log line or a band key can be read back into the object that produced it.
    """
    wanted = str(text or "").strip().lower()
    for contract in face_align.CONTRACTS:
        if contract.id.lower() == wanted:
            return contract
    raise ShadowUnavailable(
        f"unknown shadow input contract {text!r}; expected one of "
        f"{sorted(contract.id for contract in face_align.CONTRACTS)}"
    )


def shadow_contract() -> face_align.Contract:
    """The contract the *shadow* graph is fed, refused when it is not the incumbent's.

    See refusal 2 in the module docstring: one crop is handed to both encoders, so a pair that
    disagrees about the tensor is not a pair of encoders - it is one encoder and one graph being
    fed something it did not ask for.
    """
    settings = _settings()
    text = str(getattr(settings, "facenet_shadow_contract", "") or "") if settings else ""
    contract = contract_from_id(text or INCUMBENT_CONTRACT.id)
    if contract.id != INCUMBENT_CONTRACT.id:
        raise ShadowUnavailable(
            f"the shadow is configured for {contract.id} but the incumbent is fed "
            f"{INCUMBENT_CONTRACT.id}, and both encoders are handed the same crop. A graph fed "
            "the wrong tensor returns a plausible vector while recognising nobody, so this is "
            "refused rather than scored. Measure the shadow's contract with "
            "tools/contract_ab.py and make the pair agree before migrating."
        )
    return contract


# ---------------------------------------------------------------------------
# the pair
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Encoders:
    """Two graphs of different widths, on one contract, and the facts needed to interpret them."""

    enforced: Any
    shadow: Any
    contract: face_align.Contract

    @property
    def enforced_width(self) -> int:
        return int(self.enforced.width)

    @property
    def shadow_width(self) -> int:
        return int(self.shadow.width)

    def describe(self) -> dict[str, Any]:
        return {
            "contract": self.contract.id,
            "enforced": {
                "model_id": self.enforced.model_id[:16],
                "width": self.enforced_width,
                "input_size": int(self.enforced.input_size),
                "file": Path(self.enforced.path).name,
            },
            "shadow": {
                "model_id": self.shadow.model_id[:16],
                "width": self.shadow_width,
                "input_size": int(self.shadow.input_size),
                "file": Path(self.shadow.path).name,
            },
        }


def validate_pair(enforced: Any, shadow: Any) -> None:
    """Refuse a pair that cannot be a controlled comparison. Pure, so it is cheap to pin.

    Two checks, and both are about the comparison being *about the encoder*:

    * **different widths** - otherwise there is nothing to compare and the gate's own
      universal-agreement check would have to notice, a day later, what is visible now;
    * **the same input size** - ``ShadowScorer.score`` hands one tensor to both graphs, so a pair
      whose graphs declare different canvases cannot be fed the same pixels, and resizing to
      reconcile them would make this a crop experiment wearing an encoder experiment's name.
    """
    if int(enforced.width) == int(shadow.width):
        raise ShadowUnavailable(
            f"the shadow encoder has the same width as the enforced one "
            f"({int(enforced.width)}): there is nothing to compare. The shadow path is probably "
            "pointed at the incumbent's graph."
        )
    if int(enforced.input_size) != int(shadow.input_size):
        raise ShadowUnavailable(
            f"the graphs declare different input canvases "
            f"({int(enforced.input_size)} and {int(shadow.input_size)}); one tensor is fed to both, "
            "so these cannot be compared without resizing - which would be a crop change, not an "
            "encoder change"
        )


def load_encoders(*, warmup: bool = False) -> Encoders:
    """The pair, or ``ShadowUnavailable`` naming what to do about it.

    The order of the checks is the order of their cost, and the cheapest one is also the common
    case in a deployment that has not migrated: the 90 MiB graph is not there. That is reported by
    *path*, because the fix is to put a file somewhere, not to change a decision.
    """
    from facenet_ort import EmbeddingError, FaceNetORT

    import facenet_ort

    contract = shadow_contract()
    shadow_path = shadow_model_path()
    if not shadow_path.exists():
        raise ShadowUnavailable(
            f"no shadow graph at {shadow_path}. This is the shipped state, not a fault: the "
            "artifact is ~90 MiB and is fetched per host rather than committed. Put a FaceNet "
            "export there, or point FACENET_SHADOW_MODEL_PATH at one, and the migration can start."
        )
    enforced_path = enforced_model_path()
    if not enforced_path.exists():
        raise ShadowUnavailable(f"the incumbent graph is missing: {enforced_path}")

    try:
        enforced = FaceNetORT(enforced_path, warmup=warmup)
        shadow_encoder = FaceNetORT(
            shadow_path, warmup=warmup, expected_sha256=shadow_model_sha256() or None
        )
    except EmbeddingError as exc:
        raise ShadowUnavailable(f"could not open the graphs: {exc}") from exc

    validate_pair(enforced, shadow_encoder)
    return Encoders(enforced=enforced, shadow=shadow_encoder, contract=contract)


def status() -> dict[str, Any]:
    """Whether the migration can start, in one object, without raising.

    Loads the graphs when it can, because "the file is there" and "the file is a FaceNet graph of a
    different width on the same canvas" are different answers and only one of them means the
    migration has begun. Every failure is a string in ``reason``.
    """
    report: dict[str, Any] = {
        "configured": {
            "shadow_path": str(shadow_model_path()),
            "shadow_present": shadow_model_path().exists(),
            "enforced_path": str(enforced_model_path()),
            "enforced_present": enforced_model_path().exists(),
            "contract": None,
            "log": str(shadow_log_path()),
        },
        "started": False,
        "reason": None,
        "encoders": None,
    }
    try:
        report["configured"]["contract"] = shadow_contract().id
    except ShadowUnavailable as exc:
        report["reason"] = str(exc)
        return report
    try:
        encoders = load_encoders()
    except ShadowUnavailable as exc:
        report["reason"] = str(exc)
        return report
    report["started"] = True
    report["encoders"] = encoders.describe()
    return report


# ---------------------------------------------------------------------------
# the crop, and the tensor
# ---------------------------------------------------------------------------
@dataclass
class CropResult:
    """One frame's crop, or the reason there is none."""

    tensor: np.ndarray | None
    reason: str | None = None
    face_px: int | None = None

    @property
    def ok(self) -> bool:
        return self.tensor is not None


def crop_frame(frame: Any, *, contract: face_align.Contract, input_size: int) -> CropResult:
    """A frame -> the app's own crop -> the shared tensor, or a named reason.

    The crop is ``face_detector.detect_and_align``'s - the *same* call the punch path makes - and
    the subject rule is ``face_detector.subject_detections``: duplicate boxes for one face are
    merged and sub-template specks dropped, because a shadow that counted a face-shaped patch of
    poster as a second person would drop exactly the frames a migration most needs to look at. The
    one-subject rule is the punch path's rule, so it is applied here rather than relaxed: an
    ambiguity the gate refuses is not evidence about an encoder.

    ``face_px`` is the detection's own width in the frame the detector saw, recorded beside the
    tensor so a report can say what its probes actually were.
    """
    if frame is None:
        return CropResult(None, "image could not be read")
    try:
        from face_align import to_tensor

        detections = face_detector.detect_and_align(frame)
    except Exception as exc:  # noqa: BLE001 - a bad frame is a skip, never a failed run
        return CropResult(None, f"detector failed: {type(exc).__name__}")
    if not detections:
        return CropResult(None, "no face")
    subjects = face_detector.subject_detections(detections)
    if subjects.count == 0:
        return CropResult(None, "no subject-sized face")
    if subjects.count > 1:
        return CropResult(None, "many faces")
    chosen = subjects.faces[0]
    area = chosen.get("facial_area") if isinstance(chosen, dict) else None
    width = None
    if isinstance(area, dict):
        try:
            width = int(float(area.get("w")))
        except (TypeError, ValueError):  # a detector shape this module does not know
            width = None
    try:
        tensor = to_tensor(chosen["face"], contract, size=int(input_size))
    except Exception as exc:  # noqa: BLE001
        return CropResult(None, f"could not align the crop: {type(exc).__name__}")
    return CropResult(tensor, None, width)


def tensor_for_path(path: str | Path, *, contract: face_align.Contract, input_size: int) -> CropResult:
    """The same thing for a file on disk (a stored punch frame, a reference selfie)."""
    import cv2

    frame = cv2.imread(str(path))
    return crop_frame(frame, contract=contract, input_size=input_size)


# ---------------------------------------------------------------------------
# the galleries
# ---------------------------------------------------------------------------
def worker_ids(conn: sqlite3.Connection) -> list[str]:
    """Accounts that can be in a gallery: live workers, ordered so a run is reproducible."""
    rows = conn.execute(
        "SELECT user_id FROM users WHERE role = 'worker' AND COALESCE(NULLIF(is_active, 0), 1) = 1 "
        "ORDER BY user_id"
    ).fetchall()
    return [str(row[0]) for row in rows]


def build_enforced_gallery(
    conn: sqlite3.Connection, encoders: Encoders
) -> tuple[Any, dict[str, Any]]:
    """The incumbent gallery: the templates already on disk, at their stored width.

    Read through ``biometrics`` - the application's own resolver and parser - so the gallery is
    what a punch is actually scored against, not a re-derivation of it. Nothing is re-embedded
    here: the incumbent arm of the comparison must be the numbers production already uses, and
    re-running the 128 graph over the stored selfies would replace the comparison's control with a
    second approximation of it.
    """
    import biometrics
    import shadow

    gallery = shadow.Gallery(
        width=encoders.enforced_width, band=face_detector.active_band(), name="enforced"
    )
    report: dict[str, Any] = {"templates": 0, "skipped": []}
    for user_id in worker_ids(conn):
        if not biometrics.is_enrolled(user_id):
            report["skipped"].append({"worker_id": user_id, "reason": "no template"})
            continue
        try:
            reference = biometrics.read_reference(biometrics.resolve_reference(user_id))
            gallery.add_normalized(user_id, np.asarray(reference.embedding, dtype=np.float64))
            report["templates"] += 1
        except Exception as exc:  # noqa: BLE001 - a gallery is a report, and one member is not it
            report["skipped"].append(
                {"worker_id": user_id, "reason": f"{type(exc).__name__}: {exc}"[:200]}
            )
    report["size"] = gallery.size
    return gallery, report


def build_shadow_gallery(
    conn: sqlite3.Connection, encoders: Encoders, *, band: Any
) -> tuple[Any, dict[str, Any]]:
    """The shadow gallery: every enrolled worker's stored selfie, re-embedded at the new width.

    This is the backfill, and it has to re-embed rather than convert: a 512-D template cannot be
    derived from a 128-D one, and every way of pretending otherwise (pad, truncate, project) is a
    way to accept the wrong person - which ``shadow.Gallery`` refuses to store. So the *photograph*
    is the source of truth for the new space, read through ``biometrics.resolve_photo``.

    A worker with no selfie on disk is reported as skipped, by name, with the reason. That is what
    keeps ``coverage()`` honest: a gallery that quietly omitted the people it could not process
    would report a fraction of the workforce while describing itself as complete.
    """
    import biometrics
    import shadow

    gallery = shadow.Gallery(width=encoders.shadow_width, band=band, name="shadow")
    report: dict[str, Any] = {"templates": 0, "skipped": []}
    for user_id in worker_ids(conn):
        photo = biometrics.resolve_photo(user_id)
        if not photo:
            report["skipped"].append({"worker_id": user_id, "reason": "no reference photo"})
            continue
        cropped = tensor_for_path(
            photo, contract=encoders.contract, input_size=int(encoders.shadow.input_size)
        )
        if not cropped.ok:
            report["skipped"].append({"worker_id": user_id, "reason": cropped.reason})
            continue
        try:
            embedding = encoders.shadow.embed(cropped.tensor)
            gallery.add_normalized(user_id, np.asarray(embedding, dtype=np.float64))
            report["templates"] += 1
        except Exception as exc:  # noqa: BLE001
            report["skipped"].append(
                {"worker_id": user_id, "reason": f"{type(exc).__name__}: {exc}"[:200]}
            )
    report["size"] = gallery.size
    log.info(
        "shadow gallery backfilled: %s template(s), %s skipped",
        report["templates"],
        len(report["skipped"]),
    )
    return gallery, report


def shadow_band(*, approve: float, evidence: str, model: str, review: float | None = None) -> Any:
    """A band for the shadow's own space, from a line somebody measured for it.

    Built by ``dataclasses.replace`` on the live band rather than as a fresh record, so the fields
    this module does not own - the boundaries, and the derivation rule that makes the lines
    re-derivable - keep coming from the definition ``face_detector`` and the band runbook own. Only
    the model name, the two lines and the evidence are replaced.

    ``evidence`` and ``model`` are required arguments, not defaults. A line with no stated
    provenance is the defect the whole band table was built to remove: the previous version of this
    was two literals in ``main.py`` with nothing recording what they had been measured against, and
    nobody noticed for two crops. A caller that cannot say what measured this line, and which space
    it is a line in, should not be able to build one.

    The refuse line defaults to the approve line, which is ``MatchBand``'s own one-line shape - a
    window too narrow for ``IMPOSTOR_MARGIN`` to separate has no review tier, and declaring that
    honestly is better than placing a second line inside the overlap and sending real workers to
    review every day.
    """
    live = face_detector.active_band()
    return replace(
        live,
        model=str(model),
        approve=float(approve),
        review=float(review) if review is not None else float(approve),
        evidence=str(evidence),
    )


# ---------------------------------------------------------------------------
# the offline paired run
# ---------------------------------------------------------------------------
def score_frames(
    encoders: Encoders,
    *,
    enforced_gallery: Any,
    shadow_gallery: Any,
    frames: Iterable[str | Path],
    db_path: str | Path | None = None,
    sample_every: int = 1,
    site: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Score stored frames through both encoders, into the paired log. Returns what it saw.

    Every frame that cannot be used is counted *by reason* rather than dropped: a run that read
    500 frames and scored 40 has told an operator something important, and a bare 40 would read as
    a small corpus instead.
    """
    import shadow

    scorer = shadow.ShadowScorer(
        encoders.enforced,
        encoders.shadow,
        db_path=db_path or shadow_log_path(),
        enforced_gallery=enforced_gallery,
        shadow_gallery=shadow_gallery,
        site=site,
        sample_every=sample_every,
        # Recorded, not applied: ShadowScorer hands one tensor to both graphs, and every tensor
        # this module builds is on the pair's single contract (see ``shadow_contract``).
        enforced_contract=encoders.contract.id,
        shadow_contract=encoders.contract.id,
    )
    report: dict[str, Any] = {
        "scored": 0,
        "skipped": {},
        "enforced_verdicts": {},
        "shadow_distances": 0,
        "probe_source": "stored frames",
    }
    for path in frames:
        cropped = tensor_for_path(
            path, contract=encoders.contract, input_size=int(encoders.enforced.input_size)
        )
        if not cropped.ok:
            report["skipped"][cropped.reason] = report["skipped"].get(cropped.reason, 0) + 1
            continue
        _embedding, verdict, shadow_distance = scorer.score(cropped.tensor, worker_id=worker_id)
        report["scored"] += 1
        if verdict is not None:
            report["enforced_verdicts"][verdict] = report["enforced_verdicts"].get(verdict, 0) + 1
        if shadow_distance is not None:
            report["shadow_distances"] += 1
    report["shadow_log"] = str(db_path or shadow_log_path())
    report["summary"] = scorer.summary()
    return report


def frame_paths(folder: str | Path, *, limit: int | None = None) -> list[Path]:
    """The frames in a folder, newest last, optionally capped.

    Read here rather than stored anywhere: the punch-frame folder is attendance evidence with its
    own retention, and a migration instrument has no business copying a worker's face somewhere
    new. Sorted by name, which is stable across runs; ``limit`` takes the newest, because the
    frames a migration is about are the ones the current cameras produce.
    """
    directory = Path(folder)
    if not directory.is_dir():
        raise ShadowUnavailable(f"not a folder of frames: {directory}")
    found = sorted(
        path for path in directory.iterdir() if path.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    if limit is not None and limit > 0:
        found = found[-limit:]
    return found


def save_gallery(path: str | Path, gallery: Any, *, model_id: str, contract: str, band: Any) -> Path:
    """Write a built gallery to a JSON cache, with everything needed to refuse it later.

    The backfill is the expensive half of this migration - one detection and one embedding per
    worker - so it is written where the next run can read it. What travels with the vectors is the
    point: the graph's own digest and width, the contract, and the band's evidence string. A cache
    that carried only numbers would be loadable by a *different* graph, and 512 floats from another
    space compare perfectly plausibly against a unit probe while recognising nobody.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "width": int(gallery.width),
        "name": gallery.name,
        "model_id": str(model_id),
        "contract": str(contract),
        "band": {
            "model": getattr(band, "model", ""),
            "approve": float(getattr(band, "approve", 0.0)),
            "review": float(getattr(band, "review", 0.0)),
            "evidence": str(getattr(band, "evidence", "")),
        },
        "templates": {key: [float(value) for value in vector] for key, vector in gallery.templates.items()},
    }
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return target


def load_gallery(
    path: str | Path, *, width: int, model_id: str, band: Any
) -> tuple[Any, dict[str, Any]]:
    """Read a cached gallery back, refusing one that belongs to another graph.

    Two refusals, and they are the reason the cache is safe to have at all: the width must match the
    graph about to score against it, and the *digest* must match too - a differently-trained export
    of the same width produces vectors in a different space, which is the subtler half of the same
    mistake. A mismatch is raised rather than reported, because the caller's next step (re-run the
    backfill) is the same either way and a wrong gallery must not be usable by accident.
    """
    import shadow

    source = Path(path)
    if not source.exists():
        raise ShadowUnavailable(
            f"no shadow gallery at {source}; build one with 'backfill' first - a gallery of the "
            "workforce's faces is the thing the paired comparison is scored against"
        )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShadowUnavailable(f"could not read the gallery at {source}: {exc}") from exc
    cached_width = int(payload.get("width") or 0)
    if cached_width != int(width):
        raise ShadowUnavailable(
            f"the cached gallery at {source} is {cached_width}-D but the shadow graph is "
            f"{int(width)}-D; re-run 'backfill' so the templates belong to the encoder that will "
            "score them"
        )
    cached_model = str(payload.get("model_id") or "")
    if cached_model != str(model_id):
        raise ShadowUnavailable(
            f"the cached gallery at {source} was built by graph {cached_model[:16] or '(unknown)'}, "
            f"not by {str(model_id)[:16]}. The vectors are in another space; re-run 'backfill'."
        )
    gallery = shadow.Gallery(width=cached_width, band=band, name=str(payload.get("name") or "shadow"))
    for key, vector in (payload.get("templates") or {}).items():
        gallery.add_normalized(key, np.asarray(vector, dtype=np.float64))
    return gallery, payload


def total_workers(conn: sqlite3.Connection) -> int:
    """The workforce the coverage fraction is a fraction *of*."""
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'worker' AND COALESCE(NULLIF(is_active, 0), 1) = 1"
    ).fetchone()
    return int(row[0] or 0)


def rollout(
    encoders: Encoders,
    *,
    enforced_gallery: Any,
    shadow_gallery: Any,
    workers: int,
    enforced_threshold: float,
    shadow_threshold: float,
    db_path: str | Path | None = None,
    min_coverage: float = 0.98,
    min_paired_samples: int = 2000,
    max_error_rate: float = 0.005,
) -> dict[str, Any]:
    """The gate's verdict on what the paired log holds, via ``shadow``'s own rule.

    Not re-implemented: ``shadow.rollout_report`` owns the coverage, volume, health and
    permissiveness gates, and a second copy of that arithmetic here would be a second answer to
    "is this migration safe" - which is exactly the kind of thing that drifts.
    """
    import shadow

    scorer = shadow.ShadowScorer(
        encoders.enforced,
        encoders.shadow,
        db_path=db_path or shadow_log_path(),
        enforced_gallery=enforced_gallery,
        shadow_gallery=shadow_gallery,
        enforced_contract=encoders.contract.id,
        shadow_contract=encoders.contract.id,
    )
    report = shadow.rollout_report(
        scorer,
        total_workers=workers,
        rollout={
            "enforced_threshold": float(enforced_threshold),
            "shadow_threshold": float(shadow_threshold),
            "min_coverage": float(min_coverage),
            "min_paired_samples": int(min_paired_samples),
            "max_error_rate": float(max_error_rate),
        },
    )
    report["encoders"] = encoders.describe()
    return report


def dump(report: dict[str, Any]) -> str:
    """One object as text, for a CLI and for a log line."""
    return json.dumps(report, indent=2, sort_keys=True, default=str)
