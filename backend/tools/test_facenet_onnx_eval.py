"""Is FaceNet-128 on ONNX Runtime a better biometric engine than VGG-Face on Keras/TF?

WHY THIS EXISTS
---------------
The production pipeline embeds every punch with ``VGG-Face``, a 4096-dimension vector from a
553 MiB Keras graph, through TensorFlow. FaceNet-128 is the obvious candidate to replace it:
32x smaller vectors, 88 MiB of weights, and a materially better LFW score. The open question
is not accuracy - it is whether the *runtime* can deliver the latency and memory the swap
promises, and whether the two runtimes agree closely enough that a template written by one
can be read by the other.

So this script measures three things and refuses to assume any of them:

1. **The export.** ``tf2onnx`` writes the exact DeepFace ``Facenet`` Keras graph (Keras 3
   under TF 2.21, complete with its 21 ``Lambda(scaling)`` residual gates) to ONNX at opset
   17 with an explicit ``(None, 160, 160, 3)`` float32 input. The graph is then checked for
   the operations it is *supposed* to contain - a graph that exported without a single
   ``Conv`` would compare equal to nothing and prove nothing.

2. **Parity, in two layers.** First, the tensor DeepFace really hands the model is captured
   and compared against the standalone NumPy/OpenCV preprocessing this script implements -
   so "my preprocessing matches theirs" is a measurement, not a claim. Second, that captured
   tensor is pushed through Keras and through ONNX Runtime and the two embeddings are
   compared. Runtime drift is the difference a template migration cannot survive; the
   preprocessing difference is the one that is explainable and fixable.

3. **The cost.** Cold import, model instantiation, 15 timed ``perf_counter`` forward passes,
   idle resident memory before and after inference, weights and on-disk template footprint,
   1-to-1 cosine cost against a stored template, and a concurrency sweep, because the
   application runs two face calls at once (see ``face_engine``) and a latency measured on an
   idle box is not the latency a punch sees.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not import the application. It is standalone on purpose: it must be runnable on a
candidate host *before* anything is deployed, and a harness that shares the code under test
cannot be used to judge it.

It also does not derive a production ``MatchBand``. That is not an oversight - a band is a
property of a vector space, and with a single photograph per worker this checkout cannot
supply the same-person pairs a genuine ceiling needs. ``--stage geometry`` measures what the
available photographs *can* support (the impostor floor, in both spaces) and says plainly
what is missing.

COLLECTION SAFETY
-----------------
Named ``test_*`` as requested, but it lives in ``tools/`` (pytest's ``testpaths`` is
``tests``) and every heavy import happens inside a function, so even a bare ``pytest`` run
that picked the file up would import it cheaply and collect nothing from it.

USAGE
-----
    venv/Scripts/python.exe tools/test_facenet_onnx_eval.py                 # everything
    venv/Scripts/python.exe tools/test_facenet_onnx_eval.py --stage export
    venv/Scripts/python.exe tools/test_facenet_onnx_eval.py --image ../worker_photos/x.jpg
    venv/Scripts/python.exe tools/test_facenet_onnx_eval.py --passes 30 --no-vgg
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# the numbers this evaluation is pinned to
# --------------------------------------------------------------------------- #

#: The model under evaluation: DeepFace's 128-dimension Inception-ResNet-v1.
MODEL_NAME = "Facenet"
DIMENSIONS = 128
INPUT_SIZE = 160
OPSET = 17

#: The baseline the application actually runs, for the comparison column.
BASELINE_MODEL = "VGG-Face"
BASELINE_DIMENSIONS = 4096

#: Cosine drift between the two runtimes on the same input, above which they are not
#: interchangeable. A template written by one must score identically under the other, and
#: 1e-5 is roughly two orders of magnitude below the smallest distance that separates any
#: two decision lines in ``face_detector.BANDS`` (0.05 of granularity, 0.10 between bands).
DRIFT_COSINE_TOL = 1e-5

DEFAULT_PASSES = 15
DEFAULT_CONCURRENCY = (1, 2, 4)

#: Where the harness writes its artifacts. Never the checkout: an evaluation that can leave
#: a 90 MiB graph or a face crop inside the repository is one nobody should run casually.
DEFAULT_OUT = Path(tempfile.gettempdir()) / "facenet_onnx_eval"

#: The recorded ground truth this run is reconciled against: (median forward ms, idle RSS,
#: weights bytes, template JSON bytes). VGG-Face and FaceNet-128 were measured on this host
#: in an earlier session; both are re-measured here so the comparison is same-run, and the
#: recorded values are printed beside them so a host that has drifted is visible.
RECORDED = {
    "vgg": {"label": "VGG-Face (Keras/TF)", "median_ms": 247.6, "rss": 2.3 * 1024**3,
            "weights": 580085408, "json": 29000},
    "facenet": {"label": "FaceNet-128 (Keras/TF)", "median_ms": 405.9, "rss": 586 * 1024**2,
                "weights": 92190816, "json": 2600},
}


# --------------------------------------------------------------------------- #
# measurement primitives
# --------------------------------------------------------------------------- #

def use_utf8_stdout() -> None:
    """Photo filenames are not always ASCII and this console is not always UTF-8."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - stdout is not always reconfigurable
        pass


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_memory_info():
    """``K32GetProcessMemoryInfo`` with its prototype declared, or ``None``.

    The prototypes are not optional decoration. Without ``argtypes``, ctypes passes the
    process handle as a 32-bit ``c_int`` and the call fails - which is how the first version
    of this function reported a 0 MiB working set for a process holding a 2.3 GiB graph, and
    why the harness now prints whether the working set is readable at all before quoting it.
    """
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        try:
            function = kernel32.K32GetProcessMemoryInfo
        except AttributeError:  # pragma: no cover - pre-Vista, kept for completeness
            function = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        function.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        function.restype = wintypes.BOOL
    except Exception:
        return None

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not function(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters


def rss_bytes() -> int:
    """Resident set size of this process, or 0 where it genuinely cannot be read.

    ``psutil`` is not a dependency of this project and will not become one for a benchmark,
    so the working set comes from the platform. Returns 0 rather than raising - but 0 is
    printed as ``unavailable`` rather than as ``0 B``, because a memory row that silently
    reads zero is worse than one that admits it is missing.
    """
    if os.name == "nt":
        counters = _windows_memory_info()
        return int(counters.WorkingSetSize) if counters else 0
    try:  # POSIX
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. ``statistics`` has no p95 before 3.8's quantiles."""
    if not values:
        return float("nan")
    return float(np.percentile(np.asarray(values, dtype=np.float64), q * 100.0))


def clock_integrity(seconds: float = 0.25) -> dict:
    """Prove the timer before trusting anything timed with it.

    Every latency below is a ``perf_counter`` delta. If the clock were coarse, monotonic
    only by accident, or wrong by a scale factor, all of them would be wrong together and
    the table would look plausible. Sleeping a known interval and measuring it costs a
    quarter of a second and makes the timer part of the evidence.
    """
    start = time.perf_counter()
    time.sleep(seconds)
    measured = time.perf_counter() - start
    return {"requested_s": seconds, "measured_s": measured,
            "error_ms": abs(measured - seconds) * 1000.0,
            "resolution_s": time.get_clock_info("perf_counter").resolution}


def human_bytes(value: float) -> str:
    if not value:
        return "unavailable"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024.0 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} GiB"


# --------------------------------------------------------------------------- #
# stage 1: the export
# --------------------------------------------------------------------------- #

def build_keras_model(model_name: str = MODEL_NAME):
    """DeepFace's built model client. Returns the *client*, not the bare Keras model.

    The client carries ``input_shape``/``output_shape`` and the real ``forward`` the
    library calls, which is what makes the preprocessing capture in ``parity`` possible.
    """
    from deepface.modules import modeling

    return modeling.build_model(task="facial_recognition", model_name=model_name)


def export_onnx(keras_model, out_path: Path) -> dict:
    """Export the Keras graph to ONNX at ``OPSET`` with a fixed input signature.

    ``input_signature`` is given explicitly rather than inferred: the inferred shape of this
    graph is batch-dynamic in some operators and hard-coded in others, and a mismatch
    between the two is the classic source of a session that works at batch 1 and fails
    later. Stating ``(None, 160, 160, 3)`` float32 makes that the contract.
    """
    import tensorflow as tf
    import tf2onnx

    signature = [
        tf.TensorSpec((None, INPUT_SIZE, INPUT_SIZE, 3), tf.float32, name="input")
    ]
    started = time.perf_counter()
    onnx_model, _ = tf2onnx.convert.from_keras(
        keras_model, input_signature=signature, opset=OPSET
    )
    export_s = time.perf_counter() - started

    import onnx

    onnx.checker.check_model(onnx_model)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(onnx_model, str(out_path))
    return {"export_s": export_s, **describe_onnx(out_path)}


def describe_onnx(path: Path) -> dict:
    """What the graph is, and a check that it is the graph we meant to write."""
    import onnx

    model = onnx.load(str(path))
    ops: dict[str, int] = {}
    for node in model.graph.node:
        ops[node.op_type] = ops.get(node.op_type, 0) + 1

    inputs = []
    for tensor in model.graph.input:
        dims = [d.dim_value if d.HasField("dim_value") else None
                for d in tensor.type.tensor_type.shape.dim]
        inputs.append({"name": tensor.name, "shape": dims,
                       "dtype": onnx.TensorProto.DataType.Name(
                           tensor.type.tensor_type.elem_type)})

    outputs = []
    for tensor in model.graph.output:
        dims = [d.dim_value if d.HasField("dim_value") else None
                for d in tensor.type.tensor_type.shape.dim]
        outputs.append({"name": tensor.name, "shape": dims})

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "ir_version": model.ir_version,
        "opset": [{"domain": o.domain or "ai.onnx", "version": o.version}
                  for o in model.opset_import],
        "nodes": len(model.graph.node),
        "op_types": dict(sorted(ops.items(), key=lambda kv: -kv[1])),
        "inputs": inputs,
        "outputs": outputs,
    }


#: The structural landmarks of Inception-ResNet-v1, with the count a faithful export cannot
#: fall below. Deliberately *not* op-exact: tf2onnx legitimately rewrites what it exports.
_GRAPH_LANDMARKS = (
    ("Conv", 100, "convolution layers"),
    ("Relu", 100, "activations"),
    ("Concat", 10, "inception branch merges"),
    ("Add", 15, "residual adds (21 Block35/Block17/Block8 gates)"),
    ("Mul", 15, "the 21 Lambda(scaling) gates"),
)


def check_graph_is_not_degenerate(info: dict) -> list[str]:
    """The export can 'succeed' and still be useless. Refuse to benchmark that.

    This is a *structural* check and nothing more. It catches the export that lost whole
    layer classes - the graph that is not Inception-ResNet-v1 at all - and it deliberately
    does not try to judge numerical fidelity, because it cannot: whether the batch
    normalisations survived is a question with a numeric answer, and
    ``compare_embeddings``/``drift`` is the thing that answers it. A structural check that
    asserted the presence of a literal ``BatchNormalization`` node would have failed a
    *correct* export, since tf2onnx folds those into the convolution weights - which is
    what happened, and why this check no longer looks for one.
    """
    problems = []
    ops = info["op_types"]
    for op_type, minimum, description in _GRAPH_LANDMARKS:
        count = ops.get(op_type, 0)
        if count < minimum:
            problems.append(f"{op_type} x{count} < {minimum} ({description})")
    if info["outputs"] and info["outputs"][0]["shape"][-1] not in (DIMENSIONS, None):
        problems.append(
            f"output last dim is {info['outputs'][0]['shape'][-1]}, expected {DIMENSIONS}"
        )
    return problems


def describe_normalisation_folding(info: dict) -> str:
    """Where the batch normalisations went - reported, because it is the usual surprise."""
    ops = info["op_types"]
    if ops.get("BatchNormalization", 0):
        return f"BatchNormalization kept as {ops['BatchNormalization']} nodes"
    return ("BatchNormalization folded into the Conv weights (no BatchNormalization "
            "node remains); correctness is settled by the drift probe, not by this line")


# --------------------------------------------------------------------------- #
# stage 2: preprocessing, standalone and TF-free
# --------------------------------------------------------------------------- #

def preprocess_standalone(bgr: np.ndarray) -> np.ndarray:
    """FaceNet preprocessing in NumPy/OpenCV, with no TensorFlow in the path.

    This is the function the spec asks for, and it is the function a production cutover
    would ship:

    * **BGR to RGB.** OpenCV hands everything over in BGR; FaceNet was trained on RGB.
    * **Resize to 160x160.** Direct resize, not DeepFace's letterbox pad - see
      ``compare_preprocessing`` for the measured consequence of that difference.
    * **Standard score.** ``(x - mean) / max(std, 1/sqrt(N))``. The floor on the denominator
      is the reason this is written by hand rather than divided by ``std``: a crop that is
      nearly uniform has a std near zero, and dividing by it turns rounding noise into a
      full-scale embedding.
    * Output stays in ``0..255`` scale, float32, NCHW-free ``(1, 160, 160, 3)``.

    The embedding's L2 normalisation is *not* here - it is ``l2_normalize``, applied to the
    vector rather than the pixels, and kept separate so the drift probe can measure the raw
    embedding as well as the normalised one.
    """
    image = np.asarray(bgr)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"expected an HxWx3 image, got {image.shape}")

    rgb = image[:, :, ::-1]  # BGR -> RGB, the whole reason this is not a one-liner
    resized = _resize_direct(rgb, INPUT_SIZE).astype(np.float32)

    std = float(resized.std())
    floor = 1.0 / float(np.sqrt(resized.size))
    return ((resized - float(resized.mean())) / max(std, floor))[None, ...].astype(np.float32)


def _resize_direct(image: np.ndarray, size) -> np.ndarray:
    """``cv2.resize`` with DeepFace's interpolation, taking either a side or a (w, h)."""
    import cv2

    target = (size, size) if isinstance(size, int) else size
    return cv2.resize(image, target, interpolation=cv2.INTER_LINEAR)


def preprocess_bgr_standardized(bgr: np.ndarray) -> np.ndarray:
    """The spec's crop with the channel swap removed, as the control.

    FaceNet was trained on RGB, so this variant is wrong by construction. It exists so
    ``compare_preprocessing`` proves it can tell RGB from BGR rather than asserting it: a
    probe that only ever offered the correct answer would agree with everything it was
    shown.
    """
    image = np.asarray(bgr)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    bgr_arr = _resize_direct(image, INPUT_SIZE).astype(np.float32)
    std = float(bgr_arr.std())
    floor = 1.0 / float(np.sqrt(bgr_arr.size))
    return ((bgr_arr - float(bgr_arr.mean())) / max(std, floor))[None, ...].astype(np.float32)


def preprocess_deepface_like(bgr: np.ndarray) -> np.ndarray:
    """Reproduce, line for line, the tensor DeepFace 0.0.100 hands its face models.

    This is the finding the parity probe produced, written down as code rather than believed.
    On the ``detector_backend="skip"`` path the application uses, DeepFace:

    1. flips BGR->RGB "to keep compatibility with ``extract_faces``" and then flips straight
       back to BGR before the model call, so the net channel order is **BGR**;
    2. *letterboxes* rather than resizes - the image is scaled to fit and centred on a black
       square, so a 450x800 photograph keeps its aspect ratio and gains black bands;
    3. divides by 255 whenever the crop's own maximum exceeds 1 (``preprocessing.resize_image``
       lines 123-125), so the model receives **float32 in [0, 1]**;
    4. applies no standard-score normalization at all, because ``represent``'s ``normalization``
       argument defaults to ``"base"``, which is a no-op - and the application does not pass it.

    Which is to say the shipped default is not FaceNet's training preprocessing, and not
    VGG-Face's per-channel mean subtraction either. That is not by itself an alarm - this is
    the pipeline every measured ``MatchBand`` was calibrated through, and the bands are real
    - but it is why a vector from here and a vector from a canonical FaceNet preprocessing
    are not the same kind of vector, and can never share a decision line.
    """
    image = np.asarray(bgr)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)

    factor = min(INPUT_SIZE / image.shape[0], INPUT_SIZE / image.shape[1])
    resized = _resize_direct(image, (int(image.shape[1] * factor), int(image.shape[0] * factor)))
    pad_top = (INPUT_SIZE - resized.shape[0]) // 2
    pad_left = (INPUT_SIZE - resized.shape[1]) // 2
    resized = np.pad(
        resized,
        (
            (pad_top, INPUT_SIZE - resized.shape[0] - pad_top),
            (pad_left, INPUT_SIZE - resized.shape[1] - pad_left),
            (0, 0),
        ),
        "constant",
    )
    if resized.shape[:2] != (INPUT_SIZE, INPUT_SIZE):
        resized = _resize_direct(resized, INPUT_SIZE)

    array, _ = np.asarray(resized, dtype=np.float32), None
    if array.max() > 1:
        array = (array / 255.0).astype(np.float32)
    return array[None, ...]


def l2_normalize(vector) -> np.ndarray:
    """Unit-length copy of an embedding, as FaceNet's output layer intends it."""
    array = np.asarray(vector, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(array))
    return array if norm == 0.0 else (array / norm).astype(np.float32)


def cosine_distance(a, b) -> float:
    """The application's own comparison, so a number here means a number there."""
    from scipy.spatial.distance import cosine

    return float(cosine(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)))


def capture_deepface_call(client, bgr: np.ndarray, *, model_name: str = MODEL_NAME, **kwargs):
    """Run DeepFace's real ``represent`` and record the tensor *that* model was handed.

    The model is the only place that knows what preprocessing actually happened, so it is
    asked rather than read. ``represent`` resolves ``modeling.build_model(...)`` to the same
    memoised client object this harness holds, so patching the instance's ``forward``
    intercepts the call without touching the library.

    ``model_name`` is a parameter because the input contract is per-model: VGG-Face's graph
    takes 224x224 and FaceNet's takes 160x160, so benchmarking the baseline against the
    challenger's tensor raises rather than compares - which is how this signature was found.
    """
    from deepface import DeepFace

    captured: dict = {}
    original = client.forward

    def spy(img):
        captured["input"] = np.array(img, copy=True)
        return original(img)

    client.forward = spy
    try:
        result = DeepFace.represent(
            img_path=bgr,
            model_name=model_name,
            detector_backend="skip",
            enforce_detection=False,
            **kwargs,
        )
    finally:
        client.forward = original

    embedding = np.asarray(result[0]["embedding"], dtype=np.float32)
    return captured["input"], embedding


def compare_preprocessing(bgr: np.ndarray) -> dict:
    """Which preprocessing does DeepFace actually agree with, and by how much?

    Five candidates are offered for the tensor DeepFace handed the model and the smallest
    max-absolute difference is the answer. Three of them are controls that are *expected to
    lose*: ``bgr_letterbox_0_255`` differs from the winner only in scale, ``rgb_letterbox_0_1``
    only in channel order, and ``bgr_direct_standardized`` differs from the spec's own crop
    only in channel order. A probe that could not separate those would agree with everything
    it was shown and would be measuring nothing.

    The winner is reproduced verbatim in ``preprocess_deepface_like``, so whatever this
    reports is executable rather than a sentence in a report.
    """
    from deepface.modules import modeling

    client = modeling.build_model(task="facial_recognition", model_name=MODEL_NAME)
    captured, deepface_embedding = capture_deepface_call(client, bgr)

    library = preprocess_deepface_like(bgr)
    candidates = {
        "bgr_letterbox_0_1 (expected)": library,
        "bgr_letterbox_0_255 (control)": (library * 255.0).astype(np.float32),
        "rgb_letterbox_0_1 (control)": library[:, :, ::-1],
        "spec_rgb_direct_standardized": preprocess_standalone(bgr),
        "bgr_direct_standardized (control)": preprocess_bgr_standardized(bgr),
    }

    comparison: dict = {}
    for name, candidate in candidates.items():
        candidate = np.ascontiguousarray(candidate, dtype=np.float32)
        if candidate.shape != captured.shape:
            comparison[name] = {
                "shape_mismatch": [list(candidate.shape), list(captured.shape)]
            }
            continue
        delta = np.abs(candidate.astype(np.float64) - captured.astype(np.float64))
        comparison[name] = {
            "max_abs": float(delta.max()),
            "mean_abs": float(delta.mean()),
            # Bit-exactness, not "close enough": this decides whether the reproduction can be
            # used as the preprocessing of record.
            "bit_exact": bool(np.array_equal(candidate, captured)),
            # Correlation is the channel-order fingerprint. Swapping R and B preserves the
            # per-channel means and standard deviations exactly, so only a pixel-wise
            # comparison can reveal it - which is why this column is not enough on its own.
            "cosine_to_captured": float(
                1.0 - cosine_distance(candidate.ravel(), captured.ravel())
            ),
        }

    ranked = sorted(
        (name for name in comparison if "max_abs" in comparison[name]),
        key=lambda name: comparison[name]["max_abs"],
    )
    expected = comparison.get("bgr_letterbox_0_1 (expected)", {})
    return {
        "captured_shape": list(captured.shape),
        "captured_dtype": str(captured.dtype),
        "captured_min": float(captured.min()),
        "captured_max": float(captured.max()),
        "captured_mean": float(captured.mean()),
        "candidates": comparison,
        "best_match": ranked[0] if ranked else None,
        "library_form_is_bit_exact": bool(expected.get("bit_exact")),
        "spec_form_max_abs": comparison.get("spec_rgb_direct_standardized", {}).get("max_abs"),
        "deepface_embedding_norm": float(np.linalg.norm(deepface_embedding)),
    }


# --------------------------------------------------------------------------- #
# stage 3: the two engines
# --------------------------------------------------------------------------- #

class KerasEngine:
    """The reference runtime: the DeepFace client's own Keras graph."""

    name = "FaceNet-128 (Keras/TF)"

    def __init__(self, keras_model, load_ms: float):
        self.model = keras_model
        self.load_ms = load_ms
        self.rss_after_load = rss_bytes()

    def infer(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.model(x, training=False), dtype=np.float32).reshape(-1)


class OnnxEngine:
    """ONNX Runtime running the exported graph.

    ``optimization`` is reported rather than assumed: ORT's graph optimiser runs at session
    build time, so the only honest way to say the graph is optimised is to load it with the
    optimiser off and on and time both.
    """

    name = "FaceNet-128 (ONNX RT)"

    def __init__(self, path: Path, threads: int | None = None,
                 optimization: str = "all"):
        import onnxruntime as ort

        levels = {
            "disabled": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
            "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
            "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
            "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
        }
        options = ort.SessionOptions()
        options.graph_optimization_level = levels[optimization]
        if threads is not None:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        options.log_severity_level = 3

        started = time.perf_counter()
        self.session = ort.InferenceSession(str(path), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.load_ms = (time.perf_counter() - started) * 1000.0
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.optimization = optimization
        self.threads = threads
        self.rss_after_load = rss_bytes()

    def infer(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(
            self.session.run([self.output_name], {self.input_name: x})[0], dtype=np.float32
        ).reshape(-1)


def measure_reference_embedding(model_name: str, bgr: np.ndarray) -> np.ndarray:
    """One embedding from the shipped library path, for the geometry comparison."""
    from deepface import DeepFace

    result = DeepFace.represent(
        img_path=bgr, model_name=model_name, detector_backend="skip", enforce_detection=False
    )
    return np.asarray(result[0]["embedding"], dtype=np.float32)


# --------------------------------------------------------------------------- #
# stage 4: the benchmark
# --------------------------------------------------------------------------- #

def benchmark_engine(engine, x: np.ndarray, passes: int) -> dict:
    """Warm up, then time ``passes`` forward passes with ``perf_counter``.

    The warm-up pass is discarded from the distribution but its duration is reported: for a
    Keras graph the first call includes lazily-built kernel selection, and for an ORT session
    it includes the first allocation of the arena. Reporting it separately is the difference
    between "the model is slow" and "the process was cold".
    """
    rss_before = rss_bytes()

    warm_start = time.perf_counter()
    engine.infer(x)
    warmup_ms = (time.perf_counter() - warm_start) * 1000.0

    latencies: list[float] = []
    for _ in range(passes):
        start = time.perf_counter()
        engine.infer(x)
        latencies.append((time.perf_counter() - start) * 1000.0)

    rss_after = rss_bytes()
    return {
        "warmup_ms": warmup_ms,
        "passes": passes,
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "median_ms": statistics.median(latencies),
        "mean_ms": statistics.fmean(latencies),
        "stdev_ms": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
        "p95_ms": percentile(latencies, 0.95),
        "rss_before": rss_before,
        "rss_after": rss_after,
        "rss_delta": rss_after - rss_before,
        "raw_ms": latencies,
    }


def cosine_cost(stored, live, iterations: int = 2000) -> dict:
    """What one 1-to-1 comparison costs, by the application's own call and by the vectorised one.

    ``main.compare_faces_sync`` uses ``scipy.spatial.distance.cosine`` on two Python lists,
    which is the number that matters for a punch. A NumPy dot product on the same vectors is
    reported beside it because it is what a 1-to-N search would have to use, and the gap
    between them is the cost of the per-call Python overhead rather than of the arithmetic.
    """
    stored_arr = np.asarray(stored, dtype=np.float64)
    live_arr = np.asarray(live, dtype=np.float64)

    scipy_ms: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        cosine_distance(stored_arr, live_arr)
        scipy_ms.append((time.perf_counter() - start) * 1000.0)

    # NumPy path: the same formula, no per-call interpreter overhead.
    numpy_ms: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        denominator = float(np.linalg.norm(stored_arr) * np.linalg.norm(live_arr))
        1.0 - float(stored_arr @ live_arr) / denominator
        numpy_ms.append((time.perf_counter() - start) * 1000.0)

    return {
        "iterations": iterations,
        "scipy_median_ms": statistics.median(scipy_ms),
        "scipy_p95_ms": percentile(scipy_ms, 0.95),
        "numpy_median_ms": statistics.median(numpy_ms),
        "numpy_p95_ms": percentile(numpy_ms, 0.95),
        "dimensions": int(stored_arr.size),
    }


def concurrency_sweep(engine, x: np.ndarray, levels=(1, 2, 4), passes: int = 5) -> list[dict]:
    """Per-call latency and aggregate throughput as callers stack up.

    The application runs its own face engine with a measured capacity of two, so a single
    -threaded latency is not the number that decides a punch. ``threading`` is the right
    instrument here even for the Keras engine: what is being measured is whether the runtime
    shares the CPU usefully, and that is what a worker arriving at the gate experiences.
    """
    import threading

    results = []
    for level in levels:
        latencies: list[float] = []
        lock = threading.Lock()

        def drain(count: int) -> None:
            mine = []
            for _ in range(count):
                start = time.perf_counter()
                engine.infer(x)
                mine.append((time.perf_counter() - start) * 1000.0)
            with lock:
                latencies.extend(mine)

        engine.infer(x)  # warm this worker's path once
        threads = [threading.Thread(target=drain, args=(passes,)) for _ in range(level)]
        wall_start = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.perf_counter() - wall_start

        total = level * passes
        results.append({
            "concurrency": level,
            "calls": total,
            "wall_s": wall,
            "per_call_median_ms": statistics.median(latencies),
            "per_call_max_ms": max(latencies),
            "calls_per_second": total / wall if wall > 0 else float("nan"),
        })
    return results


#: Snippets run in a *fresh* interpreter, because cold start is a property of a process.
#: In-process these two rows would be lies: by the time the benchmark stage runs,
#: TensorFlow is imported and every model is memoised, so "instantiation" measures a
#: dictionary lookup.
#:
#: Paths arrive through ``sys.argv``, not through string substitution. Substituting a Windows
#: path into a Python string literal is a syntax error waiting to happen - ``"C:\Users\..."``
#: is a ``\U`` unicode escape - and it happened here before it was fixed. Arguments carry no
#: escaping rules at all.
_COLD_KERAS_SNIPPET = """
import json, sys, time
tools_dir, model = sys.argv[1], sys.argv[2]
sys.path.insert(0, tools_dir)
import test_facenet_onnx_eval as harness

times = {}
t0 = time.perf_counter()
import tensorflow as tf
times["tensorflow"] = time.perf_counter() - t0
t0 = time.perf_counter()
from deepface.modules import modeling
times["deepface"] = time.perf_counter() - t0
t0 = time.perf_counter()
client = modeling.build_model(task="facial_recognition", model_name=model)
times["build"] = time.perf_counter() - t0
times["rss_after_build"] = harness.rss_bytes()
times["parameter_count"] = int(client.model.count_params())

# One inference, because the working set a worker holds *while serving* is the number a
# deployment has to size for - a session that has only loaded is not yet holding its
# activations. Reported separately from the load itself so the two cannot be confused.
import numpy as np
side = int(client.input_shape[0])
scratch = np.zeros((1, side, side, 3), dtype=np.float32)
client.model(scratch, training=False)
times["rss_after_inference"] = harness.rss_bytes()
print("COLD" + json.dumps(times))
"""

_COLD_ONNX_SNIPPET = """
import json, sys, time
tools_dir, onnx_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, tools_dir)
import test_facenet_onnx_eval as harness

times = {}
t0 = time.perf_counter()
import onnxruntime as ort
times["onnxruntime"] = time.perf_counter() - t0
options = ort.SessionOptions()
options.log_severity_level = 3
t0 = time.perf_counter()
session = ort.InferenceSession(
    onnx_path, sess_options=options, providers=["CPUExecutionProvider"])
times["build"] = time.perf_counter() - t0
times["rss_after_build"] = harness.rss_bytes()
times["inputs"] = [[i.name, i.shape, i.type] for i in session.get_inputs()]

# Same reason as the Keras snippet: the load is not the steady state.
import numpy as np
entry = session.get_inputs()[0]
shape = [d if isinstance(d, int) else 1 for d in entry.shape]
session.run([session.get_outputs()[0].name], {entry.name: np.zeros(shape, dtype=np.float32)})
times["rss_after_inference"] = harness.rss_bytes()
print("COLD" + json.dumps(times))
"""


def _run_cold_snippet(snippet: str, *args: str) -> dict:
    """Run one snippet in a new process and read the single ``COLD`` line back."""
    completed = subprocess.run(
        [sys.executable, "-c", snippet, *args],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent), check=False,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("COLD"):
            return json.loads(line[4:])
    tail = [ln for ln in (completed.stderr or "").splitlines() if ln.strip()][-2:]
    return {"error": " | ".join(tail) or f"exit {completed.returncode}"}


def cold_start_probe(onnx_path: Path, models: list[str]) -> dict:
    """Import and instantiate each engine in its own interpreter, and remember the cost."""
    tools_dir = str(Path(__file__).resolve().parent)
    cold: dict = {}

    for model in models:
        key = "vgg" if model == BASELINE_MODEL else "facenet"
        raw = _run_cold_snippet(_COLD_KERAS_SNIPPET, tools_dir, model)
        if "error" not in raw:
            raw["import_s"] = raw.get("tensorflow")
            raw["build_s"] = raw.get("build")
        cold[key] = raw

    raw = _run_cold_snippet(_COLD_ONNX_SNIPPET, tools_dir, str(onnx_path))
    if "error" not in raw:
        raw["import_s"] = raw.get("onnxruntime")
        raw["build_s"] = raw.get("build")
    cold["onnx"] = raw
    return cold


def payload_sizes(embedding, model_name: str, pipeline: str) -> dict:
    """Three ways to hold one template, because they differ by more than 30x.

    * the float32 buffer a runtime would keep,
    * the boxed Python floats DeepFace returns and a JSON file stores,
    * the JSON the application actually writes to ``local_references/<id>.json``.

    The template is written in the application's own record shape - ``model``, ``pipeline``,
    ``dimensions``, ``embedding`` - so the byte count is the byte count of a real file rather
    than of a bare list of numbers.
    """
    array = np.asarray(embedding, dtype=np.float32)
    as_list = [float(v) for v in array]

    record = {
        "model": model_name,
        "pipeline": pipeline,
        "dimensions": int(array.size),
        "embedding": as_list,
    }
    serialized = json.dumps(record)

    return {
        "dimensions": int(array.size),
        "float32_bytes": int(array.nbytes),
        "boxed_list_bytes": int(sys.getsizeof(as_list)
                                + sum(sys.getsizeof(v) for v in as_list)),
        "json_bytes": len(serialized.encode("utf-8")),
        "template_path_example": f"local_references/<id>.json ({model_name})",
    }


def measure_space_geometry(images: dict[str, np.ndarray], pipeline: str) -> dict:
    """How far apart are different people, in each vector space, on the same photographs?

    This is the measurement that decides whether a ``MatchBand`` can be carried across a
    model change, and the answer is no for a reason that is arithmetic rather than
    philosophical: the bands are in the units of their own vector space. What *is* available
    here is the impostor floor - different people, same crop - which is one of the two
    boundaries a band is derived from. The genuine ceiling needs same-person pairs and this
    checkout has one photograph per worker, so it is reported as unavailable rather than
    estimated.
    """
    geometry: dict[str, dict] = {}
    names = sorted(images)

    for model_name in (MODEL_NAME, BASELINE_MODEL):
        vectors = {}
        for name in names:
            try:
                vectors[name] = measure_reference_embedding(model_name, images[name])
            except Exception as exc:  # noqa: BLE001 - reported, not hidden
                vectors[name] = None
                geometry.setdefault("_errors", []).append(f"{model_name}/{name}: {exc}")

        distances = []
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                if vectors.get(left) is None or vectors.get(right) is None:
                    continue
                distances.append({
                    "pair": f"{left} vs {right}",
                    "distance": cosine_distance(l2_normalize(vectors[left]),
                                                l2_normalize(vectors[right])),
                })
        distances.sort(key=lambda row: row["distance"])
        geometry[model_name] = {
            "dimensions": None if not vectors or all(v is None for v in vectors.values())
            else int(max(v.size for v in vectors.values() if v is not None)),
            "pairs": distances,
            # Deliberately *not* called an impostor floor. This checkout carries no identity
            # labels, so the harness cannot know whether two photographs are two people or
            # one person twice - and the two readings of the same number are opposite
            # conclusions (a tight acceptance line, or a false-accept risk). Naming it a floor
            # would assert the reading the data cannot support.
            "closest_pair_cosine": distances[0]["distance"] if distances else None,
            "identity_labelled": False,
            "pipeline": pipeline,
        }
    return geometry


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def print_export(info: dict, reused: bool) -> None:
    print_header("STAGE 1  Export to ONNX")
    print(f"  graph          : {info['path']}")
    print(f"  {'reused (already on disk)' if reused else 'written this run'}")
    print(f"  size           : {human_bytes(info['bytes'])} ({info['bytes']} B)")
    print(f"  nodes          : {info['nodes']}")
    print(f"  ir / opset     : {info['ir_version']} / "
          + ", ".join(f"{o['domain']}:{o['version']}" for o in info["opset"]))
    for entry in info["inputs"]:
        print(f"  input          : {entry['name']} {entry['shape']} {entry['dtype']}")
    for entry in info["outputs"]:
        print(f"  output         : {entry['name']} {entry['shape']}")
    top = list(info["op_types"].items())[:8]
    print("  op types       : " + ", ".join(f"{k}x{v}" for k, v in top))
    print(f"  normalisation  : {describe_normalisation_folding(info)}")
    if "export_s" in info:
        print(f"  export time    : {info['export_s'] * 1000:.1f} ms")


def print_parity(parity: dict, drift: dict) -> None:
    print_header("STAGE 2  Preprocessing parity and numerical drift")
    print(f"  tensor DeepFace fed the model: shape {parity['captured_shape']} "
          f"{parity['captured_dtype']}")
    print(f"    min {parity['captured_min']:.3f}  max {parity['captured_max']:.3f}  "
          f"mean {parity['captured_mean']:.3f}")
    print()
    print("  candidate preprocessing vs the captured tensor (lower max_abs is closer):")
    print(f"    {'variant':<36}{'max_abs':>13}{'mean_abs':>13}{'cosine':>11}{'bit exact':>11}")
    for name, row in parity["candidates"].items():
        if "max_abs" not in row:
            print(f"    {name:<36}{'shape mismatch':>13}  {row['shape_mismatch']}")
            continue
        print(f"    {name:<36}{row['max_abs']:>13.6f}{row['mean_abs']:>13.6f}"
              f"{row['cosine_to_captured']:>11.6f}{str(row['bit_exact']):>11}")
    print(f"  -> closest to the library: {parity['best_match']}")
    print(f"  -> reproduction of the library's preprocessing is bit-exact: "
          f"{parity['library_form_is_bit_exact']}")
    print(f"  -> the spec's own preprocessing differs from it by max_abs "
          f"{parity['spec_form_max_abs']:.3f}")
    print()
    print("  runtime drift on an identical input tensor:")
    print(f"    cosine distance (raw)          : {drift['cosine_raw']:.3e}"
          f"   {'PASS' if drift['cosine_raw'] < DRIFT_COSINE_TOL else 'FAIL'}"
          f" (tol {DRIFT_COSINE_TOL:.1e})")
    print(f"    cosine distance (L2-normalized): {drift['cosine_normalized']:.3e}"
          f"   {'PASS' if drift['cosine_normalized'] < DRIFT_COSINE_TOL else 'FAIL'}")
    print(f"    max absolute element difference: {drift['max_abs']:.3e}")
    print(f"    mean absolute difference       : {drift['mean_abs']:.3e}")
    print(f"    relative L2 difference         : {drift['relative_l2']:.3e}")
    print(f"    keras norm {drift['keras_norm']:.6f}   onnx norm {drift['onnx_norm']:.6f}")
    print()
    print("  the preprocessing gap - the same photograph, two preprocessing contracts:")
    print(f"    cosine distance, library form vs spec form: "
          f"{drift['spec_vs_library_space_cosine']:.4f}")
    print("    (a runtime drift of 1e-12 is free to migrate; this is not - it is a new "
          "vector space)")


def print_matrix(rows: dict) -> None:
    """The Benchmarking Results Matrix, measured values only."""
    print_header("Benchmarking Results Matrix (this host, this run)")
    labels = []
    for key in ("vgg", "facenet", "onnx"):
        if rows.get(key):
            labels.append((key, rows[key].get("label", key)))

    print(f"  {'Metric':<34}" + "".join(f"{lbl:>24}" for _, lbl in labels))
    print(f"  {'-' * 34}" + "-" * (24 * len(labels)))

    def line(metric: str, getter) -> None:
        cells = []
        for key, _ in labels:
            row = rows[key]
            value = getter(row)
            cells.append(f"{value:>24}" if value is not None else f"{'n/a':>24}")
        print(f"  {metric:<34}" + "".join(cells))

    line("Median forward pass (ms)", lambda r: f"{r['bench']['median_ms']:.1f}")
    line("Mean forward pass (ms)", lambda r: f"{r['bench']['mean_ms']:.1f}")
    line("p95 forward pass (ms)", lambda r: f"{r['bench']['p95_ms']:.1f}")
    line("Stdev (ms)", lambda r: f"{r['bench']['stdev_ms']:.2f}")
    line("Min / Max (ms)",
         lambda r: f"{r['bench']['min_ms']:.0f} / {r['bench']['max_ms']:.0f}")
    line("Warm-up (cold first call, ms)", lambda r: f"{r['bench']['warmup_ms']:.0f}")
    line("Cold import (ms)",
         lambda r: f"{(r['cold'].get('import_s') or 0) * 1000:.0f}" if r.get("cold") else None)
    line("Model instantiation (ms)",
         lambda r: f"{(r['cold'].get('build_s') or 0) * 1000:.0f}" if r.get("cold")
         else f"{r['load_ms']:.0f}")
    line("Idle RSS before inference", lambda r: human_bytes(r['bench']['rss_before']))
    line("RSS after inference", lambda r: human_bytes(r['bench']['rss_after']))
    line("Weights on disk", lambda r: human_bytes(r["weights_bytes"]))
    line("Template JSON on disk", lambda r: human_bytes(r["payload"]["json_bytes"]))
    line("Embedding dimension", lambda r: f"{r['payload']['dimensions']}")
    line("float32 buffer", lambda r: f"{r['payload']['float32_bytes']} B")
    line("Boxed Python floats", lambda r: human_bytes(r['payload']['boxed_list_bytes']))
    line("Cosine 1-to-1, scipy (app's call)",
         lambda r: f"{r['cosine']['scipy_median_ms'] * 1000:.1f} us")
    line("Cosine 1-to-1, numpy",
         lambda r: f"{r['cosine']['numpy_median_ms'] * 1000:.2f} us")
    line("Graph/weights file bytes",
         lambda r: human_bytes(r["graph_bytes"]) if r.get("graph_bytes") else None)


def print_reconciliation(rows: dict) -> None:
    """Reconcile this run against the recorded host ground truth.

    Two independent things can move a number between sessions: the host, and the measurement
    conditions. A single-model process is not a three-model process, so each row says which
    it was - and a delta large enough to change a decision is called out rather than left for
    the reader to notice.
    """
    print_header("Reconciliation against the recorded host ground truth")
    print(f"  {'engine':<26}{'metric':<22}{'recorded':>14}{'measured':>14}{'delta':>12}")
    print(f"  {'-' * 26}{'-' * 22}{'-' * 14}{'-' * 14}{'-' * 12}")

    def row(label: str, metric: str, recorded: float, measured: float) -> None:
        if not measured:
            return
        delta = (measured - recorded) / recorded * 100.0 if recorded else float("nan")
        print(f"  {label:<26}{metric:<22}{recorded:>14.1f}{measured:>14.1f}{delta:>+11.1f}%")

    def row_ratio(label: str, metric: str, recorded: float, measured: float) -> None:
        """Memory reconciles by ratio, not by percentage: the two are the same thing here."""
        if not measured:
            return
        print(f"  {label:<26}{metric:<22}{human_bytes(recorded):>14}"
              f"{human_bytes(measured):>14}{measured / recorded:>11.2f}x")

    for key in ("facenet", "vgg"):
        if not rows.get(key):
            continue
        row_data, recorded = rows[key], RECORDED[key]
        row(recorded["label"], "median forward (ms)", recorded["median_ms"],
            row_data["bench"]["median_ms"])
        row_ratio(recorded["label"], "idle RSS", recorded["rss"],
                  row_data["bench"]["rss_before"])

    if rows.get("onnx"):
        print(f"  {rows['onnx']['label']:<26}{'median forward (ms)':<22}{'no baseline':>14}"
              f"{rows['onnx']['bench']['median_ms']:>14.1f}{'-':>12}")
        print(f"  {rows['onnx']['label']:<26}{'idle RSS':<22}{'no baseline':>14}"
              f"{human_bytes(rows['onnx']['bench']['rss_before']):>14}{'-':>12}")


def print_concurrency(rows: dict) -> None:
    print_header("Concurrency sweep (per-call latency vs aggregate throughput)")
    for key in ("facenet", "onnx", "vgg"):
        row = rows.get(key)
        if not row or not row.get("concurrency"):
            continue
        print(f"  {row['label']}")
        print(f"    {'level':>6}{'calls':>8}{'per-call med (ms)':>20}"
              f"{'per-call max (ms)':>20}{'calls/s':>10}")
        for entry in row["concurrency"]:
            print(f"    {entry['concurrency']:>6}{entry['calls']:>8}"
                  f"{entry['per_call_median_ms']:>20.1f}{entry['per_call_max_ms']:>20.1f}"
                  f"{entry['calls_per_second']:>10.2f}")
        print()


def print_geometry(geometry: dict, provenance: dict | None = None) -> None:
    print_header("Vector-space geometry on this checkout's real photographs")
    if provenance:
        dropped = provenance.get("duplicates_dropped") or {}
        print(f"  {provenance['scanned']} image files scanned -> {provenance['unique']} distinct "
              f"({len(dropped)} duplicate(s) dropped)")
        print("  duplicate files are excluded rather than scored: a photograph against a copy of")
        print("  itself has distance 0 and would pass for the closest different-people pair")
        print()
    if not geometry:
        print("  (skipped: no photographs available, or --stage geometry not requested)")
        return
    for model_name, block in geometry.items():
        if model_name == "_errors":
            continue
        print(f"  {model_name} (dim {block['dimensions']})")
        if not block["pairs"]:
            print("    no usable pairs")
            continue
        for entry in block["pairs"]:
            print(f"    {entry['pair']:<64}{entry['distance']:>10.4f}")
        closest = block["closest_pair_cosine"]
        if closest is not None:
            print(f"    closest pair: {closest:.4f}  (identity NOT labelled - assumed, "
                  f"not known, to be two people)")
        print()
    pairs = sum(len(block["pairs"]) for key, block in geometry.items() if key != "_errors")
    print(f"  {pairs} distinct pair(s) per space is far too few to derive a line from - the")
    print("  production bands rest on 495 impostor pairs and 18 capture variants, and a")
    print("  genuine ceiling needs same-person pairs this checkout does not hold. So geometry")
    print("  is reported as context for 'the distances move', never as a proposed threshold.")
    if geometry.get("_errors"):
        print("  errors:", geometry["_errors"])


def print_cold_start(cold: dict) -> None:
    """The two rows that cannot be measured in this process, measured in another one."""
    print_header("Cold start, each engine in a fresh interpreter")
    print(f"  {'engine':<26}{'import':>9}{'instantiate':>13}{'ready':>9}"
          f"{'RSS loaded':>13}{'RSS serving':>13}")
    labels = (("facenet", RECORDED["facenet"]["label"]),
              ("vgg", RECORDED["vgg"]["label"]),
              ("onnx", OnnxEngine.name))
    for key, label in labels:
        block = cold.get(key)
        if not block:
            continue
        if "error" in block:
            print(f"  {label:<26}{'probe failed':>9}  {block['error'][:60]}")
            continue
        import_ms = (block.get("import_s") or 0.0) * 1000.0
        build_ms = (block.get("build_s") or 0.0) * 1000.0
        loaded = (block.get("rss_after_build") or 0) / 1024 ** 2
        serving = (block.get("rss_after_inference") or 0) / 1024 ** 2
        ready = block.get("ready_s") or (import_ms + build_ms) / 1000.0
        print(f"  {label:<26}{import_ms:>8.0f}ms{build_ms:>12.0f}ms{ready:>8.2f}s"
              f"{loaded:>12.0f}M{serving:>12.0f}M")
    print("  'ready' is import + instantiate: what a worker's first punch waits for.")
    print("  TensorFlow's import is shared by both Keras rows; the ONNX row never pays it,")
    print("  which is why a single-model ONNX worker is serving in a fraction of the time.")


def print_summary(rows: dict, drift: dict, info: dict) -> None:
    print_header("Verdict inputs")
    facenet = rows.get("facenet")
    onnx = rows.get("onnx")
    def memory_phrase(other_rss: float, onnx_rss: float) -> str:
        """Say which direction the memory went, instead of always claiming a saving.

        Both numbers come from one process that has already built every other model, so the
        resident set is shared and attribution is approximate - it is reported that way, and
        ``print_cold_start`` gives the per-engine figure that a deployment actually pays.
        """
        if not other_rss or not onnx_rss:
            return "resident memory unavailable"
        ratio = other_rss / onnx_rss
        if ratio >= 1.0:
            return f"{ratio:.2f}x less resident memory"
        return (f"{1 / ratio:.2f}x MORE resident memory in-process "
                f"(they share one working set; see cold start)")

    if facenet and onnx:
        speedup = facenet["bench"]["median_ms"] / onnx["bench"]["median_ms"]
        print(f"  ONNX vs Keras (same model, same input): {speedup:.2f}x faster, "
              + memory_phrase(facenet["bench"]["rss_before"], onnx["bench"]["rss_before"]))
        if facenet.get("cold") and onnx.get("cold"):
            print(f"  Cold start: keras {facenet['cold']['build_s'] * 1000:.0f} ms "
                  f"(+{facenet['cold']['import_s'] * 1000:.0f} ms import) vs onnx "
                  f"{onnx['cold']['build_s'] * 1000:.0f} ms (+{onnx['cold']['import_s'] * 1000:.0f} ms)")
    if rows.get("vgg") and onnx:
        vgg = rows["vgg"]
        print(f"  ONNX FaceNet vs shipped VGG-Face baseline: "
              f"{vgg['bench']['median_ms'] / onnx['bench']['median_ms']:.2f}x faster, "
              + memory_phrase(vgg["bench"]["rss_before"], onnx["bench"]["rss_before"]))
        print(f"  Template payload: {human_bytes(vgg['payload']['json_bytes'])} -> "
              f"{human_bytes(onnx['payload']['json_bytes'])}")
    print(f"  Parity: cosine drift {drift['cosine_normalized']:.2e} "
          f"({'within' if drift['cosine_normalized'] < DRIFT_COSINE_TOL else 'ABOVE'} "
          f"{DRIFT_COSINE_TOL:.0e})")
    problems = check_graph_is_not_degenerate(info)
    print(f"  Graph integrity: {'OK' if not problems else '; '.join(problems)}")


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def synthetic_crop(seed: int = 7) -> np.ndarray:
    """A deterministic pseudo-face so the harness runs on a host with no photographs.

    It is not a face and the drift probe does not need one - runtime parity is a statement
    about arithmetic, not about anatomy. Geometry is not run on this: distances between
    gradients would be numbers about graphics.
    """
    import cv2

    rng = np.random.default_rng(seed)
    canvas = np.zeros((240, 240, 3), dtype=np.uint8)
    for y in range(0, 240, 4):
        canvas[y:y + 4, :] = (y % 255, (y * 2) % 255, 128)
    for _ in range(6):
        centre = (int(rng.integers(60, 180)), int(rng.integers(60, 180)))
        axes = (int(rng.integers(12, 40)), int(rng.integers(12, 40)))
        colour = tuple(int(v) for v in rng.integers(60, 255, size=3))
        cv2.ellipse(canvas, centre, axes,
                    int(rng.integers(0, 180)), 0, 360, colour, -1)
    canvas = cv2.GaussianBlur(canvas, (5, 5), 0)
    return canvas


def _content_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_real_photographs(limit: int = 6) -> tuple[dict[str, np.ndarray], dict]:
    """Distinct photographs from the checkout, plus how many were duplicates.

    **De-duplication by content is load-bearing, and this was found by not doing it.**
    ``worker_photos`` holds three files of which two are byte-identical - one worker enrolled
    twice - and all 39 ``quick_link_photos`` are the *same* image. Fed to a pairwise probe
    those produce an "impostor floor" of 0.0000, which measures nothing: it is the distance
    from a photograph to a copy of itself, presented as the closest different-people pair.
    Distinct images are returned and the duplicates are reported, so the caller can see how
    thin the sample is instead of trusting a number that copies invented.

    Read-only: this script never writes to these directories.
    """
    import cv2

    repo = Path(__file__).resolve().parent.parent.parent
    found: dict[str, np.ndarray] = {}
    duplicates: dict[str, str] = {}
    originals: dict[str, str] = {}
    scanned = 0

    for folder in ("worker_photos", "quick_link_photos"):
        for path in sorted((repo / folder).glob("*.jpg")):
            scanned += 1
            digest = _content_digest(path)
            name = f"{folder}/{path.name}"
            if digest in originals:
                duplicates[name] = originals[digest]
                continue
            originals[digest] = name
            if len(found) >= limit:
                continue
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is not None:
                found[name] = image

    return found, {
        "scanned": scanned,
        "unique": len(originals),
        "duplicates_dropped": duplicates,
    }


def benchmark_keras_model(model_name: str, x: np.ndarray, passes: int,
                          sweep: tuple, weights_path: Path | None) -> dict:
    """Build, load and time one Keras-backed model end to end."""
    started = time.perf_counter()
    client = build_keras_model(model_name)
    load_ms = (time.perf_counter() - started) * 1000.0

    engine = KerasEngine(client.model, load_ms)
    embedding = engine.infer(x)
    bench = benchmark_engine(engine, x, passes)
    payload = payload_sizes(embedding, model_name, "yunet-2023mar")
    cosine = cosine_cost(embedding, l2_normalize(embedding))

    return {
        "label": RECORDED.get("vgg" if model_name == BASELINE_MODEL else "facenet",
                              {}).get("label", model_name),
        "model_name": model_name,
        "load_ms": load_ms,
        "weights_bytes": weights_path.stat().st_size if weights_path and weights_path.exists() else 0,
        "weights_path": str(weights_path) if weights_path else "",
        "bench": bench,
        "payload": payload,
        "cosine": cosine,
        "concurrency": concurrency_sweep(engine, x, sweep) if sweep else [],
        "embedding_norm": float(np.linalg.norm(embedding)),
        "embedding": embedding,
    }


def weights_file_for(model_name: str) -> Path | None:
    """Where DeepFace keeps this model's weights, for the disk-footprint row."""
    names = {"Facenet": "facenet_weights.h5", "VGG-Face": "vgg_face_weights.h5",
             "Facenet512": "facenet512_weights.h5"}
    filename = names.get(model_name)
    if not filename:
        return None
    root = Path(os.path.expanduser("~")) / ".deepface" / "weights" / filename
    return root if root.exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate FaceNet-128 on ONNX Runtime against the VGG-Face baseline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stage", default="all",
                        choices=["all", "export", "parity", "bench", "geometry"])
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="artifact directory (default: %(default)s)")
    parser.add_argument("--passes", type=int, default=DEFAULT_PASSES)
    parser.add_argument("--concurrency", default=",".join(str(v) for v in DEFAULT_CONCURRENCY),
                        help="comma-separated concurrency levels; empty to skip")
    parser.add_argument("--threads", type=int, default=None,
                        help="ORT intra-op threads (default: ORT chooses)")
    parser.add_argument("--image", action="append", default=None,
                        help="a real image for the parity probe (repeatable)")
    parser.add_argument("--no-vgg", action="store_true",
                        help="skip the VGG-Face baseline column (saves 553 MiB of weights)")
    parser.add_argument("--only", default="",
                        help="comma-separated subset of facenet,onnx,vgg for an uncontended "
                             "single-engine run (default: all)")
    parser.add_argument("--force", action="store_true", help="re-export even if it exists")
    parser.add_argument("--json", default=None, help="write raw results here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    use_utf8_stdout()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = out_dir / "facenet128.onnx"
    sweep = tuple(int(v) for v in args.concurrency.split(",") if v.strip())

    report: dict = {
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
            "machine": platform.machine(),
        },
        "spec": {"model": MODEL_NAME, "opset": OPSET, "input": [None, INPUT_SIZE, INPUT_SIZE, 3],
                 "drift_tolerance": DRIFT_COSINE_TOL, "passes": args.passes},
        "out_dir": str(out_dir),
    }
    fail = 0

    # -- what to measure on -----------------------------------------------
    if args.image:
        images = {}
        import cv2

        for path in args.image:
            image = cv2.imread(path, cv2.IMREAD_COLOR)
            if image is None:
                print(f"cannot read image: {path}", file=sys.stderr)
                return 2
            images[path] = image
    else:
        probe_images, _ = find_real_photographs(limit=1)
        images = probe_images or {"synthetic": synthetic_crop()}
    probe_name, probe_image = next(iter(images.items()))

    # -- stage 1 ----------------------------------------------------------
    report["clock"] = clock_integrity()
    report["rss_readable"] = rss_bytes() > 0
    if not args.quiet:
        print(f"host: {report['host']['platform']} | {report['host']['cpu_count']} CPUs | "
              f"python {report['host']['python']}")
        print(f"clock integrity: sleep {report['clock']['requested_s']}s measured "
              f"{report['clock']['measured_s']:.4f}s "
              f"(error {report['clock']['error_ms']:.2f} ms)")
        print(f"probe image: {probe_name} {probe_image.shape}")
        print(f"artifacts: {out_dir}")

    need_export = args.force or not onnx_path.exists()
    if need_export:
        keras_client = build_keras_model(MODEL_NAME)
        export_info = export_onnx(keras_client.model, onnx_path)
    else:
        export_info = describe_onnx(onnx_path)
        export_info["export_s"] = 0.0
    report["export"] = export_info
    if not args.quiet:
        print_export(export_info, reused=not need_export)

    problems = check_graph_is_not_degenerate(export_info)
    if problems:
        print("GRAPH INTEGRITY FAILED: " + "; ".join(problems), file=sys.stderr)
        fail = 1

    if args.stage == "export":
        _write_json(args.json, report)
        return fail

    # -- stage 2 ----------------------------------------------------------
    parity = compare_preprocessing(probe_image)
    report["parity_preprocessing"] = parity

    from deepface.modules import modeling  # memoised; the export stage already built it

    keras_client = modeling.build_model(task="facial_recognition", model_name=MODEL_NAME)
    captured_tensor, _ = capture_deepface_call(keras_client, probe_image)

    keras_engine = KerasEngine(keras_client.model, 0.0)
    onnx_engine = OnnxEngine(onnx_path, threads=args.threads, optimization="all")

    keras_raw = keras_engine.infer(captured_tensor)
    onnx_raw = onnx_engine.infer(captured_tensor)
    keras_norm = l2_normalize(keras_raw)
    onnx_norm = l2_normalize(onnx_raw)
    absolute = np.abs(keras_raw.astype(np.float64) - onnx_raw.astype(np.float64))

    # How far the *spec's* preprocessing moves the vector from the library's, on the same
    # photograph: the size of the recalibration a cutover to canonical FaceNet preprocessing
    # buys. Without it, "the drift is 5e-13" would read as "nothing changed", when what
    # changed invisibly is the preprocessing in front of the graph.
    spec_tensor = preprocess_standalone(probe_image)
    spec_vector = l2_normalize(keras_engine.infer(spec_tensor))
    library_space = cosine_distance(keras_norm, spec_vector)

    drift = {
        "spec_vs_library_space_cosine": library_space,
        "cosine_raw": cosine_distance(keras_raw, onnx_raw),
        "cosine_normalized": cosine_distance(keras_norm, onnx_norm),
        "max_abs": float(absolute.max()),
        "mean_abs": float(absolute.mean()),
        "relative_l2": float(np.linalg.norm(absolute)
                             / max(np.linalg.norm(keras_raw.astype(np.float64)), 1e-12)),
        "keras_norm": float(np.linalg.norm(keras_raw)),
        "onnx_norm": float(np.linalg.norm(onnx_raw)),
        "keras_first5": keras_norm[:5].round(7).tolist(),
        "onnx_first5": onnx_norm[:5].round(7).tolist(),
        "input_shape": list(captured_tensor.shape),
        "input_sha1": _sha1(captured_tensor),
    }
    report["parity_drift"] = drift
    if not args.quiet:
        print_parity(parity, drift)
    if drift["cosine_normalized"] >= DRIFT_COSINE_TOL:
        print(f"DRIFT EXCEEDS TOLERANCE ({drift['cosine_normalized']:.3e})", file=sys.stderr)
        fail = 1

    # -- stage 3 ----------------------------------------------------------
    if args.stage in ("all", "bench"):
        # ``--only`` exists because these rows contaminate each other. VGG-Face holds a
        # 2.3 GiB graph; measuring it in a process that has already built FaceNet measures
        # the pair, not the model. A single-engine run is the clean number, and this is how
        # a contended reading is told apart from a real one.
        wanted = {name.strip() for name in args.only.split(",") if name.strip()} or {
            "facenet", "onnx", "vgg"
        }
        if args.no_vgg:
            wanted.discard("vgg")
        unknown = wanted - {"facenet", "onnx", "vgg"}
        if unknown:
            print(f"unknown --only value(s): {sorted(unknown)}", file=sys.stderr)
            return 2

        rows: dict = {}
        cold = cold_start_probe(
            onnx_path, [MODEL_NAME] + ([BASELINE_MODEL] if "vgg" in wanted else []))
        report["cold_start"] = cold

        if "facenet" in wanted:
            rows["facenet"] = benchmark_keras_model(
                MODEL_NAME, captured_tensor, args.passes, sweep, weights_file_for(MODEL_NAME))
            rows["facenet"]["label"] = RECORDED["facenet"]["label"]
            rows["facenet"]["input_shape"] = list(captured_tensor.shape)

        if "onnx" in wanted:
            rows["onnx"] = {
                "label": OnnxEngine.name,
                "model_name": f"{MODEL_NAME} (ONNX)",
                "load_ms": onnx_engine.load_ms,
                "weights_bytes": export_info["bytes"],
                "graph_bytes": export_info["bytes"],
                "bench": benchmark_engine(onnx_engine, captured_tensor, args.passes),
                "payload": payload_sizes(onnx_norm, MODEL_NAME, "yunet-2023mar"),
                "cosine": cosine_cost(onnx_norm, l2_normalize(onnx_norm)),
                "concurrency": (
                    concurrency_sweep(onnx_engine, captured_tensor, sweep) if sweep else []
                ),
                "input_shape": list(captured_tensor.shape),
            }

        if "vgg" in wanted:
            # The baseline's own input contract: 224x224 through DeepFace's preprocessing,
            # captured the same way, so the two columns are measured on the crops each model
            # would really receive rather than on one crop wearing the other's shape.
            vgg_client = modeling.build_model(
                task="facial_recognition", model_name=BASELINE_MODEL)
            vgg_tensor, _ = capture_deepface_call(
                vgg_client, probe_image, model_name=BASELINE_MODEL)
            rows["vgg"] = benchmark_keras_model(
                BASELINE_MODEL, vgg_tensor, args.passes, sweep,
                weights_file_for(BASELINE_MODEL))
            rows["vgg"]["label"] = RECORDED["vgg"]["label"]
            rows["vgg"]["input_shape"] = list(vgg_tensor.shape)

        for key in list(rows):
            block = cold.get(key)
            if not block:
                continue
            if "error" in block:
                if not args.quiet:
                    print(f"cold-start probe for {key} failed: {block['error'][:120]}",
                          file=sys.stderr)
                continue
            rows[key]["cold"] = block

        for row in rows.values():
            row.pop("embedding", None)
        report["rows"] = rows
        if not args.quiet:
            print_matrix(rows)
            print_reconciliation(rows)
            print_cold_start(cold)
            print_concurrency(rows)
            print_summary(rows, drift, export_info)

    # -- geometry ---------------------------------------------------------
    if args.stage in ("all", "geometry"):
        real, provenance = find_real_photographs(limit=6)
        report["geometry_inputs"] = provenance
        if len(real) >= 2:
            geometry = measure_space_geometry(real, "yunet-2023mar")
            report["geometry"] = geometry
            if not args.quiet:
                print_geometry(geometry, provenance)
        elif not args.quiet:
            print_header("Vector-space geometry")
            print(f"  only {len(real)} distinct photograph(s) available; need 2+ - skipped")

    _write_json(args.json or str(out_dir / "facenet_onnx_eval.json"), report)
    if not args.quiet:
        print()
        print(f"raw results: {args.json or str(out_dir / 'facenet_onnx_eval.json')}")
    return fail


def _sha1(array: np.ndarray) -> str:
    import hashlib

    return hashlib.sha1(np.ascontiguousarray(array, dtype=np.float64).tobytes()).hexdigest()[:16]


def _write_json(path: str | None, report: dict) -> None:
    if not path:
        return
    def default(obj):
        if isinstance(obj, (np.floating, np.integer)):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return str(obj)

    Path(path).write_text(json.dumps(report, indent=2, default=default), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
