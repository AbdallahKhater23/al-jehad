"""Does detecting at a capped working size change the crop a punch is scored with?

WHY THIS EXISTS
---------------
The detector's working set is sized by the frame it is *shown*, and it is the deployment's second
memory floor: ~106 MB peak and ~88 MB kept at the 1280 px ceiling, against a 232 KB graph
(``tools/yunet_memory.py``). Running the pass at a 640-class input instead costs ~26 MB, and
``detector_640`` already does the hard half - a letterboxed input with an *invertible* map back to
native coordinates, verified to ~1e-13 px - with an optional 2x2 grid that recovers the far-range
geometric reach on paper (``min_detectable_width``).

What none of that measures is whether the **crop** survives, and the crop is the whole answer: a
template is an embedding of a crop, so a crop that moves by a few pixels moves the vector, and that
vector is compared against templates enrolled through the *native* pass with about two hundredths of
cosine between the measured genuine ceiling and the decision line. This tool measures that.

THE ARMS, AND WHY THERE IS A CONTROL
------------------------------------
Every arm sees the *same* frame, and every crop is made by the *same* ``face_detector.align`` - only
the detection that positions it differs:

    native      face_detector.detect_raw           the deployment today (reference)
    control     detector_640, input = ceiling      same resolution, new plumbing
    cap640x1    detector_640, input 640, 1 tile    the memory win, no reach recovery
    cap640x2    detector_640, input 640, 2 tiles   the memory win *and* the reach back

``control`` is what makes this controlled rather than a comparison of two detectors: it runs the
letterbox-and-map machinery at the frame's own resolution, so its drift is the plumbing being wrong
while the 640 arms' drift is resolution. Without it, a bad map and a lost face are one number.

TWO MODES
---------
* ``--mode corpus`` measures the deployment's own stored frames. This is the answer when the host
  has them; it reports subject coverage (a cap that loses a face loses a *punch*), box agreement,
  and crop drift bucketed by face width - because a cap is supposed to cost the far range
  specifically, and the buckets say whether that is where the cost lands.
* ``--mode synth`` builds far-range frames out of real faces when no far-range corpus exists: each
  real photo is scaled so its detected face hits a target width, and that scaled *scene* is pasted
  onto a blurred version of itself at the ceiling size. The face keeps its own real texture, its own
  local background and its own JPEG history; what it does not keep is a real distant scene's
  clutter, so a face found here is a **ceiling** on recall, not a field estimate. It is the honest
  way to find the width at which the cap starts losing faces the native pass still finds.

THE DATA, AND WHAT IS NOT DONE WITH IT
--------------------------------------
``--corpus`` defaults to the deployment's punch-frame directory and ``--synth-from`` to the enrolled
worker photos. Those are **people**: this reads them, embeds crops and prints aggregates - it writes
no image, copies nothing, and prints no per-frame geometry or path (synth labels are anonymous).
Run it only on a host you are allowed to run it on.

    python backend/tools/detector_resolution_ab.py --mode corpus --sample 200
    python backend/tools/detector_resolution_ab.py --mode synth

Exit codes
----------
    0  the cap preserves the crop inside the band's headroom on this material
    1  it does not: drift or lost subjects make the cap unsafe for stored templates
    2  the question could not be answered (no frames, no model, nothing survived)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

#: Face widths (native px) the drift is bucketed by. The deployment's punch band puts a subject at
#: 106-226 px (``face_detector.MIN_SUBJECT_PX``), so the far range is everything below ~96 and the
#: buckets exist to show whether a cap's cost lands there rather than spread evenly.
SIZE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<32 px", 0.0, 32.0),
    ("32-64 px", 32.0, 64.0),
    ("64-96 px", 64.0, 96.0),
    ("96-160 px", 96.0, 160.0),
    ("160-320 px", 160.0, 320.0),
    (">=320 px", 320.0, float("inf")),
)

#: The face widths the synth mode drives, in native pixels. The first is below the native pass's own
#: 16 px anchor floor (that arm is the control that says the harness can measure a miss at all), the
#: rest walk up through the far range and into the deployment's own band.
SYNTH_TARGETS: tuple[int, ...] = (12, 16, 24, 32, 48, 64, 96, 128)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class Unanswerable(RuntimeError):
    """This host cannot answer the question asked of it."""


def arm_specs(ceiling: int) -> list[dict]:
    """The arms, in report order. ``native`` is the reference and has no ``detector_640`` peer."""
    return [
        {"name": "native", "input_size": None, "tiles": 1},
        {"name": "control", "input_size": ceiling, "tiles": 1},
        {"name": "cap640x1", "input_size": 640, "tiles": 1},
        {"name": "cap640x2", "input_size": 640, "tiles": 2},
        {"name": "cap480x2", "input_size": 480, "tiles": 2},
    ]


def image_paths(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise Unanswerable(f"no such folder: {folder}")
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        raise Unanswerable(f"no image files under {folder}")
    return files


def sample_frames(corpus: Path, count: int) -> list[Path]:
    """``count`` frames, evenly spaced through the sorted listing.

    Deterministic rather than random: the same corpus must give the same answer twice, or a number
    quoted from one run cannot be checked by the next person.
    """
    files = image_paths(corpus)
    if len(files) <= count:
        return files
    step = len(files) / count
    return [files[int(index * step)] for index in range(count)]


def usable_rows(face_detector, frame) -> list:
    """Production detections that could be the person at the gate, largest first."""
    rows = []
    for row in face_detector.detect_raw(frame):
        try:
            width, height = float(row[2]), float(row[3])
        except (IndexError, TypeError, ValueError):
            continue
        if min(width, height) >= face_detector.MIN_SUBJECT_PX:
            rows.append(row)
    return sorted(rows, key=lambda row: float(row[2]) * float(row[3]), reverse=True)


def row_from_detection(detection):
    """A ``detector_640.Detection`` in the YuNet row shape ``face_detector.align`` reads.

    The crop has to be made by the *same* code in every arm, so an arm's geometry is handed to
    ``align`` in the layout it already understands rather than re-warped here - the one thing this
    experiment must not vary is the aligner.
    """
    import numpy as np

    x, y, w, h = detection.box
    points = np.asarray(detection.landmarks, dtype=np.float32).reshape(5, 2)
    return np.array([x, y, w, h, *points.ravel(), float(detection.score)], dtype=np.float32)


def cosine_distance(first, second, np) -> float:
    """``1 - cos(a, b)``, normalising here rather than trusting the engine to.

    The engine does L2-normalise, but this number is compared against a decision *line*, so it is
    worth being independent of that: an un-normalised vector would otherwise report a drift that is
    really a magnitude.
    """
    a = first / (float(np.linalg.norm(first)) or 1.0)
    b = second / (float(np.linalg.norm(second)) or 1.0)
    return 1.0 - float(np.dot(a, b))


def bucket_for(width: float) -> str:
    for name, low, high in SIZE_BUCKETS:
        if low <= width < high:
            return name
    return SIZE_BUCKETS[-1][0]


def distribution(values: list[float]) -> dict:
    """Mean / p50 / p95 / p99 / max, or just a count when there is nothing to summarise."""
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def quantile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]

    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 5),
        "p50": round(quantile(0.5), 5),
        "p95": round(quantile(0.95), 5),
        "p99": round(quantile(0.99), 5),
        "max": round(ordered[-1], 5),
    }


class Rig:
    """The models and the arms, loaded once and reused across frames."""

    def __init__(self, ceiling: int):
        import cv2
        import numpy as np

        import detector_640
        import face_detector
        import face_onnx

        cv2.setNumThreads(1)
        ok, reason = face_detector.available()
        if not ok:
            raise Unanswerable(f"no detector: {reason}")
        self.cv2, self.np = cv2, np
        self.face_detector = face_detector
        self.detector_640 = detector_640
        self.ceiling = ceiling
        #: Control detectors keyed by the frame's own long edge. The control must run at the
        #: frame's resolution, not at the ceiling: a frame smaller than the ceiling would be
        #: *upscaled* into a ceiling-sized letterbox, which changes the subject scale and so is
        #: no longer a control for anything.
        self.control_cache: dict[int, object] = {}
        self.specs = arm_specs(ceiling)
        self.tiles = {spec["name"]: spec["tiles"] for spec in self.specs}
        self.detectors = {
            spec["name"]: detector_640.YuNetDetector(
                str(face_detector.model_path()), input_size=spec["input_size"]
            )
            for spec in self.specs
            if spec["input_size"] is not None
        }
        self.engine = face_onnx.get_engine()
        # Size the embedding graph off the measured loop, so its one-time allocation is not
        # charged to whichever frame happened to be first.
        self.engine.embed(np.zeros((112, 112, 3), dtype=np.uint8))

    def compare(self, frame, label: str, hint: float | None) -> dict | None:
        """One frame through every arm, against production's own answer.

        Returns ``None`` when production cannot decide the frame (no subject, or more than one):
        only a frame production can decide is a crop a cap can be held against.
        """
        face_detector = self.face_detector
        native_rows = usable_rows(face_detector, frame)
        if len(native_rows) != 1:
            return None
        native_row = native_rows[0]
        width = float(min(native_row[2], native_row[3]))
        native_vector = self.engine.embed(face_detector.align(frame, native_row))

        out = {
            "label": label,
            "width": width if hint is None else hint,
            "measured_width": width,
            "arms": {},
        }
        for name, detector in self.detectors.items():
            record = _blank()
            out["arms"][name] = record
            try:
                found = self._detector_for(name, frame, detector).detect(
                    frame, tiles=self.tiles[name]
                )
            except Exception as exc:  # noqa: BLE001 - an arm that crashes is a finding
                record["errors"].append(f"{type(exc).__name__}: {exc}")
                continue
            usable = [d for d in found if d.size() >= face_detector.MIN_SUBJECT_PX]
            if not usable:
                record["found_none"] += 1
                continue
            if len(usable) != 1:
                record["found_many"] += 1
                continue
            record["found_one"] += 1
            arm_row = row_from_detection(usable[0])
            iou, _containment = face_detector._overlap(
                tuple(float(v) for v in native_row[:4]), tuple(usable[0].box)
            )
            drift = cosine_distance(
                native_vector, self.engine.embed(face_detector.align(frame, arm_row)), self.np
            )
            record["iou"].append(iou)
            record["drift"].append(drift)
        return out


    def _detector_for(self, name: str, frame, default):
        """The arm's detector for this frame. Only the control is frame-size dependent."""
        if name != "control":
            return default
        long_edge = max(frame.shape[1], frame.shape[0])
        cached = self.control_cache.get(long_edge)
        if cached is None:
            cached = self.detector_640.YuNetDetector(
                str(self.face_detector.model_path()), input_size=long_edge
            )
            self.control_cache[long_edge] = cached
        return cached


def _blank() -> dict:
    return {"found_one": 0, "found_none": 0, "found_many": 0, "iou": [], "drift": [], "errors": []}


def merge(totals: dict, per_frame: dict) -> None:
    """Fold one frame's arm records into the running totals."""
    for name, record in per_frame["arms"].items():
        target = totals.setdefault(name, _blank())
        for key in ("found_one", "found_none", "found_many"):
            target[key] += record[key]
        target["iou"].extend(record["iou"])
        target["drift"].extend(record["drift"])
        target["errors"].extend(record["errors"])
        if name == "cap640x2":
            bucket = totals.setdefault("by_bucket", {})
            entry = bucket.setdefault(bucket_for(per_frame["width"]), {"iou": [], "drift": []})
            entry["iou"].extend(record["iou"])
            entry["drift"].extend(record["drift"])


def summarise(totals: dict, band: dict) -> dict:
    arms = {}
    worst = 0.0
    for name, record in totals.items():
        if name == "by_bucket":
            continue
        arms[name] = {
            "found_one": record["found_one"],
            "found_none": record["found_none"],
            "found_many": record["found_many"],
            "iou": distribution(record["iou"]),
            "drift": distribution(record["drift"]),
        }
        if record["errors"]:
            arms[name]["errors"] = sorted(set(record["errors"]))[:5]
        if name != "native" and record["drift"]:
            worst = max(worst, max(record["drift"]))
    buckets = {
        name: {"iou": distribution(v["iou"]), "drift": distribution(v["drift"])}
        for name, v in sorted(totals.get("by_bucket", {}).items())
    }
    headroom = band["headroom"]
    return {
        "arms": arms,
        "cap640x2_by_bucket": buckets,
        "worst_drift": round(worst, 5),
        "headroom_used": round(worst / headroom, 3) if headroom else None,
    }


def band_facts(face_detector) -> dict:
    band = face_detector.active_band()
    approve, review = band.derived()
    return {
        "pipeline": face_detector.active_pipeline(),
        "genuine_ceiling": band.genuine_ceiling,
        "impostor_floor": band.impostor_floor,
        "approve": approve,
        "review": review,
        # The cosine between the worst genuine pair the band was measured on and the line a punch
        # is actually decided by. Stored templates were made through the native pass, so a crop
        # change spends this directly.
        "headroom": round(review - band.genuine_ceiling, 5),
    }


def synth_frames(rig: Rig, photos: list[Path], targets: tuple[int, ...]):
    """Far-range frames built from real faces: each photo scaled so its face hits ``target``.

    The whole *scene* is scaled, not just the face, so the face keeps its own local background and
    JPEG history. The canvas is a blurred version of the same photo, which is featureless enough
    not to produce detections of its own - but that is also the honesty limit: a real distant scene
    has clutter the native pass and the cap would both have to work through, so a face found here
    is a ceiling on recall rather than an estimate of it.
    """
    cv2 = rig.cv2
    height, width = rig.ceiling, rig.ceiling * 3 // 4
    frames = []
    for path in photos:
        source = cv2.imread(str(path))
        if source is None:
            continue
        rows = usable_rows(rig.face_detector, source)
        if len(rows) != 1:
            continue
        face_width = float(rows[0][2])
        for target in targets:
            scale = target / max(1.0, face_width)
            tile = cv2.resize(
                source,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR,
            )
            tile_height, tile_width = tile.shape[:2]
            if tile_height > height or tile_width > width:
                continue  # an upscaled target the source cannot hold at this ceiling
            small = cv2.resize(source, (64, 48), interpolation=cv2.INTER_AREA)
            canvas = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)
            canvas = cv2.GaussianBlur(canvas, (0, 0), 6)
            top, left = (height - tile_height) // 2, (width - tile_width) // 2
            canvas[top : top + tile_height, left : left + tile_width] = tile
            frames.append((f"face{target}px", canvas, float(target)))
    return frames


def measure_corpus(corpus: Path, count: int, ceiling: int) -> dict:
    rig = Rig(ceiling)
    frames = sample_frames(corpus, count)
    totals: dict = {}
    with_subject = 0
    sizes: list[float] = []
    frame_shapes: dict[str, int] = {}
    for path in frames:
        frame = rig.cv2.imread(str(path))
        if frame is None or frame.size == 0:
            continue
        shape = f"{frame.shape[1]}x{frame.shape[0]}"
        frame_shapes[shape] = frame_shapes.get(shape, 0) + 1
        per_frame = rig.compare(frame, "corpus", None)
        if per_frame is None:
            continue
        with_subject += 1
        sizes.append(per_frame["measured_width"])
        merge(totals, per_frame)
    if with_subject == 0:
        raise Unanswerable(
            "no sampled frame had exactly one subject - this corpus cannot answer the question "
            "(synthetic 240x240 frames have none; a real deployment's punch_frames do)"
        )
    facts = band_facts(rig.face_detector)
    return {
        "mode": "corpus",
        "source": str(corpus),
        "sampled": len(frames),
        "with_one_subject": with_subject,
        "frame_shapes": frame_shapes,
        "subject_width": {
            "p05": round(sorted(sizes)[int(0.05 * (len(sizes) - 1))], 1),
            "p50": round(statistics.median(sizes), 1),
            "p95": round(sorted(sizes)[int(0.95 * (len(sizes) - 1))], 1),
        },
        "band": facts,
        **summarise(totals, facts),
    }


def measure_synth(photos_dir: Path, targets: tuple[int, ...], ceiling: int) -> dict:
    rig = Rig(ceiling)
    photos = image_paths(photos_dir)
    frames = synth_frames(rig, photos, targets)
    if not frames:
        raise Unanswerable(f"no frame could be synthesized from {photos_dir}")
    totals: dict = {}
    with_subject = 0
    by_target: dict[str, dict] = {}
    for label, frame, target in frames:
        entry = by_target.setdefault(
            label,
            {"tried": 0, "native_one": 0, "cap_one": 0, "drift": [], "iou": []},
        )
        entry["tried"] += 1
        per_frame = rig.compare(frame, label, target)
        if per_frame is None:
            continue
        with_subject += 1
        entry["native_one"] += 1
        caps = per_frame["arms"].get("cap640x2", _blank())
        if caps["found_one"]:
            entry["cap_one"] += 1
            entry["drift"].extend(caps["drift"])
            entry["iou"].extend(caps["iou"])
        merge(totals, per_frame)
    if with_subject == 0:
        raise Unanswerable("the native pass found no face in any synthesized frame")
    facts = band_facts(rig.face_detector)
    return {
        "mode": "synth",
        "source": str(photos_dir),
        "photos": len(photos),
        "frames": len(frames),
        "with_one_subject": with_subject,
        "band": facts,
        "by_target": {
            label: {
                "tried": v["tried"],
                "native_sees_one": v["native_one"],
                "cap640x2_sees_one": v["cap_one"],
                "iou": distribution(v["iou"]),
                "drift": distribution(v["drift"]),
            }
            for label, v in sorted(
                by_target.items(), key=lambda kv: int(kv[0].split("face")[1][:-2])
            )
        },
        **summarise(totals, facts),
    }


def render(report: dict) -> None:
    band = report["band"]
    print(f"mode              : {report['mode']}  ({report['source']})")
    if report["mode"] == "corpus":
        print(
            f"sampled           : {report['sampled']} frames, "
            f"{report['with_one_subject']} with one subject"
        )
        print(f"frame shapes      : {report['frame_shapes']}")
        print(f"subject width px  : {report['subject_width']}")
    else:
        print(f"photos            : {report['photos']}  frames {report['frames']}")
    print(
        f"band              : {band['pipeline']}  ceiling {band['genuine_ceiling']:.4f}  "
        f"line {band['review']:.4f}  headroom {band['headroom']:.4f}"
    )
    print()
    for name, arm in report["arms"].items():
        drift, iou = arm["drift"], arm["iou"]
        print(
            f"{name:<9} found {arm['found_one']:>4}  missed {arm['found_none']:>4}  "
            f"many {arm['found_many']:>3}   IoU p50 {iou.get('p50', 0):.3f}  "
            f"drift p50 {drift.get('p50', 0):.4f}  p95 {drift.get('p95', 0):.4f}  "
            f"max {drift.get('max', 0):.4f}"
        )
    if report["mode"] == "synth":
        print()
        print("cap640x2 by face width (native sees one / cap sees one -> drift):")
        for label, values in report["by_target"].items():
            drift = values["drift"]
            print(
                f"  {label:<10} n {values['tried']:>3}  native {values['native_sees_one']:>3}  "
                f"cap {values['cap640x2_sees_one']:>3}  "
                f"IoU p50 {values['iou'].get('p50', 0):.3f}  drift p50 {drift.get('p50', 0):.4f}"
            )
    else:
        print()
        print("cap640x2 by face width:")
        for bucket, values in report["cap640x2_by_bucket"].items():
            drift = values["drift"]
            print(
                f"  {bucket:<10} n {drift.get('n', 0):>4}  IoU p50 {values['iou'].get('p50', 0):.3f}  "
                f"drift p50 {drift.get('p50', 0):.4f}  p99 {drift.get('p99', 0):.4f}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether a capped working size preserves the crop the native pass makes."
    )
    parser.add_argument("--mode", choices=("corpus", "synth"), default="corpus")
    parser.add_argument("--corpus", default=None, help="folder of gate frames (default: punch_frames)")
    parser.add_argument("--synth-from", default=None, help="folder of real faces (default: worker_photos)")
    parser.add_argument("--sample", type=int, default=200, help="frames to measure (default 200)")
    parser.add_argument("--ceiling", type=int, default=None,
                        help="input size for the control arm (default: the frame ceiling)")
    parser.add_argument("--json", default=None, help="write the full report here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    import config
    import face_detector

    ceiling = args.ceiling or face_detector.frame_ceiling()
    try:
        if args.mode == "corpus":
            source = Path(args.corpus or config.settings.punch_frames_dir)
            report = measure_corpus(source, args.sample, ceiling)
        else:
            source = Path(args.synth_from or config.settings.worker_photos_dir)
            report = measure_synth(source, SYNTH_TARGETS, ceiling)
    except Unanswerable as exc:
        print(f"cannot answer on this machine: {exc}")
        return 2

    if not args.quiet:
        render(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")

    worst, headroom = report["worst_drift"], report["band"]["headroom"]
    lost = sum(
        arm["found_none"] + arm["found_many"]
        for name, arm in report["arms"].items()
        if name != "native"
    )
    print()
    if lost:
        print(
            f"NOTE: {lost} arm-frame(s) did not see exactly one subject where production did - "
            "a cap costs punches as well as accuracy."
        )
    if worst >= headroom:
        print(
            f"FAIL: the worst crop drift ({worst:.4f}) reaches the band's headroom ({headroom:.4f}) - "
            "templates enrolled through the native pass would start being refused."
        )
        return 1
    print(
        f"OK: worst crop drift {worst:.4f} against {headroom:.4f} of headroom "
        f"({report['headroom_used']:.0%} consumed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
