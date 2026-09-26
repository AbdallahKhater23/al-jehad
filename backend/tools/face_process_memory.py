"""Measure what the API process stops carrying when the models move to a child.

Why this exists
---------------
``face_process`` moves the face models out of the API process, and the claim it makes is
specific: **the API process's resident set stops containing the model floor.** The floor is
the ~150 MiB the 87 MiB FaceNet graph leaves behind once its session is built, plus the
detector's own arena sized by the largest frame it has seen (~86 MiB at the 1280 px ceiling) -
one-time, never returned, and measured in ``docs/FACE_VERIFICATION_MEMORY_REPORT.md``.

That claim is an architectural one, so it should be *shown* rather than argued. This tool
runs the same two model calls the startup preload and the first punch make, once with the
flag off and once with it on, each in a fresh interpreter, and reports where the megabytes
end up on this host:

    python backend/tools/face_process_memory.py

It is deliberately not part of the test suite: the suite's own guard
(``tests/test_face_process.py``) pins the *wiring* everywhere, and this is the instrument an
operator points at a host to see the trade in the deployment's own allocator.

Method, and why it is a comparison
----------------------------------
* **A fresh interpreter per mode.** Resident memory is a process-lifetime high-water mark
  and never comes back down, so measuring both modes in one process would make the second
  inherit the first's peak.
* **The same calls on both sides.** ``face_onnx.load_now()`` and then
  ``face_engine.ENGINE.detect_direct`` on a 1280x960 frame: the preload plus a detector run
  at the ceiling size. With the flag on those two forward to the child, so the *same work*
  happens and only its address changes.
* **The child is read from outside it.** The child's own RSS is taken with the same
  ``punch_saturation`` reader that measures any other process, keyed on the pid the child
  reported (``worker_pid``) rather than the pid we spawned - because on Windows a virtualenv's
  ``python.exe`` is a launcher stub and the models run in a different process from the one
  ``subprocess`` returned.
* **A sensitivity check.** A third run allocates one 160 MiB buffer and confirms the reader
  can see a buffer of the floor's size. Without it, "the parent really did drop the models"
  and "this allocator reports nothing useful" measure the same.

The verdict is a *difference* and a *destination*: the parent's floor is smaller with the
flag on, and the child that got the work is itself large. The second half matters - a
prototype that merely failed to load the models would pass the first half alone.

Exit codes
----------
    0  the floor left the API process and landed in the child
    1  the API process is still carrying the floor with the flag on
    2  this machine cannot answer the question (no reader, no model, allocator blind)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent

MIB = 1024 * 1024

#: The floor's own size, for the sensitivity check: a buffer big enough that a reader which
#: cannot see it cannot see the model session either.
CALIBRATION_BUFFER = 160 * MIB

#: How much smaller the parent's floor must be before the difference is called real. The
#: floor is ~150 MiB and the run-to-run noise is a few MiB, so this is several times the
#: noise and still well inside the signal.
MARGIN = 64 * MIB

#: Runs in a fresh interpreter. ``mode`` is ``calibrate`` (reader sensitivity), ``inline``
#: (flag off) or ``child`` (flag on).
_PROBE = r'''
import json, os, sys

mode = sys.argv[1]
backend = sys.argv[2]
if mode == "child":
    # Set before config is imported anywhere: the flag is read at import.
    os.environ["FACE_ENGINE_PROCESS"] = "1"
sys.path.insert(0, backend)                              # backend: the application
sys.path.insert(0, os.path.join(backend, "tools"))       # the shared peak reader

import numpy as np
import punch_saturation

reader, platform, anon_separable = punch_saturation.process_reader()
here_pid = os.getpid()


def read(pid=None):
    return reader(pid or here_pid)


def report(**fields):
    print(json.dumps(fields))


if mode == "calibrate":
    base = read().peak_bytes
    held = b"\x00" * (160 * 1024 * 1024)  # written, so the pages are resident
    report(platform=platform, allocated=len(held), seen_bytes=int(read().peak_bytes - base))
    raise SystemExit(0)

import face_engine
import face_onnx

base_now = read().memory_bytes
base_peak = read().peak_bytes

# The startup preload, then a detector run at the ceiling frame size: the two things that
# build the floor. Both forward when the flag is on.
face_onnx.load_now()
frame = np.zeros((960, 1280, 3), dtype=np.uint8)
try:
    face_engine.ENGINE.detect_direct(frame)
    detect_error = None
except Exception as exc:  # noqa: BLE001 - reported, not fatal
    detect_error = f"{type(exc).__name__}: {exc}"

here = read()
out = {
    "platform": platform,
    "mode": mode,
    "parent_memory_above_base": int(here.memory_bytes - base_now),
    "parent_peak_above_base": int(here.peak_bytes - base_peak),
    "parent_rss_bytes": int(here.memory_bytes),
    "detect_error": detect_error,
}

if mode == "child":
    import face_process

    client = face_process.current() or face_process.client()
    out["worker_pid"] = client.worker_pid
    out["transport"] = client.stats()
    if client.worker_pid:
        child = read(client.worker_pid)
        out["child_rss_bytes"] = int(child.memory_bytes)
        out["child_peak_bytes"] = int(child.peak_bytes)

report(**out)
'''


class Unanswerable(RuntimeError):
    """This machine cannot measure the thing being asked about."""


def measure(mode: str) -> dict:
    """One probe run, in a fresh interpreter. Returns its JSON report."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, mode, str(BACKEND_DIR)],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise Unanswerable(f"the {mode!r} probe failed: {result.stderr.strip()[-600:]}")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise Unanswerable(f"the {mode!r} probe printed nothing usable: {exc}") from exc


def mi(value: int | None) -> str:
    return "n/a" if value is None else f"{value / MIB:.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the API process's face-model floor with the models inline and "
        "with them in a child process (FACE_ENGINE_PROCESS).",
    )
    parser.add_argument("--json", action="store_true", help="emit the numbers as JSON")
    args = parser.parse_args(argv)

    try:
        calibration = measure("calibrate")
        inline = measure("inline")
        child = measure("child")
    except Unanswerable as exc:
        print(f"cannot answer on this machine: {exc}")
        return 2

    seen = calibration["seen_bytes"]
    inline_floor = inline["parent_memory_above_base"]
    child_parent = child["parent_memory_above_base"]
    child_own = child.get("child_rss_bytes")
    saving = inline_floor - child_parent

    if args.json:
        print(
            json.dumps(
                {
                    "platform": inline["platform"],
                    "margin_bytes": MARGIN,
                    "calibrator_bytes": CALIBRATION_BUFFER,
                    "allocator_sensitivity_bytes": seen,
                    "inline_parent_floor_bytes": inline_floor,
                    "child_parent_floor_bytes": child_parent,
                    "child_process_rss_bytes": child_own,
                    "parent_saving_bytes": saving,
                    "detect_error": child.get("detect_error"),
                    "worker_pid": child.get("worker_pid"),
                },
                indent=2,
            )
        )
    else:
        print(f"platform            : {inline['platform']}")
        print(f"margin              : {MARGIN / MIB:.0f} MiB")
        print(f"allocator check     : {mi(seen)}  (one {CALIBRATION_BUFFER // MIB} MiB buffer)")
        print()
        print(f"models inline       : API process + {mi(inline_floor)}")
        print(f"models in a child   : API process + {mi(child_parent)}")
        print(f"                    : model process {mi(child_own)}")
        print(f"                    : API process saved {mi(saving)}")

    if seen <= MARGIN:
        print()
        print(
            "INCONCLUSIVE: this allocator did not expose a buffer the size of the model "
            "floor, so 'the parent dropped the models' and 'the reader sees nothing' look "
            "the same here."
        )
        return 2

    if inline_floor <= MARGIN:
        print()
        print(
            "INCONCLUSIVE: with the flag off the API process did not carry a model floor at "
            "all - the models are probably missing from this checkout, so there is nothing "
            "to move."
        )
        return 2

    if saving <= MARGIN:
        print()
        print(
            "FAIL: with the flag on the API process still carries the model floor "
            "(docs/FACE_VERIFICATION_MEMORY_REPORT.md, section 2)."
        )
        return 1

    if child_own is not None and child_own <= MARGIN:
        print()
        print(
            f"FAIL: the API process dropped {mi(saving)}, but the model process holds only "
            f"{mi(child_own)} - the work did not move, it went missing."
        )
        return 1

    print()
    print(
        f"OK: the models are in the child ({mi(child_own)} held there) and the API process "
        f"carries {mi(saving)} less (from {mi(inline_floor)} to {mi(child_parent)})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
