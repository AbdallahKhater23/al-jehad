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
   schema the way a hand-written list always does. Rule 1a, beside it, records *which* list the
   runtime-written ledgers belong in: the guard passes for a table parked in any of the three,
   and the expensive mistake is a live row kept instead of cleared.
2. **Planted activity does not survive a reset.** Rows are written with each table's own
   required columns, read from the schema, so the test keeps working when a column is added.
3. **The configuration the tests read is the shipped one.** The company rules are configuration
   a real administrator can change from the console, so they are normalised too.
4. **The file trees rotate with the database.** Biometric templates, reference selfies, punch
   frames and quick-link photos live in the same generation directory as the ``times.db`` the
   reset installs, so a file a test wrote is gone for the next test - and every directory global
   the application owns is either declared here as a tree or as one it only reads.
5. **The suite still runs when it is parallelised.** The throwaway working directory is entered
   by a fixture, never at import, because pytest-xdist collects each worker's nodeids relative
   to its working directory - a ``chdir`` at import made every worker collect nothing and the
   whole suite report "no tests ran" without an error to read (rule 5, below).

It also keeps the seeded fixture *present* (one active session, one ``pending_review`` record):
clearing must not eat what the other suites address by id.

Rule 4 is the newest and was the least true. The database was rotated per test from the start;
the directories were created once per *process*, so an enrollment one test wrote was still on
disk for every test after it, with no row anywhere that could explain it - and since
``punch_frames.FRAMES_DIR`` was never redirected at all, every frame a test's punch stored went
into ``<checkout>/punch_frames``, which had collected 1467 files.
"""

from __future__ import annotations

import importlib
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from PIL import Image

import harness
from harness import (
    ACTIVITY_TABLES,
    CONFIGURATION_TABLES,
    SEEDED_TABLES,
    WORKER,
    bearer,
    db_scalar,
)

import developer
import security

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


#: Where the write watch records what the fixture did. Named so that it cannot be mistaken for
#: application state, and created only inside the one test that reads it.
WRITE_WATCH = "fixture_write_watch"


def _watch_writes_to(table: str) -> None:
    """Record every statement the fixture issues against ``table``, until the watch is dropped.

    A *figure* cannot carry the claim "nothing wrote this table": the snapshot's counter and the
    migration's own default are both 0 in this database, so "left as the snapshot had it" and
    "created afresh by the reset" are the same number. The writes can: SQLite fires these for the
    fixture's own statements - ``clear_activity``'s ``DELETE``, ``seed_database``'s inserts, a
    migration's ``INSERT OR IGNORE`` - whichever list the table happens to be in.
    """
    connection = sqlite3.connect(str(harness.current_db_path()))
    try:
        connection.execute(f"CREATE TABLE IF NOT EXISTS {WRITE_WATCH} (op TEXT NOT NULL)")
        for op, event in (("insert", "INSERT"), ("update", "UPDATE"), ("delete", "DELETE")):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS fixture_watch_{op} AFTER {event} ON {table} "
                f"BEGIN INSERT INTO {WRITE_WATCH} (op) VALUES ('{op}'); END"
            )
        connection.commit()
    finally:
        connection.close()


def _watched_writes() -> list[str]:
    """What the watch saw, in the order it happened."""
    connection = sqlite3.connect(str(harness.current_db_path()))
    try:
        return [row[0] for row in connection.execute(f"SELECT op FROM {WRITE_WATCH}")]
    finally:
        connection.close()


def _drop_write_watch() -> None:
    connection = sqlite3.connect(str(harness.current_db_path()))
    try:
        for op in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS fixture_watch_{op}")
        connection.execute(f"DROP TABLE IF EXISTS {WRITE_WATCH}")
        connection.commit()
    finally:
        connection.close()


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


def test_the_runtime_written_ledgers_are_activity_and_not_configuration():
    """*Which* list, not merely *a* list - the half the classification guard cannot see.

    ``test_every_table_is_classified_as_activity_configuration_or_seeded`` fails when a table is in
    none of the three lists, and is satisfied by whichever one it lands in. For most tables that is
    enough: ``users`` and ``company_settings`` are seeded and obvious. These four were classified
    while the application was being extended, and the wrong choice passes the guard while looking
    exactly as deliberate as the right one - in one direction an infrastructure alert somebody's
    real usage raised, or the answer to a live overtime crossing, reaching an assertion written as
    though nobody had used the system; in the other a runtime flag a test flips never being flipped
    back. So the decision is recorded here with its reason, where moving a table costs an edit.

    ``developer_config_version`` is the exception that proves the rule: it is written at runtime
    too, and is still kept, because clearing it does not reset anything - an ``UPDATE ... WHERE id
    = 1`` that matches nothing simply stops the bump, which would have a suite chasing a stale-cache
    bug that only its own fixture invented.
    """
    must_be_cleared = ("developer_alerts", "developer_config", "overtime_authorisations")
    for table in must_be_cleared:
        assert table in ACTIVITY_TABLES, (
            f"{table} is written while people use the application, so a live row reaching an "
            "assertion is the failure this fixture exists to prevent; it belongs in "
            "harness.ACTIVITY_TABLES like developer_config_version does NOT"
        )
        assert table not in CONFIGURATION_TABLES and table not in SEEDED_TABLES, (
            f"{table} is classified twice over; 'cleared and kept' is not a decision"
        )

    assert "developer_config_version" in CONFIGURATION_TABLES
    assert "developer_config_version" not in ACTIVITY_TABLES, (
        "clearing the version counter does not reset a cached configuration, it stops the bump"
    )


def test_a_kept_table_is_left_alone_by_a_reset_rather_than_only_declared_kept(app_module, client):
    """The observation behind the declaration, on the one table where the wrong answer is silent.

    "Kept" was a word in a comment. The reset *rotates the whole generation*, so a value a test
    wrote is replaced by the snapshot's whatever the classification says - which is why the
    rotation cannot be the evidence. What can:

    1. **nothing in the reset writes the table**, observed rather than inferred. This is the
       non-vacuous half: the snapshot's counter and the migration's own default are both 0 here,
       so no figure could distinguish "left as the snapshot had it" from "created afresh";
    2. **a test's own value does not reach the next test**, which is true of a kept table for the
       same reason it is true of a cleared one - the generation is replaced, not patched;
    3. **the bump still bites**, through the application. That is the part that matters, because
       the failure is not an error: ``developer._read_version`` answers 0 for a missing row and
       the shared invalidation is an ``UPDATE ... WHERE id = 1`` that matches nothing, so the
       counter would never move again and every worker would keep serving the configuration it
       had already cached.
    """
    dev_password = "root-credential-for-the-fixture-state-0001"
    counter = "SELECT version FROM developer_config_version WHERE id = 1"

    harness.reset_database(app_module)
    first = db_scalar(counter)
    assert first is not None, "the version counter has no row before anything has written to it"

    _watch_writes_to("developer_config_version")
    try:
        # The reset's own content steps, in the order ``reset_database`` runs them. Split here
        # because the watch has to stand while they run - and these are the public pieces it
        # calls, so a step added to the reset without being added here is the one thing this
        # would not see.
        app_module.init_db()
        harness.clear_activity()
        harness.seed_database(app_module)
        assert _watched_writes() == [], (
            "a step of the reset wrote the configuration counter the deployment shares, so the "
            "figure the next test reads is the fixture's rather than the snapshot's"
        )
    finally:
        _drop_write_watch()

    connection = sqlite3.connect(str(harness.current_db_path()))
    try:
        connection.execute("UPDATE developer_config_version SET version = 424242 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()

    harness.reset_database(app_module)
    assert db_scalar(counter) == first, (
        "a value written by one test reached the next, or the fixture minted a counter of its "
        "own instead of installing the snapshot's"
    )

    developer.seed_developer_account(password=dev_password, actor="test:fixture_state")
    root = bearer(developer.DEVELOPER_ID_DEFAULT, role=security.DEVELOPER_ROLE)
    read = client.get("/api/v1/developer/runtime", headers=root)
    assert read.status_code == 200, read.text
    changed = client.patch(
        "/api/v1/developer/runtime/maintenance_mode",
        json={"value": True, "note": "the fixture contract, not a deployment"},
        headers=root,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == read.json()["version"] + 1, (
        "a runtime change after a reset did not move the shared version by exactly one, so the "
        "row the bump targets is missing and workers would never see the change"
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


# ---------------------------------------------------------------------------
# 4. The same promise, for the files
# ---------------------------------------------------------------------------
#: The app modules that own a file tree. Scanning their globals is how a tree added to the
#: application - the way ``punch_frames.FRAMES_DIR`` was added and missed - is caught here
#: instead of by a directory of stray JPEGs in the checkout.
APP_MODULE_NAMES: Final = ("main", "punch_frames", "quick_links", "corpus", "registrations")

#: Directories the application only *reads*. Rotating these would be wrong - the frontend ships
#: with the code and is not a test's data - but a directory global that belongs to neither list
#: is a tree nobody decided about, which is what the check below refuses to allow.
READ_ONLY_DIRECTORIES: Final = {
    "main.FRONTEND_DIR": "the bundled SPA, served by the app and never written to",
    "main.BACKEND_DIR": "the directory this code lives in, imported from config to build paths",
    "main._BACKEND_DIR": "the same directory, from main's own __file__, for the sys.path insert",
}


def _directory_globals(module_names: tuple[str, ...]) -> dict[str, Path]:
    """Every module-level ``*_DIR`` global in those modules, resolved past any junction."""
    found: dict[str, Path] = {}
    for module_name in module_names:
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if name.endswith("_DIR") and isinstance(value, (str, Path)):
                found[f"{module_name}.{name}"] = Path(os.path.realpath(str(value)))
    return found


def test_every_directory_global_is_either_rotated_or_declared_read_only():
    """A new directory global has to be a decision, not an omission.

    This is rule 1 applied to the files. ``punch_frames.FRAMES_DIR`` was added to the
    application and not to the harness, and nothing said so: the tests passed, and the
    checkout quietly collected a frame per punch. A global that appears here unexpectedly fails
    one test instead, and the fix is one line in ``harness.FILE_TREES``.
    """
    rotated = {
        f"{module_name}.{attribute}" for module_name, attribute, _directory, _env in harness.FILE_TREES
    }
    found = set(_directory_globals(APP_MODULE_NAMES))

    unclassified = sorted(found - rotated - set(READ_ONLY_DIRECTORIES))
    assert not unclassified, (
        f"{unclassified} are directory globals this fixture knows nothing about. If the "
        "application writes files there, add it to harness.FILE_TREES so every reset rotates it "
        "and a test cannot leave its files in the checkout; if it is only read, say so and why "
        "in READ_ONLY_DIRECTORIES."
    )
    stale = sorted((rotated | set(READ_ONLY_DIRECTORIES)) - found)
    assert not stale, f"{stale} are listed but no longer exist; the lists have gone stale"


def test_each_rotated_tree_is_where_the_reset_put_it(app_module):
    """The tables are the intent; the application's own globals are the fact."""
    generation = harness.current_generation()
    for module_name, attribute, directory, _env in harness.FILE_TREES:
        module = importlib.import_module(module_name)
        resolved = Path(os.path.realpath(str(getattr(module, attribute))))
        assert resolved == generation / directory, (
            f"{module_name}.{attribute} points at {resolved}, not at the current generation's "
            f"{directory} tree ({generation / directory}) - so a file it writes would not be "
            "rotated away with the database it belongs to"
        )


def test_files_a_test_writes_do_not_survive_a_reset(app_module):
    """The leak, in the smallest form: write one file and see whether the next test inherits it.

    Written through each module's own global rather than through a path this test computes, so
    what is asserted is where the *application* would put a file - which is the thing that was
    wrong.
    """
    planted: list[Path] = []
    for module_name, attribute, _directory, _env in harness.FILE_TREES:
        module = importlib.import_module(module_name)
        target = Path(getattr(module, attribute)) / "planted-by-a-finished-test.txt"
        target.write_text("a file the application would have written")
        planted.append(target)

    assert all(target.exists() for target in planted), planted

    harness.reset_database(app_module)

    survived = sorted(str(target) for target in planted if target.exists())
    assert not survived, (
        f"{survived} survived a reset. The next test starts with a row-less file the application "
        "cannot explain, which is the state these trees were in before they rotated with the "
        "database."
    )

    # Rotating the trees must not take the seeded fixture with it: every seeded account has to
    # be enrolled again in the new generation, or the rotation would have "fixed" isolation by
    # deleting what the other suites need.
    for user_id in harness.SEED_USERS:
        assert harness.template_exists(user_id), (
            f"the reset rotated the file trees but left {user_id} without a template, so every "
            "punch test after this one would fail on an unenrolled worker"
        )


def test_a_stored_punch_frame_lands_in_the_generation_and_leaves_with_it(app_module):
    """The application's own writer, because that is the path that leaked 1467 files.

    A punch stores its frame through ``punch_frames.store_frame``; if that lands anywhere other
    than the current generation, every test that punches is filing a picture of a face next to
    the source code.
    """
    import punch_frames

    stored = Path(punch_frames.frames_dir()) / punch_frames.store_frame(
        Image.new("RGB", (48, 48), (12, 34, 56))
    )
    resolved = Path(os.path.realpath(str(stored))).resolve()

    assert resolved.parent == (harness.current_generation() / "frames").resolve(), (
        f"the punch frame was written to {resolved}, not into the current generation's frames tree"
    )
    assert harness.PROJECT_ROOT.resolve() not in resolved.parents, (
        f"the punch frame was written inside the checkout ({resolved}); test runs are how "
        "punch_frames accumulated 1467 images before this fixture rotated it"
    )

    harness.reset_database(app_module)

    assert not stored.exists(), (
        "the frame written by the previous test is still readable, so punch evidence outlives "
        "the database row that names it"
    )


def test_a_reset_rotates_one_whole_generation_and_sweeps_the_old_ones(app_module):
    """Both halves of the rotation: a new world each time, and a bounded number of old ones.

    The second half matters because a generation is now five things - a database and four file
    trees - and a rotation that never swept would trade a leaking directory for a leaking
    temp tree.
    """
    seen = {harness.current_generation()}
    for _ in range(5):
        harness.reset_database(app_module)
        generation = harness.current_generation()
        assert generation not in seen, (
            f"a reset handed back {generation}, which this suite has already used - the database "
            "and the file trees would then be inherited rather than fresh"
        )
        seen.add(generation)

    remaining = sorted(harness.GENERATIONS_DIR.glob("gen-*"))
    assert len(remaining) <= 3, (
        f"{len(remaining)} generations are on disk; the sweep keeps the last three and each one "
        "is a database plus four file trees, so an unbounded rotation is its own leak"
    )

    current = harness.current_generation()
    assert (current / "times.db").exists(), "the current generation has no database"
    for directory in harness.GENERATION_DIRECTORIES:
        assert (current / directory).is_dir(), (
            f"the current generation has no {directory} tree, so the first write into it would "
            "have to create the directory - and a write that creates its own directory can "
            "create it in the wrong place"
        )


# ---------------------------------------------------------------------------
# Rule 5: the working directory is entered by a fixture, not at import
# ---------------------------------------------------------------------------
# The reason this is a rule and not a detail: pytest-xdist imports ``harness`` (through
# ``conftest``) in every worker and then has that worker collect nodeids - ``tests/test_x.py::
# test_y`` - resolved against the worker's *working directory*. ``os.chdir`` at import ran
# before that collection, so every nodeid named a path the temp directory does not have: all
# sixteen workers collected zero tests, and the run ended in three seconds saying "no tests
# ran", with no error, no traceback, and nothing red to read. The suite's most valuable guard
# (the throwaway working directory) was the thing that made parallel runs silently test
# nothing.


def _child(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a python child against this checkout, from the checkout's own directory."""
    environment = dict(os.environ)
    environment.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(harness.BACKEND_DIR),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        **kwargs,
    )


def test_importing_the_harness_does_not_move_the_working_directory():
    """The fact xdist depends on, measured in a child so this process's cwd cannot mask it.

    A child is the right instrument twice over: it is what a worker is, and it is the only way
    to observe the import without this test's own session having already entered the temp root.
    """
    done = _child(
        [
            "-c",
            "import os, sys; sys.path.insert(0, 'tests'); import harness; "
            "print(os.getcwd()); print(harness.TMP_ROOT)",
        ]
    )
    assert done.returncode == 0, done.stderr
    working_directory, _temp_root = done.stdout.splitlines()[:2]

    assert Path(working_directory) == harness.BACKEND_DIR, (
        f"importing harness moved the working directory to {working_directory}. That happens "
        "before pytest-xdist has collected anything, and the nodeids it collects are relative "
        "to this directory - which is how a parallel run collects zero tests and reports no "
        "failures at all"
    )


def test_the_suite_runs_tests_when_it_is_parallelised():
    """The end-to-end pin: ``pytest -n 2`` on a real module has to run tests, not nothing.

    Deliberately the same command shape an operator would type, over a module small enough to
    finish in seconds, because the failure this guards against is not subtle in its effect - the
    session succeeds, exits 5, and tells you the suite is empty.
    """
    pytest.importorskip("xdist", reason="pytest-xdist is what this test is about; without it -n is not a thing")

    done = _child(["-m", "pytest", "tests/test_json_payloads_skip_the_encoder.py", "-n", "2", "-q"])
    output = done.stdout + done.stderr

    assert "no tests ran" not in output, (
        "pytest -n 2 collected nothing and reported success-with-no-failures; the worker "
        "collection is being done from a working directory that has no tests in it:"
        f"\n{output}"
    )
    assert "[0 items]" not in output, f"xdist scheduled no tests at all:\n{output}"
    assert done.returncode == 0, f"the parallel run did not pass:\n{output}"

    passed = re.search(r"(\d+) passed", output)
    assert passed is not None, f"the parallel run printed no result to read:\n{output}"
    assert int(passed.group(1)) >= 1, (
        f"pytest -n 2 ran {passed.group(1)} tests of a module that has some - so it dropped "
        f"them somewhere between collection and the workers:\n{output}"
    )
