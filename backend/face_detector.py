"""YuNet: the face detector, and the only place that knows how a face is cropped.

WHY THIS EXISTS
---------------
Detection was the dominant cost of a punch. Measured on the deployment-class host, at the
application's own 640x640 working size, against the detector this app used before (MTCNN,
through DeepFace):

    MTCNN detection                     308 ms
    YuNet detection                      10 ms     <- 30.4x
    full verification (detect + embed)  537 ms -> 209 ms   <- 2.6x, 61% of the call

Detection was ~70% of a verification; the VGG-Face embedding (~189 ms) is now the larger
half. YuNet is a 232 KB ONNX model run by OpenCV's own DNN, so it needs no new dependency
and no TensorFlow graph for the detection half.

WHY IT IS NOT IN ``face_engine``
--------------------------------
``face_engine`` is the pool and the queue; this is what one job does. Keeping the detector
here also keeps the "only the engine calls the models" rule honest: this module names no
model library, so the source scan in ``tests/test_face_engine.py`` still finds exactly one
place where the face models are called.

THE CROP IS THE CONTRACT
------------------------
A template is an embedding of a *crop*, so changing the crop changes every stored template.
Measured agreement between the two detectors' boxes is IoU 0.814 - a ~19% different crop -
so an enrolment made under MTCNN does **not** match a punch scored under YuNet closely
enough to trust. That is what ``PIPELINE`` is for: it is written beside every template, and
a template from an older pipeline is reported as needing re-enrolment rather than silently
producing bad scores (see ``biometrics`` and ``main``).

The same change moves the *distances* themselves, so the crop owns the decision lines too -
see ``MatchBand`` below: one band per ``(pipeline, model)``, each derived from boundaries
measured through that pipeline, none of them inherited. A template's provenance is what a
punch is scored against, and a build that can run a pipeline it has no band for refuses to
start rather than approve against numbers nobody derived.

LANDMARK ALIGNMENT
------------------
YuNet reports five landmarks per face. They are aligned onto the standard 112x112 template
with a partial affine transform - the same normalisation OpenCV's own recognition sample
performs - so the crop is upright and scale-normalised before it is embedded, rather than
whatever rectangle the face happened to occupy in the frame.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

#: The pipeline that produced a template. Bump this whenever the crop changes - a different
#: detector, a different alignment, a different model - because every stored template becomes
#: stale at that moment and must be re-enrolled.
#:
#: A bump is **not** complete until the decision lines below have been re-derived for the new
#: name: ``band_for`` has no default, the startup gate refuses to serve without a band for the
#: pipeline that would really run, and a test asserts that every pipeline this build can run
#: is covered. A crop change cannot quietly inherit the previous one's thresholds.
PIPELINE = "yunet-2023mar"

#: What this detector is called in telemetry and in readiness output.
DETECTOR_NAME = "yunet"

#: What runs instead when the model is not on disk: DeepFace's previous detector, which is
#: slower but needs nothing fetched. Named here so that "which one is live" is answerable in
#: one place rather than from a boolean that each caller interprets differently.
FALLBACK_DETECTOR = "mtcnn"
FALLBACK_PIPELINE = "mtcnn"

#: Where the model lives. ``FACE_DETECTOR_MODEL_PATH`` overrides it, the same way
#: ``LIVENESS_MODEL_PATH`` does for the liveness model.
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"

#: The five-point template YuNet's landmarks are aligned onto, for a 112x112 crop. YuNet
#: reports (right eye, left eye, nose, right mouth corner, left mouth corner) in this order.
_ALIGN_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)
ALIGNED_SIZE = 112

#: Below this, a detection is noise. OpenCV's sample default is 0.5.
SCORE_THRESHOLD = 0.6

#: The narrowest detection that can be the person standing at the gate, in frame pixels.
#:
#: The alignment template is ``ALIGNED_SIZE`` (112 px) and the punch band (0.7-1.5 m, see the framing
#: coach) puts a subject at 106-226 px in a 1280-wide frame, so a detection a quarter of the template
#: is not a smaller subject - it is a *crop with no face in it*, upscaled from interpolation. YuNet at
#: score 0.6 does return such detections: a poster, a photograph on a wall, a face on a phone screen
#: across the room. They were being counted as people, and ``compare_faces_sync`` refuses a frame with
#: more than one face - so a speck 12 px wide anywhere in the picture could stop a worker clocking in
#: with "more than one face is in the photo". Named here because the number is about this pipeline's
#: own crop, not about detectors in general.
MIN_SUBJECT_PX = 24

#: How much two boxes must overlap before they are the same face found twice.
#:
#: A detector run over a frame at an unusual scale - or over tiles that overlap - can emit two boxes
#: for one face, and this deployment raises the punch frame ceiling to 1280 (YuNet's own heads are
#: anchored for much smaller inputs), so it does happen. Two boxes over one face are one person; the
#: caller that refuses "more than one face" must merge them first, or a close-up selfie is refused
#: for being a crowd. IoU alone is not enough (a box nested inside a slightly larger one of the same
#: face has a low IoU because the *union* is large), which is why containment is checked too.
DUPLICATE_IOU = 0.45
DUPLICATE_CONTAINMENT = 0.6

#: The size the graph is driven at. It sat in the constructor as ``(320, 320)``, which is how this
#: deployment's coverage failure stayed invisible: 320 is a *scale*, not a setting, and at that scale a
#: face 60 native pixels wide arrives at the detector as 15 - below the anchor stride, so it is not
#: found at all. Named, because anything that records or reasons about "which crop is this" needs the
#: number, and a literal in a constructor is not a number anybody can cite. See
#: ``detector_640.min_detectable_width`` for the arithmetic and ``corpus`` for the provenance that
#: travels with every stored crop.
DETECTOR_INPUT_SIZE = 320

_lock = threading.Lock()
_detector: Any = None
_load_error: str | None = None
_loaded = False


def model_path() -> Path:
    """The configured model file. Read at call time so a test can point it elsewhere."""
    try:
        import config

        override = getattr(config.settings, "face_detector_model_path", None)
        if override:
            return Path(override)
    except Exception:  # pragma: no cover - config is importable everywhere else
        pass
    return DEFAULT_MODEL_PATH


def _fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def available() -> tuple[bool, str]:
    """Whether this detector can run, and why not when it cannot.

    Never raises: a checkout without the model keeps working on the previous path, exactly
    as it does without the liveness model. ``readiness`` reports which one is live.
    """
    ok, reason = _ensure()
    return ok, reason


def _ensure() -> tuple[bool, str]:
    """Load the model once, on first use. Returns ``(ok, reason)``.

    The detector object is shared and ``setInputSize`` mutates it, so every call takes the
    lock - YuNet costs ~10 ms, so serialising it costs far less than the MTCNN detection
    this replaced.
    """
    global _detector, _load_error, _loaded
    with _lock:
        if _loaded:
            return (_detector is not None), (_load_error or "")
        _loaded = True
        path = model_path()
        if not path.exists():
            _load_error = f"face detector model not found at {path}"
            log.info("face detector unavailable: %s", _load_error)
            return False, _load_error
        expected = _configured_sha256()
        if expected and _fingerprint(path) != expected:
            _load_error = "face detector model does not match FACE_DETECTOR_MODEL_SHA256"
            log.error(_load_error)
            return False, _load_error
        try:
            import cv2

            _detector = cv2.FaceDetectorYN.create(
                str(path), "", (DETECTOR_INPUT_SIZE, DETECTOR_INPUT_SIZE),
                SCORE_THRESHOLD, 0.3, 5000
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised at import time
            _load_error = f"{type(exc).__name__}: {exc}"
            log.error("face detector failed to load: %s", _load_error)
            _detector = None
            return False, _load_error
        log.info("face detector ready: %s (%s)", path.name, DETECTOR_NAME)
        return True, ""


def _configured_sha256() -> str:
    try:
        import config

        return str(getattr(config.settings, "face_detector_model_sha256", "") or "")
    except Exception:  # pragma: no cover
        return ""


def fingerprint() -> str:
    """The loaded model's fingerprint, for readiness. Empty when there is no model."""
    return _fingerprint(model_path())


def active() -> tuple[str, str]:
    """``(detector, pipeline)`` for what would actually run right now.

    The distinction this exists for: ``DETECTOR_NAME``/``PIPELINE`` are what this build is
    *for*, and this is what a deployment is *doing*. On a host without the model the
    application detects with the previous detector - and a template written there was made
    from a different crop of the face, so recording the intended name instead of the
    effective one would label an MTCNN embedding as a YuNet one and lose the very
    mismatch this provenance exists to catch.
    """
    ok, _reason = _ensure()
    return (DETECTOR_NAME, PIPELINE) if ok else (FALLBACK_DETECTOR, FALLBACK_PIPELINE)


def active_pipeline() -> str:
    """The pipeline a template written right now would be made by."""
    return active()[1]


def active_detector() -> str:
    """The detector a verification right now would use."""
    return active()[0]


# ---------------------------------------------------------------------------
# the decision lines, and the pipeline that owns them
# ---------------------------------------------------------------------------
# A distance on its own says nothing: it is *approved*, *logged for review* or *refused*
# only against a line, and a line is only meaningful for the crop and model that produced
# both vectors. So the lines live here, keyed by exactly that.
#
# WHY THEY ARE DERIVED RATHER THAN CHOSEN
# ---------------------------------------
# They used to be 0.40 and 0.60, which are DeepFace's published numbers for *its* crop -
# inherited by this application, never re-derived when the crop changed, and never tied to
# anything. Measured on the deployment's own material and on a public multi-identity corpus
# through this pipeline's own code path (2026-09-20), that pair was inside the window but
# for no recorded reason, and the next crop change would have inherited it again.
#
# The two boundaries that can be measured are:
#
# * the **genuine ceiling** - the *highest* distance seen between a photograph and the
#   template of the same person, in this deployment's own capture conditions;
# * the **impostor floor** - the *lowest* distance seen between two different people.
#
# Neither is the true boundary of its distribution, so each line is placed at a stated
# distance from the measurement that stands for it: the approve line at the geometric
# centre of the measured window (the point that maximises the margin to both boundaries,
# in the space the distance itself lives in), and the refuse line a factor below the
# impostor floor. Everything between the two is review - logged and routed to a human -
# which is what the middle outcome is for: a distance this deployment has never produced
# for either class is a question, not a verdict.
#
# ``derived()`` recomputes both lines from the evidence beside them, and a test asserts
# that the shipped numbers are what that rule produces: a line cannot be edited without
# editing the measurement it came from.
MATCH_APPROVED = "approved"
MATCH_REVIEW = "review"
MATCH_REFUSED = "refused"

#: The granularity a derived line is rounded to. A chosen constant carries as many decimal
#: places as the person who typed it; a derived one must not - 0.397 and 0.40 are the same
#: decision, and only one of them pretends to be more precise than the measurement is.
LINE_GRANULARITY = 0.05

#: How far below the closest observed impostor the refuse line sits. Above 1.0 by this
#: much, not level with it: the corpora are clean, frontal and evenly lit, which is the
#: *easiest* end of impostors, so the observed floor is an upper bound on the real one.
IMPOSTOR_MARGIN = 1.35


def _line(value: float) -> float:
    """A derived line, rounded to the granularity a decision is actually made at."""
    return round(round(value / LINE_GRANULARITY) * LINE_GRANULARITY, 2)


@dataclass(frozen=True)
class MatchBand:
    """Where one pipeline's distances approve, go to a human, and are refused.

    The evidence travels with the lines rather than beside them in a comment, because the
    numbers are only defensible together: ``basis()`` is computed from the two boundaries
    this band was derived from, so a line and the measurement behind it cannot disagree.
    """

    #: The recognition model the lines were measured in, beside the crop. The crop decides
    #: which pixels are embedded and the model decides what the vector means, so a band is
    #: only about a *pair* - see ``band_for``.
    model: str
    #: At or below this, the capture is accepted without a human.
    approve: float
    #: Above ``approve`` and at or below this, it is logged and routed to review.
    review: float
    #: The highest same-person distance measured for this pipeline. What the approve line
    #: has to clear.
    genuine_ceiling: float
    #: The lowest different-people distance measured for this pipeline. What the refuse
    #: line has to stay below.
    impostor_floor: float
    #: What the two boundaries rest on: which pairs, how many, where they came from.
    evidence: str

    def classify(self, distance: float) -> str:
        """One of ``MATCH_APPROVED`` / ``MATCH_REVIEW`` / ``MATCH_REFUSED``.

        Boundaries are inclusive, and stated once, here: a caller that compares against
        ``approve``/``review`` itself is a caller that can disagree with this module about
        whether 0.50 is a review or a refusal.
        """
        if distance <= self.approve:
            return MATCH_APPROVED
        if distance <= self.review:
            return MATCH_REVIEW
        return MATCH_REFUSED

    def derived(self) -> tuple[float, float]:
        """``(approve, review)`` as the stated rule produces them from the evidence.

        ``review`` is clamped up to ``approve``, and that clamp is load-bearing rather than
        defensive. The two lines are placed by rules that are only *usually* ordered: ``approve``
        is the geometric centre of the measured window and ``review`` sits ``IMPOSTOR_MARGIN``
        below the impostor floor, which lands above the centre only while the floor is at least
        ``IMPOSTOR_MARGIN ** 2`` times the genuine ceiling (about 1.82x). A narrow window breaks
        that, and the unclamped rule then returns a review line *below* the approve line - a band
        whose ``classify`` can never route anything to a human, and which breaks the ordering the
        rest of this module assumes. Clamped, a narrow window is an honest **one-line** band:
        ``approve == review``, everything at or below the line is approved, everything above is
        refused, and nothing goes to review. That is a real configuration to state, not a
        malformed one to hide.
        """
        centre = (self.genuine_ceiling * self.impostor_floor) ** 0.5
        approve = _line(centre)
        return approve, max(_line(self.impostor_floor / IMPOSTOR_MARGIN), approve)

    def basis(self) -> str:
        """The derivation in one operator-readable sentence, for readiness and a log.

        A ceiling of zero is a *reachable* state rather than a corrupt one - two byte-identical
        captures of one person measure exactly 0.0 - so the ratio is only stated when there is
        a ceiling to divide by. ``derived()`` still returns an approve line above it, and this
        sentence says what the 0.000 means instead of raising from a log call.
        """
        approve, review = self.derived()
        above = (
            f"{approve / self.genuine_ceiling:.2f}x above the ceiling"
            if self.genuine_ceiling > 0
            else "without a ceiling to clear (0.000 means duplicate captures)"
        )
        if review > approve:
            below = f"{(self.impostor_floor / review):.2f}x below the impostor floor"
        elif review > 0:
            below = (
                "the same line as approve, so there is no review window - the measured window "
                "is narrower than IMPOSTOR_MARGIN can separate, and every capture is decided "
                "approved or refused with nobody asked"
            )
        else:
            below = "with no room under the impostor floor (it measured 0.000)"
        return (
            f"approve {approve:.2f} = geometric centre of the measured window "
            f"({self.genuine_ceiling:.3f} genuine ceiling .. {self.impostor_floor:.3f} impostor "
            f"floor, {above}); review {review:.2f} = {below}. {self.evidence}"
        )


#: Every band this build can score with, keyed by pipeline name, each recording the
#: recognition model it was derived with.
#:
#: The keys are **written out**, not built from ``PIPELINE``/``FALLBACK_PIPELINE``, and that is
#: the load-bearing part: a table built from those constants would quietly grow a band for
#: whatever name they were bumped to, which is exactly the silent inheritance this table
#: exists to stop. Written out, a bump makes ``band_for`` raise and the startup gate refuse
#: until somebody measures the new crop - and a test holds the keys to the pipeline names this
#: build can actually write, so a renamed constant cannot leave an orphan band behind either.
#: **NOTHING IS SCORED WITHOUT A MEASUREMENT.** A crop with no entry here is not scored against
#: anybody else's lines: ``band_for`` raises and the startup gate refuses to serve until one of
#: these has been derived for it.
#:
#: Two bands used to live here - ``yunet-2023mar`` (approve 0.40 / review 0.50) and
#: ``mtcnn`` (approve 0.40 / review 0.55) - and both carried ``model="VGG-Face"``. They were
#: real measurements, taken on real pairs through the VGG-Face 4096-dimension pipeline. They
#: are not *this* build's measurements, and that is the whole distinction this table exists to
#: keep: this build embeds with FaceNet-128 through ONNX Runtime (``face_onnx``), which is a
#: different vector space. The arithmetic is unforgiving about it - the two preprocessing
#: contracts alone differ by a cosine of ~0.48 on the same photograph, which is larger than
#: the approve line those bands were built around - so reusing those numbers would not be a
#: conservative choice. It would be applying thresholds nobody measured to vectors nobody
#: measured them for, and every one of those numbers would be wrong in a direction nobody
#: could predict. A 0.40 line in FaceNet's space is not "about the same" as a 0.40 line in
#: VGG's; it is a number that happens to have the same digits.
#:
#: **THIS TABLE NOW HOLDS ONE MEASURED BAND** - ``yunet-2023mar`` / ``Facenet`` - derived by
#: ``backend/tools/derive_facenet_band.py`` from a labelled corpus of twelve identities with four
#: captures each, ten of them usable once faces smaller than the engine's 160px input and frames
#: holding more than one face are excluded. It separated cleanly: over 14 same-person and 196
#: different-people pairs, the highest same-person distance was 0.496 and the lowest
#: different-people distance 0.538.
#:
#: It is a **one-line band** (``approve == review``), and that is a measurement rather than a
#: mistake. The measured window is only 0.041 wide, and the review rule places its line
#: ``IMPOSTOR_MARGIN`` below the impostor floor - which lands above the geometric centre only
#: while that floor is at least ``IMPOSTOR_MARGIN ** 2`` times the genuine ceiling (about
#: 1.82x; this corpus measures 1.08x). ``MatchBand.derived()`` clamps in that case, so the band
#: approves at or below 0.50 and refuses above it, and **nothing is routed to review**. A corpus
#: that resembles the deployment's own captures - gate selfies, one camera, one session -
#: separates far more widely and would yield two lines; replacing this band is therefore a
#: re-measurement, not an edit.
#:
#: An empty table would still be a *working* state rather than a broken one: ``band_for`` raises
#: ``UnknownPipelineError``, ``readiness`` reports ``face_match_band`` as a FATAL check naming
#: the missing measurement, and the startup gate refuses to serve. A deployment that cannot
#: score a punch is one somebody fixes in five minutes; a deployment that scores punches against
#: invented lines is one that silently approves the wrong people for a year.
#:
#: **TO REPLACE IT**, point ``backend/tools/derive_facenet_band.py`` at a folder of labelled
#: calibration images - genuine pairs (two or more captures of the same person) and impostor
#: pairs (different people) - and paste the ``MatchBand(...)`` it prints in below, keyed by
#: ``face_detector.active_pipeline()``. The tool refuses to emit anything when the corpus cannot
#: support a line, which is the same refusal this table makes, one layer up - and it takes the
#: live pipeline's name and no other, because every embedding it produces comes from the live
#: crop. Lines for ``FALLBACK_PIPELINE`` have to be measured where *that* crop can be embedded;
#: this tool cannot produce them, so it will not label a measurement with that name either.
BANDS: dict[str, MatchBand] = {
    "yunet-2023mar": MatchBand(
        model="Facenet",
        approve=0.5,
        review=0.5,
        genuine_ceiling=0.4964,
        impostor_floor=0.5377,
        evidence=(
            "Measured in this build's own vector space on 14 same-person pair(s) across 10 "
            "identities and 196 different-people pair(s), all through this pipeline's own "
            "detector and ONNX engine, cosine distance as ``compare_faces_sync`` computes it. "
            "Corpus: faces (48 image(s), 27 skipped; faces smaller than 160px on their shorter "
            "side excluded, because the engine upscales its crop to 160px and a smaller face "
            "would be embedded as interpolation). Genuine 0.142-0.496; impostor 0.538-1.291. "
            "Floors are extremes of a sample, so the lines sit at a stated distance from them "
            "(see IMPOSTOR_MARGIN and LINE_GRANULARITY)."
        ),
    ),
}


class UnknownPipelineError(RuntimeError):
    """A pipeline this build can run and whose decision lines were never derived.

    Raised rather than answered with a default. A default here would be inherited numbers
    applied to a crop they were never measured for, which is the defect this table exists
    to make impossible - and a missing band is a broken build, not a worker's problem.
    """


def band_for(pipeline: str, model: str | None = None) -> MatchBand:
    """The band for a pipeline, or ``UnknownPipelineError``. Never quietly falls back.

    ``pipeline`` is the provenance recorded beside a *template*, so the lines are read from
    the crop that produced the embedding being scored - which the staleness check has
    already required to be the live one, or the template would not have been scored at all.

    ``model`` defaults to the live one, read from ``face_engine`` (a lazy import: that module
    imports this one, so a module-level import would be a cycle). Two ways this refuses, and
    both matter: a pipeline with no band, and a band derived in a *different* model's vector
    space. A model swap keeps the pipeline's name, so without the second check it would keep
    that pipeline's lines too - numbers measured for embeddings the punch no longer produces.
    """
    if model is None:
        import face_engine

        model = face_engine.FACE_MODEL
    band = BANDS.get(pipeline)
    if band is None:
        raise UnknownPipelineError(
            f"no face-match thresholds have been derived for pipeline {pipeline!r} with model "
            f"{model!r}; measure the new crop and add its band before scoring anything"
        )
    if band.model != model:
        raise UnknownPipelineError(
            f"the thresholds for pipeline {pipeline!r} were derived with the {band.model!r} "
            f"embedding, and this build scores with {model!r}; no distance in this vector space "
            "has a measured line"
        )
    return band


def active_band() -> MatchBand:
    """The band the pipeline that would really run is scored against."""
    return band_for(active_pipeline())


def _to_bgr(image) -> np.ndarray | None:
    """The array OpenCV expects, from whatever the caller had, or ``None``.

    Accepts a path or an array. Non-uint8 arrays (DeepFace hands round floats around) are
    scaled back to bytes, because a detector that reads a 0..1 image as 0..255 pixels finds
    nothing and the failure looks like "no face in the photo".

    ``None`` for anything that is not an image - an unreadable file, ``None`` itself, an array
    of the wrong rank - because every caller here is asking "is there a face in this?" and
    "no" is the answer for a frame that cannot be decoded. Raising would turn a corrupt upload
    into a 500 in the middle of a punch.

    Greyscale and alpha are converted rather than passed through: YuNet wants three channels,
    and a worker's phone photo of a phone screenshot is exactly where a 4-channel PNG arrives.
    """
    if image is None:
        return None
    if isinstance(image, (str, os.PathLike)):
        import cv2

        return cv2.imread(str(image))
    try:
        array = np.asarray(image)
    except Exception:  # noqa: BLE001 - an object that is not array-like is not a frame
        return None
    if array.dtype == object or array.ndim not in (2, 3):
        return None
    if array.size == 0 or array.shape[0] == 0 or array.shape[1] == 0:
        return None
    if array.dtype != np.uint8:
        try:
            array = np.clip(array, 0, 255)
            array = array.astype(np.uint8) if array.max() > 1.5 else (array * 255).astype(np.uint8)
        except (TypeError, ValueError):
            return None
    if array.ndim == 2:
        import cv2

        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if array.shape[2] == 4:
        import cv2

        return cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    if array.shape[2] == 3:
        return array
    return None


def detect_raw(image) -> list[np.ndarray]:
    """Raw YuNet rows for one image: ``[x, y, w, h, 5x(x,y), score]`` each."""
    ok, _reason = _ensure()
    if not ok or _detector is None:
        return []
    frame = _to_bgr(image)
    if frame is None or frame.size == 0 or frame.shape[0] == 0 or frame.shape[1] == 0:
        return []
    height, width = frame.shape[:2]
    with _lock:
        _detector.setInputSize((width, height))
        try:
            _retval, faces = _detector.detect(frame)
        except Exception:  # noqa: BLE001 - a frame it cannot parse is "no face", not a crash
            log.warning("face detector failed on a %sx%s frame", width, height, exc_info=True)
            return []
    if faces is None:
        return []
    return [row for row in faces]


def landmarks_of(row: np.ndarray | None) -> np.ndarray:
    """The five landmarks in a raw YuNet row, as ``(5, 2)`` float32 in frame pixels.

    The row layout (``x, y, w, h, 5x(x, y), score``) is this module's business, so anything that needs
    the points asks here rather than slicing ``row[4:14]`` and inheriting the layout - which is the
    same reason ``_facial_area`` exists. A row that cannot supply five finite points raises, because a
    caller storing those points as a capture's crop provenance must not be handed something that will
    silently become a crop of the wrong place.
    """
    if row is None:
        raise ValueError("no detection row")
    points = np.asarray(row[4:14], dtype=np.float32).reshape(5, 2)
    if points.shape != (5, 2) or not np.isfinite(points).all():
        raise ValueError(f"row does not carry five finite landmarks: {points!r}")
    return points


def detect_landmarks(image) -> list[dict[str, Any]]:
    """Detections with their landmarks, in frame coordinates - the crop, before it is cropped.

    ``detect_and_align`` answers with an aligned crop and throws the geometry away, which is right for
    scoring a punch and useless for keeping one: a stored crop that cannot be re-derived is a crop
    nobody can check. This is the same single detection pass, reported rather than consumed - named
    separately so the punch path keeps paying for exactly one pass (see ``corpus`` for the one caller
    that pays for a second one on purpose, on the stored copy).
    """
    faces = []
    for row in detect_raw(image):
        try:
            points = landmarks_of(row)
            score = float(row[-1])
        except (ValueError, IndexError, TypeError):
            continue
        faces.append(
            {"landmarks": points, "facial_area": _facial_area(row), "confidence": score}
        )
    return faces


def _box_of(area: Any) -> tuple[float, float, float, float] | None:
    """``(x, y, w, h)`` from a ``facial_area`` mapping, or ``None`` when it is not one."""
    if not isinstance(area, dict):
        return None
    try:
        box = tuple(float(area[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if box[2] <= 0 or box[3] <= 0:
        return None
    return box  # type: ignore[return-value]


def _overlap(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> tuple[float, float]:
    """``(IoU, containment)`` for two boxes: the second is how much of the *smaller* sits inside."""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0:
        return 0.0, 0.0
    smaller = min(aw * ah, bw * bh)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0, intersection / smaller if smaller else 0.0


@dataclass
class SubjectReport:
    """What a frame's detections mean for the one-subject rule.

    ``faces`` are the detections that could be the person at the gate, in the order a caller should
    consider them (largest first). ``merged`` counts boxes that were the *same* face found twice,
    and ``specks`` counts detections too small to be anybody - both are reported rather than
    silently discarded, because "two faces" and "one face and a smudge" have different fixes.
    """

    faces: list[dict[str, Any]] = field(default_factory=list)
    merged: int = 0
    specks: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.faces)

    def summary(self) -> str:
        widths = ", ".join(
            str(int((_box_of(face.get("facial_area")) or (0, 0, 0, 0))[2])) for face in self.faces
        )
        parts = [f"{self.count} subject-sized face(s) [{widths}]" if self.count else "no subject-sized face"]
        if self.merged:
            parts.append(f"{self.merged} duplicate box(es) merged")
        if self.specks:
            parts.append(f"{len(self.specks)} speck(s) ignored {self.specks} px")
        return "; ".join(parts)


def subject_detections(
    faces: list[dict[str, Any]],
    *,
    min_px: float = MIN_SUBJECT_PX,
) -> SubjectReport:
    """The people in a frame, from a detector's raw list - duplicates merged, specks dropped.

    The rule the application enforces is "exactly one person at the gate", and it is enforced by
    *counting detections*. Counting them naively makes two mistakes, and both of them cost a worker
    their clock-in with a sentence that blames them:

    * **one face, two boxes.** A detector driven at an unusual scale (this deployment raises the punch
      ceiling to 1280) or over overlapping tiles can fire twice on one face. Merging overlapping
      boxes and their nested twins is the cure; refusing the punch is not.
    * **a speck counted as a person.** A face-shaped 12 px region of a poster, a wall photograph or a
      phone screen is not the person standing at the gate, and cannot even be cropped into the
      pipeline's own template (see ``MIN_SUBJECT_PX``). Under the old rule it refused the punch.

    What is *not* relaxed: two detections that are both subject-sized are two people, and a caller
    that requires one subject must still refuse - this function only stops the count lying. A frame
    whose only detections are specks reports ``count == 0``, which is honest: there is no face here
    that could be the worker's.
    """
    ranked = []
    for index, face in enumerate(faces):
        box = _box_of(face.get("facial_area")) if isinstance(face, dict) else None
        if box is None:
            # A caller that hands over detections with no geometry (a stub in a test, an older
            # shape) counts as one subject each: unable to *improve* the answer, it must not
            # silently drop a face that the caller meant to report.
            ranked.append((index, face, None))
        else:
            ranked.append((index, face, box))
    ranked.sort(key=lambda item: (-(item[2][2] * item[2][3]) if item[2] else 0.0, item[0]))

    report = SubjectReport()
    for _index, face, box in ranked:
        if box is None:
            report.faces.append(face)
            continue
        width = box[2]
        merged_into = None
        for kept_face in report.faces:
            kept_box = _box_of(kept_face.get("facial_area"))
            if kept_box is None:
                continue
            iou, containment = _overlap(kept_box, box)
            if iou >= DUPLICATE_IOU or containment >= DUPLICATE_CONTAINMENT:
                merged_into = kept_face
                break
        if merged_into is not None:
            # The bigger box wins, which is the order this loop already walks in; the smaller one
            # is the same face seen again, and counting it would refuse the punch.
            report.merged += 1
            continue
        if width < min_px:
            report.specks.append(int(round(width)))
            continue
        report.faces.append(face)
    return report


def _facial_area(row: np.ndarray) -> dict[str, int]:
    x, y, w, h = (int(round(float(value))) for value in row[:4])
    return {"x": x, "y": y, "w": w, "h": h}


def align(image, row: np.ndarray) -> np.ndarray:
    """The aligned 112x112 crop for one detection.

    A partial affine transform from YuNet's five landmarks onto the standard template: it
    removes rotation and normalises scale, so two captures of the same person at different
    angles are compared as the same picture. Falls back to the plain padded box when the
    landmarks are unusable - a bad crop is still a crop, and refusing here would take a
    worker's clock-in away over a landmark the detector was unsure about.
    """
    frame = _to_bgr(image)
    landmarks = np.asarray(row[4:14], dtype=np.float32).reshape(5, 2)
    if frame is not None and landmarks.shape == (5, 2):
        try:
            import cv2

            matrix, _inliers = cv2.estimateAffinePartial2D(landmarks, _ALIGN_TEMPLATE)
            if matrix is not None:
                return cv2.warpAffine(
                    frame, matrix, (ALIGNED_SIZE, ALIGNED_SIZE), flags=cv2.INTER_LINEAR
                )
        except Exception:  # noqa: BLE001 - fall through to the box crop
            log.debug("landmark alignment failed; using the detection box", exc_info=True)

    x, y, w, h = (int(round(float(value))) for value in row[:4])
    height, width = (frame.shape[:2] if frame is not None else (0, 0))
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(width, x + w), min(height, y + h)
    crop = frame[y1:y2, x1:x2] if frame is not None else None
    if crop is None or not crop.size:
        return np.zeros((ALIGNED_SIZE, ALIGNED_SIZE, 3), dtype=np.uint8)
    import cv2

    return cv2.resize(crop, (ALIGNED_SIZE, ALIGNED_SIZE), interpolation=cv2.INTER_LINEAR)


def detect_and_align(image) -> list[dict[str, Any]]:
    """Faces in an image, cropped and aligned - the shape DeepFace's own callers expect.

    ``[{"face": <aligned crop>, "facial_area": {...}, "confidence": <score>}, ...]``, which is
    what ``extract_faces`` returns and what ``face_engine`` and ``quick_links`` read. This is
    the seam the test harness replaces, so a suite that stubs the models does not need a real
    face in a synthetic photo.
    """
    faces = []
    for row in detect_raw(image):
        try:
            score = float(row[-1])
        except (IndexError, TypeError, ValueError):
            score = 1.0
        faces.append(
            {
                "face": align(image, row),
                "facial_area": _facial_area(row),
                "confidence": score,
            }
        )
    return faces


def describe() -> dict[str, Any]:
    """What readiness reports about this detector."""
    ok, reason = _ensure()
    return {
        "detector": DETECTOR_NAME,
        "pipeline": PIPELINE,
        "active_detector": active_detector(),
        "active_pipeline": active_pipeline(),
        "available": ok,
        "model": str(model_path()),
        "model_fingerprint": fingerprint(),
        "error": reason or None,
    }
