"""The coverage sweep: one corpus, four detector configurations, and what each discards.

WHAT IS PINNED HERE
-------------------
The sweep is the evidence behind (or against) the Phase 1 claim that 640 and tiling recover the
faces 320 loses - and behind the fourth configuration's claim that SCRFD is better *on these
frames*, which is why that configuration may be skipped but never silently dropped. Its own
failure modes, in the order they would bite:

* **a sweep that agreed with itself by construction** - the three YuNet configurations share one
  detector model, one gate and one set of frames; the test asserts the *differences* between
  them come from input size and tiling, by sweeping a corpus with a face only the bigger input
  can reach and checking the flip table reports exactly that frame;
* **a detector comparison nobody ran** - the SCRFD configuration is refused when its model file
  is absent, and when it is skipped on purpose the report says so; without both guards a
  three-line sweep would read as "SCRFD was measured and lost";
* **a family change that reads as a tuning step** - the flip table marks the row where the
  network changes (``crosses_detector``), so a tiling gain is never filed as evidence about
  SCRFD;
* **a miss with no reason** - "3 discarded" is not actionable; ``no_face`` versus
  ``quality:too_small`` versus ``quality:extreme_roll`` are three different fixes, so every
  miss carries its reason;
* **read-only as a property, not a promise** - the sweep runs against the corpus *directory*
  contract without ever writing a capture, sidecar or consent row, because its whole value is
  being runnable before capture is turned on;
* **the flip table is the answer** - the frame the 320 configuration loses and the 640
  configuration recovers is the deployment decision in one row;
* **a counting rule that hid the rule** - a frame with a bystander in it is a ``many_faces`` loss
  under the punch path's rule and a found face under the sweep's measurement rule
  (``--multi-subject``), and the two runs differ by a large margin, so the report has to say which
  rule produced its numbers; and the relaxed rule must relax the *count* only, never the gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import corpus
import harness
from corpus_ingest import DetectorSpec, Frame, FrameListSource, GateConfig

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# ---------------------------------------------------------------------------
# helpers: synthetic frames whose geometry the test controls
# ---------------------------------------------------------------------------
def _face_pixels(width: int, height: int, cx: float, cy: float, size: float) -> Image.Image:
    """A frame with one synthetic face: the landmarks drive the detector? No - they drive the
    *stub*. The real detector is not needed for the sweep's own logic; what is needed is that
    each configuration's *reach* is controlled. A stub detector that knows the input size of the
    spec that built it models YuNet's anchor-stride behaviour: at 320 a small face is below the
    stride, at 640 it is found."""
    return Image.new("RGB", (width, height), (110, 110, 110))


class _StrideStub:
    """A detector that finds a face only when the face is big enough *at its input size*.

    This models exactly the behaviour the sweep exists to measure - the anchor-stride starvation
    that makes a 60px face at 320 input a 15px subject the detector cannot see. The stub is built
    per spec and told that spec's input size, so its answer is a function of input size (and the
    tiling flag, which raises the window's effective scale) - which is what makes the sweep's
    arithmetic testable without an ONNX model.
    """

    def __init__(self, native_face_px: float, input_size: int, tiles: int = 1,
                 *, frame_w: int = 1280, frame_h: int = 720, stride_floor: float = 24.0,
                 kind: str = "yunet") -> None:
        self.native_face_px = native_face_px
        self.input_size = input_size
        self.tiles = tiles
        self.frame_w, self.frame_h = frame_w, frame_h
        #: The smallest subject each *family* can resolve at its input. YuNet's practical anchor
        #: stride is the 24px the sweep's arithmetic is about; SCRFD's stride-8 head is the reason
        #: the family comparison exists, and modelling it here is what lets a detector-family flip
        #: be tested without an ONNX file.
        self.stride_floor = stride_floor if kind != "scrfd" else 12.0
        self.kind = kind

    def __call__(self, bgr):
        import detector_640

        h, w = bgr.shape[:2]
        # The effective subject scale: full-frame letterbox into the input canvas - or, when
        # tiled, the worst *window* (the largest one is scaled down least, so use the max
        # window scale a face in a corner window would enjoy; the stub only needs the
        # monotonicity "tiling sees more").
        base = min(self.input_size / self.frame_w, self.input_size / self.frame_h)
        scale = base * (1.6 if self.tiles > 1 else 1.0)  # 1.6 ~ the measured 2x2 window gain
        face_px = self.native_face_px * scale
        if face_px < self.stride_floor:  # below this family's practical anchor stride
            return []
        box = (
            self.frame_w * 0.4,
            self.frame_h * 0.3,
            self.native_face_px,
            self.native_face_px,
        )
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        half = self.native_face_px / 2
        landmarks = np.array(
            [
                [cx - half * 0.3, cy - half * 0.2],
                [cx + half * 0.3, cy - half * 0.2],
                [cx, cy + half * 0.1],
                [cx - half * 0.25, cy + half * 0.4],
                [cx + half * 0.25, cy + half * 0.4],
            ],
            dtype=np.float32,
        )
        return [detector_640.Detection(box=box, landmarks=landmarks, score=0.9)]


class _SpecStubbed:
    """Build stubs per spec, so each configuration's behaviour follows its own input size."""

    def __init__(self) -> None:
        self.native_face_px = 48.0

    def build_for(self, spec: DetectorSpec):
        stub = _StrideStub(
            self.native_face_px, spec.input_size, spec.tiles, kind=spec.kind
        )
        return stub


@pytest.fixture
def stubbed_specs(monkeypatch):
    """Patch ``DetectorSpec.build`` so no ONNX model is needed and geometry is controlled."""
    holder = _SpecStubbed()

    def fake_build(self):
        return holder.build_for(self)

    monkeypatch.setattr(DetectorSpec, "build", fake_build)
    return holder


def _source(frame_name: str = "gate_0001.jpg", size: tuple[int, int] = (1280, 720)):
    return FrameListSource(items=[Frame(image=_face_pixels(*size, 640, 360, 48), name=frame_name)])


# ---------------------------------------------------------------------------
# the sweep's own logic
# ---------------------------------------------------------------------------
def test_a_face_the_320_configuration_loses_and_the_640_configuration_recovers(stubbed_specs):
    """The flip table is the Phase 1 claim in one row - asserted here on controlled geometry."""
    from coverage_sweep import default_specs, sweep

    # 48px native face on a 1280x720 frame: 0.5 full-frame scale -> 24px at the 320 input
    # (below the stride, lost) and 48px at 640 (found). Tiling cannot recover what 640 already
    # found, so the flip must sit exactly between the first two configurations.
    report = sweep(_source(), default_specs())
    assert report["frames_read"] == 1
    configs = report["configs"]
    assert configs["yunet@320"]["found"] == 0
    assert configs["yunet@320"]["discarded"] == 1
    assert configs["yunet@320"]["misses_by_reason"] == {"no_face": 1}
    assert configs["yunet@640"]["found"] == 1

    flip = report["flips"][0]
    assert flip["from"] == "yunet@320" and flip["to"] == "yunet@640"
    assert flip["recovered_count"] == 1
    assert flip["recovered"][0]["frame"] == "gate_0001.jpg"
    assert flip["net"] == 1


def test_every_miss_carries_a_reason_a_different_fix_would_need(stubbed_specs):
    """``no_face`` and gate discards are different problems with different fixes."""
    from coverage_sweep import default_specs, sweep_frame

    # A zero-face frame and a gate-discard face exercise the reason taxonomy end to end.
    gate = GateConfig(discard_face_px=200.0)  # any found face is discarded as too_small
    outcomes = sweep_frame(_face_pixels(1280, 720, 640, 360, 48), default_specs(), gate=gate)
    assert all(not o.found for o in outcomes)
    assert outcomes[0].miss_reason == "no_face"
    assert outcomes[1].miss_reason == "quality:too_small"
    assert outcomes[2].miss_reason == "quality:too_small"


def test_many_faces_is_its_own_reason_and_not_a_gate_discard(stubbed_specs, monkeypatch):
    """Two detections is an ambiguity, reported as its own reason rather than resolved."""
    from coverage_sweep import default_specs, sweep_frame

    class _TwoFaces:
        def __call__(self, bgr):
            stub = _StrideStub(48.0, 640)
            one = stub(bgr)[0]
            second = stub(bgr)[0]
            second.box = (second.box[0] + 400, second.box[1], second.box[2], second.box[3])
            return [one, second]

    monkeypatch.setattr(DetectorSpec, "build", lambda self: _TwoFaces())
    outcomes = sweep_frame(_face_pixels(1280, 720, 640, 360, 48), default_specs())
    assert all(o.miss_reason == "many_faces" for o in outcomes), [o.miss_reason for o in outcomes]


def test_the_sweep_stores_nothing(monkeypatch, tmp_path):
    """The measurement aid must not write: no capture, no sidecar, no consent row."""
    from coverage_sweep import default_specs, sweep

    calls: list[tuple] = []
    monkeypatch.setattr(
        corpus, "store", lambda *a, **kw: calls.append((a, kw)) or pytest.fail("sweep stored a capture")
    )

    class _Empty:
        def frames(self):
            return iter(())

        skipped: list = []

    specs = default_specs()
    def _boom(self):
        raise AssertionError("a sweep over an empty corpus must not build a detector at all")
    monkeypatch.setattr(DetectorSpec, "build", _boom)
    report = sweep(_Empty(), specs)
    assert report["frames_read"] == 0
    assert calls == []


def test_the_report_is_json_shaped_and_names_its_gate(stubbed_specs):
    """A report an operator pastes into an issue must carry the gate it was judged by."""
    from coverage_sweep import default_specs, sweep

    report = sweep(_source(), default_specs())
    gate = report["gate"]
    assert gate["min_face_px"] == 30.0 and gate["discard_face_px"] == 16.0
    for name, config in report["configs"].items():
        assert config["frames"] == report["frames_read"]
        assert config["found"] + config["discarded"] == config["frames"]
        assert "spec" in config and config["spec"]["input_size"] in (320, 640)


def test_flip_table_compares_configurations_in_the_order_given(stubbed_specs):
    """The progression reads 320 -> 640 -> 640+tiling -> SCRFD; the order is the report's grammar."""
    from coverage_sweep import default_specs, flip_table, spec_name

    specs = default_specs()
    results = {
        spec_name(s): {"outcomes": []} for s in specs
    }
    rows = flip_table(results, specs)
    assert [row["from"] for row in rows] == ["yunet@320", "yunet@640", "yunet@640+2x2tiling"]
    assert [row["to"] for row in rows] == [
        "yunet@640",
        "yunet@640+2x2tiling",
        "scrfd@640",
    ]


def test_only_the_scrfd_step_is_reported_as_a_detector_change(stubbed_specs):
    """A tiling gain and a family change are different findings and must not read alike.

    The first two steps hold the network fixed and vary input size and tiling - a tuning result
    the deployment can act on by editing a spec. The third swaps the network, and its numbers are
    the detector comparison this configuration exists to produce. Both used to be the same
    ``from -> to`` row shape, so a reader planning a migration would take a tiling gain for
    evidence about SCRFD.
    """
    from coverage_sweep import default_specs, flip_table, spec_name

    specs = default_specs()
    rows = flip_table({spec_name(s): {"outcomes": []} for s in specs}, specs)
    assert [row["crosses_detector"] for row in rows] == [False, False, True]
    assert rows[-1]["from_kind"] == "yunet" and rows[-1]["to_kind"] == "scrfd"


def test_default_specs_hold_the_input_size_once_and_change_the_network_once():
    """The four configurations, spelled out: three YuNet steps then one family comparison at 640.

    Pinned as a shape rather than a count so a fifth configuration cannot be added by editing
    the list alone: the *comparison* has to stay honest - one variable per step - and the family
    change has to stay at the input size where it means something (640, not the legacy 320).
    """
    from coverage_sweep import default_specs, spec_name

    specs = default_specs()
    assert [spec_name(s) for s in specs] == [
        "yunet@320",
        "yunet@640",
        "yunet@640+2x2tiling",
        "scrfd@640",
    ]
    assert [s.kind for s in specs] == ["yunet", "yunet", "yunet", "scrfd"], (
        "the family comparison comes last, after the three steps that hold it fixed"
    )
    scrfd = specs[-1]
    assert scrfd.input_size == 640 and scrfd.tiles == 1, (
        "SCRFD is compared at the coverage size, untiled: tiling it too would measure two "
        "changes at once and answer neither question"
    )


def test_the_scrfd_configuration_is_measured_not_copied(stubbed_specs):
    """The fourth line answers its own question: a face YuNet at 640 misses, SCRFD finds.

    This is the whole point of adding the configuration - the detector comparison measured on our
    frames rather than assumed from the papers in which SCRFD's stride-8 head wins. The stub
    models that head's finer reach, so the test pins the *reporting* half: the fourth
    configuration is run at all, its result is its own, and the step that produced it is marked
    as a family change rather than another YuNet tuning row.
    """
    from coverage_sweep import default_specs, sweep

    # 26px native on a 1280x720 frame. At the 0.5 full-frame scale that is 13px (below YuNet's
    # 24px floor at the 320 *and* the 640 input), and the tiling gain of ~1.6 raises it to 20.8px
    # - still below the floor, which is the point: this face is lost by every YuNet configuration
    # and found only by the family comparison. 13px is above SCRFD's 12px floor.
    stubbed_specs.native_face_px = 26.0
    report = sweep(_source(), default_specs())
    configs = report["configs"]
    assert configs["yunet@640"]["found"] == 0, configs["yunet@640"]
    assert configs["yunet@640+2x2tiling"]["found"] == 0, configs["yunet@640+2x2tiling"]
    assert configs["scrfd@640"]["found"] == 1, configs["scrfd@640"]

    last = report["flips"][-1]
    assert last["to"] == "scrfd@640" and last["crosses_detector"] is True, last
    assert [entry["frame"] for entry in last["recovered"]] == ["gate_0001.jpg"], last
    # ... and the detector family that produced it is named in the report, so "SCRFD found it"
    # is answerable months later without re-running the sweep.
    assert report["configs"]["scrfd@640"]["spec"]["kind"] == "scrfd"


class _BystanderStub:
    """One subject - plus the bystander a wider window sweeps in.

    The deployment's actual complaint, modelled: 2x2 tiling raises the subject scale, which is how
    it reaches a distant face, and it only does that by *widening the field of view* - which is
    also how the person walking past the doorway ends up in the frame. Both detections are real,
    both clear the gate, and the two counting rules disagree about whether the frame counts.
    """

    def __init__(self, native_face_px: float, input_size: int, tiles: int = 1, *,
                 kind: str = "yunet") -> None:
        self.tiles = tiles
        self.subject_stub = _StrideStub(native_face_px, input_size, tiles, kind=kind)
        # 0.7 of the subject: above the gate's discard line at both corpus sizes below, so it is a
        # *detected, usable* face the rule can refuse on the count alone. (It may be flagged small -
        # a flag is not a discard, and the gate is not what the two runs differ by.)
        self.bystander_px = native_face_px * 0.7

    def __call__(self, bgr):
        found = self.subject_stub(bgr)
        if not found or self.tiles <= 1:
            return found
        return found + [self._bystander()]

    def _bystander(self):
        import detector_640

        size = self.bystander_px
        box = (192.0, 156.0, size, size)
        cx, cy = box[0] + size / 2, box[1] + size / 2
        half = size / 2
        landmarks = np.array(
            [
                [cx - half * 0.3, cy - half * 0.2],
                [cx + half * 0.3, cy - half * 0.2],
                [cx, cy + half * 0.1],
                [cx - half * 0.25, cy + half * 0.4],
                [cx + half * 0.25, cy + half * 0.4],
            ],
            dtype=np.float32,
        )
        return detector_640.Detection(box=box, landmarks=landmarks, score=0.85)


def _bystander_specs(monkeypatch, native_face_px: float = 48.0) -> None:
    """The crowd-prone configuration only: 640 full-frame against 640 tiled."""
    monkeypatch.setattr(
        DetectorSpec,
        "build",
        lambda self: _BystanderStub(native_face_px, self.input_size, self.tiles),
    )


def test_a_bystander_frame_is_lost_under_the_punch_paths_rule_however_hard_tiling_worked(
    monkeypatch,
):
    """The defect ``--multi-subject`` exists to remove, pinned as the strict rule's own behaviour.

    Tiling finds the subject *and* the passer-by, and the one-subject rule throws both away: the
    report reads "tiling lost a frame that full-frame 640 held", which is the opposite of what
    happened. Kept as a test so the strict numbers are never mistaken for a defect in tiling.
    """
    from coverage_sweep import spec_name, sweep

    _bystander_specs(monkeypatch)
    specs = [DetectorSpec(input_size=640, tiles=1), DetectorSpec(input_size=640, tiles=2)]
    report = sweep(_source(), specs)
    configs = report["configs"]
    assert configs["yunet@640"]["found"] == 1
    assert configs["yunet@640+2x2tiling"]["found"] == 0
    assert configs["yunet@640+2x2tiling"]["misses_by_reason"] == {"many_faces": 1}
    # The rule saved nothing, by construction - and the report says so with a zero rather than by
    # omitting the number, so "no bystanders here" and "bystanders here, all refused" differ.
    assert configs[spec_name(specs[1])]["bystander_frames"] == 0
    assert configs[spec_name(specs[1])]["multi_face_frames"] == 1
    flip = report["flips"][0]
    assert flip["recovered_count"] == 0 and flip["lost"] == ["gate_0001.jpg"]


def test_multi_subject_measures_tilings_reach_instead_of_its_bystanders(monkeypatch):
    """The frame tiling alone can reach, counted for reach: the two runs disagree, and both are right.

    This is what the mode is for. A face too small for 640 full-frame sits inside the tiled window,
    where the subject scale is high enough to resolve it - and where the passer-by is now in shot
    too. The strict rule books that as ``many_faces``, so the one configuration that could reach the
    face is the one configuration that appears to lose it; the relaxed rule counts what the
    detector could see. The gate is identical in both runs, and the subject is still the subject:
    the credited face is the largest detection that clears the gate, never whichever head the
    detector emitted first.
    """
    from coverage_sweep import SUBJECT_ANY, sweep

    # 40px native on a 1280x720 frame: 20px at the 640 full-frame scale (below YuNet's 24px floor,
    # lost) and ~32px inside a tiled window (found) - alongside a 28px bystander.
    _bystander_specs(monkeypatch, native_face_px=40.0)
    specs = [DetectorSpec(input_size=640, tiles=1), DetectorSpec(input_size=640, tiles=2)]

    strict = sweep(_source(), specs)
    assert strict["configs"]["yunet@640"]["found"] == 0
    assert strict["configs"]["yunet@640+2x2tiling"]["misses_by_reason"] == {"many_faces": 1}
    assert strict["flips"][0]["recovered_count"] == 0, (
        "the strict rule is what the mode is measured against: it books this reach as a loss"
    )

    report = sweep(_source(), specs, subject=SUBJECT_ANY)
    tiled = report["configs"]["yunet@640+2x2tiling"]
    assert tiled["found"] == 1
    assert tiled["bystander_frames"] == 1
    assert tiled["multi_face_frames"] == 1
    outcome = tiled["outcomes"][0]
    assert outcome["faces"] == 2, outcome
    assert outcome["face_px"] == 40.0, outcome  # the 28px bystander was not credited
    flip = report["flips"][0]
    assert flip["recovered_count"] == 1 and flip["lost"] == []
    assert flip["recovered"][0]["frame"] == "gate_0001.jpg"
    # ... and the crowd travels in the flip row, so a recovery made with two people in the frame
    # is not read as an ambiguity the stricter run simply resolved.
    assert flip["recovered"][0]["faces"] == 2, flip["recovered"]


def test_multi_subject_relaxes_the_count_rule_and_nothing_else(monkeypatch):
    """The mode must not become "a face was somewhere in this image".

    A strip of faces all below the gate's discard line is still a loss under the relaxed rule, and
    the reason is the gate's - never ``many_faces``, which says nothing about whether anything
    usable was in the frame.
    """
    from coverage_sweep import SUBJECT_ANY, sweep_frame

    _bystander_specs(monkeypatch)
    spec = DetectorSpec(input_size=640, tiles=2)
    image = _face_pixels(1280, 720, 640, 360, 48)

    strict = sweep_frame(image, [spec])[0]
    assert strict.miss_reason == "many_faces" and strict.faces == 2

    gate = GateConfig(discard_face_px=200.0)  # every detection in the frame is unusable
    relaxed = sweep_frame(image, [spec], gate=gate, subject=SUBJECT_ANY)[0]
    assert relaxed.found is False
    assert relaxed.faces == 2, "the count still reports what the detector saw"
    assert relaxed.miss_reason == "quality:too_small", relaxed.miss_reason


def test_the_report_and_the_rendered_page_name_the_counting_rule(stubbed_specs):
    """A found-count means nothing without the rule it was counted under.

    The same corpus measures very differently under the two rules, so an operator comparing this
    week's sweep with last month's - or reading a JSON pulled from a CI artifact - has to be able
    to see which one produced the number, in both the machine and the human rendering.
    """
    from coverage_sweep import SUBJECT_ANY, SUBJECT_EXACTLY_ONE, default_specs, render, sweep

    strict = sweep(_source(), default_specs())
    relaxed = sweep(_source(), default_specs(), subject=SUBJECT_ANY)
    assert strict["subject"] == SUBJECT_EXACTLY_ONE
    assert relaxed["subject"] == SUBJECT_ANY
    assert "subject rule: ONE" in render(strict)
    assert "subject rule: ANY" in render(relaxed)
    assert "--multi-subject" in render(strict)
    # The strict rule's own framing names the punch path, so nobody reads its found-count as a
    # statement about detector reach.
    assert "punch path" in render(strict)


def test_an_unknown_subject_policy_is_refused_rather_than_falling_back_to_the_strict_rule():
    """A typo'd ``subject=`` must not quietly produce strict numbers under a multi-subject heading."""
    from coverage_sweep import SweepError, default_specs, sweep_frame

    with pytest.raises(SweepError) as excinfo:
        sweep_frame(_face_pixels(1280, 720, 640, 360, 48), default_specs(), subject="anyone")
    assert "subject policy" in str(excinfo.value), str(excinfo.value)


def test_tiling_reaches_where_the_full_frame_cannot(stubbed_specs):
    """The long-range configuration earns its keep on the face 640 full-frame still misses."""
    from coverage_sweep import default_specs, sweep

    # Make the face small enough that even 640 full-frame loses it (48px native at 0.5 scale =
    # 24px... exactly at the stride; drop it to 40px so 640 full-frame is below), but a 2x2
    # grid - whose windows see the subject at ~1.6x the full-frame scale - finds it.
    stubbed_specs.native_face_px = 40.0
    report = sweep(_source(), default_specs())
    configs = report["configs"]
    assert configs["yunet@640"]["found"] == 0
    assert configs["yunet@640+2x2tiling"]["found"] == 1
    second_flip = report["flips"][1]
    assert second_flip["recovered_count"] == 1


# ---------------------------------------------------------------------------
# the CLI contract
# ---------------------------------------------------------------------------
def test_the_cli_refuses_a_missing_corpus_and_never_needs_consent():
    from coverage_sweep import main

    assert main(["--corpus", "does-not-exist"]) == 2


def _frames_dir(tmp_path: Path) -> Path:
    corpus_dir = tmp_path / "frames"
    corpus_dir.mkdir()
    _face_pixels(1280, 720, 640, 360, 48).save(corpus_dir / "gate.jpg")
    return corpus_dir


def _stub_directory_source(monkeypatch) -> None:
    """The folder contract without touching disk twice: one synthetic frame, as before."""
    import corpus_ingest

    monkeypatch.setattr(
        corpus_ingest, "DirectorySource", lambda root: FrameListSource(
            items=[Frame(image=_face_pixels(1280, 720, 640, 360, 48), name="gate.jpg")]
        )
    )


def test_the_cli_writes_the_json_report_when_asked(monkeypatch, tmp_path, stubbed_specs):
    """Four configurations, both model files named in the report.

    The SCRFD model is a stand-in here (``DetectorSpec.build`` is stubbed, so nothing loads it);
    what the test pins is that the fourth configuration is *run* - and that the report says which
    two files the comparison was made with, because "SCRFD was better" is not a finding without
    the weights it was measured on.
    """
    from coverage_sweep import main

    corpus_dir = _frames_dir(tmp_path)
    _stub_directory_source(monkeypatch)
    scrfd = tmp_path / "scrfd_10g_bnkps.onnx"
    scrfd.write_bytes(b"not a real onnx: the detector is stubbed")

    out = tmp_path / "sweep.json"
    exit_code = main([
        "--corpus", str(corpus_dir), "--json", str(out), "--scrfd-model", str(scrfd),
    ])
    assert exit_code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["frames_read"] == 1
    assert set(report["configs"]) == {
        "yunet@320", "yunet@640", "yunet@640+2x2tiling", "scrfd@640",
    }
    assert report["skipped_configs"] == []
    models = report["models"]
    assert scrfd.name in " ".join(models["scrfd"]), models
    assert models["yunet"], "the YuNet file the first three steps ran must be named too"


def test_the_cli_carries_multi_subject_into_the_report(monkeypatch, tmp_path, stubbed_specs):
    """The flag has to reach the counting rule *and* the JSON, or the report is unfalsifiable.

    A bystander frame swept with the flag but serialized without it would leave a reader with a
    higher found-count and no way to know it was measured under a rule the clock-in path does not
    use. Both runs are taken here, from the CLI, so the difference in the artifact is the point.
    """
    from coverage_sweep import main

    corpus_dir = _frames_dir(tmp_path)
    _stub_directory_source(monkeypatch)
    scrfd = tmp_path / "scrfd_10g_bnkps.onnx"
    scrfd.write_bytes(b"not a real onnx: the detector is stubbed")

    strict_out, relaxed_out = tmp_path / "strict.json", tmp_path / "relaxed.json"
    base = ["--corpus", str(corpus_dir), "--scrfd-model", str(scrfd)]
    assert main([*base, "--json", str(strict_out)]) == 0
    assert main([*base, "--json", str(relaxed_out), "--multi-subject"]) == 0

    strict = json.loads(strict_out.read_text(encoding="utf-8"))
    relaxed = json.loads(relaxed_out.read_text(encoding="utf-8"))
    assert strict["subject"] == "exactly_one"
    assert relaxed["subject"] == "any"
    assert strict["gate"] == relaxed["gate"], "the relaxed rule changes the count, never the gate"
    assert set(relaxed["configs"]) == set(strict["configs"])


def test_a_missing_scrfd_model_is_refused_rather_than_quietly_dropped(monkeypatch, tmp_path, stubbed_specs, capsys):
    """The failure this configuration exists to prevent: a comparison that was never run.

    A sweep that silently falls back to the three YuNet lines prints a report that reads as "the
    other network was measured and lost", which is exactly the paper-assumption the fourth
    configuration is meant to replace with measurement. So the absent model is a refusal - exit
    2, with the two ways forward named - and never a quieter sweep.
    """
    from coverage_sweep import main

    corpus_dir = _frames_dir(tmp_path)
    _stub_directory_source(monkeypatch)

    exit_code = main([
        "--corpus", str(corpus_dir),
        "--scrfd-model", str(tmp_path / "absent.onnx"),
    ])
    assert exit_code == 2
    message = capsys.readouterr().err
    assert "SCRFD model is missing" in message, message
    assert "--scrfd-model" in message and "--skip-scrfd" in message, message


def test_a_configuration_that_will_not_build_is_named_rather_than_raising(monkeypatch, tmp_path, capsys):
    """The wrong file behind ``--scrfd-model`` is a file path to fix, not a stack trace.

    The fourth configuration invites pointing the tool at an ONNX the operator just downloaded,
    and the likely mistake - the YuNet file by habit, or a RetinaFace export - is refused by the
    backend at construction. Reported per frame that would be forty identical ``detector_error``
    rows around the one sentence that matters, so it is raised once, naming the configuration.
    """
    from coverage_sweep import main

    import corpus_ingest

    corpus_dir = _frames_dir(tmp_path)
    _stub_directory_source(monkeypatch)
    # Build succeeds for the YuNet steps and fails for SCRFD, as a wrong export does.
    real_build = DetectorSpec.build

    def build(self):
        if self.kind == "scrfd":
            raise RuntimeError("SCRFD export does not expose a complete score_/bbox_/kps_ set")
        return real_build(self)

    monkeypatch.setattr(DetectorSpec, "build", build)
    # A file that exists (so the presence check passes) and is not a SCRFD export - the mistake
    # this guard is about: an ONNX on disk, and the wrong one.
    wrong_file = tmp_path / "yunet-by-habit.onnx"
    wrong_file.write_bytes(b"the wrong export")
    exit_code = main([
        "--corpus", str(corpus_dir),
        "--scrfd-model", str(wrong_file),
    ])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "scrfd@640" in err and "could not be built" in err, err
    assert "score_/bbox_/kps_" in err, err


def test_the_scrfd_configuration_can_be_skipped_and_the_report_says_it_was_not_measured(
    monkeypatch, tmp_path, stubbed_specs
):
    """The explicit opt-out: three lines, and a JSON that will not be misread as a comparison."""
    from coverage_sweep import main

    corpus_dir = _frames_dir(tmp_path)
    _stub_directory_source(monkeypatch)

    out = tmp_path / "sweep.json"
    exit_code = main([
        "--corpus", str(corpus_dir), "--json", str(out), "--skip-scrfd",
    ])
    assert exit_code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert set(report["configs"]) == {"yunet@320", "yunet@640", "yunet@640+2x2tiling"}
    skipped = report["skipped_configs"]
    assert [entry["name"] for entry in skipped] == ["scrfd@640"], skipped
    assert "--skip-scrfd" in skipped[0]["reason"], skipped
    from coverage_sweep import render

    assert "NOT MEASURED" in render(report), render(report)
