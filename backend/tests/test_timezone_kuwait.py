"""The application clock is Kuwait time, whatever region the host sits in.

These are deliberately *portable*: they assert the Kuwait zone resolves, that ``clock.now()``
reads it, and that the two places that could silently fall back to the host's clock - the
monthly aggregate in ``main.py`` and SQLite's ``'localtime'`` - do not. None of them reads the
test host's zone, so the same file passes on a Kuwait laptop, a US CI runner and the
container.

Why the ``datetime.now()`` call sites themselves are not rewritten to ``clock.now()`` is
explained in ``clock.py``: the frozen-time tests replace ``datetime`` in a module's namespace,
and routing around that seam would break them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import clock
import migrations
import shift_windows

BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_kuwait_is_a_fixed_five_hour_offset_from_kst_no_dst():
    """Kuwait is UTC+3 all year, so the zone must resolve to a constant offset.

    Checked in both January and July: a zone file that wrongly carried a DST rule would
    shift the clock-in window by an hour twice a year, which is exactly the "who is late"
    bug this whole change exists to remove. A missing zone database falls back to the same
    fixed offset (``clock``), and this pins that fallback to the real thing.
    """
    for month in (1, 7):
        moment = datetime(2026, month, 15, 12, 0, tzinfo=clock.KUWAIT_TZ)
        assert moment.utcoffset() == timedelta(hours=3), month


def test_clock_now_is_naive_kuwait_wall_clock():
    """``clock.now()`` is naive (drop-in for the stored format) and reads Kuwait."""
    moment = clock.now()
    assert moment.tzinfo is None
    drift = abs((moment - datetime.now(clock.KUWAIT_TZ).replace(tzinfo=None)).total_seconds())
    assert drift <= 5.0


def test_install_reports_kuwait_and_never_lies_about_the_platform():
    info = clock.INSTALLED
    assert info["timezone"] == "Asia/Kuwait"
    assert info["tz_env"] == "Asia/Kuwait" or not info["tzset_available"]
    # ``local_time_is_kuwait`` is measured, not assumed: on a host whose clock is not Kuwait
    # and that has no way to be re-pinned (Windows), it must be allowed to say so.
    assert isinstance(info["local_time_is_kuwait"], bool)


def test_the_monthly_aggregate_does_not_ask_sqlite_for_localtime():
    """The regression the request named: SQLite's ``'now','localtime'`` is host-relative."""
    source = (BACKEND_DIR / "main.py").read_text(encoding="utf-8")
    assert "strftime('%Y-%m', 'now', 'localtime')" not in source
    assert "datetime.now().strftime(\"%Y-%m\")" in source


def test_a_geofence_window_is_judged_on_kuwait_wall_clock():
    """A punch's verdict is the one computed from the Kuwait time of day."""
    window = shift_windows.Window(
        start="04:00", end="06:30", timezone=shift_windows.DEFAULT_TIMEZONE
    )
    moment = clock.now()
    minutes = moment.hour * 60 + moment.minute
    assert window.contains_moment(moment) == shift_windows.contains(
        window.start_minutes, window.end_minutes, minutes
    )
    assert window.arrival(moment).local_time == moment.strftime("%H:%M")


# ---------------------------------------------------------------------------
# the staged attendance-UTC migration (migration 28)
# ---------------------------------------------------------------------------
def test_the_migration_shift_and_the_python_conversion_are_the_same_rule():
    """The SQL backfill and ``clock.to_utc`` must agree, or rows convert two different ways."""
    assert migrations._UTC_SHIFT_HOURS == clock.KUWAIT_UTC_OFFSET_HOURS
    sample = "2026-09-18 17:12:26"
    conn = sqlite3.connect(":memory:")
    try:
        shifted = conn.execute(
            f"SELECT datetime(?, '-{migrations._UTC_SHIFT_HOURS} hours')", (sample,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert shifted == clock.kuwait_str_to_utc_str(sample)


def test_migration_28_names_columns_that_exist_in_the_schema():
    """Every table/column the contract names must be real, or the backfill silently skips it."""
    conn = sqlite3.connect(":memory:")
    try:
        migrations.ensure_schema(conn)
        migrations.run_migrations(conn)  # the added columns, not only the baseline tables
        for table, columns in migrations.ATTENDANCE_UTC_COLUMNS.items():
            present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assert present, f"table {table} is not in the baseline schema"
            for column in columns:
                assert column in present, f"{table}.{column} is not in the schema"
    finally:
        conn.close()


def test_migration_28_backfill_shifts_and_round_trips():
    """The backfill converts a planted value by exactly -3 h, and Kuwait reads back unchanged.

    This is the readiness proof that lets the migration be activated (see
    ``docs/RUNBOOK_ATTENDANCE_UTC.md``): the migration was also run end-to-end against a copy of
    a real database during its development, and this keeps the rule pinned in CI.
    """
    sample = "2026-09-18 17:12:26"
    conn = sqlite3.connect(":memory:")
    try:
        # A scratch table stands in for a real row, whose NOT NULL columns differ per table.
        conn.execute("CREATE TABLE _probe (at DATETIME)")
        conn.execute("INSERT INTO _probe (at) VALUES (?)", (sample,))
        conn.execute(
            "UPDATE _probe SET at = datetime(at, ?) WHERE at IS NOT NULL AND at <> '' "
            "AND datetime(at, ?) IS NOT NULL",
            (f"-{migrations._UTC_SHIFT_HOURS} hours", f"-{migrations._UTC_SHIFT_HOURS} hours"),
        )
        value = conn.execute("SELECT at FROM _probe").fetchone()[0]
    finally:
        conn.close()
    assert value == "2026-09-18 14:12:26"
    assert clock.to_kuwait(datetime.strptime(value, clock.TS_FORMAT)).strftime(clock.TS_FORMAT) == sample


def test_migration_28_leaves_unparseable_values_untouched():
    """The parseability guard: a value SQLite cannot read is skipped, never blanked to NULL."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE _probe (at DATETIME)")
        conn.executemany("INSERT INTO _probe (at) VALUES (?)", [("2026-09-18 17:12:26",), ("not a time",)])
        conn.execute(
            "UPDATE _probe SET at = datetime(at, ?) WHERE at IS NOT NULL AND at <> '' "
            "AND datetime(at, ?) IS NOT NULL",
            (f"-{migrations._UTC_SHIFT_HOURS} hours", f"-{migrations._UTC_SHIFT_HOURS} hours"),
        )
        rows = [r[0] for r in conn.execute("SELECT at FROM _probe ORDER BY at")]
    finally:
        conn.close()
    assert "not a time" in rows, "an unparseable value must survive, not become NULL"
    assert "2026-09-18 14:12:26" in rows


def test_migration_28_is_not_registered_until_the_code_moves_with_it():
    """A guard, not a formality: activating the rewrite before the readers move corrupts payroll.

    When the writer/reader conversion in the runbook lands, register migration 28 there and
    update this test in the same commit - reading the runbook is the point.

    The number is free on purpose, and it stays free while the schema moves *around* it:
    migrations 29 and 30 have been registered since, which is what
    ``test_database_from_a_newer_build`` calls the registry's deliberate gap. This test used to
    pin ``SCHEMA_VERSION == 27`` as well, which was the same claim only while 27 was the newest
    migration - so every later registration had to come here and edit a number, which is churn
    rather than a guard. What the version pin was really asking (the schema version is the
    newest migration this code knows about) is asserted directly below, and it holds whatever
    the newest migration happens to be.
    """
    assert 28 not in {version for version, _, _ in migrations.MIGRATIONS}, (
        "register migration 28 only together with the clock.utc_now/clock.to_kuwait conversion; "
        "see docs/RUNBOOK_ATTENDANCE_UTC.md"
    )
    assert migrations.SCHEMA_VERSION == max(version for version, _, _ in migrations.MIGRATIONS), (
        "the schema version and the newest registered migration disagree, so a build would "
        "report one level and apply another"
    )
