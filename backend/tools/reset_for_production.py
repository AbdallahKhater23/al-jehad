#!/usr/bin/env python
"""Clear a deployment's operational data down to a production-testing baseline.

WHY THIS EXISTS
---------------
A deployment that has been used for testing carries the test roster with it: worker
accounts, their shifts and attendance, the sites those punches were taken against, the
queued offline punches, the review notifications, and - most of all - the face photos
and punch frames those accounts produced. Handing that to a customer, or re-running a
production test on top of it, means the new roster shares a database with the old one:
duplicate device registrations, an anchor history nobody can explain, and biometric
images of people who no longer work there.

``tools/purge_workers.py`` already removes a *named* set of worker accounts. This tool
is the different, broader operation: keep the console accounts and the configuration,
delete everything operational.

WHAT SURVIVES, AND WHY
----------------------
* ``developer`` and ``head_admin`` accounts - the two root tiers. They are the only way
  back in, so the tool **refuses to run** if either is missing or not ``active``. A
  reset that locks the operator out of production is worse than the data it removed.
* ``site_categories`` - the site taxonomy is configuration, not history, and the request
  that produced this tool named it explicitly as the one site-related thing to keep.
* ``company_settings``, ``registration_settings``, ``shift_rules``,
  ``developer_config``/``developer_config_version`` - the deployment's own settings.
* ``schema_migrations`` - the record of how the schema got here. Deleting it would make
  the app think it is un-migrated.
* ``audit_log`` and ``corpus_capture_consents`` - both carry a ``BEFORE DELETE`` trigger
  that raises ``is append-only``. They are deliberately immutable, so this tool does not
  try: the trail of what the test data did outlives the test data, and the rows naming a
  deleted account stay as history. They are reported, not silently skipped.
* ``retention_runs`` - operational telemetry about retention sweeps, not test data.

WHAT GOES
---------
Every other account (workers *and* any non-root admin), every site, and every
transactional row: attendance, active shifts, offline punch queue, device registrations
and anchors, refused punches, quick links and their uses, notes and replies, overtime
authorisations, enrollment invites/jobs, registrations, and notifications. The evidence
on disk goes with them - punch frames, worker photos, face references and quick-link
photos - because the rows that name those files are what make them evidence.

It is guarded on purpose:

* a full on-disk backup via SQLite's own backup API must succeed before anything is written,
* the root accounts are verified present and active *before* anything is written,
* files belonging to a surviving account are never touched,
* ``--apply`` is what actually deletes. Dry-run is the default.

    # what would go (no writes)
    python backend/tools/reset_for_production.py --db times.db --data-dir .

    # do it
    python backend/tools/reset_for_production.py --db times.db --data-dir . --apply

Run it where the database lives - on the host for a local checkout, or inside the
deployment container (``/data``) for a Railway volume. The dry run is safe to run
against production and is the intended first step there.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

#: Roles that must survive a reset, because they are the only way back into the console.
ROOT_ROLES = ("developer", "head_admin")

#: Tables cleared completely, in an order that removes dependants before their parents.
#: ``(table, why)`` - the reason is printed in the dry run so the operator can audit the
#: list rather than trust it.
CLEARED: tuple[tuple[str, str], ...] = (
    ("worker_note_messages", "note replies"),
    ("worker_notes", "worker notes"),
    ("quick_link_uses", "quick-link uses"),
    ("quick_links", "quick links"),
    ("enrollment_job_items", "enrollment job items"),
    ("enrollment_jobs", "enrollment jobs"),
    ("enrollment_invites", "enrollment invites"),
    ("registration_requests", "walk-up registrations"),
    ("overtime_authorisations", "overtime decisions"),
    ("punch_queue", "queued offline punches"),
    ("refused_punches", "refused punches"),
    ("attendance_logs", "attendance and hours"),
    ("active_sessions", "open shifts"),
    ("device_anchors", "offline time anchors"),
    ("worker_devices", "device registrations"),
    ("worker_push_subscriptions", "push subscriptions"),
    ("worker_notifications", "worker notifications"),
    ("admin_notifications", "admin notifications"),
    ("construction_sites", "sites"),
)

#: Never deleted from: a trigger refuses, and the immutability is the point.
APPEND_ONLY: tuple[str, ...] = ("audit_log", "corpus_capture_consents")

#: Directories under the state root whose *data* files are removed with the rows.
#: ``desktop.ini`` and friends are Windows/OS bookkeeping, not evidence.
EVIDENCE_DIRS: tuple[str, ...] = (
    "punch_frames",
    "worker_photos",
    "local_references",
    "quick_link_photos",
    "registration_photos",
    "calibration_corpus",
)

#: Files that are never wiped even inside an evidence directory.
NON_DATA_FILES = {"desktop.ini", "thumbs.db", ".ds_store", ".gitkeep", ".gitignore"}


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def read_roots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ROOT_ROLES)
    return conn.execute(
        f"SELECT id, name, role, status, biometric_id FROM users WHERE role IN ({placeholders})",
        ROOT_ROLES,
    ).fetchall()


def guard(conn: sqlite3.Connection) -> list[str]:
    """Reasons this reset must not proceed. Empty means it is safe."""
    problems: list[str] = []
    roots = {str(row["role"]): row for row in read_roots(conn)}
    for role in ROOT_ROLES:
        row = roots.get(role)
        if row is None:
            problems.append(
                f"no {role} account exists - clearing this database would lock everyone out"
            )
            continue
        if str(row["status"] or "").lower() != "active":
            problems.append(
                f"the {role} account {row['id']} is {row['status']!r}, not 'active'"
            )
    return problems


def survivors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every account that stays: the root tiers, plus any account they own."""
    placeholders = ",".join("?" for _ in ROOT_ROLES)
    return conn.execute(
        f"SELECT id, name, role, biometric_id FROM users WHERE role IN ({placeholders})",
        ROOT_ROLES,
    ).fetchall()


def doomed_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ROOT_ROLES)
    return conn.execute(
        f"SELECT id, name, role FROM users WHERE role NOT IN ({placeholders}) "
        "ORDER BY CAST(id AS INTEGER)",
        ROOT_ROLES,
    ).fetchall()


def plan(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows each cleared table would lose, plus the doomed accounts."""
    counts: dict[str, int] = {}
    for table, _why in CLEARED:
        if not table_exists(conn, table):
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if n:
            counts[table] = int(n)
    counts["users"] = len(doomed_accounts(conn))
    return counts


def kept_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Append-only rows that will remain, for an honest report."""
    kept: dict[str, int] = {}
    for table in APPEND_ONLY:
        if not table_exists(conn, table):
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if n:
            kept[table] = int(n)
    return kept


def collect_files(
    data_dir: Path, keep_biometrics: set[str], keep_ids: set[str]
) -> tuple[list[Path], list[Path]]:
    """``(files_to_wipe, files_kept)``, listed before any row is deleted.

    This sweeps the evidence directories rather than reading filenames out of the rows
    about to go: a frame left behind by an earlier failed sweep is still a face on disk,
    and a clear-out that only removed the files the database still pointed at would
    leave it there. Subdirectories are left alone, and files belonging to a surviving
    account are kept - the head admin's own face template is not test data, and a reset
    must not blind the person meant to sign back in.
    """
    protected: set[str] = set()
    for biometric in keep_biometrics:
        protected.add(f"{biometric}.jpg")
        protected.add(f"{biometric}.json")
    for user_id in keep_ids:
        protected.add(f"{user_id}.jpg")
        protected.add(f"{user_id}.json")

    wipe: list[Path] = []
    keep: list[Path] = []
    for name in EVIDENCE_DIRS:
        directory = data_dir / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if path.name.lower() in NON_DATA_FILES or path.name in protected:
                keep.append(path)
            else:
                wipe.append(path)
    return wipe, keep


def backup(db: Path, keep: int = 5) -> Path:
    """A consistent copy via SQLite's own backup API (safe under WAL, unlike a file copy)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = db.with_name(f"{db.name}.pre-reset-{stamp}.bak")
    source = sqlite3.connect(str(db))
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    siblings = sorted(
        db.parent.glob(f"{db.name}.pre-reset-*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in siblings[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def wipe_file(path: Path, directory: Path) -> bool:
    """Overwrite then unlink one file, preferring the application's own helper.

    ``retention.wipe_file`` is used when it can be imported, because it carries the path
    guards that matter for a name that came out of the database (basename only, resolved
    parent must be the directory, symlinks refused). When it **refuses** a file, that
    refusal is honoured: the fallback below is for a deployment where the module is not
    importable at all, not a way around the app's own safety check.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import retention  # type: ignore
    except Exception:
        retention = None  # type: ignore[assignment]

    if retention is not None:
        try:
            retention.wipe_file(str(path), directory=str(directory))
            return True
        except Exception:
            return False

    if path.is_symlink():
        return False
    try:
        size = path.stat().st_size
        with open(path, "r+b", buffering=0) as handle:
            remaining = size
            while remaining > 0:
                chunk = min(1024 * 1024, remaining)
                handle.write(os.urandom(chunk))
                remaining -= chunk
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        return True
    except OSError:
        return False


def clear_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete every operational row and the non-root accounts, in one transaction."""
    deleted: dict[str, int] = {}
    for table, _why in CLEARED:
        if not table_exists(conn, table):
            continue
        cursor = conn.execute(f"DELETE FROM {table}")
        if cursor.rowcount:
            deleted[table] = cursor.rowcount

    placeholders = ",".join("?" for _ in ROOT_ROLES)
    cursor = conn.execute(f"DELETE FROM users WHERE role NOT IN ({placeholders})", ROOT_ROLES)
    deleted["users"] = cursor.rowcount
    return deleted


def reset_sequences(conn: sqlite3.Connection, cleared: list[str]) -> list[str]:
    """Restart AUTOINCREMENT counters for the cleared tables, so ids begin at 1 again.

    Only for tables this tool emptied: leaving ``attendance_logs`` counting from 105 in a
    database with no attendance rows is the kind of leftover that makes a fresh
    production test look like it inherited history.
    """
    if not table_exists(conn, "sqlite_sequence"):
        return []
    reset: list[str] = []
    for name in cleared:
        row = conn.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            continue
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (name,))
        reset.append(name)
    return reset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "times.db"))
    parser.add_argument("--data-dir", default=".",
                        help="the state root carrying punch_frames/ worker_photos/ etc.")
    parser.add_argument("--keep-files", action="store_true",
                        help="clear rows only; leave punch frames and face files on disk")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}")
        return 1
    data_dir = Path(args.data_dir)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    problems = guard(conn)
    if problems:
        print("refusing to reset:")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    roots = survivors(conn)
    doomed = doomed_accounts(conn)
    counts = plan(conn)
    kept = kept_counts(conn)
    keep_biometrics = {str(r["biometric_id"]) for r in roots if r["biometric_id"]}
    keep_ids = {str(r["id"]) for r in roots}
    files, protected = collect_files(data_dir, keep_biometrics, keep_ids)

    print(f"database : {db}  ({db.stat().st_size} bytes)")
    print(f"data dir : {data_dir}")
    print()
    print("surviving accounts:")
    for row in roots:
        print(f"  {str(row['id']):>14}  {row['role']:<12} {row['name']}")
    print()
    print(f"accounts to delete ({len(doomed)}):")
    for row in doomed:
        print(f"  {str(row['id']):>14}  {row['role']:<12} {row['name']}")
    print()
    print("rows to delete:")
    for key in sorted(counts):
        print(f"  {key:36} {counts[key]}")
    print(f"  {'TOTAL':36} {sum(counts.values())}")
    if kept:
        print("kept (append-only, cannot be deleted):")
        for key in sorted(kept):
            print(f"  {key:36} {kept[key]}")
    if not args.keep_files:
        print(f"files    : {len(files)} evidence file(s) to wipe, {len(protected)} kept")
        for path in protected:
            print(f"           keep {path.relative_to(data_dir) if data_dir in path.parents else path}")

    if not args.apply:
        print("\ndry run: nothing written (pass --apply to delete)")
        conn.close()
        return 0

    saved = backup(db)
    print(f"\nbackup   : {saved}")
    try:
        deleted = clear_rows(conn)
        reset = reset_sequences(conn, list(deleted))
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - report and leave the database consistent
        conn.rollback()
        print(f"reset failed, rolled back: {exc}")
        conn.close()
        return 2

    print("deleted rows:")
    for key in sorted(deleted):
        print(f"  {key:36} {deleted[key]}")
    if reset:
        print(f"restarted id sequences: {', '.join(sorted(reset))}")

    wiped = 0
    if not args.keep_files:
        for path in files:
            wiped += 1 if wipe_file(path, path.parent) else 0
        print(f"wiped {wiped} file(s)")

    left = doomed_accounts(conn)
    remaining = plan(conn)
    print()
    print(f"accounts left besides the root tiers: {len(left)}")
    print(f"operational rows left: {sum(v for k, v in remaining.items() if k != 'users')}")
    conn.close()
    return 0 if not left else 2


if __name__ == "__main__":
    sys.exit(main())
