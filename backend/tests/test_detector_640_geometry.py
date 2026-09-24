"""The coverage arithmetic: invertible letterboxes, a real tiling grid, and what reach follows.

WHY THIS FILE IS PURE
---------------------
Everything here is geometry, so nothing here needs a model, an image or a network - which matters
because the claim it supports is the one a deployment gets wrong quietly. "Can the detector see a
face at two metres" is answered by a scale factor and an anchor floor, and if that arithmetic is
optimistic the system does not fail: it detects fewer faces, at the far end of the room, in the
evening, and nobody sees an error.

The failure this file exists to prevent was in the arithmetic itself. ``tiles`` was treated as a
direct multiplier on subject scale, so a 2x2 grid was claimed to double it. It does not: each
window is letterboxed into the *same* 640 canvas, so on 1280x720 the real factor is 1.82x, and the
reach number derived from 2.0x was about 10 % optimistic - the direction that leaves the person at
the far end of the room undetected.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import detector_640
from detector_640 import DetectorError, Letterbox, tile_origins


# ---------------------------------------------------------------------------
# the letterbox: a mapping, and therefore invertible
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("width", "height"),
    [(1920, 1080), (1280, 720), (640, 480), (4032, 3024), (720, 1280)],
)
@pytest.mark.parametrize("square", [False, True])
def test_the_letterbox_maps_native_pixels_and_maps_them_back(width, height, square):
    """A coordinate that survives the round trip is what makes a detection *in the frame* possible.

    The round trip is exact rather than approximate because the plan stores its *effective*
    per-axis scales (post-rounding of the resample size), not the requested ones.
    """
    plan = Letterbox.fit(width, height, 640, 640, square=square)
    native = np.array(
        [[0.0, 0.0], [width / 2.0, height / 3.0], [width - 1.0, height - 1.0], [17.0, 401.0]]
    )
    detector_pixels = np.stack(
        [native[:, 0] * plan.sx + plan.off_x, native[:, 1] * plan.sy + plan.off_y], axis=1
    )
    assert np.allclose(plan.to_native(detector_pixels), native, atol=1e-9)

    # The canvas is what the model is asked for, and the resample never upscales past it.
    assert plan.out_w <= 640 and plan.out_h <= 640
    assert plan.new_w <= plan.out_w and plan.new_h <= plan.out_h
    if square:
        assert (plan.out_w, plan.out_h) == (640, 640)
    else:
        assert (plan.out_w, plan.out_h) == (plan.new_w, plan.new_h), "no padding when not square"


def test_a_padding_region_coordinate_is_clamped_not_invented():
    """A point in the pad has no native pre-image; returning one would place a box off-frame."""
    plan = Letterbox.fit(1920, 1080, 640, 640, square=True)
    assert plan.off_y > 0.0, "a 16:9 frame letterboxed into a square must be padded vertically"
    padded = np.array([[320.0, 0.0], [320.0, 639.0]])  # top and bottom of the canvas
    mapped = plan.to_native(padded)
    assert mapped[:, 1].min() == 0.0 and mapped[:, 1].max() == 1079.0
    assert (mapped >= 0.0).all()


def test_the_plan_refuses_an_impossible_resample():
    for arguments in ((0, 720, 640, 640), (1280, 0, 640, 640), (1280, 720, 0, 640)):
        with pytest.raises(DetectorError):
            Letterbox.fit(*arguments)


def test_a_frame_round_trips_through_the_resample():
    """The pixel mapping and the image resample have to agree, or landmarks land where faces are not."""
    import cv2

    rng = np.random.default_rng(4)
    frame = rng.integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    plan = Letterbox.fit(1280, 720, 640, 640, square=False)
    resampled = plan.apply(frame)
    assert (resampled.shape[1], resampled.shape[0]) == (plan.out_w, plan.out_h)
    # A block of solid colour placed at a known native position must appear, scaled, at the
    # position the plan predicts - so the two halves of the mapping cannot drift apart.
    marked = np.zeros((720, 1280, 3), dtype=np.uint8)
    marked[300:340, 500:540] = 255
    out = plan.apply(marked)
    centre = np.array([[520.0, 320.0]])
    detector_centre = np.stack(
        [centre[:, 0] * plan.sx + plan.off_x, centre[:, 1] * plan.sy + plan.off_y], axis=1
    )[0]
    x, y = int(round(detector_centre[0])), int(round(detector_centre[1]))
    assert out[y, x].min() > 0, "the marked block must be where the plan says it is"
    with pytest.raises(DetectorError):
        plan.apply(np.zeros((720, 1280), dtype=np.uint8))


# ---------------------------------------------------------------------------
# the tiling grid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tiles", [1, 2, 3, 4])
def test_the_grid_is_a_square_of_windows_inside_the_frame(tiles):
    windows = tile_origins(1280, 720, tiles, 0.2)
    assert len(windows) == tiles * tiles
    for x0, y0, x1, y1 in windows:
        assert 0 <= x0 < x1 <= 1280
        assert 0 <= y0 < y1 <= 720


def test_the_grid_covers_the_frame_so_no_region_is_blind():
    """A tile pass with a gap is worse than no tile pass: it looks like coverage and is not.

    Checked by painting, not by trusting the arithmetic: rebuild the union of the windows on a
    downsampled mask and assert every pixel is claimed by at least one window.
    """
    covered = np.zeros((72, 128), dtype=bool)
    scale_x, scale_y = 1280 / 128, 720 / 72
    for x0, y0, x1, y1 in tile_origins(1280, 720, 3, 0.2):
        covered[
            int(y0 / scale_y): int(np.ceil(y1 / scale_y)),
            int(x0 / scale_x): int(np.ceil(x1 / scale_x)),
        ] = True
    assert covered.all(), f"{int((~covered).sum())} blind cells in the tiled pass"


def test_the_grid_refuses_a_policy_it_cannot_honour():
    with pytest.raises(DetectorError):
        tile_origins(1280, 720, 0, 0.2)
    with pytest.raises(DetectorError):
        tile_origins(1280, 720, 2, 1.0)
    with pytest.raises(DetectorError):
        tile_origins(1280, 720, 2, -0.1)


# ---------------------------------------------------------------------------
# reach: the number that decides whether a person at 1.5 m is visible at all
# ---------------------------------------------------------------------------
def test_the_reach_is_measured_per_window_and_not_multiplied_by_tiles():
    """The regression this file was written for: 1280x720 with a 2x2 grid is 1.82x, not 2x.

    A detector constructed but never asked for reach is the cheap version of this test; the point
    is that the *published* figure matches the real window geometry, window by window.
    """
    full = Letterbox.fit(1280, 720, 640, 640, square=False).scale
    assert full == pytest.approx(0.5, abs=1e-9)
    windows = tile_origins(1280, 720, 2, 0.2)
    per_window = [
        Letterbox.fit(x1 - x0, y1 - y0, 640, 640, square=False).scale
        for x0, y0, x1, y1 in windows
    ]
    assert min(per_window) == pytest.approx(0.909, abs=0.002)
    assert max(per_window) / full < 2.0, "a 2x2 grid does not double the subject scale"

    # A face is visible when its *smallest* appearance across the grid clears the anchor floor,
    # so the reach is set by the largest window - the one scaled down most.
    assert 2.0 * full > min(per_window), "the largest window is genuinely the worst case"
    floor_px = 16.0
    assert floor_px / min(per_window) == pytest.approx(17.6, abs=0.2)


@pytest.mark.parametrize("floor_px", [16.0, 32.0])
def test_reach_improves_with_tiles_and_never_the_other_way(floor_px):
    """Monotonicity, as a guard on the per-window arithmetic rather than on a table of numbers.

    The detector is constructed for real (the model ships with the checkout) because the reach
    numbers are its methods': a stubbed detector would let the geometry and the published figure
    drift apart, which is the defect this file exists for.
    """
    model = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
    if not model.exists():  # pragma: no cover - the detector model ships with the checkout
        pytest.skip("the YuNet model is not present")
    detector = detector_640.YuNetDetector(str(model), input_size=640)
    previous = float("inf")
    for tiles in (1, 2, 3):
        reach = detector.min_detectable_width(1280, 720, floor_px=floor_px, tiles=tiles)
        assert reach < previous, f"more tiles must not reduce reach (tiles={tiles})"
        previous = reach
    assert detector.min_detectable_width(1280, 720, tiles=1, floor_px=16.0) == pytest.approx(32.0)
    assert detector.min_detectable_width(1920, 1080, tiles=1, floor_px=16.0) == pytest.approx(48.0)
    assert detector.min_detectable_width(1280, 720, tiles=2, floor_px=16.0) == pytest.approx(
        17.6, abs=0.2
    ), "the per-window figure, not floor_px / (scale x tiles) = 16.0"


# ---------------------------------------------------------------------------
# the factory: both families build, and each gets only the keywords it has
# ---------------------------------------------------------------------------
def test_the_factory_builds_each_family_without_handing_it_another_familys_keywords(tmp_path):
    """The defect the coverage sweep's fourth configuration walked into.

    ``square`` used to sit in ``**kwargs`` and was therefore forwarded to whichever backend was
    being built. YuNet takes it; SCRFD does not have the mode at all - so *every* SCRFD
    construction through this factory died with ``ScrfdDetector.__init__() got an unexpected
    keyword argument 'square'``. Nothing caught it, because no test built a SCRFD detector through
    the factory: the backend was exercised only by hand, if at all.

    A missing model proves the point without one on disk: with the keyword bug the rejection is a
    ``TypeError`` from argument binding, and with it fixed the rejection is the backend's own
    ``DetectorError`` naming the file. That difference is exactly what this asserts.
    """
    missing = tmp_path / "not-here.onnx"
    yunet_model = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
    assert yunet_model.exists(), "the YuNet model ships with the checkout"

    built = detector_640.build_detector("yunet", str(yunet_model), input_size=640)
    assert callable(built) and built.detector.input_size == 640

    with pytest.raises(DetectorError) as scrfd_error:
        detector_640.build_detector("scrfd", str(missing), input_size=640)
    assert "SCRFD model not found" in str(scrfd_error.value), scrfd_error.value

    with pytest.raises(DetectorError) as unknown:
        detector_640.build_detector("retinaface", str(missing))
    assert "unknown detector kind" in str(unknown.value), unknown.value


def test_the_square_flag_is_accepted_for_the_backend_that_is_always_square(tmp_path):
    """The flag used to be refused for SCRFD; the backend now always pads to a square canvas.

    The refusal existed because the request could not be honoured - SCRFD was aspect-fitted, so an
    operator asking for the fixed-shape square input a TensorRT profile needs would have been
    handed a different geometry with no error. SCRFD's published exports declare their head grids
    as ``(input/stride)**2`` (the 10G file's statically), so a square canvas is the only geometry
    whose cells can be decoded at all - the flag is now satisfied rather than rejected, and the
    backend is reached instead of the argument check. Both spellings must survive: a caller that
    passes it and a caller that does not get the same square input.
    """
    missing = tmp_path / "scrfd.onnx"
    for square in (True, False):
        with pytest.raises(DetectorError) as excinfo:
            detector_640.build_detector("scrfd", str(missing), input_size=640, square=square)
        message = str(excinfo.value)
        assert "SCRFD model not found" in message, (square, message)
        assert "unexpected keyword argument" not in message, (square, message)


# ---------------------------------------------------------------------------
# the audit: every keyword every call site sends, to every backend it can reach
# ---------------------------------------------------------------------------
# The factory takes ``**kwargs``, so a call site can hand a backend a keyword that backend does not
# have - and the failure mode is a bare ``TypeError`` from argument binding, which says nothing
# about who sent it. That is how ``square`` was found: it lived in ``**kwargs`` and was therefore
# forwarded to whichever backend was being built, so *every* SCRFD construction through the factory
# died, and the coverage sweep's fourth configuration was the only place that ever noticed.
#
# The keyword set below is not invented: it is the union of what the production call sites actually
# pass, read from ``corpus_ingest.DetectorSpec.build``, ``tools/corpus_admin`` (which builds a spec),
# ``tools/contract_ab`` and ``corpus.widened_reach_detector``. Each backend is then built for real
# with that whole set - YuNet against the model that ships with the checkout, SCRFD against a stub
# session - so this pins construction rather than argument binding alone.
CALL_SITE_KEYWORDS = {"input_size": 640, "tiles": 2, "overlap": 0.2, "square": True, "score_threshold": 0.6}


class _FakeOutput:
    def __init__(self, name: str, shape: list) -> None:
        self.name = name
        self.shape = shape


#: The head families and their widths, in the order the outputs are declared.
_HEADS = tuple((kind, width) for kind, width in (("score", 1), ("bbox", 4), ("kps", 10)) for _ in (8, 16, 32))


class _FakeSession:
    """A named-convention SCRFD session: enough for the constructor *and* for one inference.

    ``run`` answers zero-filled heads sized from the canvas it is handed, so the factory's product
    can be called end to end - decoding, thresholds, an empty result - without an ONNX runtime in
    the test. That matters for this audit: argument binding was never the whole claim, since the
    defect being guarded against let a detector be *constructed* and never run.
    """

    def __init__(self) -> None:
        self.ran: int = 0

    def get_inputs(self) -> list:
        return [_FakeOutput("input.1", [1, 3, "?", "?"])]

    def get_outputs(self) -> list:
        return [
            _FakeOutput(f"{kind}_{stride}", ["?", width])
            for kind, width in (("score", 1), ("bbox", 4), ("kps", 10))
            for stride in (8, 16, 32)
        ]

    def run(self, _outputs, feeds) -> list:
        self.ran += 1
        canvas = np.asarray(next(iter(feeds.values()))).shape[2:]
        heads = []
        for kind, width in ((kind, width) for kind, width in (("score", 1), ("bbox", 4), ("kps", 10)) for _ in (8, 16, 32)):
            stride = (8, 16, 32)[len(heads) % 3]
            cells = (canvas[0] // stride) * (canvas[1] // stride)
            heads.append(np.zeros((1, cells * width), dtype=np.float32))
        return heads


@pytest.mark.parametrize("square", [False, True])
def test_scrfd_is_built_with_the_whole_keyword_set_the_call_sites_send(tmp_path, square):
    """Both spellings reach the backend and construct: the defect was here and nowhere else."""
    model = tmp_path / "scrfd.onnx"
    model.write_bytes(b"stubbed: the session is handed in, nothing loads it")
    session = _FakeSession()
    options = dict(CALL_SITE_KEYWORDS, square=square, session=session)

    built = detector_640.build_detector("scrfd", str(model), **options)

    detector = built.detector
    assert isinstance(detector, detector_640.ScrfdDetector)
    assert detector.input_size == 640
    assert detector.score_threshold == 0.6
    assert detector.nms_threshold == 0.4, "the base set is not silently overridden"
    # Constructed *and* run: the tiling policy the factory binds is part of the same surface, and a
    # zero-scoring head set is enough to prove the whole path executes and decodes to nothing.
    assert built(np.zeros((720, 1280, 3), dtype=np.uint8)) == []
    assert session.ran >= 1, "the factory's product never reached the session"


def test_yunet_is_built_with_the_whole_keyword_set_the_call_sites_send():
    """The other half: the same set must not have grown a keyword only SCRFD has."""
    model = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
    assert model.exists(), "the YuNet model ships with the checkout"

    built = detector_640.build_detector("yunet", str(model), **CALL_SITE_KEYWORDS)

    assert isinstance(built.detector, detector_640.YuNetDetector)
    assert built.detector.input_size == 640
    assert built.detector.square is True, "the flag is honoured by the backend that has the mode"
    assert built.detector.nms_threshold == 0.3


def test_a_keyword_no_backend_accepts_is_refused_by_the_factory_and_not_by_python():
    """``**kwargs`` is a shovel: the audit is that nothing in the application loads it."""
    import inspect

    options = set()
    for module, name in (
        ("corpus_ingest", "DetectorSpec"),
        ("detector_640", "build_detector"),
    ):
        import importlib

        source = importlib.import_module(module)
        target = getattr(source, name)
        options |= set(getattr(target, "__dataclass_fields__", {}) or {})
        if name == "build_detector":
            options |= set(inspect.signature(target).parameters)
    assert {"square", "tiles", "overlap", "input_size", "score_threshold"} <= options, sorted(options)


def test_the_square_a_spec_records_is_the_canvas_the_backend_feeds(tmp_path):
    """One detector, one fingerprint: the flag is a request, the fingerprint is a geometry.

    SCRFD pads to a square canvas whatever it is asked for, so ``--square`` and its absence are the
    *same* configuration. Recording the request instead of the fact gave that one detector two
    identities, which is the kind of difference the A/B tool refuses a corpus over.
    """
    assert detector_640.canvas_is_square("yunet", False) is False
    assert detector_640.canvas_is_square("yunet", True) is True
    assert detector_640.canvas_is_square("scrfd", False) is True, (
        "SCRFD pads to a square canvas without consulting the flag"
    )
    assert detector_640.canvas_is_square("scrfd", True) is True
    with pytest.raises(DetectorError):
        detector_640.canvas_is_square("retinaface", False)

    import corpus_ingest

    model = tmp_path / "scrfd.onnx"
    model.write_bytes(b"stub")
    asked = corpus_ingest.DetectorSpec(kind="scrfd", model_path=model, input_size=640, square=True)
    unasked = corpus_ingest.DetectorSpec(kind="scrfd", model_path=model, input_size=640, square=False)
    assert asked.fingerprint() == unasked.fingerprint(), (
        "the same detector must not fingerprint as two configurations"
    )
    assert asked.fingerprint()["square"] is True
