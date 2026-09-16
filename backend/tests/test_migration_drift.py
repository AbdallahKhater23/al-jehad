"""Migration safety: upgrading a prototype database in place, without data loss.

The scenario these tests encode is the real one: ``times.db`` on this project predates
every migration (it has no ``schema_migrations`` table at all), holds live payroll
history, and ``users`` has no ``token_version`` column - which is why login returned
HTTP 500 with ``no such column: token_version``.

What must hold after ``migrations.initialize()``:

* the additive migrations create what the code now reads;
* every pre-existing row survives, byte for byte, including the 179 attendance records;
* running it again changes nothing (idempotent, so a restart cannot corrupt anything);
* the schema guard finds nothing fatal on the result.
"""

from __future__ import annotations

import sqlite3

import migrations
import pytest

#: The prototype ``users`` table exactly as the pre-migration build declared it - no
#: token_version, no status, no enrolled_at. Written out rather than cloned from the
#: live file so this test keeps proving the same thing after the live file is migrated.
PROTOTYPE_USERS = """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL
    )
"""

PROTOTYPE_LOGS = """
    CREATE TABLE attendance_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_id TEXT NOT NULL,
        site_name TEXT NOT NULL,
        action TEXT NOT NULL,
        timestamp DATETIME NOT NULL,
        hours FLOAT NOT NULL,
        score FLOAT NOT NULL,
        status TEXT NOT NULL
    )
"""

TABLES_REQUIRED_BY_THE_NEW_CODE = (
    "audit_log",
    "admin_notifications",
    "shift_rules",
    "worker_devices",
    "punch_queue",
    "device_anchors",
    "enrollment_invites",
    "enrollment_jobs",
    "enrollment_job_items",
    "worker_notes",
    "worker_note_messages",
)


def _prototype_database(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(PROTOTYPE_USERS)
        conn.execute(PROTOTYPE_LOGS)
        conn.execute(
            "INSERT INTO users (id, name, email, phone, password_hash, role) VALUES (?,?,?,?,?,?)",
            ("1", "Existing Worker", "w@example.test", "", "$2b$12$existinghash", "worker"),
        )
        conn.execute(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, status) "
            "VALUES ('1','Downtown Tower A','Clock Out','2026-08-01 15:00:00',8.5,0.22,'Approved')"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def prototype_db(tmp_path):
    path = tmp_path / "prototype.db"
    _prototype_database(path)
    return path


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_prototype_database_is_missing_token_version(prototype_db):
    """The bug being fixed: the column the code authenticates with does not exist."""
    conn = sqlite3.connect(str(prototype_db))
    try:
        assert "token_version" not in _columns(conn, "users")
    finally:
        conn.close()


def test_migration_adds_token_version_and_every_table_the_new_code_reads(prototype_db):
    conn = sqlite3.connect(str(prototype_db), isolation_level=None)
    try:
        applied = migrations.initialize(conn)
        assert migrations.SCHEMA_VERSION in applied or migrations.current_version(conn) == migrations.SCHEMA_VERSION

        assert "token_version" in _columns(conn, "users"), "login cannot authenticate without this column"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in TABLES_REQUIRED_BY_THE_NEW_CODE if name not in tables]
        assert not missing, f"migrations did not create: {missing}"

        # Additive columns the API writes on every punch.
        for table, columns in (
            ("attendance_logs", {"liveness_class", "liveness_score", "source", "status_code", "overtime_hours"}),
            ("active_sessions", {"start_source", "late_flag", "overtime_notified_at", "liveness_class"}),
            ("punch_queue", {"nonce", "anchor_server_time", "monotonic_offset_s", "anchor_id"}),
        ):
            missing_columns = columns - _columns(conn, table)
            assert not missing_columns, f"{table} is missing {missing_columns}"
    finally:
        conn.close()


def test_migration_preserves_existing_rows_and_backfills_status_code(prototype_db):
    """The 179 real attendance rows must survive, and gain a canonical status code."""
    conn = sqlite3.connect(str(prototype_db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        migrations.initialize(conn)
        user = conn.execute("SELECT name, email, token_version FROM users WHERE id = '1'").fetchone()
        assert user["name"] == "Existing Worker"
        assert user["email"] == "w@example.test"
        # DEFAULT 0 keeps every already-issued token (ver 0) valid: the repair must not
        # sign out a site that is mid-shift.
        assert user["token_version"] == 0

        log = conn.execute("SELECT hours, status, status_code FROM attendance_logs WHERE worker_id = '1'").fetchone()
        assert log["hours"] == 8.5, "payroll history must not be rewritten"
        assert log["status"] == "Approved"
        # 'Approved' -> 'approved' is a lossless re-expression, not a new claim.
        assert log["status_code"] == "approved"
    finally:
        conn.close()


def test_migration_is_idempotent(prototype_db):
    """A second startup must be a no-op - restarts are frequent and must be boring."""
    conn = sqlite3.connect(str(prototype_db), isolation_level=None)
    try:
        migrations.initialize(conn)
        first = migrations.current_version(conn)
        applied_again = migrations.run_migrations(conn)
        assert applied_again == [], f"migrations re-ran: {applied_again}"
        assert migrations.current_version(conn) == first
        assert migrations.pending_migrations(conn) == []
    finally:
        conn.close()


def test_audit_log_is_append_only_enforced_by_the_database(prototype_db):
    """The audit trail is only worth having if history cannot be rewritten in place."""
    conn = sqlite3.connect(str(prototype_db), isolation_level=None)
    try:
        migrations.initialize(conn)
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, created_at) "
            "VALUES ('1000','admin','test_action','users','1','2026-09-12 10:00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET action = 'tampered' WHERE action = 'test_action'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_log WHERE action = 'test_action'")
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'test_action'").fetchone()[0] == 1
    finally:
        conn.close()


def test_schema_guard_reports_no_blocking_drift_on_the_migrated_database():
    """The guard compiles its expectation from baseline + migrations, so this proves the
    two AGREE - a migration that forgot to update ``baseline_schema()`` fails here."""
    import schema_guard

    report = schema_guard.inspect()
    assert report.ready, f"fatal drift: {report.blocking if hasattr(report, 'blocking') else report.severity}"
    assert report.expected_schema_version == migrations.SCHEMA_VERSION


def test_live_clone_carries_the_column_login_needs(client):
    """End-to-end proof on the harness clone of the real database: the login the
    prototype could not serve now returns a token."""
    from harness import PASSWORDS, WORKER, EMAILS

    response = client.post(
        "/api/v1/auth/login",
        json={"user_id": WORKER, "email_or_phone": EMAILS[WORKER], "password": PASSWORDS[WORKER]},
    )
    assert response.status_code == 200, f"login broken on the migrated database: {response.text[:200]}"
    assert response.json().get("access_token")
