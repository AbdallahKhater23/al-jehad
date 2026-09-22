"""FaceNet in ONNX Runtime: session configuration, IOBinding, and host-side L2 normalization.

WHAT IS AND IS NOT IN THE GRAPH (measured on ``models/facenet128.onnx``)
-----------------------------------------------------------------------
    ir_version 8   opset ai.onnx 17   producer tf2onnx 1.17.0
    input   input               [N, 160, 160, 3]  float32   NHWC, dynamic batch
    output  Bottleneck_BatchNorm [N, 128]         float32
    initializers 271   parameters 22,779,445   weights 91.12 MB   (file 91,229,417 B)
    nodes 336 : Conv 132, Relu 131, Concat 23, Add 22, Mul 21, MaxPool 3,
                GlobalAveragePool 1, Squeeze 1, MatMul 1, Transpose 1

Two facts decide this module's design:

1. **There is no L2 normalization in the graph.** No ``Square``/``ReduceSum``/``Sqrt``/``Div``/
   ``Maximum`` node exists. The tail is ``Block8_6 -> Add (residual) -> GlobalAveragePool ->
   Squeeze -> MatMul W[1792,128] -> Mul(scale[128]) -> Add(bias[128])`` - a folded BatchNormalization.
   MEASURED output norm: **5.7762** on random input, **6.0189** on uniform input. Not 1.0. A pipeline
   that treats this output as a unit vector computes a cosine that is off by a scale-dependent
   factor, and the resulting band is meaningless while looking entirely plausible.
2. **The projection is a single ``MatMul`` of 1792x128** (229,376 MACs). At 512 the same node is
   1792x512 (917,504 MACs), i.e. **+688,512 params / +2.63 MiB fp32** and **+0.001 ms** of CPU work
   against a measured 11.0 ms forward pass at 8 threads. The width is not a performance decision.

SESSION POLICY, AND WHY
-----------------------
* ``ORT_ENABLE_ALL``: Conv+BN folding and constant folding are what make the measured 32.4 ms -> 11.0 ms
  scaling possible; without them the graph executes the unfused form it was exported in.
* ``intra_op_num_threads = physical cores``, ``inter_op_num_threads = 1``: one request at a time is
  *latency*-bound, and inter-op parallelism only helps when several nodes can run concurrently - here
  the graph is a chain.
* ``enable_mem_pattern`` is disabled when IOBinding is used: bound buffers bypass arena planning, and
  leaving it on makes ORT plan an arena it will not use.
* warmup: the arena is sized on the first ``run()``; a 91 MB graph's first inference is otherwise a
  visible latency spike on the first worker of the morning.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np
import onnxruntime as ort

log = logging.getLogger("attendance.facenet")

#: The existing application's ``l2_normalize`` policy: a zero-norm vector is returned unchanged
#: rather than divided by a floor. Scaling it by an epsilon would invent a direction for a vector
#: that has none; refusing it belongs to the comparison, which is where ``cosine_distance`` raises.
NORM_FLOOR: Final = 0.0

#: TensorRT needs concrete shapes; the export's batch dim is symbolic (``unk__1196``).
_DEFAULT_PROFILE: Final = "input:1x160x160x3"


class EmbeddingError(RuntimeError):
    """The graph could not be loaded, or could not produce an embedding."""


class DimensionMismatch(EmbeddingError):
    """A probe and a gallery template of different widths. Never comparable - always a bug."""


@dataclass(frozen=True)
class GraphFacts:
    """Everything a band, a log line or an incident report needs to identify this graph."""

    model_id: str
    path: str
    input_name: str
    input_shape: tuple[Any, ...]
    output_name: str
    width: int
    providers: tuple[str, ...]
    channels_last: bool
    input_size: int

    def describe(self) -> str:
        return (
            f"{Path(self.path).name} dim={self.width} model={self.model_id[:12]} "
            f"input={self.input_name}{list(self.input_shape)} providers={list(self.providers)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "file": Path(self.path).name,
            "width": self.width,
            "input_name": self.input_name,
            "input_shape": [str(d) for d in self.input_shape],
            "output_name": self.output_name,
            "providers": list(self.providers),
            "channels_last": self.channels_last,
            "input_size": self.input_size,
        }


def sha256_of(path: Path) -> str:
    """The graph's identity. Bands are keyed by this, not by a filename somebody may overwrite."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def provider_specs(requested: Sequence[str] | None = None) -> tuple[list[Any], list[str]]:
    """``(provider specs, names actually available)``, falling back loudly rather than silently.

    A deployment that asks for CUDA and gets CPU must be able to find that out from a log line -
    the alternative is a latency incident investigated for a day against the wrong hypothesis.
    """
    available = set(ort.get_available_providers())
    wanted = list(requested) if requested else [
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    specs: list[Any] = []
    chosen: list[str] = []
    for name in wanted:
        if name not in available:
            continue
        if name == "TensorrtExecutionProvider":
            specs.append(
                (
                    name,
                    {
                        "trt_fp16_enable": True,
                        "trt_engine_cache_enable": True,
                        "trt_engine_cache_path": os.environ.get("TRT_CACHE", "./trt_cache"),
                        "trt_max_workspace_size": 1 << 30,
                        "trt_profile_min_shapes": _DEFAULT_PROFILE,
                        "trt_profile_opt_shapes": _DEFAULT_PROFILE,
                        "trt_profile_max_shapes": "input:8x160x160x3",
                    },
                )
            )
        elif name == "CUDAExecutionProvider":
            specs.append(
                (
                    name,
                    {
                        "arena_extend_strategy": "kSameAsRequested",
                        "cudnn_conv_algo_search": "HEURISTIC",
                        "do_copy_in_default_stream": True,
                    },
                )
            )
        else:
            specs.append(name)
        chosen.append(name)
    if "CPUExecutionProvider" not in chosen:
        specs.append("CPUExecutionProvider")
        chosen.append("CPUExecutionProvider")
    if requested and chosen[0] not in requested:
        log.warning(
            "requested EP %s unavailable; running on %s (available: %s)",
            list(requested), chosen[0], sorted(available),
        )
    return specs, chosen


class FaceNetORT:
    """One FaceNet graph: one session, one contract-checked input, unit-norm output."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        providers: Sequence[str] | None = None,
        intra_threads: int | None = None,
        use_iobinding: bool = True,
        warmup: bool = True,
        optimization: str = "all",
        device_id: int = 0,
    ) -> None:
        path = Path(model_path)
        if not path.exists():
            raise EmbeddingError(f"ONNX graph not found: {path}")
        self.path = str(path)
        self.model_id = sha256_of(path)
        self.device_id = int(device_id)

        options = ort.SessionOptions()
        levels = {
            "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
            "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
            "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
        }
        if optimization not in levels:
            raise EmbeddingError(f"unknown optimization level {optimization!r}; expected {sorted(levels)}")
        options.graph_optimization_level = levels[optimization]
        options.intra_op_num_threads = int(intra_threads or max(1, (os.cpu_count() or 2)))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = not use_iobinding
        options.enable_cpu_mem_arena = True
        options.log_severity_level = 3

        specs, chosen = provider_specs(providers)
        try:
            self._session = ort.InferenceSession(self.path, options, providers=specs)
        except Exception as exc:  # noqa: BLE001 - a session that cannot be built is fatal
            raise EmbeddingError(f"could not create an InferenceSession for {path.name}: {exc}") from exc

        inputs, outputs = self._session.get_inputs(), self._session.get_outputs()
        if len(inputs) != 1 or not outputs:
            raise EmbeddingError(f"expected 1 input and >= 1 output, got {len(inputs)} and {len(outputs)}")
        self._input_name = inputs[0].name
        self._output_name = outputs[0].name
        shape = list(inputs[0].shape)
        if len(shape) != 4:
            raise EmbeddingError(f"expected a 4-D input, got {shape}")
        channels_last = shape[-1] == 3
        if not channels_last and shape[1] != 3:
            raise EmbeddingError(f"input {shape} is neither NHWC nor NCHW with 3 channels")
        spatial = int(shape[1] if channels_last else shape[2])
        if spatial <= 0:
            raise EmbeddingError(f"input {shape} has a non-static spatial size")
        try:
            width = int(outputs[0].shape[-1])
        except (TypeError, ValueError) as exc:
            raise EmbeddingError(f"output {outputs[0].shape} has no static embedding width") from exc
        if width <= 0:
            raise EmbeddingError(f"output {outputs[0].shape} has a non-static embedding width")

        self.channels_last = channels_last
        self.input_size = spatial
        self.width = width
        self.facts = GraphFacts(
            model_id=self.model_id,
            path=self.path,
            input_name=self._input_name,
            input_shape=tuple(shape),
            output_name=self._output_name,
            width=width,
            providers=tuple(chosen),
            channels_last=channels_last,
            input_size=spatial,
        )
        self._device = "cuda" if chosen[0] in ("CUDAExecutionProvider", "TensorrtExecutionProvider") else "cpu"
        self._use_iobinding = bool(use_iobinding)
        self._binding: Any | None = self._session.io_binding() if use_iobinding else None
        if warmup:
            self.warm()
        log.info("face embedder ready: %s", self.facts.describe())

    # -- shape contract -------------------------------------------------------
    @property
    def tensor_shape(self) -> tuple[int, ...]:
        """``(1, S, S, 3)`` or ``(1, 3, S, S)`` - what ``embed`` accepts, from the graph itself."""
        if self.channels_last:
            return (1, self.input_size, self.input_size, 3)
        return (1, 3, self.input_size, self.input_size)

    def warm(self, rounds: int = 2) -> float:
        """Force arena allocation and (with TRT) engine build before serving traffic.

        Returns the warm pass's duration in milliseconds, which the triage output prints: a deployment
        whose warmup is 900 ms is one whose first worker of the day waits 900 ms.
        """
        dummy = np.zeros(self.tensor_shape, dtype=np.float32)
        started = time.perf_counter()
        for _ in range(max(1, rounds)):
            self._run(dummy)
        elapsed = (time.perf_counter() - started) * 1000.0 / max(1, rounds)
        log.info("warmup: %.2f ms per pass on %s", elapsed, self.facts.providers[0])
        return elapsed

    # -- inference ------------------------------------------------------------
    def _run(self, tensor: np.ndarray) -> np.ndarray:
        if tensor.shape != self.tensor_shape:
            raise EmbeddingError(
                f"tensor {tensor.shape} does not match the graph's {self.tensor_shape}"
            )
        payload = np.ascontiguousarray(tensor, dtype=np.float32)
        if self._binding is None:
            try:
                return self._session.run([self._output_name], {self._input_name: payload})[0]
            except Exception as exc:  # noqa: BLE001 - surfaced with context, never swallowed
                raise EmbeddingError(f"inference failed: {type(exc).__name__}: {exc}") from exc
        binding = self._binding
        try:
            if self._device == "cpu":
                binding.bind_cpu_input(self._input_name, payload)
                binding.bind_output(self._output_name, "cpu")
            else:
                # The documented OrtValue path: one device buffer for the input, one for the output,
                # and no host<->device copy inside the timing.
                input_value = ort.OrtValue.ortvalue_from_numpy(payload, self._device, self.device_id)
                output_shape = (tensor.shape[0], self.width)
                output_value = ort.OrtValue.ortvalue_from_shape_and_type(
                    output_shape, np.float32, self._device, self.device_id
                )
                binding.bind_ortvalue_input(self._input_name, input_value)
                binding.bind_ortvalue_output(self._output_name, output_value)
            self._session.run_with_iobinding(binding)
            if self._device != "cpu":
                binding.synchronize_outputs()
                return np.asarray(binding.get_outputs()[0].numpy())
            outputs = binding.copy_outputs_to_cpu()
            return np.asarray(outputs[0])
        except Exception as exc:  # noqa: BLE001 - one error type out of the module
            raise EmbeddingError(f"IOBinding inference failed: {type(exc).__name__}: {exc}") from exc
        finally:
            binding.clear_binding_inputs()
            binding.clear_binding_outputs()

    # -- public API -----------------------------------------------------------
    def embed(self, tensor: np.ndarray) -> np.ndarray:
        """One aligned crop -> a ``(width,)`` float32 **unit** vector.

        The L2 stage is host-side on purpose: keeping ``Square/ReduceSum/Sqrt/Div`` off-graph avoids
        an unfusable elementwise chain, an extra arena tensor, and float32 accumulation error inside
        a runtime the application does not control. It also makes the normalization auditable in one
        place, which is what the measured 5.78-vs-1.0 defect needed.
        """
        raw = np.asarray(self._run(tensor), dtype=np.float32).reshape(-1)
        if raw.size != self.width:
            raise EmbeddingError(f"graph returned {raw.size} values; declared width {self.width}")
        return l2_normalize(raw)

    def embed_batch(self, tensors: Sequence[np.ndarray]) -> np.ndarray:
        """``(N, width)`` unit vectors. One forward pass; each row normalized independently."""
        if not tensors:
            return np.zeros((0, self.width), dtype=np.float32)
        stacked = np.concatenate([np.asarray(t, dtype=np.float32) for t in tensors], axis=0)
        if stacked.shape[1:] != self.tensor_shape[1:]:
            raise EmbeddingError(f"batch tensor {stacked.shape} does not match {self.tensor_shape[1:]}")
        raw = np.asarray(self._run(stacked), dtype=np.float64)
        if raw.ndim == 1:
            raw = raw[None, :]
        return np.stack([l2_normalize(row) for row in raw])

    def embed_raw(self, tensor: np.ndarray) -> np.ndarray:
        """The graph's raw output, for diagnostics - explicitly *not* a usable embedding."""
        return np.asarray(self._run(tensor), dtype=np.float32).reshape(-1)

    def distance(self, reference: np.ndarray, live: np.ndarray) -> float:
        return cosine_distance(reference, live)


@dataclass(frozen=True)
class Timing:
    passes: int
    median_ms: float
    p10_ms: float
    p90_ms: float

    def as_dict(self) -> dict[str, float]:
        return {
            "passes": float(self.passes),
            "median_ms": round(self.median_ms, 3),
            "p10_ms": round(self.p10_ms, 3),
            "p90_ms": round(self.p90_ms, 3),
        }


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    """Unit vector, float64 accumulation, zero-norm returned unchanged.

    Float64 is not pedantry here. Summing 512 squares in float32 loses ~1e-6 relative precision,
    and a 128-D and a 512-D template normalized with different precisions cannot be compared
    bit-for-bit - which matters when the whole migration argument is "the measured distances are
    the same quantity under a wider encoder".
    """
    array = np.asarray(vector, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(array))
    if norm <= NORM_FLOOR:
        return array.astype(np.float32)
    return (array / norm).astype(np.float32)


def cosine_distance(reference: np.ndarray, live: np.ndarray) -> float:
    """``1 - cos`` - the deployment's metric (``scipy.spatial.distance.cosine``), on unit vectors.

    The dimension guard is a hard error rather than a warning. Comparing a 128-D template with a
    512-D probe has no meaning; truncating or padding to make the shapes agree is how a migration
    turns into a silent refund of every refused clock-in.
    """
    a = np.asarray(reference, dtype=np.float64).reshape(-1)
    b = np.asarray(live, dtype=np.float64).reshape(-1)
    if a.size != b.size:
        raise DimensionMismatch(f"cannot compare a {a.size}-D reference with a {b.size}-D probe")
    norm_a, norm_b = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        raise EmbeddingError("zero-norm embedding: the graph produced no direction to compare")
    return float(1.0 - float(a @ b) / (norm_a * norm_b))


def time_embedder(embedder: FaceNetORT, *, passes: int = 15, warm: int = 3) -> Timing:
    """Single-batch latency, median and deciles. The measurement the migration argument rests on.

    Reported as a distribution rather than a mean because the interesting question is whether a
    change is *visible*: on this host the 128-D graph measures 11.0 ms at 8 threads with a p10-p90
    spread of ~1.5 ms, and the 512-D projection's arithmetic is 0.001 ms - so it cannot be seen, and
    a report claiming otherwise is reporting noise.
    """
    tensor = np.zeros(embedder.tensor_shape, dtype=np.float32)
    for _ in range(max(1, warm)):
        embedder.embed(tensor)
    samples: list[float] = []
    for _ in range(max(1, passes)):
        started = time.perf_counter()
        embedder.embed(tensor)
        samples.append((time.perf_counter() - started) * 1000.0)
    array = np.asarray(samples)
    return Timing(
        passes=len(samples),
        median_ms=float(np.median(array)),
        p10_ms=float(np.percentile(array, 10)),
        p90_ms=float(np.percentile(array, 90)),
    )
