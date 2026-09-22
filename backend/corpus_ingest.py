"""Filling the corpus from real traffic: sources, the detector, the quality gate, and the refusal.

WHY THIS IS NOT PART OF ``corpus`` (OR OF THE CLI)
--------------------------------------------------
``corpus`` is a *store*: it answers what a capture is, and it is deliberately unable to decide whether a
face is any good - a store that decided what may be measured would be deciding what a measurement may
be made of. This module is the opposite half: it is the only place that reads a camera, runs a detector,
and **decides whether a frame is allowed into the corpus**. Keeping the two apart is what lets the
quality policy be argued with, tested and changed without touching the format every other tool reads.

The CLI is a third thing again - an operator's command line. It calls in here; it does not reimplement
any of this. That matters because the failure this module exists to prevent is *two* ingestion paths
that disagree: the folder importer's idea of "a usable capture" and the RTSP puller's, drifting apart
until nobody can say which rule produced the corpus that a threshold was fitted to.

THE THREE OUTCOMES OF A FRAME
-----------------------------
1. **Stored** - filed under the identity (or ``_unlabelled``), carrying the crop, the detector
   configuration, the camera, and the quality assessment;
2. **Stored and flagged** - a *hard case*: real, kept, and explicitly marked. A corpus for measuring a
   coverage failure has to contain the failures, so the small, the rolled and the barely-detected are
   kept and named rather than silently dropped. ``export`` and ``stats`` leave them out of a
   *measurement* by default, which is the distinction that makes keeping them safe;
3. **Discarded** - below the floor at which a capture is evidence of nothing: no face, or a face so
   small or so far off-angle that the 160x160 template it would produce is not a picture of anybody.

The floor is what makes the middle case meaningful. "Quality gate" is usually said as if there were one
line; there are two, and they answer different questions - *can this be measured at all* (discard) and
*is this typical of the traffic we are calibrating for* (flag).

WHAT THE NUMBERS IN THE GATE MEAN, AND WHAT THEY DO NOT
-------------------------------------------------------
Confidence and size are direct measurements; pose is not, and pretending otherwise is how a corpus ends
up describing angles nobody verified:

* ``roll_deg`` is the angle of the eye line, and it is a real measurement - the eye line is the one
  facial axis that is visible in all five of the landmarks the detector returns;
* ``yaw`` is the nose's horizontal offset from the eye midpoint, in interocular units. It is a **monotone
  proxy**: it grows as the head turns, it is not a calibrated angle, and it cannot distinguish a turn of
  the head from an asymmetric detector response;
* ``pitch`` is the eye-to-nose distance over the eye-to-mouth distance. It is a proxy in the same sense,
  and a weaker one - it moves in opposite directions for a downward and an upward pitch, so the gate
  uses its *absolute deviation* and the flag says so.

All three are measured as deviations from :data:`REFERENCE_*`, which are the pose values **of the
alignment template itself** (``face_align.template_for(160)``). That is the only defensible zero point
available without a labelled 3D dataset: it is the frontal framing the embedder was trained to receive,
so "0.2 interocular units of yaw" means "a fifth of an eye separation away from the framing the model
expects" - a statement about the pipeline, not a claim about degrees of head rotation.

The gate's *flag* thresholds are set where a capture stops being typical; the *discard* thresholds sit
well past them, where the capture is not evidence of anything. Both are configuration, not physics, and
every capture records the gate that judged it so a corpus can be re-read later under a different one.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Iterator, Mapping, Protocol, Sequence

import numpy as np
from PIL import Image

import corpus

log = logging.getLogger("attendance.corpus.ingest")

#: Image suffixes a folder source will read. Named once so the CLI's help text and the walker agree.
IMAGE_SUFFIXES: Final = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


class IngestError(RuntimeError):
    """An ingestion that cannot be started or continued as asked."""


# ---------------------------------------------------------------------------
# the frontal reference: the model's own alignment framing
# ---------------------------------------------------------------------------
#: Measured from ``face_align.template_for(160)`` - the five points the embedder's alignment was built
#: for. Kept as literals with the derivation written down rather than recomputed at import, because a
#: quality gate that silently changed its zero point when the template moved would re-flag a whole
#: stored corpus. ``tests/test_corpus_ingest.py`` recomputes them and fails if the template has moved.
REFERENCE_ROLL_DEG: Final = -0.316912
REFERENCE_YAW: Final = 0.003178
REFERENCE_PITCH: Final = 0.494956

#: Landmark order, as ``detector_640`` and ``face_detector`` both emit it (YuNet's own order).
ORDER: Final = ("right_eye", "left_eye", "nose", "right_mouth", "left_mouth")


def pose_of(landmarks: Sequence[Sequence[float]] | np.ndarray) -> tuple[float, float, float]:
    """``(roll_deg, yaw, pitch)`` for five landmarks, in stored-image pixels.

    Degenerate geometries are a real case, not a theoretical one: a 12-pixel face can have its two eye
    points land on the same pixel, and an interocular distance of zero turns yaw into a division by zero.
    A ``ValueError`` is raised instead, because a caller that gates on these numbers must not be handed
    infinities - the frame is then skipped as unmeasurable, which is the honest outcome for a face the
    detector could not resolve.
    """
    points = np.asarray(landmarks, dtype=np.float64).reshape(5, 2)
    if not np.isfinite(points).all():
        raise ValueError("landmarks are not finite")
    right_eye, left_eye, nose, right_mouth, left_mouth = points
    interocular = float(np.linalg.norm(left_eye - right_eye))
    if interocular <= 0.0:
        raise ValueError("the eyes coincide: the face is too small to have a pose")
    delta = left_eye - right_eye
    angle = math.degrees(math.atan2(float(delta[1]), float(delta[0])))
    # Fold onto (-90, 90]: the two eyes are unordered by which is left *in the image*, so a frame
    # rotated past vertical would otherwise read as a 170-degree roll instead of a 10-degree one. The
    # gate is about "is this head upright enough to align", which is the folded quantity.
    if angle > 90.0:
        angle -= 180.0
    elif angle < -90.0:
        angle += 180.0
    eye_middle = (right_eye + left_eye) / 2.0
    mouth_middle = (right_mouth + left_mouth) / 2.0
    yaw = float((nose[0] - eye_middle[0]) / interocular)
    eye_to_mouth = float(np.linalg.norm(eye_middle - mouth_middle))
    if eye_to_mouth <= 0.0:
        raise ValueError("eye and mouth points coincide: the face is too small to have a pose")
    pitch = float(np.linalg.norm(eye_middle - nose) / eye_to_mouth)
    return angle, yaw, pitch


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GateConfig:
    """Two lines per measurement: where a capture stops being *typical*, and where it stops being usable.

    Read the pairs as (flag, discard). Both are in stored-image pixels for the size, which is the frame
    a later reader will open; the store records the native size beside it, so the camera-distance
    question stays answerable from the same record.
    """

    #: Below this, a capture is kept but flagged ``small_face``. 30 px is where a 160-pixel template
    #: starts being built mostly from interpolation - the number the rollout's coverage discussion uses.
    min_face_px: float = 30.0
    #: Below this, no crop is stored at all: at 16 px the five landmarks are inside one detector cell,
    #: so the "crop" would be a guess with a face printed nowhere in it.
    discard_face_px: float = 16.0
    #: YuNet's own default is 0.6; a capture under it is kept and flagged, because a weak detection that
    #: a human can still recognise is exactly the kind of frame a coverage experiment needs.
    min_score: float = 0.6
    discard_score: float = 0.30
    min_roll_deg: float = 15.0
    discard_roll_deg: float = 45.0
    #: Yaw in interocular units, as an absolute deviation from the template's own value.
    max_yaw: float = 0.20
    discard_yaw: float = 0.50
    #: Pitch as an absolute *relative* deviation from the template's ratio.
    max_pitch: float = 0.20
    discard_pitch: float = 0.45

    @classmethod
    def permissive(cls) -> "GateConfig":
        """The gate for a corpus whose *purpose* is the hard cases: keep everything a face was found in.

        Used by the folder importer, which is handed a controlled session rather than a camera feed -
        a calibration sitting where every frame is what the operator meant to take, and where dropping
        the dim ones would silently choose the corpus's difficulty for it.
        """
        return cls(
            min_face_px=0.0,
            discard_face_px=1.0,
            min_score=0.0,
            discard_score=0.0,
            min_roll_deg=180.0,
            discard_roll_deg=180.0,
            max_yaw=99.0,
            discard_yaw=99.0,
            max_pitch=99.0,
            discard_pitch=99.0,
        )

    def as_dict(self) -> dict[str, float]:
        return {key: float(value) for key, value in self.__dict__.items()}


@dataclass
class Quality:
    """What was measured about one frame, and what the gate concluded."""

    face_px: float
    score: float
    roll_deg: float
    yaw: float
    pitch: float
    flags: list[str] = field(default_factory=list)
    hard_case: bool = False
    discard: bool = False
    reason: str | None = None
    gate: dict[str, float] = field(default_factory=dict)
    pose_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "face_px": round(self.face_px, 2),
            "score": round(self.score, 4),
            "roll_deg": round(self.roll_deg, 3),
            "yaw": round(self.yaw, 4),
            "pitch": round(self.pitch, 4),
            "flags": list(self.flags),
            "hard_case": bool(self.hard_case),
            "gate": self.gate,
        }
        if self.pose_error:
            payload["pose_error"] = self.pose_error
        return payload


def assess(
    *,
    box: Sequence[float] | None,
    score: float | None,
    landmarks: Sequence[Sequence[float]] | np.ndarray,
    gate: GateConfig | None = None,
) -> Quality:
    """Measure one face against the gate. Pure: no I/O, so the policy is testable on its own numbers.

    A face whose pose cannot be computed is **discarded**, not flagged, and the reason names the
    failure: the pose numbers are what the gate would have judged, so a missing measurement has to be
    the strictest answer rather than the default one.
    """
    active = gate or GateConfig()
    points = np.asarray(landmarks, dtype=np.float64).reshape(5, 2)
    width = float(min(box[2], box[3])) if box is not None else _spread(points)
    measured = float(score) if score is not None else 0.0
    try:
        roll, yaw, pitch = pose_of(points)
        pose_error = None
    except ValueError as exc:
        return Quality(
            face_px=width,
            score=measured,
            roll_deg=0.0,
            yaw=0.0,
            pitch=0.0,
            flags=["unmeasurable"],
            hard_case=True,
            discard=True,
            reason="unmeasurable",
            gate=active.as_dict(),
            pose_error=str(exc),
        )

    yaw_deviation = abs(yaw - REFERENCE_YAW)
    pitch_deviation = abs(pitch / REFERENCE_PITCH - 1.0) if REFERENCE_PITCH else 0.0
    roll_deviation = abs(roll - REFERENCE_ROLL_DEG)

    flags: list[str] = []
    if width < active.min_face_px:
        flags.append("small_face")
    if measured < active.min_score:
        flags.append("low_confidence")
    if roll_deviation > active.min_roll_deg:
        flags.append("rolled")
    if yaw_deviation > active.max_yaw:
        flags.append("yawed")
    if pitch_deviation > active.max_pitch:
        flags.append("pitched")

    discard = False
    reason = None
    if width < active.discard_face_px:
        discard, reason = True, "too_small"
    elif measured < active.discard_score:
        discard, reason = True, "low_score"
    elif roll_deviation > active.discard_roll_deg:
        discard, reason = True, "extreme_roll"
    elif yaw_deviation > active.discard_yaw:
        discard, reason = True, "extreme_yaw"
    elif pitch_deviation > active.discard_pitch:
        discard, reason = True, "extreme_pitch"

    return Quality(
        face_px=width,
        score=measured,
        roll_deg=roll,
        yaw=yaw,
        pitch=pitch,
        flags=flags,
        hard_case=bool(flags),
        discard=discard,
        reason=reason,
        gate=active.as_dict(),
    )


def _spread(points: np.ndarray) -> float:
    """A face size for a detection that arrived without a box, mirroring ``corpus._landmark_spread``."""
    eyes, mouth = points[0:2], points[3:5]
    return float(
        np.linalg.norm(eyes[0] - eyes[1]) + 2.0 * np.linalg.norm(eyes.mean(axis=0) - mouth.mean(axis=0))
    )


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    """One frame as it arrives, before anything has been decided about it."""

    image: Image.Image
    name: str
    camera: str | None = None
    captured_at: str | None = None
    source: str = corpus.SOURCE_DIRECTORY
    meta: dict[str, Any] = field(default_factory=dict)


class Source(Protocol):
    """Anything that yields :class:`Frame` objects and can report what it could not read."""

    @property
    def camera(self) -> str | None: ...

    @property
    def skipped(self) -> list[tuple[str, str]]: ...

    def frames(self) -> Iterator[Frame]: ...


@dataclass
class FrameListSource:
    """Frames already in memory: what an API caller or a test hands over."""

    items: Sequence[Frame]
    _skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def camera(self) -> str | None:
        return next((item.camera for item in self.items if item.camera), None)

    @property
    def skipped(self) -> list[tuple[str, str]]:
        return self._skipped

    def frames(self) -> Iterator[Frame]:
        yield from self.items


@dataclass
class DirectorySource:
    """Images under a folder, in a deterministic order.

    Sorted, and sorted the same way twice: a corpus is evidence, and "which 40 of the 900 frames were
    imported" has to be answerable by re-running the command. Unreadable files are *reported* rather
    than raised on - one truncated JPEG on a card is not a reason to abandon a session - and the report
    keeps the reason, so a short import is visible as a short import.
    """

    root: str | Path
    camera: str | None = None
    recursive: bool = True
    #: An explicit list of images, instead of walking the root. Used by the folder importer, which has
    #: already decided which identity each image belongs to and must not have that decision re-derived
    #: here - a second, slightly different rule for "whose frame is this" is how one person's captures
    #: end up in two partitions.
    paths: Sequence[str | Path] | None = None
    _skipped: list[tuple[str, str]] = field(default_factory=list, repr=False)

    @property
    def skipped(self) -> list[tuple[str, str]]:
        return self._skipped

    def files(self) -> list[Path]:
        base = Path(self.root)
        if not base.exists():
            raise IngestError(f"source folder not found: {base}")
        if not base.is_dir():
            raise IngestError(f"source is not a folder: {base}")
        candidates = (
            [Path(path) for path in self.paths]
            if self.paths is not None
            else list(base.rglob("*") if self.recursive else base.glob("*"))
        )
        return sorted(
            path
            for path in candidates
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )

    def frames(self) -> Iterator[Frame]:
        self._skipped.clear()
        for path in self.files():
            try:
                # ``convert("RGB")`` matters for a folder of PNGs and 16-bit TIFFs: the detector takes
                # three channels, and a palette image would otherwise be decoded differently depending
                # on which library happened to open it.
                image = Image.open(path).convert("RGB")
            except (OSError, ValueError) as exc:
                self._skipped.append((str(path), f"unreadable: {exc}"))
                continue
            yield Frame(
                image=image,
                name=path.name,
                camera=self.camera,
                source=corpus.SOURCE_DIRECTORY,
                meta={"path": str(path)},
            )


@dataclass
class CameraSource:
    """A live camera or an RTSP stream, pulled frame by frame.

    **The open timeout is set, and the read timeout is not.** ``cv2.VideoCapture`` exposes an open
    timeout and nothing equivalent for ``read()``, so a stream that goes silent *after* it opened can
    block inside OpenCV with no way for this process to interrupt it. What can be done about that is
    done: a bounded number of consecutive failed reads ends the pull (a closed socket returns
    ``False``), and the caller is told how many frames it got. What cannot be done is claimed nowhere -
    the operator's answer to a hung gate camera is a ``timeout`` around the command, and the runbook
    says so.

    ``every`` samples the stream rather than taking every frame. A gate camera at 25 fps produces 90 000
    frames an hour, and a corpus does not want 90 000 pictures of the same four people walking past;
    it wants frames spread over the day, which is what sampling buys.
    """

    source: int | str
    camera: str | None = None
    limit: int | None = None
    every: int = 1
    open_timeout_ms: int = 3000
    max_consecutive_failures: int = 25
    warmup: int = 0
    _skipped: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit <= 0:
            raise IngestError("limit must be positive")
        if self.every < 1:
            raise IngestError("every must be at least 1")
        if self.camera is None and isinstance(self.source, str):
            # A URL with credentials in it must never reach a stored record: the corpus is copied off
            # machines and shared with reviewers, and a camera password in a sidecar is a leak with a
            # long half-life.
            self.camera = redact(self.source)
        elif self.camera is None and isinstance(self.source, int):
            self.camera = f"webcam:{self.source}"

    @property
    def skipped(self) -> list[tuple[str, str]]:
        return self._skipped

    def frames(self) -> Iterator[Frame]:
        import cv2  # imported here: a folder import must not need OpenCV's video backends loaded

        self._skipped.clear()
        capture = self._open(cv2)
        delivered = 0
        index = 0
        failures = 0
        try:
            for _ in range(max(0, int(self.warmup))):
                if not capture.read()[0]:
                    break
            while True:
                if self.limit is not None and delivered >= self.limit:
                    break
                ok, frame = capture.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= self.max_consecutive_failures:
                        self._skipped.append(
                            (self._label(), f"stream stopped: {failures} failed reads in a row")
                        )
                        break
                    continue
                failures = 0
                if index % self.every:
                    index += 1
                    continue
                index += 1
                delivered += 1
                yield Frame(
                    image=Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                    name=f"{self._label()}#{delivered}",
                    camera=self.camera,
                    captured_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    source=corpus.SOURCE_WEBCAM if isinstance(self.source, int) else corpus.SOURCE_RTSP,
                    meta={"index": index - 1},
                )
        finally:
            capture.release()

    def _label(self) -> str:
        return str(self.camera or self.source)

    def _open(self, cv2: Any) -> Any:
        params: list[int] = []
        target: Any = self.source
        if isinstance(self.source, str):
            # Only the FFMPEG backend honours these; asking for them on a device index is a no-op that
            # some builds warn about, so they are passed only for a URL.
            params = [int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), int(self.open_timeout_ms)]
            capability = getattr(cv2, "CAP_FFMPEG", None)
            if capability is not None:
                capture = cv2.VideoCapture(target, capability, params)
            else:
                capture = cv2.VideoCapture(target, params)
        else:
            capture = cv2.VideoCapture(int(target))
        if capture is None or not capture.isOpened():
            if capture is not None:
                capture.release()
            raise IngestError(
                f"could not open {self._label()}"
                + (f" within {self.open_timeout_ms} ms" if params else "")
                + ": for an RTSP stream, check the URL, the credentials and that the host is reachable "
                "from here"
            )
        return capture


def redact(url: str) -> str:
    """An RTSP/HTTP URL with any credentials removed, for logs, records and camera labels.

    Kept as a *labelled* camera rather than a bare host so a corpus captured from several gates keeps
    them apart - and stripping the userinfo is the whole point: ``rtsp://admin:hunter2@10.0.0.9/gate``
    becomes ``rtsp://10.0.0.9/gate``, which identifies the camera and not the credentials.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    return re.sub(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://[^/@]*@", r"\g<scheme>://", text)


# ---------------------------------------------------------------------------
# the detector, bound to the policy that decides the crop
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DetectorSpec:
    """Which detector, at which input size, with which tiling - and the fingerprint that records it.

    A spec rather than a callable, so the object that runs cannot disagree with the fingerprint that is
    stored: ``build_detector`` already binds tiling for this reason (a call site that constructs a
    detector and forgets ``tiles=2`` silently reverts the coverage fix), and this extends the same
    argument to the *record*. ``build`` is lazy, so importing this module - which the CLI does for
    ``--help`` - never needs an ONNX model on disk.
    """

    kind: str = "yunet"
    model_path: str | Path | None = None
    input_size: int = 640
    tiles: int = 1
    overlap: float = 0.2
    square: bool = False
    score_threshold: float = 0.6
    model_fingerprint: str | None = None

    def build(self) -> Callable[[np.ndarray], list[Any]]:
        import detector_640

        if self.model_path is None:
            raise IngestError("a detector spec needs a model path")
        return detector_640.build_detector(
            self.kind,
            self.model_path,
            input_size=self.input_size,
            tiles=self.tiles,
            overlap=self.overlap,
            square=self.square,
            score_threshold=self.score_threshold,
        )

    def fingerprint(self) -> dict[str, Any]:
        return corpus.detector_fingerprint(
            kind=self.kind,
            model_path=self.model_path,
            model_fingerprint=self.model_fingerprint,
            input_size=self.input_size,
            square=self.square,
            tiles=self.tiles,
            overlap=self.overlap,
        )


def as_bgr(image: Image.Image | np.ndarray) -> np.ndarray:
    """OpenCV's input contract, stated: **BGR**, ``uint8``, three channels.

    Unlike the embedder's contract - which nothing in an ONNX file records and which the A/B tool exists
    to *measure* - this one is documented by OpenCV, and YuNet's ONNX wrapper consumes whatever array it
    is handed. So the conversion is made here, once, explicitly, instead of being left to whichever
    library decoded the frame.
    """
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] != 3:
        raise IngestError(f"a frame must be three-channel, got shape {array.shape}")
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(array[:, :, ::-1])


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------
@dataclass
class IngestReport:
    """What an ingestion did, frame by frame, in the terms the next command needs."""

    stored: list[corpus.Capture] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    frames: int = 0
    identity: str | None = None
    camera: str | None = None
    detector: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, float] = field(default_factory=dict)

    @property
    def hard_cases(self) -> list[str]:
        return [record.capture_id for record in self.stored if record.hard_case]

    def counts(self) -> dict[str, int]:
        """Every frame that arrived, attributed to exactly one outcome. The accounting is the point."""
        counted: dict[str, int] = {"frames": self.frames, "stored": len(self.stored)}
        for record in self.stored:
            for flag in record.flags:
                counted[flag] = counted.get(flag, 0) + 1
        for item in self.skipped:
            counted[item["reason"]] = counted.get(item["reason"], 0) + 1
        return counted

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "stored": len(self.stored),
            "captures": [record.capture_id for record in self.stored],
            "hard_cases": len(self.hard_cases),
            "identity": self.identity,
            "camera": self.camera,
            "detector": self.detector,
            "gate": self.gate,
            "counts": self.counts(),
            "skipped": self.skipped,
        }

    def summary(self) -> str:
        if not self.frames:
            return "no frames arrived"
        counts = self.counts()
        parts = [f"stored {counts.get('stored', 0)} capture(s) of {self.frames} frame(s)"]
        flags = sorted(
            (name, value) for name, value in counts.items() if name not in {"frames", "stored"}
        )
        if flags:
            parts.append("flagged " + ", ".join(f"{value} {name}" for name, value in flags))
        return "; ".join(parts)


def ingest(
    source: Source,
    *,
    detector: DetectorSpec,
    identity: str | None = None,
    consent: str,
    actor: str | None = None,
    camera: str | None = None,
    gate: GateConfig | None = None,
    choose: str = "largest",
    keep_hard_cases: bool = True,
    note: str | None = None,
    limit: int | None = None,
) -> IngestReport:
    """Detect, judge and file every frame a source yields. The one ingestion path.

    ``choose`` is how a frame with more than one face is handled - ``largest`` (the subject is the person
    standing at the gate) or ``skip`` (a corpus whose *point* is unambiguous identity). Neither is right
    for every corpus, so it is a parameter rather than a judgement this code makes quietly.

    ``consent`` is required, exactly as ``corpus.store`` requires it: this is the code that fills a
    biometric store from a live camera, and "nobody asked" must not be expressible by forgetting a flag.
    """
    if not consent:
        raise IngestError(
            "ingestion needs a stated consent basis: the corpus is a biometric store, so the command "
            "that fills it records what was relied on"
        )
    if choose not in {"largest", "skip"}:
        raise IngestError(f"unknown multi-face policy {choose!r}; expected 'largest' or 'skip'")
    active = gate or GateConfig()
    fingerprint = detector.fingerprint()
    detection = detector.build()
    report = IngestReport(identity=identity, detector=fingerprint, gate=active.as_dict())
    report.camera = camera or source.camera

    for frame in source.frames():
        if limit is not None and len(report.stored) >= int(limit):
            break
        report.frames += 1
        try:
            prepared = corpus.prepare(frame.image)
        except (corpus.CorpusError, OSError, ValueError) as exc:
            report.skipped.append(_skip(frame, "unreadable", str(exc)))
            continue
        try:
            found = detection(as_bgr(prepared.frame))
        except Exception as exc:  # noqa: BLE001 - one bad frame must not end a folder
            report.skipped.append(_skip(frame, "detector_failed", str(exc)))
            continue
        if not found:
            report.skipped.append(_skip(frame, "no_face", "nothing detected at this input size"))
            continue
        if len(found) > 1 and choose == "skip":
            report.skipped.append(
                _skip(frame, "many_faces", f"{len(found)} faces detected, need exactly 1")
            )
            continue
        best = max(found, key=lambda item: item.area())
        quality = assess(box=best.box, score=best.score, landmarks=best.landmarks, gate=active)
        if quality.discard:
            report.skipped.append(_skip(frame, f"quality:{quality.reason}", quality.pose_error or ""))
            continue
        if quality.hard_case and not keep_hard_cases:
            report.skipped.append(
                _skip(frame, "quality:hard_case", ",".join(quality.flags))
            )
            continue
        extra = f"faces={len(found)}" if len(found) > 1 else None
        try:
            record = corpus.store(
                prepared,
                detector=fingerprint,
                identity=identity,
                landmarks=best.landmarks,
                box=best.box,
                score=best.score,
                source=frame.source,
                consent=consent,
                actor=actor,
                camera=camera or frame.camera or source.camera,
                quality=quality.as_dict(),
                landmark_source=detector.kind,
                captured_at=frame.captured_at,
                note="; ".join(part for part in (note, frame.name, extra) if part),
            )
        except (corpus.CorpusError, OSError) as exc:
            report.skipped.append(_skip(frame, "store_failed", str(exc)))
            continue
        report.stored.append(record)
    for name, reason in source.skipped:
        report.skipped.append({"frame": name, "reason": "source", "detail": reason})
    return report


def _skip(frame: Frame, reason: str, detail: str = "") -> dict[str, Any]:
    return {"frame": frame.name, "reason": reason, "detail": detail}


# ---------------------------------------------------------------------------
# enrolment: a real worker id against a captured reference set
# ---------------------------------------------------------------------------
def enroll(
    source: Source,
    *,
    identity: str,
    detector: DetectorSpec,
    consent: str,
    actor: str | None = None,
    camera: str | None = None,
    gate: GateConfig | None = None,
    roster: Callable[[str], Mapping[str, Any] | None] | None = None,
    check_roster: bool = False,
    keep_hard_cases: bool = True,
    limit: int | None = None,
) -> IngestReport:
    """Bind captures to a **worker id**, optionally verifying that the id is real before anything is written.

    A corpus labelled by hand is a corpus of strings: ``bilal_khan`` and ``BK-1042`` and ``Bilal Khan``
    are three identities holding one person's face, and every pair count a band is derived from is wrong
    in a way nothing downstream can detect. Enrolment is the operation that prevents it, and the
    roster check is what makes "a real worker id" a promise rather than an instruction:

    * ``roster`` is a lookup returning the worker record for an id, or ``None``. Injected rather than
      imported so this module stays independent of the schema - and so the check is testable without one.
    * ``check_roster`` **refuses** on an unknown id. It never substitutes a name from the roster: the
      label is whatever the operator said, and a lookup that silently rewrote it would make the corpus's
      identity field a function of the database's contents at import time.

    A deactivated-but-present worker is accepted deliberately, with a note: their reference set may be
    enrolled before they start, and refusing could strand an already-captured session. Whether they may
    *clock in* is a different question, answered elsewhere.
    """
    if check_roster:
        if roster is None:
            raise IngestError("cannot check the roster without a roster lookup")
        record = roster(identity)
        if record is None:
            raise IngestError(
                f"no worker {identity!r} in the roster: refusing to enrol, because a corpus labelled "
                "with an id that does not exist is a pair count nobody can reconcile later"
            )
    report = ingest(
        source,
        detector=detector,
        identity=identity,
        consent=consent,
        actor=actor,
        camera=camera,
        gate=gate,
        keep_hard_cases=keep_hard_cases,
        note="enrolment",
        limit=limit,
    )
    return report


# ---------------------------------------------------------------------------
# what a corpus looks like next to the gate it is meant to model
# ---------------------------------------------------------------------------
def coverage(
    *,
    expected_face_px: float | None = None,
    native: bool = True,
    detector: DetectorSpec | None = None,
    gate: GateConfig | None = None,
) -> dict[str, Any]:
    """How much of this corpus sits where a gate camera actually puts faces.

    The corpus exists to answer a question about *distance*, and the failure mode of building one is
    quietly collecting the easy half: staff who happened to walk close to the camera. This reports the
    small-face share against a stated expectation, plus - when a detector spec is given - the smallest
    face that detector could have found at the frame sizes involved, so "we have no small faces" can be
    told apart from "our detector cannot see small faces".
    """
    active = gate or GateConfig()
    key = "native_face_px" if native else "face_px"
    records = [record for record in corpus.sidecars() if getattr(record, key, 0) > 0]
    if not records:
        return {"captures": 0, "verdict": "empty"}
    sizes = sorted(float(getattr(record, key)) for record in records)
    small = [size for size in sizes if size < active.min_face_px]
    payload: dict[str, Any] = {
        "captures": len(records),
        "regime": "native" if native else "stored",
        "min_px": round(sizes[0], 2),
        "median_px": round(sizes[len(sizes) // 2], 2),
        "max_px": round(sizes[-1], 2),
        "below_gate_px": len(small),
        "below_gate_share": round(len(small) / len(sizes), 4),
    }
    if expected_face_px is not None:
        within = [size for size in sizes if size <= expected_face_px]
        payload["expected_face_px"] = float(expected_face_px)
        payload["at_or_below_expected"] = len(within)
        payload["verdict"] = (
            "usable"
            if len(within) >= 20
            else "thin: fewer than 20 captures at or below the expected face size - the corpus is "
            "mostly the easy regime, so a band derived from it will look better than the gate does"
        )
    if detector is not None:
        payload["detector"] = detector.fingerprint()
    return payload
