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
SCHEMA_VERSION = 26

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
    # The two outcomes of a refusal (migration-free: they are new *values* in an existing
    # column, not new columns). They are different codes because they mean opposite things
    # about the hours, and one code cannot carry both. Spelled out here rather than
    # referring to the constants below, which are defined after this table - the same way
    # the 8 h close above is spelled out.
    "Overtime Rejected": "overtime_rejected",
    "Rejected": "rejected",
    # An offline punch that was *materialised* rather than confirmed. It is neither approved nor
    # awaiting a decision of its own - see ``STATUS_CODE_UNVERIFIED_OFFLINE``.
    "Unverified (offline)": "unverified_offline",
}

#: The status row written when the system closes a shift because its paid hours have
#: reached ``regular_hours`` (see ``overtime.scan_auto_close``). The label is what a
#: human reads on the timesheet, the code is what payroll logic matches on - the same
#: split attendance has had since migration 2.
STATUS_AUTO_CLOSED_LABEL = "Auto-Closed (8h Limit)"
STATUS_CODE_AUTO_CLOSED_8H = "auto_closed_8h"

#: The status written when an administrator **refuses the hours past the regular day**.
#:
#: The work happened and the standard day is owed, so this code is *payable* (see
#: ``reports.PAYABLE_CODES``) at the regular hours the administrator left in
#: ``approved_hours`` - the overtime is not credited. It is a code of its own rather than
#: ``approved`` for the reason ``auto_closed_8h`` is: a reader of the timesheet has to be
#: able to tell a refused-overtime day from an ordinary one without reading the audit log,
#: and a settled row must not be silently rewritten into a plain approval.
STATUS_OVERTIME_REJECTED_LABEL = "Overtime Rejected"
STATUS_CODE_OVERTIME_REJECTED = "overtime_rejected"

#: The status written when an administrator decides an **unconfirmed punch was not work at
#: all** - the location was outside every site's radius, or the face did not match. It
#: counts for nothing: ``reports._shift_hours`` returns zero for any code that is neither
#: payable nor awaiting a decision, which is the arithmetic "not worked" has to have. The
#: attendance report already knew this code by name (``flagged``) before anything wrote it.
STATUS_REJECTED_LABEL = "Rejected"
STATUS_CODE_REJECTED = "rejected"

#: The status of an offline punch that has been recorded but never checked: a signed queue row
#: materialised into attendance, whose frame nobody looked at when it was taken.
#:
#: It is a code of its own because **both** of the alternatives are wrong. ``approved`` would
#: claim a check that never happened; ``pending_review`` - correct for the clock-out that decides
#: the pay - puts a row with no hours and no decision in front of an administrator, and worse,
#: the clock-out and quick-link paths refuse to close a shift while *any* ``pending_review`` row
#: exists (see ``main.py``). A worker who arrived with no signal would then be unable to clock
#: out at all until somebody approved their arrival - the app would punish them for the dead spot,
#: and the day's hours would never be recorded. Nor is it payable: ``reports.PAYABLE_CODES`` does
#: not contain it, so an arrival contributes nothing to a total either way.
STATUS_LABEL_UNVERIFIED_OFFLINE = "Unverified (offline)"
STATUS_CODE_UNVERIFIED_OFFLINE = "unverified_offline"

DEFAULT_SHIFT_RULES = {
    "clock_in_window_start": "04:00",
    "clock_in_window_end": "06:30",
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    "hard_cutoff_hours": 11.0,
    "working_days": "6,0,1,2,3,4",
    "site_timezone": "Asia/Kuwait",
    # -- the unpaid break and the end of the day (migration 9) -----------------
    # A full day is 8 h paid plus a 30-minute break that is not paid, so a full day
    # runs 8.5 h from clock-in to clock-out. The break is deducted only from a shift
    # long enough to have contained one (``break_after_hours``, half a working day),
    # so a two-hour part-shift is paid as worked rather than losing half an hour it
    # never took. See ``shift_hours.py``.
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    #: Whether the system writes the clock-out itself when the paid hours reach
    #: ``regular_hours``.
    #:
    #: **Off by default, and the default was on until it was noticed that on was not
    #: true.** The shipped pair is ``overtime_notify_hours`` 8.1 against ``regular_hours``
    #: 8.0, i.e. the alert line sits *above* the paid day - and ``shift_hours.day_end_rules``
    #: resolves that pair by standing the close down, because a shift the close ended at 8 h
    #: could never reach 8.1 h and the crossing would be unobservable. So the switch read
    #: "on" on every fresh deployment while the close never once acted, which is exactly the
    #: state ``readiness``'s ``overtime_close_deferred`` advisory exists to report - and a
    #: fresh volume was born failing it. Shipping the value the behaviour already implies is
    #: therefore both the fix and the honest description: nothing observable changes (nothing
    #: was being closed), the console stops showing a switch that does nothing, and the
    #: overtime workflow is explicitly the owner of the end of the day.
    #:
    #: The forgotten-shift case the close existed for is still covered: the crossing alert
    #: fires at 8.1 h and keeps watching until somebody clocks out, and the hours past the
    #: paid day go to overtime review. An operator who wants the close to *act* sets it back
    #: on **and** moves the alert strictly below ``regular_hours`` - the two numbers are one
    #: decision, and ``day_end_rules`` is where that is decided.
    "auto_close_at_regular": 0,
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
            site_timezone TEXT NOT NULL DEFAULT 'Asia/Kuwait',
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
    not charged half an hour it never took; ``auto_close_at_regular`` is whether the
    system writes the clock-out when the paid hours reach ``regular_hours``.

    That last one is added **off**, and the DDL default below is what a fresh volume is
    born with, so the value here is the whole of the shipped behaviour rather than a
    starting point something later overrides. It was shipped on, against an overtime alert
    line (8.1) above the paid day (8.0) - a pair ``shift_hours.day_end_rules`` resolves by
    standing the close down. The switch was on and the close never acted, on every new
    deployment, which ``readiness``'s ``overtime_close_deferred`` reports and which nothing
    else in the application would have told an operator. Turning a rule *off* by default is
    a policy change and this one is deliberate: it changes no shift that would have been
    closed, because under these two figures none ever was.

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
    # Off: see the note on ``DEFAULT_SHIFT_RULES`` above and on this migration's docstring.
    # ``add_column`` fills the existing single row with this default, so on a fresh database
    # this literal - not the dict - is what the deployment is born with.
    add_column(conn, "shift_rules", "auto_close_at_regular", "INTEGER NOT NULL DEFAULT 0")
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

    **Why NULL and not a stamped default.** Stamping ``DEFAULT 'Asia/Kuwait'`` on this column would
    write a value into every existing site, and a value *overrides* the global    ``shift_rules.site_timezone``- so an installation whose global zone is not Kuwait would
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


def migration_14_worker_notifications_and_push(conn: sqlite3.Connection) -> None:
    """The worker's own notification channel: a record per worker, and where to reach them.

    WHY A SECOND TABLE AND NOT A COLUMN ON ``admin_notifications``
    -------------------------------------------------------------
    ``admin_notifications`` is *about* workers but it is **for** administrators - it is the
    triage queue, it carries the audit-facing payloads, and its read state is an operator's.
    A worker's inbox is the other side of the same events: "your shift passed the overtime
    line", read by the person it happened to, pruned on its own schedule, and never carrying
    an administrative payload. Folding them together would mean every admin alert needed an
    audience column, one read-state meaning two different things, and a bug in the filter
    handing a worker somebody else's queue. Two tables cost one insert per event, in the same
    transaction, which is what ``notifications.notify_worker`` does.

    ``delivered_at`` / ``delivery_attempts`` / ``last_error`` are the delivery state of the
    *push* channel, not of the inbox. A notification whose push failed is still a
    notification the worker will see when they next open the app, so delivery failure never
    withholds the record - it only stops the server retrying for ever.

    ``worker_push_subscriptions`` stores what a browser hands us when a worker allows
    notifications. ``endpoint`` is UNIQUE because re-subscribing the same browser must update
    the row rather than add one: a phone that reinstalls the app is the same phone.
    ``revoked_at`` is how a subscription a push service has told us is gone is retired - kept
    rather than deleted, so "why did my alerts stop" has an answer on the row.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            payload TEXT,
            dedupe_key TEXT UNIQUE,
            created_at DATETIME NOT NULL,
            read_at DATETIME,
            delivered_at DATETIME,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_notifications_inbox "
        "ON worker_notifications(worker_id, created_at DESC)"
    )
    # The delivery scan's index: undelivered rows, oldest first. Without it every dispatch
    # walks the whole inbox, which is the one table that only ever grows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_notifications_undelivered "
        "ON worker_notifications(delivered_at, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT,
            created_at DATETIME NOT NULL,
            last_ok_at DATETIME,
            last_error TEXT,
            revoked_at DATETIME
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_push_subscriptions_worker "
        "ON worker_push_subscriptions(worker_id, revoked_at)"
    )


def migration_16_company_branding(conn: sqlite3.Connection) -> None:
    """The company's own name and mark, so a screen can say whose app this is.

    WHY A TABLE AND NOT A CONSTANT
    ------------------------------
    The wordmark, the legal suffix, the founding year and the tagline were a JavaScript
    object in ``frontendjavascript.js``, and the mark was a file beside it. That is the
    right place for the *artwork* and the wrong place for the *name*: every screen that
    says whose payroll this is - the login panel, the handset header, the console rail,
    the printed timesheet - read that constant, so a company that renamed itself, or
    wanted its own logo on the sheet it hands to payroll, needed a code change and a
    redeploy. A settings row is the same idea as ``shift_rules`` next door: the company's
    own numbers and words, editable where the company is administered.

    NULL AND THE EMPTY STRING MEAN DIFFERENT THINGS, ON PURPOSE
    -------------------------------------------------------------
    ``NULL`` is "nobody has touched this column", and the shipped lockup is used - which
    is what every deployment that never opens the panel keeps, byte for byte. An **empty
    string** is a decision: the line is not printed. A company with no legal suffix and no
    founding year has to be able to *remove* those lines, and a rule of "blank means the
    shipped value" (which is what ``shift_rules`` does, because a blank clock-in window
    would be a real hours bug) would put them straight back. So the four text columns are
    nullable with no default, and the reader distinguishes the two states.

    The mark is stored as bytes rather than as a path for the same reason the rest of this
    row is stored: it travels with the backup, there is no file to lose when a deployment
    moves, and nothing on disk has to be writable for a company to have a logo. It is
    always an image this application re-encoded itself (see ``branding.encode_logo``), so
    the stored type is one of two it chose, never one a browser guessed at.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            company_name TEXT,
            company_legal TEXT,
            company_est TEXT,
            company_tagline TEXT,
            logo_bytes BLOB,
            logo_mime TEXT,
            logo_width INTEGER,
            logo_height INTEGER,
            logo_version INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME,
            updated_by TEXT
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO company_settings (id, updated_at) VALUES (1, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
    )


def migration_15_worker_report_columns(conn: sqlite3.Connection) -> None:
    """Which columns this account's *own* timesheet files carry.

    A preference, and an account's rather than a browser's. The file a worker hands in is
    theirs and is handed in from wherever they happen to be: keeping the choice in the
    browser's storage would lose it with the phone, and would give the same account two
    different files depending on which device it downloaded from. The value is the
    comma-joined column ids, in the order ``reports.REPORT_COLUMNS`` declares, and NULL
    means "never chose" - which is served as the default rather than as an empty file.

    No CHECK constraint on the contents, for the reason ``construction_sites`` has none on
    its window either: the vocabulary belongs to the application that fills the cells (see
    ``reports.REPORT_COLUMNS``), and a schema that hardcoded today's column ids would have
    to be migrated by every release that adds one. The reader normalises regardless of what
    is stored, so junk on this column can never reach a file - it degrades to the default.
    """
    add_column(conn, "users", "report_columns", "TEXT")


def migration_17_punch_queue_photo_scored_at(conn: sqlite3.Connection) -> None:
    """When the queued selfie for this punch was scored at sync.

    A punch taken with no signal carries a selfie the *phone* decides to keep, and the
    server only ever sees it if the phone uploads it after the queue arrives. This column
    is when that happened - ``NULL`` means the punch is still waiting for its frame to be
    scored, which is what makes a retried upload harmless: the first scoring owns the row
    and a second attempt is answered from it instead of re-running the models and
    re-writing the evidence.

    It is a timestamp rather than a boolean because "this was scored, and when" is the
    question an operator asks of a review queue, and a stamp answers both. Additive and
    nullable, so the rows already in the table - punches whose photos were never
    uploaded, which is every punch from before this column existed - keep the meaning they
    had.
    """
    add_column(conn, "punch_queue", "photo_scored_at", "DATETIME")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_punch_photo_pending "
        "ON punch_queue(status, photo_scored_at, received_at)"
    )


def migration_18_punch_frame(conn: sqlite3.Connection) -> None:
    """The downscaled frame stored with every punch, as the review card's evidence.

    A pending review used to arrive as numbers only - a distance, a liveness verdict, a flag
    sentence - with the frame those numbers were measured from decoded, used and dropped.
    This column is the name of the stored copy, filed under a random name in ``punch_frames/``
    and served by log id to an administrator (see ``punch_frames`` and
    ``/admin/pending_review_frame``).

    It is evidence, not a verdict, and it is optional in both directions: rows written before
    this column existed keep their numbers without a picture, and a row that could not keep
    its frame (a full disk) keeps its numbers without one too - the score is the record, the
    frame is the picture beside it. Retention sweeps the file and clears the column on the
    same window (``retention._sweep_punch_frames``), and serving the frame never widens past
    ``admin_only``.
    """
    add_column(conn, "attendance_logs", "punch_frame", "TEXT")


def migration_19_notification_acknowledgement(conn: sqlite3.Connection) -> None:
    """What an operator decided about an alert, and why - not merely that they saw it.

    ``read_at``/``read_by`` answered "has anybody looked at this", which is the wrong question
    for the alerts that are *decisions*: a forced start past a failing self-test, a push channel
    that has stopped delivering. Those stay on the record until a human accepts them and says
    why, and the acceptance has to be attributable - "somebody read it" cannot be reviewed, and
    it cannot be shown to have been the right call.

    The note is the point rather than a nicety. A forced start carries a reason from the
    environment (``STARTUP_OVERRIDE_REASON``); the acknowledgement carries the operator's own,
    written after the fact, and the two answers are different people answering different
    questions. The note is vetted as prose (``textguard``) at the endpoint, like every other
    free text this application stores, so a reader that forgets to escape is not a way in.

    Nullable in both directions, which is what makes the state readable: an alert written
    before this migration has no acknowledgement, and neither has one nobody has answered.
    """
    add_column(conn, "admin_notifications", "acknowledged_at", "DATETIME")
    add_column(conn, "admin_notifications", "acknowledged_by", "TEXT")
    add_column(conn, "admin_notifications", "acknowledgement_note", "TEXT")


def migration_20_developer_operations(conn: sqlite3.Connection) -> None:
    """The root tier's own storage: runtime configuration, and the private alert hub.

    Two tables, and the split between them is a security decision rather than a normalising
    one.

    ``developer_config`` is *shared state*: a key, its value, who changed it and when, and a
    single-row version counter beside it. Every worker reads through it (``developer.runtime``),
    so a flag flipped from the developer surface takes effect on all of them without a restart.
    That is the distributed-configuration requirement, met with the storage this deployment
    actually has: the version counter is what makes an in-process cache safe to keep, because a
    cached read is only trusted while the version it was taken at is still the version on
    disk. Adding Redis to a single-file SQLite application would buy a second source of truth
    and a new failure mode at 04:00, not a faster read.

    ``developer_alerts`` is the private hub, and it is a table of its own rather than a
    visibility column on ``admin_notifications`` for exactly one reason: **concealment by
    construction**. A column is a filter somebody can forget in a query written next year; a
    table that no administrator-facing route, service or report names cannot be read by
    accident. The administrator's own alerts stay where they were, with the acknowledgement
    flow migration 19 gave them, and neither table can leak into the other's answers.

    ``dedupe_key`` is uniquely indexed, and the key is expected to carry its own window
    (``pool_saturation:2026-09-22T15``): one row per window by construction of the index, which
    is what stops a saturated pool writing an alert every tick.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS developer_config (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            value_type TEXT NOT NULL,
            updated_at DATETIME NOT NULL,
            updated_by TEXT,
            note TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS developer_config_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL DEFAULT 0,
            changed_at DATETIME
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO developer_config_version (id, version, changed_at) VALUES (1, 0, NULL)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS developer_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            source TEXT NOT NULL,
            summary TEXT NOT NULL,
            detail_json TEXT,
            trace_id TEXT,
            dedupe_key TEXT,
            created_at DATETIME NOT NULL,
            read_at DATETIME,
            read_by TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_developer_alerts_created ON developer_alerts(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_developer_alerts_unread ON developer_alerts(read_at, created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_developer_alerts_dedupe "
        "ON developer_alerts(dedupe_key) WHERE dedupe_key IS NOT NULL"
    )


def migration_21_overtime_authorisations(conn: sqlite3.Connection) -> None:
    """The mid-shift overtime decision: a question answered while the shift is still open.

    WHY THIS IS A TABLE AND NOT A NOTIFICATION
    ------------------------------------------
    The crossing used to be an ``admin_notifications`` row (``overtime_exceeded``), which put a
    live *question* in the tab built for *notices*. The two differ in exactly the way that
    matters: a notice is read and stops existing, and a question is answered and stops being
    asked. Reading the crossing row did nothing to the shift, and the shift - still open, still
    accumulating - stayed open whether or not anybody looked.

    So the crossing is derived (see ``overtime.open_crossings``, which reads the open shifts)
    and the *answer* is stored here. Derived, because an open shift's crossing state is already
    computable from the shift and the rules, so a stored copy would be a second answer that can
    disagree with the first; and because a queue item that disappears the moment the shift ends
    needs no dismissal, no expiry and no sweeper.

    WHY THE CEILING AND NOT A FLAG
    ------------------------------
    ``authorised_hours`` is a **number**: the paid hours this shift may run to. An approval that
    said only "yes" would authorise an amount nobody could state, and the clock-out would either
    ignore it or guess. The decision is recorded as an amount so that the settled row is
    arithmetic (``max(0, paid - ceiling)``) rather than a special case, and so that "how much did
    they authorise, and how much had been worked when they said it" is answerable from the row.

    ``recorded_hours_at_decision`` is that second half, and it is not redundant with the
    decision: without it nobody can tell a generous 12 h ceiling given at 8.5 h from a
    rubber-stamp given at 11.9 h, and the review that reads this row is a review of a judgement.

    A refusal is a ceiling too - the regular paid day - so the clock-out reads one column for
    both answers instead of branching on the decision; ``decision`` carries the *why*.

    Append-only per shift, with a partial unique index for the live one: a later decision
    supersedes rather than overwrites, because a shift that runs past the ceiling needs a second
    answer and the first one is what the second is measured against. Same rule the forced-start
    acknowledgement already follows.

    ``consumed_by_log_id`` is set by the clock-out that settles under this decision, which is
    what makes "was this ever cashed in?" answerable, and what stops a decision taken for one
    shift being read as authorisation for the next one the same worker starts.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS overtime_authorisations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            clock_in_time DATETIME NOT NULL,
            decision TEXT NOT NULL,
            authorised_hours REAL NOT NULL,
            recorded_hours_at_decision REAL NOT NULL,
            decided_by TEXT NOT NULL,
            decided_at DATETIME NOT NULL,
            note TEXT,
            consumed_by_log_id INTEGER,
            superseded_by INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_overtime_authorisations_shift "
        "ON overtime_authorisations(worker_id, clock_in_time, id DESC)"
    )
    # One live decision per shift. Two rows for the same open shift would be two answers to one
    # question, and the clock-out would have to pick - which is how the arithmetic and the queue
    # come to disagree about the same shift.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_overtime_authorisations_live "
        "ON overtime_authorisations(worker_id, clock_in_time) "
        "WHERE consumed_by_log_id IS NULL AND superseded_by IS NULL"
    )


def migration_22_refused_punches(conn: sqlite3.Connection) -> None:
    """The punch the band refused: score, reason and frame, kept for triage instead of dropped.

    WHY THIS EXISTS
    ---------------
    A refused punch used to leave *nothing*: the frame was written and then discarded, the score
    went into the 422 body and vanished, and the only trace was a log line. That is exactly the
    wrong shape for the refusal the deployment actually needs to see - the one where a band
    derived from the wrong corpus refuses honest workers all day. A queue of refusals with the
    frame beside the score is the difference between "it keeps saying mismatch" (unsolvable) and
    "11 refusals today, all at 0.51-0.55, frames look like the worker" (the band is wrong).

    Deliberately NOT an attendance_logs row
    ---------------------------------------
    A refusal is not attendance: nothing was worked, nothing is payable, and putting it in the
    logs would put it in every report, export and payroll join in the application. It is its own
    store, with its own retention, readable only by administrators.

    Fields mirror what the 422 already said (worker, site, score, error reason) plus the frame,
    which the refusal path had in memory and threw away. ``punch_frame`` uses the same store and
    conventions as the log rows' frames (random name, same directory, same sweep), so one
    retention implementation covers both.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refused_punches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT,
            biometric_id TEXT,
            site_name TEXT,
            action TEXT NOT NULL,
            error_code TEXT NOT NULL,
            score REAL,
            pipeline TEXT,
            source TEXT NOT NULL DEFAULT 'online',
            punch_frame TEXT,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_refused_punches_created "
        "ON refused_punches(created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_refused_punches_worker "
        "ON refused_punches(worker_id, created_at DESC)"
    )


def migration_23_corpus_capture_consents(conn: sqlite3.Connection) -> None:
    """A worker's own yes-or-no on calibration capture, as an append-only record.

    WHY THIS EXISTS
    ---------------
    The calibration corpus is a biometric store, and until this table the consent basis for
    live captures was a deployment-wide string: ``CALIBRATION_CAPTURE_ENABLED`` with a notice.
    An operator flipping one switch decided for every worker at once, and the record kept
    saying "the deployment consented" long after anybody had stopped asking whether the
    person *in the frame* had. A face kept for calibration is that person's face; the
    deployment switch is necessary (it states the operator intends to collect at all) but it
    is not sufficient, and this table is the sufficient half.

    WHY APPEND-ONLY
    ---------------
    Consent is a decision with a history, not a flag: "granted in March, withdrawn in June"
    answers a different question than "never granted" - the first names a corpus that may
    already hold captures, the second one that should hold none. The current state is the
    newest row per worker (``is_current``), and older rows are kept so the answer to "when did
    this start, and when did they take it back" is a SELECT rather than a memory. The
    database-level trigger pattern this codebase already uses (audit_log) rejects UPDATE and
    DELETE, so history cannot be quietly rewritten by a careless query.

    ``revoked_at`` is NULL for a grant and stamped for a withdrawal - the same shape
    ``worker_push_subscriptions`` uses for a live device versus a retired one, so a reader of
    this codebase already knows how to read it.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corpus_capture_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT NOT NULL,
            granted INTEGER NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            created_at DATETIME NOT NULL,
            created_by TEXT NOT NULL,
            created_ip TEXT,
            superseded_at DATETIME,
            revoked_at DATETIME,
            revoked_ip TEXT
        )
        """
    )
    # The newest row answers the question; the index also serves "the whole history for this
    # worker" in creation order, which is what an audit of a consent reads.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_corpus_consents_worker "
        "ON corpus_capture_consents(worker_id, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_corpus_consents_current "
        "ON corpus_capture_consents(is_current) WHERE is_current = 1"
    )
    # Append-only in the database, like audit_log: a consent history that UPDATE or DELETE can
    # rewrite is not a history, it is whatever the last query left.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS corpus_capture_consents_no_update
        BEFORE UPDATE ON corpus_capture_consents
        BEGIN
            SELECT RAISE(ABORT, 'corpus_capture_consents is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS corpus_capture_consents_no_delete
        BEFORE DELETE ON corpus_capture_consents
        BEGIN
            SELECT RAISE(ABORT, 'corpus_capture_consents is append-only');
        END
        """
    )


def migration_24_walk_up_registration(conn: sqlite3.Connection) -> None:
    """The request, not the account: a photograph and some details waiting for a decision.

    WHY THIS EXISTS
    ---------------
    The registration *link* that already ships is issued per person: an administrator types the
    name, the role and the account id, and the link reserves that id when it is created
    (``enrollment.create_invite``). That is the right shape for one named hire and the wrong one
    for a walk-up, where nobody has applied yet and so no id can be reserved. The account has to
    be created later - and once an account can be created later it can also be refused, which is
    the state this table records.

    WHY THE ID ON THIS ROW IS NOT DECIDED UNTIL THE DECISION
    -------------------------------------------------------
    ``assigned_id`` stays NULL while the request is pending, deliberately. A proposed id would be
    a promise this application cannot keep - one id per walk-up, and the applicant would be told a
    number another request may take in the meantime. ``security.lowest_free_id`` decides at
    approval, inside the same write transaction that inserts the account.

    WHY ``photo_sha256`` IS UNIQUE WHILE PENDING
    -------------------------------------------
    A partial unique index: the *same* upload cannot become two requests. A script with one JPEG
    gets one row instead of filling the review queue with copies of it, and an honest applicant
    who taps submit three times gets one - the answer they need is "we have it", not three
    identical faces for an administrator to read. It stops applying once a decision is made, so a
    rejected applicant may apply again with the same photograph.

    WHY THE PHOTO IS A PATH AND NOT A BLOB
    --------------------------------------
    The upload is streamed to disk under the policy in ``uploads`` and this row points at it. A
    deployment that held every pending request's photo in memory would have a memory cost per
    *applicant*, and nothing between submission and review needs the bytes.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS registration_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
                CHECK (status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            requested_role TEXT NOT NULL,
            work_details TEXT NOT NULL DEFAULT '',
            -- The credential the applicant chose on the public form, hashed on the way in and
            -- never held in the clear. Empty on a rejected request: the row stays as the record
            -- of a refusal, and a refusal is not a reason to keep somebody's credential.
            password_hash TEXT NOT NULL DEFAULT '',
            photo_path TEXT NOT NULL,
            photo_sha256 TEXT NOT NULL,
            photo_bytes INTEGER NOT NULL DEFAULT 0,
            photo_mime TEXT NOT NULL DEFAULT 'image/jpeg',
            assigned_id TEXT,
            submitted_ip TEXT,
            consent_at DATETIME,
            consent_version TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            reviewed_by TEXT,
            reviewed_at DATETIME,
            decision_note TEXT
        )
        """
    )
    # The review queue is "the pending ones, oldest first", which is the only read this table
    # gets on a busy morning - so the index carries the status it is always filtered by.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_registration_requests_status "
        "ON registration_requests(status, id)"
    )
    # One pending request per photograph. The status is in the predicate so the rule stops at the
    # decision: a rejected applicant is allowed to apply again, with the same selfie or another.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_registration_requests_pending_photo "
        "ON registration_requests(photo_sha256) WHERE status = 'PENDING_REVIEW'"
    )


#: The site categories a fresh installation opens with.
#:
#: Ops already groups its sites this way - a warehouse, a factory, a project - and a console
#: that started with an empty list would ask the first administrator to retype the three words
#: the company already uses. They are seeded *without* hours on purpose: a category with NULL
#: hours inherits exactly what the site inherits today (the company window), so seeding changes
#: no punch anywhere. ``INSERT OR IGNORE`` against the UNIQUE name keeps the step idempotent,
#: which is what the migrator's contract requires and what lets an operator delete one without
#: the next run putting it back twice.
SEED_SITE_CATEGORIES: tuple[str, ...] = ("مخزن", "مصنع", "مشاريع")


def migration_25_site_categories(conn: sqlite3.Connection) -> None:
    """A category of sites that carries the clock-in window for every site inside it.

    WHY A LAYER AND NOT A COPY
    --------------------------
    The obvious way to make "edit one warehouse, all its sites change" work is to write the
    new hours into every member site when the category is saved. That is rejected, and the
    reason is the console line it would break: ``GET /admin/sites`` publishes a resolved
    window *and where each field came from* (``shift_windows.SOURCE_*``), because an
    administrator looking at 04:00 has to know whether editing the site will change it. Copy
    -on-save destroys that answer - every site would read "configured here" - and it also
    lets the next category edit stamp over hours somebody deliberately set on one site.

    So the category is *a layer*, tried between the site's own column and the company rules:
    site -> category -> company. Nothing is backfilled and no existing site is touched: a
    site with ``category_id`` NULL resolves exactly as it did before this migration.

    WHY ``category_id`` AND NOT THE NAME
    ------------------------------------
    A foreign key by ``name`` would make renaming a category a rewrite of every member site,
    and the name is the one field an operator will want to correct (a typo in a warehouse's
    name is not a new warehouse). The id is the key; the name is a label.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS site_categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            clock_in_window_start TEXT {_hhmm_check('clock_in_window_start')},
            clock_in_window_end TEXT {_hhmm_check('clock_in_window_end')},
            site_timezone TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    add_column(conn, "construction_sites", "category_id", "INTEGER")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for name in SEED_SITE_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO site_categories (name, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (name, stamp, stamp),
        )


def migration_26_off_office_workers(conn: sqlite3.Connection) -> None:
    """Split the old 500-999 ``moallem`` band into moallem (500-749) and off-office (750-999).

    WHY A MIGRATION AND NOT JUST A NEW RANGE
    ----------------------------------------
    A range in ``security.ROLE_ID_RANGES`` is a rule about ids an *API* may hand out; it says
    nothing about rows already in ``users``. Once the range shrank, an account numbered 800
    and still carrying ``role = 'moallem'`` would be a moallem outside the moallem band - the
    one thing the ranges exist to make impossible, and invisible until somebody tried to edit
    that account's id and was refused for a range that no longer contains the id they already
    have. Rewriting the row is what makes the stored data and the range agree again.

    The threshold is the band boundary, not a curated list: every id that used to belong to
    moallem and now belongs to off-office moves, so the split is exactly the one the new ranges
    describe. ``CAST`` matches how every other consumer reads ``users.id`` (TEXT holding a
    decimal integer).

    On a database with no such accounts this is a no-op, which is the common case here - the
    old band was rarely filled past its midpoint - so it is safe to run unconditionally.
    """
    conn.execute(
        "UPDATE users SET role = 'off_office' "
        "WHERE role = 'moallem' AND CAST(id AS INTEGER) BETWEEN 750 AND 999"
    )


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
    (14, "worker_notifications_and_push", migration_14_worker_notifications_and_push),
    (15, "worker_report_columns", migration_15_worker_report_columns),
    (16, "company_branding", migration_16_company_branding),
    (17, "punch_queue_photo_scored_at", migration_17_punch_queue_photo_scored_at),
    (18, "punch_frame", migration_18_punch_frame),
    (19, "notification_acknowledgement", migration_19_notification_acknowledgement),
    (20, "developer_operations", migration_20_developer_operations),
    (21, "overtime_authorisations", migration_21_overtime_authorisations),
    (22, "refused_punches", migration_22_refused_punches),
    (23, "corpus_capture_consents", migration_23_corpus_capture_consents),
    (24, "walk_up_registration", migration_24_walk_up_registration),
    (25, "site_categories", migration_25_site_categories),
    (26, "off_office_workers", migration_26_off_office_workers),
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
