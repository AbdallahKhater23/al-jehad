"""SCRFD's head layouts: two export conventions, one validated geometry, and a square canvas.

WHAT IS PINNED HERE
-------------------
The fourth coverage-sweep configuration compares SCRFD against YuNet *on this deployment's
frames*, which means a real SCRFD file has to load and decode correctly. Two things stood between
the checkout and that measurement, and both are silent-failure shaped, so both are pinned:

* **the export convention.** ``score_8``/``bbox_8``/``kps_8`` is what the SCRFD repo's export
  script writes, and it is the only convention this decoder used to read. The files the runbook
  points at - ``scrfd_2.5g_bnkps.onnx``, ``scrfd_10g_bnkps.onnx`` - are insightface's, whose
  tensors are named after their graph ids and whose layout is *positional*: every score tensor,
  then every bbox, then every keypoint, each block in the same stride order. A decoder that reads
  names only refuses them; a decoder that guessed would pair the right tensors with the wrong
  strides, producing plausible boxes that point at the wrong pixels. So the layout is derived from
  facts a foreign model cannot easily satisfy at once - three equal blocks whose last dimensions
  are 1, 4 and 10 - and each head's stride is recovered from its row count and *validated*,
  refusing when no single stride explains it.
* **the canvas.** Every published export of this family lays its heads out as ``(input/stride)**2``
  cells - 12800, 3200, 800 at 640 - and the 10G file declares those shapes statically. An
  aspect-fitted canvas (640x360 for a 16:9 frame, which is what the ordinary path produces) makes
  that arithmetic false: the real grids come out 7200, 1840 and 480, and no single rounding rule
  explains them. SCRFD therefore letterboxes into a *square* canvas, and the decoder refuses any
  other shape by name rather than decoding cells it cannot verify.

The planted-cell test is the one that catches a wrong decode rather than a crash: a single
candidate is placed at a known grid cell and the decoded box is checked against the native pixel
the geometry says that cell is - under both conventions, so a divergence between them cannot hide.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from detector_640 import DetectorError, Letterbox, ScrfdDetector

MODELS = Path(__file__).resolve().parent.parent / "models"
SCRFD_FILES = (MODELS / "scrfd_2.5g_bnkps.onnx", MODELS / "scrfd_10g_bnkps.onnx")
STRIDES = (8, 16, 32)

#: Last dimension of each tensor family, and the order the imports declare them in.
KINDS = (("score", 1), ("bbox", 4), ("kps", 10))


# ---------------------------------------------------------------------------
# a session stub: the layout is under test, not ONNX
# ---------------------------------------------------------------------------
class _FakeOutput:
    def __init__(self, name: str, shape: list) -> None:
        self.name = name
        self.shape = shape


class _FakeSession:
    """Just enough of an ORT session for the constructor and the decoder."""

    def __init__(self, outputs: list[_FakeOutput]) -> None:
        self._outputs = outputs

    def get_inputs(self) -> list[_FakeOutput]:
        return [_FakeOutput("input.1", [1, 3, "?", "?"])]

    def get_outputs(self) -> list[_FakeOutput]:
        return self._outputs


def _numeric_session(static_rows: tuple[int, ...] | None = None) -> _FakeSession:
    """The insightface convention: graph-id names, each output as wide as its family."""
    names = ("446", "466", "486", "449", "469", "489", "452", "472", "492")
    shape_of = [width for _, width in KINDS for _ in STRIDES]
    return _FakeSession([
        _FakeOutput(name, [None if static_rows is None else static_rows[index], width])
        for index, (name, width) in enumerate(zip(names, shape_of))
    ])


def _named_session() -> _FakeSession:
    """The SCRFD-repo convention: the head names carry the stride."""
    return _FakeSession([
        _FakeOutput(f"{kind}_{stride}", ["?", width])
        for kind, width in KINDS
        for stride in STRIDES
    ])


def _detector(session: _FakeSession, tmp_path: Path, **kwargs) -> ScrfdDetector:
    model = tmp_path / "scrfd.onnx"
    model.write_bytes(b"stubbed: the session is handed in, nothing loads it")
    return ScrfdDetector(str(model), input_size=640, session=session, **kwargs)


def _heads(
    *,
    spike: tuple[int, int, int, int, float] | None = None,
    rows: int | None = None,
) -> list[np.ndarray]:
    """Nine head arrays for a square 640 canvas, in the order the real exports declare them.

    ``spike`` is ``(stride, row, col, anchor, score)``: that candidate is the only one above the
    threshold, and its box is ``(dl, dt, dr, db) = (1, 2, 3, 4)`` in stride units while its
    landmark offsets are zero - both chosen so the expected native geometry is arithmetic the test
    can state independently. ``rows`` overrides every head's row count, for the refusal test.
    """
    per_stride: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for stride in STRIDES:
        grid = 640 // stride
        count = rows if rows is not None else grid * grid * 2
        scores = np.full((count, 1), 0.01, dtype=np.float32)
        boxes = np.zeros((count, 4), dtype=np.float32)
        kps = np.zeros((count, 10), dtype=np.float32)
        if spike is not None and spike[0] == stride:
            _, row, col, anchor, score = spike
            index = (row * grid + col) * 2 + anchor
            scores[index, 0] = score
            boxes[index, :] = (1.0, 2.0, 3.0, 4.0)
        per_stride[stride] = (scores, boxes, kps)
    return [per_stride[stride][family] for family in range(3) for stride in STRIDES]


# ---------------------------------------------------------------------------
# the two conventions
# ---------------------------------------------------------------------------
def test_the_positional_layout_is_read_from_the_blocks_not_the_names(tmp_path):
    """insightface's numeric-named export: score/bbox/kps blocks, widths 1/4/10."""
    detector = _detector(_numeric_session(), tmp_path)
    assert detector._strides == (), "an unlabelled export has no construction-time strides"
    assert detector._layout == [(0, 3, 6), (1, 4, 7), (2, 5, 8)], detector._layout


def test_the_named_layout_still_wins_so_the_original_convention_is_untouched(tmp_path):
    """``score_8`` and friends keep working, and take the named path rather than the positional one."""
    detector = _detector(_named_session(), tmp_path)
    assert detector._strides == STRIDES, detector._strides
    assert detector._layout == [], detector._layout


def test_a_foreign_model_is_refused_by_name_rather_than_misread(tmp_path):
    """YuNet's heads interleave (1, 1, 4, 10 per stride), so they cannot pass the block test.

    This is the mistake the fourth configuration invites - pointing ``--scrfd-model`` at the YuNet
    file already on disk - and the refusal names both conventions and the shapes actually seen,
    because the operator's next step is replacing a file.
    """
    interleaved = [
        _FakeOutput(f"{kind}_{stride}", ["?", width])
        for stride in STRIDES
        for kind, width in (("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10))
    ]
    with pytest.raises(DetectorError) as excinfo:
        _detector(_FakeSession(interleaved), tmp_path)
    message = str(excinfo.value)
    assert "score_/bbox_/kps_" in message, message
    assert "positional" in message, message
    assert "refusing to decode" in message, message


def test_a_head_that_resolves_to_no_stride_is_refused(tmp_path):
    """A row count no (anchors, stride) pair explains is a file this decoder does not understand."""
    detector = _detector(_numeric_session(), tmp_path)
    with pytest.raises(DetectorError) as excinfo:
        detector._output_plan(_heads(rows=1000))
    message = str(excinfo.value)
    assert "1000 rows" in message, message
    assert "refusing to decode" in message, message


# ---------------------------------------------------------------------------
# the geometry: a planted cell decodes to the native pixel the arithmetic says
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("layout", ["positional", "named"])
def test_a_planted_cell_decodes_to_the_native_pixel_the_geometry_says(tmp_path, layout):
    """The decode check a wrong stride cannot pass: cell (40, 30) of the stride-8 head.

    A stride-8 cell's centre is ``(col + 0.5) * stride`` in canvas pixels, so row 40, column 30
    sits at canvas (244, 324). The frame is 1280x720 letterboxed into a square 640 canvas at scale
    0.5 with 140 rows of padding, so that centre is native (488, 368); the planted box is 8, 16, 24
    and 32 canvas pixels on its four sides, i.e. 16, 32, 48 and 64 native ones. A decoder that read
    the wrong stride - or used the canvas width for the row count - lands somewhere else entirely.
    """
    stride, row, col = 8, 40, 30
    session = _numeric_session() if layout == "positional" else _named_session()
    detector = _detector(session, tmp_path, score_threshold=0.5)

    plan = Letterbox.fit(1280, 720, 640, 640, square=True)
    assert (plan.out_w, plan.out_h) == (640, 640), "the backend always feeds a square canvas"

    found = detector._decode(plan, _heads(spike=(stride, row, col, 0, 0.9)), canvas=(640, 640))
    assert len(found) == 1, found
    detection = found[0]

    def native(canvas_x: float, canvas_y: float) -> tuple[float, float]:
        return (canvas_x - plan.off_x) / plan.scale, (canvas_y - plan.off_y) / plan.scale

    centre_x, centre_y = (col + 0.5) * stride, (row + 0.5) * stride
    x1, y1 = native(centre_x - 1.0 * stride, centre_y - 2.0 * stride)
    x2, y2 = native(centre_x + 3.0 * stride, centre_y + 4.0 * stride)
    assert detection.box[0] == pytest.approx(x1, abs=0.01), detection.box
    assert detection.box[1] == pytest.approx(y1, abs=0.01), detection.box
    assert detection.box[2] == pytest.approx(x2 - x1, abs=0.01), detection.box
    assert detection.box[3] == pytest.approx(y2 - y1, abs=0.01), detection.box
    assert detection.score == pytest.approx(0.9, abs=1e-6), detection.score
    assert detection.origin == f"stride{stride}", detection.origin
    # The five landmarks are the cell centre itself: zero offsets in stride units.
    assert detection.landmarks.shape == (5, 2)
    assert detection.landmarks[0][0] == pytest.approx(centre_x / plan.scale, abs=0.01)
    assert detection.landmarks[0][1] == pytest.approx((centre_y - plan.off_y) / plan.scale, abs=0.01)


def test_a_non_square_canvas_is_refused_rather_than_decoded(tmp_path):
    """The aspect-fitted canvas this backend used to feed: 7200-row heads whose cells are unrecoverable.

    Refusing is the point. Decoding it anyway would place plausible-looking boxes at the wrong
    pixels - the failure mode that hides a geometry bug behind a working-looking detector.
    """
    detector = _detector(_numeric_session(), tmp_path)
    plan = Letterbox.fit(1280, 720, 640, 640)  # aspect-fit: a 640x360 canvas, no padding
    with pytest.raises(DetectorError) as excinfo:
        detector._decode(plan, _heads(rows=7200), canvas=(360, 640))
    message = str(excinfo.value)
    assert "square canvas" in message, message
    assert "640x360" in message, message
    assert "refusing to decode" in message, message


# ---------------------------------------------------------------------------
# the real files, when they are on disk
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", SCRFD_FILES, ids=[path.name for path in SCRFD_FILES])
def test_a_real_scrfd_export_parses_its_heads_and_runs_an_inference(model):
    """The files the runbook points at, through a real ONNX session - skipped when absent.

    Nothing about the download is asserted (a checkout may legitimately not carry the file); what
    is asserted is that a file which *is* there parses into strides 8, 16 and 32 and survives an
    inference end to end. A flat frame is enough: the point is that the session loads, the heads
    pair up, the geometry validates, and an empty result comes back rather than a refusal.
    """
    if not model.exists():
        pytest.skip(f"{model.name} is not in the checkout")
    detector = ScrfdDetector(str(model), input_size=640, score_threshold=0.5)
    assert detector._layout or detector._strides, "one of the two conventions must have parsed"

    flat = np.full((720, 1280, 3), 130, dtype=np.uint8)
    assert detector.detect(flat) == [], "a flat frame carries no face under either file"
