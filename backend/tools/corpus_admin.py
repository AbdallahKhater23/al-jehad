"""Operator command for the labelled corpus: capture frames, label them, export them, erase them.

WHY THIS FILE IS NOT CALLED ``corpus.py``
----------------------------------------
Because every tool here puts its own directory on ``sys.path`` (``contract_ab`` and
``derive_facenet_band`` both do, to import their neighbours), and ``tools/corpus.py`` would then
shadow the application's own ``backend/corpus.py`` for every later ``import corpus`` in the process -
including the punch path's capture hook, which would silently get a command-line module instead of the
store. It was called that for one afternoon.

WHY A CLI AND NOT A SCREEN
--------------------------
The corpus is measurement material, not a product surface. Every action here is either an operator's
deliberate act (pulling frames from a gate camera, importing a folder of staff photographs, labelling a
quarantine capture, purging a period) or a maintenance one (exporting for a tool run), and none of them
belongs behind a route that a worker, a manager or a curious administrator could reach. A screen would
also have to solve "who may see these faces", and the answer here is "whoever has the shell and the
directory", which is a claim a deployment can actually enforce.

**This command contains no ingestion logic of its own.** It parses arguments, prints, and calls
``corpus_ingest``. That is deliberate: the failure worth engineering against is not a missing feature,
it is *two* ingestion paths - the folder importer's idea of a usable capture and the camera puller's -
drifting apart until nobody can say which rule produced the corpus a threshold was fitted to.

WHAT IT DOES, IN THE ORDER A ROLLOUT USES IT
--------------------------------------------
    # live gates: detect on the stored copy, judge quality, file under the identity
    venv/Scripts/python.exe tools/corpus_admin.py ingest --camera rtsp://10.0.0.9/gate \\
        --camera-name gate-north --identity W-1042 --consent "deployment notice 2026-08" \\
        --actor "R. Ops" --limit 60 --every 25

    # enrol: bind a captured reference set to a worker id that exists in the roster
    venv/Scripts/python.exe tools/corpus_admin.py enroll --identity W-1042 \\
        --source ./session-2026-09-22 --check-roster --consent "signed form 2026-09-22" \\
        --actor "R. Ops"

    # a controlled sitting, where every frame is one the operator meant to take
    venv/Scripts/python.exe tools/corpus_admin.py add --source ./lab-2026-09 \\
        --consent "staff calibration session, signed form 2026-09-22" --actor "R. Ops" \\
        --input-size 640 --tiles 2

    venv/Scripts/python.exe tools/corpus_admin.py list --unlabelled
    venv/Scripts/python.exe tools/corpus_admin.py label --capture 8f3a… --identity "Bilal Khan"
    venv/Scripts/python.exe tools/corpus_admin.py stats
    venv/Scripts/python.exe tools/corpus_admin.py export --destination ./corpus-export

    # and the two tools that consume it, which will read the *stored* crops:
    venv/Scripts/python.exe tools/contract_ab.py --corpus ./corpus-export
    venv/Scripts/python.exe tools/derive_facenet_band.py --corpus ./corpus-export

    # erasure, dry run first, always
    venv/Scripts/python.exe tools/corpus_admin.py purge --older-than-days 180
    venv/Scripts/python.exe tools/corpus_admin.py purge --identity "Bilal Khan" --apply

``add`` is the *controlled sitting*: the folder is the material, every frame in it is one somebody chose
to take, and the gate is permissive so a dim or off-angle shot is kept and measured rather than dropped.
``ingest`` is the *camera*: frames arrive whether or not they are any good, so the gate does its work -
discarding what is not evidence of anything, and keeping-and-flagging the rest so a coverage experiment
has the hard cases to look at. The identity of an imported image comes from the **folder contract** the
band tool already uses (see ``derive_facenet_band``): a subdirectory is the identity, a flat
``name_1.jpg`` is split on its trailing number. A folder that carries no identity is imported
**unlabelled** - into the same store, visible in ``list --unlabelled`` and ``stats`` - because a corpus
that cannot represent "we have not decided who this is yet" forces somebody to guess at import time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _entry in (_TOOLS_DIR, _BACKEND_DIR):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

#: A stated basis for an operator import. The store refuses a capture with no consent string, so the
#: tool has one to offer - and it names the actor, because "somebody imported these" is not a basis.
IMPORT_CONSENT_TEMPLATE = "operator import by {actor}"


def _load() -> tuple[Any, Any, Any]:
    """Imported lazily so ``--help`` costs nothing and a missing model is not a startup failure."""
    import corpus
    import detector_640
    from derive_facenet_band import collect, identity_of

    return corpus, detector_640, (collect, identity_of)


def _spec(args: argparse.Namespace) -> Any:
    """The detector *spec*: which model, at which input size, with which tiling - the crop, bound.

    A spec rather than a built detector, so the object that runs and the fingerprint stored on every
    capture come from one place; see ``corpus_ingest.DetectorSpec``.
    """
    import face_detector

    import corpus_ingest

    model = Path(args.model) if args.model else Path(face_detector.model_path())
    if not model.exists():
        raise SystemExit(f"detector model not found: {model}")
    return corpus_ingest.DetectorSpec(
        kind=args.detector,
        model_path=model,
        input_size=args.input_size,
        tiles=args.tiles,
        overlap=args.overlap,
        square=args.square,
        score_threshold=getattr(args, "score_threshold", 0.6),
    )


def _gate(args: argparse.Namespace) -> Any:
    import corpus_ingest

    if getattr(args, "gate", "typical") == "permissive":
        return corpus_ingest.GateConfig.permissive()
    return corpus_ingest.GateConfig()


def _source(args: argparse.Namespace, *, identity: str | None = None) -> Any:
    """The frame source the arguments describe: a folder of images, or a camera/RTSP stream."""
    import corpus_ingest

    somewhere = getattr(args, "camera", None)
    if somewhere:
        target: Any = int(somewhere) if str(somewhere).isdigit() else str(somewhere)
        return corpus_ingest.CameraSource(
            target,
            camera=getattr(args, "camera_name", None),
            limit=getattr(args, "limit", None) or None,
            every=getattr(args, "every", 1) or 1,
        )
    folder = getattr(args, "source", None)
    if not folder:
        raise SystemExit("give --source <folder> or --camera <index|rtsp-url>")
    return corpus_ingest.DirectorySource(folder, camera=getattr(args, "camera_name", None))


def _print_skips(notes: list[dict[str, Any]], limit: int = 20) -> None:
    if not notes:
        return
    print("skipped:", file=sys.stderr)
    for item in notes[:limit]:
        detail = f": {item['detail']}" if item.get("detail") else ""
        print(f"  - {item['frame']} ({item['reason']}{detail})", file=sys.stderr)
    if len(notes) > limit:
        print(f"  ... and {len(notes) - limit} more", file=sys.stderr)


def _print_stored(records: list[Any], *, quiet: bool) -> None:
    if quiet:
        return
    for record in records:
        flags = ",".join(record.flags) or "-"
        camera = record.camera or "-"
        print(
            f"{record.capture_id}  {record.identity or '(unlabelled)':<24} "
            f"face={record.face_px:6.1f}px native={record.native_face_px:6.1f}px "
            f"stored={record.stored_size[0]}x{record.stored_size[1]}  "
            f"conf={record.score if record.score is not None else float('nan'):.2f}  "
            f"[{flags}]  {camera}"
        )


# ---------------------------------------------------------------------------
# add: a controlled sitting
# ---------------------------------------------------------------------------
def cmd_add(args: argparse.Namespace) -> int:
    import corpus_ingest

    corpus, _detector_640, (collect, identity_of) = _load()

    source = Path(args.source)
    consent = args.consent or (IMPORT_CONSENT_TEMPLATE.format(actor=args.actor) if args.actor else "")
    if not consent:
        print(
            "an import needs --consent, and --actor alone is not a basis: the corpus is a biometric "
            "store, so the command that fills it has to record what was relied on (for example "
            '--consent "staff calibration session, signed form 2026-09-22" --actor "R. Ops")',
            file=sys.stderr,
        )
        return 2

    grouped = collect(source) if source.is_dir() else {}
    if not grouped:
        print(f"no images under {source} (looked for {sorted(IMAGE_SUFFIXES)})", file=sys.stderr)
        return 2
    if args.identity:
        # The whole folder is one person: the lab-session case, where a handful of shots were taken
        # in one sitting and filing them per subfolder would be filing the same label N times.
        grouped = {args.identity: [path for paths in grouped.values() for path in paths]}
    elif args.unlabelled:
        grouped = {"": [path for paths in grouped.values() for path in paths]}

    try:
        spec = _spec(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    stored: list[str] = []
    skips: list[dict[str, Any]] = []
    unreadable = 0
    budget = int(args.limit) if args.limit else None
    fingerprint: dict[str, Any] = {}
    for identity, paths in sorted(grouped.items()):
        if budget is not None and len(stored) >= budget:
            break
        # A controlled sitting: the gate is permissive. Every frame here is one the operator chose to
        # take, so dropping the dim ones would silently choose the corpus's difficulty for it - and the
        # *coverage* question (what happens at range) needs frames the embedder found hard.
        source_frames = corpus_ingest.DirectorySource(
            source, camera=getattr(args, "camera_name", None), recursive=False, paths=paths
        )
        report = corpus_ingest.ingest(
            source_frames,
            detector=spec,
            identity=identity or None,
            consent=consent,
            actor=args.actor,
            camera=getattr(args, "camera_name", None),
            gate=corpus_ingest.GateConfig.permissive(),
            choose="skip" if args.strict_single_face else "largest",
            note=f"imported from {source}",
            limit=None if budget is None else max(0, budget - len(stored)),
        )
        fingerprint = report.detector
        stored.extend(record.capture_id for record in report.stored)
        skips.extend(report.skipped)
        unreadable += len(source_frames.skipped)
        _print_stored(report.stored, quiet=args.quiet)

    report_payload = {
        "source": str(source),
        "stored": len(stored),
        "skipped": len(skips),
        "unreadable": unreadable,
        "identities": len({corpus.load(capture_id).identity for capture_id in stored}) or 0,
        "crop": corpus.crop_description(fingerprint),
        "consent": consent,
        "captures": stored,
        "skips": skips,
    }
    _print_skips(skips)
    print(f"stored {len(stored)} capture(s), skipped {len(skips)}; crop: {report_payload['crop']}")
    _maybe_write_json(args, report_payload)
    return 0


# ---------------------------------------------------------------------------
# ingest: a camera
# ---------------------------------------------------------------------------
def cmd_ingest(args: argparse.Namespace) -> int:
    import corpus_ingest

    corpus, _detector_640, _helpers = _load()

    if not args.consent:
        print(
            "ingestion needs --consent: this command fills a biometric store from a live camera, so "
            "the command itself records what it relied on. If collection is switched off in the "
            "deployment, set CALIBRATION_CAPTURE_ENABLED too - the switch and the basis are two "
            "different statements.",
            file=sys.stderr,
        )
        return 2
    try:
        spec = _spec(args)
        source = _source(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        report = corpus_ingest.ingest(
            source,
            detector=spec,
            identity=None if args.unlabelled else args.identity,
            consent=args.consent,
            actor=args.actor,
            camera=args.camera_name,
            gate=_gate(args),
            choose=args.multi_face,
            keep_hard_cases=not args.drop_hard_cases,
            note=f"ingest from {args.camera or args.source}",
        )
    except corpus_ingest.IngestError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    _print_stored(report.stored, quiet=args.quiet)
    _print_skips(report.skipped)
    print(report.summary())
    print(f"crop: {corpus.crop_description(report.detector)}")
    hard = report.hard_cases
    if hard:
        print(
            f"{len(hard)} capture(s) are hard cases: kept and flagged, and left out of an export "
            "unless --include-hard-cases is asked for."
        )
    if args.coverage:
        coverage = corpus_ingest.coverage(expected_face_px=args.expected_face_px, gate=_gate(args))
        for key in ("captures", "min_px", "median_px", "max_px", "below_gate_share"):
            if key in coverage:
                print(f"coverage {key}: {coverage[key]}")
        if coverage.get("verdict"):
            print(f"coverage verdict: {coverage['verdict']}")
        report_payload_extra = {"coverage": coverage}
    else:
        report_payload_extra = {}
    payload = {**report.as_dict(), **report_payload_extra}
    _maybe_write_json(args, payload)
    return 0


# ---------------------------------------------------------------------------
# enroll: a worker id against a captured reference set
# ---------------------------------------------------------------------------
def _roster_lookup() -> Any:
    """A read-only lookup into the deployment's own roster, for ``--check-roster``.

    Read-only and deliberately *not* wired into the store: this asks "does this worker id exist", and
    the answer changes nothing about the capture. The alternative - writing a foreign key from a corpus
    record to a users row - would make erasing attendance history a question about the corpus too, and
    is exactly the coupling ``corpus``'s docstring argues against.
    """

    def lookup(worker_id: str) -> Any:
        import database

        found = database.rows("SELECT id, name, role FROM users WHERE id = ?", (str(worker_id),))
        return dict(found[0]) if found else None

    return lookup


def cmd_enroll(args: argparse.Namespace) -> int:
    import corpus_ingest

    corpus, _detector_640, _helpers = _load()

    consent = args.consent or (IMPORT_CONSENT_TEMPLATE.format(actor=args.actor) if args.actor else "")
    if not consent:
        print(
            "enrolment needs --consent: a reference set is biometric material and the record has to "
            "say what it is held under",
            file=sys.stderr,
        )
        return 2
    try:
        spec = _spec(args)
        source = _source(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    roster = _roster_lookup() if args.check_roster else None
    if roster is not None:
        record = roster(args.identity)
        if record is None:
            print(
                f"no worker {args.identity!r} in the roster: refusing to enrol. A corpus labelled "
                "with an id that does not exist is a pair count nobody can reconcile later.",
                file=sys.stderr,
            )
            return 2
        if record.get("role") != "worker":
            # Not a refusal: supervisors sit for calibration too, and the corpus is about faces rather
            # than privileges. But it is worth printing, because "the reference set is filed under an
            # administrator's account" is the kind of thing that is obvious only in hindsight.
            print(
                f"note: {args.identity} has role {record.get('role')!r}, not 'worker' "
                f"({record.get('name')})"
            )
        else:
            print(f"roster: {args.identity} = {record.get('name')}")

    try:
        report = corpus_ingest.enroll(
            source,
            identity=args.identity,
            detector=spec,
            consent=consent,
            actor=args.actor,
            camera=args.camera_name,
            gate=_gate(args),
            keep_hard_cases=not args.drop_hard_cases,
        )
    except corpus_ingest.IngestError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    _print_stored(report.stored, quiet=args.quiet)
    _print_skips(report.skipped)
    print(f"enrolled {report.summary()}")
    stats = corpus.stats()
    count = stats["identity_counts"].get(args.identity)
    if not count:
        print(
            f"note: {args.identity} has no captures in the measured set - every shot was either "
            "discarded or flagged as a hard case.",
            file=sys.stderr,
        )
    else:
        print(
            f"{args.identity}: {count} capture(s) now in the corpus; "
            f"{stats['genuine_pairs']} genuine pairs overall, {stats['impostor_pairs']} impostor "
            f"pairs ({'can' if stats['can_support_floor'] else 'cannot'} support a floor)"
        )
    _maybe_write_json(args, report.as_dict())
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    corpus, _detector_640, _helpers = _load()

    targets: list[str] = []
    if args.capture:
        targets = list(args.capture)
    elif args.all_unlabelled:
        targets = [record.capture_id for record in corpus.sidecars(unlabelled_only=True)]
        if not targets:
            print("no unlabelled captures to label")
            return 0
    else:
        print("label needs --capture <id> (repeatable) or --all-unlabelled", file=sys.stderr)
        return 2
    if not args.identity and not args.clear:
        print("label needs --identity <name>, or --clear to remove a wrong one", file=sys.stderr)
        return 2

    identity = None if args.clear else args.identity
    for capture_id in targets:
        try:
            record = corpus.label(capture_id, identity, actor=args.actor)
        except corpus.CorpusError as exc:
            print(f"{capture_id}: {exc}", file=sys.stderr)
            return 2
        print(f"{record.capture_id} -> {record.identity or '(unlabelled)'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    corpus, _detector_640, _helpers = _load()

    records = corpus.sidecars(
        identity=args.identity, unlabelled_only=args.unlabelled
    )
    if not records:
        print("no captures match")
        return 0
    for record in records:
        flags = ",".join(record.flags) or "-"
        print(
            f"{record.capture_id}  {record.identity or '(unlabelled)':<24} "
            f"{record.captured_at}  face={record.face_px:6.1f}px  native={record.native_face_px:6.1f}px  "
            f"{record.source:<8} {record.camera or '-':<16} [{flags}] "
            f"{corpus.crop_description(record.detector)}"
        )
    print(f"\n{len(records)} capture(s)")
    _maybe_write_json(args, {"captures": [record.as_dict() for record in records]})
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    corpus, _detector_640, _helpers = _load()

    payload = corpus.stats()
    print(f"captures      : {payload['captures']} ({payload['labelled']} labelled, "
          f"{payload['unlabelled']} awaiting a label)")
    print(f"identities    : {payload['identities']}"
          + (f"  (single-capture: {', '.join(payload['single_capture_identities'])})"
             if payload["single_capture_identities"] else ""))
    print(f"pairs         : genuine {payload['genuine_pairs']}, impostor {payload['impostor_pairs']}"
          f"  -> a floor needs >= 100 impostor pairs and >= 1 genuine pair")
    print(f"can derive    : {'yes' if payload['can_support_floor'] else 'no'}")
    print(f"face sizes    : {payload['face_px']}  (small-face share {payload['small_face_share']:.1%})")
    print(f"native sizes  : {payload['native_face_px']}")
    print(f"measured      : {payload['measured']} of {payload['captures']} capture(s); "
          f"hard cases left out: {payload['quality']['excluded_from_measurement']}")
    if payload["quality"]["flags"]:
        print(f"quality flags : {payload['quality']['flags']}")
    print(f"cameras       : {payload['cameras']}")
    print(f"crops         : {payload['detectors']}")
    print(f"retention     : {payload['oldest']} .. {payload['newest']}, {payload['bytes'] / 1e6:.2f} MB")
    print(f"consent       : {payload['consent']}")
    if len(payload["detectors"]) > 1:
        print(
            "\nnote: this corpus holds more than one crop configuration. A tool run that asks for one "
            "of them will refuse the other's captures rather than re-crop them silently - export and "
            "measure them separately, or re-detect deliberately with --landmarks detect."
        )
    if not payload["can_support_floor"]:
        print(
            "\nnote: not enough different-people pairs for a floor. Add identities - the impostor "
            "distribution is what a refuse line is measured against, and no amount of one person's "
            "captures substitutes for it."
        )
    _maybe_write_json(args, payload)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    corpus, _detector_640, _helpers = _load()

    try:
        report = corpus.export(
            args.destination,
            identities=args.identity,
            include_unlabelled=args.include_unlabelled,
            include_hard_cases=args.include_hard_cases,
        )
    except corpus.CorpusError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    print(f"exported {report['images']} image(s) across {report['identities']} identity(ies) "
          f"to {report['destination']}")
    if report["unlabelled_skipped"]:
        print(f"  {report['unlabelled_skipped']} unlabelled capture(s) left behind "
              "(--include-unlabelled puts them under 'unlabelled')")
    if report["hard_cases_skipped"]:
        print(f"  {report['hard_cases_skipped']} hard case(s) left behind "
              "(--include-hard-cases measures what the gate *flagged*, not what the gate *sees*)")
    print(f"  crop: {report['crop']}")
    _maybe_write_json(args, report)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    corpus, _detector_640, _helpers = _load()
    from config import settings

    days = args.older_than_days
    if days is None and not args.identity and not args.capture:
        days = int(settings.calibration_corpus_retention_days)
        print(f"no selector given: using the configured retention period ({days} days)")
    try:
        report = corpus.purge(
            older_than_days=days,
            identity=args.identity,
            capture_ids=args.capture,
            dry_run=not args.apply,
        )
    except corpus.CorpusError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    verb = "would erase" if report["dry_run"] else "erased"
    print(f"{verb} {report['captures']} capture(s), {report['bytes'] / 1e6:.2f} MB"
          + (f", identities: {', '.join(report['identities'])}" if report["identities"] else ""))
    if report["dry_run"] and report["captures"]:
        print("re-run with --apply to erase them; the frames are overwritten, not just unlinked")
    _maybe_write_json(args, {**report, "wiped": report["wiped"][:50]})
    return 0


def _maybe_write_json(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    target = getattr(args, "json", None)
    if target:
        Path(target).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {target}")


def _detector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--detector", default="yunet", choices=("yunet", "scrfd"))
    parser.add_argument("--model", default=None, help="detector ONNX path (default: the live model)")
    parser.add_argument("--input-size", type=int, default=640,
                        help="detector input edge; 640 is the coverage fix, 320 is the legacy pass")
    parser.add_argument("--tiles", type=int, default=1,
                        help="overlapping grid for the small-face band (2 = 2x2 windows)")
    parser.add_argument("--overlap", type=float, default=0.2)
    parser.add_argument("--square", action="store_true",
                        help="letterbox to a square input (needed by a fixed-shape engine)")
    parser.add_argument("--score-threshold", type=float, default=0.6,
                        help="the detector's own cut; the corpus gate has a second, softer one")
    parser.add_argument("--camera-name", default=None,
                        help="what to record as this capture's camera (never a URL with credentials)")
    parser.add_argument("--gate", default="typical", choices=("typical", "permissive"),
                        help="'permissive' keeps everything a face was found in (a controlled sitting)")
    parser.add_argument("--drop-hard-cases", action="store_true",
                        help="skip flagged captures instead of keeping them as edge cases")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus_admin.py",
        description="Capture, import, label, export and erase the labelled calibration corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="import a folder of images, detecting once per image on the stored copy")
    add.add_argument("--source", required=True, help="folder of images (subfolder = identity)")
    add.add_argument("--identity", default=None,
                     help="label every image in the folder with this one identity")
    add.add_argument("--unlabelled", action="store_true",
                     help="import without a label, to be decided later")
    add.add_argument("--consent", default=None,
                     help="the basis these captures are held under (required unless --actor is given)")
    add.add_argument("--actor", default=None, help="who is importing, recorded on every capture")
    _detector_arguments(add)
    add.add_argument("--strict-single-face", action="store_true",
                     help="skip a frame with more than one face instead of using the largest")
    add.add_argument("--limit", type=int, default=0, help="stop after this many stored captures")
    add.add_argument("--json", default=None)
    add.add_argument("--quiet", action="store_true")
    add.set_defaults(handler=cmd_add)

    ingest = sub.add_parser(
        "ingest", help="pull frames from a camera or RTSP stream and file the ones worth measuring"
    )
    ingest.add_argument("--source", default=None, help="folder of images instead of a camera")
    ingest.add_argument("--camera", default=None, help="webcam index or rtsp:// URL")
    ingest.add_argument("--identity", default=None, help="the worker these frames belong to")
    ingest.add_argument("--unlabelled", action="store_true",
                        help="file them for a human to decide (the default when --identity is absent)")
    ingest.add_argument("--consent", default=None, help="the basis these captures are held under")
    ingest.add_argument("--actor", default=None)
    _detector_arguments(ingest)
    ingest.add_argument("--multi-face", default="largest", choices=("largest", "skip"),
                        help="which face a frame with several is filed under")
    ingest.add_argument("--limit", type=int, default=0, help="stop after this many stored captures")
    ingest.add_argument("--every", type=int, default=1, help="sample the stream: keep 1 frame in N")
    ingest.add_argument("--coverage", action="store_true",
                        help="report the face-size distribution next to the gate's own line")
    ingest.add_argument("--expected-face-px", type=float, default=None,
                        help="the face size this corpus is meant to model, for the coverage verdict")
    ingest.add_argument("--json", default=None)
    ingest.add_argument("--quiet", action="store_true")
    ingest.set_defaults(handler=cmd_ingest)

    enroll = sub.add_parser(
        "enroll", help="bind a captured reference set to a worker id, optionally checking the roster"
    )
    enroll.add_argument("--identity", required=True, help="the worker id the captures belong to")
    enroll.add_argument("--source", default=None, help="folder of images")
    enroll.add_argument("--camera", default=None, help="webcam index or rtsp:// URL")
    enroll.add_argument("--check-roster", action="store_true",
                        help="refuse an id that is not in the deployment's users table")
    enroll.add_argument("--consent", default=None)
    enroll.add_argument("--actor", default=None)
    _detector_arguments(enroll)
    enroll.add_argument("--limit", type=int, default=0)
    enroll.add_argument("--every", type=int, default=1)
    enroll.add_argument("--json", default=None)
    enroll.add_argument("--quiet", action="store_true")
    enroll.set_defaults(handler=cmd_enroll)

    label = sub.add_parser("label", help="attach, change or clear an identity")
    label.add_argument("--capture", action="append", default=None)
    label.add_argument("--all-unlabelled", action="store_true")
    label.add_argument("--identity", default=None)
    label.add_argument("--clear", action="store_true", help="remove a wrong label")
    label.add_argument("--actor", default=None)
    label.set_defaults(handler=cmd_label)

    listing = sub.add_parser("list", help="every capture, or a subset")
    listing.add_argument("--identity", default=None)
    listing.add_argument("--unlabelled", action="store_true")
    listing.add_argument("--json", default=None)
    listing.set_defaults(handler=cmd_list)

    stats = sub.add_parser("stats", help="what the corpus holds, in the terms a band is decided in")
    stats.add_argument("--json", default=None)
    stats.set_defaults(handler=cmd_stats)

    export = sub.add_parser("export", help="write the folder contract the two tools read")
    export.add_argument("--destination", required=True)
    export.add_argument("--identity", action="append", default=None)
    export.add_argument("--include-unlabelled", action="store_true")
    export.add_argument("--include-hard-cases", action="store_true",
                        help="export the flagged captures too (for a coverage experiment, not a band)")
    export.add_argument("--json", default=None)
    export.set_defaults(handler=cmd_export)

    purge = sub.add_parser("purge", help="erase captures by age, identity or id (dry run by default)")
    purge.add_argument("--older-than-days", type=int, default=None)
    purge.add_argument("--identity", default=None)
    purge.add_argument("--capture", action="append", default=None)
    purge.add_argument("--apply", action="store_true", help="actually erase (default: report only)")
    purge.add_argument("--json", default=None)
    purge.set_defaults(handler=cmd_purge)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
