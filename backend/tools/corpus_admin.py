"""Operator command for the labelled corpus: import frames, label them, export them, erase them.

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
deliberate act (importing a folder of staff photographs, labelling a quarantine capture, purging a
period) or a maintenance one (exporting for a tool run), and none of them belongs behind a route that
a worker, a manager or a curious administrator could reach. A screen would also have to solve "who may
see these faces", and the answer here is "whoever has the shell and the directory", which is a claim a
deployment can actually enforce.

WHAT IT DOES, IN THE ORDER A ROLLOUT USES IT
--------------------------------------------
    # one capture per image, detector run once, frame + landmarks + provenance stored
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

The identity of an imported image comes from the **folder contract** the band tool already uses (see
``derive_facenet_band``): a subdirectory is the identity, a flat ``name_1.jpg`` is split on its
trailing number. A folder that carries no identity is imported **unlabelled** - into the same store,
visible in ``list --unlabelled`` and ``stats`` - because a corpus that cannot represent "we have not
decided who this is yet" forces somebody to guess at import time.
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


def _detector(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """The detector this import will crop with, plus the fingerprint stored on every capture."""
    import face_detector

    import detector_640
    import corpus

    model = Path(args.model) if args.model else Path(face_detector.model_path())
    if not model.exists():
        raise SystemExit(f"detector model not found: {model}")
    detection = detector_640.build_detector(
        args.detector, model, input_size=args.input_size, tiles=args.tiles,
        overlap=args.overlap, square=args.square,
    )
    fingerprint = corpus.detector_fingerprint(
        kind=args.detector, model_path=model, input_size=args.input_size,
        square=args.square, tiles=args.tiles, overlap=args.overlap,
    )
    return detection, fingerprint


def _frame_as_bgr(frame: Any) -> Any:
    import cv2
    import numpy as np

    return cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR)


def cmd_add(args: argparse.Namespace) -> int:
    import cv2
    from pathlib import Path as _Path

    from PIL import Image

    corpus, _detector_640, (collect, identity_of) = _load()

    source = _Path(args.source)
    if not source.is_dir():
        print(f"source folder not found: {source}", file=sys.stderr)
        return 2
    consent = args.consent or (IMPORT_CONSENT_TEMPLATE.format(actor=args.actor) if args.actor else "")
    if not consent:
        print(
            "an import needs --consent, and --actor alone is not a basis: the corpus is a biometric "
            "store, so the command that fills it has to record what was relied on (for example "
            '--consent "staff calibration session, signed form 2026-09-22" --actor "R. Ops")',
            file=sys.stderr,
        )
        return 2

    grouped = collect(source)
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
        detection, fingerprint = _detector(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    stored: list[str] = []
    skipped: list[str] = []
    budget = int(args.limit) if args.limit else None
    for identity, paths in sorted(grouped.items()):
        for path in paths:
            if budget is not None and len(stored) >= budget:
                break
            try:
                image = Image.open(path).convert("RGB")
            except (OSError, ValueError) as exc:
                skipped.append(f"{path} (unreadable: {exc})")
                continue
            prepared = corpus.prepare(image)
            found = detection(_frame_as_bgr(prepared.frame))
            if not found:
                skipped.append(f"{path} (no face detected at input={args.input_size} tiles={args.tiles})")
                continue
            if args.strict_single_face and len(found) != 1:
                skipped.append(f"{path} ({len(found)} faces detected, need exactly 1)")
                continue
            best = max(found, key=lambda item: item.area())
            try:
                record = corpus.store(
                    prepared,
                    detector=fingerprint,
                    identity=identity or None,
                    landmarks=best.landmarks,
                    box=best.box,
                    score=best.score,
                    source=corpus.SOURCE_IMPORT,
                    consent=consent,
                    actor=args.actor,
                    note=f"imported from {path}",
                )
            except (corpus.CorpusError, OSError) as exc:
                skipped.append(f"{path} ({exc})")
                continue
            stored.append(record.capture_id)
            if not args.quiet:
                print(
                    f"{record.capture_id}  {record.identity or '(unlabelled)':<24} "
                    f"face={record.face_px:.0f}px native={record.native_face_px:.0f}px "
                    f"stored={record.stored_size[0]}x{record.stored_size[1]}  {path.name}"
                )

    report = {
        "source": str(source),
        "stored": len(stored),
        "skipped": len(skipped),
        "identities": len({Path(path).parent.name for path in stored}) or 0,
        "crop": corpus.crop_description(fingerprint),
        "consent": consent,
        "captures": stored,
        "skips": skipped,
    }
    if skipped:
        print("skipped:", file=sys.stderr)
        for note in skipped[:20]:
            print(f"  - {note}", file=sys.stderr)
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more", file=sys.stderr)
    print(f"stored {len(stored)} capture(s), skipped {len(skipped)}; crop: {report['crop']}")
    _maybe_write_json(args, report)
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
        print(
            f"{record.capture_id}  {record.identity or '(unlabelled)':<24} "
            f"{record.captured_at}  face={record.face_px:6.1f}px  native={record.native_face_px:6.1f}px  "
            f"{record.source:<8} {corpus.crop_description(record.detector)}"
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
            args.destination, identities=args.identity, include_unlabelled=args.include_unlabelled
        )
    except corpus.CorpusError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    print(f"exported {report['images']} image(s) across {report['identities']} identity(ies) "
          f"to {report['destination']}")
    if report["unlabelled_skipped"]:
        print(f"  {report['unlabelled_skipped']} unlabelled capture(s) left behind "
              "(--include-unlabelled puts them under 'unlabelled')")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus_admin.py",
        description="Import, label, export and erase the labelled calibration corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="import images, detecting once per image on the stored copy")
    add.add_argument("--source", required=True, help="folder of images (subfolder = identity)")
    add.add_argument("--identity", default=None,
                     help="label every image in the folder with this one identity")
    add.add_argument("--unlabelled", action="store_true",
                     help="import without a label, to be decided later")
    add.add_argument("--consent", default=None,
                     help="the basis these captures are held under (required unless --actor is given)")
    add.add_argument("--actor", default=None, help="who is importing, recorded on every capture")
    add.add_argument("--detector", default="yunet", choices=("yunet", "scrfd"))
    add.add_argument("--model", default=None, help="detector ONNX path (default: the live model)")
    add.add_argument("--input-size", type=int, default=640)
    add.add_argument("--tiles", type=int, default=1)
    add.add_argument("--overlap", type=float, default=0.2)
    add.add_argument("--square", action="store_true")
    add.add_argument("--strict-single-face", action="store_true",
                     help="skip a frame with more than one face instead of using the largest")
    add.add_argument("--limit", type=int, default=0, help="stop after this many stored captures")
    add.add_argument("--json", default=None)
    add.add_argument("--quiet", action="store_true")
    add.set_defaults(handler=cmd_add)

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
