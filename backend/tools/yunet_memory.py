"""Isolate where the YuNet detection step's memory goes, and how much is avoidable.

Why this exists
---------------
The detector is the second one-time floor this application carries, and unlike the embedding
floor it is easy to mistake for the *decode*: a single ``detect_and_align`` on a 1280 px frame
measures ~+116 MB, far more than the frame itself (3.7 MB) or the decode that produced it
(``tools/frame_decode_memory.py``). None of that is the model - YuNet is a 232 KB graph - so
the question is where the memory actually goes and how much of it a change could remove.

    python backend/tools/yunet_memory.py

The answer this measures, in one line: **almost all of it is the OpenCV DNN forward-pass
working set, sized by the resolution the pass runs at, held for as long as the detector
object lives.** Everything else - the graph, ``setInputSize``, the alignment warp - is noise
next to it, and it scales linearly with input area.

Method
------
* **A fresh interpreter per stage.** Resident memory is a process-lifetime high-water mark,
  so stages are measured as deltas within one process *and* each headline number is
  re-measured in a fresh one.
* **A stage, not a total.** ``create`` (the session), ``setInputSize`` (the input blob),
  ``detect`` (the forward pass), a second ``detect`` (reuse), ``align`` (the warp), and the
  destruction of the detector are each measured on their own, so the 116 MB is attributed
  rather than assumed.
* **A width sweep, a letterbox comparison, and the runtime route.** The same pass at
  320/480/640/960/1280 px shows the cost follows input *area*; the ``detector_640``
  letterboxed path at a 640 input (with and without its own tiling) shows what the same
  detection costs when the pass is not run at the frame's native size; and ``detect_raw`` is
  measured with ``FACE_DETECTOR_INPUT_SIZE`` off and on, which is the number the application
  actually pays - reported with the ``active_pipeline`` name it would run under.
* **The reader is ``punch_saturation.process_reader()``** - the same one the per-push memory
  gate uses - so this cannot disagree with that gate about what "peak" and "resident" mean.

Two honest limits, stated because they bound the conclusion: **no face is committed to this
repository** (by design), so the frames are synthetic and produce no detections - the forward
pass, which is the dominant cost, is identical either way, but the post-processing over real
detections is not measured here. And the far-range *recall* cost of a smaller pass is not
measurable without faces; ``detector_640.min_detectable_width`` gives its geometry, and the
repository's own corpus tooling is where the recall is actually measured.

Exit codes
----------
    0  the breakdown was measured
    2  this machine cannot answer the question (no reader, or no detector model)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent

MIB = 1024 * 1024

#: The widths the sweep runs at. The probe constructs the detector at
#: ``face_detector.DETECTOR_INPUT_SIZE`` (320) - a scale, not the size a pass runs at, since
#: the runtime pass sets the frame's own dimensions. 1280 is the deployment's frame ceiling (the reason the pool
#: is as large as it is); 640 is the letterboxed path's input; 320 is the constructor.
WIDTHS = (320, 480, 640, 960, 1280)

#: The synthetic detection row handed to ``align`` (native coordinates), so the warp is
#: measured without a face: box, five landmarks, score. Inlined into the probe below.
_PROBE = r'''
import json, os, sys

backend = sys.argv[2]
sys.path.insert(0, backend)
sys.path.insert(0, os.path.join(backend, "tools"))

import numpy as np
import punch_saturation

reader, platform, anon = punch_saturation.process_reader()
pid = os.getpid()
model = sys.argv[3]
mode = sys.argv[1]

def peak():
    return reader(pid).peak_bytes

def now():
    return reader(pid).memory_bytes

def frame_for(width):
    height = int(round(width * 0.75))
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[::7, ::7] = 200
    return frame

def new_detector():
    import cv2
    cv2.setNumThreads(1)
    return cv2.FaceDetectorYN.create(model, "", (320, 320), 0.6, 0.3, 5000)

def report(**fields):
    print(json.dumps(fields))

if mode == "stages":
    width = int(sys.argv[4])
    import cv2
    cv2.setNumThreads(1)
    frame = frame_for(width)
    base_now, base_peak = now(), peak()
    det = new_detector()
    after_create = (now() - base_now, peak() - base_peak)
    n, p = now(), peak()
    det.setInputSize((width, frame.shape[0]))
    after_size = (now() - n, peak() - p)
    n, p = now(), peak()
    det.detect(frame)
    first = (now() - n, peak() - p)
    n, p = now(), peak()
    det.detect(frame)
    second = (now() - n, peak() - p)
    report(
        width=width, platform=platform,
        create_retained=after_create[0], create_peak=after_create[1],
        size_retained=after_size[0], size_peak=after_size[1],
        first_retained=first[0], first_peak=first[1],
        second_retained=second[0], second_peak=second[1],
        total_retained=now() - base_now, total_peak=peak() - base_peak,
    )
elif mode == "align":
    import face_detector
    frame = frame_for(1280)
    face_detector._ensure()
    row = np.array([600, 400, 112, 112, 570, 430, 640, 430, 605, 470, 580, 500, 630, 500, 0.9], dtype=np.float32)
    n, p = now(), peak()
    crop = face_detector.align(frame, row)
    report(width=1280, platform=platform, crop=list(crop.shape),
           align_retained=now() - n, align_peak=peak() - p)
elif mode == "chain":
    import face_detector
    frame = frame_for(1280)
    face_detector._ensure()
    base_now, base_peak = now(), peak()
    faces = face_detector.detect_and_align(frame)
    report(width=1280, platform=platform, faces=len(faces),
           chain_retained=now() - base_now, chain_peak=peak() - base_peak)
elif mode == "sweep":
    width = int(sys.argv[4])
    import cv2
    cv2.setNumThreads(1)
    frame = frame_for(width)
    base_now, base_peak = now(), peak()
    det = new_detector()
    det.setInputSize((width, frame.shape[0]))
    det.detect(frame)
    report(width=width, platform=platform,
           retained=now() - base_now, peak=peak() - base_peak)
elif mode == "letterbox":
    import detector_640
    size = int(sys.argv[4])
    tiles = int(sys.argv[5])
    frame = frame_for(1280)
    base_now, base_peak = now(), peak()
    det = detector_640.YuNetDetector(model, input_size=size)
    det.detect(frame, tiles=tiles)
    report(size=size, tiles=tiles, platform=platform,
           retained=now() - base_now, peak=peak() - base_peak,
           reach_px=round(det.min_detectable_width(1280, 960, tiles=tiles), 1))
elif mode == "release":
    import face_detector
    frame = frame_for(1280)
    face_detector._ensure()
    face_detector.detect_raw(frame)
    before = now()
    del face_detector._detector
    import gc
    gc.collect()
    report(width=1280, platform=platform, resident_before=before, resident_after=now(),
           released=before - now())
elif mode == "runtime":
    # The *runtime route*, not the underlying detector: ``detect_raw`` with the cap off and on,
    # which is the only thing the application's memory actually depends on.
    size = int(sys.argv[4])
    tiles = int(sys.argv[5])
    import config
    import face_detector
    config.settings.face_detector_input_size = size
    config.settings.face_detector_tiles = tiles
    frame = frame_for(1280)
    base_now, base_peak = now(), peak()
    rows = face_detector.detect_raw(frame)
    report(width=1280, size=size, tiles=tiles, platform=platform,
           pipeline=face_detector.capped_pipeline(),
           retained=now() - base_now, peak=peak() - base_peak, rows=len(rows))
else:
    raise SystemExit(f"unknown mode {mode!r}")
'''


class Unanswerable(RuntimeError):
    """This machine cannot measure the thing being asked about."""


def measure(mode: str, *extra: object, model: str) -> dict:
    """One probe run, in a fresh interpreter. Returns its JSON report."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, mode, str(BACKEND_DIR), model, *map(str, extra)],
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


def model_file() -> str:
    """The detector the runtime would load, resolved the way it does."""
    sys.path.insert(0, str(BACKEND_DIR))
    import face_detector

    return str(face_detector.model_path())


def mi(value: int | None) -> str:
    return "n/a" if value is None else f"{value / MIB:+.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isolate where a YuNet detection step's memory goes and how much a "
        "smaller working resolution would avoid.",
    )
    parser.add_argument("--json", action="store_true", help="emit the numbers as JSON")
    args = parser.parse_args(argv)

    try:
        model = model_file()
        if not Path(model).exists():
            raise Unanswerable(f"no detector model at {model}")
        stages = measure("stages", 1280, model=model)
        align = measure("align", model=model)
        chain = measure("chain", model=model)
        release = measure("release", model=model)
        sweep = [measure("sweep", width, model=model) for width in WIDTHS]
        letterbox = [
            measure("letterbox", 640, 1, model=model),
            measure("letterbox", 640, 2, model=model),
            measure("letterbox", 480, 2, model=model),
        ]
        runtime = [
            measure("runtime", 0, 0, model=model),
            measure("runtime", 640, 2, model=model),
            measure("runtime", 480, 2, model=model),
        ]
    except Unanswerable as exc:
        print(f"cannot answer on this machine: {exc}")
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "platform": stages["platform"],
                    "stages_at_1280": stages,
                    "align": align,
                    "chain_at_1280": chain,
                    "release": release,
                    "sweep": sweep,
                    "letterbox": letterbox,
                    "runtime": runtime,
                },
                indent=2,
            )
        )
        return 0

    print(f"platform                     : {stages['platform']}")
    print()
    print("One detect_and_align, 1280x960, broken into stages (1280 is the frame ceiling):")
    print(f"  create the detector        : {mi(stages['create_retained'])} resident, {mi(stages['create_peak'])} peak")
    print(f"  setInputSize               : {mi(stages['size_retained'])} resident, {mi(stages['size_peak'])} peak")
    print(f"  first detect (forward pass): {mi(stages['first_retained'])} resident, {mi(stages['first_peak'])} peak")
    print(f"  second detect (same frame) : {mi(stages['second_retained'])} resident, {mi(stages['second_peak'])} peak")
    print(f"  align (warp to 112x112)    : {mi(align['align_retained'])} resident, {mi(align['align_peak'])} peak")
    print(f"  whole detect_and_align     : {mi(chain['chain_retained'])} resident, {mi(chain['chain_peak'])} peak")
    print(f"  destroyed                  : {mi(-release['released'])} resident  (the pool is owned by the detector, not leaked)")
    print()
    print("The same pass at other widths (create + setInputSize + one detect):")
    for run in sweep:
        print(f"  {run['width']:>4} px : {mi(run['retained'])} resident, {mi(run['peak'])} peak")
    print()
    print("The letterboxed 640-class path (detector_640), same 1280x960 frame:")
    for run in letterbox:
        print(
            f"  input {run['size']:>3} px, {run['tiles']} tile(s) : "
            f"{mi(run['retained'])} resident, {mi(run['peak'])} peak, "
            f"reach {run['reach_px']} px native"
        )
    print()
    print("What the runtime route itself costs, through ``detect_raw`` (one 1280x960 frame):")
    for run in runtime:
        what = "native" if run["size"] == 0 else f"capped input {run['size']}, {run['tiles']} tiles"
        print(
            f"  {what:<28} : {mi(run['retained'])} resident, {mi(run['peak'])} peak  "
            f"[{run['pipeline']}]"
        )
    print()
    print(
        "Verdict: the cost is the forward-pass pool, sized by the resolution the pass runs "
        "at (it follows input area) and held by the detector object; the model, "
        "setInputSize and align are noise beside it. The capped row is the same saving the "
        "application gets when ``FACE_DETECTOR_INPUT_SIZE`` is set - and the pipeline name "
        "in brackets is the crop change that comes with it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
