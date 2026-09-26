"""Re-measure one face-frame decode's peak, on the machine this is run on.

Why this exists
---------------
The ``exif_transpose`` change (``docs/FACE_VERIFICATION_MEMORY_REPORT.md`` §3) is worth about
12.5 MB of peak per verification, and the numbers behind that claim were taken on a **Windows**
development box. The deployment is Linux, where the allocator is a different one and the
absolute figures are not expected to match - the *difference* should, because it is one
full-resolution buffer, but that is a prediction and predictions are what this re-takes.

    python backend/tools/frame_decode_memory.py

It is deliberately not part of the test suite: the suite's own guard
(``tests/test_face_frame_chain.py::test_an_upright_decode_does_not_pay_for_a_transpose_buffer``)
asks the same question but has to run everywhere without a hand-held reader, while this is the
instrument an operator points at the deployment host.

The route to the Linux figures from a machine that has no Linux on it is the hand-dispatched
``frame-decode-memory`` job in ``.github/workflows/punch-memory.yml``: it runs this file
unchanged on ``ubuntu-latest`` and then again inside the built deployment image under
``--memory 512m --memory-swap 512m --cpus 1``, where the allocator is the deployment's own
jemalloc rather than the runner's glibc.

Method, and why it is not one measurement
-----------------------------------------
* **A fresh interpreter per measurement.** Peak resident memory is a process-lifetime
  high-water mark (``VmHWM`` on Linux, ``PeakWorkingSetSize`` on Windows) and never comes back
  down, so two measurements in one process would make the second inherit the first's peak and
  always look free.
* **A comparison, not an absolute figure.** An absolute megabyte count is a property of one
  allocator on one operating system. A *rotated* photo's transpose is required, so its peak
  necessarily contains the extra full-resolution buffer that the fix avoids for an upright one
  - the same pixel dimensions on both sides mean the difference is that buffer and nothing
  else. The verdict is therefore "upright is cheaper than rotated by at least a quarter of a
  source buffer", which travels between platforms.
* **A sensitivity check.** A third run allocates two 2048x1536 buffers with nothing else
  happening, which is what proves this allocator can see such a buffer *at all*. Without it,
  "the allocator reused the freed memory" and "the copy is back in the chain" measure the same,
  and a green tick would mean nothing.

The peak reader is ``punch_saturation.process_reader()``'s - the same one the per-push memory
gate uses - so this cannot disagree with that gate about what "peak" means on either platform.

Exit codes
----------
    0  the saving is present and measurable on this machine
    1  no saving measured: the upright decode costs what the rotated one does
    2  this machine cannot answer the question (no reader, or the allocator hid the buffer)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent

#: The upload the report measured at: 3.1 MP, inside the 2048 px ingestion boundary. The frame
#: the chain produces is 1280x960, but the buffer this is about is the *source* one below.
SOURCE_WIDTH, SOURCE_HEIGHT = 2048, 1536
SOURCE_BUFFER = SOURCE_WIDTH * SOURCE_HEIGHT * 3

#: How much of one source buffer the two runs must differ by before the difference is called
#: real. The measured difference is ~1.3 buffers and the run-to-run noise is ~0.3 MB, so a
#: quarter of a buffer is ~8x the noise and still far inside the signal.
MARGIN = SOURCE_BUFFER // 4

#: Runs in a fresh interpreter. ``calibrate`` reports whether this allocator exposes a
#: full-resolution buffer; ``1`` and ``6`` are EXIF orientations to decode.
_PROBE = r'''
import io, json, os, sys

mode = sys.argv[1]
sys.path.insert(0, sys.argv[2])            # backend/tools: the shared peak reader
sys.path.insert(0, os.path.abspath("."))   # backend: the application itself

import numpy as np
import punch_saturation
import uploads
from PIL import Image

reader, platform, anon_separable = punch_saturation.process_reader()


def peak():
    """This process's own high-water mark, in bytes, from the platform's accounting."""
    return reader(os.getpid()).peak_bytes


def report(delta):
    print(json.dumps({
        "peak_above_base": int(delta),
        "platform": platform,
        "anon_separable": bool(anon_separable),
    }))


if mode == "calibrate":
    # Two full-resolution buffers, the second allocated while the first is live and nothing
    # else happening. When this is ~0 the allocator cannot answer the question and the caller
    # says so instead of reading a level result as "the chain stopped allocating the buffer".
    held = Image.new("RGB", (2048, 1536))
    base = peak()
    extra = held.copy()
    report(peak() - base)
    raise SystemExit(0)

rng = np.random.RandomState(0)
photo = Image.fromarray(rng.randint(0, 255, (1536, 2048, 3), dtype=np.uint8))
buffer = io.BytesIO()
exif = Image.Exif()
exif[0x0112] = int(mode)
photo.save(buffer, format="JPEG", quality=85, exif=exif)
data = buffer.getvalue()
del photo, buffer
# Built *before* the baseline is read, so the encoder's own peak is not charged to the decode.
base = peak()
frame = uploads.face_frame(data, field="selfie")
report(peak() - base)
'''


class Unanswerable(RuntimeError):
    """This machine cannot measure the thing being asked about."""


def measure(mode: str) -> dict:
    """One probe run, in a fresh interpreter. Returns its JSON report."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, mode, str(TOOLS_DIR)],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise Unanswerable(f"the {mode!r} probe failed: {result.stderr.strip()[-500:]}")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise Unanswerable(f"the {mode!r} probe printed nothing usable: {exc}") from exc


def format_mib(value: int) -> str:
    return f"{value / (1024 * 1024):+.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure one face-frame decode's peak and report whether the upright path "
        "avoids the transpose buffer.",
    )
    parser.add_argument("--json", action="store_true", help="emit the numbers as JSON")
    args = parser.parse_args(argv)

    try:
        calibration = measure("calibrate")
        upright_run = measure("1")
        rotated = measure("6")["peak_above_base"]
    except Unanswerable as exc:
        print(f"cannot answer on this machine: {exc}")
        return 2

    sensitivity = calibration["peak_above_base"]
    upright = upright_run["peak_above_base"]
    platform = upright_run["platform"]
    saving = rotated - upright
    present = sensitivity > MARGIN and saving > MARGIN

    if args.json:
        print(
            json.dumps(
                {
                    "platform": platform,
                    "source_buffer_bytes": SOURCE_BUFFER,
                    "margin_bytes": MARGIN,
                    "allocator_sensitivity_bytes": sensitivity,
                    "upright_peak_bytes": upright,
                    "rotated_peak_bytes": rotated,
                    "saving_bytes": saving,
                    "saving_present": present,
                    "allocator_can_see_a_buffer": sensitivity > MARGIN,
                },
                indent=2,
            )
        )
    else:
        print(f"platform        : {platform}")
        print(
            f"source buffer   : {SOURCE_BUFFER / (1024 * 1024):.1f} MiB, "
            f"margin {MARGIN / (1024 * 1024):.1f} MiB"
        )
        print(f"allocator check : {format_mib(sensitivity)}  (one extra 2048x1536 buffer)")
        print(f"upright decode  : {format_mib(upright)}")
        print(f"rotated decode  : {format_mib(rotated)}")
        print(f"difference      : {format_mib(saving)}")

    if sensitivity <= MARGIN:
        print()
        print(
            "INCONCLUSIVE: this allocator did not expose a single extra full-resolution buffer, "
            "so a missing copy and a reused allocation look the same here."
        )
        return 2

    if not present:
        print()
        print(
            "FAIL: an upright decode costs what a rotated one does, on a machine that *can* see "
            "a full-resolution buffer - a copy of the photo is being allocated again "
            "(docs/FACE_VERIFICATION_MEMORY_REPORT.md, section 3)."
        )
        return 1

    print()
    print(
        f"OK: the upright decode avoids one full-resolution buffer ({format_mib(saving)} measured "
        f"against a {SOURCE_BUFFER / (1024 * 1024):.1f} MiB source buffer)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
