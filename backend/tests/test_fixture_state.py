"""The fixture itself: a clone of a database that is in use, and the rows it must not keep.

WHY THIS EXISTS
---------------
Every suite here runs against a throwaway copy of the live ``times.db``. That copy is a
photograph of a database **somebody is using**, so it carries this morning's punches, this
week's quick links and today's late-arrival notifications, and the tests are written as if they
were the only thing happening. When they are not, an assertion like "exactly one late arrival"
or "this account's password has never been reset" fails on somebody else's traffic - and the
failure looks exactly like a regression in the feature under test, which is the expensive part:
it sends you reading code that is fine.

Three tests in this repository failed that way, deterministically, for as long as the
application was actually in use (a punch notification, a password reset, a quick link). The fix
is in ``harness.clear_activity``: the clone supplies the **schema and the configuration**, and
the tests supply their own data.

This suite pins the rule rather than the three cases:

1. **Every table is classified.** A new table has to be declared as activity (cleared), shipped
   configuration (kept) or seeded - otherwise this fails, so the list cannot fall behind the
   schema the way a hand-written list always does.
2. **Planted activity does not survive a reset.** Rows are written with each table's own
   required columns, read from the schema, so the test keeps working when a column is added.
3. **The configuration the tests read is the shipped one.** The company rules are configuration
   a real administrator can change from the console, so they are normalised too.

It also keeps the seeded fixture *present* (one active session, one ``pending_review`` record):
clearing must not eat what the other suites address by id.
"""

from __future__ import annotations

import sqlite3

import pytest

import harness
from harness import (
    ACTIVITY_TABLES,
    CONFIGURATION_TABLES,
    SEEDED_TABLES,
    WORKER,
    bearer,
    db_scalar,
)

def _activity_counts() -> dict[str, int]:
    """Rows per activity table, right now."""
    connection = sqlite3.connect(str(harness.DB_PATH))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ACTIVITY_TABLES
        }
    finally:
        connection.close()


#: Columns this build does not know about are left alone by the rules normalisation, so the
#: comparison is over the keys the application ships.
def _table_columns(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]


def _sample_value(declared_type: str) -> object:
    """A value that satisfies one NOT NULL column, by its declared type."""
    kind = (declared_type or "").upper()
    if "INT" in kind:
        return 1
    if any(word in kind for word in ("REAL", "FLOA", "DOUB", "NUM")):
        return 1.0
    if "DATE" in kind or "TIME" in kind:
        return "2026-09-17 09:00:00"
    return "live-usage"


def _plant_one_row(connection: sqlite3.Connection, table: str) -> int:
    """Insert the smallest row this table's schema accepts, and say how many columns that took."""
    columns, values = [], []
    for _cid, name, declared_type, not_null, default, primary_key in _table_columns(connection, table):
        if primary_key or not not_null or default is not None:
            continue
        columns.append(name)
        values.append(_sample_value(declared_type))
    if not columns:
        # Every column is nullable or defaulted - ``active_sessions`` in this schema - and
        # ``INSERT INTO t DEFAULT VALUES`` is SQLite's spelling for "a row of the defaults".
        connection.execute(f"INSERT INTO {table} DEFAULT VALUES")
        return 0
    placeholders = ", ".join("?" * len(columns))
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values
    )
    return len(columns)


def test_every_table_is_classified_as_activity_configuration_or_seeded():
    """A table in none of the three lists is a table whose live rows reach an assertion."""
    connection = sqlite3.connect(harness.DB_PATH)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()

    classified = set(ACTIVITY_TABLES) | set(CONFIGURATION_TABLES) | set(SEEDED_TABLES)
    unclassified = sorted(tables - classified)
    assert not unclassified, (
        f"{unclassified} exist in the schema and are in none of harness.ACTIVITY_TABLES, "
        "CONFIGURATION_TABLES or SEEDED_TABLES. Decide which it is: if the application writes "
        "it while people use it, it belongs in ACTIVITY_TABLES and is cleared before every test."
    )
    stale = sorted(classified - tables)
    assert not stale, f"{stale} are classified but no longer exist; the lists have gone stale"


def test_the_three_lists_do_not_overlap():
    overlap = (set(ACTIVITY_TABLES) & set(CONFIGURATION_TABLES)) | (
        set(ACTIVITY_TABLES) & set(SEEDED_TABLES)
    ) | (set(CONFIGURATION_TABLES) & set(SEEDED_TABLES))
    assert not overlap, (
        f"{sorted(overlap)} are in more than one list; a table is either cleared, kept or "
        "rewritten, and 'cleared and kept' is not a decision"
    )


def test_planted_live_activity_does_not_survive_a_reset(app_module):
    """The whole point, table by table: write like real usage, reset, find nothing.

    The rows are built from each table's own required columns rather than a hand-written insert
    per table, so a column added by a later migration cannot quietly make this test stop
    covering a table.

    "Find nothing" is measured against a *baseline* rather than a hardcoded zero, because the
    fixture writes rows of its own (one session, the pending review, the two completed shifts
    the report suite reads). Comparing two resets of the same clone is what makes the assertion
    "nothing from the live database survived" instead of "nothing at all is here" - the second
    would fail the moment the fixture grew a seeded row, which is how a test like this gets
    weakened rather than fixed.
    """
    harness.reset_database(app_module)
    baseline = _activity_counts()

    connection = sqlite3.connect(str(harness.DB_PATH))
    try:
        for table in ACTIVITY_TABLES:
            _plant_one_row(connection, table)
        connection.commit()
    finally:
        connection.close()

    planted = _activity_counts()
    assert all(planted[table] > baseline[table] for table in ACTIVITY_TABLES), (
        planted,
        baseline,
    )

    harness.reset_database(app_module)

    assert _activity_counts() == baseline, (
        "an activity table kept rows across a reset; those rows come from the live database "
        "and every exact-count assertion in this suite is written as if they did not exist"
    )


def test_the_seeded_fixture_is_still_there_after_the_clear(app_module):
    """Clearing must not eat what the other suites address by id."""
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,)) == 1
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM attendance_logs WHERE status = 'pending_review' AND worker_id = ?",
            (WORKER,),
        )
        == 1
    )


def test_a_password_reset_in_the_live_log_cannot_reach_the_roster(client, app_module):
    """The roster's ``password_changed_at`` is ``MAX(created_at)`` over the audit log.

    A real reset - by an administrator, in the console - therefore used to appear on a seeded
    test account, and ``test_admin_credentials_roster`` failed asserting that a freshly seeded
    account has never had its password changed. Which, at the time, was true.
    """
    connection = sqlite3.connect(str(harness.DB_PATH))
    try:
        connection.execute(
            "INSERT INTO audit_log (action, actor_id, actor_role, entity, entity_id, created_at) "
            "VALUES ('password_reset', ?, 'admin', 'users', ?, ?)",
            (harness.ADMIN, WORKER, "2026-09-17 09:00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    harness.reset_database(app_module)

    response = client.get("/api/v1/admin/users", headers=bearer(harness.ADMIN))
    assert response.status_code == 200, response.text[:200]
    roster = {row["id"]: row for row in response.json()}
    assert roster[WORKER]["password_changed_at"] is None, (
        "a reset recorded in the live audit log is being reported as this seeded account's own"
    )


def test_a_late_arrival_notification_from_live_usage_is_not_inherited(app_module):
    """The punch tests count late arrivals; a real one from this morning used to be counted."""
    connection = sqlite3.connect(str(harness.DB_PATH))
    try:
        connection.execute(
            "INSERT INTO admin_notifications (kind, severity, worker_id, title, body, created_at) "
            "VALUES ('late_arrival', 'info', ?, 'Late arrival', 'outside the window', ?)",
            (harness.MOALLEM, "2026-09-17 05:10:00"),
        )
        connection.commit()
    finally:
        connection.close()

    harness.reset_database(app_module)

    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = 'late_arrival'") == 0


def test_the_company_rules_are_the_shipped_ones_after_a_reset(app_module):
    """Shift rules are configuration a real administrator can change from the console now."""
    connection = sqlite3.connect(str(harness.DB_PATH))
    try:
        connection.execute(
            "UPDATE shift_rules SET clock_in_window_start = '21:30', clock_in_window_end = '05:30', "
            "regular_hours = 6.5, break_minutes = 0 WHERE id = 1"
        )
        connection.commit()
    finally:
        connection.close()

    harness.reset_database(app_module)

    defaults = app_module.DEFAULT_SHIFT_RULES
    stored = app_module.get_shift_rules()
    for key, expected in defaults.items():
        assert stored[key] == expected or str(stored[key]) == str(expected), (
            f"{key} is {stored[key]!r} after a reset instead of the shipped {expected!r}: a test "
            "would be asserting whatever shift the live deployment is on this week"
        )


@pytest.mark.parametrize("table", ["users", "construction_sites", "shift_rules"])
def test_seeded_configuration_is_rewritten_rather_than_inherited(app_module, table):
    """The third list, checked the same way: what the tests read is what the harness wrote."""
    assert db_scalar(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = '{table}'"
    ) == 1
    connection = sqlite3.connect(str(harness.DB_PATH))
    try:
        rows = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()
    assert rows >= 1, f"{table} is empty after a reset, so the fixture did not seed it"
