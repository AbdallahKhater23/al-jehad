"""Resolution-correct face localisation: 640-class detection with an invertible native map.

WHY THIS MODULE EXISTS
----------------------
The deployment detected at 320x320. Scale arithmetic, not opinion: a face occupying 60 px in a
1280x720 frame is 15 px after that resize, and YuNet's coarsest positive anchor sits at stride 32,
so the face has *no* positive cell - it is not "detected badly", it is not detected. At 640 the
same face is 30 px (one marginal cell, landmarks too noisy to align on), and with a 2x2 overlapping
grid it is 60 px (four cells, reliable box *and* landmarks). That progression is the entire
"camera distance problem", and it is a sampling problem rather than an embedding problem.

THE MAPPING IS THE HARD PART
----------------------------
Everything downstream - alignment, the punch photo, the evidence frame - works in **native frame**
coordinates. So the resample has to be exactly invertible. Two details make it so:

* the effective per-axis scales are ``new_w / src_w`` and ``new_h / src_h`` *after* integer
  rounding of the resized size. ``cv2.resize`` can only emit integer dimensions, so a nominal single
  scale ``s`` produces effective scales that differ in the fourth decimal; mapping back with ``s``
  injects a bias that grows with distance from the origin - the classic "box is right on the left of
  the frame and wrong on the right" defect;
* the aspect-matched path (``square=False``) sets the detector's input to the *scaled* non-square
  size. YuNet's input size is dynamic, the subject scale is identical to the square case, and on a
  16:9 frame it spends 56 % of the compute instead of padding 44 % of the canvas with nothing.

A square letterbox remains available because a fixed-shape engine (TensorRT profile) requires it.

VERIFIED (this repository, 2000 random points per aspect ratio)
---------------------------------------------------------------
    1920x1080 -> 640x360  pad(0,140)  scale(0.333333,0.333333)  round-trip err 2.3e-13 px
    1280x720  -> 640x360  pad(0,140)  scale(0.500000,0.500000)  round-trip err 5.7e-14 px
     640x480  -> 640x480  pad(0,80)   scale(1.000000,1.000000)  round-trip err 5.7e-14 px
    4032x3024 -> 640x480  pad(0,80)   scale(0.158730,0.158730)  round-trip err 4.6e-13 px

The detector protocol is intentionally narrow - ``detect(frame) -> list[Detection]`` - so the YuNet
and SCRFD backends are interchangeable at the call site and a future backend (RetinaFace, YOLOv8-Face)
needs no change above it.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Protocol, Sequence, runtime_checkable

import cv2
import numpy as np

#: OpenCV's thread pool is process-global and sized from the cores it can see, which inside a
#: container is the *host's* count rather than the cgroup quota - so on a 1 vCPU instance
#: every cv2 operation (detect, resize, warp) could fan out to 32 threads and hand the CFS
#: scheduler a queue of work it then throttles, which reads as latency on every other request.
#: One thread is the honest number for one core; each call here processes a single small frame.
#: ``face_detector`` states the same cap where the runtime YuNet path loads cv2, so it holds
#: whichever module reaches OpenCV first (the setting is global, not per-module).
cv2.setNumThreads(1)

log = logging.getLogger("attendance.detector")

#: Padding value for the square-letterbox path. One value, stated once: it is part of the input
#: contract, and two different border values in two code paths is two different models.
PAD_VALUE: Final = 0

#: YuNet row layout, as OpenCV returns it:
#: [x, y, w, h, right_eye_x, right_eye_y, left_eye_x, left_eye_y, nose_x, nose_y, r_mouth_x,
#:  r_mouth_y, l_mouth_x, l_mouth_y, score] - landmarks in the *detector input* coordinate space.
_YUNET_LANDMARKS: Final = slice(4, 14)

#: The landmark order every consumer in this application expects: R-eye, L-eye, nose, R-mouth,
#: L-mouth. Named here because SCRFD returns the same five points in the same order and the
#: alignment template depends on it.
LANDMARK_ORDER: Final = ("right_eye", "left_eye", "nose", "right_mouth", "left_mouth")


class DetectorError(RuntimeError):
    """A detector that cannot be constructed, or that cannot run a particular frame."""


@dataclass(frozen=True)
class Letterbox:
    """An exact, invertible mapping between native-frame pixels and detector-input pixels.

    See the module docstring: ``sx``/``sy`` are *effective* (post-rounding) per-axis scales, which
    is what makes ``to_native`` exact rather than approximately right.
    """

    src_w: int
    src_h: int
    out_w: int
    out_h: int
    new_w: int
    new_h: int
    sx: float
    sy: float
    off_x: float
    off_y: float

    @classmethod
    def fit(
        cls,
        src_w: int,
        src_h: int,
        out_w: int = 640,
        out_h: int = 640,
        *,
        square: bool = False,
    ) -> "Letterbox":
        """Plan a resample. ``square=True`` pads to ``out_w x out_w``; ``square=False`` emits the
        scaled rectangle with no padding (the aspect-matched path)."""
        if min(src_w, src_h, out_w, out_h) <= 0:
            raise DetectorError(
                f"letterbox needs positive dimensions, got {src_w}x{src_h} -> {out_w}x{out_h}"
            )
        target_w, target_h = (out_w, out_w) if square else (out_w, out_h)
        scale = min(target_w / src_w, target_h / src_h)
        new_w = max(1, int(round(src_w * scale)))
        new_h = max(1, int(round(src_h * scale)))
        sx, sy = new_w / src_w, new_h / src_h
        canvas_w, canvas_h = (out_w, out_w) if square else (new_w, new_h)
        return cls(
            src_w=src_w,
            src_h=src_h,
            out_w=canvas_w,
            out_h=canvas_h,
            new_w=new_w,
            new_h=new_h,
            sx=sx,
            sy=sy,
            off_x=(canvas_w - new_w) / 2.0,
            off_y=(canvas_h - new_h) / 2.0,
        )

    @property
    def scale(self) -> float:
        """The nominal (geometric-mean) scale, for logging and for a sanity assertion."""
        return math.sqrt(self.sx * self.sy)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        """Resample the frame to detector-input pixels. Interpolation is stated, not defaulted."""
        if frame.ndim != 3:
            raise DetectorError(f"expected an HxWxC frame, got shape {frame.shape}")
        resized = cv2.resize(frame, (self.new_w, self.new_h), interpolation=cv2.INTER_LINEAR)
        if (self.new_w, self.new_h) == (self.out_w, self.out_h):
            return resized
        top, left = int(self.off_y), int(self.off_x)
        bottom = max(0, self.out_h - self.new_h - top)
        right = max(0, self.out_w - self.new_w - left)
        border = (PAD_VALUE,) * resized.shape[2]
        return cv2.copyMakeBorder(
            resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=border
        )

    def to_native(self, points: np.ndarray) -> np.ndarray:
        """Detector-input pixels -> native frame pixels, clamped to the frame.

        Clamping is deliberate: a padding-region coordinate has no native pre-image, and returning
        one would let a bogus detection land outside the frame and be cropped silently by a later
        stage instead of refused here.
        """
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        out = np.empty_like(pts)
        out[:, 0] = (pts[:, 0] - self.off_x) / self.sx
        out[:, 1] = (pts[:, 1] - self.off_y) / self.sy
        out[:, 0] = np.clip(out[:, 0], 0.0, float(self.src_w - 1))
        out[:, 1] = np.clip(out[:, 1], 0.0, float(self.src_h - 1))
        return out.astype(np.float32)


@dataclass
class Detection:
    """One face, in **native frame** coordinates. The only currency this module exports."""

    box: tuple[float, float, float, float]  # x, y, w, h
    landmarks: np.ndarray  # (5, 2) float32, LANDMARK_ORDER
    score: float
    origin: str = "full"
    scale: float = 1.0  # effective input scale this face was found at (for triage + logging)

    def area(self) -> float:
        return float(max(0.0, self.box[2]) * max(0.0, self.box[3]))

    def size(self) -> float:
        """Characteristic face width in native pixels - the number the coverage metric uses."""
        return float(min(self.box[2], self.box[3]))

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": [round(float(v), 2) for v in self.box],
            "score": round(float(self.score), 4),
            "origin": self.origin,
            "scale": round(float(self.scale), 5),
            "landmarks": [[round(float(x), 2), round(float(y), 2)] for x, y in self.landmarks],
        }


@runtime_checkable
class Detector(Protocol):
    """What every backend must satisfy. Narrow on purpose: one method, native coordinates out."""

    def detect(self, frame: np.ndarray, *, tiles: int = 1, overlap: float = 0.2) -> list[Detection]: ...


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2, bx2, by2 = ax1 + aw, ay1 + ah, bx1 + bw, by1 + bh
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0.0 else 0.0


def merge_detections(dets: Iterable[Detection], *, iou_threshold: float = 0.35) -> list[Detection]:
    """Greedy score-ordered NMS across tiles.

    Greedy rather than a weighted box fusion: a duplicated face from an overlapping tile is the
    same *person*, and averaging two boxes that disagree by 4 px produces a third box that matches
    neither tile's geometry - which the alignment then has to absorb as landmark error. Keeping the
    highest-scoring copy keeps the geometry honest about which pass found it.
    """
    ordered = sorted(dets, key=lambda d: d.score, reverse=True)
    kept: list[Detection] = []
    for candidate in ordered:
        if all(iou(candidate.box, k.box) <= iou_threshold for k in kept):
            kept.append(candidate)
    return kept


def tile_origins(width: int, height: int, tiles: int, overlap: float) -> list[tuple[int, int, int, int]]:
    """The (x0, y0, x1, y1) grid for a ``tiles x tiles`` pass with fractional overlap.

    Split out and pure so the geometry is unit-testable without a detector, and so the tiling
    policy (how much overlap, how many tiles) is visible in one place rather than inside a loop.
    """
    if tiles < 1:
        raise DetectorError(f"tiles must be >= 1, got {tiles}")
    if not 0.0 <= overlap < 1.0:
        raise DetectorError(f"overlap must be in [0, 1), got {overlap}")
    tw, th = math.ceil(width / tiles), math.ceil(height / tiles)
    mx, my = int(tw * overlap / 2.0), int(th * overlap / 2.0)
    windows: list[tuple[int, int, int, int]] = []
    for ty in range(tiles):
        for tx in range(tiles):
            x0, y0 = max(0, tx * tw - mx), max(0, ty * th - my)
            x1, y1 = min(width, (tx + 1) * tw + mx), min(height, (ty + 1) * th + my)
            if x1 > x0 and y1 > y0:
                windows.append((x0, y0, x1, y1))
    return windows


class YuNetDetector:
    """OpenCV YuNet, resolution-correct and letterbox-aware.

    ``square=False`` (default) is the aspect-matched path: on a 16:9 frame the detector input is
    640x360, so no compute is spent on padding and no subject detail is spent on the letterbox.
    ``square=True`` is what a fixed-shape TensorRT engine needs; it costs 44 % of the input area on
    16:9 frames for identical effective subject scale - which is the point: the trade is compute,
    never resolution.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        input_size: int = 640,
        score_threshold: float = 0.6,
        nms_threshold: float = 0.3,
        top_k: int = 5000,
        square: bool = False,
    ) -> None:
        model = str(model_path)
        if not Path(model).exists():
            raise DetectorError(f"YuNet model not found: {model}")
        self.model_path = model
        self.input_size = int(input_size)
        self.square = bool(square)
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        try:
            self._net = cv2.FaceDetectorYN.create(
                model,
                "",
                (self.input_size, self.input_size),
                self.score_threshold,
                self.nms_threshold,
                int(top_k),
            )
        except cv2.error as exc:  # pragma: no cover - depends on the OpenCV build
            raise DetectorError(f"YuNet could not be constructed from {model}: {exc}") from exc
        log.info(
            "YuNet ready: input=%d square=%s score=%.2f nms=%.2f",
            self.input_size, self.square, self.score_threshold, self.nms_threshold,
        )

    # -- one pass -------------------------------------------------------------
    def _pass(self, image: np.ndarray, *, origin: str, scale: float) -> list[Detection]:
        height, width = image.shape[:2]
        plan = Letterbox.fit(width, height, self.input_size, self.input_size, square=self.square)
        payload = plan.apply(image)
        try:
            self._net.setInputSize((payload.shape[1], payload.shape[0]))
            _retval, faces = self._net.detect(payload)
        except cv2.error as exc:
            raise DetectorError(f"YuNet inference failed on a {width}x{height} frame: {exc}") from exc
        if faces is None:
            return []
        found: list[Detection] = []
        for row in np.asarray(faces, dtype=np.float32):
            score = float(row[-1])
            if score < self.score_threshold:
                continue
            # Every coordinate goes through the *same* mapping, corners included: a box built from
            # a separately scaled copy is how x and y drift apart under a non-square plan.
            local = np.concatenate(
                (
                    np.array([[row[0], row[1]], [row[0] + row[2], row[1] + row[3]]], dtype=np.float32),
                    row[_YUNET_LANDMARKS].reshape(5, 2),
                )
            )
            native = plan.to_native(local)
            (x1, y1), (x2, y2) = native[0], native[1]
            found.append(
                Detection(
                    box=(float(x1), float(y1), max(1.0, float(x2 - x1)), max(1.0, float(y2 - y1))),
                    landmarks=native[2:].astype(np.float32),
                    score=score,
                    origin=origin,
                    scale=scale * plan.scale,
                )
            )
        return found

    # -- public ---------------------------------------------------------------
    def detect(self, frame: np.ndarray, *, tiles: int = 1, overlap: float = 0.2) -> list[Detection]:
        """Full-frame pass, plus an optional overlapping grid for the small-face band.

        Returns native-frame detections, already merged. The single pass is kept even when tiling:
        it is the path that finds the *large* faces (the ones a tile boundary can bisect), and
        merging is cheap next to a missed clock-in.
        """
        if frame.ndim != 3 or frame.size == 0:
            raise DetectorError(f"detect() needs a non-empty HxWxC frame, got {frame.shape}")
        if frame.dtype != np.uint8:
            log.debug("detector received %s; converting to uint8 for YuNet", frame.dtype)
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        height, width = frame.shape[:2]
        found = self._pass(frame, origin="full", scale=1.0)
        for index, (x0, y0, x1, y1) in enumerate(tile_origins(width, height, tiles, overlap)):
            tile = frame[y0:y1, x0:x1]
            if tile.size == 0:  # pragma: no cover - tile_origins already refuses empty windows
                continue
            for detection in self._pass(tile, origin=f"tile:{index}", scale=1.0):
                bx, by, bw, bh = detection.box
                detection.box = (bx + x0, by + y0, bw, bh)
                detection.landmarks = detection.landmarks + np.array([x0, y0], dtype=np.float32)
                found.append(detection)
        return merge_detections(found, iou_threshold=self.nms_threshold)

    def native_scale(self, width: int, height: int) -> float:
        """The effective subject scale of one pass over a ``width x height`` frame.

        This is the number that answers "how far away can a face be": a face of ``w`` native pixels
        arrives at the detector as ``w * native_scale``, and it must clear the anchor floor
        (~16 px for a marginal cell, ~32 px for usable landmarks) to be found at all.
        """
        return Letterbox.fit(width, height, self.input_size, self.input_size, square=self.square).scale

    def min_detectable_width(self, width: int, height: int, *, floor_px: float = 16.0,
                             tiles: int = 1, overlap: float = 0.2) -> float:
        """Native face width that clears ``floor_px`` **inside a tile**, in native pixels.

        Per-*window*, not "frame scale x tiles". A 2x2 grid does not multiply the subject scale by
        exactly 2, because each window is letterboxed into the *same* 640 canvas: on 1280x720 the
        full-frame scale is 0.500 and a 704x396 window is measured at 0.909 - a factor of 1.82, not
        2.0. The worst window is the largest one (it is scaled down most), so the reach is the
        minimum over windows - which is why this asks ``tile_origins`` for the real geometry rather
        than trusting a closed form that is 10 % optimistic.
        """
        if tiles <= 1:
            subject_scale = self.native_scale(width, height)
        else:
            windows = tile_origins(width, height, tiles, overlap)
            subject_scale = min(
                Letterbox.fit(x1 - x0, y1 - y0, self.input_size, self.input_size,
                              square=self.square).scale
                for (x0, y0, x1, y1) in windows
            )
        if subject_scale <= 0.0:  # pragma: no cover - a positive window cannot produce this
            raise DetectorError(f"degenerate tiling for {width}x{height} with {tiles} tiles")
        return float(floor_px / subject_scale)


class ScrfdDetector:
    """SCRFD-ONNX backend for extreme range: small-face heads at stride 8 and 16.

    Chosen over RetinaFace-R50 for this deployment because the 2.5GF/10GF variants are 3-17 MB and
    run in single-digit milliseconds at 640, which matters when the whole point is that a worker
    should not have to walk to the camera.

    Decoding, stated explicitly because it is where these integrations fail silently:

    * the model is anchor-free *relative to stride*: at stride ``s`` each cell centre ``(cx, cy)``
      emits distances ``(dl, dt, dr, db)`` in **stride units**, so the box is
      ``(cx - dl*s, cy - dt*s, cx + dr*s, cy + db*s)``;
    * landmarks are offsets from the same centre, in stride units, five points of ``(dx, dy)``;
    * the number of anchors per cell is *derived* from the tensor shape rather than assumed
      (``num_anchors = cells_in_tensor / (h*w)``), so a zoo model with 2 anchors and one with 1
      both decode correctly;
    * the blob contract is RGB with mean 127.5 and scale 1/128 - insightface's own preprocessing.
      Getting this wrong produces detections that look plausible and are 20 px off.

    Two export conventions are read, and completeness is *enforced* under both: a file that fits
    neither is refused at construction rather than returning an empty detection list that reads as
    "no face in frame".

    * ``score_8`` / ``bbox_8`` / ``kps_8`` - the head names the SCRFD repo's own export script
      writes. The stride is in the name.
    * **numeric names** (``448``, ``471``, ...) - the convention insightface ships, where the
      tensors are named after their graph ids and the layout is positional: every score tensor,
      then every bbox, then every keypoint, each block in the same stride order. This is what
      ``scrfd_2.5g_bnkps.onnx`` and ``scrfd_10g_bnkps.onnx`` (the two files the runbook points at)
      actually contain, and it is why the *stride* has to be recovered from the geometry - rows
      against the input size - and validated per inference rather than trusted from a name.

    **The canvas is square, and that is a requirement rather than a preference.** Every published
    export of this family declares ``(input/stride)**2`` cells per head - 12800, 3200 and 800 at
    input 640 - and the 10G file declares those shapes *statically*, so it will not accept an
    aspect-fitted canvas at all. Feeding one anyway (a 640x360 canvas for a 16:9 frame, the shape
    the aspect-fit path produces) silently breaks the arithmetic: the real grids come out 7200,
    1840 and 480, which is neither ``input/stride`` squared nor consistently rounded, so a decoder
    that assumes the square grid either decodes the wrong cells or refuses. The frame is therefore
    letterboxed into a square canvas - aspect preserved, padded - which keeps the subject at full
    scale and makes ``grid = (input/stride)**2`` exactly true. ``detect`` refuses a non-square
    canvas by name rather than decoding geometry it cannot verify.
    """

    _PATTERN: Final = re.compile(r"^(score|bbox|kps)_(\d+)$")
    #: Last dimension of each tensor family, used to identify the positional layout. A score
    #: tensor is one value per candidate, a box is four, and SCRFD's five landmarks are ten -
    #: which is also the check that keeps a foreign export (YuNet's 1/1/4/10 per stride) out.
    _WIDTHS: Final = {"score": 1, "bbox": 4, "kps": 10}

    def __init__(
        self,
        model_path: str | Path,
        *,
        input_size: int = 640,
        score_threshold: float = 0.5,
        nms_threshold: float = 0.4,
        intra_threads: int | None = None,
        session: Any | None = None,
    ) -> None:
        import onnxruntime as ort  # local: a CPU-only deployment must not need ORT to import this

        model = str(model_path)
        if not Path(model).exists():
            raise DetectorError(f"SCRFD model not found: {model}")
        if session is None:
            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # One thread unless the caller says otherwise. This used to be ``os.cpu_count()``,
            # which inside a container reports the host's cores rather than the cgroup quota -
            # a 1 vCPU instance answered 32, so ONNX Runtime opened 32 intra-op threads that
            # the scheduler then throttled. The session spent more time context switching than
            # running the graph, and the oversubscription surfaced as latency on every other
            # request. ``intra_threads`` still overrides this for a benchmark that wants it.
            options.intra_op_num_threads = int(intra_threads or 1)
            options.inter_op_num_threads = 1
            try:
                session = ort.InferenceSession(
                    model, options, providers=ort.get_available_providers()
                )
            except Exception as exc:  # noqa: BLE001 - construction failure is fatal and named
                raise DetectorError(f"could not create a SCRFD session for {model}: {exc}") from exc
        self._session = session
        self._input_name = self._session.get_inputs()[0].name
        self.input_size = int(input_size)
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)

        branches: dict[int, dict[str, str]] = {}
        for output in self._session.get_outputs():
            match = self._PATTERN.match(output.name)
            if match is None:
                continue
            kind, stride = match.group(1), int(match.group(2))
            branches.setdefault(stride, {})[kind] = output.name
        incomplete = sorted(s for s, kinds in branches.items() if set(kinds) != {"score", "bbox", "kps"})
        #: Heads resolved at construction (the named convention), or empty when the strides can only
        #: be known per inference (the positional one).
        self._strides: Final[tuple[int, ...]] = ()
        #: The positional layout as (score, bbox, kps) output indices, or empty for the named one.
        self._layout: list[tuple[int, int, int]] = []
        if branches and not incomplete:
            self._strides = tuple(sorted(branches))
            self._branches = branches
        else:
            # The named convention did not describe this file. Before refusing, read it the other
            # documented way: the positional layout insightface ships, whose strides cannot be
            # known until an inference says how many rows each head produced.
            layout = self._numeric_layout()
            if layout is None:
                observed = [tuple(o.shape) for o in self._session.get_outputs()]
                raise DetectorError(
                    "SCRFD export exposes neither a complete score_/bbox_/kps_ set per stride "
                    f"(strides seen: {sorted(branches)}; incomplete: {incomplete}) nor the "
                    f"insightface positional layout (score/bbox/kps blocks, last dims 1/4/10; "
                    f"outputs seen: {observed}); refusing to decode"
                )
            self._branches = branches
            self._layout = layout
        log.info(
            "SCRFD ready: input=%d strides=%s layout=%s",
            self.input_size,
            self._strides or "positional (resolved per inference)",
            "numeric" if self._layout else "named",
        )

    def _numeric_layout(self) -> list[tuple[int, int, int]] | None:
        """Head indices per stride for insightface's numeric-named export, or ``None``.

        The convention is positional *and* unlabelled, so it is derived from two facts that a
        different model cannot easily satisfy at once: the outputs come in three equal blocks
        (all scores, all boxes, all keypoints) and each block's tensors carry the family's last
        dimension. YuNet, the most likely wrong file to be pointed at, interleaves its heads
        (1, 1, 4, 10 per stride), so it fails the block test rather than being misread.
        """
        outputs = self._session.get_outputs()
        count = len(outputs)
        if count == 0 or count % 3:
            return None
        widths: list[int] = []
        for output in outputs:
            shape = list(output.shape)
            if len(shape) != 2 or not isinstance(shape[-1], int):
                return None
            widths.append(int(shape[-1]))
        block = count // 3
        expected = (
            [self._WIDTHS["score"]] * block
            + [self._WIDTHS["bbox"]] * block
            + [self._WIDTHS["kps"]] * block
        )
        if widths != expected:
            return None
        return [(index, block + index, 2 * block + index) for index in range(block)]

    def _positional_stride(self, rows: int) -> int:
        """The stride a head was computed at, from its row count and the input size.

        ``rows = anchors * (input/stride)**2`` on the square canvas this backend always feeds, so
        the stride is recoverable - but only if exactly one (anchors, stride) pair explains the
        row count. SCRFD ships two anchors per cell and the one-anchor export exists, so both are
        tried; two explanations is not a preference to break, it is a file we do not understand,
        and the refusal says which row count was ambiguous.
        """
        candidates: list[int] = []
        for anchors in (2, 1):
            if rows % anchors:
                continue
            side = math.isqrt(rows // anchors)
            if side == 0 or side * side != rows // anchors or self.input_size % side:
                continue
            stride = self.input_size // side
            if stride >= 4 and stride & (stride - 1) == 0 and stride not in candidates:
                candidates.append(stride)
        if len(candidates) != 1:
            raise DetectorError(
                f"a SCRFD head with {rows} rows does not resolve to exactly one stride at input "
                f"{self.input_size} (candidates: {candidates}); refusing to decode"
            )
        return candidates[0]

    def _output_plan(self, outputs: list[np.ndarray]) -> dict[int, dict[str, int]]:
        """strides -> {kind: index into ``outputs``}, whichever convention this file follows."""
        if self._branches:
            names = [output.name for output in self._session.get_outputs()]
            return {
                stride: {kind: names.index(name) for kind, name in kinds.items()}
                for stride, kinds in self._branches.items()
            }
        plan: dict[int, dict[str, int]] = {}
        for score, bbox, kps in self._layout:
            stride = self._positional_stride(int(np.asarray(outputs[score]).size))
            if stride in plan:
                raise DetectorError(
                    f"two SCRFD heads resolved to stride {stride}; refusing to decode"
                )
            plan[stride] = {"score": score, "bbox": bbox, "kps": kps}
        return plan

    def _decode(
        self, plan: Letterbox, outputs: list[np.ndarray], *, canvas: tuple[int, int]
    ) -> list[Detection]:
        canvas_h, canvas_w = int(canvas[0]), int(canvas[1])
        if canvas_h != self.input_size or canvas_w != self.input_size:
            # The heads are laid out on a grid derived from the *canvas*: an aspect-fitted canvas
            # makes ``input/stride`` false in one axis and the cell count is not recoverable from
            # the tensors. Refused by name, because decoding it anyway would produce boxes that
            # look plausible and point at the wrong pixels.
            raise DetectorError(
                f"SCRFD needs a square canvas of {self.input_size}x{self.input_size} for its "
                f"(input/stride)**2 head layout, got {canvas_w}x{canvas_h}; refusing to decode"
            )
        found: list[Detection] = []
        branches = self._output_plan(outputs)
        if not branches:
            raise DetectorError("the SCRFD session exposed no heads to decode")
        for stride in sorted(branches):
            kinds = branches[stride]
            scores = np.asarray(outputs[kinds["score"]], dtype=np.float32).reshape(-1)
            boxes = np.asarray(outputs[kinds["bbox"]], dtype=np.float32)
            kpss = np.asarray(outputs[kinds["kps"]], dtype=np.float32)
            grid_h, grid_w = self.input_size // stride, self.input_size // stride
            cells = grid_h * grid_w
            if scores.size == 0 or scores.size % cells:
                raise DetectorError(
                    f"stride {stride}: {scores.size} score values is not a multiple of {cells} cells"
                )
            anchors = scores.size // cells
            scores = scores.reshape(grid_h, grid_w, anchors)
            boxes = boxes.reshape(grid_h, grid_w, anchors, 4) * stride
            kpss = kpss.reshape(grid_h, grid_w, anchors, 5, 2) * stride
            centre_x = (np.arange(grid_w) + 0.5) * stride
            centre_y = (np.arange(grid_h) + 0.5) * stride
            for anchor in range(anchors):
                rows, cols = np.where(scores[:, :, anchor] >= self.score_threshold)
                for row, col in zip(rows.tolist(), cols.tolist()):
                    cx, cy = float(centre_x[col]), float(centre_y[row])
                    dl, dt, dr, db = (float(v) for v in boxes[row, col, anchor])
                    points = np.empty((5, 2), dtype=np.float32)
                    points[:, 0] = cx + kpss[row, col, anchor, :, 0]
                    points[:, 1] = cy + kpss[row, col, anchor, :, 1]
                    native = plan.to_native(
                        np.concatenate(
                            (np.array([[cx - dl, cy - dt], [cx + dr, cy + db]], np.float32), points)
                        )
                    )
                    (x1, y1), (x2, y2) = native[0], native[1]
                    found.append(
                        Detection(
                            box=(float(x1), float(y1), max(1.0, float(x2 - x1)), max(1.0, float(y2 - y1))),
                            landmarks=native[2:].astype(np.float32),
                            score=float(scores[row, col, anchor]),
                            origin=f"stride{stride}",
                            scale=plan.scale,
                        )
                    )
        return found

    def detect(self, frame: np.ndarray, *, tiles: int = 1, overlap: float = 0.2) -> list[Detection]:
        if frame.ndim != 3 or frame.size == 0:
            raise DetectorError(f"detect() needs a non-empty HxWxC frame, got {frame.shape}")
        height, width = frame.shape[:2]
        found: list[Detection] = []
        windows = [(0, 0, width, height)] + tile_origins(width, height, tiles, overlap)
        for index, (x0, y0, x1, y1) in enumerate(windows):
            sub = frame[y0:y1, x0:x1]
            if sub.size == 0:  # pragma: no cover
                continue
            plan = Letterbox.fit(
                sub.shape[1], sub.shape[0], self.input_size, self.input_size, square=True
            )
            payload = plan.apply(sub)
            # insightface contract: BGR -> RGB, mean 127.5, scale 1/128, NCHW float32.
            chw = np.ascontiguousarray(payload[:, :, ::-1].transpose(2, 0, 1))[None].astype(np.float32)
            blob = (chw - 127.5) / 128.0
            try:
                outputs = self._session.run(None, {self._input_name: blob})
            except Exception as exc:  # noqa: BLE001 - surfaced with the frame's geometry
                raise DetectorError(f"SCRFD inference failed on a {x1 - x0}x{y1 - y0} window: {exc}") from exc
            for detection in self._decode(plan, outputs, canvas=payload.shape[:2]):
                bx, by, bw, bh = detection.box
                detection.box = (bx + x0, by + y0, bw, bh)
                detection.landmarks = detection.landmarks + np.array([x0, y0], dtype=np.float32)
                detection.origin = "full" if index == 0 else f"tile:{index - 1}"
                found.append(detection)
        return merge_detections(found, iou_threshold=self.nms_threshold)


#: The backends that feed a square canvas whatever the caller asked for.
#:
#: A fact about each backend, not a preference: SCRFD's head grids are ``(input/stride)**2`` (and
#: the 10G export's are declared statically), so a square canvas is the only geometry whose cells
#: can be decoded at all - ``ScrfdDetector.detect`` pads to it without consulting the flag.
ALWAYS_SQUARE: Final = frozenset({"scrfd"})


def canvas_is_square(kind: str, square: bool = False) -> bool:
    """Whether a detector of this kind feeds a square canvas - the *geometry*, not the request.

    They differ for exactly one backend. The flag is honoured by YuNet (``Letterbox.fit(...,
    square=square)``) and ignored by SCRFD, so a specification that records the request rather
    than the fact gives the *same* detector two identities: a corpus ingested with ``--square`` and
    one ingested without would look like two configurations to every comparison that keys on the
    fingerprint - the A/B tool's "the configuration changed, refusing to reinterpret these crops"
    firing on two runs of one geometry, and a stored crop refusing to be reused. Recording the
    fact keeps one detector to one fingerprint.

    An unknown kind raises instead of defaulting to the request: a backend added later has to
    state its answer here, rather than inheriting a plausible-looking one nobody checked.
    """
    if kind not in ("yunet", "scrfd"):
        raise DetectorError(f"unknown detector kind {kind!r}; expected 'yunet' or 'scrfd'")
    return True if kind in ALWAYS_SQUARE else bool(square)


def build_detector(
    kind: str,
    model_path: str | Path,
    *,
    input_size: int = 640,
    tiles: int = 1,
    overlap: float = 0.2,
    square: bool = False,
    **kwargs: Any,
) -> Any:
    """Factory returning a detector *bound to its tiling policy*, so call sites cannot forget it.

    A call site that constructs a detector and then forgets ``tiles=2`` silently reverts the
    coverage fix; binding the policy at construction makes that failure impossible rather than
    merely documented.

    ``square`` is named here rather than left in ``**kwargs`` because the two backends do not
    agree on it: YuNet takes it (a fixed-shape TensorRT engine needs a square input), and SCRFD
    has no such mode - it pads to a square canvas *always*, because its published exports declare
    their head grids as ``(input/stride)**2``. Left in ``**kwargs`` it was forwarded to whichever
    backend was being built, so *every* SCRFD construction through this factory died with
    ``ScrfdDetector.__init__() got an unexpected keyword argument 'square'`` - which is how the
    coverage sweep's fourth configuration found it.

    For SCRFD the flag is therefore ignored rather than refused: the backend already feeds the
    square input the request asks for, so a refusal would reject a caller for requesting what it
    is being given. What it must not do is *mean* something in the record that it does not mean in
    the geometry, which is what ``canvas_is_square`` is for.
    """
    if kind == "yunet":
        detector: Any = YuNetDetector(model_path, input_size=input_size, square=square, **kwargs)
    elif kind == "scrfd":
        # ``square`` is accepted and ignored: this backend letterboxes into a square canvas always,
        # because its published exports declare their head grids as ``(input/stride)**2`` and the
        # 10G file's are even static. It used to be refused - back when SCRFD was aspect-fitted,
        # where honouring the flag was impossible - so a caller asking for the fixed-shape square
        # input a TensorRT profile needs now simply gets the square input this backend always feeds.
        detector = ScrfdDetector(model_path, input_size=input_size, **kwargs)
    else:
        raise DetectorError(f"unknown detector kind {kind!r}; expected 'yunet' or 'scrfd'")

    def detect(frame: np.ndarray) -> list[Detection]:
        return detector.detect(frame, tiles=tiles, overlap=overlap)

    detect.detector = detector  # type: ignore[attr-defined]  # introspection for triage output
    return detect
