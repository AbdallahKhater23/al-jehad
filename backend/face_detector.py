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
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

#: The pipeline that produced a template. Bump this whenever the crop changes - a different
#: detector, a different alignment, a different model - because every stored template becomes
#: stale at that moment and must be re-enrolled.
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
                str(path), "", (320, 320), SCORE_THRESHOLD, 0.3, 5000
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
