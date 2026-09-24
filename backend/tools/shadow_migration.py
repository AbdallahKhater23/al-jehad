"""Start and read the FaceNet-512 shadow migration. Read-only against attendance data.

WHY THIS EXISTS
---------------
``shadow_rollout`` is the wiring; this is the operator's half of it, and it exists so that beginning
the migration is four commands rather than an afternoon of reading. Every command is a thin front for
something that module owns - there is no second definition of a crop, a gallery, a band or a gate
here, which is the same rule ``corpus_admin.py`` follows for the corpus.

    # can it start at all? (this is the first question, and the answer is often "fetch a graph")
    python tools/shadow_migration.py status

    # re-embed every enrolled worker into the 512-D gallery, and cache it
    python tools/shadow_migration.py backfill

    # score stored punch frames through both encoders into the paired log
    python tools/shadow_migration.py score --limit 500 \
        --shadow-approve 0.42 --evidence "measured 2026-09-24, contract_ab BGR/[0,1]"

    # the cutover gate's verdict on everything the log holds
    python tools/shadow_migration.py report --shadow-approve 0.42

WHAT IT NEVER DOES
------------------
It does not touch ``times.db`` beyond reading the roster and the templates, it never writes a
template, and it cannot affect a punch: the enforced encoder here is a *reader of the same graph the
punch path uses*, and nothing it computes is read back by the application. It also never copies a
frame or a selfie anywhere new - the backfill reads the stored photograph in place, because the
alternative is a second copy of a worker's face on disk with no retention policy over it.

``--shadow-approve`` is required by ``score`` and ``report``, and deliberately has no default. The
approve line is the number that decides whether somebody is let in, and a line measured in a 128-D
space is not a line in a 512-D one - they are not even the same units. A tool that supplied a
default here would be handing out an unmeasured access-control decision with a plausible look.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

#: Run as a script, this file is ``sys.path[0]``; the application modules are importable only once
#: the backend directory is on the path, and ``tools`` so a sibling tool can be named.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _entry in (_TOOLS_DIR, _BACKEND_DIR):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


def _database_path() -> Path:
    """The database the application would use, read through the setting that decides it."""
    try:
        import config

        return Path(str(config.settings.database_path))
    except Exception:  # pragma: no cover - a checkout without settings loaded
        import os

        return Path(os.environ.get("DATABASE_PATH") or "times.db")


def _read_only_connection() -> sqlite3.Connection:
    """A connection that cannot write, so no command here can damage attendance data."""
    path = _database_path()
    if not path.exists():
        raise SystemExit(f"no database at {path}; set DATABASE_PATH to the deployment's copy")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _print(payload, as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print("\n".join(lines))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def cmd_status(args) -> int:
    import shadow_rollout

    report = shadow_rollout.status()
    configured = report["configured"]
    lines = [
        f"incumbent : {configured['enforced_path']} ({'present' if configured['enforced_present'] else 'MISSING'})",
        f"shadow    : {configured['shadow_path']} ({'present' if configured['shadow_present'] else 'not fetched'})",
        f"contract  : {configured['contract']}",
        f"paired log: {configured['log']}",
    ]
    if report["started"]:
        encoders = report["encoders"]
        lines += [
            "started   : yes",
            f"  enforced: {encoders['enforced']['width']}-D, {encoders['enforced']['file']}, "
            f"model {encoders['enforced']['model_id']}",
            f"  shadow  : {encoders['shadow']['width']}-D, {encoders['shadow']['file']}, "
            f"model {encoders['shadow']['model_id']}",
        ]
    else:
        lines += ["started   : no", f"  reason  : {report['reason']}"]
    _print(report, args.json, lines)
    return 0


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------
def cmd_backfill(args) -> int:
    import shadow_rollout

    encoders = shadow_rollout.load_encoders()
    connection = _read_only_connection()
    try:
        enforced_gallery, enforced_report = shadow_rollout.build_enforced_gallery(connection, encoders)
        band = shadow_rollout.shadow_band(
            approve=args.shadow_approve,
            review=args.shadow_review,
            model=f"Facenet-{encoders.shadow_width}",
            evidence=args.evidence,
        )
        shadow_gallery, shadow_report = shadow_rollout.build_shadow_gallery(
            connection, encoders, band=band
        )
        workers = shadow_rollout.total_workers(connection)
    finally:
        connection.close()

    cache = Path(args.gallery) if args.gallery else shadow_rollout.shadow_log_path().with_name(
        "shadow_gallery.json"
    )
    shadow_rollout.save_gallery(
        cache,
        shadow_gallery,
        model_id=encoders.shadow.model_id,
        contract=encoders.contract.id,
        band=band,
    )
    report = {
        "encoders": encoders.describe(),
        "workers": workers,
        "enforced": enforced_report,
        "shadow": shadow_report,
        "coverage": round(shadow_gallery.size / workers, 4) if workers else 0.0,
        "gallery_cache": str(cache),
        "band": {"approve": band.approve, "review": band.review, "evidence": band.evidence},
    }
    lines = [
        f"workforce          : {workers}",
        f"incumbent gallery  : {enforced_gallery.size} template(s) at {encoders.enforced_width}-D",
        f"shadow gallery     : {shadow_gallery.size} template(s) at {encoders.shadow_width}-D",
        f"coverage           : {report['coverage']:.1%} of the workforce",
        f"cached to          : {cache}",
    ]
    for name, section in (("incumbent", enforced_report), ("shadow", shadow_report)):
        if section["skipped"]:
            lines.append(f"{name} skipped ({len(section['skipped'])}):")
            lines += [f"  {row['worker_id']}: {row['reason']}" for row in section["skipped"][:20]]
    _print(report, args.json, lines)
    if shadow_gallery.size == 0:
        print(
            "\nthe shadow gallery is empty: nothing was re-embedded, so there is nothing to score "
            "against. Check that reference selfies are on disk (WORKER_PHOTOS_DIR).",
            file=sys.stderr,
        )
        return 2
    return 0


# ---------------------------------------------------------------------------
# score / report
# ---------------------------------------------------------------------------
def _band_from(args, encoders):
    """The band the shadow's own distances are read against, named for the graph that made them."""
    import shadow_rollout

    return shadow_rollout.shadow_band(
        approve=args.shadow_approve,
        review=args.shadow_review,
        # Named from the graph's own width rather than the string "512": the migration is
        # "a second encoder of a different width", and a 1792-D drop-in is the same migration.
        model=f"Facenet-{encoders.shadow_width}",
        evidence=args.evidence,
    )


def _load_cached_gallery(encoders, args):
    import shadow_rollout

    cache = Path(args.gallery) if args.gallery else shadow_rollout.shadow_log_path().with_name(
        "shadow_gallery.json"
    )
    gallery, payload = shadow_rollout.load_gallery(
        cache,
        width=encoders.shadow_width,
        model_id=encoders.shadow.model_id,
        band=_band_from(args, encoders),
    )
    return gallery, payload, cache


def cmd_score(args) -> int:
    import punch_frames
    import shadow_rollout

    encoders = shadow_rollout.load_encoders()
    connection = _read_only_connection()
    try:
        enforced_gallery, _ = shadow_rollout.build_enforced_gallery(connection, encoders)
    finally:
        connection.close()
    shadow_gallery, _payload, _cache = _load_cached_gallery(encoders, args)

    folder = Path(args.folder) if args.folder else Path(punch_frames.frames_dir())
    frames = shadow_rollout.frame_paths(folder, limit=args.limit)
    if not frames:
        print(
            f"no frames to score in {folder}. Punch frames live on the volume "
            "(PUNCH_FRAMES_DIR); the repository's own copy holds the suite's synthetic output.",
            file=sys.stderr,
        )
        return 2
    report = shadow_rollout.score_frames(
        encoders,
        enforced_gallery=enforced_gallery,
        shadow_gallery=shadow_gallery,
        frames=frames,
        sample_every=max(1, args.sample_every),
        site=args.site,
    )
    lines = [
        f"frames read        : {len(frames)} from {folder}",
        f"scored             : {report['scored']}",
        f"enforced verdicts  : {report['enforced_verdicts']}",
        f"shadow distances   : {report['shadow_distances']}",
        f"paired log         : {report['shadow_log']}",
        f"paired samples     : {report['summary']['paired_samples']}",
        f"agreement          : {report['summary']['verdict_agreement']}",
        f"shadow error rate  : {report['summary']['error_rate']}",
    ]
    if report["skipped"]:
        lines.append("skipped:")
        lines += [f"  {count}x {reason}" for reason, count in sorted(report["skipped"].items())]
    _print(report, args.json, lines)
    return 0


def cmd_report(args) -> int:
    import face_detector
    import shadow_rollout

    encoders = shadow_rollout.load_encoders()
    connection = _read_only_connection()
    try:
        enforced_gallery, _ = shadow_rollout.build_enforced_gallery(connection, encoders)
        workers = shadow_rollout.total_workers(connection)
    finally:
        connection.close()
    shadow_gallery, _payload, _cache = _load_cached_gallery(encoders, args)

    report = shadow_rollout.rollout(
        encoders,
        enforced_gallery=enforced_gallery,
        shadow_gallery=shadow_gallery,
        workers=workers,
        enforced_threshold=face_detector.active_band().approve,
        shadow_threshold=shadow_gallery.band.approve,
        min_coverage=args.min_coverage,
        min_paired_samples=args.min_paired_samples,
        max_error_rate=args.max_error_rate,
    )
    summary = report["summary"]
    paired = report["paired"]
    lines = [
        f"coverage           : {report['coverage']:.1%} of {workers} worker(s)",
        f"paired samples     : {paired.get('paired_samples', 0)}",
        f"shadow error rate  : {summary['error_rate']}",
        f"verdict agreement  : {summary['verdict_agreement']}",
        f"shadow more permissive: {paired.get('shadow_more_permissive', 0.0)}",
        f"enforced accepts   : {paired.get('enforced_accept_rate')} "
        f"(line {paired.get('enforced_threshold')})",
        f"shadow accepts     : {paired.get('shadow_accept_rate')} "
        f"(line {paired.get('shadow_threshold')})",
        "",
        f"flip ready         : {report['flip_ready']}",
        f"reason             : {report['reason']}",
    ]
    _print(report, args.json, lines)
    # Non-zero when the gate is not clear, like verify_readiness.py: this is a gate, and a
    # monitoring job wants the exit code rather than a sentence it has to parse.
    return 0 if report["flip_ready"] else 1


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def _add_band_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--shadow-approve",
        type=float,
        required=True,
        help="the shadow's own approve line, measured for the 512-D space (no default: see the module docstring)",
    )
    parser.add_argument("--shadow-review", type=float, default=None, help="optional review line")
    parser.add_argument(
        "--evidence",
        default="",
        help="what measured this line, recorded on the band (a line with no provenance is the defect)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="can the migration start, and with which graphs")
    status.set_defaults(func=cmd_status)

    backfill = sub.add_parser("backfill", help="re-embed the workforce into the shadow gallery")
    backfill.add_argument("--gallery", help="where to write the gallery cache")
    _add_band_arguments(backfill)
    backfill.set_defaults(func=cmd_backfill)

    score = sub.add_parser("score", help="score stored frames through both encoders")
    score.add_argument("--folder", help="a folder of frames (default: the punch-frame store)")
    score.add_argument("--limit", type=int, default=500, help="score only the newest N frames")
    score.add_argument("--sample-every", type=int, default=1, help="log one frame in N")
    score.add_argument("--site", default=None, help="recorded on every row")
    score.add_argument("--gallery", help="the gallery cache written by backfill")
    _add_band_arguments(score)
    score.set_defaults(func=cmd_score)

    report = sub.add_parser("report", help="the cutover gate's verdict on the paired log")
    report.add_argument("--gallery", help="the gallery cache written by backfill")
    report.add_argument("--min-coverage", type=float, default=0.98)
    report.add_argument("--min-paired-samples", type=int, default=2000)
    report.add_argument("--max-error-rate", type=float, default=0.005)
    _add_band_arguments(report)
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - a refusal is the tool's normal failure
        import shadow_rollout

        if isinstance(exc, shadow_rollout.ShadowUnavailable):
            print(f"cannot start the migration: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
