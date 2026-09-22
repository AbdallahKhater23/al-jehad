"""Measure this build's decision lines, from labelled pairs, through the real pipeline.

WHY THIS IS A TOOL AND NOT A CONSTANT
-------------------------------------
A distance has no meaning on its own. 0.31 is ``approved`` or ``refused`` only against a
line, and that line is only meaningful for the *crop* and *model* that produced both vectors
being compared. This build embeds FaceNet-128 through ONNX Runtime (``face_onnx``) from a
112x112 YuNet-aligned crop (``face_detector``). The two bands this project shipped before
were measured in a different space entirely - VGG-Face, 4096 dimensions, through DeepFace's
own preprocessing - so they were removed rather than carried over, and ``face_detector.BANDS``
is empty until somebody runs this.

Two boundaries are measurable, and they are the only two this tool computes:

* the **genuine ceiling** - the *highest* cosine distance between two captures of the same
  person. The approve line has to clear this, or a real worker is sent to review every day.
* the **impostor floor** - the *lowest* cosine distance between two different people. The
  refuse line has to stay below this, or the wrong person is let in.

Both are extremes of a sample, not the true boundaries of a distribution, so each line is
placed at a stated distance from the measurement standing for it, by the rule that already
lives in ``face_detector``: the approve line at the geometric centre of the measured window,
the review line ``IMPOSTOR_MARGIN`` below the impostor floor, both rounded to the granularity
a decision is actually made at. ``MatchBand.derived()`` is that rule, and this tool calls it
rather than re-implementing it - so the numbers printed here are, by construction, the numbers
the project's own test suite will recompute from the evidence string.

THE FOLDER CONTRACT
-------------------
Identity is a directory: one folder per person, every image in it a capture of that person.

    corpus/
      alice/    1.jpg  2.jpg  3.jpg
      bob/      1.jpg  2.jpg
      carol/    1.jpg

A flat folder also works when the identity is in the filename before a trailing number -
``alice_1.jpg``, ``alice_2.jpg``, ``bob-1.jpg`` - which is what a phone export usually looks
like. Subdirectories win when both are present. Every image is run through the application's
own detector and the application's own engine; nothing here reimplements the crop, because a
band measured through a *copy* of the pipeline is a band for that copy.

IT REFUSES RATHER THAN GUESSES
------------------------------
This exits non-zero, printing what is missing, when the corpus cannot support a line:

* no genuine pairs (one photograph per person) - a ceiling cannot be measured at all;
* fewer than ``--min-impostor-pairs`` impostor pairs - the floor would be an anecdote;
* the two distributions overlapping, so no line separates them - which is a statement about
  the corpus or the pipeline, and either way needs a human, not a number;
* ``--pipeline`` naming any crop but the one this build really runs, because every embedding
  here is produced by the live detector (see that flag): a band for another crop cannot be
  measured through this tool, only mislabelled.

Refusing is the point. A band written from a corpus that cannot support one is exactly the
defect ``face_detector.BANDS`` was emptied to avoid: thresholds that look authoritative and
were never measured.

USAGE
-----
    venv/Scripts/python.exe tools/derive_facenet_band.py --corpus ./calibration
    venv/Scripts/python.exe tools/derive_facenet_band.py --corpus ./calibration --json band.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

#: Run as a script, this file is ``sys.path[0]`` - so the application modules beside it are
#: not importable until the backend directory is added. ``face_detector``, ``face_onnx`` and
#: ``face_engine`` are imported lazily inside the functions below (so ``--help`` costs nothing),
#: which means this bootstrap only has to be in place before one of them runs.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

#: Image extensions worth attempting. Anything else in the folder is ignored rather than
#: reported as an error - a corpus folder accumulates `.DS_Store` and thumbnails.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

#: Identities with fewer than this many usable captures cannot contribute a genuine pair.
MIN_CAPTURES_PER_IDENTITY = 2

#: The smallest detected face, in **source pixels** on its shorter side, this tool will embed.
#: ``None`` means the engine's own input size (``face_onnx.INPUT_SIZE``, 160), which is the
#: principled floor rather than a tuned one: the engine resizes every crop to 160x160, so a face
#: box smaller than that is *upscaled* - the vector is an embedding of interpolated pixels, not
#: of a face. On a real corpus this is not a corner case: a wide shot detects a 33x43 pixel face
#: in a 960x640 frame, the engine expands it fivefold, and two captures of the same person then
#: measure 0.79 apart - enough to poison a band on its own. The deployment's own captures are
#: selfies at the gate, where the face is large, so refusing the tiny crops makes the calibration
#: resemble what it is calibrating. Override with ``--min-face-pixels`` (0 accepts everything).
MIN_FACE_PIXELS_DEFAULT: int | None = None

#: The floor for impostor pairs before a measurement is allowed to be called a floor. The
#: shipped VGG band rested on 495; this is the point below which the number is anecdote, and
#: the tool says so instead of printing it. Override with ``--min-impostor-pairs``.
DEFAULT_MIN_IMPOSTOR_PAIRS = 100

#: Reported when the corpus is real but thin. Above the refusal floor and below this, the
#: output is still produced and carries a warning, because a small clean corpus is a
#: legitimate starting point - it just must not be mistaken for a settled one.
COMFORTABLE_IMPOSTOR_PAIRS = 300


def cosine_distance(a, b) -> float:
    """The application's own comparison, so a number here means a number there.

    ``main.compare_faces_sync`` scores punches with ``scipy.spatial.distance.cosine``; using
    a different formula here would derive lines for a metric the application does not use.
    """
    from scipy.spatial.distance import cosine

    return float(cosine(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)))


def identity_of(path: Path, corpus: Path) -> str:
    """Which person this file belongs to.

    A subdirectory is the identity (``corpus/alice/3.jpg`` -> ``alice``). A flat file is split
    on its trailing capture number (``alice_2.jpg`` -> ``alice``); a flat file with no trailing
    number is its own identity, which makes a folder of one-off portraits a valid impostor set
    with no genuine pairs - and the refusal below then says exactly that.
    """
    relative = path.relative_to(corpus)
    if len(relative.parts) > 1:
        return relative.parts[0]

    stem = path.stem
    for separator in ("_", "-", " "):
        head, _, tail = stem.rpartition(separator)
        if head and tail.isdigit():
            return head
    return stem


def collect(corpus: Path) -> dict[str, list[Path]]:
    """``{identity: [images]}`` for everything under ``corpus``. Sorted, so runs compare."""
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(corpus.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            grouped[identity_of(path, corpus)].append(path)
    return dict(grouped)


#: How much of a face's size to infer from its landmarks when a stored crop arrives without a box:
#: the landmark span (outer eye corners to outer mouth corners) times this. Only used for the
#: engine-floor check, and named so a reviewer can see where the number came from - a crop whose
#: provenance records no box is still usable, but it must not be described as a measured box.
BOX_FROM_LANDMARKS_MARGIN = 1.6


def _row_from_sidecar(stored: dict) -> np.ndarray:
    """A YuNet-shaped row built from a stored crop, so ``face_detector.align`` does the cropping.

    Rebuilding the row rather than cropping here is the whole point: this tool derives lines for the
    *live pipeline's* crop, and a band measured through a second implementation of that crop is a band
    for the second implementation. The aligner's input is a row in YuNet's layout, so that is what a
    stored capture has to be turned back into.
    """
    points = np.asarray(stored["landmarks"], dtype=np.float32).reshape(5, 2)
    box = stored.get("box")
    if not box:
        low, high = points.min(axis=0), points.max(axis=0)
        centre, half = (low + high) / 2.0, (high - low) * BOX_FROM_LANDMARKS_MARGIN / 2.0
        box = [centre[0] - half[0], centre[1] - half[1], half[0] * 2.0, half[1] * 2.0]
    return np.concatenate(
        [np.asarray(box, dtype=np.float32), points.reshape(-1),
         np.asarray([float(stored.get("score") or 1.0)], dtype=np.float32)]
    )


def _stored_short_side(stored: dict) -> float:
    """The face's shorter side in the stored image - the number the engine floor is about.

    From the stored box when there is one, otherwise from the landmarks via the stated margin. The
    *stored* size, not ``native_face_px``: the pixels this tool will embed are the file's, and a
    capture downscaled by ``corpus`` is genuinely a smaller face than the frame it came from - which
    is the regime the floor exists to exclude.
    """
    box = stored.get("box")
    if box:
        return float(min(box[2], box[3]))
    return float(stored.get("face_px") or 0.0)


def embed_all(
    grouped: dict[str, list[Path]], *, min_face_pixels: int = 0,
    landmarks: str = "auto", expected: dict | None = None,
) -> tuple[dict[str, list[np.ndarray]], list[str], dict[str, int]]:
    """One embedding per usable image, through the application's own detector and engine.

    A frame the detector cannot find a face in is **skipped and reported**, not embedded: an
    embedding of a whole photograph with no face in it is a vector about a wall, and letting
    one into the impostor set would move the floor with a number about nothing.

    ``min_face_pixels`` is the same refusal for a face that is *too small*: below the engine's
    input size the crop is upscaled, so the vector describes interpolated pixels rather than a
    face. See ``MIN_FACE_PIXELS_DEFAULT`` - the exclusion is reported per image, like every
    other skip, so the corpus a band came from is always auditable from its own output.

    ``landmarks`` decides where the crop comes from, exactly as it does in ``tools/contract_ab.py``:
    ``auto`` prefers a stored capture's own points (which is what makes a corpus of real traffic
    reproducible months later), ``stored`` requires one, ``detect`` ignores them. A stored crop
    measured with a *different* detector configuration is a refusal, not a fallback: re-detecting
    quietly would put this pipeline's name on another pipeline's crops.
    """
    import cv2

    import corpus as corpus_store
    import face_detector
    import face_onnx

    engine = face_onnx.get_engine()
    vectors: dict[str, list[np.ndarray]] = {}
    skipped: list[str] = []
    sources = {"stored": 0, "detected": 0}

    for identity, paths in sorted(grouped.items()):
        kept: list[np.ndarray] = []
        for path in paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                skipped.append(f"{path} (unreadable)")
                continue
            stored = None
            if landmarks != "detect":
                stored = corpus_store.landmarks_for(path, expect=expected)
                if stored is None and landmarks == "stored":
                    skipped.append(
                        f"{path} (no stored landmarks, and --landmarks stored requires them)"
                    )
                    continue
            if stored is not None:
                crop = face_detector.align(image, _row_from_sidecar(stored))
                short_side = _stored_short_side(stored)
                sources["stored"] += 1
            else:
                faces = face_detector.detect_and_align(image)
                if len(faces) != 1:
                    # More than one face is as unusable as none: the band is about two people's
                    # *own* captures, and picking one box out of several would make the crop a
                    # choice this tool made rather than something the pipeline did.
                    skipped.append(f"{path} ({len(faces)} faces detected, need exactly 1)")
                    continue
                area = faces[0].get("facial_area") or {}
                short_side = min(int(area.get("w") or 0), int(area.get("h") or 0))
                crop = faces[0]["face"]
                sources["detected"] += 1
            if short_side < min_face_pixels:
                skipped.append(
                    f"{path} (face is {short_side:.0f}px on its shorter side, below the "
                    f"{min_face_pixels}px floor: the crop would be upscaled)"
                )
                continue
            kept.append(engine.embed(crop))
        if kept:
            vectors[identity] = kept
    return vectors, skipped, sources


def pairwise(vectors: dict[str, list[np.ndarray]]) -> tuple[list[float], list[float]]:
    """``(genuine distances, impostor distances)`` over every comparable pair."""
    genuine: list[float] = []
    impostor: list[float] = []

    for identity, items in sorted(vectors.items()):
        if len(items) >= MIN_CAPTURES_PER_IDENTITY:
            for left, right in combinations(range(len(items)), 2):
                genuine.append(cosine_distance(items[left], items[right]))

    identities = sorted(vectors)
    for first, second in combinations(identities, 2):
        for left in vectors[first]:
            for right in vectors[second]:
                impostor.append(cosine_distance(left, right))

    return genuine, impostor


def summarise(values: list[float]) -> dict:
    """A distribution as an operator reads it: extremes first, then shape."""
    if not values:
        return {"count": 0}
    array = np.asarray(values, dtype=np.float64)
    return {
        "count": len(values),
        "min": float(array.min()),
        "max": float(array.max()),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "p05": float(np.percentile(array, 5)),
        "p95": float(np.percentile(array, 95)),
    }


def build_band(model: str, genuine: list[float], impostor: list[float], corpus: Path,
               extras: dict) -> tuple[object, dict]:
    """The ``MatchBand`` the measurements imply, computed by ``MatchBand.derived()``.

    The rule is not restated here on purpose. ``face_detector.MatchBand.derived()`` is the
    authority - a project test recomputes the shipped lines from their evidence - so calling it
    is what makes a pasted band consistent with the suite that will police it.
    """
    import face_detector

    ceiling = max(genuine)
    floor = min(impostor)
    evidence = (
        f"Measured {MODEL_CLAUSE} on {len(genuine)} same-person pair(s) across "
        f"{extras['identities']} identities and {len(impostor)} different-people pair(s), all "
        f"through this pipeline's own detector and ONNX engine, cosine distance as "
        f"``compare_faces_sync`` computes it. Corpus: {corpus.name} "
        f"({extras['images']} image(s), {extras['skipped']} skipped; faces smaller than "
        f"{extras['min_face_pixels']}px on their shorter side excluded, because the engine "
        f"upscales its crop to 160px and a smaller face would be embedded as interpolation). "
        f"Genuine {min(genuine):.3f}-{ceiling:.3f}; impostor {floor:.3f}-{max(impostor):.3f}. "
        f"Floors are extremes of a sample, so the lines sit at a stated distance from them "
        f"(see IMPOSTOR_MARGIN and LINE_GRANULARITY)."
    )
    band = face_detector.MatchBand(
        model=model,
        approve=0.0,          # replaced below by the rule's own output, never hand-typed
        review=0.0,
        genuine_ceiling=round(ceiling, 4),
        impostor_floor=round(floor, 4),
        evidence=evidence,
    )
    approve, review = band.derived()
    # Rebuilt rather than mutated: ``MatchBand`` is frozen, which is the property that stops a
    # line being edited without editing the measurement beside it.
    band = face_detector.MatchBand(
        model=model,
        approve=approve,
        review=review,
        genuine_ceiling=band.genuine_ceiling,
        impostor_floor=band.impostor_floor,
        evidence=evidence,
    )
    if band.derived() != (approve, review):  # pragma: no cover - defends the line above
        raise RuntimeError("the band's own rule disagrees with the lines this tool wrote")
    return band, {
        "genuine_ceiling": band.genuine_ceiling,
        "impostor_floor": band.impostor_floor,
        "approve": approve,
        "review": review,
        "basis": band.basis(),
    }


MODEL_CLAUSE = "in this build's own vector space"


def render(band, pipeline: str, model: str) -> str:
    """The paste-ready block. Formatted the way ``face_detector.BANDS`` is written."""
    evidence = band.evidence.replace('"', "'")
    return (
        f'    "{pipeline}": MatchBand(\n'
        f'        model="{model}",\n'
        f'        approve={band.approve},\n'
        f'        review={band.review},\n'
        f'        genuine_ceiling={band.genuine_ceiling},\n'
        f'        impostor_floor={band.impostor_floor},\n'
        f'        evidence=(\n'
        f'            "{evidence}"\n'
        f'        ),\n'
        f'    ),'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Derive face_detector.BANDS for the live pipeline from a labelled corpus. "
            "Refuses, rather than guessing, when the corpus cannot support a line."
        )
    )
    parser.add_argument("--corpus", required=True,
                        help="folder of calibration images; one subfolder per person, or "
                             "'name_1.jpg' style filenames")
    parser.add_argument("--pipeline", default=None,
                        help="pipeline key to emit; must be the pipeline this build would really "
                             "run (default: face_detector.active_pipeline()), because every "
                             "embedding below is produced by that pipeline's own detector")
    parser.add_argument("--min-impostor-pairs", type=int, default=DEFAULT_MIN_IMPOSTOR_PAIRS)
    parser.add_argument(
        "--landmarks", default="auto", choices=("auto", "stored", "detect"),
        help="where each crop comes from: a stored capture's own landmarks when they match this "
             "pipeline (auto, the default - this is what makes a corpus of real traffic "
             "reproducible), a stored capture or a refusal (stored), or a fresh detection pass "
             "(detect)",
    )
    parser.add_argument(
        "--min-face-pixels", type=int, default=MIN_FACE_PIXELS_DEFAULT,
        help="smallest accepted face, in source pixels on its shorter side (default: the "
             "engine's 160px input size; 0 disables the floor)",
    )
    parser.add_argument("--json", default=None, help="write the raw measurements here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"corpus folder not found: {corpus}", file=sys.stderr)
        return 2

    import face_detector
    import face_engine
    import face_onnx

    pipeline = args.pipeline or face_detector.active_pipeline()
    model = face_engine.FACE_MODEL
    # The name on a band is a claim about which *crop* produced the vectors under it, and this
    # tool has exactly one path to a vector: ``face_detector``'s own detector, which is the live
    # pipeline's. So a run labelled with any other pipeline's name would emit a measurement taken
    # on the live crop, behind another crop's name - and nothing downstream can tell, because the
    # numbers look authoritative and the crop they came from is not in them. That is precisely the
    # inheritance ``face_detector.BANDS`` was emptied to end (a VGG band still sitting on a FaceNet
    # table), and it is a live temptation: the fallback pipeline is named in ``active()`` beside
    # the live one, so ``--pipeline mtcnn`` looks like the way to cover it. It is not, until this
    # tool can force that detector - so it refuses rather than guessing.
    live = face_detector.active_pipeline()
    if pipeline != live:
        print(
            f"--pipeline {pipeline!r} names a crop this build cannot embed through. Every "
            f"embedding below is produced by the live detector, which is {live!r}'s, so a band "
            f"labelled {pipeline!r} would be one crop's numbers under another crop's name: "
            f"unfalsifiable after the fact, and applied to every punch scored on that crop. "
            f"Measure {live!r} (the pipeline this build really runs), or give this tool a way "
            "to force the other detector first.",
            file=sys.stderr,
        )
        return 2
    # ``None`` means "the engine's own input size", resolved here rather than in the default so
    # the flag and the engine cannot drift apart.
    min_face_pixels = max(0, int(face_onnx.INPUT_SIZE if args.min_face_pixels is None
                                else args.min_face_pixels))

    grouped = collect(corpus)
    images = sum(len(paths) for paths in grouped.values())
    if not grouped:
        print(f"no images under {corpus} (looked for {sorted(IMAGE_SUFFIXES)})", file=sys.stderr)
        return 2

    # What this run believes it is cropping with. Built from the *live* detector, because that is the
    # crop these lines are for; a corpus captured at another configuration refuses rather than being
    # measured under a name it was not measured under.
    import corpus as corpus_store

    expected = None
    if args.landmarks != "detect":
        expected = corpus_store.detector_fingerprint(
            kind=face_detector.DETECTOR_NAME,
            model_fingerprint=face_detector.fingerprint(),
            pipeline=face_detector.active_pipeline(),
            input_size=face_detector.DETECTOR_INPUT_SIZE,
        )
    try:
        vectors, skipped, crop_sources = embed_all(
            grouped, min_face_pixels=min_face_pixels,
            landmarks=args.landmarks, expected=expected,
        )
    except corpus_store.CorpusError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    genuine, impostor = pairwise(vectors)
    identities = len(vectors)

    if not args.quiet:
        print(f"corpus        : {corpus}")
        print(f"images        : {images} found, {images - len(skipped)} embedded, "
              f"{len(skipped)} skipped")
        print(f"identities    : {identities} usable of {len(grouped)} "
              f"({len(grouped) - identities} had no usable capture)")
        print(f"pipeline/model: {pipeline} / {model}")
        print(f"face floor    : {min_face_pixels}px on the shorter side (0 = no floor)")
        print(f"crops from    : {crop_sources['stored']} stored sidecar(s), "
              f"{crop_sources['detected']} fresh detection(s)")
        if skipped and not args.quiet:
            print("skipped       :")
            for entry in skipped[:10]:
                print(f"  - {entry}")
            if len(skipped) > 10:
                print(f"  ... and {len(skipped) - 10} more")
        print()

    report = {
        "corpus": str(corpus),
        "pipeline": pipeline,
        "model": model,
        "images_found": images,
        "images_skipped": skipped,
        "identities": identities,
        "min_face_pixels": min_face_pixels,
        "crop_source": {**crop_sources, "mode": args.landmarks},
        "genuine": summarise(genuine),
        "impostor": summarise(impostor),
    }

    # -- refusals ---------------------------------------------------------
    problems = []
    #: True when the measured window separates but is too narrow for two lines. Not a refusal -
    #: ``MatchBand.derived`` clamps, so the band that comes out is a legitimate one-line band -
    #: but the operator has to be told, because it means the review queue receives no captures.
    one_line = False
    if not genuine:
        problems.append(
            f"no same-person pairs: {identities} identity(ies) with at least "
            f"{MIN_CAPTURES_PER_IDENTITY} usable captures are needed to measure a genuine "
            "ceiling (one photograph per person cannot produce one)"
        )
    if len(impostor) < args.min_impostor_pairs:
        problems.append(
            f"only {len(impostor)} different-people pair(s); at least "
            f"{args.min_impostor_pairs} are required for the floor to be a measurement "
            "rather than an anecdote (--min-impostor-pairs)"
        )
    if genuine and max(genuine) <= 0.0:
        problems.append(
            "the highest same-person distance is 0.0000, which is what two byte-identical "
            "captures measure - the identities in this corpus are duplicates of one another, "
            "not two photographs of one person. A ceiling of zero cannot place an approve "
            "line above it, so replace the duplicated captures with real ones"
        )
    if genuine and impostor:
        ceiling, floor = max(genuine), min(impostor)
        if ceiling >= floor:
            problems.append(
                f"the distributions overlap: the highest same-person distance "
                f"({ceiling:.4f}) is at or above the lowest different-people distance "
                f"({floor:.4f}). No line separates them, so no band can be derived "
                "from this corpus - check the labelling, then the crop"
            )
        else:
            # A window that separates can still be too *narrow* for two lines. The review rule
            # sits ``IMPOSTOR_MARGIN`` below the floor, which lands above the geometric centre
            # only while the floor is at least ``IMPOSTOR_MARGIN ** 2`` times the ceiling; below
            # that, ``MatchBand.derived()`` clamps the review line up to the approve line and the
            # band is a one-line band. That is a legitimate band rather than a malformed one, so
            # it is reported, not refused - but it is worth saying out loud, because a one-line
            # band means the review queue receives no captures at all and nobody would see that
            # from the lines themselves. Computed through the rule, so this can never disagree
            # with the band that actually gets pasted.
            seed = face_detector.MatchBand(
                model=model,
                approve=0.0,
                review=0.0,
                genuine_ceiling=round(ceiling, 4),
                impostor_floor=round(floor, 4),
                evidence="candidate window",
            )
            approve, review = seed.derived()
            one_line = review == approve
    if len(vectors) < 2:
        problems.append("fewer than two identities have usable captures")

    if problems:
        report["refused"] = problems
        print("REFUSING TO DERIVE A BAND", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nNothing was emitted. This is the same refusal ``face_detector.BANDS`` is "
            "making: a threshold that looks authoritative but was never measured is worse "
            "than an empty table, because only one of the two is visible.",
            file=sys.stderr,
        )
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return 2

    if not args.quiet:
        print("genuine (same person)   :", report["genuine"])
        print("impostor (different)    :", report["impostor"])
        print()
        if len(impostor) < COMFORTABLE_IMPOSTOR_PAIRS:
            print(f"WARNING: {len(impostor)} impostor pair(s) is thin. The shipped VGG band "
                  f"rested on 495. Treat these lines as provisional until the corpus grows.")
            print()

    band, derived = build_band(
        model, genuine, impostor, corpus,
        {
            "identities": identities,
            "images": images,
            "skipped": len(skipped),
            "min_face_pixels": min_face_pixels,
        },
    )
    report["band"] = derived
    report["one_line_band"] = one_line
    report["rendered"] = render(band, pipeline, model)

    # The lines are ordered by the refusal above, so a band that reaches this point always has a
    # real review window. Nothing here re-checks it: a second guard that can never fire would
    # only hide the day the first one stops working.
    if not args.quiet:
        if one_line:
            print(f"WARNING: this band has ONE line, not two. The measured window "
                  f"({derived['genuine_ceiling']:.3f}..{derived['impostor_floor']:.3f}) is narrower "
                  f"than IMPOSTOR_MARGIN ({face_detector.IMPOSTOR_MARGIN:g}) can separate, so "
                  f"``derived()`` clamped the review line up to the approve line: captures at or "
                  f"below it are approved, everything above is refused, and NOTHING is routed to "
                  f"the review queue. That is a real configuration, not a malformed band - but if "
                  f"you want borderline captures reviewed by a human, the corpus has to separate "
                  f"more widely (the rule needs the impostor floor at about "
                  f"{face_detector.IMPOSTOR_MARGIN ** 2:.2f}x the genuine ceiling; this corpus "
                  f"measures {derived['impostor_floor'] / derived['genuine_ceiling']:.2f}x).")
            print()
        print(f"genuine ceiling : {derived['genuine_ceiling']}")
        print(f"impostor floor  : {derived['impostor_floor']}")
        print(f"approve line    : {derived['approve']}  (clears the ceiling by "
              f"{derived['approve'] / derived['genuine_ceiling']:.2f}x)")
        print(f"review line     : {derived['review']}  "
              f"(below the floor by {derived['impostor_floor'] / derived['review']:.2f}x)")
        print()
        print("Paste into face_detector.BANDS:")
        print()
        print(report["rendered"])
        print()
        print("Then verify with the project's own checks:")
        print("    python -m pytest tests/test_face_match_bands.py -q")
        print()
        print("What this does not prove: that the corpus resembles the deployment's captures.")
        print("The floor is the *easiest* end of impostors - clean, frontal, evenly lit - so it")
        print("is an upper bound on the real one, which is why IMPOSTOR_MARGIN exists.")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        if not args.quiet:
            print(f"\nraw measurements: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
