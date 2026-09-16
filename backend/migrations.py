"""Forward-only, additive schema migrations.

Rules (see the drift guard in ``schema_guard.py``, which enforces the spirit of
these mechanically):

* **Additive only** - no DROP, no RENAME, no type changes, no rewriting of
  existing ``status`` display strings. ``ADD COLUMN`` with constant defaults only,
  because SQLite rejects non-constant defaults and prefers them anyway.
* **Idempotent** - every step is guarded, so running the migrator twice changes
  nothing. The test suite relies on this: it migrates the throwaway database
  before every single test.
* **Multi-worker safe** - ``BEGIN IMMEDIATE`` plus re-checking ``schema_migrations``
  inside the transaction means N uvicorn workers cannot race the same migration;
  the losers observe it already applied and skip.

``baseline_schema()`` is the single source of truth for the original DDL: it is
used both by first-time installation and by the drift guard, which compiles it
(plus every migration) into an in-memory database and compares that against the
live file.
"""

from __future__ import annotations

import secrets
import sqlite3
import sys
from datetime import datetime
from typing import Callable

import biometrics
from config import ConfigError, settings
from security import hash_password

#: The version this build expects a database to be at - it must name the newest entry in
#: ``MIGRATIONS``. ``readiness`` refuses to start a deployment whose database is older, so a
#: migration added without bumping this is a server that will not boot; the invariant is
#: asserted in ``tests/test_site_shift_windows.py`` rather than left to memory.
SCHEMA_VERSION = 13

#: Magic number stamped into the SQLite header so we can recognise "this is our
#: database" - cheap protection against pointing DATABASE_PATH at some other file.
APPLICATION_ID = 0x51AE0A77

BASELINE_VERSION = 1


# ---------------------------------------------------------------------------
# baseline DDL (what a fresh install creates)
# ---------------------------------------------------------------------------
def baseline_schema() -> list[str]:
    """Original table definitions, exactly as the application has always declared them.

    ``score``/``status`` are declared NOT NULL because the shipped ``CREATE TABLE``
    includes them inline; ``active_sessions`` declares its columns NOT NULL because
    that is what the code says, even though the long-lived production table was
    created by an older revision and is laxer. The drift guard reports that
    difference as informational rather than an error - it cannot break writes.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS active_sessions (
            worker_id TEXT PRIMARY KEY,
            site_name TEXT NOT NULL,
            clock_in_time DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            token_version INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            site_name TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp DATETIME NOT NULL,
            hours FLOAT NOT NULL,
            score FLOAT NOT NULL,
            status TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS construction_sites (
            site_name TEXT PRIMARY KEY,
            lat FLOAT NOT NULL,
            lon FLOAT NOT NULL,
            radius FLOAT NOT NULL
        )
        """,
    ]


#: Deterministic, lossless translation of the human display strings into a
#: canonical status code. Payroll logic must never be built on display text, and
#: anything unrecognised fails closed to ``pending_review``.
STATUS_CODE_MAP = {
    "Approved": "approved",
    "Approved by Admin": "approved",
    "Force Clocked In by Admin": "approved",
    "Force Clocked Out by Admin": "approved",
    "Auto-Closed (11h Limit)": "auto_closed",
    # The 8 h policy close (migration 9) is a *different* code on purpose: it means
    # "the system ended this shift at the paid limit and it is payable", while the 11 h
    # rows above meant "nobody decided, so this needs an administrator". Reusing
    # ``auto_closed`` would silently change the meaning of rows already in the database
    # - and therefore of payroll totals already reported - which is not a migration.
    "Auto-Closed (8h Limit)": "auto_closed_8h",
    "pending_review": "pending_review",
    "Pending Overtime Approval": "pending_overtime",
}

#: The status row written when the system closes a shift because its paid hours have
#: reached ``regular_hours`` (see ``overtime.scan_auto_close``). The label is what a
#: human reads on the timesheet, the code is what payroll logic matches on - the same
#: split attendance has had since migration 2.
STATUS_AUTO_CLOSED_LABEL = "Auto-Closed (8h Limit)"
STATUS_CODE_AUTO_CLOSED_8H = "auto_closed_8h"

DEFAULT_SHIFT_RULES = {
    "clock_in_window_start": "04:00",
    "clock_in_window_end": "06:30",
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    "hard_cutoff_hours": 11.0,
    "working_days": "6,0,1,2,3,4",
    "site_timezone": "Africa/Cairo",
    # -- the unpaid break and the end of the day (migration 9) -----------------
    # A full day is 8 h paid plus a 30-minute break that is not paid, so a full day
    # runs 8.5 h from clock-in to clock-out. The break is deducted only from a shift
    # long enough to have contained one (``break_after_hours``, half a working day),
    # so a two-hour part-shift is paid as worked rather than losing half an hour it
    # never took. See ``shift_hours.py``.
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    #: Whether the system writes the clock-out itself when the paid hours reach
    #: ``regular_hours``. On by default: the day ends at 8 paid hours, and the extra
    #: hours a forgotten shift would otherwise accrue are the reason this exists.
    "auto_close_at_regular": 1,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """Add a column if it is missing. Returns True when it was added."""
    if column in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------
def migration_1_audit_notifications_shift_rules(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            actor_id TEXT,
            actor_role TEXT,
            action TEXT NOT NULL,
            entity TEXT,
            entity_id TEXT,
            before_json TEXT,
            after_json TEXT,
            ip TEXT,
            user_agent TEXT,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, created_at)")
    # Append-only enforced by the database itself, not only by convention: the
    # application has no legitimate UPDATE/DELETE against this table, so a
    # correction must be appended as a new event.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            worker_id TEXT,
            site_name TEXT,
            session_id INTEGER,
            log_id INTEGER,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            payload TEXT,
            dedupe_key TEXT UNIQUE,
            created_at DATETIME NOT NULL,
            read_at DATETIME,
            read_by TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_unread "
        "ON admin_notifications(read_at, created_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shift_rules (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            clock_in_window_start TEXT NOT NULL DEFAULT '04:00',
            clock_in_window_end TEXT NOT NULL DEFAULT '06:30',
            regular_hours REAL NOT NULL DEFAULT 8.0,
            overtime_notify_hours REAL NOT NULL DEFAULT 8.1,
            hard_cutoff_hours REAL NOT NULL DEFAULT 11.0,
            working_days TEXT NOT NULL DEFAULT '6,0,1,2,3,4',
            site_timezone TEXT NOT NULL DEFAULT 'Africa/Cairo',
            updated_at DATETIME NOT NULL,
            updated_by TEXT
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO shift_rules (id, updated_at) VALUES (1, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
    )


def migration_2_provenance_columns(conn: sqlite3.Connection) -> None:
    for column, ddl in (
        ("lat", "FLOAT"),
        ("lon", "FLOAT"),
        ("accuracy", "FLOAT"),
        ("source", "TEXT NOT NULL DEFAULT 'online'"),
        ("status_code", "TEXT"),
        ("liveness_class", "TEXT"),
        ("liveness_score", "REAL"),
        ("flag_reason", "TEXT"),
        ("approved_hours", "REAL"),
        ("overtime_hours", "REAL"),
        ("reviewed_by", "TEXT"),
        ("reviewed_at", "DATETIME"),
        ("client_timestamp", "DATETIME"),
        ("device_id", "TEXT"),
        ("request_id", "TEXT"),
    ):
        add_column(conn, "attendance_logs", column, ddl)

    for column, ddl in (
        ("start_source", "TEXT NOT NULL DEFAULT 'online'"),
        ("late_flag", "TEXT"),
        ("overtime_notified_at", "DATETIME"),
    ):
        add_column(conn, "active_sessions", column, ddl)

    for column, ddl in (
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ("enrolled_at", "DATETIME"),
        ("template_version", "INTEGER NOT NULL DEFAULT 0"),
        ("hourly_rate", "REAL"),
    ):
        add_column(conn, "users", column, ddl)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attendance_worker_ts ON attendance_logs(worker_id, timestamp)"
    )

    # Derived, deterministic, lossless: translate the human status text into the
    # canonical code. Unlike backfilling coordinates (which would fabricate
    # evidence), this invents nothing - it re-expresses a value that is already
    # there and keeps the original text for display.
    for text, code in STATUS_CODE_MAP.items():
        conn.execute(
            "UPDATE attendance_logs SET status_code = ? WHERE status_code IS NULL AND status = ?",
            (code, text),
        )
    # Anything unrecognised must not be treated as approved.
    conn.execute(
        "UPDATE attendance_logs SET status_code = 'pending_review' WHERE status_code IS NULL"
    )


def migration_3_offline_devices(conn: sqlite3.Connection) -> None:
    """Offline punch infrastructure (Phase 5 tables, created now so provenance
    columns on attendance_logs always have a referent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            key_salt TEXT NOT NULL,
            key_epoch INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            last_seen_at DATETIME,
            revoked_at DATETIME,
            UNIQUE (worker_id, device_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS punch_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_punch_id TEXT NOT NULL UNIQUE,
            device_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            action TEXT NOT NULL,
            client_timestamp DATETIME NOT NULL,
            client_offset_s INTEGER,
            lat REAL,
            lon REAL,
            accuracy REAL,
            location_trusted INTEGER NOT NULL DEFAULT 0,
            photo_sha256 TEXT,
            signature TEXT NOT NULL,
            received_at DATETIME NOT NULL,
            status TEXT NOT NULL,
            rejection_code TEXT,
            materialized_log_id INTEGER
        )
        """
    )


def migration_4_users_token_version(conn: sqlite3.Connection) -> None:
    """Repair the missing ``users.token_version`` column.

    ``security.py`` compares the token's ``ver`` claim against
    ``COALESCE(token_version, 0)`` and both password endpoints bump the column, but
    no earlier revision ever created it: ``baseline_schema()`` did not declare it
    and migrations 1-3 did not add it. On a database created by the prototype the
    write path therefore failed with ``no such column: token_version`` - login
    through ``/auth/login`` returned 500 and every password change did too.

    ``DEFAULT 0`` is deliberate rather than cosmetic: every token already issued
    carries ``ver`` 0 (or omits it, which decodes as 0), so existing sessions stay
    valid. A repair that revoked every live session on a site mid-shift would be
    worse than the bug.
    """
    add_column(conn, "users", "token_version", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attendance_log_status ON attendance_logs(status_code, timestamp)"
    )


def _unique_index_if_clean(
    conn: sqlite3.Connection, name: str, table: str, columns: str
) -> None:
    """Create a UNIQUE index, degrading to a plain one if legacy rows collide.

    A UNIQUE index is a real correctness guarantee (it is what makes the punch
    replay ledger authoritative), so it is created unconditionally on a clean
    table. On a table that already holds duplicates the statement would abort the
    whole migration, so the fallback keeps the deployment running and the plain
    index still serves the lookup. The collision is visible either way, because
    the punch path reports ``replayed_nonce`` from its own query as well.
    """
    try:
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    except sqlite3.IntegrityError:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name}_nonunique ON {table}({columns})")


def migration_5_phase02_enrollment_and_sync(conn: sqlite3.Connection) -> None:
    """Phase 02: rapid enrollment, liveness provenance and offline sync support.

    Additive only. No existing table is rewritten and no row is modified, so
    ``users``, ``attendance_logs``, ``active_sessions`` and ``construction_sites``
    keep both their shape and their data. Every added column is NULL-able or has a
    constant default, because SQLite cannot add a column with a non-constant
    default to a non-empty table.
    """
    # --- self-service enrollment invites -------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrollment_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            created_by TEXT,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0,
            revoked_at DATETIME,
            completed_at DATETIME,
            last_used_at DATETIME,
            last_used_ip TEXT,
            note TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invites_worker ON enrollment_invites(worker_id, created_at)"
    )

    # --- bulk onboarding jobs ------------------------------------------------
    # A job's progress lives in the database rather than in the background task's
    # memory: it survives a restart, it is pollable by the dashboard, and a batch
    # that died half way can be resumed item by item.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrollment_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'bulk_csv_zip',
            status TEXT NOT NULL DEFAULT 'queued',
            created_by TEXT,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            source_name TEXT,
            dry_run INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            processed INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            detail TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enroll_jobs_status ON enrollment_jobs(status, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrollment_job_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            worker_id TEXT,
            name TEXT,
            role TEXT,
            email TEXT,
            phone TEXT,
            photo_name TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            error_code TEXT,
            error_detail TEXT,
            template_version INTEGER,
            created_at DATETIME NOT NULL,
            finished_at DATETIME
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enroll_items_job ON enrollment_job_items(job_id, status)")

    # --- offline punch provenance -------------------------------------------
    for column, ddl in (
        ("nonce", "TEXT"),
        ("anchor_server_time", "DATETIME"),
        ("monotonic_offset_s", "REAL"),
        ("signature_version", "INTEGER NOT NULL DEFAULT 1"),
        ("liveness_class", "TEXT"),
        ("processed_at", "DATETIME"),
        ("flag_reason", "TEXT"),
        ("anchor_id", "TEXT"),
    ):
        add_column(conn, "punch_queue", column, ddl)

    # Issued time anchors. The anchor is what binds an offline punch to a moment the
    # device was genuinely online, so it must be signed with a SERVER-ONLY key and
    # recorded here: a device key is held by the client, so a signature made with it
    # could be forged by the very client it is meant to constrain.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_anchors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anchor_id TEXT NOT NULL UNIQUE,
            device_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            server_time DATETIME NOT NULL,
            issued_at DATETIME NOT NULL,
            issued_ip TEXT,
            consumed_by TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anchors_device ON device_anchors(device_id, issued_at)")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_punch_status ON punch_queue(status, received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_punch_worker ON punch_queue(worker_id, client_timestamp)")
    # The replay ledger. A re-sent nonce is a byte-identical copy of a request the
    # server already accepted, which is exactly what a captured-packet replay is.
    _unique_index_if_clean(conn, "idx_punch_nonce", "punch_queue", "device_id, nonce")

    # --- device enrollment provenance ---------------------------------------
    add_column(conn, "worker_devices", "note", "TEXT")
    add_column(conn, "worker_devices", "registered_ip", "TEXT")
    add_column(conn, "worker_devices", "last_anchor_at", "DATETIME")

    # Liveness provenance on the session row, so a shift that started from a
    # genuine frame is distinguishable from an admin override or an offline punch.
    add_column(conn, "active_sessions", "liveness_class", "TEXT")


def migration_6_punch_queue_site_name(conn: sqlite3.Connection) -> None:
    """Store the geofence decision taken during punch verification.

    This is deliberately a NEW migration rather than an edit to migration 5. Migration 5
    had already been applied to the live database, and ``schema_migrations`` records
    versions, not contents: editing an applied migration means its statement never runs
    again, so the column simply never appears and every punch insert fails with
    "no column named site_name". Found exactly that way, against the already-migrated
    database, which is why the rule is *never edit an applied migration - add one*.

    The value is stored rather than re-derived because materialising a queued punch must
    not depend on the current site list: a site deleted between capture and sync would
    otherwise silently reclassify a verified punch.
    """
    add_column(conn, "punch_queue", "site_name", "TEXT")


def migration_7_worker_notes(conn: sqlite3.Connection) -> None:
    """Worker notes: the written channel between a worker and the administrator.

    A request a worker can only make by catching somebody on the phone ("my password
    stopped working", "the cement never arrived", "my Wednesday is missing from the
    timesheet") is a request that quietly never happens. Two tables give it a record:
    the note itself (who, what kind, where it stands) and the messages that make it a
    two-way thread rather than a form nobody answers.

    ``worker_id`` is a plain column, not a foreign key: this database deliberately
    declares no FKs (see ``baseline_schema``), and the note must survive an account
    being recreated with the same id.

    Statuses are stored as codes (``open`` / ``in_progress`` / ``resolved`` / ``closed``)
    for the same reason attendance has ``status_code`` beside its display text: logic
    must never be built on a string a future translator might reword.

    The two unread counters are denormalised on purpose. A badge that has to aggregate
    the message table on every poll is the kind of query that gets dropped from the
    dashboard for being slow, and then "the admin never saw my note" stops being
    visible. They are integers this code owns end to end, so there is nothing to drift.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            last_reply_at DATETIME,
            resolved_at DATETIME,
            resolved_by TEXT,
            closed_at DATETIME,
            closed_by TEXT,
            admin_unread INTEGER NOT NULL DEFAULT 1,
            worker_unread INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_notes_status ON worker_notes(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_notes_worker ON worker_notes(worker_id, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_note_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            author_id TEXT NOT NULL,
            author_role TEXT NOT NULL,
            body TEXT NOT NULL,
            internal INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_note_messages_note ON worker_note_messages(note_id, id)"
    )


def migration_8_registration_invites(conn: sqlite3.Connection) -> None:
    """Registration links: an invite that creates the account instead of just its face.

    ``enrollment_invites`` was written for one job - register the face of an account
    that already exists - so it has no room for a person who does not exist yet. Rather
    than a second table with the same token machinery, the invite grows a ``kind`` and
    the columns describing the *pending* account, empty for the original kind.

    Why one table and not two: a token is a token. Expiry, single use, revocation, the
    hashed-at-rest secret and the "who sent this and when" audit are identical for both
    kinds, and a second table would mean a second copy of that logic that could drift
    out of step with the first - exactly the failure mode this codebase deletes
    elsewhere. The kind is what differs, and it is a column.

    ``kind`` defaults to ``'enroll'``, which is what every existing row already is, so
    the ALTER cannot reinterpret a link somebody is holding right now. The four pending
    columns are NULL for an enrollment invite, and the code that reads them is guarded
    by the kind, so an old row can never be mistaken for a pending account.

    ``pending_role`` is stored rather than derived because the role is chosen by the
    administrator when the link is issued, never by whoever opens it. A link is a bearer
    token sent over WhatsApp, so a link that let its holder pick a role would be a
    privilege-escalation lever that leaks with the message.
    """
    add_column(conn, "enrollment_invites", "kind", "TEXT NOT NULL DEFAULT 'enroll'")
    add_column(conn, "enrollment_invites", "pending_name", "TEXT")
    add_column(conn, "enrollment_invites", "pending_role", "TEXT")
    add_column(conn, "enrollment_invites", "pending_email", "TEXT")
    add_column(conn, "enrollment_invites", "pending_phone", "TEXT")
    # The dashboard lists links by kind ("who is waiting to register"), and the index
    # keeps that from being a table scan as link history accumulates.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invites_kind ON enrollment_invites(kind, created_at)"
    )


def migration_9_unpaid_break_and_auto_close(conn: sqlite3.Connection) -> None:
    """The unpaid break, and the end of the paid day.

    Three new shift rules and one new column on the shift itself.

    ``break_minutes`` (30) is the unpaid break; ``break_after_hours`` (4) is how long a
    shift must run before one is assumed to have been taken, so a short part-shift is
    not charged half an hour it never took; ``auto_close_at_regular`` (on) is whether
    the system writes the clock-out when the paid hours reach ``regular_hours``.

    ``attendance_logs.break_hours`` is what the shift *recorded* as break. Without it
    the deduction would be invisible: a shift that ran 8.5 h on site and reads 8.0 h
    paid looks like a bug, and neither the worker nor the administrator could tell an
    unpaid break from a lost half hour. Its ``hours`` column keeps its meaning (what is
    paid); ``hours + break_hours`` is time on site, for every row written from here on.
    Rows written before this migration have NULL and mean "no break was ever recorded",
    which is read-only history rather than a claim about the past.

    Deliberately additive, and deliberately not backfilled: recomputing old shifts with
    a break nobody had agreed to yet would rewrite paid hours that have already been
    reported.
    """
    add_column(conn, "shift_rules", "break_minutes", "REAL NOT NULL DEFAULT 30.0")
    add_column(conn, "shift_rules", "break_after_hours", "REAL NOT NULL DEFAULT 4.0")
    add_column(conn, "shift_rules", "auto_close_at_regular", "INTEGER NOT NULL DEFAULT 1")
    add_column(conn, "attendance_logs", "break_hours", "FLOAT")


def migration_10_quick_links(conn: sqlite3.Connection) -> None:
    """One-tap clock links, and the log of every punch one produced.

    A quick link is a *credential*: an administrator issues it for one worker, sends it
    through WhatsApp, and the worker taps it and is clocked in - or out - without a
    password. That makes two things worth storing, and they are two tables because they
    answer two different questions:

    * ``quick_links`` - which links exist, for whom, until when, how many times they may
      be used, and whether an administrator has revoked one. The token itself is stored
      as a SHA-256 hash and never in readable form: a database leak must not hand somebody
      a working clock-in, which is the same rule ``enrollment_invites`` already follows.
    * ``quick_link_uses`` - every punch a link has produced, with the selfie that was
      taken, where the phone was, and the attendance row it wrote. This is the *manual
      review* the whole feature is built around: the photo is deliberately not compared
      against the worker's face reference (a link somebody else may be holding is not
      proof of who is holding it), so a human looks at it afterwards, and this table is
      where what they look at is kept.

    Additive only: no existing table changes shape, and nothing is backfilled.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quick_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            created_by TEXT,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 0,
            uses INTEGER NOT NULL DEFAULT 0,
            revoked_at DATETIME,
            last_used_at DATETIME,
            last_used_ip TEXT,
            note TEXT
        )
        """
    )
    # ``max_uses`` 0 means "as many taps as the shift needs until it expires": a clock
    # link that dies after one tap cannot clock anybody *out*, which is half its job.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quick_links_worker ON quick_links(worker_id, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quick_link_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            action TEXT NOT NULL,
            site_name TEXT,
            lat REAL,
            lon REAL,
            accuracy REAL,
            log_id INTEGER,
            photo_path TEXT,
            ip TEXT,
            user_agent TEXT,
            face_count INTEGER,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quick_link_uses_link ON quick_link_uses(link_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quick_link_uses_log ON quick_link_uses(log_id)"
    )


def migration_11_biometric_ids(conn: sqlite3.Connection) -> None:
    """Give every account an immutable id to file its biometric files under.

    The template and the reference selfie were named after the account id
    (``local_references/1.json``), which made a face reachable from a number anybody
    can iterate, and - worse - made a template **inheritabe**: account ids are issued
    again after a deletion, and a template the delete could not remove belonged to
    whoever took the id next, so a new worker's punch could be scored against the
    previous worker's face.

    ``biometrics`` owns the id format and every path built from it; this only records
    one per row. The existing files are renamed by ``biometrics.adopt_legacy_files()``
    right after the migrations run (see ``main.init_db``) rather than from here, so that
    this function stays a pure, replayable statement about the schema - the drift guard
    compiles it against an in-memory database, where a filesystem rename would have
    nothing to work with and no user rows to do it for.

    Nullable column on purpose: a fresh install has no rows to backfill, and a row that
    somehow arrived without an id is repaired by ``biometrics.ensure_id`` at first use
    rather than by a migration that cannot run again.
    """
    add_column(conn, "users", "biometric_id", "TEXT")

    used = {
        str(row[0])
        for row in conn.execute(
            "SELECT biometric_id FROM users WHERE biometric_id IS NOT NULL AND biometric_id <> ''"
        )
    }
    pending = conn.execute(
        "SELECT id FROM users WHERE biometric_id IS NULL OR biometric_id = ''"
    ).fetchall()
    for row in pending:
        candidate = biometrics.new_id()
        while candidate in used:  # pragma: no cover - 2^128 draws; kept so a repeat is impossible
            candidate = biometrics.new_id()
        conn.execute(
            "UPDATE users SET biometric_id = ? WHERE id = ?", (candidate, str(row[0]))
        )
        used.add(candidate)

    # Uniqueness is the property the whole arrangement rests on: two accounts sharing an
    # id would share a face, and the database is the only place that can refuse it.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_biometric_id ON users(biometric_id)"
    )


def _hhmm_check(column: str) -> str:
    """A column CHECK that accepts ``NULL`` or a strict ``HH:MM`` time, and nothing else.

    ``GLOB`` is SQLite's own globbing and is case-sensitive with no character classes beyond
    ranges, which is enough here: the two alternatives are exactly the two halves of
    ``^([01][0-9]|2[0-3]):[0-5][0-9]$``. Written out rather than left to the application
    because the application is not the only writer - an operator with a ``sqlite3`` session, a
    restored dump and a hand-edited CSV all reach this column, and a window that does not
    parse is not a validation failure at the gate, it is a window that silently stops being
    applied (see ``shift_windows``).
    """
    return (
        f"CHECK ({column} IS NULL "
        f"OR {column} GLOB '[01][0-9]:[0-5][0-9]' "
        f"OR {column} GLOB '2[0-3]:[0-5][0-9]')"
    )


def migration_12_site_clock_in_windows(conn: sqlite3.Connection) -> None:
    """Let a site run its own shift, including one that crosses midnight.

    Three nullable columns on ``construction_sites``: a start, an end and a timezone. The
    window an arrival is measured against is now the site's, falling back **per field** to
    ``shift_rules`` - so a night site can set only the hours and keep the company timezone,
    and the global rule stays the default it has always been.

    **Why NULL and not ``DEFAULT 'Africa/Cairo'``.** Stamping a default on this column would
    write a value into every existing site, and a value *overrides* the global
    ``shift_rules.site_timezone`` - so an installation whose global zone is not Cairo would
    have every site silently moved to Cairo by an upgrade, changing who is late at 04:00 with
    nobody having configured anything. NULL is the honest encoding of "a site that has not
    chosen", and it is what makes the fallback in ``shift_windows`` possible at all. The
    documented default lives in ``shift_windows.DEFAULT_TIMEZONE`` and is applied when neither
    the site nor the global rules name a zone.

    **Why a database CHECK as well as the API's validator.** The admin API refuses ``7:30``
    and ``25:00`` where an administrator can see the error. This is the second line, for
    values that arrive from somewhere else, and it is deliberately the same language
    (``HHMM_PATTERN``) so the two cannot drift apart into two different opinions about what a
    time is.

    **Nothing is backfilled.** Every existing site keeps exactly the behaviour it had before
    this migration: NULL means inherit, and the inherited value is the one already in force.
    """
    add_column(conn, "construction_sites", "clock_in_window_start", f"TEXT {_hhmm_check('clock_in_window_start')}")
    add_column(conn, "construction_sites", "clock_in_window_end", f"TEXT {_hhmm_check('clock_in_window_end')}")
    # No CHECK: only the runtime knows the tzdata it can resolve, so the API validates this
    # against ``zoneinfo`` (see ``shift_windows.is_known_timezone``) rather than against a
    # list hardcoded into the schema.
    add_column(conn, "construction_sites", "site_timezone", "TEXT")


def migration_13_retention_runs(conn: sqlite3.Connection) -> None:
    """A row per applied retention sweep: what it erased, when, and how much.

    Why a table and not a log line. The question an operator asks about a retention system is
    not "is it configured?" but "when did it last actually run, and did it succeed?" - and a
    log line is gone after the next rotation or restart, which is exactly when nobody is
    looking. Readiness reads this table (``retention.last_run``), so a sweeper that has been
    silently dead for a month is visible as a failing advisory check rather than as a policy
    that is assumed to be running.

    ``dry_run`` is stored for completeness and is always 0: a dry run writes nothing at all,
    including here, because "show me what you would delete" must not be a way to write to the
    audit trail - its guarantee is that the connection is *read-only*.

    The digest columns are the audit trail's audit trail: the same fingerprints the compliance
    event carries, so the deletion can be verified from either place.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retention_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at DATETIME NOT NULL,
            finished_at DATETIME,
            dry_run INTEGER NOT NULL DEFAULT 0,
            actor TEXT,
            policy_json TEXT,
            summary_json TEXT,
            deleted_total INTEGER NOT NULL DEFAULT 0,
            bytes_wiped INTEGER NOT NULL DEFAULT 0,
            failures_total INTEGER NOT NULL DEFAULT 0,
            digest TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retention_runs_started ON retention_runs(started_at DESC)")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "audit_notifications_shift_rules", migration_1_audit_notifications_shift_rules),
    (2, "provenance_columns_status_code", migration_2_provenance_columns),
    (3, "offline_devices_and_punch_queue", migration_3_offline_devices),
    (4, "users_token_version_drift_repair", migration_4_users_token_version),
    (5, "phase02_enrollment_liveness_offline", migration_5_phase02_enrollment_and_sync),
    (6, "punch_queue_site_name", migration_6_punch_queue_site_name),
    (7, "worker_notes", migration_7_worker_notes),
    (8, "registration_invites", migration_8_registration_invites),
    (9, "unpaid_break_and_auto_close", migration_9_unpaid_break_and_auto_close),
    (10, "quick_links", migration_10_quick_links),
    (11, "biometric_ids", migration_11_biometric_ids),
    (12, "site_clock_in_windows", migration_12_site_clock_in_windows),
    (13, "retention_runs", migration_13_retention_runs),
]


# ---------------------------------------------------------------------------
# schema inspection / execution
# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the baseline tables (idempotent) and stamp the application id."""
    for statement in baseline_schema():
        conn.execute(statement)
    try:
        current = conn.execute("PRAGMA application_id").fetchone()[0]
        if int(current or 0) != APPLICATION_ID:
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    except sqlite3.Error:
        pass


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    try:
        return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
    except sqlite3.Error:
        return set()


def current_version(conn: sqlite3.Connection) -> int:
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def pending_migrations(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    applied = applied_versions(conn)
    return [(version, name) for version, name, _ in MIGRATIONS if version not in applied]


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in one transaction each. Returns applied versions."""
    conn.isolation_level = None  # autocommit: we manage the transaction ourselves
    if conn.in_transaction:  # pragma: no cover - defensive
        conn.commit()
    applied_now: list[int] = []
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at DATETIME NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read inside the write transaction: another worker may have applied a
        # migration between our earlier check and acquiring the write lock.
        known = applied_versions(conn)
        for version, name, function in MIGRATIONS:
            if version in known:
                continue
            function(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (
                    version,
                    name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    _checksum(name),
                ),
            )
            applied_now.append(version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied_now


def _checksum(name: str) -> str:
    import hashlib

    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def ensure_bootstrap_admin(conn: sqlite3.Connection) -> dict | None:
    """Create a head admin *only* when none exists.

    Two things this deliberately does NOT do, both of which were live defects:

    * it never rewrites the password of an existing user (the shipped code
      re-hashed a hardcoded demo password into user 1 on every single startup);
    * it never creates a demo worker with a published password.

    On a database that already has a head admin this is a no-op, so it cannot
    lock an operator out of a working system.
    """
    try:
        existing = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'head_admin'").fetchone()[0]
    except sqlite3.Error:
        return None
    if existing:
        return None

    generated = settings.bootstrap_admin_password is None
    password = settings.bootstrap_admin_password or secrets.token_urlsafe(12)
    try:
        password_hash = hash_password(password, enforce_policy=False)
    except Exception as exc:  # pragma: no cover - defensive
        raise ConfigError(f"BOOTSTRAP_ADMIN_PASSWORD could not be hashed: {exc}") from exc

    conn.execute(
        "INSERT INTO users (id, name, email, phone, password_hash, role, biometric_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "5000",
            "Head Admin",
            "admin@siteops.com",
            "+200000000000",
            password_hash,
            "head_admin",
            biometrics.new_account_id("5000"),
        ),
    )
    return {"id": "5000", "generated": generated, "password": password if generated else None}


def initialize(conn: sqlite3.Connection) -> dict:
    """Bring a database up to date: baseline tables, migrations, bootstrap admin."""
    ensure_schema(conn)
    applied_now = run_migrations(conn)
    bootstrap = ensure_bootstrap_admin(conn)
    if bootstrap and bootstrap.get("password"):
        print(
            "\n" + "!" * 72 + "\n"
            "  FIRST RUN: a Head Admin account was created.\n"
            "    user id : 5000\n"
            "    email   : admin@siteops.com\n"
            f"    password: {bootstrap['password']}\n"
            "  This password is shown once and is not stored in plaintext anywhere.\n"
            "  Change it immediately from the app, or set BOOTSTRAP_ADMIN_PASSWORD.\n" + "!" * 72 + "\n",
            file=sys.stderr,
        )
    return {
        "schema_version": current_version(conn),
        "migrations_applied": applied_now,
        "bootstrap_admin": bootstrap,
    }
