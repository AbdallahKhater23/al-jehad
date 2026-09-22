"""Which of the four input contracts does this graph actually want? Measure it, do not assume it.

WHY THIS EXISTS
---------------
A FaceNet graph takes a 160x160 float32 tensor and returns a vector. Almost nothing about that
tensor is in the ONNX file: nothing in it says "RGB" or "BGR", nothing says "scale to [0, 1]" or
"to [-1, 1]", and any exporter that knew would have nowhere to write it. So the contract is a
*convention* between whoever exported the graph and whoever feeds it - and when the two disagree,
the graph still runs. It returns a vector of the right shape, in the right range, and the whole
system reports plausible numbers while nobody can be recognised. This is the worst kind of
defect: no exception, no warning, no smoke, just a separation window that has quietly closed.

This tool treats the contract as an unknown and measures it. Four permutations exist
(``face_align.CONTRACTS``), and the only honest way to rank them is a labelled corpus scored
through every one of them.

THE ONE THING THAT MAKES THIS A CONTROLLED EXPERIMENT
----------------------------------------------------
**The crops are shared.** Detection and alignment run *once* per image; only the tensor mapping
(channel order, value range) differs between the four arms. That is what makes the comparison a
measurement of the contract rather than a measurement of four pipelines: same pixels, same
landmarks, same warp, same graph, same weights - one variable. It also means the arms are
directly comparable pair-by-pair, which is what the shift and rank diagnostics below need.

What it deliberately does *not* vary: the detector and the framing. A crop change moves the
ceiling and the floor for reasons that have nothing to do with the contract, so ``--zoom`` and
``--tiles`` are exposed as separate experiments rather than folded into this one.

A CORPUS OF REAL TRAFFIC CARRIES ITS OWN CROP, AND THIS TOOL RESPECTS IT
------------------------------------------------------------------------
A capture stored by ``corpus`` keeps the landmarks its detector measured, plus that detector's
fingerprint. ``--landmarks auto`` (the default) uses those points instead of re-detecting, which is
what makes a corpus of *real* gate captures reproducible months later: the crop cannot drift with a
detector upgrade, an OpenCV version, or a different machine.

A mismatch is a refusal rather than a quiet re-detect. If the stored crop came from a 320-input YuNet
and this run is configured for 640 with tiling, then the report would carry one detector's name over
another detector's crops - the exact substitution the band tool refuses for pipelines, and the reason
the fingerprint travels with the points at all. The fix is one flag: ``--landmarks detect`` to say "I
mean to measure a new crop on this corpus", after which every number is that new crop's.

WHAT IS REPORTED, AND WHY EACH NUMBER IS THERE
----------------------------------------------
* **separation ratio** ``floor / ceiling`` - the decision statistic, because a threshold lives on
  the *tail*, and the rule the deployment gates on is ``floor/ceiling >= IMPOSTOR_MARGIN^2``
  (1.35^2 = 1.82). A ratio at 1.083 says the two distributions overlap; the ratio is what a
  contract defect destroys.
* **the absolute window** - ``ceiling`` and ``floor`` themselves. A wrong contract typically
  compresses *all* distances toward 1.0 (everything looks dissimilar to everything), which lowers
  the ratio without reordering anybody. Reporting the ratio alone would hide that; reporting both
  makes "the graph is being fed the wrong tensor" legible at a glance.
* **bootstrap CI on the ratio** (identity-level, from ``calibration``) - because a floor is the
  minimum of a sample, and a minimum from 200 pairs is a claim about the 0.5th percentile made
  from 200 observations. The gate is applied to the CI's lower end.
* **shift vs. the incumbent** - the median and p10/p90 change in pairwise distance. A near-constant
  offset across the whole distribution is the signature of a global input transform, and it comes
  with an *implied* ratio ``(floor + shift) / (ceiling + shift)`` that should match the measured
  one. When it does, the entire separation change is explained by one constant and no per-identity
  effect - which is the difference between "this contract is the wrong tensor mapping" and "this
  contract happens to help some people".
* **rank agreement** (Spearman over the same pairs) - the corroboration. Under a global affine
  input change the *ordering* of pairs cannot change much; a low rank agreement would mean the two
  arms are effectively different biometric systems and the shift story is a coincidence.

REFUSALS
--------
It exits non-zero rather than printing a ranking it cannot support:

* a corpus with fewer than two identities, or with genuine pairs on no identity (one capture each) -
  a ceiling cannot be measured;
* a corpus whose images cannot all be cropped - the skip list is printed and, if *every* image is
  skipped, the run stops;
* fewer impostor pairs than ``--min-impostor-pairs``;
* any arm producing a different number of embeddings from the others, which can only mean the
  shared-crop invariant was broken and the comparison is no longer controlled.

USAGE
-----
    venv/Scripts/python.exe tools/contract_ab.py --corpus ./calibration
    venv/Scripts/python.exe tools/contract_ab.py --corpus ./calibration --json contracts.json
    venv/Scripts/python.exe tools/contract_ab.py --corpus ./calibration \
        --model models/facenet512.onnx --tiles 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np

#: Run as a script, this file is ``sys.path[0]``. The application modules are importable only once
#: the backend directory is on the path; ``tools`` is added too so the corpus folder contract is
#: imported from its one definition instead of being retyped here.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _entry in (_TOOLS_DIR, _BACKEND_DIR):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

#: Report order is ``face_align.CONTRACTS``' order, incumbent first - imported, not retyped.
DEFAULT_MODEL_CANDIDATES: Final[tuple[str, ...]] = (
    "models/facenet128.onnx",
    "models/facenet512.onnx",
    "models/facenet.onnx",
)

#: Below this many impostor pairs the floor is an anecdote, not a measurement.
DEFAULT_MIN_IMPOSTOR_PAIRS: Final = 100

#: Reported (not refused) below this: a clean 150-pair corpus is a legitimate first answer that
#: must not be mistaken for a settled one.
COMFORTABLE_IMPOSTOR_PAIRS: Final = 300


# ---------------------------------------------------------------------------
# the prepared corpus - one crop per image, shared by every arm
# ---------------------------------------------------------------------------
@dataclass
class Prepared:
    """One image, cropped once. ``crop`` is BGR uint8: the *only* thing the arms vary on."""

    path: Path
    identity: str
    crop: np.ndarray
    face_px: float
    landmarks: np.ndarray
    used_fallback: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Arm:
    """One contract's measurement: the contract, its distances, and its calibration report."""

    contract: Any
    distances: tuple[np.ndarray, np.ndarray]  # (genuine, impostor) cosine distances
    report: dict[str, Any]
    norm_mean: float
    norm_min: float
    norm_max: float
    tensor_stats: dict[str, float]
    #: sha256 of the first tensor this arm fed the graph. The *proof* that the arms varied at all:
    #: min/max/mean cannot distinguish RGB from BGR - a channel swap moves no summary statistic -
    #: so a report carrying only those three numbers would look identical across two arms whose
    #: inputs were actually different.
    tensor_digest: str = ""
    shift: dict[str, float] = field(default_factory=dict)
    implied_ratio: float | None = None
    rank_agreement: float | None = None

    @property
    def ratio(self) -> float:
        return float(self.report["raw"]["separation_ratio"])

    @property
    def ratio_low(self) -> float | None:
        ci = self.report.get("ratio_ci")
        return float(ci[0]) if ci else None

    @property
    def passes(self) -> bool:
        return bool(self.report.get("passes_margin_on_ci"))


def _ranks(values: np.ndarray) -> np.ndarray:
    """Average ranks, ties shared - the prerequisite for a Spearman that is honest about ties."""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(values.size, dtype=np.float64)
    ranks[order] = np.arange(1, values.size + 1, dtype=np.float64)
    sorted_values = values[order]
    start = 0
    for index in range(1, sorted_values.size + 1):
        if index == sorted_values.size or sorted_values[index] != sorted_values[start]:
            if index - start > 1:
                ranks[order[start:index]] = ranks[order[start:index]].mean()
            start = index
    return ranks


def spearman(first: np.ndarray, second: np.ndarray) -> float:
    """Rank correlation over the same pair set. 1.0 means the arms merely rescaled the distances."""
    if first.size != second.size:
        raise ValueError(f"rank agreement needs equal-length arms, got {first.size} and {second.size}")
    if first.size < 3:
        return math.nan
    a, b = _ranks(first), _ranks(second)
    a = a - a.mean()
    b = b - b.mean()
    denominator = float(np.sqrt((a @ a) * (b @ b)))
    if denominator == 0.0:
        return math.nan
    return float((a @ b) / denominator)


def shift_stats(reference: np.ndarray, other: np.ndarray) -> dict[str, float]:
    """``other - reference`` per pair, summarised. A constant offset is a global input transform."""
    delta = np.asarray(other, dtype=np.float64) - np.asarray(reference, dtype=np.float64)
    return {
        "median": round(float(np.median(delta)), 4),
        "mean": round(float(delta.mean()), 4),
        "p10": round(float(np.percentile(delta, 10)), 4),
        "p90": round(float(np.percentile(delta, 90)), 4),
        "spread": round(float(np.percentile(delta, 90) - np.percentile(delta, 10)), 4),
    }


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------
def prepare(
    grouped: dict[str, list[Path]],
    *,
    detect: Any,
    size: int,
    zoom: float,
    min_face_pixels: int,
    strict_single_face: bool,
    landmarks: str = "auto",
    expected: dict[str, Any] | None = None,
) -> tuple[list[Prepared], list[str], dict[str, int]]:
    """Detect once, align once, per image. Returns ``(prepared, skip_notes, landmark_sources)``.

    The skips are **global** rather than per-arm, and that is load-bearing: an image the detector
    cannot crop in one arm would be missing from that arm's pair set and present in the others',
    so the four ratios would be computed over four different corpora. The comparison would still
    print, and would be meaningless.

    ``landmarks`` decides where the crop comes from - ``auto`` prefers a stored sidecar, ``stored``
    requires one for every image, ``detect`` ignores them. A stored crop whose detector fingerprint
    does not match ``expected`` is a **refusal, not a fallback**: re-detecting silently would mean the
    report says one detector's name and the crops are another's, which is the defect this whole tool
    is built to avoid - so the operator has to say ``--landmarks detect`` to mean it.
    """
    import cv2

    import corpus
    import face_align

    template = face_align.framing_variants(size, zooms=(zoom,))[f"zoom={zoom:.2f}"]
    prepared: list[Prepared] = []
    skipped: list[str] = []
    sources = {"stored": 0, "detected": 0}

    for identity, paths in sorted(grouped.items()):
        for path in paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                skipped.append(f"{path} (unreadable)")
                continue
            stored = None
            if landmarks != "detect":
                stored = corpus.landmarks_for(path, expect=expected)
                if stored is None and landmarks == "stored":
                    skipped.append(f"{path} (no stored landmarks, and --landmarks stored requires them)")
                    continue
            if stored is not None:
                # The stored crop is authoritative: its points are in *this* image's pixels, so the
                # aligner can use them as they are. ``face_px`` is the stored size on purpose - what
                # the tools below will actually see is this file, not the frame it came from.
                points = np.asarray(stored["landmarks"], dtype=np.float32).reshape(5, 2)
                box = stored.get("box")
                shortest = min(box[2], box[3]) if box else stored.get("face_px") or 0.0
                if shortest < min_face_pixels:
                    skipped.append(
                        f"{path} (stored face is {float(shortest):.0f}px on its shorter side, below "
                        f"the {min_face_pixels}px floor)"
                    )
                    continue
                crop = face_align.align_to_size(image, points, size=size, template=template)
                prepared.append(
                    Prepared(
                        path=path,
                        identity=identity,
                        crop=crop,
                        face_px=float(shortest),
                        landmarks=points,
                        used_fallback=False,
                        meta={
                            "source": "stored",
                            "capture_id": stored.get("capture_id"),
                            "native_face_px": stored.get("native_face_px"),
                        },
                    )
                )
                sources["stored"] += 1
                continue
            faces = detect(image)
            if not faces:
                skipped.append(f"{path} (no face detected)")
                continue
            if strict_single_face and len(faces) != 1:
                skipped.append(f"{path} ({len(faces)} faces detected, need exactly 1)")
                continue
            best = max(faces, key=lambda det: det.area())
            if best.size() < min_face_pixels:
                skipped.append(
                    f"{path} (face is {best.size():.0f}px on its shorter side, below the "
                    f"{min_face_pixels}px floor: the crop would be upscaled)"
                )
                continue
            crop, _matrix, fallback = face_align.align_to_size(
                image, best.landmarks, size=size, template=template, return_matrix=True
            )
            prepared.append(
                Prepared(
                    path=path,
                    identity=identity,
                    crop=crop,
                    face_px=float(best.size()),
                    landmarks=best.landmarks,
                    used_fallback=bool(fallback),
                    meta={**best.as_dict(), "source": "detector"},
                )
            )
            sources["detected"] += 1
    return prepared, skipped, sources


def embed_arm(
    prepared: Sequence[Prepared], *, engine: Any, contract: Any
) -> tuple[np.ndarray, dict[str, float], dict[str, float], str]:
    """Embed every prepared crop under one contract.

    ``engine.embed`` returns a unit vector (``facenet_ort`` normalizes host-side), which is the
    point of that module: a raw FaceNet output has a norm near 5.78, and a cosine taken against an
    un-normalized vector is the second half of the separation defect. The norm statistics returned
    here are the evidence that normalization is actually in force.
    """
    import face_align

    vectors: list[np.ndarray] = []
    norms: list[float] = []
    stats: dict[str, float] = {}
    digest = ""
    for item in prepared:
        tensor = face_align.to_tensor(item.crop, contract, size=engine.input_size)
        if not stats:
            stats = face_align.tensor_stats(tensor)
            digest = hashlib.sha256(np.ascontiguousarray(tensor).tobytes()).hexdigest()
        vector = engine.embed(tensor)
        vectors.append(np.asarray(vector, dtype=np.float32).reshape(-1))
        norms.append(float(np.linalg.norm(vector)))
    if not vectors:
        raise RuntimeError("no crops to embed")
    widths = {vector.size for vector in vectors}
    if len(widths) != 1:
        raise RuntimeError(f"the engine returned {sorted(widths)} different widths in one arm")
    return (
        np.stack(vectors),
        {"min": round(min(norms), 5), "max": round(max(norms), 5), "mean": round(float(np.mean(norms)), 5)},
        stats,
        digest,
    )


def _evaluate(calibration: Any, vectors: np.ndarray, labels: Sequence[str], args: Any) -> dict[str, Any]:
    """``calibration.evaluate`` with a refusal printed as a refusal.

    ``derive_band`` raises ``CalibrationError`` when the two distributions leave no room for a line
    - two different people closer than two captures of one - and a traceback out of a diagnostic tool
    reads as "the tool is broken" rather than "this corpus cannot support a band".
    """
    try:
        return calibration.evaluate(vectors, labels, bootstrap=args.bootstrap, seed=args.seed)
    except calibration.CalibrationError as exc:
        print(f"{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def ratio_ci_text(arm: Arm) -> str:
    ci = arm.report.get("ratio_ci")
    if not ci:
        return "--"
    return f"[{ci[0]:.3f}, {ci[1]:.3f}]"


def render_table(arms: Sequence[Arm], *, reference: Arm) -> str:
    header = (
        f"{'contract':<24} {'ceiling':>8} {'floor':>8} {'ratio':>7} {'ratio 95% CI':>18} "
        f"{'EER':>6} {'shift med':>10} {'implied':>8} {'spearman':>9} {'margin':>7}"
    )
    lines = [header, "-" * len(header)]
    for arm in arms:
        eer = arm.report.get("operating", {}).get("eer")
        implied = "--" if arm.implied_ratio is None else f"{arm.implied_ratio:.3f}"
        spearman_text = "--" if arm.rank_agreement is None else f"{arm.rank_agreement:.4f}"
        name = arm.contract.describe()
        marker = "=" if arm is reference else " "
        lines.append(
            f"{marker}{name:<23} {arm.report['raw']['genuine_ceiling']:>8.4f} "
            f"{arm.report['raw']['impostor_floor']:>8.4f} {arm.ratio:>7.3f} {ratio_ci_text(arm):>18} "
            f"{'--' if eer is None else f'{eer:.4f}':>6} "
            f"{arm.shift.get('median', float('nan')):>10.4f} {implied:>8} {spearman_text:>9} "
            f"{'pass' if arm.passes else 'FAIL':>7}"
        )
    return "\n".join(lines)


def verdict(arms: Sequence[Arm], *, required_ratio: float) -> tuple[Arm, str]:
    """The best arm and the sentence explaining it. Ranked on the CI's lower end, not the point
    estimate: a corpus this small can rank two contracts in either order on the raw ratio alone."""
    ranked = sorted(
        arms,
        key=lambda arm: (arm.passes, arm.ratio_low if arm.ratio_low is not None else arm.ratio, arm.ratio),
        reverse=True,
    )
    best = ranked[0]
    if best.passes:
        return best, (
            f"{best.contract.describe()} clears the margin rule on the bootstrap's lower end "
            f"({ratio_ci_text(best)} vs. the required {required_ratio:.2f})."
        )
    passing = [arm.contract.describe() for arm in arms if arm.passes]
    if not passing:
        return best, (
            f"no contract clears {required_ratio:.2f}. The best is {best.contract.describe()} at "
            f"{best.ratio:.3f} ({ratio_ci_text(best)}). A separation this narrow is a statement "
            "about the corpus, the crop or the detector as much as about the contract - the four "
            "arms differ only in the tensor mapping, so a contract fix is now ruled out as the "
            "cause and the next experiment is the crop (--zoom) or the detector (--tiles)."
        )
    return best, f"{', '.join(passing)} clear(s) the margin rule; ranked by the CI's lower end."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Score a labelled corpus through all four input contracts (RGB/BGR x [0,1]/[-1,1]) on "
            "identical crops, and report which one the graph's own export convention is."
        )
    )
    parser.add_argument("--corpus", required=True,
                        help="folder of images; one subfolder per person, or 'name_1.jpg' filenames")
    parser.add_argument("--model", default=None,
                        help=f"ONNX graph to score against (default: first of {DEFAULT_MODEL_CANDIDATES} "
                             "that exists beside the backend)")
    parser.add_argument("--detector", default="yunet", choices=("yunet", "scrfd"))
    parser.add_argument("--detector-model", default=None,
                        help="detector ONNX path (default: the model the live pipeline uses)")
    parser.add_argument("--input-size", type=int, default=640,
                        help="the detector's input size, i.e. the crop's reach (default 640; the live "
                             "pipeline runs 320, so pass --input-size 320 to reuse a corpus captured "
                             "by it as it was)")
    parser.add_argument("--square", action="store_true",
                        help="letterbox into a square canvas instead of the aspect-matched rectangle")
    parser.add_argument("--landmarks", default="auto", choices=("auto", "stored", "detect"),
                        help="where each crop comes from: a stored sidecar when there is one and it "
                             "matches this detector (auto), a stored sidecar or a refusal (stored), or "
                             "a fresh detection pass (detect)")
    parser.add_argument("--size", type=int, default=160, help="graph input size (default 160)")
    parser.add_argument("--zoom", type=float, default=1.0,
                        help="framing zoom applied to the alignment template, 1.0 = template as-is")
    parser.add_argument("--tiles", type=int, default=1,
                        help="detector tiling policy; 2 enables the 2x2 small-face pass")
    parser.add_argument("--overlap", type=float, default=0.2, help="tile overlap for --tiles 2")
    parser.add_argument("--min-face-pixels", type=int, default=None,
                        help="skip faces smaller than this on their shorter side (default: the "
                             "graph's input size, because below it the crop is upscaled)")
    parser.add_argument("--min-impostor-pairs", type=int, default=DEFAULT_MIN_IMPOSTOR_PAIRS)
    parser.add_argument("--bootstrap", type=int, default=400, help="identity-cluster bootstrap rounds")
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--contracts", default=None,
                        help="comma-separated contract ids to score (default: all four)")
    parser.add_argument("--strict-single-face", action="store_true",
                        help="skip an image with more than one face instead of using the largest")
    parser.add_argument("--json", default=None, help="write the full measurements here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # Named ``corpus_root`` rather than ``corpus``: ``corpus`` is the *store module* this tool reads
    # stored crops from, and a local variable of the same name would shadow it into a ``Path`` right
    # where the fingerprint comparison needs it.
    corpus_root = Path(args.corpus)
    if not corpus_root.is_dir():
        print(f"corpus folder not found: {corpus_root}", file=sys.stderr)
        return 2

    import calibration
    import corpus
    import face_align
    import facenet_ort
    from derive_facenet_band import collect, identity_of  # the folder contract, not a copy of it

    # -- model resolution, before any work ------------------------------------
    if args.model:
        model_path = Path(args.model)
        if not model_path.is_absolute():
            candidate = Path(_BACKEND_DIR) / args.model
            model_path = candidate if candidate.exists() else model_path
        if not model_path.exists():
            print(f"graph not found: {model_path}", file=sys.stderr)
            return 2
    else:
        found = [Path(_BACKEND_DIR) / name for name in DEFAULT_MODEL_CANDIDATES]
        existing = [path for path in found if path.exists()]
        if not existing:
            print(
                "no graph found; pass --model (looked for " +
                ", ".join(str(path) for path in found) + ")",
                file=sys.stderr,
            )
            return 2
        model_path = existing[0]

    # -- contracts ------------------------------------------------------------
    wanted = {part.strip() for part in args.contracts.split(",")} if args.contracts else None
    contracts = [c for c in face_align.iter_contracts() if not wanted or c.id in wanted]
    if not contracts:
        print(f"no contract matched {sorted(wanted)}", file=sys.stderr)
        return 2

    grouped = collect(corpus_root)
    images = sum(len(paths) for paths in grouped.values())
    if not grouped:
        print(f"no images under {corpus_root}", file=sys.stderr)
        return 2

    engine = facenet_ort.FaceNetORT(model_path)
    min_face_pixels = engine.input_size if args.min_face_pixels is None else max(0, args.min_face_pixels)

    # -- detector, resolved the same way the live pipeline resolves it --------
    import detector_640

    detector_model = args.detector_model
    if not detector_model:
        import face_detector  # the live detector, resolved the way the live pipeline resolves it

        # ``model_path()`` rather than the module constant: it honours the deployment's own
        # override (``FACE_DETECTOR_MODEL_PATH``), so a corpus cropped here is cropped by the
        # detector this deployment actually runs - the same reasoning that keeps this tool from
        # reading a band out of ``face_detector.BANDS``.
        resolved = Path(face_detector.model_path())
        if not resolved.exists():
            print(
                f"the live pipeline's detector model is missing: {resolved}; pass --detector-model",
                file=sys.stderr,
            )
            return 2
        detector_model = str(resolved)
    detection = detector_640.build_detector(
        args.detector, detector_model, input_size=args.input_size, tiles=args.tiles,
        overlap=args.overlap, square=args.square,
    )
    # What this run *believes* it is cropping with. Compared against every stored sidecar, so a corpus
    # measured under another configuration refuses rather than being silently reinterpreted.
    requested = corpus.detector_fingerprint(
        kind=args.detector, model_path=detector_model, input_size=args.input_size,
        square=args.square, tiles=args.tiles, overlap=args.overlap,
    )

    try:
        prepared, skipped, landmark_sources = prepare(
            grouped,
            detect=detection,
            size=args.size,
            zoom=args.zoom,
            min_face_pixels=min_face_pixels,
            strict_single_face=args.strict_single_face,
            landmarks=args.landmarks,
            expected=requested,
        )
    except corpus.CorpusError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    if len(prepared) < 2:
        print(
            f"only {len(prepared)} of {images} images could be cropped; nothing can be measured. "
            "Skips are listed below.",
            file=sys.stderr,
        )
        for note in skipped[:20]:
            print(f"  - {note}", file=sys.stderr)
        return 2

    labels = [item.identity for item in prepared]
    identities = len(set(labels))
    if identities < 2:
        print(
            f"the corpus resolved to one identity ({identities}); an impostor distribution needs "
            "at least two. Check the folder contract: subdirectories are identities, and a flat "
            "file is split on a trailing number.",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        print(f"corpus        : {corpus_root}")
        print(f"images        : {images} found, {len(prepared)} cropped, {len(skipped)} skipped")
        print(f"identities    : {identities} of {len(grouped)}")
        print(f"graph         : {model_path.name}  width={engine.width}  input={engine.input_size}  "
              f"sha256={engine.model_id[:12]}  providers={list(engine.facts.providers)}")
        print(f"classifier    : {engine._device}")  # noqa: SLF001 - reported, not used
        print(f"detector      : {args.detector} input={args.input_size} square={args.square} "
              f"tiles={args.tiles} overlap={args.overlap:.2f} face floor={min_face_pixels}px")
        print(f"crop          : {corpus.crop_description(requested)}")
        print(f"crops from    : {landmark_sources['stored']} stored sidecar(s), "
              f"{landmark_sources['detected']} fresh detection(s)")
        print(f"framing       : zoom={args.zoom:.2f} (template-derived, not a second constant)")
        fallback = sum(1 for item in prepared if item.used_fallback)
        if fallback:
            print(f"               {fallback} crop(s) used the landmark-box fallback, not the "
                  "similarity fit")
        if skipped:
            print("skipped       :")
            for note in skipped[:10]:
                print(f"  - {note}")
            if len(skipped) > 10:
                print(f"  ... and {len(skipped) - 10} more")
        print()

    # -- score every arm on the identical crop set ----------------------------
    arms: list[Arm] = []
    for contract in contracts:
        vectors, norms, stats, digest = embed_arm(prepared, engine=engine, contract=contract)
        try:
            genuine, impostor = calibration.pair_distances(vectors, labels)
        except calibration.CalibrationError as exc:
            print(f"{contract.id}: {exc}", file=sys.stderr)
            return 2
        if impostor.size < args.min_impostor_pairs:
            print(
                f"{contract.id}: only {impostor.size} impostor pairs, below the "
                f"{args.min_impostor_pairs} this tool will call a floor. Add identities.",
                file=sys.stderr,
            )
            return 2
        arms.append(
            Arm(
                contract=contract,
                distances=(genuine, impostor),
                report=_evaluate(calibration, vectors, labels, args),
                norm_mean=norms["mean"],
                norm_min=norms["min"],
                norm_max=norms["max"],
                tensor_stats=stats,
                tensor_digest=digest,
            )
        )

    counts = {arm.distances[1].size for arm in arms}
    if len(counts) != 1:
        print(
            f"arms disagree on the pair count ({sorted(counts)}); the shared-crop invariant is "
            "broken and the comparison is not controlled. This is a bug in this tool, not in the "
            "corpus.",
            file=sys.stderr,
        )
        return 2

    reference = arms[0]
    ref_pairs = np.concatenate(reference.distances)
    for arm in arms:
        if arm is reference:
            arm.shift = {"median": 0.0, "mean": 0.0, "p10": 0.0, "p90": 0.0, "spread": 0.0}
            arm.implied_ratio = arm.ratio
            arm.rank_agreement = 1.0
            continue
        arm.shift = shift_stats(ref_pairs, np.concatenate(arm.distances))
        # What the ratio *would* be if the change were a pure constant offset. If this matches the
        # measured ratio, the whole separation change is one additive term - a global input
        # transform - and not a per-identity effect dressed up as one.
        ceiling = arm.report["raw"]["genuine_ceiling"] + arm.shift["median"]
        floor = arm.report["raw"]["impostor_floor"] + arm.shift["median"]
        arm.implied_ratio = round(floor / ceiling, 4) if ceiling > 0 else math.inf
        arm.rank_agreement = spearman(ref_pairs, np.concatenate(arm.distances))

    best, why = verdict(arms, required_ratio=calibration.MARGIN_SQUARED)

    print(render_table(arms, reference=reference))
    print()
    print(f"margin rule   : floor/ceiling >= {calibration.MARGIN_SQUARED:.2f} "
          f"(IMPOSTOR_MARGIN {calibration.IMPOSTOR_MARGIN}^2), gated on the CI's lower end")
    print(f"pairs         : genuine {reference.distances[0].size}, "
          f"impostor {reference.distances[1].size}, identities {identities}")
    print(f"graph health  : embedding norms {reference.norm_min:.4f}..{reference.norm_max:.4f} "
          f"(mean {reference.norm_mean:.4f}) - a unit-norm output means host-side L2 normalisation "
          "is in force; a value near 5.8 means it is not, and every cosine below is wrong")
    print(f"tensor        : {reference.contract.id} -> {reference.tensor_stats}")
    print()
    print(f"verdict       : {best.contract.describe()}")
    print(f"                {why}")
    if best.rank_agreement is not None and best is not reference and best.rank_agreement > 0.99:
        print("                rank agreement with the incumbent is ~1: the arms differ by a "
              "global offset, so the winner is a contract fix rather than a change of behaviour.")
    if reference.distances[1].size < COMFORTABLE_IMPOSTOR_PAIRS:
        print(f"                caution: {reference.distances[1].size} impostor pairs is thin - the "
              f"floor is an estimate of roughly the "
              f"{100.0 / reference.distances[1].size:.1f}th percentile. "
              f"{calibration.min_pair_count_for_floor()} pairs are needed for the minimum to sit "
              "near the 0.5th percentile.")
    if any(arm.report.get("review_window_collapsed") for arm in arms):
        print("                at least one arm collapsed to a single line: no review tier can "
              "exist until the ratio clears the rule.")

    if args.json:
        payload = {
            "corpus": str(corpus_root),
            "graph": {
                "path": str(model_path),
                "sha256": engine.model_id,
                "width": engine.width,
                "input_size": engine.input_size,
                "providers": list(engine.facts.providers),
            },
            "corpus_stats": {
                "images": images,
                "cropped": len(prepared),
                "skipped": len(skipped),
                "identities": identities,
                "genuine_pairs": int(reference.distances[0].size),
                "impostor_pairs": int(reference.distances[1].size),
            },
            "detector": {"kind": args.detector, "tiles": args.tiles, "overlap": args.overlap},
            "crop_fingerprint": requested,
            "crop_source": {"stored": landmark_sources["stored"],
                            "detected": landmark_sources["detected"],
                            "mode": args.landmarks},
            "framing": {"size": args.size, "zoom": args.zoom},
            "required_ratio": calibration.MARGIN_SQUARED,
            "arms": [
                {
                    "contract": arm.contract.id,
                    "label": arm.contract.describe(),
                    "calibration": arm.report,
                    "shift": arm.shift,
                    "implied_ratio_from_shift": arm.implied_ratio,
                    "rank_agreement": arm.rank_agreement,
                    "embedding_norms": {
                        "min": arm.norm_min, "max": arm.norm_max, "mean": arm.norm_mean
                    },
                    "tensor": arm.tensor_stats,
                    "tensor_sha256": arm.tensor_digest,
                }
                for arm in arms
            ],
            "verdict": {"contract": best.contract.id, "passes": best.passes, "reason": why},
            "skipped": skipped,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if not args.quiet:
            print(f"\nwrote         : {args.json}")

    return 0 if best.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
