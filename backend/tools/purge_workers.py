#!/usr/bin/env python
"""Permanently delete worker accounts and everything tied to them.

WHY THIS EXISTS
---------------
``POST /api/v1/admin/users/delete`` deliberately refuses an account that has any
attendance history: those shifts are the record of work that was done and of hours
that are owed, and the reports ``LEFT JOIN`` the account, so deleting it would leave
rows standing that name nobody. The console's answer is "deactivate instead", which
is right for a person.

It is the wrong answer for the deployment's own test accounts. The load harness
creates a roster (``Load Test Worker 5`` …), punches with it, and deactivates it, and
what is left behind is scaffolding nobody is paid from: thousands of attendance and
audit rows, notification rows, and the punch frames the run captured. This tool is
the deliberate, narrow exception that removes exactly that - and nothing else - from
the database directly, because the API is (correctly) not allowed to.

It is guarded on purpose:

* every target must live in the worker id band (``< 5000``); an admin id is refused,
* every target's name must start with ``--name-prefix`` (default ``Load Test Worker``)
  unless ``--force`` says otherwise, so a typo cannot delete a real roster,
* every target's role must be ``worker``,
* nothing is written until a full on-disk backup of the database has succeeded.

Dry-run is the default. ``--apply`` is what actually deletes.

    # what would go (no writes)
    python tools/purge_workers.py --db /data/times.db --data-dir /data --ids 5-54

    # do it
    python tools/purge_workers.py --db /data/times.db --data-dir /data --ids 5-54 --apply

Run it where the database lives (inside the deployment container), not over the API.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

#: Ids at or above this are the console's own accounts and are never touched.
ADMIN_ID_FLOOR = 5000

#: (table, column) pairs that hold a reference to a worker. A target's rows are removed
#: from all of them, so no row is left naming an id the ``users`` table no longer has.
WORKER_TABLES: tuple[tuple[str, str], ...] = (
    ("active_sessions", "worker_id"),
    ("admin_notifications", "worker_id"),
    ("attendance_logs", "worker_id"),
    ("device_anchors", "worker_id"),
    ("enrollment_invites", "worker_id"),
    ("enrollment_job_items", "worker_id"),
    ("overtime_authorisations", "worker_id"),
    ("punch_queue", "worker_id"),
    ("quick_link_uses", "worker_id"),
    ("quick_links", "worker_id"),
    ("refused_punches", "worker_id"),
    ("worker_devices", "worker_id"),
    ("worker_notes", "worker_id"),
    ("worker_notifications", "worker_id"),
    ("worker_push_subscriptions", "worker_id"),
)

#: Tables a worker is referenced from but that the database itself forbids deleting from: a
#: ``BEFORE DELETE`` trigger raises ``audit_log is append-only``. These rows are therefore
#: **kept** - the trail of what the account did is meant to outlive the account - and the tool
#: reports them rather than pretending they are gone.
APPEND_ONLY: tuple[tuple[str, str], ...] = (
    ("audit_log", "actor_id"),
    ("corpus_capture_consents", "worker_id"),
)


def parse_ids(spec: str) -> list[str]:
    """``"5-54"`` / ``"5,7,9"`` / ``"5-10,20"`` in any mix, as strings in order."""
    out: list[str] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            out.extend(str(n) for n in range(int(low), int(high) + 1))
        else:
            out.append(str(int(part)))
    return out


def ids_from_roster(path: Path) -> list[str]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [str(row["user_id"]) for row in rows if row.get("user_id") is not None]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def read_targets(conn: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT id, name, role, biometric_id FROM users WHERE id IN ({placeholders})",
        ids,
    ).fetchall()


def guard(rows: list[sqlite3.Row], ids: list[str], name_prefix: str, force: bool) -> list[str]:
    """Return the reasons this purge must not proceed. Empty means it is safe."""
    problems: list[str] = []
    present = {str(row["id"]) for row in rows}
    missing = [i for i in ids if i not in present]
    if missing:
        problems.append(f"{len(missing)} id(s) not on this deployment: {missing[:10]}")
    for row in rows:
        user_id = str(row["id"])
        if int(user_id) >= ADMIN_ID_FLOOR:
            problems.append(f"{user_id} is in the admin band (>= {ADMIN_ID_FLOOR})")
        if str(row["role"] or "").lower() != "worker":
            problems.append(f"{user_id} has role {row['role']!r}, not 'worker'")
        if name_prefix and not str(row["name"] or "").startswith(name_prefix) and not force:
            problems.append(
                f"{user_id} is named {row['name']!r}, which is not a {name_prefix!r} "
                "account (use --force only if that is intended)"
            )
    return problems


def backup(db: Path, keep: int = 5) -> Path:
    """A consistent copy via SQLite's own backup API (safe under WAL, unlike a file copy)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = db.with_name(f"{db.name}.pre-purge-{stamp}.bak")
    source = sqlite3.connect(str(db))
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    # Keep the target dir tidy: only the newest few pre-purge copies survive.
    siblings = sorted(
        db.parent.glob(f"{db.name}.pre-purge-*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in siblings[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def plan_counts(conn: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    """Rows each table would lose, keyed ``"table.column"``. Missing tables read as 0."""
    placeholders = ",".join("?" for _ in ids)
    counts: dict[str, int] = {}
    for table, column in WORKER_TABLES:
        if not table_exists(conn, table):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", ids
        ).fetchone()[0]
        if n:
            counts[f"{table}.{column}"] = int(n)
    # Note replies hang off worker_notes, not the worker, so they are counted through it.
    if table_exists(conn, "worker_notes") and table_exists(conn, "worker_note_messages"):
        n = conn.execute(
            "SELECT COUNT(*) FROM worker_note_messages WHERE note_id IN "
            f"(SELECT id FROM worker_notes WHERE worker_id IN ({placeholders}))",
            ids,
        ).fetchone()[0]
        if n:
            counts["worker_note_messages.note_id"] = int(n)
    counts["users.id"] = len(read_targets(conn, ids))
    return counts


def append_only_counts(conn: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    """Rows the database will keep forever, keyed the same way, for an honest report."""
    placeholders = ",".join("?" for _ in ids)
    kept: dict[str, int] = {}
    for table, column in APPEND_ONLY:
        if not table_exists(conn, table):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", ids
        ).fetchone()[0]
        if n:
            kept[f"{table}.{column}"] = int(n)
    return kept


def collect_files(conn: sqlite3.Connection, rows: list[sqlite3.Row], ids: list[str]) -> tuple[list[Path], list[Path]]:
    """``(frame_files, face_files)`` a purge would remove, read before any row is deleted."""
    placeholders = ",".join("?" for _ in ids)
    frames: set[str] = set()
    for table in ("attendance_logs", "refused_punches"):
        if not table_exists(conn, table):
            continue
        for (name,) in conn.execute(
            f"SELECT punch_frame FROM {table} WHERE worker_id IN ({placeholders}) "
            "AND punch_frame IS NOT NULL AND punch_frame <> ''",
            ids,
        ):
            frames.add(os.path.basename(str(name)))
    return (
        [Path(name) for name in sorted(frames)],
        face_file_names(rows),
    )


def face_file_names(rows: list[sqlite3.Row]) -> list[Path]:
    """Face template + reference filenames for each account, canonical and legacy."""
    names: list[Path] = []
    for row in rows:
        user_id = str(row["id"])
        biometric = str(row["biometric_id"] or "").strip()
        if biometric:
            names.append(Path("worker_photos") / f"{biometric}.jpg")
            names.append(Path("local_references") / f"{biometric}.json")
        # Pre-migration-11 files were named after the account id.
        names.append(Path("worker_photos") / f"{user_id}.jpg")
    return names


def purge_rows(conn: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    """Delete every deletable row for ``ids`` in one transaction. Returns what went."""
    placeholders = ",".join("?" for _ in ids)
    deleted: dict[str, int] = {}
    # Replies hang off the note, not the worker, so their note ids are read *before* the notes
    # themselves are deleted - otherwise the subquery finds nothing and the replies are orphaned.
    if table_exists(conn, "worker_notes") and table_exists(conn, "worker_note_messages"):
        cursor = conn.execute(
            "DELETE FROM worker_note_messages WHERE note_id IN "
            f"(SELECT id FROM worker_notes WHERE worker_id IN ({placeholders}))",
            ids,
        )
        if cursor.rowcount:
            deleted["worker_note_messages.note_id"] = cursor.rowcount
    for table, column in WORKER_TABLES:
        if not table_exists(conn, table):
            continue
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids
        )
        if cursor.rowcount:
            deleted[f"{table}.{column}"] = cursor.rowcount
    cursor = conn.execute(f"DELETE FROM users WHERE id IN ({placeholders})", ids)
    deleted["users.id"] = cursor.rowcount
    return deleted


def wipe(path: Path) -> bool:
    """Remove one file, preferring the app's overwrite-then-unlink for faces when importable."""
    if not path.exists():
        return False
    try:
        sys.path.insert(0, "/app/backend")  # so retention.wipe_file is available in-container
        import retention  # type: ignore
        retention.wipe_file(str(path), directory=str(path.parent))
        return True
    except Exception:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "/data/times.db"))
    parser.add_argument("--data-dir", default="/data",
                        help="the state root carrying punch_frames/ worker_photos/ etc.")
    parser.add_argument("--ids", help='e.g. "5-54" or "5,7,9"')
    parser.add_argument("--roster", help="a roster JSON to read user ids from")
    parser.add_argument("--name-prefix", default="Load Test Worker")
    parser.add_argument("--force", action="store_true",
                        help="skip the name-prefix guard (role and id-band guards still apply)")
    parser.add_argument("--no-files", action="store_true",
                        help="delete rows only; leave punch frames and face files")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args(argv)

    if not args.ids and not args.roster:
        print("give --ids or --roster")
        return 1
    ids = parse_ids(args.ids) if args.ids else ids_from_roster(Path(args.roster))
    if not ids:
        print("no ids to work on")
        return 1

    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}")
        return 1
    data_dir = Path(args.data_dir)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = read_targets(conn, ids)

    problems = guard(rows, ids, args.name_prefix, args.force)
    if problems:
        print("refusing to purge:")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    counts = plan_counts(conn, ids)
    kept = append_only_counts(conn, ids)
    frames, faces = collect_files(conn, rows, ids)
    frame_files = [data_dir / "punch_frames" / name for name in frames]
    face_files = [data_dir / name for name in faces]
    frames_on_disk = [p for p in frame_files if p.exists()]
    faces_on_disk = [p for p in face_files if p.exists()]

    print(f"database : {db}")
    print(f"targets  : {len(rows)} account(s)  {[str(r['id']) for r in rows][:6]}"
          f"{' …' if len(rows) > 6 else ''}")
    print("rows to delete:")
    for key in sorted(counts):
        print(f"  {key:36} {counts[key]}")
    print(f"  {'TOTAL':36} {sum(counts.values())}")
    if kept:
        print("kept (append-only, cannot be deleted):")
        for key in sorted(kept):
            print(f"  {key:36} {kept[key]}")
    if not args.no_files:
        print(f"files    : {len(frames_on_disk)} punch frame(s), {len(faces_on_disk)} face/ref file(s)")

    if not args.apply:
        print("\ndry run: nothing written (pass --apply to delete)")
        return 0

    saved = backup(db)
    print(f"\nbackup   : {saved}")
    try:
        deleted = purge_rows(conn, ids)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - report and leave the database consistent
        conn.rollback()
        print(f"purge failed, rolled back: {exc}")
        return 2

    print("deleted rows:")
    for key in sorted(deleted):
        print(f"  {key:36} {deleted[key]}")

    removed = 0
    if not args.no_files:
        for path in (*frames_on_disk, *faces_on_disk):
            removed += 1 if wipe(path) else 0
        print(f"removed {removed} file(s)")

    left = read_targets(conn, ids)
    print(f"\nremaining accounts with those ids: {len(left)}")
    conn.close()
    return 0 if not left else 2


if __name__ == "__main__":
    sys.exit(main())
