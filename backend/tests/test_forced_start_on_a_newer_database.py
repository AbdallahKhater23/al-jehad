"""A database from a *newer* build, forced up with ``STARTUP_OVERRIDE_REASON``.

WHY THIS EXISTS
---------------
The mirror of ``test_forced_start_schema_ahead``, in the other direction.

``test_database_from_a_newer_build`` pins the *refusal* this state produces on its own: a
rolled-back deployment is stopped at startup, with no override in play. This file is what
happens one step later, when somebody decides to serve anyway - which the mechanism *allows*
(``NON_OVERRIDABLE`` is ``secret_key_configured`` and ``database_reachable``, and neither is
broken here) and which the judgement does not (older code cannot know what the newer migration
added, so the runbook's advice is to roll the code forward). That gap is exactly where a test
belongs: the hatch is accepted, so the only thing standing between an operator and an
unattributable forced start is what that start leaves behind.

Three places carry it, and every one of them is the sibling file's assertion turned around:

* the **startup log** - the fatal ``schema_current`` refusal the operator overrode, the override
  line carrying the reason they typed, and, on purpose, *no* ``schema_version_ahead`` warning.
  That advisory check's subject is the opposite divergence (code ahead of the data); firing it
  here would name the wrong version and send an operator to apply migrations that are already
  applied.
* the **readiness surfaces** - ``ok: false`` with ``schema_current`` failing and
  ``schema_version_ahead`` kept out of ``degraded_checks``: a forced start is never healthy,
  and never warned about the wrong thing.
* the **console** - ``startup_override`` at critical severity, unread, naming the reason and the
  check it covered; and no degraded alert blaming the code-ahead warning.

The control for that silence is inside this file: the *same* override, against a database one
migration short of the code, must warn. Quietness that could not have been otherwise is not
evidence, so the two directions are asserted side by side under one gate.
"""

from __future__ import annotations

import sqlite3

import harness
import migrations
import notifications
import pytest
import readiness
import schema_guard
from config import settings

#: The version a newer build recorded. ``SCHEMA_VERSION`` is the newest migration this code has,
#: so anything above it is a migration it has never heard of.
NEWER_VERSION = migrations.SCHEMA_VERSION + 1

#: The ledger row that build left behind, named the way this module's sibling names its own:
#: derived from the version, so it cannot drift into claiming the wrong number.
NEWER_MIGRATION = f"{NEWER_VERSION:04d}_next_build_only"

#: The object that migration created and that is still in the file. The running code has no idea
#: what it is - and the guard classifies an extra object as informational, which is why the
#: version comparison is the only thing that can catch this state.
NEWER_TABLE = "shift_handover_notes"

#: The reason an operator types. Asserted verbatim in the log line and in the console alert: a
#: forced start nobody can attribute is indistinguishable from a permanent bypass.
OVERRIDE_REASON = "rollback drill: serving on a database from a newer build while the release is fixed"


def _connect(path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


def _table_exists(path, name: str) -> bool:
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return bool(row[0])
    finally:
        conn.close()


def _migrated(path) -> None:
    """Bring a database to ``SCHEMA_VERSION`` with the application's own migrator."""
    conn = _connect(path)
    try:
        migrations.initialize(conn)
    finally:
        conn.close()


@pytest.fixture
def future_database(tmp_path):
    """The file a newer build left behind: its migration's ledger row *and* its object.

    Both halves are deliberate. The ledger row is what the version check reads; the table is
    what a real rollback leaves lying around, and the reason the drift guard cannot help here -
    an extra object is informational to it rather than drift worth repairing.
    """
    path = tmp_path / "from_a_newer_build.db"
    conn = _connect(path)
    try:
        migrations.initialize(conn)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {NEWER_TABLE} (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
            (NEWER_VERSION, NEWER_MIGRATION, "2026-09-20 08:15:00", migrations._checksum(NEWER_MIGRATION)),
        )
    finally:
        conn.close()
    return path


@pytest.fixture
def ledger_behind(tmp_path):
    """The control: the *other* divergence, one migration short of the code.

    Same shape as the sibling module's ``short_ledger``. The newest row is deleted rather than
    the newest migration un-applied, so what the database has applied stops one short of
    ``SCHEMA_VERSION`` while every column the code reads is still there. Under the same override
    this is the state the code-ahead warning is *for*.
    """
    path = tmp_path / "code_ahead.db"
    _migrated(path)
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migrations.SCHEMA_VERSION,))
        assert migrations.current_version(conn) < migrations.SCHEMA_VERSION
    finally:
        conn.close()
    return path


def _forced_up(path, monkeypatch, reason: str = OVERRIDE_REASON):
    """Run the startup gate the way a forced deployment does, capturing its log.

    ``log`` is the gate's own seam - every line an operator sees at boot goes through it - so
    these are the real lines, not a reconstruction of them.
    """
    monkeypatch.setattr(settings, "startup_override_reason", reason, raising=False)
    monkeypatch.setattr(settings, "startup_override_until", None, raising=False)
    assert settings.startup_override_active is True, (
        "the reason did not activate the override, so the gate would refuse to start and these "
        "tests would be about the refusal instead of about what a forced start says"
    )
    lines: list[str] = []
    report = readiness.run_startup_gate(None, db_path=path, log=lines.append)
    return report, lines


def _advisory_names(report) -> list[str]:
    return [check.name for check in report.advisory_failures]


def test_the_fixture_is_the_divergence_only_the_version_check_can_see(future_database):
    """The premise, asserted rather than assumed - and it is an uncomfortable one.

    The running code has nothing left to apply and the guard has nothing to block on, so neither
    mechanism an operator would expect to notice this actually does. If that stops being true the
    assertions below would be passing for the wrong reason, so this fails first and says so.
    """
    conn = _connect(future_database)
    try:
        applied = migrations.applied_versions(conn)
        live = migrations.current_version(conn)
        pending = migrations.pending_migrations(conn)
    finally:
        conn.close()

    assert live == NEWER_VERSION, f"the ledger is at {live}, not at the newer build's {NEWER_VERSION}"
    assert pending == [], (
        "the running code still has migrations to apply, so this fixture is the *other* "
        "divergence and every assertion below would be about a state no rollback produces"
    )
    assert set(range(1, migrations.SCHEMA_VERSION + 1)) <= applied, (
        f"the ledger is missing migrations this code does know about: {sorted(applied)}"
    )
    assert _table_exists(future_database, NEWER_TABLE), (
        "the newer migration's own object is missing, so this fixture is a ledger edit rather "
        "than the file a rolled-back deployment is really started against"
    )

    guard = schema_guard.inspect(future_database)
    assert guard.blocking == [], (
        f"the guard has something to block on: {[item.name for item in guard.blocking]}"
    )
    assert [(item.kind, item.name, item.severity) for item in guard.drift] == [
        ("extra_table", NEWER_TABLE, "info")
    ], (
        "the guard's only finding about this file has to be the newer build's extra object, "
        "classified informational - anything repairable is something a startup could change "
        f"under the test: {[(i.kind, i.name, i.severity) for i in guard.drift]}"
    )
    assert not [item for item in guard.drift if item.repairable], (
        f"the guard offers to repair something, so the gate would rewrite the file: "
        f"{[item.name for item in guard.drift if item.repairable]}"
    )


def test_a_forced_start_serves_and_leaves_the_newer_ledger_where_it_was(future_database, monkeypatch):
    """The hatch is accepted here, and nothing quietly resolves the divergence.

    Both halves matter. The first is the mechanism: nothing in ``NON_OVERRIDABLE`` is broken, so
    ``STARTUP_OVERRIDE_REASON`` buys traffic even in the direction the runbook says to roll
    forward from. The second is why the flags below have to be emitted at all: the gate does not
    re-interpret the ledger, so the deployment keeps running against data it cannot fully read
    until somebody deals with it.
    """
    guard = schema_guard.inspect(future_database)
    assert guard.blocking == [], (
        "the guard has something to repair, so this start would be about a repaired database "
        f"rather than about the divergence: {[item.name for item in guard.blocking]}"
    )

    report, _ = _forced_up(future_database, monkeypatch)

    assert report.override_used is True, (
        "the gate did not record that it served despite fatal failures, so nothing downstream "
        "can tell a forced start from a healthy one"
    )
    assert report.as_dict()["ready"] is False, report.as_dict()
    assert report.degraded is True, report.as_dict()
    assert "schema_current" in [check.name for check in report.fatal_failures], [
        check.name for check in report.fatal_failures
    ]

    conn = _connect(future_database)
    try:
        assert migrations.current_version(conn) == NEWER_VERSION, (
            "the startup changed the ledger, so the divergence it served through was quietly "
            "reinterpreted and the next boot would not be refused"
        )
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE version = ?", (NEWER_VERSION,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == NEWER_MIGRATION, (
        f"the newer build's ledger row was rewritten by the older code: {row}"
    )
    assert _table_exists(future_database, NEWER_TABLE), (
        "the startup removed the newer build's object, so whatever it was is gone before anyone "
        "could decide what to do with it"
    )


def test_the_log_carries_the_refusal_the_override_and_no_code_ahead_warning(
    future_database, monkeypatch
):
    """The line an operator overrode, the line that attributes it - and the line that must not be there.

    Two ways to get this wrong that read the same in a log: reporting the refusal with the
    "migrations have not been applied" sentence, which sends an operator to run a migration that
    is already applied, and letting the code-ahead warning fire, which names the wrong version in
    the same direction. The first is a wrong instruction; the second is a wrong *direction*, and
    an operator who learns that warning is noise will ignore it on the shift it is right.
    """
    _, lines = _forced_up(future_database, monkeypatch)
    log = "\n".join(lines)

    refusal = [line for line in lines if line.startswith("[startup] FATAL: schema_current:")]
    assert refusal, "no fatal schema_current line in the startup log:\n" + log
    assert str(NEWER_VERSION) in refusal[0] and str(migrations.SCHEMA_VERSION) in refusal[0], (
        f"the line does not name both versions, so nobody can tell which side to move: {refusal[0]}"
    )
    assert "older build" in refusal[0], (
        f"the line does not say which side is older, so it is indistinguishable from the "
        f"migrations-have-not-run direction: {refusal[0]}"
    )

    override = [line for line in lines if line.startswith("[startup] OVERRIDE ACTIVE:")]
    assert override, f"the forced start was not logged as an override:\n" + log
    assert OVERRIDE_REASON in override[0], (
        f"the override line does not carry the reason an operator typed, so the start cannot be "
        f"attributed: {override[0]}"
    )
    assert "schema_current" in override[0], (
        f"the override line does not name what it overrode: {override[0]}"
    )

    assert not [line for line in lines if "schema_version_ahead" in line], (
        "the code-ahead warning fired for a database *ahead* of the code: it names the wrong "
        f"version and sends an operator to apply migrations that are already applied.\n" + log
    )
    assert not any(line.startswith("[startup] FATAL: migrations_all_applied:") for line in lines), (
        "every migration this build knows about is applied, so reporting one as pending would be "
        "a lie about the fixture and a wrong instruction to the operator:\n" + log
    )

    assert lines[-1].startswith("[startup] self-test complete"), (
        f"a forced start that served wrote no closing summary: {lines[-1]}"
    )
    assert "(degraded)" in lines[-1], (
        f"the closing summary does not mark the run degraded, so a forced start reads as a "
        f"healthy boot: {lines[-1]}"
    )


def test_the_readiness_surfaces_flag_the_refusal_and_report_unwell(future_database, monkeypatch):
    """``ok: false`` for anyone curling the probe, and the direction legible to an administrator.

    The public verdict is the assertion that matters most: a deployment forced up against data
    from the future must not answer a load balancer, a monitor or a colleague with "healthy".
    """
    report, _ = _forced_up(future_database, monkeypatch)

    public = readiness._public_body(None, future_database)
    assert public["ok"] is False, (
        f"a database from a newer build was reported healthy once it had been overridden: {public}"
    )
    assert "schema_current" in public["failed_checks"], public["failed_checks"]
    assert public["checks"]["schema_current"] == {"ok": False, "tier": "fatal"}, (
        f"the anonymous projection does not carry the fatal verdict: "
        f"{public['checks']['schema_current']}"
    )
    assert "schema_version_ahead" not in public["degraded_checks"], (
        "the public probe lists the code-ahead warning for a database that is ahead of the code, "
        f"so the one sentence an operator would act on points the wrong way: "
        f"{public['degraded_checks']}"
    )

    internal = readiness._internal_body(report, future_database)
    assert internal["ok"] is False, internal
    assert internal["schema"]["current"] == NEWER_VERSION, internal["schema"]
    assert internal["schema"]["expected"] == migrations.SCHEMA_VERSION, internal["schema"]
    assert internal["hardened_build_live"] is False, (
        f"the admin surface called a deployment it is refusing to serve hardened: "
        f"{internal['hardened_build_live']}"
    )

    body = public["checks"].get("schema_version_ahead")
    assert body is not None, (
        f"the code-ahead check is missing from the report entirely, so its silence here would "
        f"be indistinguishable from never having run: {sorted(public['checks'])}"
    )
    assert body == {"ok": True, "tier": "advisory"}, body


def test_the_code_ahead_warning_warns_when_the_direction_is_the_other_one(
    future_database, ledger_behind, monkeypatch
):
    """The control, under one override and one gate: silence that *could* have been a warning.

    ``schema_version_ahead`` warning here would be wrong, and this test would still pass if the
    check simply never fired for anything - so the same override is run against the *other*
    divergence and the warning is required there. Between the two runs, the only thing that
    changed is which side is ahead.
    """
    ahead = readiness._check_schema_version_ahead({"db_path": future_database})
    assert ahead.ok is True, (
        f"the code-ahead warning fired for a database ahead of the code: {ahead.detail}"
    )
    assert ahead.value["database"] == NEWER_VERSION, ahead.value
    assert ahead.value["code"] == migrations.SCHEMA_VERSION, ahead.value
    assert str(NEWER_VERSION) in ahead.detail and str(migrations.SCHEMA_VERSION) in ahead.detail, (
        f"the quiet verdict does not name both numbers, so it reads like a check that did not "
        f"look: {ahead.detail}"
    )

    report, lines = _forced_up(future_database, monkeypatch)
    assert "schema_version_ahead" not in _advisory_names(report), _advisory_names(report)

    behind_report, behind_lines = _forced_up(ledger_behind, monkeypatch)
    assert "schema_version_ahead" in _advisory_names(behind_report), (
        "the control did not warn, so this test cannot tell a check that stayed quiet on purpose "
        f"from one that never fires: {_advisory_names(behind_report)}"
    )
    warning = [
        line for line in behind_lines if line.startswith("[startup] ADVISORY: schema_version_ahead:")
    ]
    assert warning, "the control's log carries no code-ahead warning:\n" + "\n".join(behind_lines)
    assert str(migrations.SCHEMA_VERSION) in warning[0], warning[0]
    assert not [line for line in lines if "schema_version_ahead" in line], (
        "the future-database run warned after all:\n" + "\n".join(lines)
    )


def test_the_console_is_flagged_with_the_override_and_no_code_ahead_warning(
    future_database, monkeypatch, app_module
):
    """The alert that survives the terminal being closed, and what it must not say.

    ``_record`` writes into ``admin_notifications``, which is the console's queue. An operator
    coming back in the morning has the reason, the check it covered and the fact that nobody has
    answered it yet - and no advisory line sending them to re-run migrations.
    """
    _forced_up(future_database, monkeypatch)

    with app_module.db() as conn:
        rows = conn.execute(
            "SELECT kind, severity, title, body, read_at FROM admin_notifications "
            "WHERE kind IN (?, ?) ORDER BY id",
            (notifications.KIND_STARTUP_OVERRIDE, notifications.KIND_STARTUP_DEGRADED),
        ).fetchall()

    override = [row for row in rows if row["kind"] == notifications.KIND_STARTUP_OVERRIDE]
    assert len(override) == 1, f"a forced start left {len(override)} startup_override alerts"
    assert override[0]["severity"] == notifications.SEVERITY_CRITICAL, override[0]["severity"]
    assert override[0]["read_at"] is None, (
        "the override alert was born read, so nobody is told a forced start happened"
    )
    assert OVERRIDE_REASON in override[0]["body"], (
        f"the alert does not carry the reason the operator typed: {override[0]['body']!r}"
    )
    assert "schema_current" in override[0]["body"], override[0]["body"]

    for row in rows:
        assert "schema_version_ahead" not in row["body"], (
            f"a {row['kind']} alert blames the code-ahead warning while the database is ahead of "
            f"the code: {row['body']!r}"
        )


def test_the_live_probe_answers_503_and_names_the_override_as_active(
    future_database, client, monkeypatch
):
    """The end of the chain: the real routes, over HTTP, against the real divergence.

    Asserts the status code rather than only the body, because ``curl -f`` is how a deployment
    runbook asks - a 200 with ``ok: false`` inside would still read as up. And it asserts the
    override is legible to an administrator, because "the gate served this" is exactly the fact a
    forced deployment is tempted to hide.
    """
    monkeypatch.setattr(settings, "database_path", future_database, raising=False)
    monkeypatch.setattr(settings, "startup_override_reason", OVERRIDE_REASON, raising=False)
    monkeypatch.setattr(settings, "startup_override_until", None, raising=False)

    public = client.get("/api/v1/readiness")
    assert public.status_code == 503, public.text
    assert public.json()["ok"] is False, public.text
    assert "schema_current" in public.json()["failed_checks"], public.text
    assert "schema_version_ahead" not in public.json()["degraded_checks"], public.text

    admin = client.get("/api/v1/admin/readiness", headers=harness.bearer(harness.HEAD_ADMIN))
    assert admin.status_code == 503, admin.text
    body = admin.json()
    assert body["admin"]["startup_override_active"] is True, body["admin"]
    assert body["schema"]["current"] > body["schema"]["expected"], body["schema"]
    checks = {check["name"]: check for check in body["admin"]["checks"]}
    assert checks["schema_current"]["ok"] is False, checks["schema_current"]
    assert checks["schema_version_ahead"]["ok"] is True, checks["schema_version_ahead"]
