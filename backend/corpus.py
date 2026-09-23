"""The labelled corpus: real gate captures, filed by identity, with the crop that was measured.

WHY A DIRECTORY STORE AND NOT A TABLE
-------------------------------------
Every other kind of personal data in this application lives in a table, with retention sweeps, audit
events and a reader on some screen. This one deliberately does not, and each reason is a requirement
rather than a preference:

* **a corpus is not attendance data.** Nothing in the app reads it, no report joins to it, and no
  administrator can browse it. Putting it in the schema would put calibration faces one careless query
  away from a payroll screen, and would make an erasure request a question about *two* stores;
* **a corpus has to be moveable.** The A/B and the band derivation run on an operator's laptop, on a
  workstation with a GPU, or in a container beside the model files. A folder copies; a table needs a
  dump, a restore and a schema version;
* **the two tools already read folders.** ``derive_facenet_band`` and ``tools/contract_ab.py`` take a
  corpus directory in the folder contract this store and its export both use, so a store in a different
  shape would need a translator, and a translator is a second place for the crop to be decided.

The price is that the app's retention timer does not sweep it: ``purge()`` is the operator's command and
the runbook says so. That is a real cost, paid deliberately, and ``retention.wipe_file`` still does the
erasing so there is one implementation of overwrite-and-unlink in this codebase.

THE LAYOUT
----------
    <corpus_root>/
      bilal_khan/
        20260922T204113_9f3a2b1c.jpg      the frame, capped at MAX_PX
        20260922T204113_9f3a2b1c.json     everything it would take to reproduce the crop
      worker_two/
        ...
      _unlabelled/
        20260922T205502_77c1de04.jpg      nobody has decided who this is yet

Identity as a *directory*, because that is the folder contract the two measurement tools already read -
so an exported corpus is a copy rather than a translation, and nothing between the capture and the band
can change what "this person" means. Names are ``<timestamp>_<random>``: a timestamp so a folder sorts
into the order it was captured, a random suffix so two captures in the same second cannot collide, and
nothing else - a filename is not an identifier this project exposes, so it must not encode one (a
worker's face filed under a countable number is a directory of faces anybody can enumerate).

``_unlabelled`` is the one partition that is not an identity, and it exists because a corpus of real
traffic contains captures nobody has decided about yet. A store that cannot represent that forces a
guess at import time, and a guessed label is indistinguishable from a verified one afterwards.

THE THREE RULES THAT MAKE A CORPUS WORTH MEASURING ON
-----------------------------------------------------
**1. Only a verified capture may carry a label.** A punch whose band said ``approved`` is a capture
whose identity the system established; a flagged or refused one is a *candidate*. So a label is attached
at capture time only for an approved punch, and anything else lands **unlabelled** - visible, countable,
and labellable later by a human who looked at it. (An operator import is the other case where a label is
legitimate: a session with known people is a stated basis, and ``consent`` records what it was.)

**2. The crop travels with the detector that made it.** A corpus is reused for weeks, and in that time
somebody will upgrade the detector, change its input size, or turn on tiling. Landmarks measured by an
old configuration are not wrong - they are *that configuration's* crop, and a tool that reuses them
while believing it is measuring the new one is measuring the old detector and naming it the new one. So
the fingerprint (model digest, input size, square, tiles, overlap) is stored with the points, and
``landmarks_for(..., expect=...)`` refuses a mismatch instead of returning stale geometry.

**3. Coordinates are stated, and the regime is recorded.** Landmarks are stored in **stored-image
pixels**, because that is the image every later reader will open; ``scale`` and ``source_size`` record
what the frame was before it was capped, and ``native_face_px`` records how big the face was *before*
that cap. A corpus silently downscaled into the small-face regime would be the perfect illustration of
the defect it is supposed to be measuring, so both numbers are kept.

WHAT THIS MODULE WILL NOT DO
----------------------------
It will not invent a label, guess an identity from a filename it does not trust, or export a corpus it
believes is unusable without saying so. It will not link a capture to a punch row: the corpus knows the
*worker* (that is the label) and nothing about their hours, so purging attendance history cannot disturb
calibration and vice versa. And it cannot make an erasure unconditional - see ``retention``'s docstring
on wear levelling, snapshots and backups, which applies here with the added wrinkle that a copied corpus
is outside this process the moment it leaves the machine.

Quality assessment (pose, size, confidence) lives in ``corpus_ingest``, not here: this module is the
*store*, and a store that decided what counts as a usable face would be deciding what a measurement may
be made of. It records the assessment it is handed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import numpy as np
from PIL import Image

from config import settings

log = logging.getLogger("attendance.corpus")

#: Where the corpus lives. Repointed by the test harness (see ``harness.FILE_TREES``) and read at call
#: time, so a test run never writes faces into the checkout - the same convention
#: ``punch_frames.FRAMES_DIR`` and ``quick_links.PHOTOS_DIR`` use.
ROOT_DIR = str(settings.calibration_corpus_dir)

#: Long edge of the stored frame. 1280 keeps a 1920x1080 gate frame's *subject* at two thirds of its
#: native size, which is enough to re-crop a 160px template from anything the detector found, while
#: holding a frame to ~200 KB. ``0`` keeps the original size: choose it when the experiment is about the
#: small-face regime itself, because downscaling moves a corpus into that regime.
MAX_PX = int(getattr(settings, "calibration_corpus_max_px", 1280))

#: JPEG quality of the stored frame. High on purpose: the corpus is measuring what the *contract* and
#: the *crop* do to a face, and compression artefacts are a third variable nobody asked for.
JPEG_QUALITY = 92

#: The sidecar format's version. A reader that finds a version it does not know refuses rather than
#: guessing which fields moved - the file is the only place the crop's provenance exists.
#:
#: Version 1 was the flat layout (``captures/<id>.jpg``) with no ``camera`` and no ``quality``. Those
#: two are *additions*, and the layout is forced to follow the label - neither changes what a stored
#: landmark means, which is the test a version bump has to pass. So v1 records are still read, and
#: ``label()`` re-homes them, which is the only thing the new layout asks of them.
SIDECAR_VERSION: Final = 2
READABLE_VERSIONS: Final = (1, 2)

#: The partition that is not an identity: captures waiting for a human to decide.
UNLABELLED: Final = "_unlabelled"

#: The version-1 directory, read for compatibility and never written to.
LEGACY_PARTITION: Final = "captures"

#: Capture sources, spelled once so a report can group by them.
SOURCE_PUNCH: Final = "punch"
SOURCE_IMPORT: Final = "import"
SOURCE_DIRECTORY: Final = "directory"
SOURCE_WEBCAM: Final = "webcam"
SOURCE_RTSP: Final = "rtsp"

#: Buckets the face-size histogram reports. They answer the question the corpus exists for - "do these
#: captures actually include the small-face regime the failures are at" - without a custom script.
FACE_SIZE_BUCKETS: Final = ((0, 32), (32, 64), (64, 128), (128, 10_000))

#: Consent basis recorded on a capture taken from live traffic under a *worker's own* opt-in. Named
#: once so every record carries the same string, and so an operator reading the corpus can see exactly
#: what was relied on - and can see, per capture, whether the person in the frame is the one who said
#: yes (``worker-granted``) or whether the capture predates the per-worker requirement (the older
#: deployment string, kept below so history reads honestly rather than being relabelled).
WORKER_CONSENT: Final = "worker-granted: per-worker opt-in, audited in corpus_capture_consents"

class CorpusError(RuntimeError):    """A corpus operation that cannot be completed as asked."""


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
@dataclass
class Capture:
    """One capture: the frame, its landmarks, the camera it came from, and how good it was."""

    capture_id: str
    identity: str | None
    image: str
    image_sha256: str
    stored_size: tuple[int, int]
    source_size: tuple[int, int]
    scale: tuple[float, float]
    landmarks: list[list[float]]
    box: list[float] | None
    score: float | None
    face_px: float
    native_face_px: float
    detector: dict[str, Any]
    source: str
    consent: str
    captured_at: str
    actor: str | None = None
    note: str | None = None
    landmark_source: str = "detector"
    version: int = SIDECAR_VERSION
    label_history: list[dict[str, Any]] = field(default_factory=list)
    #: Where the frame came from: a camera name, a gate, or an RTSP URL's *label* (never the URL with
    #: credentials in it - see ``corpus_ingest.redact``). Free text because it is an operator's word and
    #: the corpus should not force a taxonomy on a site.
    camera: str | None = None
    #: The quality assessment this store was handed (flags, pose, the gate that produced them). Empty
    #: for a capture whose caller did not assess it, which is different from "assessed and fine".
    quality: dict[str, Any] = field(default_factory=dict)

    @property
    def points(self) -> np.ndarray:
        """The five landmarks as ``(5, 2)`` float32, in stored-image pixels."""
        return np.asarray(self.landmarks, dtype=np.float32).reshape(5, 2)

    @property
    def flags(self) -> list[str]:
        return [str(flag) for flag in (self.quality or {}).get("flags") or []]

    @property
    def hard_case(self) -> bool:
        """A capture that is usable but not typical - small, off-angle, or barely detected.

        Kept, because a coverage experiment needs exactly these; excluded from a *measurement* by
        default, because a band derived from hard cases is a band for hard cases.
        """
        return bool((self.quality or {}).get("hard_case", bool(self.flags)))

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stored_size"] = list(self.stored_size)
        payload["source_size"] = list(self.source_size)
        payload["scale"] = list(self.scale)
        return payload

    def crop_is(self, fingerprint: dict[str, Any]) -> bool:
        return same_crop(self.detector, fingerprint)


@dataclass(frozen=True)
class Prepared:
    """A frame capped for storage, with the scale that got it there. Detected on, then stored.

    Two steps rather than one because the detector has to run on **the pixels that will be kept**: a
    capture's landmarks are only reproducible for whoever opens the file later if they were measured on
    that file. A single ``store(image, landmarks)`` call would either force the caller to detect on the
    original (landmarks that mean something else) or hide a second downscale inside the store.
    """

    frame: Image.Image
    stored_size: tuple[int, int]
    scale: tuple[float, float]
    source_size: tuple[int, int]

    @property
    def mean_scale(self) -> float:
        return float(math.sqrt(self.scale[0] * self.scale[1])) or 1.0


# ---------------------------------------------------------------------------
# paths and partitions
# ---------------------------------------------------------------------------
def root_dir() -> str:
    """The corpus root, created on first use."""
    os.makedirs(ROOT_DIR, exist_ok=True)
    return ROOT_DIR


def identity_partition(identity: str | None) -> str:
    """The directory an identity's captures live in. ``None`` is :data:`UNLABELLED`, not an error."""
    return UNLABELLED if not identity else _identity_slug(identity)


def partition_dir(partition: str, *, create: bool = True) -> str:
    path = os.path.join(root_dir(), _checked_partition(partition))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def partitions(*, existing_only: bool = True) -> list[str]:
    """Every partition under the root, sorted. ``captures/`` is the version-1 layout, still read.

    A directory with no sidecars in it is not a partition anybody can use, so it is filtered out of the
    default answer - an operator listing a corpus wants the identities, not the leftovers of a failed
    run.
    """
    root = root_dir()
    found: list[str] = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith("."):
            continue
        if existing_only and not any(entry.endswith(".json") for entry in os.listdir(path)):
            continue
        found.append(name)
    return found


def captures_dir() -> str:
    """The root, for a caller that wants the whole tree rather than one identity (tests, backups).

    Kept as a name because it is what ``harness.FILE_TREES`` and the environment suite resolve; the
    *layout* below it is a partition per identity.
    """
    return root_dir()


def find_capture(capture_id: str) -> tuple[str, str] | None:
    """``(partition, directory)`` for a capture id, or ``None``. One lookup for every path question.

    Scanned rather than stored, because the partition *is* the label and a label can change: an index
    would be a second place for which identity a capture belongs to, and the two would eventually
    disagree after a re-label. A corpus is thousands of files, not millions; a directory listing per
    lookup is the cheap side of that trade.
    """
    wanted = _checked_id(capture_id)
    for partition in [*partitions(existing_only=False)]:
        directory = partition_dir(partition, create=False)
        if os.path.exists(os.path.join(directory, f"{wanted}.json")):
            return partition, directory
    return None


def image_path(capture_id: str) -> str:
    """The frame's path, wherever its partition happens to be."""
    found = find_capture(capture_id)
    if found is None:
        raise CorpusError(f"no such capture: {capture_id}")
    return os.path.join(found[1], f"{_checked_id(capture_id)}.jpg")


def sidecar_path(capture_id: str) -> str:
    """The sidecar's path, wherever its partition happens to be."""
    found = find_capture(capture_id)
    if found is None:
        raise CorpusError(f"no such capture: {capture_id}")
    return os.path.join(found[1], f"{_checked_id(capture_id)}.json")


def sidecar_for(image: str | os.PathLike[str]) -> str:
    """The sidecar that belongs to an image path: same stem, same directory.

    One rule, used by the store and by every export, so a reader never has to know which of the two it
    is looking at - and so a corpus copied off a machine keeps working, since the rule travels with the
    files rather than with a manifest.
    """
    path = Path(image)
    return str(path.parent / f"{path.stem}.json")


def new_capture_id(moment: datetime | None = None) -> str:
    """``<timestamp>_<random>``: sortable, collide-proof, and carrying no identity."""
    stamp = (moment or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{secrets.token_hex(4)}"


def _checked_id(capture_id: str) -> str:
    """A capture id, or a refusal. The id indexes a filename, so it is checked like one."""
    text = str(capture_id or "").strip()
    if not text or len(text) > 64 or not all(c.isalnum() or c in "-_" for c in text):
        raise CorpusError(f"{capture_id!r} is not a capture id")
    return text


def _checked_partition(partition: str) -> str:
    text = str(partition or "").strip()
    if not text or text != os.path.basename(text) or text in {".", ".."}:
        raise CorpusError(f"{partition!r} is not a partition name")
    return text


def _identity_slug(identity: str) -> str:
    """A label that is safe as a directory name, and stable for one identity.

    Whitespace becomes ``_`` and anything else outside a conservative set is dropped, because a label
    arrives from a roster ("Seed Head Admin") and a directory is not the place to find out how the
    filesystem feels about that. Two identities can therefore collide in theory - ``list`` and ``stats``
    report the partition each capture is actually in, so a collision is visible rather than silent.
    """
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_" for character in identity.strip()
    )
    slug = "_".join(part for part in cleaned.split("_") if part)
    if not slug:
        raise CorpusError(f"identity {identity!r} has no characters usable in a directory name")
    return slug[:64]


# ---------------------------------------------------------------------------
# detector provenance
# ---------------------------------------------------------------------------
def detector_fingerprint(
    *,
    kind: str,
    model_path: str | os.PathLike[str] | None = None,
    pipeline: str | None = None,
    model_fingerprint: str | None = None,
    input_size: int,
    square: bool = False,
    tiles: int = 1,
    overlap: float = 0.2,
) -> dict[str, Any]:
    """What produced these landmarks: enough to tell two configurations apart, and no more.

    The digest is of the model *file*, so swapping two YuNet exports - same name, same input size,
    different weights - is a mismatch rather than a silent reinterpretation of every stored crop.
    """
    digest = model_fingerprint
    if digest is None and model_path is not None:
        digest = _file_digest(Path(model_path))
    return {
        "kind": str(kind),
        "pipeline": str(pipeline) if pipeline else None,
        "model": Path(model_path).name if model_path else None,
        "model_fingerprint": digest or None,
        "input_size": int(input_size),
        "square": bool(square),
        "tiles": int(tiles),
        "overlap": round(float(overlap), 4),
    }


def same_crop(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    """Do two fingerprints describe the same crop? Compare what *makes* a crop, not what it is called.

    The compared fields are the model's digest and the four settings that decide where the pixels come
    from: input size, square-or-aspect-matched, tiling and overlap. ``kind`` and ``pipeline`` are
    deliberately **not** compared - two names for one graph (``yunet`` and ``yunet-2023mar``) is a
    labelling difference, and it must not be able to make a reusable corpus look unusable; the digest is
    the identity that matters, and a changed digest is what a real detector swap looks like.

    An unknown digest on either side is a mismatch: ``None == None`` would make two corpora with no
    provenance look like one crop, which is the case where the answer has to be "we cannot tell".
    """
    if not left or not right:
        return False
    # ``.get`` rather than indexing: a fingerprint written by hand, or by an older build, may simply not
    # carry the key - and the answer to "is this the same crop" must be no, not a ``KeyError`` in the
    # middle of a corpus run.
    if not left.get("model_fingerprint") or not right.get("model_fingerprint"):
        return False

    def key(record: dict[str, Any]) -> tuple[Any, ...]:
        tiles = int(record.get("tiles", 1))
        return (
            record["model_fingerprint"],
            int(record.get("input_size") or 0),
            bool(record.get("square", False)),
            tiles,
            # An overlap with a single window is not a thing: one pass covers the frame whatever this
            # says, and calling the two crops different because a number nobody used differs is how a
            # reusable corpus gets refused. It matters only when there is more than one window.
            round(float(record.get("overlap", 0.0)), 4) if tiles > 1 else 0.0,
        )

    return key(left) == key(right)


def crop_description(fingerprint: dict[str, Any] | None) -> str:
    if not fingerprint:
        return "unknown detector"
    return (
        f"{fingerprint.get('kind') or fingerprint.get('pipeline')} "
        f"input={fingerprint.get('input_size')} tiles={fingerprint.get('tiles')} "
        f"square={fingerprint.get('square')} model={(fingerprint.get('model_fingerprint') or '?')[:12]}"
    )


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


# ---------------------------------------------------------------------------
# writing a capture
# ---------------------------------------------------------------------------
def downscale(image: Image.Image) -> tuple[Image.Image, tuple[int, int], tuple[float, float]]:
    """Cap the long edge at :data:`MAX_PX`, returning the frame, its size and the per-axis scale.

    Per-axis and *effective* (computed from the rounded integer size), exactly the way ``Letterbox``
    does it, so a landmark scaled forward and back does not drift by a pixel and a half.
    """
    width, height = int(image.width), int(image.height)
    if width <= 0 or height <= 0:
        raise CorpusError(f"a capture needs a real frame, got {width}x{height}")
    if MAX_PX <= 0 or max(width, height) <= MAX_PX:
        return image.copy(), (width, height), (1.0, 1.0)
    factor = MAX_PX / float(max(width, height))
    new_w = max(1, int(round(width * factor)))
    new_h = max(1, int(round(height * factor)))
    return (
        image.resize((new_w, new_h), Image.LANCZOS),
        (new_w, new_h),
        (new_w / float(width), new_h / float(height)),
    )


def prepare(image: Image.Image) -> Prepared:
    """Cap the frame at :data:`MAX_PX`. The crop the detector sees is the crop that gets stored."""
    frame, stored_size, scale = downscale(image)
    return Prepared(
        frame=frame,
        stored_size=stored_size,
        scale=scale,
        source_size=(int(image.width), int(image.height)),
    )


def scale_landmarks(
    points: Sequence[Sequence[float]] | np.ndarray, scale: Sequence[float]
) -> np.ndarray:
    """Native-frame landmarks -> stored-image landmarks. One multiply, stated, per axis."""
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return array * np.asarray(scale, dtype=np.float64).reshape(2)


def store(
    prepared: Prepared,
    *,
    detector: dict[str, Any],
    identity: str | None = None,
    landmarks: Sequence[Sequence[float]] | np.ndarray | None = None,
    box: Sequence[float] | None = None,
    score: float | None = None,
    source: str = SOURCE_IMPORT,
    consent: str = "",
    actor: str | None = None,
    note: str | None = None,
    camera: str | None = None,
    quality: dict[str, Any] | None = None,
    landmark_source: str = "detector",
    captured_at: str | None = None,
    capture_id: str | None = None,
) -> Capture:
    """Store a prepared frame and its crop provenance in the identity's partition. Returns the record.

    Order matters, and in the same direction ``retention`` argues: the frame is written first, then the
    sidecar, and a sidecar that cannot be written removes the frame again. The alternative - a record
    describing a frame that is not there - is the failure the retention module goes out of its way to
    avoid, and a corpus is no different: an image with no provenance is a face nobody can explain, and
    it is worse than a missing capture because a missing capture is countable.
    """
    if not consent:
        raise CorpusError(
            "a capture needs a stated consent basis; the corpus is a biometric store and 'nobody "
            "asked' is not one"
        )
    if landmarks is None:
        raise CorpusError(
            "a capture with no landmarks is a frame, not a corpus entry: the crop would have to be "
            "re-derived by whoever reads it, under whatever detector they have then"
        )
    try:
        points = np.asarray(landmarks, dtype=np.float64).reshape(5, 2)
    except (ValueError, TypeError) as exc:
        raise CorpusError(f"landmarks must be five (x, y) points: {exc}") from exc
    if not np.isfinite(points).all():
        raise CorpusError("landmarks are not finite")
    height, width = prepared.stored_size[1], prepared.stored_size[0]
    outside = (points[:, 0] < 0) | (points[:, 1] < 0) | (points[:, 0] > width) | (points[:, 1] > height)
    if outside.any():
        # The guard that catches a caller handing over *native-frame* coordinates for a frame that was
        # capped on the way in: those points describe a crop of an image nobody will open again, and the
        # export would either crop the wrong region or refuse much later, far from the mistake.
        raise CorpusError(
            f"landmarks fall outside the stored {width}x{height} frame (point "
            f"{points[int(np.argmax(outside))].tolist()}): they must be in the *stored* image's "
            "coordinates - run the detector on the prepared frame, or scale native points with "
            "``corpus.scale_landmarks``"
        )
    if box is not None:
        if len(box) != 4 or not np.isfinite(np.asarray(box, dtype=np.float64)).all():
            raise CorpusError("a box must be four finite numbers")
        if float(box[2]) <= 0 or float(box[3]) <= 0:
            raise CorpusError(f"a box needs a positive width and height, got {list(box)}")

    moment = captured_at or _now_text()
    identifier = _checked_id(capture_id) if capture_id else new_capture_id(_parse_time(moment))
    directory = partition_dir(identity_partition(identity))
    frame_path = os.path.join(directory, f"{identifier}.jpg")
    if os.path.exists(frame_path):
        raise CorpusError(f"{identifier} is already stored in {os.path.basename(directory)}")
    prepared.frame.save(frame_path, format="JPEG", quality=JPEG_QUALITY)
    digest = hashlib.sha256(Path(frame_path).read_bytes()).hexdigest()
    face_px = (
        float(min(box[2], box[3])) if box else _landmark_spread(points, prepared.scale)
    )
    record = Capture(
        capture_id=identifier,
        identity=str(identity) if identity else None,
        image=os.path.basename(frame_path),
        image_sha256=digest,
        stored_size=prepared.stored_size,
        source_size=prepared.source_size,
        scale=prepared.scale,
        landmarks=[[round(float(x), 3), round(float(y), 3)] for x, y in points],
        box=[round(float(value), 3) for value in box] if box else None,
        score=round(float(score), 5) if score is not None else None,
        face_px=round(face_px, 2),
        native_face_px=round(face_px / prepared.mean_scale, 2),
        detector=dict(detector),
        source=str(source),
        consent=str(consent),
        captured_at=str(moment),
        actor=str(actor) if actor else None,
        note=str(note) if note else None,
        camera=str(camera) if camera else None,
        quality=dict(quality or {}),
        landmark_source=str(landmark_source),
        label_history=(
            [{"at": str(moment), "by": str(actor) if actor else None, "from": None, "to": str(identity)}]
            if identity
            else []
        ),
    )
    try:
        _write_sidecar(record)
    except OSError:
        _remove(frame_path)
        raise
    return record


def _landmark_spread(points: np.ndarray, scale: Sequence[float]) -> float:
    """A face size for a capture that arrived without a box: eye-to-mouth distance, x2.

    Used by an operator import that supplied landmarks by hand, and named rather than guessed at so a
    reviewer can see where the number came from.
    """
    eyes = points[0:2]
    mouth = points[3:5]
    eye_mid = eyes.mean(axis=0)
    mouth_mid = mouth.mean(axis=0)
    return float(np.linalg.norm(eyes[0] - eyes[1]) + 2.0 * np.linalg.norm(eye_mid - mouth_mid))


def _write_sidecar(record: Capture) -> None:
    target = os.path.join(partition_dir(identity_partition(record.identity)), f"{record.capture_id}.json")
    temporary = f"{target}.tmp"
    Path(temporary).write_text(
        json.dumps(record.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, target)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def load(capture_id: str) -> Capture:
    """One capture by id. Raises rather than returning ``None``: a caller asking by id has one."""
    path = sidecar_path(capture_id)
    if not os.path.exists(path):
        raise CorpusError(f"no such capture: {capture_id}")
    return _parse(Path(path).read_text(encoding="utf-8"), where=path)


def sidecars(
    *, identity: str | None = None, unlabelled_only: bool = False, partition: str | None = None
) -> list[Capture]:
    """Every capture, optionally filtered. Sorted by capture time, then id, so runs compare."""
    wanted = None if partition is None else _checked_partition(partition)
    found: list[Capture] = []
    for name in partitions(existing_only=True):
        if wanted is not None and name != wanted:
            continue
        directory = partition_dir(name, create=False)
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".json"):
                continue
            path = os.path.join(directory, entry)
            try:
                record = _parse(Path(path).read_text(encoding="utf-8"), where=path)
            except (CorpusError, OSError) as exc:
                log.warning("skipping an unreadable sidecar (%s): %s", path, exc)
                continue
            if identity is not None and record.identity != identity:
                continue
            if unlabelled_only and record.identity is not None:
                continue
            found.append(record)
    found.sort(key=lambda record: (record.captured_at, record.capture_id))
    return found


def _parse(text: str, *, where: str) -> Capture:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"sidecar is not JSON ({where}): {exc}") from exc
    if not isinstance(payload, dict):
        raise CorpusError(f"sidecar is not an object ({where})")
    version = int(payload.get("version") or 0)
    if version not in READABLE_VERSIONS:
        raise CorpusError(
            f"sidecar {where} is version {version}, this build reads {list(READABLE_VERSIONS)}: "
            "refusing rather than guessing which fields moved, because the crop's provenance is in here"
        )
    try:
        landmarks = [[float(value) for value in pair] for pair in payload["landmarks"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise CorpusError(f"sidecar {where} has no usable landmarks: {exc}") from exc
    if len(landmarks) != 5 or any(len(pair) != 2 for pair in landmarks):
        raise CorpusError(f"sidecar {where} has {len(landmarks)} landmarks, expected 5")
    box = payload.get("box")
    return Capture(
        capture_id=str(payload["capture_id"]),
        identity=payload.get("identity"),
        image=str(payload["image"]),
        image_sha256=str(payload.get("image_sha256") or ""),
        stored_size=tuple(payload.get("stored_size") or (0, 0)),  # type: ignore[arg-type]
        source_size=tuple(payload.get("source_size") or (0, 0)),  # type: ignore[arg-type]
        scale=tuple(payload.get("scale") or (1.0, 1.0)),  # type: ignore[arg-type]
        landmarks=landmarks,
        box=[float(value) for value in box] if box else None,
        score=float(payload["score"]) if payload.get("score") is not None else None,
        face_px=float(payload.get("face_px") or 0.0),
        native_face_px=float(payload.get("native_face_px") or 0.0),
        detector=dict(payload.get("detector") or {}),
        source=str(payload.get("source") or SOURCE_IMPORT),
        consent=str(payload.get("consent") or ""),
        captured_at=str(payload["captured_at"]),
        actor=payload.get("actor"),
        note=payload.get("note"),
        landmark_source=str(payload.get("landmark_source") or "detector"),
        version=version,
        label_history=list(payload.get("label_history") or []),
        camera=payload.get("camera"),
        quality=dict(payload.get("quality") or {}),
    )


# ---------------------------------------------------------------------------
# labelling, and re-homing
# ---------------------------------------------------------------------------
def label(capture_id: str, identity: str | None, *, actor: str | None = None) -> Capture:
    """Attach (or clear) an identity, **moving the capture into that identity's partition**.

    The move is the layout doing its job: the partition is the label, so the two cannot disagree, and an
    operator listing the corpus sees who is in it without opening a file.

    It is also the one operation in this module that can lose a capture, so it is written against the
    two ways that can happen rather than the happy path:

    * **the frame is copied to a temporary name and renamed into place**, so an interrupted copy leaves
      a ``.tmp`` file - which no scan reads and the next attempt overwrites - instead of a truncated
      JPEG that looks like a capture;
    * **the destination is written and only then is the source removed**, so a crash anywhere leaves a
      *duplicate*, which ``stats`` counts and a human can see in two partitions, rather than the two
      states nothing can recover from: an image with no provenance, or a record pointing at a frame
      that moved away.

    The history is not bookkeeping for its own sake: a corpus is evidence for a threshold, and a label
    that changed without a trace is a label nobody can defend when the band it justified is questioned
    six months later. Clearing is supported because a mislabelled capture must be correctable - the
    alternative is a corpus that can only grow wrong.
    """
    record = load(capture_id)
    previous = record.identity
    record.identity = str(identity) if identity else None
    record.actor = actor or record.actor
    record.version = SIDECAR_VERSION
    record.label_history.append(
        {"at": _now_text(), "by": str(actor) if actor else None, "from": previous, "to": record.identity}
    )
    source_dir = os.path.dirname(sidecar_path(capture_id))
    target_dir = partition_dir(identity_partition(record.identity))
    if os.path.realpath(source_dir) == os.path.realpath(target_dir):
        _write_sidecar(record)
        return record

    source_frame = os.path.join(source_dir, f"{record.capture_id}.jpg")
    source_sidecar = os.path.join(source_dir, f"{record.capture_id}.json")
    target_frame = os.path.join(target_dir, f"{record.capture_id}.jpg")
    if os.path.exists(target_frame):
        raise CorpusError(
            f"{record.capture_id} already exists in {os.path.basename(target_dir)}: refusing to move "
            "one capture onto another"
        )
    if os.path.exists(source_frame):
        temporary = f"{target_frame}.tmp"
        try:
            with open(source_frame, "rb") as reader, open(temporary, "wb") as writer:
                writer.write(reader.read())
            os.replace(temporary, target_frame)
        except OSError:
            _remove(temporary)
            raise
    _write_sidecar(record)
    _remove(source_frame)
    _remove(source_sidecar)
    return record


def exists(capture_id: str) -> bool:
    """Is this capture in the store? For callers that are *asking*, not looking one up.

    Distinct from :func:`image_path`, which raises for an unknown id because a caller holding an id it
    wants to open has a bug if the capture is gone. This is the shape a report or a CLI wants.
    """
    try:
        return find_capture(capture_id) is not None
    except CorpusError:
        return False


def camera_of(capture_id: str) -> str | None:
    return load(capture_id).camera


# ---------------------------------------------------------------------------
# consuming the crop from another tool
# ---------------------------------------------------------------------------
def landmarks_for(
    image: str | os.PathLike[str], *, expect: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The stored crop for an image, or ``None`` when there is no sidecar beside it.

    ``expect`` is the detector configuration the caller *believes* it is measuring. A mismatch raises
    rather than returning the old geometry: this is the refusal that keeps a corpus from being reused
    under a pipeline it was not measured through (rule 2 in the module docstring).
    """
    path = sidecar_for(image)
    if not os.path.exists(path):
        return None
    record = _parse(Path(path).read_text(encoding="utf-8"), where=path)
    if expect is not None and not same_crop(record.detector, expect):
        raise CorpusError(
            "the stored crop was measured with a different detector configuration:\n"
            f"  stored:    {crop_description(record.detector)}\n"
            f"  requested: {crop_description(expect)}\n"
            "Reusing its landmarks would measure the stored detector while reporting the requested "
            "one. Re-detect explicitly if that is what you mean."
        )
    return {
        "landmarks": record.points,
        "box": record.box,
        "score": record.score,
        "identity": record.identity,
        "detector": record.detector,
        "face_px": record.face_px,
        "native_face_px": record.native_face_px,
        "capture_id": record.capture_id,
        "camera": record.camera,
        "quality": record.quality,
    }


# ---------------------------------------------------------------------------
# export: the folder contract the two tools read
# ---------------------------------------------------------------------------
def export(
    destination: str | os.PathLike[str],
    *,
    identities: Iterable[str] | None = None,
    include_unlabelled: bool = False,
    include_hard_cases: bool = False,
) -> dict[str, Any]:
    """Materialise ``destination/<identity>/<capture>.jpg`` plus the sidecar beside each image.

    The store's layout already *is* the folder contract, so this is a copy with two filters:

    * **hard cases are excluded by default.** A capture that is small, rolled or barely detected is
      exactly what a *coverage* experiment wants and exactly what a *band* must not be derived from -
      a threshold fitted to hard cases is a threshold for hard cases. ``include_hard_cases=True``
      puts them back, and the report says how many were left behind so a run of "200 images" cannot
      quietly be a run of 180.
    * **unlabelled captures are excluded** unless asked for, and land under ``unlabelled/``.

    Refuses to overwrite a file whose content differs. Names carry a timestamp and a random suffix, so a
    collision means somebody is exporting into a corpus that is already there - and merging two corpora
    by filename is exactly the accident that would put two people's captures under one label.
    """
    target_root = Path(destination)
    target_root.mkdir(parents=True, exist_ok=True)
    wanted = set(identities) if identities else None
    written: list[str] = []
    skipped_unlabelled: list[str] = []
    skipped_hard: list[str] = []
    for record in sidecars():
        if record.identity is None:
            if not include_unlabelled:
                skipped_unlabelled.append(record.capture_id)
                continue
            identity = "unlabelled"
        else:
            identity = record.identity
        if wanted is not None and identity not in wanted:
            continue
        if record.hard_case and not include_hard_cases:
            skipped_hard.append(record.capture_id)
            continue
        folder = target_root / _identity_slug(identity)
        folder.mkdir(parents=True, exist_ok=True)
        source_frame = Path(image_path(record.capture_id))
        if not source_frame.exists():
            raise CorpusError(f"capture {record.capture_id} has no frame on disk: {source_frame}")
        destination_frame = folder / f"{record.capture_id}.jpg"
        if destination_frame.exists():
            existing = hashlib.sha256(destination_frame.read_bytes()).hexdigest()
            if existing != record.image_sha256:
                raise CorpusError(
                    f"{destination_frame} already holds a different image. Export into a fresh "
                    "directory rather than merging two corpora by capture id."
                )
        else:
            destination_frame.write_bytes(source_frame.read_bytes())
        (folder / f"{record.capture_id}.json").write_text(
            json.dumps(record.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        written.append(str(destination_frame))
    report = {
        "destination": str(target_root),
        "images": len(written),
        "identities": len({Path(path).parent.name for path in written}),
        "unlabelled_skipped": len(skipped_unlabelled),
        "hard_cases_skipped": len(skipped_hard),
        "can_support_floor": stats(
            include_hard_cases=include_hard_cases, include_unlabelled=include_unlabelled
        )["can_support_floor"],
        "crop": crop_description(sidecars()[0].detector) if written else "empty",
    }
    return report


# ---------------------------------------------------------------------------
# statistics: whether this corpus can carry a measurement at all
# ---------------------------------------------------------------------------
def stats(*, include_hard_cases: bool = False, include_unlabelled: bool = False) -> dict[str, Any]:
    """What the corpus holds, in the terms a calibration decision is made in.

    Reported *before* a derivation run on purpose: a corpus that cannot support a floor should say so
    here, in one command, rather than four hours into a bootstrap. The pair counts are the ones the band
    rule consumes - genuine pairs are captures of one identity, impostor pairs are captures of different
    identities - and the face-size histogram is the camera-distance question made explicit.

    ``include_hard_cases`` and ``include_unlabelled`` say what the *measurement* would consume, so the
    default pair counts are the ones an export would produce rather than the ones the disk happens to
    hold. A corpus of 4 000 captures that exports 300 images is a 300-image corpus, and this is where
    that is visible.
    """
    records = sidecars()
    labelled = [
        record
        for record in records
        if record.identity and (include_hard_cases or not record.hard_case)
    ]
    unlabelled = [record for record in records if not record.identity]
    counts: dict[str, int] = {}
    for record in labelled:
        counts[record.identity] = counts.get(record.identity, 0) + 1  # type: ignore[index]
    genuine = sum(count * (count - 1) // 2 for count in counts.values())
    total_pairs = len(labelled) * (len(labelled) - 1) // 2
    impostor = total_pairs - genuine
    sizes = [record.face_px for record in labelled if record.face_px > 0]
    native_sizes = [record.native_face_px for record in labelled if record.native_face_px > 0]
    detectors: dict[str, int] = {}
    cameras: dict[str, int] = {}
    flags: dict[str, int] = {}
    for record in records:
        detectors[crop_description(record.detector)] = detectors.get(crop_description(record.detector), 0) + 1
        key = record.camera or "(unstated)"
        cameras[key] = cameras.get(key, 0) + 1
        for flag in record.flags:
            flags[flag] = flags.get(flag, 0) + 1
    hard = [record for record in records if record.hard_case]
    return {
        "captures": len(records),
        "measured": len(labelled),
        "labelled": len([record for record in records if record.identity]),
        "unlabelled": len(unlabelled),
        "identities": len(counts),
        "identity_counts": dict(sorted(counts.items())),
        "single_capture_identities": sorted(name for name, count in counts.items() if count < 2),
        "genuine_pairs": genuine,
        "impostor_pairs": impostor,
        "can_support_floor": impostor >= 100 and genuine > 0,
        "face_px": _histogram(sizes),
        "native_face_px": _histogram(native_sizes),
        "small_face_share": (
            round(sum(1 for size in sizes if size < 64) / len(sizes), 4) if sizes else 0.0
        ),
        "detectors": dict(sorted(detectors.items())),
        "cameras": dict(sorted(cameras.items())),
        "quality": {
            "flagged": len(hard),
            "flags": dict(sorted(flags.items())),
            "excluded_from_measurement": len([record for record in hard if record.identity]),
            "measured_with_hard_cases": bool(include_hard_cases),
        },
        "partitions": partitions(existing_only=True),
        "oldest": records[0].captured_at if records else None,
        "newest": records[-1].captured_at if records else None,
        "bytes": _bytes(),
        "consent": sorted({record.consent for record in records if record.consent}),
        "sources": sorted({record.source for record in records}),
    }


def _histogram(sizes: Sequence[float]) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for low, high in FACE_SIZE_BUCKETS:
        label = f"{low}-{high}" if high < 10_000 else f"{low}+"
        buckets[label] = sum(1 for size in sizes if low <= size < high)
    return buckets


def _bytes() -> int:
    total = 0
    for record in sidecars():
        try:
            total += os.path.getsize(os.path.join(os.path.dirname(sidecar_path(record.capture_id)),
                                                  f"{record.capture_id}.jpg"))
            total += os.path.getsize(sidecar_path(record.capture_id))
        except (OSError, CorpusError):
            continue
    return total


# ---------------------------------------------------------------------------
# erasure
# ---------------------------------------------------------------------------
def purge(
    *,
    older_than_days: int | None = None,
    identity: str | None = None,
    camera: str | None = None,
    capture_ids: Sequence[str] | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Erase frames (and their provenance) by age, identity, camera or id. Dry run by default.

    Dry run by *default*, not by an option an operator has to remember, and the reason is the same one
    ``retention`` gives: this function deletes faces, and the cost of the two commands is asymmetric - a
    dry run that nobody acts on costs a second, a real run that nobody meant costs a corpus.

    The order is ``retention``'s: **the frame first, then the record**. A record that outlives its frame
    describes a face that is gone; a frame that outlives its record is an unattributed face, and the
    store's own reader would never be able to explain it.
    """
    from retention import wipe_file  # one implementation of overwrite-and-unlink, imported not copied

    if not any(
        (older_than_days is not None, identity is not None, camera is not None, capture_ids is not None)
    ):
        raise CorpusError("purge needs one of: older_than_days, identity, camera, capture_ids")
    moment = now or datetime.now()
    wanted = set(capture_ids) if capture_ids else None
    chosen: list[Capture] = []
    for record in sidecars():
        if wanted is not None and record.capture_id not in wanted:
            continue
        if identity is not None and record.identity != identity:
            continue
        if camera is not None and (record.camera or "") != camera:
            continue
        if older_than_days is not None:
            captured = _parse_time(record.captured_at)
            if captured is None or captured > moment - timedelta(days=int(older_than_days)):
                continue
        chosen.append(record)

    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "captures": len(chosen),
        "bytes": 0,
        "identities": sorted({record.identity or "(unlabelled)" for record in chosen}),
        "older_than_days": older_than_days,
        "identity": identity,
        "camera": camera,
        "wiped": [],
    }
    for record in chosen:
        pair = (image_path(record.capture_id), sidecar_path(record.capture_id))
        directory = os.path.dirname(pair[0])
        try:
            report["bytes"] += sum(os.path.getsize(path) for path in pair if os.path.exists(path))
        except OSError:
            pass
        report["wiped"].append(record.capture_id)
        if dry_run:
            continue
        for path in pair:
            if os.path.exists(path):
                wipe_file(path, directory=directory)
    return report


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# the live capture path
# ---------------------------------------------------------------------------
#: Captures whose band verdict may carry a label. ``approved`` is the only verdict that *established* an
#: identity; ``pending_review`` is a question and ``refused`` is a null hypothesis, and neither is
#: evidence of who a person is. Both are still captured - they are the interesting failures and the
#: corpus should contain them - but unlabelled, for a human to decide.
LABEL_CARRYING_VERDICTS: Final = frozenset({"approved"})

#: Consent basis recorded on a capture taken from live traffic. Named once so every record carries the
#: same string, and so an operator reading the corpus can see exactly what was relied on.
#:
#: Kept as the basis for captures taken *before* per-worker consent existed, so an old sidecar reads
#: as what it was. New live captures must not use it: ``maybe_capture_punch`` now requires the
#: worker's own opt-in (``WORKER_CONSENT``), because a deployment switch states the operator's
#: intent to collect and nothing about the person in the frame.
LIVE_CONSENT: Final = "deployment: CALIBRATION_CAPTURE_ENABLED with worker notice"


def maybe_capture_punch(
    image: Image.Image,
    *,
    worker_id: str | None,
    verdict: str | None,
    site: str | None = None,
    camera: str | None = None,
    pipeline: str | None = None,
    gate: Any | None = None,
) -> str | None:
    """Capture a punch frame for calibration, if this deployment collects them *and this worker
    has opted in*. Never raises.

    Two switches, and both must be on - the deployment's (``CALIBRATION_CAPTURE_ENABLED``, the
    operator's statement that this site collects at all) and the worker's own (the newest row in
    ``corpus_capture_consents``, recorded through an audited endpoint). The first switch alone was
    the original design and it was not enough: a consent basis that says "the deployment decided"
    is a decision made about somebody, not by them, and the person in the frame is the one whose
    face is kept. A worker who has not opted in - or a punch from an unauthenticated path with no
    worker at all - is never captured, whatever the operator flipped.

    Deliberately quiet about
    everything else: this runs inside the punch path, and a measurement aid must not be able to fail a
    punch or change its answer - the same rule ``shadow``'s scorer follows, for the same reason.

    The capture re-runs the detector **on the stored (downscaled) copy**, so the landmarks it stores are
    in stored-image coordinates and reproduce exactly for anyone who opens that file later. That is worth
    one extra detection pass on an opt-in path: the alternative is landmarks in native coordinates that
    only mean the same thing if the reader reconstructs the same downscale.

    It is judged by the same gate the camera puller uses, and a frame that gate *discards* is not stored
    at all. A punch capture that arrived with no assessment would read as "assessed and fine" to every
    downstream tool - the one frame source nobody looks at would be the one that never gets flagged.
    """
    if not getattr(settings, "calibration_capture_enabled", False):
        return None
    # The worker's own half of the consent. A read per capture, through the same indexed lookup the
    # consent endpoint writes - and a *failed* read answers no, because the failure mode of a
    # consent check must never be "capture anyway".
    if not worker_consent(worker_id):
        return None
    try:
        import face_detector

        prepared = prepare(image)
        found = face_detector.detect_landmarks(prepared.frame)
        if len(found) != 1:
            # None or several faces: both are unusable as a *labelled* capture, and a human cannot label
            # what they cannot see, so it is not stored at all rather than stored unusably.
            log.debug("corpus: skipping a punch capture with %d faces", len(found))
            return None
        faced = found[0]
        box = faced.get("facial_area") and _box_of(faced["facial_area"])
        # The same gate the camera puller uses, applied to a live punch - imported here rather than at
        # module level because ``corpus_ingest`` is written on top of this module, and a live capture is
        # the *only* thing here that needs the policy. Without it a punch capture would arrive with no
        # assessment at all, which reads as "assessed and fine" to everybody downstream: the one frame
        # source that never gets looked at would be the one that never gets flagged.
        import corpus_ingest

        quality = corpus_ingest.assess(
            box=box,
            score=faced.get("confidence"),
            landmarks=faced["landmarks"],
            gate=gate,
        )
        if quality.discard:
            log.debug("corpus: skipping a punch capture the gate discarded (%s)", quality.reason)
            return None
        identity = str(worker_id) if worker_id and verdict in LABEL_CARRYING_VERDICTS else None
        record = store(
            prepared,
            detector=detector_fingerprint(
                kind=face_detector.DETECTOR_NAME,
                model_fingerprint=face_detector.fingerprint(),
                pipeline=pipeline or face_detector.active_pipeline(),
                input_size=getattr(face_detector, "DETECTOR_INPUT_SIZE", 320),
                tiles=1,
                overlap=0.0,
            ),
            identity=identity,
            landmarks=faced["landmarks"],
            box=box,
            score=faced.get("confidence"),
            quality=quality.as_dict(),
            landmark_source=face_detector.DETECTOR_NAME,
            source=SOURCE_PUNCH,
            consent=WORKER_CONSENT,
            actor="system:punch",
            camera=camera or site,
            note=f"verdict={verdict or 'unknown'}" + (f" site={site}" if site else ""),
        )
        return record.capture_id
    except Exception as exc:  # noqa: BLE001 - a punch must not fail over a measurement aid
        log.warning("corpus: could not capture a punch frame for %s: %s", worker_id, exc)
        return None


def _box_of(area: dict[str, Any]) -> list[float]:
    return [
        float(area.get("x") or 0),
        float(area.get("y") or 0),
        float(area.get("w") or 0),
        float(area.get("h") or 0),
    ]


# ---------------------------------------------------------------------------
# per-worker consent (the corpus's own opt-in record)
# ---------------------------------------------------------------------------
#: The table this module owns in the database. It is the one piece of the corpus that lives in a
#: table rather than a sidecar, because it must be *writable at capture time* (the punch path asks
#: it) and *append-only against tampering* (see migration 23's triggers) - neither is natural in a
#: file format the tools copy around.
CONSENT_TABLE: Final = "corpus_capture_consents"


def _consent_conn():
    """A write connection for the consent table, opened only by the functions that need one.

    Imported late, like every other use of the database in this module: ``corpus`` is importable
    by the offline tools, which run without the application's database.
    """
    from database import db

    return db


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def worker_consent(worker_id: str | None, conn=None) -> bool:
    """Has this worker opted in to calibration capture? The punch path asks this every capture.

    The newest row per worker *is* the state: a grant with ``revoked_at IS NULL`` is a yes,
    anything else - no row, or a withdrawal as the newest decision - is a no. Deliberately strict
    in that direction: the corpus is a biometric store, and the failure mode of answering "yes"
    on missing data is a face filed under a consent nobody can produce. ``conn`` lets a caller
    inside an open transaction reuse it; every other caller opens its own read-only connection.
    """
    if not worker_id:
        return False
    query = (
        f"SELECT granted, revoked_at FROM {CONSENT_TABLE} "
        "WHERE worker_id = ? ORDER BY id DESC LIMIT 1"
    )
    try:
        if conn is not None:
            row = conn.execute(query, (str(worker_id),)).fetchone()
            return bool(row and row["granted"] and row["revoked_at"] is None)
        with _db() as own:
            row = own.execute(query, (str(worker_id),)).fetchone()
            return bool(row and row["granted"] and row["revoked_at"] is None)
    except Exception:  # noqa: BLE001 - a capture path must not fail over a consent read
        return False


def _db():
    """A fresh read connection, through the application's own factory.

    Imported late so the offline tools can import this module without the database.
    """
    from database import db

    return db()


def record_consent(
    conn,
    *,
    worker_id: str,
    granted: bool,
    actor_id: str,
    note: str | None = None,
    ip: str | None = None,
    now: str | None = None,
) -> int:
    """Append one consent decision and make it the worker's current one. Returns the row id.

    The table is append-only *at the database level* (migration 23's triggers reject UPDATE and
    DELETE), so "current" is a property of the newest row rather than a flag to maintain: the
    newest row per worker is the state, and every older row is history that no query can rewrite.
    That is stronger than an ``is_current`` flag - a flag invites the exact UPDATE the trigger
    forbids - so the table has no flag at all, and reads use ``MAX(id)`` per worker.
    """
    moment = now or _now_text()
    cursor = conn.execute(
        f"INSERT INTO {CONSENT_TABLE} "
        "(worker_id, granted, note, created_at, created_by, created_ip, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(worker_id),
            1 if granted else 0,
            note,
            moment,
            str(actor_id),
            ip,
            None if granted else moment,
        ),
    )
    return int(cursor.lastrowid)


def consent_history(worker_id: str, conn=None) -> list[dict[str, Any]]:
    """The worker's consent decisions, newest first. The audit's answer, one SELECT long."""
    try:
        if conn is not None:
            rows = conn.execute(
                f"SELECT * FROM {CONSENT_TABLE} WHERE worker_id = ? ORDER BY id DESC",
                (str(worker_id),),
            ).fetchall()
        else:
            with _db() as own:
                rows = own.execute(
                    f"SELECT * FROM {CONSENT_TABLE} WHERE worker_id = ? ORDER BY id DESC",
                    (str(worker_id),),
                ).fetchall()
    except Exception:  # noqa: BLE001 - a reader reports, never raises
        return []
    return [
        {
            "id": int(row["id"]),
            "granted": bool(row["granted"]),
            "revoked": row["revoked_at"] is not None,
            "note": row["note"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "revoked_at": row["revoked_at"],
        }
        for row in rows
    ]


def consented_workers(conn=None) -> set[str]:
    """Every worker whose newest decision is a live grant - what the corpus's coverage reads."""
    try:
        if conn is not None:
            rows = conn.execute(
                f"SELECT worker_id FROM {CONSENT_TABLE} c "
                "WHERE granted = 1 AND revoked_at IS NULL "
                "AND id = (SELECT MAX(id) FROM corpus_capture_consents WHERE worker_id = c.worker_id)"
            ).fetchall()
        else:
            with _db() as own:
                rows = own.execute(
                    f"SELECT worker_id FROM {CONSENT_TABLE} c "
                    "WHERE granted = 1 AND revoked_at IS NULL "
                    "AND id = (SELECT MAX(id) FROM corpus_capture_consents WHERE worker_id = c.worker_id)"
                ).fetchall()
    except Exception:  # noqa: BLE001
        return set()
    return {str(row["worker_id"]) for row in rows}


def _box_of(area: dict[str, Any]) -> list[float]:
    return [
        float(area.get("x") or 0),
        float(area.get("y") or 0),
        float(area.get("w") or 0),
        float(area.get("h") or 0),
    ]
