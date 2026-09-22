"""VGG-Face against Facenet512, on this machine, on CPU, from three photographs.

Why this exists
---------------
The recognition model is the most expensive thing in the punch path and the one decision nobody
can make by reading a table. ``face_detector.BANDS`` records the model each band was *measured*
with, and both shipped bands are VGG-Face's: 4096 floats per face, and per-inference cost that
``telemetry`` reports in hundreds of milliseconds. Facenet512 is a smaller network returning 512
floats, so "is it better here" has three answers that have to be measured together:

* **latency** - what an enrollment or a punch pays per comparison, and how much of it is the
  detector rather than the model (the two are separate on purpose: detection is the expensive
  half at the application's working size);
* **separation** - the distance between the genuine pair and the closest impostor pair, because
  an approve line can only sit in that window, and a band with no window is a band with no
  evidence;
* **what a template costs** - 4096 vs 512 dimensions is 16 KB vs 2 KB as float32, and eight times
  the JSON text this application actually writes to ``local_references/<id>.json``.

The distances measured here are the *same metric production compares with* - ``scipy.spatial.
distance.cosine`` on the raw embedding, see the comparison in ``main.py`` - so the numbers can be
read against ``face_detector.BANDS`` and the band rule in ``MatchBand.derived()`` directly. The
recommended lines are derived with that same rule (geometric centre of the measured window at
0.05 granularity, review at the impostor floor / ``IMPOSTOR_MARGIN``), so a band measured here
drops into the table in the app's own shape.

What this is *not*: evidence. Three photographs measure a model, not a camera fleet. The shipped
impostor floor rests on 495 different-people pairs at punch scale; a folder of three is a demo
that tells you whether a swap is worth measuring properly.

Usage
-----
    python backend/tools/benchmark_face_models.py --images ./faces
    python backend/tools/benchmark_face_models.py --images ./faces --detector skip    # pre-cropped
    python backend/tools/benchmark_face_models.py --images ./faces --runs 30 --json ./bench.json
    python backend/tools/benchmark_face_models.py --images ./faces --models Facenet512

The folder contract
-------------------
Two photographs of one person and one of somebody else:

    ./faces/genuine-a.jpg     person A
    ./faces/genuine-b.jpg     person A
    ./faces/impostor.jpg      person B

Any file whose name contains ``impostor``, ``imposter``, ``different`` or ``other`` is the odd
one out; ``--impostor FILE`` names it explicitly when the filenames do not. Exactly two genuine
and one impostor are required, and the mapping is printed before anything is measured - a
benchmark on the wrong pair is worse than no benchmark, because it looks like an answer.

Exit codes
----------
    0  measured
    2  the folder does not hold exactly two genuine images and one impostor
    3  a model could not be loaded, or an image could not be embedded
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# CPU-only, and set *before* anything imports TensorFlow: DeepFace's import chain pulls it in, so
# this has to happen at module import rather than inside main(). A GPU would make the comparison
# useless for a CPU deployment, which is what this script is asked about.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

DEFAULT_MODELS = ("VGG-Face", "Facenet512")
DEFAULT_RUNS = 15
DEFAULT_DETECTOR = "opencv"
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"})
IMPOSTOR_MARKERS = ("impostor", "imposter", "different", "other", "stranger")

#: The application's own band rule, mirrored rather than imported - this script has to run
#: without the application on ``sys.path``. Both values are named where they come from so a
#: change there is a change here: ``face_detector.IMPOSTOR_MARGIN`` is 1.35 and
#: ``face_detector.LINE_GRANULARITY`` is 0.05, and ``MatchBand.derived()`` is the rule.
IMPOSTOR_MARGIN = 1.35
LINE_GRANULARITY = 0.05

#: The band this build ships, for reference beside whatever is measured here. The ceiling, floor
#: and evidence are ``face_detector.BANDS["yunet-2023mar"]`` - the numbers a swap would replace.
SHIPPED_BAND = {"model": "VGG-Face", "approve": 0.40, "review": 0.50, "ceiling": 0.233, "floor": 0.676}


class BenchmarkError(RuntimeError):
    """Anything that makes the measurement meaningless, with the reason the operator needs."""


# ---------------------------------------------------------------------------
# the application's arithmetic, mirrored
# ---------------------------------------------------------------------------
def line(value: float) -> float:
    """A decision line, at the granularity a decision is actually made at."""
    return round(round(value / LINE_GRANULARITY) * LINE_GRANULARITY, 2)


def derive_lines(genuine: float, impostor: float) -> tuple[float, float]:
    """``(approve, review)`` from a measured window, by the application's own rule."""
    centre = (genuine * impostor) ** 0.5
    return line(centre), line(impostor / IMPOSTOR_MARGIN)


def classify(distance: float, approve: float, review: float) -> str:
    """Where a distance lands, with the application's inclusive boundaries."""
    if distance <= approve:
        return "approved (accepted without a human)"
    if distance <= review:
        return "review (routed to a human)"
    return "refused"


def cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine distance, by the function production compares with.

    ``scipy.spatial.distance.cosine`` is what ``main.py`` calls on a stored template against a
    live embedding, and it arrives with DeepFace's own dependency set, so this adds nothing to
    install. A dimension mismatch is refused rather than scored: the application answers
    ``FACE_REFERENCE_STALE`` for exactly this case, and a distance between vectors of different
    sizes is not a distance.
    """
    from scipy.spatial.distance import cosine

    if len(left) != len(right):
        raise BenchmarkError(
            f"embeddings of {len(left)} and {len(right)} dimensions cannot be compared; the "
            f"application refuses a template of a different size as FACE_REFERENCE_STALE, and a "
            f"model swap therefore invalidates every stored template"
        )
    return float(cosine(list(left), list(right)))


def python_list_cost(values: Sequence[float]) -> int:
    """What the embedding costs as DeepFace returns it: a list of boxed Python floats."""
    return sys.getsizeof(values) + sum(sys.getsizeof(value) for value in values)


def json_template_bytes(values: Sequence[float]) -> int:
    """What one template costs on disk, which is a JSON list of these floats."""
    return len(json.dumps(list(values)).encode("utf-8"))


# ---------------------------------------------------------------------------
# image input
# ---------------------------------------------------------------------------
def load_image(path: Path) -> Any:
    """One image, read by OpenCV, in the channel order DeepFace's array path expects.

    That order is BGR, and it is worth being explicit about because getting it wrong still
    produces a number: ``image_utils.load_image`` returns an array untouched and ``represent``
    flips it to RGB itself, so handing it RGB embeds a channel-swapped face. It is also the
    order production uses - ``face_engine``'s own docstring says "BGR array or path" - so the
    distances here stay comparable with the shipped band.
    """
    import cv2

    image = cv2.imread(str(path))
    if image is None:
        raise BenchmarkError(f"OpenCV could not read {path}")
    return image


def collect_inputs(directory: Path, impostor: str | None) -> tuple[list[Path], Path]:
    """``([two genuine], impostor)`` from a folder, or the reason it cannot be read."""
    if not directory.is_dir():
        raise BenchmarkError(f"{directory} is not a directory")
    images = sorted(
        path for path in directory.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    if len(images) < 3:
        raise BenchmarkError(
            f"{directory} holds {len(images)} image(s); the benchmark needs two of one person "
            f"and one of somebody else"
        )

    odd_one = None
    if impostor is not None:
        wanted = Path(impostor)
        matches = [path for path in images if path.name == wanted.name]
        if not matches:
            raise BenchmarkError(f"--impostor {impostor} is not one of the images in {directory}")
        odd_one = matches[0]
    else:
        marked = [
            path
            for path in images
            if any(marker in path.stem.lower() for marker in IMPOSTOR_MARKERS)
        ]
        if len(marked) > 1:
            raise BenchmarkError(
                f"{[path.name for path in marked]} all look like the impostor; pass --impostor "
                f"to say which one is the different person"
            )
        odd_one = marked[0] if marked else None

    genuine = [path for path in images if path is not odd_one]
    if odd_one is None:
        raise BenchmarkError(
            f"none of {[path.name for path in images]} is marked as the different person: name "
            f"one with 'impostor' in it, or pass --impostor FILE"
        )
    if len(genuine) != 2:
        raise BenchmarkError(
            f"the benchmark needs exactly two images of the first person and one of the second; "
            f"that leaves {len(genuine)} genuine image(s) "
            f"({[path.name for path in genuine]}), so sort the folder or pass --impostor FILE"
        )
    return genuine, odd_one


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
@dataclass
class Timing:
    """One pass type's samples, in milliseconds."""

    runs: list[float] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.runs)

    @property
    def median(self) -> float:
        return statistics.median(self.runs)

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.runs) if len(self.runs) > 1 else 0.0

    @property
    def worst(self) -> float:
        return max(self.runs)

    @property
    def best(self) -> float:
        return min(self.runs)

    def percentile(self, fraction: float) -> float:
        ordered = sorted(self.runs)
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[index]


@dataclass
class ModelResult:
    """Everything one model measured, and nothing inferred."""

    model: str
    dimension: int
    dtype: str
    float32_bytes: int
    python_list_bytes: int
    json_bytes: int
    weights_ms: float
    forward: Timing
    detected: Timing | None
    detector: Timing | None
    distances: dict[str, float]

    @property
    def genuine(self) -> float:
        return self.distances["genuine"]

    @property
    def contended(self) -> bool:
        """Whether the machine was busy: one sample more than twice the median is not a model."""
        return self.forward.worst > 2 * self.forward.median

    @property
    def impostor(self) -> dict[str, float]:
        return {name: value for name, value in self.distances.items() if name != "genuine"}

    @property
    def impostor_floor(self) -> float:
        """The *closest* different-people pair: the number a band has to stay below."""
        return min(self.impostor.values())

    @property
    def margin(self) -> float:
        """How much room a band has: impostor floor minus the genuine distance, larger is better."""
        return self.impostor_floor - self.genuine


def _one_embedding(represent: Callable[..., Any], image: Any, model: str, detector: str) -> list[float]:
    """One embedding for one image, refusing anything a caller should not silently accept."""
    result = represent(
        img_path=image,
        model_name=model,
        detector_backend=detector,
        enforce_detection=False,
    )
    if not result:
        raise BenchmarkError(f"{model} returned no embedding (detector={detector!r})")
    if len(result) > 1:
        # The application refuses a frame with more than one face rather than picking one; this
        # script takes the first only so a demo folder still runs, and says so.
        print(
            f"    note: {model} found {len(result)} faces in one image; using the first "
            f"(the application would refuse the capture outright)",
            file=sys.stderr,
        )
    entry = result[0]
    embedding = entry["embedding"] if isinstance(entry, dict) else entry
    return [float(value) for value in embedding]


def _crop(extract: Callable[..., Any], image: Any, detector: str) -> Any:
    """The face region the detector found, once, outside any timing, as uint8.

    ``extract_faces`` answers with a float array scaled to [0, 1] (in BGR, matching the array
    contract above), so it is scaled back to the uint8 pixels a phone uploads and
    ``face_detector.detect_and_align`` crops. Same bytes, same dtype, same order as a punch -
    otherwise the forward pass here would be measured on a picture the application never sees.
    """
    if detector == "skip":
        return image
    faces = extract(img_path=image, detector_backend=detector, enforce_detection=False)
    if not faces:
        raise BenchmarkError(
            f"the {detector!r} detector found no face, so a forward-pass time on a crop cannot "
            f"be measured; use --detector skip for already-cropped images"
        )
    import numpy as np

    face = faces[0]["face"]
    if face.dtype != np.uint8:
        face = np.clip(np.round(face * 255.0), 0, 255).astype(np.uint8)
    return face


def measure(
    model: str,
    *,
    represent: Callable[..., Any],
    extract: Callable[..., Any],
    images: dict[str, Any],
    detector: str,
    runs: int,
) -> ModelResult:
    """Load, warm up, then time one model - pure pass always, end-to-end when detecting."""
    # 1. Weights. The first call is the weight load, and reporting it separately is the point:
    #    it is what a cold process pays, and it is minutes for one of these models on a CPU.
    started = time.perf_counter()
    _one_embedding(represent, images["genuine"][0], model, "skip")
    weights_ms = (time.perf_counter() - started) * 1000

    # 2. The pure forward pass: a crop, with detection skipped on every timed call. This is the
    #    number the user asked for, and the only one that is the model's own.
    crops = {
        name: _crop(extract, image, detector)
        for name, image in (("genuine0", images["genuine"][0]), ("genuine1", images["genuine"][1]), ("impostor", images["impostor"]))
    }
    forward = Timing()
    for _ in range(runs):
        started = time.perf_counter()
        _one_embedding(represent, crops["genuine0"], model, "skip")
        forward.runs.append((time.perf_counter() - started) * 1000)

    # 3. The same pass with the detector in front of it, when a detector is in play: the
    #    difference is what detection costs, and it is not the model's latency.
    detected: Timing | None = None
    detector_timing: Timing | None = None
    if detector != "skip":
        _one_embedding(represent, images["genuine"][0], model, detector)  # detector's own warmup
        # The detector on its own, which is the only way to know what it costs: deriving it as
        # "end-to-end minus pure" attributes every bit of machine jitter to OpenCV, and that is
        # how a contended run comes out saying the detector takes 850 ms when it takes 22.
        extract(img_path=images["genuine"][0], detector_backend=detector, enforce_detection=False)
        detector_timing = Timing()
        for _ in range(runs):
            started = time.perf_counter()
            extract(img_path=images["genuine"][0], detector_backend=detector, enforce_detection=False)
            detector_timing.runs.append((time.perf_counter() - started) * 1000)
        detected = Timing()
        for _ in range(runs):
            started = time.perf_counter()
            _one_embedding(represent, images["genuine"][0], model, detector)
            detected.runs.append((time.perf_counter() - started) * 1000)

    # 4. Embeddings for the distances, from the pure crop so the numbers do not move with the
    #    detector choice: a crop decides which pixels, the model decides what the vector means.
    embeddings = {
        name: _one_embedding(represent, crop, model, "skip") for name, crop in crops.items()
    }
    distances = {
        "genuine": cosine_distance(embeddings["genuine0"], embeddings["genuine1"]),
        "impostor(genuine0)": cosine_distance(embeddings["genuine0"], embeddings["impostor"]),
        "impostor(genuine1)": cosine_distance(embeddings["genuine1"], embeddings["impostor"]),
    }

    import numpy as np  # already in memory: DeepFace and OpenCV both depend on it

    vector = np.asarray(embeddings["genuine0"], dtype=np.float32)
    return ModelResult(
        model=model,
        dimension=len(embeddings["genuine0"]),
        dtype=vector.dtype.name,
        float32_bytes=int(vector.nbytes),
        python_list_bytes=python_list_cost(embeddings["genuine0"]),
        json_bytes=json_template_bytes(embeddings["genuine0"]),
        weights_ms=weights_ms,
        forward=forward,
        detected=detected,
        detector=detector_timing,
        distances=distances,
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _kb(value: float) -> str:
    return f"{value / 1024:.1f} KB"


def _ms(value: float) -> str:
    return f"{value:,.1f}"


def difference(baseline: float, candidate: float) -> str:
    """``(candidate - baseline) / baseline``, as the table's percentage column."""
    if baseline == 0:
        return "n/a"
    delta = (candidate - baseline) / abs(baseline) * 100
    return f"{delta:+.1f}%"


@dataclass(frozen=True)
class Row:
    """One table line: the number it compares, how that number prints, and which way is better.

    Keeping the number and its formatting apart is what makes the Difference column arithmetic
    rather than string surgery: a `480.0 .. 560.0` cell and a `16.0 KB` cell both compare on the
    measurement behind them, and a row that has no direction says so with ``better=None``.
    """

    label: str
    value: Callable[[ModelResult], float]
    render: Callable[[float], str]
    better: str | None = None
    compare: bool = True


ROWS: tuple[Row, ...] = (
    Row("Embedding dimension", lambda r: float(r.dimension), lambda v: f"{int(v):,}", "lower"),
    Row("Embedding memory (float32)", lambda r: float(r.float32_bytes), _kb, "lower"),
    Row("Template on disk (JSON)", lambda r: float(r.json_bytes), _kb, "lower"),
    Row("Weight load + first pass (ms)", lambda r: r.weights_ms, _ms, "lower"),
    Row("Forward pass, mean (ms)", lambda r: r.forward.mean, _ms, "lower"),
    Row("Forward pass, median (ms)", lambda r: r.forward.median, _ms, "lower"),
    Row("Forward pass, stdev (ms)", lambda r: r.forward.stdev, _ms, "lower"),
    Row(
        "Forward pass, min .. max (ms)",
        lambda r: r.forward.mean,
        lambda _v: "",  # replaced per model below: the range is two numbers, not one
        None,
        compare=False,
    ),
    Row("Genuine distance", lambda r: r.genuine, lambda v: f"{v:.4f}", "lower"),
    Row("Impostor distance (closest pair)", lambda r: r.impostor_floor, lambda v: f"{v:.4f}", "higher"),
    Row("Separation margin", lambda r: r.margin, lambda v: f"{v:.4f}", "higher"),
)

#: The end-to-end row, which exists only when a detector ran (with ``--detector skip`` the two
#: numbers would be the same number printed twice).
DETECTED_ROW = Row("With detection, mean (ms)", lambda r: r.detected.mean, _ms, "lower")  # type: ignore[union-attr]


def print_table(results: list[ModelResult], detector: str, runs: int) -> None:
    detector_name = f"{detector!r}"
    print()
    print(f"### {runs} timed passes per model, detector `{detector}`")
    print()
    print(f"| Metric | {results[0].model} | {results[1].model} | Difference (%) |")
    print("| --- | --- | --- | --- |")

    rows = list(ROWS)
    if all(result.detected is not None for result in results):
        rows.append(DETECTED_ROW)

    for row in rows:
        if row.compare:
            left, right = row.value(results[0]), row.value(results[1])
            cells, delta = [row.render(left), row.render(right)], difference(left, right)
        elif row.label.startswith("Forward pass, min"):
            cells = [
                f"{_ms(result.forward.best)} .. {_ms(result.forward.worst)}" for result in results
            ]
            # Two numbers cannot have one difference, so this compares the means printed above
            # it - and says so, rather than looking like the range's own arithmetic.
            delta = f"{difference(results[0].forward.mean, results[1].forward.mean)} (mean)"
        else:  # pragma: no cover - a non-comparing row without its own formatting
            cells, delta = [row.render(row.value(result)) for result in results], "n/a"
        print(f"| {row.label} | " + " | ".join(cells) + f" | {delta} |")

    print()
    print("Difference is the second model against the first; the direction that is *better* per row:")
    for row in rows:
        if row.better:
            print(f"  * {row.label} - {row.better} is better")
    print("  * Difference (%) is arithmetic only: a model that is worse on latency can win the margin.")
    print()
    print("Raw samples (ms):")
    for result in results:
        print(
            f"  * {result.model}: min {result.forward.best:.1f}, p95 {result.forward.percentile(0.95):.1f}, "
            f"max {result.forward.worst:.1f}"
            + (
                f"; end to end with detection {result.detected.mean:.1f} mean "
                f"(the {detector_name} detector alone {result.detector.mean:.1f} mean)"
                if result.detected is not None and result.detector is not None
                else "; detection skipped (pre-cropped input)"
            )
        )
        if result.contended:
            print(
                f"    ! one sample ({result.forward.worst:.0f} ms) is more than twice the median "
                f"({result.forward.median:.0f} ms), so this machine was busy: read the median, not "
                f"the mean, and re-run on an idle host before deciding anything"
            )
    print()
    print("Distances per model (cosine, production's metric):")
    for result in results:
        pairs = ", ".join(f"{name} {value:.4f}" for name, value in sorted(result.distances.items()))
        print(
            f"  * {result.model}: {pairs}; margin {result.margin:.4f} "
            f"({result.impostor_floor / result.genuine:.2f}x the genuine distance)"
            if result.genuine
            else f"  * {result.model}: {pairs}"
        )
    print()
    print("Embedding cost, three ways (one template):")
    for result in results:
        print(
            f"  * {result.model}: float32 {_kb(result.float32_bytes)}, as returned by DeepFace "
            f"{_kb(result.python_list_bytes)} (boxed Python floats), written as JSON "
            f"{_kb(result.json_bytes)}"
        )


def recommend(results: list[ModelResult], folder: Path, runs: int, detector: str) -> None:
    """The band the measured window supports, in the shape ``face_detector.BANDS`` uses."""
    target = results[-1]
    approve, review = derive_lines(target.genuine, target.impostor_floor)
    closest = min(target.impostor, key=lambda name: target.impostor[name])

    print()
    print(f"### Recommended band for {target.model}")
    print()
    print(
        f"Measured on {folder}: genuine pair {target.genuine:.4f}, closest different-people pair "
        f"{target.impostor_floor:.4f} ({closest})."
    )
    print()
    print(f"    approve (accept, no human)  {approve:.2f}   geometric centre of the window "
          f"({target.genuine:.3f} .. {target.impostor_floor:.3f}), "
          f"{(approve / target.genuine if target.genuine else float('inf')):.2f}x above the measured "
          f"genuine distance")
    print(f"    review  (route to a human)  {review:.2f}   impostor floor / {IMPOSTOR_MARGIN} "
          f"(the shipped margin), {target.impostor_floor / review if review else float('inf'):.2f}x "
          f"below the floor")
    print()
    print(
        f"Under those lines the measured pairs land where a band has to put them: genuine "
        f"{target.genuine:.4f} -> {classify(target.genuine, approve, review)}, impostor "
        f"{target.impostor_floor:.4f} -> {classify(target.impostor_floor, approve, review)}."
    )
    if approve < target.genuine:
        print()
        print(
            f"WARNING: the derived approve line ({approve:.2f}) is below the measured genuine "
            f"distance ({target.genuine:.4f}), so the rule's 0.05 rounding has eaten the window. "
            f"This folder's two people are too close together to derive a band from - add "
            f"photographs, or widen the window before trusting these lines."
        )
    if SHIPPED_BAND["model"] != target.model:
        print()
        print(
            f"Note: a band is only about *one* model's vector space - the shipped band is "
            f"{SHIPPED_BAND['model']}'s ({SHIPPED_BAND['approve']:.2f}/{SHIPPED_BAND['review']:.2f}, "
            f"ceiling {SHIPPED_BAND['ceiling']:.3f}, floor {SHIPPED_BAND['floor']:.3f} on 495 "
            f"different-people pairs) and {target.model} embeddings are {target.dimension}-dimensional, "
            f"so its lines do not transfer. ``face_detector.band_for`` refuses that combination by "
            f"design: a model swap keeps the pipeline's name, so a band left in place would keep "
            f"lines measured for vectors the punch no longer produces."
        )
    print()
    print("Paste-ready, in the shape `face_detector.BANDS` uses:")
    print()
    print(f'    "{target.model.lower()}-measured-here": MatchBand(')
    print(f'        model="{target.model}",')
    print(f"        approve={approve:.2f},")
    print(f"        review={review:.2f},")
    print(f"        genuine_ceiling={target.genuine:.3f},")
    print(f"        impostor_floor={target.impostor_floor:.3f},")
    evidence = (
        f"One genuine pair and {len(target.impostor)} different-people pairs from {folder.name}/ "
        f"on {platform.node() or 'this host'}: the window is {target.genuine:.3f} .. "
        f"{target.impostor_floor:.3f}. A demo, not evidence - the shipped floor rests on 495 "
        f"such pairs at punch scale, so re-measure on the deployment's own captures before "
        f"shipping these lines."
    )
    print("        evidence=(")
    print(f'            "{evidence}"')
    print("        ),")
    print("    ),")
    print()
    print(
        f"Also relevant before switching: the swap invalidates every stored template "
        f"({results[0].dimension:,} -> {target.dimension:,} dimensions). Production refuses a "
        f"template of a different size as FACE_REFERENCE_STALE rather than scoring it, so "
        f"enrollment has to be re-run - plan that against the shift schedule, not against the "
        f"{runs} timed passes above."
    )
    print()
    print(
        f"Sanity check against the shipped band, applied to this folder's distances "
        f"({detector}, {runs} runs): {results[0].model} genuine "
        f"{classify(results[0].genuine, SHIPPED_BAND['approve'], SHIPPED_BAND['review'])}, impostor "
        f"{classify(results[0].impostor_floor, SHIPPED_BAND['approve'], SHIPPED_BAND['review'])}. "
        f"If either half disagrees, the folder is not measuring what the shipped band was measured "
        f"on (punch-scale JPEGs of real people) - a different crop or a different camera."
    )


def utf8_output() -> None:
    """Print UTF-8 whatever console this is.

    A Windows console is not always UTF-8 - this one is cp1256 - so a single non-ASCII
    character in a measurement becomes a traceback at the very end of a two-minute run. Photo
    filenames are the case that matters: an operator's folder can hold a worker's name in
    Arabic, and the error message has to be able to print it. Pipelines and older interpreters
    simply keep their own encoding.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def provenance() -> dict[str, Any]:
    """What the numbers are only meaningful alongside."""
    import importlib.metadata as metadata

    def version(package: str) -> str:
        try:
            return metadata.version(package)
        except Exception:  # noqa: BLE001 - a missing optional package is not a failure here
            return "not installed"

    devices: list[str] = []
    try:
        import tensorflow as tf

        devices = [device.device_type for device in tf.config.list_physical_devices()]
    except Exception as exc:  # noqa: BLE001
        devices = [f"unavailable: {exc}"]

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "tensorflow_devices": devices,
        "packages": {
            name: version(name)
            for name in ("deepface", "tensorflow", "numpy", "opencv-python", "scipy")
        },
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    parser = argparse.ArgumentParser(
        description="Benchmark VGG-Face against Facenet512 on CPU, from three photographs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--images", required=True, type=Path, help="folder: two images of one person, one of another")
    parser.add_argument("--impostor", default=None, help="which file is the different person (default: the name says)")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help=f"comma-separated (default: {','.join(DEFAULT_MODELS)})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"timed passes per model (default: {DEFAULT_RUNS})")
    parser.add_argument(
        "--detector",
        default=DEFAULT_DETECTOR,
        help="DeepFace detector for the end-to-end pass; 'skip' means the images are already cropped (default: opencv)",
    )
    parser.add_argument("--json", type=Path, default=None, help="also write the measurements here")
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs must be at least 1")
    models = [name.strip() for name in args.models.split(",") if name.strip()]
    if len(models) != 2:
        parser.error(f"name exactly two models to compare, got {models}")

    try:
        genuine, impostor = collect_inputs(args.images, args.impostor)
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("## Face model benchmark (CPU)")
    print()
    print("Inputs, as they will be used:")
    for path in genuine:
        print(f"  * genuine  {path.name}")
    print(f"  * impostor {impostor.name}")
    print()
    print("Provenance:")
    info = provenance()
    print(f"  * python {info['python']} on {info['platform']}, {info['cpu_count']} CPUs")
    print(f"  * tensorflow devices: {', '.join(info['tensorflow_devices'])} (CUDA_VISIBLE_DEVICES={info['cuda_visible_devices']})")
    for name, version in info["packages"].items():
        print(f"  * {name} {version}")
    if "GPU" in info["tensorflow_devices"]:
        print("  * WARNING: a GPU is visible, so these are not CPU numbers - unset CUDA_VISIBLE_DEVICES to reproduce")

    try:
        from deepface import DeepFace
    except Exception as exc:  # noqa: BLE001 - reported, not raised: the reason matters more
        print(f"error: DeepFace could not be imported: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    images = {"genuine": [load_image(path) for path in genuine], "impostor": load_image(impostor)}
    results: list[ModelResult] = []
    for model in models:
        print()
        print(f"Measuring {model} ...")
        try:
            results.append(
                measure(
                    model,
                    represent=DeepFace.represent,
                    extract=DeepFace.extract_faces,
                    images=images,
                    detector=args.detector,
                    runs=args.runs,
                )
            )
        except BenchmarkError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        except Exception as exc:  # noqa: BLE001
            print(
                f"error: {model} failed: {type(exc).__name__}: {exc}\n"
                f"  Weights download on first use (VGG-Face ~580 MB, Facenet512 ~90 MB) - if the "
                f"network is blocked, fetch them into ~/.deepface/weights first.",
                file=sys.stderr,
            )
            return 3
        print(f"  forward pass {results[-1].forward.mean:,.1f} ms mean, dimension {results[-1].dimension:,}")

    print_table(results, args.detector, args.runs)
    recommend(results, args.images, args.runs, args.detector)

    if args.json is not None:
        payload = {
            "provenance": info,
            "inputs": {"genuine": [str(path) for path in genuine], "impostor": str(impostor)},
            "detector": args.detector,
            "runs": args.runs,
            "models": {
                result.model: {
                    "dimension": result.dimension,
                    "dtype": result.dtype,
                    "float32_bytes": result.float32_bytes,
                    "python_list_bytes": result.python_list_bytes,
                    "json_bytes": result.json_bytes,
                    "weights_ms": round(result.weights_ms, 3),
                    "forward_ms": {
                        "mean": round(result.forward.mean, 3),
                        "median": round(result.forward.median, 3),
                        "stdev": round(result.forward.stdev, 3),
                        "min": round(result.forward.best, 3),
                        "max": round(result.forward.worst, 3),
                        "p95": round(result.forward.percentile(0.95), 3),
                        "samples": [round(value, 3) for value in result.forward.runs],
                    },
                    "detected_ms": (
                        {"mean": round(result.detected.mean, 3), "samples": [round(v, 3) for v in result.detected.runs]}
                        if result.detected is not None
                        else None
                    ),
                    "detector_ms": (
                        {"mean": round(result.detector.mean, 3), "samples": [round(v, 3) for v in result.detector.runs]}
                        if result.detector is not None
                        else None
                    ),
                    "contended": result.contended,
                    "distances": {name: round(value, 6) for name, value in result.distances.items()},
                    "margin": round(result.margin, 6),
                }
                for result in results
            },
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print()
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
