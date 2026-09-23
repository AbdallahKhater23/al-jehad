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

        # Additive columns the API writes on every punch. ``punch_frame`` is here on purpose:
        # it once shipped inside migration 2, which older databases had already recorded as
        # applied - so the column was never created anywhere, and every clock-out died with
        # ``no such column: punch_frame``. A column the punch writes belongs in the pin, so
        # the next one cannot repeat that in either direction.
        for table, columns in (
            ("attendance_logs", {"liveness_class", "liveness_score", "source", "status_code", "overtime_hours", "punch_frame"}),
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


def test_a_column_the_code_writes_is_never_declared_inside_an_older_migration():
    """The specific defect that broke every clock-out, stated as a rule.

    ``punch_frame`` was added inside ``migration_2_provenance_columns`` - a migration every
    existing database had already recorded as applied. On those databases the migration never
    ran again, the column was never created, and the first punch failed with ``no such
    column: punch_frame``; the schema guard did not catch it because its expectation is
    compiled from baseline + migrations, which agreed with each other and were both wrong
    against reality. This test reads the migrations instead of trusting them.
    """
    import re
    from pathlib import Path

    source = Path(migrations.__file__).read_text(encoding="utf-8")
    # Cut at the registry: it names every column too - ``(18, "punch_frame", ...)`` - and those
    # names belong to no migration. Without this the *last* function's body runs to the end of
    # the file, swallows the registry, and every column of every migration looks like it was
    # declared twice as soon as a new migration is appended below.
    source = source.split("MIGRATIONS: list[")[0]
    # Split the file into per-migration bodies, so a column name can be attributed to every
    # migration that mentions it - whether through a literal ``add_column`` call or through a
    # tuple feeding the loop form, which is how migration 2 declares its columns.
    bodies = re.split(r"def (migration_\d+\w*)\(", source)
    # Two ways a migration can name a column, and a table-creating migration only has the
    # second: a quoted identifier (a literal ``add_column`` call, or a tuple feeding the loop
    # form, which is how migration 2 declares its columns), or a definition inside a
    # ``CREATE TABLE`` body - ``key TEXT PRIMARY KEY``.
    #
    # Both are read, because without the second this guard reports "the latest migration names
    # no column" about a migration whose entire job is two new tables. A false alarm is not
    # harmless here: the fix it invites is to weaken the guard, and the guard is what caught
    # ``punch_frame`` being declared inside an already-applied migration.
    _quoted = re.compile(r"[\"']([a-z][\w]+)[\"']")
    _declared = re.compile(
        r"^\s{8,}(\w+)\s+(?:TEXT|INTEGER|DATETIME|REAL|BLOB|FLOAT|NUMERIC|BOOLEAN)\b", re.M
    )
    mentions: dict[str, set[str]] = {}
    for name, body in zip(bodies[1::2], bodies[2::2]):
        mentions[name] = set(_quoted.findall(body)) | set(_declared.findall(body))

    latest_version = max(version for version, _, _ in migrations.MIGRATIONS)
    latest_functions = [function.__name__ for version, _, function in migrations.MIGRATIONS if version == latest_version]
    for function_name in latest_functions:
        assert mentions.get(function_name), (
            f"the latest migration ({function_name}) names no column; if it needs none, it is the wrong place for one"
        )
    # The rule: a column may be *named* only by the migration that introduces it. Mentioned
    # anywhere older means it is inside a migration existing databases have already recorded
    # as applied - which is exactly where ``punch_frame`` shipped when every clock-out broke.
    # Two homes are legal when they introduce the *same column name on different tables*:
    # migration 18 adds ``attendance_logs.punch_frame`` and migration 22 creates
    # ``refused_punches.punch_frame`` - same name, different columns, each introduced by the
    # migration that owns its table. What the rule forbids is a *second mention of the same
    # table's column* in an older migration, so the guard's failure mode (a name appearing in
    # a migration that could never have known it) stays the thing that bites.
    punch_frame_homes = sorted(name for name, names in mentions.items() if "punch_frame" in names)
    assert punch_frame_homes == ["migration_18_punch_frame", "migration_22_refused_punches"], (
        f"punch_frame must be introduced by migrations 18 and 22 (one table each) and named "
        f"nowhere else: {punch_frame_homes}"
    )


def test_the_readiness_warning_is_registered_among_the_checks():
    """The advisory check has to be in the registry, or it is dead code."""
    import readiness

    names = [function.__name__ for function in readiness.CHECKS]
    assert "_check_schema_version_ahead" in names


def test_code_ahead_of_the_database_is_warned_not_refused(prototype_db):
    """The advisory twin of ``schema_current``: the same fact, warned rather than blocked.

    The database is left one migration short - the newest is recorded as not applied, which is
    what an interrupted upgrade leaves behind and exactly the state ``SCHEMA_VERSION`` is meant
    to describe. The fatal ``schema_current`` refuses to boot for this; this check has to report
    it as a warning carrying the missing version, so it is visible on the readiness surfaces and
    when a deployment was forced up with ``STARTUP_OVERRIDE_REASON``.
    """
    import readiness

    conn = sqlite3.connect(str(prototype_db), isolation_level=None)
    try:
        migrations.initialize(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migrations.SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()

    check = readiness._check_schema_version_ahead({"db_path": prototype_db})
    assert check.tier == readiness.TIER_ADVISORY, check.tier
    assert check.ok is False, check.detail
    assert check.value["code"] == migrations.SCHEMA_VERSION
    assert check.value["database"] < migrations.SCHEMA_VERSION
    assert check.value["missing"] == [migrations.SCHEMA_VERSION], check.value
    assert str(migrations.SCHEMA_VERSION) in check.detail, check.detail


def test_a_database_level_with_the_code_is_not_warned(prototype_db):
    """No false positive: a fully migrated database is quiet, and still counted."""
    import readiness

    conn = sqlite3.connect(str(prototype_db), isolation_level=None)
    try:
        migrations.initialize(conn)
    finally:
        conn.close()

    check = readiness._check_schema_version_ahead({"db_path": prototype_db})
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is True, check.detail
    assert check.value["database"] == migrations.SCHEMA_VERSION
    assert check.value["missing"] == [], check.value


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
