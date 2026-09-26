"""FaceNet-128 embeddings through ONNX Runtime, and nothing else.

WHY THIS EXISTS
---------------
Every punch is a face embedding. It used to be a ``VGG-Face`` vector - 4096 floats out of a
553 MiB Keras graph, built and executed by TensorFlow - and the cost of that was measured,
not assumed: ~250 ms of CPU per embedding, ~2.3 GiB of resident memory per worker, a ~5 s
cold start, and a throughput that *fell* as callers stacked up because TensorFlow's own
intra-op pool was already saturated. The same convolution stack, same arithmetic, the same
model weights, compiled to ONNX and executed by ``onnxruntime`` measures **~13 ms**,
**~151 MiB** serving, **0.37 s** to ready, and it scales with concurrency instead of
losing to it.

So this module is the embedding path: one preprocessing function and one session. It
imports neither ``tensorflow`` nor ``deepface``, and it must not - the boundary is the
point. A process that only wants to embed a face should not pay six seconds of TensorFlow
import to do it, and a runtime that cannot be swapped without dragging a training framework
along is not a runtime, it is a dependency.

THE PREPROCESSING CONTRACT, AND WHY IT IS REPRODUCED RATHER THAN CHOOSEN
-----------------------------------------------------------------------
A model is only half of a vector space; the crop in front of it is the other half. An
embedding computed from differently-preprocessed pixels is a *different vector space*, and
no threshold derived in one has any meaning in the other. So this module reproduces the
upstream contract exactly, and the reproduction is verified by measurement rather than
asserted - it is bit-exact against the library it replaces (max-absolute difference 0.0 on
a real photograph):

* **BGR, not RGB.** The upstream pipeline flips BGR->RGB "to keep compatibility with
  ``extract_faces``" and then flips straight back before the model call. The net channel
  order the model sees is BGR, so this function takes the crop as OpenCV produced it and
  does not touch the channels.
* **Letterboxed, not distorted.** The crop is scaled to *fit* the canvas and centred on
  black, so aspect ratio survives. A naive resize to a square stretches the face - and for
  a 112x112 detector crop it happens to be the same thing, which is exactly why this bug
  class is invisible until a non-square frame arrives.
* **Float32 in [0, 1].** 0..255 sources are divided by 255. A source that is *already*
  0..1 - a float image, a crop that has been through a normalising step - is left alone,
  because the upstream rule is a conditional and not an unconditional division.
* **No standard-score normalisation.** ``((x - mean) / std)`` is FaceNet's *training*
  preprocessing and is deliberately **not** applied here, because the upstream pipeline
  runs with ``normalization="base"``, which is a no-op. Feeding a standardised crop to a
  graph whose thresholds were measured on unstandardised pixels moves the vector by a
  cosine of ~0.48 - larger than the approve line itself.

The L2 normalisation is applied to the **embedding**, not to the pixels, and it is a
separate function on purpose: standard-scoring pixels and unit-normalising a vector are
unrelated operations that happen to share a name, and conflating them is how a preprocessing
bug hides inside a normalisation change.

L2 NORMALISATION IS FREE FOR THE APPLICATION AND USEFUL AFTERWARDS
------------------------------------------------------------------
``main.compare_faces_sync`` scores with ``main.cosine``, which is
invariant to a vector's magnitude - so normalising changes no distance and no decision
line by even one bit of the comparison. It is done anyway because a unit-length template
is what a 1-to-N search, an approximate index, or an on-device comparison needs, and the
cost is 128 divides once per punch.

THREAD SAFETY
-------------
An ``InferenceSession`` may be run from several threads concurrently; session *creation*
may not. So the session is built under a lock on first use and read without one thereafter,
and the module-level engine is published through a double-checked singleton. That is the
whole concurrency story, and it is deliberately small: the application's own admission
control (``face_engine``'s queue) sits above this, because a bound on how much runs at once
belongs to the process, not to the graph.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("attendance.face_onnx")

#: The recognition model these embeddings belong to. Recorded beside every template and
#: compared by ``biometrics.stale_reason``: two models that both produce 128 floats still
#: describe different spaces, so the name is what keeps a template from being scored against
#: thresholds measured in another model's geometry.
MODEL_NAME = "Facenet"

#: The embedding size this engine returns. Stated here rather than only derived from the
#: graph, because the *gates* need it without loading a session - a stale-template check
#: must be answerable by a process that has no model file at all.
DIMENSIONS = 128

#: The square canvas the graph was exported for. Explicit in the ONNX signature
#: ``(None, 160, 160, 3)``, so a mismatch here fails loudly at the first run rather than
#: silently broadcasting.
INPUT_SIZE = 160

#: Where the exported graph lives. Large binaries are gitignored in this project (see
#: ``backend/models/README.md``) - the artifact is fetched or exported per host, and the
#: path is what the code depends on.
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "facenet128.onnx"

#: Concurrency settings, chosen against the *deployment* rather than against a development
#: box. The instance this runs on is **one vCPU**, and ``face_engine`` runs two face calls at
#: a time - so two intra-op threads per session would put four compute threads on one core.
#: There is no idle core for the second thread to use, so it buys no throughput and costs
#: context switching plus the per-thread scratch an ONNX session allocates on a host with
#: 512 MB to spend. One intra-op thread, one inter-op thread (there is nothing to pipeline -
#: a face graph is a chain), and sequential execution so two concurrent sessions cannot each
#: fan out.
#:
#: The constructor still takes ``intra_op_threads``, so a host with cores to spare can be
#: measured rather than assumed; the default stays where the deployment is.
DEFAULT_INTRA_OP_THREADS = 1
DEFAULT_INTER_OP_THREADS = 1

#: Graph optimisation at session build time. ORT fuses the batch norms into the convolution
#: weights and collapses the activation/convolution pairs, which is most of why this is fast;
#: ``all`` is the shipped setting and the measured one.
DEFAULT_OPTIMIZATION = "all"


class FaceNetUnavailable(RuntimeError):
    """The ONNX graph is missing, unreadable or failed to build a session.

    Raised instead of returning a zero vector: an embedding of zeros compares as a
    perfectly valid distance to everything, so a missing model that answered with one would
    approve punches against a constant. Every caller must handle a model that is not there,
    and this is the exception it handles.
    """


class FaceNetModelMismatch(FaceNetUnavailable):
    """The graph on disk is not the graph this build expects (``*_SHA256`` configured)."""


def model_path() -> Path:
    """The configured graph, read at call time so a test can point it elsewhere.

    ``config`` is imported lazily and wrapped, the way ``face_detector.model_path`` does it:
    this module must stay importable by a tool or a test that has no settings loaded.
    """
    try:
        import config

        override = getattr(config.settings, "facenet_model_path", None)
        if override:
            return Path(override)
    except Exception:  # pragma: no cover - config is importable everywhere else
        pass
    return Path(os.environ.get("FACENET_MODEL_PATH") or DEFAULT_MODEL_PATH)


def _configured_sha256() -> str:
    try:
        import config

        return str(getattr(config.settings, "facenet_model_sha256", "") or "")
    except Exception:  # pragma: no cover
        return ""


def fingerprint(path: Path | None = None) -> str:
    """First 16 hex characters of the graph's sha256, or ``""`` when it is not readable."""
    target = path or model_path()
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# preprocessing: the contract, as one pure function
# --------------------------------------------------------------------------- #
def _as_three_channel(image) -> np.ndarray:
    """The array the contract operates on, from whatever the caller had.

    A single-channel frame is stacked rather than rejected: the detector can be configured
    for grayscale input, and a face is not less of a face for arriving without colour.
    Anything that is not an image at all raises, because "embedded a constant" is not an
    answer a caller can act on.
    """
    array = np.asarray(image)
    if array.ndim == 2:
        array = np.stack([array, array, array], axis=-1)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError(f"expected an HxWx3 image, got {array.shape}")
    if array.size == 0 or array.shape[0] == 0 or array.shape[1] == 0:
        raise ValueError("expected a non-empty image")
    return array


def letterbox(image, size: int = INPUT_SIZE) -> np.ndarray:
    """Scale-to-fit into a ``size`` x ``size`` canvas, centred, padded with zeros.

    Reproduces the upstream geometry exactly, including the two details that are easy to
    get subtly wrong: the resize happens in the **source dtype** (so a uint8 crop rounds
    the way the upstream OpenCV call rounded - converting to float first and resizing after
    gives a different array), and the padding is split so the extra row or column lands on
    the bottom/right, which is what ``diff - diff // 2`` does.

    Returns the source dtype. The conversion to float32 belongs to ``preprocess``, because
    the divide-by-255 rule is stated in terms of the resized values.
    """
    array = _as_three_channel(image)
    factor = min(size / array.shape[0], size / array.shape[1])
    width = int(array.shape[1] * factor)
    height = int(array.shape[0] * factor)
    # cv2 is imported here rather than at module scope so that importing the application
    # does not pull in OpenCV on a path that never embeds anything.
    import cv2

    # OpenCV's own thread pool is a *process-wide* setting, and this is the one place in
    # this module that can touch it without defeating the lazy import above. The detector
    # modules pin it too, but a tool that imports only this one must not be the exception:
    # OpenCV fanning a resize across four threads competes with the ONNX session for the
    # same single core. Called per embed rather than guarded by a flag - it is one store to
    # a global, against a resize of a 160 px canvas.
    cv2.setNumThreads(1)

    resized = cv2.resize(array, (width, height), interpolation=cv2.INTER_LINEAR)

    pad_vertical = size - resized.shape[0]
    pad_horizontal = size - resized.shape[1]
    resized = np.pad(
        resized,
        (
            (pad_vertical // 2, pad_vertical - pad_vertical // 2),
            (pad_horizontal // 2, pad_horizontal - pad_horizontal // 2),
            (0, 0),
        ),
        "constant",
    )
    # Only reachable when rounding left the canvas a pixel short of the target; kept because
    # the upstream helper guards it and a graph with a fixed input shape cannot take a
    # 159-pixel image.
    if resized.shape[0] != size or resized.shape[1] != size:
        resized = cv2.resize(resized, (size, size), interpolation=cv2.INTER_LINEAR)
    return resized


def preprocess(image) -> np.ndarray:
    """One detector crop to the ``(1, 160, 160, 3)`` float32 tensor the graph expects.

    The contract, in the order it applies: BGR preserved, letterboxed to a square, converted
    to float32, divided by 255 only when the crop's own maximum exceeds 1, and made
    contiguous.

    The conditional divide is not defensive programming - it is the upstream rule, and the
    two readings it distinguishes are both real: a 0..255 crop becomes 0..1, and a crop that
    is already 0..1 (a float image, or a frame that has been through a normalising stage) is
    left exactly as it is. Applying the divide unconditionally would turn the second case
    into a 0..0.004 image - a black square, embedded into a meaningless vector, with no
    error anywhere to see.
    """
    array = letterbox(image)
    tensor = np.asarray(array, dtype=np.float32)
    if tensor.max() > 1.0:
        tensor = tensor / 255.0
    # The shape is stated, not inferred: an ONNX graph with a fixed input signature fails
    # on a rank-3 array, and failing here names the crop instead of the session.
    return np.ascontiguousarray(tensor[None, ...], dtype=np.float32)


def l2_normalize(vector) -> np.ndarray:
    """The unit-length form of an embedding, as ``(128,)`` float32.

    A zero-norm vector is returned unchanged rather than divided by a floor: it is the only
    input for which the operation is undefined, and scaling it by an arbitrary epsilon would
    invent a direction for a vector that has none.
    """
    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    if norm == 0.0:
        return array
    return (array / norm).astype(np.float32)


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
class OnnxFaceNetEngine:
    """A compiled FaceNet-128 graph, ready to embed crops.

    One session, lazily built, safe to share across threads. The class owns no queue and no
    admission control: bounding how much runs at once is ``face_engine``'s job, and a second
    bound here would mean two places to tune and two numbers to disagree about.

    ``embed`` is the whole public surface. It takes a crop and returns 128 unit-length
    float32 values - the exact thing ``biometrics.write_reference`` stores and
    ``main.compare_faces_sync`` scores.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        intra_op_threads: int | None = DEFAULT_INTRA_OP_THREADS,
        inter_op_threads: int | None = DEFAULT_INTER_OP_THREADS,
        optimization: str = DEFAULT_OPTIMIZATION,
        expected_sha256: str | None = None,
        load_now: bool = True,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._intra_op_threads = intra_op_threads
        self._inter_op_threads = inter_op_threads
        self._optimization = optimization
        self._expected_sha256 = expected_sha256
        self._lock = threading.Lock()
        self._session: Any = None
        self._input_name = ""
        self._output_name = ""
        self._load_error: str | None = None
        self._load_seconds: float | None = None
        if load_now:
            self.load()

    # -- lifecycle ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path or model_path()

    @property
    def loaded(self) -> bool:
        return self._session is not None

    @property
    def load_seconds(self) -> float | None:
        return self._load_seconds

    def available(self) -> tuple[bool, str]:
        """``(ok, reason)``. Never raises, so a readiness check can report the failure."""
        try:
            self.load()
        except FaceNetUnavailable as exc:
            return False, str(exc)
        return True, ""

    def load(self) -> None:
        """Build the session once. Idempotent, and safe to call from any thread.

        Raises ``FaceNetUnavailable`` rather than logging and continuing: a caller that
        receives no exception will go on to embed, and embedding against no graph is how a
        site's punches start being refused for a reason nobody can see.
        """
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:  # another thread won the race
                return
            target = self.path
            if not target.exists():
                self._load_error = f"face model not found at {target}"
                raise FaceNetUnavailable(self._load_error)
            expected = self._expected_sha256 or _configured_sha256()
            if expected and fingerprint(target) != expected[:16]:
                self._load_error = "face model does not match FACENET_MODEL_SHA256"
                raise FaceNetModelMismatch(self._load_error)
            try:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.graph_optimization_level = _optimization_level(ort, self._optimization)
                if self._intra_op_threads is not None:
                    options.intra_op_num_threads = int(self._intra_op_threads)
                if self._inter_op_threads is not None:
                    options.inter_op_num_threads = int(self._inter_op_threads)
                # Sequential: with two sessions alive at once, parallel execution mode lets
                # each one fan its operators out, and the box is oversubscribed exactly the
                # way the measurements show hurts.
                options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                options.log_severity_level = 3
                # The CPU memory arena stays **enabled**. It is ORT's default, and it is
                # written out here because the opposite is the obvious-looking change on a
                # 512 MB host - and the benchmark already rejected it: with the arena off,
                # every intermediate buffer is handed back to the system allocator and
                # asked for again on the next run, so the process pays for it in allocator
                # churn rather than in peak, and peak is what the ceiling is about. The
                # arena also keeps the allocation pattern flat across a burst, which is the
                # property the punch path depends on after a whole site arrives at once.
                options.enable_cpu_mem_arena = True

                import time as _time

                started = _time.perf_counter()
                session = ort.InferenceSession(
                    str(target), sess_options=options, providers=["CPUExecutionProvider"]
                )
                self._load_seconds = _time.perf_counter() - started
            except FaceNetUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - reported, never raised raw
                self._load_error = f"{type(exc).__name__}: {exc}"
                raise FaceNetUnavailable(self._load_error) from exc

            self._session = session
            self._input_name = session.get_inputs()[0].name
            self._output_name = session.get_outputs()[0].name
            self._load_error = None
            log.info(
                "face model ready: %s (%s, %s-dim, %s threads, %s optimisation)",
                target.name,
                MODEL_NAME,
                DIMENSIONS,
                self._intra_op_threads,
                self._optimization,
            )

    def close(self) -> None:
        """Drop the session. The graph is released when the last reference goes."""
        with self._lock:
            self._session = None

    # -- inference ---------------------------------------------------------
    def embed(self, image) -> np.ndarray:
        """A unit-length 128-float embedding for one detector crop.

        Accepts the crop in any of the shapes the application can hand over (an HxWx3 BGR
        array from the detector, a grayscale frame, a float image already in 0..1); the
        contract in ``preprocess`` is what decides what the pixels mean.

        The session is read without the lock and run without it. Two threads calling this
        concurrently is the designed case - ``onnxruntime`` supports it - and the lock only
        ever guards *building* the session, which happens once.
        """
        self.load()
        tensor = preprocess(image)
        session = self._session
        if session is None:  # pragma: no cover - load() raises rather than leaving None
            raise FaceNetUnavailable(self._load_error or "face model is not loaded")
        try:
            outputs = session.run([self._output_name], {self._input_name: tensor})
        except Exception as exc:  # noqa: BLE001 - a session failure is a model failure
            raise FaceNetUnavailable(f"{type(exc).__name__}: {exc}") from exc

        embedding = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if embedding.size != DIMENSIONS:
            raise FaceNetUnavailable(
                f"the graph returned {embedding.size} values, expected {DIMENSIONS}"
            )
        return l2_normalize(embedding)

    def embed_as_list(self, image) -> list[float]:
        """``embed`` as plain floats, for the template writer and the telemetry payload."""
        return [float(value) for value in self.embed(image)]

    # -- reporting ---------------------------------------------------------
    def describe(self) -> dict:
        """What this engine is, for a log line or a readiness check. Never raises."""
        target = self.path
        return {
            "model": MODEL_NAME,
            "dimensions": DIMENSIONS,
            "input_size": INPUT_SIZE,
            "path": str(target),
            "present": target.exists(),
            "fingerprint": fingerprint(target),
            "loaded": self.loaded,
            "load_seconds": self._load_seconds,
            "intra_op_threads": self._intra_op_threads,
            "inter_op_threads": self._inter_op_threads,
            "optimization": self._optimization,
            "error": self._load_error,
        }


def _optimization_level(ort_module, name: str):
    """The ORT graph-optimisation level for a readable name.

    Named strings rather than the enum at the call site, so that the configured value can
    live in settings or an environment variable without importing ``onnxruntime`` to be
    parsed - the same reason every other model knob in this project is a string.
    """
    levels = {
        "disabled": ort_module.GraphOptimizationLevel.ORT_DISABLE_ALL,
        "basic": ort_module.GraphOptimizationLevel.ORT_ENABLE_BASIC,
        "extended": ort_module.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
        "all": ort_module.GraphOptimizationLevel.ORT_ENABLE_ALL,
    }
    try:
        return levels[str(name).lower()]
    except KeyError as exc:
        raise FaceNetUnavailable(
            f"unknown graph optimisation {name!r}; expected one of {sorted(levels)}"
        ) from exc


# --------------------------------------------------------------------------- #
# the process-wide engine
# --------------------------------------------------------------------------- #
_engine: OnnxFaceNetEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> OnnxFaceNetEngine:
    """The shared engine, built on first use.

    Lazy on purpose, and the same rule ``face_engine`` follows for its worker threads:
    importing the application - a CLI command, a migration, the test suite, a readiness
    probe - must not open an 87 MiB graph. The first punch pays for it, once, and the
    double-checked lock means concurrent first punches pay for it once between them.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            _engine = OnnxFaceNetEngine()
        return _engine


def set_engine(engine: OnnxFaceNetEngine | None) -> None:
    """Replace the shared engine. For tests and for tools that own their own session."""
    global _engine
    with _engine_lock:
        _engine = engine


def load_now() -> dict:
    """Build the shared engine eagerly and describe it. For a preload at startup.

    With ``FACE_ENGINE_PROCESS`` set this is answered by the child (``face_process``),
    which is the whole point of the flag: this process must not be the one that opens the
    87 MiB graph. The child loads **all three** models on a warm, because a preload that
    loaded only the embedding would still leave the first punch of the day paying the
    detector's ~104 MB spike; only the embedding's description is returned, which is what
    this function has always returned.
    """
    from config import settings

    if settings.face_engine_process:
        import face_process

        snapshot = face_process.client().warm()
        embedding = snapshot.get("embedding") if isinstance(snapshot, dict) else None
        if isinstance(embedding, dict):
            return embedding
        return {"model": MODEL_NAME, "dimensions": DIMENSIONS, "loaded": True, "error": None}
    engine = get_engine()
    engine.load()
    return engine.describe()
