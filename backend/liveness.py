"""Passive, single-frame liveness / presentation-attack detection (MiniFASNet, ONNX).

WHY THIS FILE EXISTS
--------------------
``attendance_logs`` has carried ``liveness_class`` and ``liveness_score`` columns
since migration 2, and every one of them was written as ``NULL``: nothing ever
looked at whether the selfie showed a person or a photograph of one. A printed
selfie held up to the camera therefore passed face matching exactly as well as a
real worker, which defeats the entire point of biometric attendance.

The check is *passive*: one frame, no head turn, no blink challenge, no second
capture. That matters on a construction site - an active challenge adds seconds to
every punch and fails in sunlight and gloves.

POSITION IN THE PIPELINE
------------------------
Liveness runs **before** ``DeepFace.represent()``. MiniFASNet at 80x80 is roughly
two orders of magnitude cheaper than a VGG-Face embedding with an MTCNN detector,
so a presentation attack is rejected without the server ever paying for the
expensive path. That is not only a latency decision: it means an attacker cannot
use forged frames as a CPU exhaustion vector.

FAILURE POLICY (the part that decides whether this is safe to ship)
-------------------------------------------------------------------
Every failure to *evaluate* is reported as ``available=False`` with a reason -
never as "live". ``settings.liveness_mode`` then decides what that means:

* ``off``       - not called at all.
* ``advisory``  - the verdict is recorded and notified, the punch proceeds. This is
                  the default because deploying a new model straight into ``enforce``
                  on a live site risks rejecting genuine workers on day one.
* ``enforce``   - only ``verdict == "live"`` proceeds. A missing model or runtime is
                  a rejection (``liveness_unavailable``) unless
                  ``LIVENESS_ALLOW_UNAVAILABLE=1`` is set for an emergency.

Nothing here imports ``onnxruntime`` at module import time: the venv does not have
it, and a missing optional dependency must not stop the API from starting.

MODEL CONTRACT
--------------
``LIVENESS_MODEL_PATH`` is a MiniFASNet export (Silent-Face-Anti-Spoofing
``MiniFASNetV2``/``V3``, 80x80 crop). Input: ``float32`` NCHW, values in ``[0, 1]``.
Outputs handled:

* 3 values - ``[live, print_attack, replay_attack]`` (the canonical layout). These are
  **logits** - MiniFASNet's forward pass returns unnormalised scores and the official
  inference script applies the softmax - so the softmax is applied here,
* 2 values - ``[live, spoof]`` logits, softmaxed (``BINARY_LIVE_INDEX``),
* 1 value  - ``[0, 1]`` is read as the genuine probability, anything else is sigmoid.

The layout actually used is reported in ``detail`` so an operator can confirm the
assumption against the model they shipped instead of guessing.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from config import settings

log = logging.getLogger("attendance.liveness")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------
MODE_OFF = "off"
MODE_ADVISORY = "advisory"
MODE_ENFORCE = "enforce"

VERDICT_LIVE = "live"
VERDICT_SPOOF = "spoof"
VERDICT_LOW_CONFIDENCE = "low_confidence"
VERDICT_UNAVAILABLE = "unavailable"
VERDICT_ERROR = "error"

CLASS_LIVE = "live"
CLASS_PRINT = "print_attack"
CLASS_REPLAY = "replay_attack"
CLASS_UNKNOWN = "unknown"
CLASS_OFFLINE = "unverified_offline"

#: Stable, machine-readable rejection reasons. The frontend and the audit trail
#: both key off these, so they must not change casually.
ERR_SPOOF = "spoof_detected"
ERR_LOW_CONFIDENCE = "liveness_low_confidence"
ERR_UNAVAILABLE = "liveness_unavailable"
ERR_INFERENCE = "liveness_error"

#: Output ordering of a 2-value export: index 0 is the genuine class, matching the
#: 3-class layout where index 0 is also genuine.
BINARY_LIVE_INDEX = 0

#: Index of the genuine class in the canonical 3-class layout.
THREE_CLASS_LIVE_INDEX = 0


@dataclass(frozen=True)
class LivenessResult:
    """Outcome of one evaluation, before any mode policy is applied."""

    verdict: str
    is_live: bool
    available: bool
    genuine_prob: float | None = None
    spoof_class: str = CLASS_UNKNOWN
    confidence: float | None = None
    error_code: str | None = None
    detail: str = ""
    model: str = ""
    latency_ms: float = 0.0

    def log_fields(self) -> tuple[str | None, float | None]:
        """``(liveness_class, liveness_score)`` for the attendance row."""
        if self.verdict == VERDICT_UNAVAILABLE:
            return "unavailable", None
        if self.verdict == VERDICT_ERROR:
            return "error", None
        if self.is_live:
            return CLASS_LIVE, self.genuine_prob
        return self.spoof_class or CLASS_UNKNOWN, self.confidence

    def as_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "is_live": self.is_live,
            "available": self.available,
            "genuine_prob": self.genuine_prob,
            "spoof_class": self.spoof_class,
            "confidence": self.confidence,
            "error_code": self.error_code,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
            "detail": self.detail,
        }


@dataclass
class GateDecision:
    """What the caller should do, after policy."""

    allowed: bool
    result: LivenessResult
    error_code: str | None = None
    blocked: bool = False
    note: str = ""

    def log_fields(self) -> tuple[str | None, float | None]:
        return self.result.log_fields()

    def as_payload(self) -> dict:
        payload = self.result.as_payload()
        payload.update({"allowed": self.allowed, "blocked": self.blocked, "note": self.note})
        return payload


# ---------------------------------------------------------------------------
# session management
# ---------------------------------------------------------------------------
_SESSION = None
_SESSION_ERROR: str | None = None
_SESSION_LOCK = threading.Lock()
_MODEL_FINGERPRINT: str | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_session():
    """Build the ONNX session. Raises on every failure; the caller records it."""
    import onnxruntime  # imported lazily: an optional dependency

    model_path = Path(settings.liveness_model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"liveness model not found at {model_path}")
    if not model_path.is_file() or model_path.stat().st_size == 0:
        raise ValueError(f"liveness model at {model_path} is not a readable file")

    options = onnxruntime.SessionOptions()
    # One frame is tiny; extra threads add scheduling noise without throughput.
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = onnxruntime.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    return session


def get_session():
    """Cached session, or ``None`` when liveness cannot be evaluated at all.

    Double-checked locking: the first attendance request after a restart pays the
    ~50 ms load, and concurrent requests wait rather than each building a session.
    """
    global _SESSION, _SESSION_ERROR, _MODEL_FINGERPRINT
    if _SESSION is not None or _SESSION_ERROR is not None:
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None or _SESSION_ERROR is not None:
            return _SESSION
        try:
            model_path = Path(settings.liveness_model_path)
            if settings.liveness_model_sha256 and model_path.exists():
                digest = _sha256_file(model_path)
                if digest.lower() != settings.liveness_model_sha256.strip().lower():
                    raise ValueError(
                        "liveness model checksum does not match LIVENESS_MODEL_SHA256; "
                        "refusing to run an unverified model"
                    )
            _SESSION = _create_session()
            if model_path.exists():
                _MODEL_FINGERPRINT = _sha256_file(model_path)[:16]
            _SESSION_ERROR = None
        except Exception as exc:  # ImportError, FileNotFoundError, ORT errors...
            _SESSION = None
            _SESSION_ERROR = f"{type(exc).__name__}: {exc}"
            log.warning("liveness unavailable: %s", _SESSION_ERROR)
        return _SESSION


def reset_session() -> None:
    """Forget the cached session (used after changing settings, and by tests)."""
    if settings.face_engine_process:
        # The session that matters is the child's; forgetting only this process's copy would
        # leave the model the operator just changed still loaded over there. Swallowed when
        # the child is unreachable: this is a cache reset, and failing it must not be the
        # thing that stops an operator from applying a configuration.
        try:
            _remote("liveness_reset")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not reset the model process's liveness session: %s", exc)
    global _SESSION, _SESSION_ERROR, _MODEL_FINGERPRINT
    with _SESSION_LOCK:
        _SESSION = None
        _SESSION_ERROR = None
        _MODEL_FINGERPRINT = None


# ---------------------------------------------------------------------------
# where the model runs: here, or in the child that owns it
# ---------------------------------------------------------------------------
def _remote(op: str, *args, **kwargs):
    """One liveness op in the face model process (see ``face_process``).

    Only the *model call* crosses: ``gate()``, the modes and the thresholds are policy, and
    policy stays in the API process where the alternatives are (an RPC boundary that carried
    the decision would need the settings and the audit trail on both sides).
    """
    import face_process

    return face_process.client().call(op, *args, **kwargs)


def _remote_check(image_rgb: np.ndarray, *, size: int | None) -> LivenessResult:
    """``check_liveness``, evaluated in the model process.

    Never raises, exactly like the function it stands in for: a transport failure is just
    another way for liveness to be *unavailable*, which every caller already handles - the
    mode decides whether that blocks a punch or only records it.
    """
    started = time.perf_counter()
    try:
        result = _remote("liveness_check", image_rgb, size=size)
    except Exception as exc:  # noqa: BLE001 - unavailable is an answer, not a raise
        log.warning("the face model process could not evaluate liveness: %s", exc)
        return LivenessResult(
            verdict=VERDICT_UNAVAILABLE,
            is_live=False,
            available=False,
            error_code=ERR_UNAVAILABLE,
            detail=f"the face model process could not answer: {exc}",
            model=str(settings.liveness_model_path),
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    if isinstance(result, LivenessResult):
        return result
    return LivenessResult(  # pragma: no cover - a child that answered with something else
        verdict=VERDICT_ERROR,
        is_live=False,
        available=False,
        error_code=ERR_UNAVAILABLE,
        detail=f"the face model process answered with {type(result).__name__}",
        model=str(settings.liveness_model_path),
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )


def _remote_status() -> dict | None:
    """The child's own liveness status, or ``None`` when it cannot be asked.

    Consulted rather than assumed for the reason that matters at startup: this process has no
    session of its own in that mode, so reporting its own answer would tell an operator that
    ``LIVENESS_MODE=enforce`` has no model - a fatal readiness verdict - while the child is
    healthy and serving.
    """
    import face_process

    try:
        snapshot = face_process.client().status()
    except face_process.FaceProcessError as exc:
        log.warning("liveness status lives in the face model process, which did not answer: %s", exc)
        return None
    section = snapshot.get("liveness") if isinstance(snapshot, dict) else None
    return dict(section) if isinstance(section, dict) else None


def status() -> dict:
    """Read-only self-description, surfaced by ``/api/v1/status`` and readiness."""
    if settings.face_engine_process:
        remote = _remote_status()
        if remote is not None:
            return remote
    session = get_session()
    available = session is not None
    inputs = []
    if available:
        try:
            inputs = [
                {"name": i.name, "shape": list(i.shape), "type": i.type}
                for i in session.get_inputs()
            ]
        except Exception:  # pragma: no cover - exotic runtime
            inputs = []
    return {
        "mode": settings.liveness_mode,
        "available": available,
        "model_path": str(settings.liveness_model_path),
        "model_fingerprint": _MODEL_FINGERPRINT,
        "input_size": settings.liveness_input_size,
        "accept_threshold": settings.liveness_accept_threshold,
        "reject_threshold": settings.liveness_reject_threshold,
        "allow_unavailable": settings.liveness_allow_unavailable,
        "inputs": inputs,
        "error": _SESSION_ERROR,
    }


# ---------------------------------------------------------------------------
# preprocessing / inference
# ---------------------------------------------------------------------------
def preprocess(image_rgb: np.ndarray, *, size: int | None = None) -> np.ndarray:
    """``HxWx3 uint8 RGB`` -> ``1x3xSxS float32`` in ``[0, 1]``.

    Note the channel order: the canonical PyTorch MiniFASNet takes RGB crops from
    PIL. If a particular export was trained on OpenCV crops it expects BGR, and the
    symptom is a model that rejects everything - ``detail`` in the response reports
    the layout used, and swapping to BGR is a one-line change here rather than a
    silent mismatch.
    """
    from PIL import Image

    target = int(size or settings.liveness_input_size)
    array = np.asarray(image_rgb)
    if array.ndim == 2:  # grayscale
        array = np.stack([array] * 3, axis=-1)
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError(f"expected an HxWx3 image, received shape {array.shape}")
    array = array[:, :, :3]
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)

    image = Image.fromarray(array, mode="RGB").resize((target, target), Image.BILINEAR)
    batch = np.asarray(image, dtype=np.float32) / 255.0
    batch = np.transpose(batch, (2, 0, 1))[np.newaxis, ...]
    return np.ascontiguousarray(batch)


def _probabilities(raw: np.ndarray) -> tuple[np.ndarray, str]:
    """Normalise a model output into class probabilities plus the layout used."""
    values = np.asarray(raw, dtype=np.float64).reshape(-1)
    if values.size == 1:
        value = float(values[0])
        if 0.0 <= value <= 1.0:
            return np.array([value, 1.0 - value]), "binary_probability"
        genuine = 1.0 / (1.0 + np.exp(-value))
        return np.array([genuine, 1.0 - genuine]), "single_logit_sigmoid"
    if values.size == 2:
        shifted = values - values.max()
        probabilities = np.exp(shifted) / np.exp(shifted).sum()
        if BINARY_LIVE_INDEX == 1:
            probabilities = probabilities[::-1]
        return probabilities, "binary_softmax"
    shifted = values - values.max()
    probabilities = np.exp(shifted) / np.exp(shifted).sum()
    probabilities = probabilities[:3]
    return probabilities, "three_class_softmax"


def check_liveness(image_rgb: np.ndarray, *, size: int | None = None) -> LivenessResult:
    """Evaluate one frame. Never raises: failures come back as ``unavailable``/``error``."""
    if settings.face_engine_process:
        return _remote_check(image_rgb, size=size)
    started = time.perf_counter()
    session = get_session()
    if session is None:
        return LivenessResult(
            verdict=VERDICT_UNAVAILABLE,
            is_live=False,
            available=False,
            error_code=ERR_UNAVAILABLE,
            detail=_SESSION_ERROR or "liveness runtime or model is not available",
            model=str(settings.liveness_model_path),
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    try:
        batch = preprocess(image_rgb, size=size)
        inputs = session.get_inputs()
        output_names = [output.name for output in session.get_outputs()]
        outputs = session.run(output_names, {inputs[0].name: batch})
        probabilities, layout = _probabilities(np.asarray(outputs[0]))
        index = int(np.argmax(probabilities))
        top = float(probabilities[index])
        genuine = float(probabilities[THREE_CLASS_LIVE_INDEX])
        attack_index = 1 if probabilities.size > 1 else None
        attack = float(probabilities[attack_index]) if attack_index is not None else 0.0

        if probabilities.size == 1 or probabilities.size == 2:
            # Only genuine/spoof are distinguishable; there is no print-vs-replay
            # split, so an attack is reported without inventing a class.
            if index == THREE_CLASS_LIVE_INDEX and genuine >= settings.liveness_accept_threshold:
                verdict, spoof_class, is_live = VERDICT_LIVE, CLASS_LIVE, True
            elif genuine >= settings.liveness_accept_threshold:
                verdict, spoof_class, is_live = VERDICT_LIVE, CLASS_LIVE, True
            elif attack >= settings.liveness_reject_threshold:
                verdict, spoof_class, is_live = VERDICT_SPOOF, CLASS_UNKNOWN, False
            else:
                verdict, spoof_class, is_live = VERDICT_LOW_CONFIDENCE, CLASS_UNKNOWN, False
        else:
            if index == THREE_CLASS_LIVE_INDEX and genuine >= settings.liveness_accept_threshold:
                verdict, spoof_class, is_live = VERDICT_LIVE, CLASS_LIVE, True
            elif index != THREE_CLASS_LIVE_INDEX and top >= settings.liveness_reject_threshold:
                verdict, spoof_class, is_live = VERDICT_SPOOF, (CLASS_PRINT if index == 1 else CLASS_REPLAY), False
            elif genuine >= settings.liveness_accept_threshold:
                verdict, spoof_class, is_live = VERDICT_LIVE, CLASS_LIVE, True
            else:
                # Between the two thresholds: ambiguous, and biometrics fail closed.
                verdict, spoof_class, is_live = VERDICT_LOW_CONFIDENCE, CLASS_UNKNOWN, False

        error_code = None
        if verdict == VERDICT_SPOOF:
            error_code = ERR_SPOOF
        elif verdict == VERDICT_LOW_CONFIDENCE:
            error_code = ERR_LOW_CONFIDENCE

        return LivenessResult(
            verdict=verdict,
            is_live=is_live,
            available=True,
            genuine_prob=round(genuine, 4),
            spoof_class=spoof_class,
            confidence=round(top, 4),
            error_code=error_code,
            detail=(
                f"layout={layout} probs={[round(float(p), 4) for p in probabilities]} "
                f"accept>={settings.liveness_accept_threshold} reject>={settings.liveness_reject_threshold}"
            ),
            model=str(settings.liveness_model_path),
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    except Exception as exc:  # inference, shape or preprocessing failure
        log.warning("liveness inference failed: %s", exc)
        return LivenessResult(
            verdict=VERDICT_ERROR,
            is_live=False,
            available=False,
            error_code=ERR_INFERENCE,
            detail=f"{type(exc).__name__}: {exc}",
            model=str(settings.liveness_model_path),
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def mode() -> str:
    value = (settings.liveness_mode or MODE_ADVISORY).lower()
    return value if value in {MODE_OFF, MODE_ADVISORY, MODE_ENFORCE} else MODE_ADVISORY


def gate(result: LivenessResult, *, mode_override: str | None = None) -> GateDecision:
    """Apply the configured policy to a verdict.

    ``advisory`` deliberately returns ``allowed=True`` even for a detected attack:
    the punch is recorded, flagged and notified, and a human decides. The point of
    advisory is to measure the threshold against real site traffic before it is
    allowed to reject anybody.
    """
    effective = (mode_override or mode()).lower()
    if effective == MODE_OFF:
        return GateDecision(allowed=True, result=result, note="liveness mode is off")

    if result.verdict == VERDICT_LIVE:
        return GateDecision(allowed=True, result=result, note="liveness passed")

    if effective == MODE_ADVISORY:
        return GateDecision(
            allowed=True,
            result=result,
            error_code=result.error_code,
            note=f"advisory: recorded '{result.verdict}' without blocking",
        )

    # enforce
    if result.verdict in {VERDICT_UNAVAILABLE, VERDICT_ERROR} and settings.liveness_allow_unavailable:
        return GateDecision(
            allowed=True,
            result=result,
            error_code=result.error_code,
            note=(
                "enforce: evaluator unavailable and LIVENESS_ALLOW_UNAVAILABLE=1, "
                "so the punch proceeds flagged for review"
            ),
        )
    if result.verdict in {VERDICT_UNAVAILABLE, VERDICT_ERROR}:
        return GateDecision(
            allowed=False,
            blocked=True,
            result=result,
            error_code=result.error_code or ERR_UNAVAILABLE,
            note="enforce: cannot prove the frame is genuine, so it is refused",
        )
    return GateDecision(
        allowed=False,
        blocked=True,
        result=result,
        error_code=result.error_code or ERR_SPOOF,
        note=f"enforce: {result.verdict}",
    )


def inspect(image_rgb: np.ndarray, *, mode_override: str | None = None) -> GateDecision:
    """Convenience: evaluate and apply policy in one call."""
    return gate(check_liveness(image_rgb), mode_override=mode_override)


def readiness() -> dict:
    """Verdict for the startup gate.

    ``enforce`` with no usable evaluator is a *fatal* misconfiguration - it would
    reject every genuine punch - while a missing model in ``advisory``/``off`` is
    only advisory, because the model is an optional extra and must not prevent the
    API from serving attendance.
    """
    info = status()
    effective = mode()
    if effective == MODE_ENFORCE and not info["available"] and not settings.liveness_allow_unavailable:
        return {
            **info,
            "severity": "fatal",
            "ready": False,
            "reason": (
                "LIVENESS_MODE=enforce but no usable MiniFASNet model/runtime, so every "
                "punch would be rejected. Install onnxruntime and place the model at "
                f"{info['model_path']}, set LIVENESS_ALLOW_UNAVAILABLE=1 to accept that "
                "risk explicitly, or switch to LIVENESS_MODE=advisory."
            ),
        }
    if effective == MODE_ENFORCE and not info["available"]:
        return {
            **info,
            "severity": "advisory",
            "ready": True,
            "reason": "enforce mode with no model, but LIVENESS_ALLOW_UNAVAILABLE=1 was set explicitly",
        }
    return {
        **info,
        "severity": "info",
        "ready": True,
        "reason": f"liveness mode '{effective}', available={info['available']}",
    }
