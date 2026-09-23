"""The coverage sweep: one corpus of gate frames, every detector configuration, no storage.

WHY THIS EXISTS
---------------
The rollout's Phase 1 claim is arithmetic: running the detector at 640 instead of 320 doubles the
subject scale, and 2x2 tiling raises the smallest detectable face from 32px to ~18px on a 1280x720
frame. Arithmetic is not evidence. What the claim needs is *this deployment's own frames*, run
through each configuration, with the answer to one question per frame:

    at which detector settings can this real face be found, and at which does the pipeline
    discard it?

A sweep that answered with a single count per configuration would hide the shape of the answer.
A face the 320 detector never finds and a face the gate discards for extreme pose are both
"lost", but one is a detector-size problem and the other is a *capture* problem no detector size
can fix. So every miss is named by its reason (``no_face``, ``quality:too_small``,
``quality:extreme_roll``, ...), and the flip table - the frames that a bigger input size recovers
- is the direct measure of what the upgrade buys on real traffic.

READ-ONLY, DELIBERATELY
-----------------------
This tool stores nothing: no capture, no sidecar, no consent record. That is not corner-cutting -
it is what makes the sweep *runnable before consent exists*. Deciding whether to turn capture on
is exactly the decision the sweep informs, and a measurement aid that required the consent it is
measuring the value of could never be run first. The corpus's consent basis, the store's
provenance, and the gate's judgement are all unchanged by a sweep; only ``tools/corpus_admin.py
add`` files anything.

THE FOUR CONFIGURATIONS
-----------------------
320 (the shipped default), 640 (the Phase 1 upgrade), 640 with 2x2 tiling (the long-range
reach), and **SCRFD at 640** - the last one deliberately changing the *detector family* rather
than the input size.

The first three run the same YuNet model file and the same score threshold, so the sweep isolates
input size and tiling, which is the variable the deployment controls. The fourth holds the input
size where the family comparison is meaningful and swaps the network: SCRFD's stride-8 and -16
heads are the reason the wider-band claim exists at all, and whether that claim holds *here* - on
our gate frames, our exposure, our subjects - is a measurement, not something the paper it came
from can answer. Reading a detection-rate table out of a publication and planning a migration on
it is how a detector that is better on somebody else's dataset becomes a worse one on this
deployment's frames.

The configuration is appended rather than interleaved so the first three steps keep their
progression meaning, and the flip table marks the step that crosses families (``crosses_detector``)
rather than letting it read as "the same detector, a bigger input".

WHOSE FACE COUNTS (``--multi-subject``)
---------------------------------------
One question per frame is not quite enough, because two different questions are being asked of the
same number. *Would the punch path keep this frame?* refuses a frame with two faces - an ambiguity a
clock-in must not guess at - so a bystander walking behind the worker is scored as ``many_faces``.
*How far does this detector reach?* is a question about the detector, and a passer-by does not make
the detector worse.

The two questions only coincide at a camera where one person is ever in view. **2x2 tiling is
exactly the configuration that breaks that coincidence**: widening the field of view is how it
reaches smaller faces, and it sweeps in more background people in the same move. Measured under the
punch path's rule, tiling is therefore punished for doing its job - it recovers a distant face and
loses the frame to a bystander in one step, and the report reads "tiling made things worse". On this
deployment's own web-portrait corpus that effect was not subtle: tiling appeared to *halve* coverage,
and almost every loss was a ``many_faces`` it had itself introduced.

``--multi-subject`` switches the counting rule to **any detection that clears the gate**, so such a
frame counts as found and the reach question gets a straight answer. Two things keep it honest:

* it is a *measurement* mode, not the deployment's rule - the punch path's gate is unchanged, and
the report says in both the header and the JSON which rule produced the numbers, because the same
corpus measures very differently under each;
* it relaxes the *count* only. Every detection still has to clear the same gate - face size, pose,
score - so a distant speck of a bystander cannot make a frame count as covered, and the number does
not drift into "a face was somewhere in this image".

Read the two runs together: the strict run says what the pipeline would discard, the multi-subject
run says what the detector could see. The gap between them is the bystander cost of each
configuration, which is a number worth knowing before pointing a tiled detector at a public
doorway.

Matching ``DetectorSpec`` fields is deliberate: a configuration that wins the sweep is the
configuration to write into the ingest spec, so the numbers move from measurement to deployment
without a translation step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterator

#: Run as a script, this file is ``sys.path[0]``. The application modules are importable only once
#: the backend directory is on the path - the same preamble every tool here carries.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _entry in (_TOOLS_DIR, _BACKEND_DIR):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from corpus_ingest import DetectorSpec, Frame, GateConfig, assess, as_bgr  # noqa: E402
from PIL import Image  # noqa: E402

import corpus  # noqa: E402


#: The files the SCRFD configuration looks for when nobody names one: the conventional export
#: names, resolved *beside the live YuNet model* rather than at an absolute path - the detector
#: model path is already an operator-controlled location (``FACE_DETECTOR_MODEL_PATH``), and a
#: second network belongs in the same place rather than in a second place to remember.
#:
#: Both are searched, in this order. The 2.5G head is first because this deployment is CPU-only and
#: it is the smaller, faster network of the two (3.3 MB against 17 MB); the 10G head is the one the
#: original runbook named, so a checkout that already holds it keeps working. Either file is
#: measured under the same spec - the name in the report always says which one produced the
#: numbers, so a sweep can never be read as a comparison of a file it did not run.
SCRFD_MODEL_FILENAMES: Final = ("scrfd_2.5g_bnkps.onnx", "scrfd_10g_bnkps.onnx")

#: What counts as "found" when a frame holds more than one face.
#:
#: ``EXACTLY_ONE`` is the punch path's own rule: a clock-in needs one subject, so a frame with two
#: faces is an ambiguity the gate refuses rather than guesses at, and it is the default here because
#: this tool answers "would the pipeline keep this frame".
#:
#: ``ANY`` is the *measurement* mode behind ``--multi-subject``: a frame is found when **any**
#: detection passes the gate. It exists because the deployment's cameras are not studios - a
#: bystander walking behind the worker is not a detector failure, but under the punch path's rule it
#: is recorded as one, and the frames it costs are exactly the frames tiling creates (a 2x2 grid
#: widens the field of view, so more background people enter the windows). Measured under
#: ``EXACTLY_ONE``, tiling therefore looks worse the harder it works - the defect that made this mode
#: necessary: on this deployment's own web-portrait corpus, tiling "lost" half the corpus to
#: ``many_faces`` it had itself introduced.
#:
#: The mode relaxes the *count* rule only. Every detection still has to clear the same gate (face
#: size, pose, score), so a bystander too small to be a subject cannot make a frame count as found.
SUBJECT_EXACTLY_ONE: Final = "exactly_one"
SUBJECT_ANY: Final = "any"
SUBJECT_POLICIES: Final = (SUBJECT_EXACTLY_ONE, SUBJECT_ANY)

#: The configurations the sweep compares, in the order they should be reported. Named as specs so
#: the report's fingerprint and the detector actually run are the same object (see
#: ``DetectorSpec`` for why that cannot be allowed to drift).
#:
#: The fourth is SCRFD at 640 - the detector-family comparison, measured here rather than taken
#: from the paper that recommends it. It is last because the first three answer "how much input
#: size and tiling buy" and this one answers "is the other network better on our frames", and a
#: report that interleaved the two questions could not be read as either.
def default_specs() -> list[DetectorSpec]:
    """YuNet at 320, YuNet at 640, YuNet at 640 with tiling, and SCRFD at 640."""
    return [
        DetectorSpec(input_size=320, tiles=1),
        DetectorSpec(input_size=640, tiles=1),
        DetectorSpec(input_size=640, tiles=2),
        DetectorSpec(kind="scrfd", input_size=640, tiles=1),
    ]


def spec_name(spec: DetectorSpec) -> str:
    """A stable, human-readable name for one configuration, used in the flip table."""
    tiles = f"+{spec.tiles}x{spec.tiles}tiling" if spec.tiles > 1 else ""
    return f"{spec.kind}@{spec.input_size}{tiles}"


@dataclass
class CorpusStoreSource:
    """The calibration corpus *in place*, as a sweep source.

    The store's layout already is the folder contract (``<identity>/<capture>.jpg`` plus its
    sidecar), so an export works - and for the band derivation it is the documented hand-off. For a
    sweep it is the wrong first move: an export is a second copy of people's faces on disk, and this
    tool is read-only on purpose. Reading the store directly is what lets the measurement run where
    the corpus actually is (the deployment's volume) without duplicating it, and it removes a step
    that is otherwise skipped on the day someone wants the number.

    The filters default to the *opposite* of the export's, deliberately. An export destined for a
    band excludes hard cases, because a threshold fitted to hard cases is a threshold for hard
    cases; a coverage sweep wants exactly those frames, since the ones the gate flagged are the ones
    whose recoverability is the question. Everything is included unless a caller says otherwise, and
    the report records which filters were in force.
    """

    include_unlabelled: bool = True
    include_hard_cases: bool = True
    _skipped: list[tuple[str, str]] = field(default_factory=list, repr=False)

    @property
    def camera(self) -> str | None:
        """No single camera: a corpus is whatever the capture period swept up, gate by gate."""
        return None

    @property
    def skipped(self) -> list[tuple[str, str]]:
        return self._skipped

    def frames(self) -> Iterator[Frame]:
        self._skipped.clear()
        for record in corpus.sidecars():
            if record.identity is None and not self.include_unlabelled:
                continue
            if record.hard_case and not self.include_hard_cases:
                continue
            try:
                # ``convert("RGB")`` for the same reason the folder source does it: the detector
                # wants three channels, and the stored copy is not guaranteed to be one.
                image = Image.open(record.image).convert("RGB")
            except (OSError, ValueError) as exc:
                self._skipped.append((record.image, f"unreadable: {exc}"))
                continue
            yield Frame(
                image=image,
                name=os.path.basename(record.image),
                camera=record.camera,
                captured_at=record.captured_at,
                source=corpus.SOURCE_DIRECTORY,
                meta={
                    "path": record.image,
                    "identity": record.identity,
                    "capture": record.capture_id,
                    "hard_case": record.hard_case,
                },
            )

    def as_dict(self) -> dict[str, Any]:
        """What the report says about where the frames came from."""
        return {
            "kind": "calibration_store",
            "root": str(corpus.root_dir()),
            "include_unlabelled": self.include_unlabelled,
            "include_hard_cases": self.include_hard_cases,
        }


class SweepError(RuntimeError):
    """A run that cannot be trusted: a configuration that will not build, or a corpus it cannot read.

    Distinct from a per-frame failure (a frame the detector simply did not find a face in), because
    the two need different actions: one is a file path to fix, the other is a finding.
    """


@dataclass
class FrameOutcome:
    """What one configuration decided about one frame - found, or lost, and why."""

    frame: str
    found: bool
    #: Where the frame came from, when the source knows: a corpus is routinely organised one folder
    #: per identity (``angela-merkel/01.jpg``), so the *name* alone does not identify a file and two
    #: different frames can share it. Anything that joins frames across configurations must not join
    #: on the name; this is the field that can be printed instead.
    path: str | None = None
    #: The reason a face was lost: ``no_face``, ``many_faces`` (unresolvable to one subject), or a
    #: quality discard reason from the gate (``quality:too_small``, ...). ``None`` when found.
    miss_reason: str | None = None
    #: The corpus identity the frame is filed under, when the source knows one (the calibration
    #: store does; a folder of frames does not). It is what turns a coverage figure into "which
    #: worker is the far one": nine misses among four people is a different finding from nine among
    #: nine.
    identity: str | None = None
    #: How many detections the raw detector returned, before any subject rule. Kept whatever the
    #: outcome, because it is the denominator of the tuning question: a configuration that finds
    #: three faces where another finds one is reaching further, and only this number shows it.
    faces: int = 0
    #: The size and score of the face the policy credited - the subject. Under
    #: ``SUBJECT_EXACTLY_ONE`` that is the largest (and only) detection; under ``SUBJECT_ANY`` the
    #: largest detection that cleared the gate. When nothing cleared it, these carry the largest
    #: candidate's numbers, so a ``quality:too_small`` row still says how small.
    face_px: float | None = None
    score: float | None = None
    detect_ms: float = 0.0


@dataclass
class SweepOutcome:
    """One configuration's verdict over the whole corpus."""

    spec: DetectorSpec
    name: str
    outcomes: list[FrameOutcome] = field(default_factory=list)

    @property
    def found(self) -> int:
        return sum(1 for o in self.outcomes if o.found)

    @property
    def discarded(self) -> int:
        return sum(1 for o in self.outcomes if not o.found)

    def misses_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            if not o.found:
                counts[o.miss_reason] = counts.get(o.miss_reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def total_detect_ms(self) -> float:
        return sum(o.detect_ms for o in self.outcomes)


def sweep_frame(
    image: Image.Image,
    specs: list[DetectorSpec],
    *,
    detectors: dict[str, Any] | None = None,
    gate: GateConfig | None = None,
    subject: str = SUBJECT_EXACTLY_ONE,
) -> list[FrameOutcome]:
    """Run one frame through every configuration and report what each did with it.

    ``detectors`` lets a caller hand in pre-built detector callables keyed by
    :func:`spec_name` - what the CLI builds once and reuses across frames. The gate is the same
    quality judgment ingestion applies, because "the detector found it" is not the question;
    "would the pipeline keep it" is.

    ``subject`` decides what a frame with more than one face counts as; see
    :data:`SUBJECT_ANY` for why the sweep measures it both ways.
    """
    if subject not in SUBJECT_POLICIES:
        # A typo'd policy would otherwise fall through to the strict rule and quietly report a
        # multi-subject run's strict numbers under a multi-subject heading.
        raise SweepError(
            f"unknown subject policy {subject!r}; expected one of {', '.join(SUBJECT_POLICIES)}"
        )
    active = gate or GateConfig()
    prepared = corpus.prepare(image)
    frame_bgr = as_bgr(prepared.frame)
    outcomes: list[FrameOutcome] = []
    for spec in specs:
        name = spec_name(spec)
        built = (detectors or {}).get(name)
        if built is None:
            built = spec.build()
        started = time.perf_counter()
        try:
            found = built(frame_bgr)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not end the sweep
            outcomes.append(FrameOutcome(frame="", found=False, miss_reason=f"detector_error:{exc}"))
            continue
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        outcome = FrameOutcome(frame="", found=False, detect_ms=elapsed_ms, faces=len(found))
        if not found:
            outcome.miss_reason = "no_face"
        elif len(found) > 1 and subject == SUBJECT_EXACTLY_ONE:
            # Unresolvable to one subject is a real loss for a gate frame: the largest is the
            # subject, but two faces of near-equal size is an ambiguity a corpus must not guess.
            outcome.miss_reason = "many_faces"
        else:
            # Largest first, either way. Under ``SUBJECT_ANY`` the largest detection that clears
            # the gate becomes the subject, which keeps a bystander from being credited over the
            # worker standing in front of the camera - and keeps the reported ``face_px`` the size
            # of the subject rather than of whichever head the detector happened to emit first.
            # The scan stops at the first pass, so a frame full of bystanders costs no more than
            # a frame with one face.
            candidates = (sorted(found, key=lambda item: -item.area())
                          if subject == SUBJECT_ANY else [max(found, key=lambda i: i.area())])
            chosen = None
            largest_failure = None
            for item in candidates:
                quality = assess(box=item.box, score=item.score, landmarks=item.landmarks,
                                 gate=active)
                if not quality.discard:
                    chosen = quality
                    break
                if largest_failure is None:
                    largest_failure = quality
            verdict = chosen or largest_failure
            outcome.face_px = round(verdict.face_px, 2)
            outcome.score = round(verdict.score, 4)
            if chosen is None:
                outcome.miss_reason = f"quality:{largest_failure.reason}"
            else:
                outcome.found = True
        outcomes.append(outcome)
    return outcomes


def sweep(
    source,
    specs: list[DetectorSpec] | None = None,
    *,
    gate: GateConfig | None = None,
    progress=None,
    skipped_configs: list[dict[str, Any]] | None = None,
    subject: str = SUBJECT_EXACTLY_ONE,
    source_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The sweep over a whole corpus. Read-only; returns a JSON-shaped report.

    ``source`` is anything with ``frames()`` and ``skipped`` (the same protocol ingestion reads),
    so a folder of real gate frames and a camera can both be swept without this module caring
    which. ``progress`` is an optional callable invoked as ``(name, index, total)`` for a CLI
    that wants a live line.

    ``subject`` is the counting rule (see :data:`SUBJECT_ANY`); it travels in the report because
    a found-count means nothing without it - the same corpus measures ~50% or ~97% depending on
    which rule is in force, and a number that could be either is not a measurement.

    ``skipped_configs`` names configurations a caller deliberately left out, with the reason -
    the one case being a detector family whose model file is not on this machine. It travels in
    the report rather than only to the terminal, because a JSON that lists three configurations
    and says nothing about the fourth reads as "the comparison was run and SCRFD lost" instead
    of "SCRFD was never measured here".
    """
    active = gate or GateConfig()
    specs = list(specs) if specs is not None else default_specs()
    #: Built lazily on the first frame: a sweep of an unreadable or empty corpus must not pay for
    #: loading a detector model it will never use (and the CLI refuses a missing model before this
    #: point, so the error an operator sees is still the useful one).
    built: dict[str, Any] = {}
    outcomes: dict[str, list[FrameOutcome]] = {spec_name(s): [] for s in specs}
    frames_read = 0
    unreadable = 0

    frames = list(source.frames())
    for index, frame in enumerate(frames):
        if not built and frames:
            for spec in specs:
                name = spec_name(spec)
                try:
                    built[name] = spec.build()
                except Exception as exc:  # noqa: BLE001 - named, because the fix is a file path
                    # A configuration that cannot be built is not a per-frame miss: reporting it
                    # as ``detector_error`` per frame would bury "the SCRFD file you pointed at is
                    # not a SCRFD export" under forty identical rows. Named here, with the spec
                    # that failed and the reason the backend gave, so the operator knows which
                    # file to replace.
                    raise SweepError(
                        f"the {name} configuration could not be built: {exc}"
                    ) from exc
        frames_read += 1
        if progress is not None:
            progress(frame.name, index + 1, len(frames))
        try:
            prepared = corpus.prepare(frame.image)
        except (corpus.CorpusError, OSError, ValueError):
            unreadable += 1
            continue
        frame_outcomes = sweep_frame(
            prepared.frame, specs, detectors=built, gate=active, subject=subject
        )
        meta = getattr(frame, "meta", None) or {}
        source_path = meta.get("path")
        source_identity = meta.get("identity")
        for spec, outcome in zip(specs, frame_outcomes):
            outcome.frame = frame.name
            outcome.path = str(source_path) if source_path else None
            outcome.identity = str(source_identity) if source_identity else None
            outcomes[spec_name(spec)].append(outcome)

    by_name = {spec_name(s): s for s in specs}
    # Every configuration saw the same frames in the same order, so one list answers for all of
    # them. Reported because a corpus laid out one folder per identity collides constantly on
    # basenames, and a reader who joins the JSON by ``frame`` alone would silently join frames.
    every_frame = next(iter(outcomes.values())) if outcomes else []
    duplicate_frame_names = len(every_frame) - len({o.frame for o in every_frame})
    results = {
        name: {
            "spec": by_name[name].fingerprint(),
            "name": name,
            "frames": frames_read - unreadable,
            "found": sum(1 for o in outcomes[name] if o.found),
            "discarded": sum(1 for o in outcomes[name] if not o.found),
            "misses_by_reason": _counts(outcomes[name]),
            #: Frames the detector put more than one face in. Under ``exactly_one`` these are the
            #: ``many_faces`` losses; under ``any`` they are the frames the relaxed rule saves,
            #: which is the number that answers "how much of tiling's apparent loss was
            #: bystanders?".
            "multi_face_frames": sum(1 for o in outcomes[name] if o.faces > 1),
            "bystander_frames": sum(1 for o in outcomes[name] if o.faces > 1 and o.found),
            "total_detect_ms": round(sum(o.detect_ms for o in outcomes[name]), 1),
            "outcomes": [
                {
                    "frame": o.frame,
                    "path": o.path,
                    "identity": o.identity,
                    "found": o.found,
                    "miss_reason": o.miss_reason,
                    "faces": o.faces,
                    "face_px": o.face_px,
                    "score": o.score,
                    "detect_ms": round(o.detect_ms, 2),
                }
                for o in outcomes[name]
            ],
        }
        for name in by_name
    }
    flips = flip_table(results, specs)
    return {
        "frames_read": frames_read,
        "unreadable": unreadable,
        "duplicate_frame_names": duplicate_frame_names,
        "identities": sorted({o.identity for o in every_frame if o.identity}),
        # Where the frames came from, recorded because it changes what the numbers *mean*: a corpus
        # of the deployment's own captures and a folder of somebody else's photographs answer
        # different questions with the same table.
        "source": source_info,
        "gate": active.as_dict(),
        # The counting rule these found-counts were taken under. Same reasoning as the gate: a
        # coverage figure is a claim about a *rule*, and the two rules differ by large margins on
        # exactly the frames this tool was built to study.
        "subject": subject,
        # Which *weights* produced each family's numbers. The spec fingerprint in every config
        # already carries the file's name and digest; this is the same fact in one place, because
        # the comparison across two networks is only meaningful next to both files it measured.
        "models": {
            kind: sorted({
                str(s.model_path) for s in specs if s.kind == kind and s.model_path is not None
            })
            for kind in sorted({s.kind for s in specs})
        },
        "configs": {spec_name(s): results[spec_name(s)] for s in specs},
        "flips": flips,
        "skipped_configs": skipped_configs,
    }


def resolve_specs(
    *,
    detector_model: str | None = None,
    scrfd_model: str | None = None,
    skip_scrfd: bool = False,
    min_score: float = 0.6,
) -> tuple[list[DetectorSpec], list[dict[str, Any]], dict[str, str]]:
    """The configurations to run, with each family's model file resolved.

    Shared by the command line and the standing report on purpose: two places that each decide
    which files the four configurations run would eventually disagree, and a report whose numbers
    came from different weights than the other report's is precisely the comparison nobody can
    make. Raises :class:`SweepError` naming what was looked for; the caller adds its own next step,
    because the flags to pass are a command line's business and not a watcher's.

    Returns ``(specs, skipped_configs, models)``.
    """
    import face_detector

    if not detector_model:
        resolved = Path(face_detector.model_path())
        if not resolved.exists():
            raise SweepError(
                f"the live pipeline's detector model is missing: {resolved}"
            )
        detector_model = str(resolved)

    # Per family, because the fourth configuration is a different network: pointing every spec at
    # the YuNet file would run YuNet four times under a SCRFD label - a comparison that reports
    # the answer it was asked not to assume.
    models = {"yunet": detector_model}
    skipped_configs: list[dict[str, Any]] = []
    found = scrfd_model or os.environ.get("SCRFD_MODEL_PATH") or ""
    if not found:
        for filename in SCRFD_MODEL_FILENAMES:
            conventional = Path(detector_model).with_name(filename)
            if conventional.exists():
                found = str(conventional)
                break
    if skip_scrfd:
        spec = DetectorSpec(kind="scrfd", input_size=640, tiles=1)
        skipped_configs.append(
            {"name": spec_name(spec), "reason": "--skip-scrfd was passed", "spec": spec.fingerprint()}
        )
    elif not found or not Path(found).exists():
        looked_for = " or ".join(
            filename + f" (beside {detector_model})" for filename in SCRFD_MODEL_FILENAMES
        )
        raise SweepError(
            "the SCRFD model is missing, so the detector comparison cannot be measured:\n"
            f"  looked for {found or looked_for}"
        )
    else:
        models["scrfd"] = found

    specs = [
        DetectorSpec(
            kind=spec.kind,
            model_path=models[spec.kind],
            input_size=spec.input_size,
            tiles=spec.tiles,
            overlap=spec.overlap,
            square=spec.square,
            score_threshold=min_score,
        )
        for spec in default_specs()
        if spec.kind in models
    ]
    return specs, skipped_configs, models


def _counts(outcomes: list[FrameOutcome]) -> dict[str, int]:
    """Miss reasons for one configuration, largest first."""
    counts: dict[str, int] = {}
    for o in outcomes:
        if not o.found:
            counts[o.miss_reason] = counts.get(o.miss_reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def flip_table(results: dict[str, Any], specs: list[DetectorSpec]) -> list[dict[str, Any]]:
    """Frames one configuration recovers that the previous one loses - what the upgrade buys.

    Configurations are compared in the order given (the shipped default first), so the table
    reads as a progression: each row answers "moving from the previous configuration to this
    one, which frames come back?" - the direct, frame-level measure of the coverage claim.

    The two configurations are paired **by position, not by frame name**. Every configuration sees
    the same frames in the same order and appends one outcome per frame, so position is exact - and
    the name is not: a corpus is routinely laid out one folder per identity (``angela-merkel/01.jpg``,
    ``barack-obama/01.jpg``), where a dozen different frames share the name ``01.jpg``. Joining on
    the name collapsed them into one row, which under-reported every flip (a 48-frame corpus of 12
    identities reported 12 frames' worth of movement) without any error to notice.

    ``crosses_detector`` marks the row where the *network* changes rather than the input size.
    Two configurations of the same family differ by one variable the deployment controls, and a
    flip there is a tuning result; two families differ by the weights themselves, and a flip
    there is the detector comparison. Without the flag both rows read identically, and a reader
    planning a migration would take a tiling gain for evidence about SCRFD.
    """
    rows: list[dict[str, Any]] = []
    for previous, current in zip(specs[:-1], specs[1:]):
        prev_name, curr_name = spec_name(previous), spec_name(current)
        prev_outcomes = results[prev_name]["outcomes"]
        curr_outcomes = results[curr_name]["outcomes"]
        recovered = [
            {
                "frame": o["frame"],
                **({"path": o["path"]} if o.get("path") else {}),
                **({"face_px": o["face_px"]} if o.get("face_px") else {}),
                # The detection count, when it is more than one: a frame recovered with a crowd
                # in it was recovered by a reach gain, not by resolving an ambiguity, and under
                # ``--multi-subject`` that distinction is the whole measurement.
                **({"faces": o["faces"]} if o.get("faces", 0) > 1 else {}),
            }
            for before, o in zip(prev_outcomes, curr_outcomes)
            if not before["found"] and o["found"]
        ]
        lost = [
            (o.get("path") or o["frame"])
            for before, o in zip(prev_outcomes, curr_outcomes)
            if before["found"] and not o["found"]
        ]
        rows.append(
            {
                "from": prev_name,
                "to": curr_name,
                "crosses_detector": previous.kind != current.kind,
                "from_kind": previous.kind,
                "to_kind": current.kind,
                "recovered": recovered,
                "recovered_count": len(recovered),
                "lost": lost,
                "net": len(recovered) - len(lost),
            }
        )
    return rows


def render(report: dict[str, Any]) -> str:
    """The report as an operator reads it: one line per configuration, then the flips."""
    lines: list[str] = []
    lines.append(f"frames swept: {report['frames_read']}"
                 + (f" ({report['unreadable']} unreadable)" if report["unreadable"] else ""))
    source = report.get("source") or {}
    if source:
        lines.append(f"  source: {source.get('kind')} at {source.get('root')}")
    identities = report.get("identities") or []
    if identities:
        shown = ", ".join(identities[:8]) + (", ..." if len(identities) > 8 else "")
        lines.append(f"  identities: {len(identities)} ({shown})")
    # Which rule the found-counts were taken under. The two rules part company on precisely the
    # frames this tool exists to study, so a percentage without its rule beside it is not a
    # coverage figure - it is a number that could mean "would the gate keep this frame" or
    # "is there a face in it somewhere".
    subject = report.get("subject", SUBJECT_EXACTLY_ONE)
    if subject == SUBJECT_ANY:
        lines.append(
            "  subject rule: ANY - a face counts whenever any detection clears the gate; bystander "
            "frames count as covered. This measures detector reach, not the punch path's rule."
        )
    else:
        lines.append(
            "  subject rule: ONE - a frame must resolve to a single subject, as the punch path "
            "requires; several faces is a many_faces loss. Use --multi-subject to measure reach."
        )
    if report.get("duplicate_frame_names"):
        lines.append(
            f"  note: {report['duplicate_frame_names']} frame names repeat across this corpus "
            "(one folder per identity does this); frames are paired by position, and each flip "
            "row names its file, not just its basename."
        )
    for name, config in report["configs"].items():
        share = config["found"] / config["frames"] * 100 if config["frames"] else 0.0
        lines.append(
            f"  {name:28s} found {config['found']:4d}/{config['frames']:4d} ({share:5.1f}%)"
            f"  misses: {config['misses_by_reason'] or 'none'}"
            f"  [{config['total_detect_ms']:.0f} ms total]"
        )
        if config.get("multi_face_frames"):
            saved = config.get("bystander_frames", 0)
            lines.append(
                f"      multi-face frames: {config['multi_face_frames']}"
                + (f" ({saved} counted by the any-face rule)" if subject == SUBJECT_ANY else
                   " (all lost to the one-subject rule)")
            )
    for kind, paths in (report.get("models") or {}).items():
        lines.append(f"  {kind} weights: {', '.join(paths) if paths else '(unset)'}")
    for flip in report["flips"]:
        lines.append(
            f"  {flip['from']} -> {flip['to']}: +{flip['recovered_count']} recovered"
            + (f", -{len(flip['lost'])} lost" if flip["lost"] else "")
            + (f"  (net {flip['net']:+d})" if flip["net"] else "")
            # The one row that is a detector comparison rather than a tuning step. Said out
            # loud so the numbers under it are not read as one network's behaviour at two
            # input sizes.
            + (f"  [changes detector: {flip['from_kind']} -> {flip['to_kind']}]"
               if flip.get("crosses_detector") else "")
        )
        for entry in flip["recovered"][:5]:
            px = f" (face {entry['face_px']}px)" if entry.get("face_px") else ""
            crowd = f", {entry['faces']} faces" if entry.get("faces") else ""
            # Prefer the path: on a corpus laid out one folder per identity, ``03.jpg`` does not
            # say which file to open.
            lines.append(f"    + {entry.get('path') or entry['frame']}{px}{crowd}")
        for name in flip["lost"][:5]:
            lines.append(f"    - {name}")
        hidden = flip["recovered_count"] - 5
        if hidden > 0:
            lines.append(f"    ... and {hidden} more recovered")
    for entry in report.get("skipped_configs") or []:
        lines.append(f"  {entry['name']}: NOT MEASURED - {entry['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one corpus of gate frames through four detector configurations (YuNet at 320, "
            "640, 640+tiling, and SCRFD at 640) and report what each discards. Read-only: stores "
            "nothing, needs no consent. Counts under the punch path's one-subject rule by default; "
            "pass --multi-subject when the corpus has bystanders in it."
        )
    )
    parser.add_argument("--corpus", default=None, help="folder of gate frames to sweep")
    parser.add_argument("--corpus-store", action="store_true",
                        help="sweep the calibration corpus in place instead of a folder: one command "
                             "where the corpus lives (the deployment's volume), and no second copy "
                             "of people's faces written to disk just to measure them")
    parser.add_argument("--detector-model", default=None,
                        help="YuNet model file (default: the live pipeline's own, resolved the way "
                             "face_detector resolves it)")
    parser.add_argument("--scrfd-model", default=None,
                        help="SCRFD ONNX file for the fourth configuration (default: "
                             "$SCRFD_MODEL_PATH, else the conventional name beside the YuNet model)")
    parser.add_argument("--skip-scrfd", action="store_true",
                        help="drop the SCRFD configuration instead of measuring it; the report "
                             "records it as not measured, so a three-line sweep cannot be mistaken "
                             "for a comparison")
    parser.add_argument("--multi-subject", action="store_true",
                        help="count a frame as found when ANY detection clears the gate, instead "
                             "of requiring a single subject: the mode for cameras with bystanders, "
                             "because a passer-by is otherwise scored as a detector failure - and "
                             "tiling creates those frames itself by widening the field of view, so "
                             "the strict rule makes it look worse the harder it works")
    parser.add_argument("--json", default=None, help="write the full report here")
    parser.add_argument("--min-score", type=float, default=0.6,
                        help="detector score threshold, applied identically to every configuration")
    parser.add_argument("--exclude-unlabelled", action="store_true",
                        help="with --corpus-store: measure only captures that have an identity; off "
                             "by default, because an unlabelled frame is still a frame the detector "
                             "either finds a face in or does not")
    parser.add_argument("--exclude-hard-cases", action="store_true",
                        help="with --corpus-store: drop the captures the gate flagged. Off by "
                             "default - the flagged frames are the ones whose recoverability a "
                             "coverage experiment is about")
    args = parser.parse_args(argv)

    if bool(args.corpus) == bool(args.corpus_store):
        print(
            "pass exactly one source: --corpus <folder> for a folder of frames, or "
            "--corpus-store to sweep the calibration corpus in place.",
            file=sys.stderr,
        )
        return 2

    source_info: dict[str, Any]
    if args.corpus_store:
        source = CorpusStoreSource(
            include_unlabelled=not args.exclude_unlabelled,
            include_hard_cases=not args.exclude_hard_cases,
        )
        source_info = source.as_dict()
    else:
        root = Path(args.corpus)
        if not root.is_dir():
            print(f"corpus folder not found: {root}", file=sys.stderr)
            return 2
        source_info = {"kind": "folder", "root": str(root)}

    try:
        specs, skipped_configs, _models = resolve_specs(
            detector_model=args.detector_model,
            scrfd_model=args.scrfd_model,
            skip_scrfd=args.skip_scrfd,
            min_score=args.min_score,
        )
    except SweepError as exc:
        # Refused rather than dropped in silence: the whole reason the fourth configuration exists
        # is that the detector comparison has to be measured here. Falling back to three lines
        # would print a report that looks like one network won a contest it never entered.
        print(str(exc), file=sys.stderr)
        if "SCRFD model is missing" in str(exc):
            print(
                "Put the ONNX file there, pass --scrfd-model /path/to/scrfd.onnx, set "
                "SCRFD_MODEL_PATH, or pass --skip-scrfd to measure only the YuNet configurations "
                "(the report will say SCRFD was not measured).",
                file=sys.stderr,
            )
        else:
            print("pass --detector-model to name the YuNet file", file=sys.stderr)
        return 2

    if not args.corpus_store:
        from corpus_ingest import DirectorySource

        source = DirectorySource(Path(args.corpus))

    def progress(name: str, index: int, total: int) -> None:
        print(f"\r  sweeping {index}/{total}: {name[:60]}", end="", flush=True)

    subject = SUBJECT_ANY if args.multi_subject else SUBJECT_EXACTLY_ONE
    try:
        report = sweep(source, specs, progress=progress, skipped_configs=skipped_configs,
                       subject=subject, source_info=source_info)
    except SweepError as exc:
        # A configuration that will not build is the operator's next step, not a stack trace:
        # the commonest case by far is a ``--scrfd-model`` pointing at the wrong file.
        print(str(exc), file=sys.stderr)
        return 2
    print("\r" + " " * 79 + "\r", end="")  # clear the progress line
    if args.corpus_store and not report["frames_read"]:
        # An empty *folder* is a legitimate thing to sweep. An empty corpus store is almost always
        # the reason the operator is here: capture was never switched on, the volume is not mounted,
        # or the process is reading a different database. Printing four 0/0 lines would look like a
        # result from four configurations that measured nothing.
        print(
            "the calibration corpus is empty - 0 captures - so there is nothing to sweep.\n"
            f"  corpus root : {source_info.get('root')}\n"
            "Capture has to be switched on and left on for a few days before it holds anything:\n"
            "  CALIBRATION_CAPTURE_ENABLED=true, CALIBRATION_CORPUS_DIR on the volume\n"
            "  python tools/corpus_admin.py stats   # what the store holds, and what a band needs\n"
            "Or sweep frames that already exist: --corpus <folder of images>.",
            file=sys.stderr,
        )
        return 2
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"\nfull report: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
