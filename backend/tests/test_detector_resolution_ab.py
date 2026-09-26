"""The resolution A/B tool's own contract: its arms, its buckets, and its refusals.

The measurement itself needs real faces and belongs on the host that has them
(``tools/detector_resolution_ab.py``). What is pinned here is the part that would fail silently:
the arms have to straddle the ceiling and the cap (an "A/B" with both arms at 1280 measures
nothing), a frame size has to land in exactly one bucket (a gap would silently drop a sample), and
a corpus that cannot answer the question must say so rather than print a green verdict about it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

import harness

BACKEND_DIR = harness.BACKEND_DIR


def _tool():
    """The tool as a module, without running its ``__main__``."""
    spec = importlib.util.spec_from_file_location(
        "detector_resolution_ab", BACKEND_DIR / "tools" / "detector_resolution_ab.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_arms_straddle_the_ceiling_and_the_cap():
    """The reference, a same-resolution control, and at least one arm below the ceiling.

    Without an arm below the ceiling there is nothing to compare; without the same-resolution
    control, a broken map and a lost face are the same number - which is the whole reason the
    control exists.
    """
    tool = _tool()
    arms = {spec["name"]: spec for spec in tool.arm_specs(1280)}

    assert arms["native"]["input_size"] is None
    assert arms["control"]["input_size"] == 1280
    assert {name: spec["input_size"] for name, spec in arms.items() if name.startswith("cap")} == {
        "cap640x1": 640,
        "cap640x2": 640,
        "cap480x2": 480,
    }
    assert arms["cap640x2"]["tiles"] == 2, "the reach-recovering arm is the one being judged"


def test_every_face_width_lands_in_exactly_one_bucket():
    """A gap between buckets would drop a sample; an overlap would double-count one."""
    tool = _tool()
    for width in (0.0, 23.9, 31.9, 32.0, 63.9, 64.0, 95.9, 96.0, 159.9, 160.0, 319.9, 320.0, 5000.0):
        hits = [
            name
            for name, low, high in tool.SIZE_BUCKETS
            if low <= width < high
        ]
        assert len(hits) == 1, (width, hits)


def test_a_corpus_that_cannot_answer_the_question_exits_inconclusive(tmp_path: Path):
    """No frames is exit 2, not exit 0 - a green verdict from an empty sample is the one
    failure mode that would make this tool worse than not having it.
    """
    tool = _tool()
    empty = tmp_path / "empty"
    empty.mkdir()

    assert tool.main(["--mode", "corpus", "--corpus", str(empty)]) == 2


def test_an_unreadable_frame_folder_is_inconclusive_rather_than_a_crash(tmp_path: Path):
    tool = _tool()

    assert tool.main(["--mode", "corpus", "--corpus", str(tmp_path / "absent")]) == 2


def test_the_drift_is_magnitude_invariant():
    """The drift is compared against a cosine decision line, so it must not see a scale.

    An un-normalised engine vector would otherwise report a magnitude as a crop change - and a
    tool that says "the crop moved" when it did not is worse than one that says nothing.
    """
    tool = _tool()
    a = np.array([0.3, 0.4, 0.5, 0.6], dtype=np.float64)
    b = np.array([0.6, 0.5, 0.4, 0.3], dtype=np.float64)

    scaled = tool.cosine_distance(a * 12.0, b / 7.0, np)
    assert abs(tool.cosine_distance(a, b, np) - scaled) < 1e-12
    assert tool.cosine_distance(a, a, np) == 0.0


def test_the_distribution_reports_the_tail_not_just_the_middle():
    """The verdict is the *worst* drift, so the summary has to carry the tail."""
    tool = _tool()
    values = [0.001 * index for index in range(1, 101)]
    summary = tool.distribution(values)

    assert summary["n"] == 100
    assert summary["max"] == 0.1
    assert summary["p50"] <= summary["p95"] <= summary["p99"] <= summary["max"]
    assert tool.distribution([]) == {"n": 0}
