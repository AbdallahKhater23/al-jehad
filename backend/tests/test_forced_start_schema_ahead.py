"""A deployment forced up with ``STARTUP_OVERRIDE_REASON`` has to be told what it is.

WHY THIS EXISTS
---------------
``STARTUP_OVERRIDE_REASON`` exists for the deployment that has to serve anyway: the
migrations a newer build needs have not run, the columns that build reads may not exist,
and refusing traffic is judged the worse of the two. The hatch is deliberately narrow - it
needs a *reason*, the start is audited, and it can never cover ``secret_key_configured`` or
``database_reachable`` (``NON_OVERRIDABLE``) - but the one thing it must not do is make the
divergence quiet. An operator who forced the server up, closed the terminal and went to bed
has to be able to come back and find out what they did, from three places at once:

* the **startup log**: the fatal refusal they overrode, the override itself *with the reason
  they typed*, and the advisory ``schema_version_ahead`` naming the code's version against
  the database's - because the two fatal lines are what an override is normally read as
  ("I know, I set that"), and the warning is the part that says *what* was risked;
* the **readiness surfaces**: ``degraded_checks`` carries the warning on the public probe
  while the verdict stays ``ok: false`` - a deployment started with the override is never
  reported healthy - and the admin surface repeats the sentence with both numbers;
* the **admin notifications**: ``startup_override`` at critical severity, unread, carrying
  the reason; and ``startup_degraded`` naming the advisory checks.

``test_migration_drift`` already covers the check on its own, by calling
``_check_schema_version_ahead`` directly. What is pinned here is the check *inside the gate*,
which has a failure mode of its own: the gate runs the schema guard too, and a guard that
decided to repair a short ledger by re-running migrations would make the warning vanish a
moment after it was written. It does not - every migration here is additive, so a database
whose newest row was deleted still *looks* complete, ``schema_guard.inspect`` reports no
drift, and nothing repairs it. That premise is asserted below rather than assumed: the day
it stops holding, this test has to fail on the premise instead of quietly asserting nothing.
"""

from __future__ import annotations

import json
import sqlite3

import migrations
import notifications
import pytest
import readiness
import schema_guard
from config import settings
from harness import ADMIN, bearer

#: A second reason, for the rule that an acknowledgement answers *one* override and not the
#: hatch in general.
SECOND_REASON = "second drill: the same ledger, a different afternoon"

#: The reason an operator types. It is asserted verbatim in the log line and in the console
#: alert: an override nobody can attribute is indistinguishable from a permanent bypass.
OVERRIDE_REASON = "rollback drill: serving while a newer build's migrations are unapplied"


def _migrated(path) -> None:
    """Bring a database to ``SCHEMA_VERSION`` with the application's own migrator."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        migrations.initialize(conn)
    finally:
        conn.close()


@pytest.fixture
def short_ledger(tmp_path):
    """A database one migration short of the code: an interrupted upgrade, or a rollback.

    The *schema* is complete and the ledger is not, which is the state a forced start is
    for. The newest row is deleted rather than the newest migration un-applied, so what the
    database has applied stops one short of ``SCHEMA_VERSION`` while everything the code
    expects to read is still there.
    """
    path = tmp_path / "schema_ahead.db"
    _migrated(path)
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migrations.SCHEMA_VERSION,))
        assert migrations.current_version(conn) < migrations.SCHEMA_VERSION
    finally:
        conn.close()
    return path


@pytest.fixture
def level_ledger(tmp_path):
    """A fully migrated database: the control case for the same override."""
    path = tmp_path / "level.db"
    _migrated(path)
    return path


def _forced_up(path, monkeypatch, reason: str = OVERRIDE_REASON):
    """Run the startup gate the way a forced deployment does, capturing its log.

    ``log`` is the gate's own seam - it writes every line an operator sees at boot through
    it - so these are the real lines, not a reconstruction of them.
    """
    monkeypatch.setattr(settings, "startup_override_reason", reason, raising=False)
    monkeypatch.setattr(settings, "startup_override_until", None, raising=False)
    assert settings.startup_override_active is True, (
        "the reason did not activate the override, so the gate would refuse to start and "
        "these tests would be about the refusal instead of about what a forced start says"
    )
    lines: list[str] = []
    report = readiness.run_startup_gate(None, db_path=path, log=lines.append)
    return report, lines


def _pending(path) -> list[int]:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        return [version for version, _ in migrations.pending_migrations(conn)]
    finally:
        conn.close()


def test_a_forced_start_serves_and_leaves_the_divergence_where_it_was(short_ledger, monkeypatch):
    """The gate returns instead of raising, and nothing quietly fixes the database.

    Both halves matter. The first is the override working. The second is why the warning has
    to be emitted by the version checks at all: with the column the missing migration added
    already present, the guard sees a complete schema, so no repair runs and the divergence
    survives the startup that overrode it - which is exactly the deployment an operator needs
    to be able to read about afterwards.
    """
    guard = schema_guard.inspect(short_ledger)
    assert guard.drift == [], (
        f"this fixture is only faithful while the guard sees no drift - a guard with "
        f"something to repair would re-run migrations inside the gate and the warning below "
        f"would be about a database that no longer exists: {[(i.kind, i.name) for i in guard.drift]}"
    )
    assert guard.ready is True, "the schema-ahead fixture is not a structurally broken database"

    report, _ = _forced_up(short_ledger, monkeypatch)

    assert report.override_used is True, (
        "the gate did not record that it served despite fatal failures, so nothing downstream "
        "can tell a forced start from a healthy one"
    )
    assert _pending(short_ledger) == [migrations.SCHEMA_VERSION], (
        "the startup repaired the ledger, so a forced deployment would silently stop being "
        "behind the code - and the warning would be about a state that no longer exists"
    )
    assert report.degraded is True, report.as_dict()
    assert "schema_version_ahead" in [check.name for check in report.advisory_failures], (
        report.as_dict()
    )


def test_the_startup_log_carries_the_refusal_the_override_and_the_warning(short_ledger, monkeypatch):
    """Three lines, and the third is the one an override reads as noise unless it is prominent.

    The fatal lines are what the operator knowingly overrode; the override line is what makes
    the start attributable; the advisory line is what states the *risk* - the code expects a
    schema the database has not applied.
    """
    _, lines = _forced_up(short_ledger, monkeypatch)

    refusal = [line for line in lines if line.startswith("[startup] FATAL: schema_current:")]
    assert refusal, f"no fatal schema_current line in the startup log:\n" + "\n".join(lines)
    assert str(migrations.SCHEMA_VERSION) in refusal[0], (
        f"the refusal does not name the schema the code expects: {refusal[0]}"
    )

    override = [line for line in lines if line.startswith("[startup] OVERRIDE ACTIVE:")]
    assert override, f"the forced start was not logged as an override:\n" + "\n".join(lines)
    assert OVERRIDE_REASON in override[0], (
        f"the override line does not carry the reason an operator typed, so the start cannot "
        f"be attributed: {override[0]}"
    )
    assert "schema_current" in override[0], (
        f"the override line does not name what it overrode: {override[0]}"
    )

    warning = [line for line in lines if line.startswith("[startup] ADVISORY: schema_version_ahead:")]
    assert warning, (
        "the schema-ahead warning is missing from the forced start's log: the operator sees "
        "the two fatal lines they already knew about and nothing about what those lines cost "
        f"them.\n" + "\n".join(lines)
    )
    assert str(migrations.SCHEMA_VERSION) in warning[0], (
        f"the warning does not name the schema the code expects: {warning[0]}"
    )

    degraded = [line for line in lines if line.startswith("[startup] DEGRADED:")]
    assert degraded and "schema_version_ahead" in degraded[0], (
        f"the run's own degraded summary does not name the warning:\n" + "\n".join(lines)
    )
    assert lines[-1].endswith("(degraded)") or "(degraded)" in lines[-1], (
        f"the closing self-test line does not mark the run degraded: {lines[-1]}"
    )


def test_the_readiness_surfaces_flag_the_warning_and_never_report_health(short_ledger, monkeypatch):
    """A forced start is visible to anyone curling the probe, and green to nobody.

    The public verdict is the one that matters most: ``ok: false`` with the two fatal checks
    named. A deployment that was forced up must not answer a load balancer, a monitor or a
    curious colleague with "healthy" - the override buys traffic, not a clean bill of health.
    """
    report, _ = _forced_up(short_ledger, monkeypatch)

    public = readiness._public_body(None, short_ledger)
    assert public["ok"] is False, (
        f"the forced deployment reports healthy: a fatal schema divergence is not made "
        f"acceptable by having been overridden: {public}"
    )
    assert "schema_current" in public["failed_checks"], public["failed_checks"]
    assert "schema_version_ahead" in public["degraded_checks"], (
        f"the public probe does not list the schema-ahead warning among the degraded checks: "
        f"{public['degraded_checks']}"
    )
    assert set(public["checks"]["schema_version_ahead"]) == {"ok", "tier"}, (
        f"the anonymous projection of a check grew past its verdict: "
        f"{public['checks']['schema_version_ahead']}"
    )

    # The admin route's body is this verdict plus ``check.as_dict()`` for every check, so the
    # sentence and the numbers asserted here are literally what an operator reads there - the
    # route's own wrapping (paths, pid, uptime) carries nothing about the divergence.
    internal = readiness._internal_body(report, short_ledger)
    assert internal["schema"]["current"] < internal["schema"]["expected"], internal["schema"]
    assert internal["schema"]["expected"] == migrations.SCHEMA_VERSION, internal["schema"]
    warning = next(check for check in report.checks if check.name == "schema_version_ahead")
    assert warning.ok is False, warning.detail
    assert "STARTUP_OVERRIDE_REASON" in warning.detail, (
        f"the admin surface does not tell the operator the hatch that was used to serve "
        f"anyway: {warning.detail}"
    )
    assert warning.value["missing"] == [migrations.SCHEMA_VERSION], warning.value


def test_the_console_is_flagged_with_the_override_and_its_reason(short_ledger, monkeypatch, app_module):
    """The alert an operator finds without having read the log, and it is unread.

    ``_record`` writes into ``admin_notifications``, which is the console's queue: this is the
    half that survives the terminal being closed, and the reason is what an auditor reads.
    """
    _forced_up(short_ledger, monkeypatch)

    with app_module.db() as conn:
        rows = conn.execute(
            "SELECT kind, severity, title, body, read_at FROM admin_notifications "
            "WHERE kind IN (?, ?) ORDER BY id",
            (notifications.KIND_STARTUP_OVERRIDE, notifications.KIND_STARTUP_DEGRADED),
        ).fetchall()

    by_kind = {row["kind"]: row for row in rows}
    assert notifications.KIND_STARTUP_OVERRIDE in by_kind, (
        f"a forced start raised no startup_override alert: {[row['kind'] for row in rows]}"
    )
    override = by_kind[notifications.KIND_STARTUP_OVERRIDE]
    assert override["severity"] == notifications.SEVERITY_CRITICAL, override["severity"]
    assert OVERRIDE_REASON in override["body"], (
        f"the alert does not carry the reason the operator typed: {override['body']!r}"
    )
    assert "schema_current" in override["body"], override["body"]
    assert override["read_at"] is None, (
        "the override alert was born read, so nobody is told a forced start happened"
    )

    assert notifications.KIND_STARTUP_DEGRADED in by_kind, (
        f"the forced start raised no startup_degraded alert: {[row['kind'] for row in rows]}"
    )
    degraded = by_kind[notifications.KIND_STARTUP_DEGRADED]
    assert degraded["severity"] == notifications.SEVERITY_WARNING, degraded["severity"]
    assert "schema_version_ahead" in degraded["body"], (
        f"the degraded alert does not name the schema-ahead warning: {degraded['body']!r}"
    )


def test_every_forced_start_is_audited_and_the_alert_is_one_per_reason(short_ledger, monkeypatch, app_module):
    """Two records, answering two different questions: one per reason, one per start.

    The alert is the thing an operator *answers*, so it is keyed per reason - acknowledging it
    accepts that override, and a restart during the same incident must not throw the
    acknowledgement away. The audit trail is the thing an incident review *reads*, so it is
    append-only per start: "this deployment was forced up four times" is a question the alert
    row could not answer, because there is only one of it.
    """
    _forced_up(short_ledger, monkeypatch)

    with app_module.db() as conn:
        events = conn.execute(
            "SELECT actor_id, actor_role, entity, entity_id, after_json FROM audit_log "
            "WHERE action = 'startup_override' ORDER BY id"
        ).fetchall()
        alerts = conn.execute(
            "SELECT id, payload, acknowledged_at FROM admin_notifications WHERE kind = ? ORDER BY id",
            (notifications.KIND_STARTUP_OVERRIDE,),
        ).fetchall()

    assert len(events) == 1, f"a forced start left {len(events)} audit rows"
    assert len(alerts) == 1
    event = events[0]
    # A system event: no administrator did this, and attributing it to one would be a lie the
    # trail keeps for ever.
    assert event["actor_id"] is None and event["actor_role"] == "system"
    assert event["entity"] == "admin_notifications"
    assert event["entity_id"] == str(alerts[0]["id"]), (
        "the audit row does not point at the alert an operator is asked to answer"
    )
    after = json.loads(event["after_json"])
    assert after["reason"] == OVERRIDE_REASON
    assert "schema_current" in after["failed_checks"], after["failed_checks"]
    assert after["schema_expected"] == migrations.SCHEMA_VERSION
    assert after["blocked_irrespective"] == [], (
        "the trail should record that no non-overridable check was bypassed - that list is the "
        "difference between a forced start and a broken one"
    )
    assert json.loads(alerts[0]["payload"])["reason"] == OVERRIDE_REASON, (
        "the alert does not carry the reason, so the acknowledgement cannot be read next to it"
    )

    # The same reason, restarted: one alert (an acknowledgement survives the incident), two
    # audited starts.
    _forced_up(short_ledger, monkeypatch)
    with app_module.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'startup_override'"
        ).fetchone()[0] == 2, "a restart during the same override was not audited"
        assert conn.execute(
            "SELECT COUNT(*) FROM admin_notifications WHERE kind = ?",
            (notifications.KIND_STARTUP_OVERRIDE,),
        ).fetchone()[0] == 1, "a restart during the same override raised a second alert"

    # A different reason is a different question, and it is asked unanswered.
    _forced_up(short_ledger, monkeypatch, reason=SECOND_REASON)
    with app_module.db() as conn:
        rows = conn.execute(
            "SELECT payload, acknowledged_at FROM admin_notifications WHERE kind = ? ORDER BY id",
            (notifications.KIND_STARTUP_OVERRIDE,),
        ).fetchall()
    assert [json.loads(row["payload"])["reason"] for row in rows] == [OVERRIDE_REASON, SECOND_REASON]
    assert all(row["acknowledged_at"] is None for row in rows), (
        "a new override arrived already acknowledged"
    )


def test_answering_the_forced_start_closes_it_until_the_reason_changes(
    short_ledger, monkeypatch, client, app_module
):
    """The whole flow, end to end: forced up, unanswered, answered, and reopened by a new reason.

    This is the loop the two features close between them - the gate writes an alert nobody could
    answer, and readiness reports it as an open question until somebody does. The check reads the
    *newest* alert, so the test also pins that an acceptance given for one override cannot
    answer the next one.
    """
    _forced_up(short_ledger, monkeypatch)

    before = readiness._check_startup_override_acknowledged({})
    assert before.ok is False, before.detail
    assert before.value["reason"] == OVERRIDE_REASON
    alert_id = before.value["alert_id"]

    answered = client.post(
        f"/api/v1/admin/notifications/{alert_id}/acknowledge",
        json={"note": "Read the ledger by hand: the column is there, the row is not."},
        headers=bearer(ADMIN),
    )
    assert answered.status_code == 200, answered.text

    after = readiness._check_startup_override_acknowledged({})
    assert after.ok is True, after.detail
    assert after.value["acknowledged_by"] == ADMIN
    assert ADMIN in after.detail, "the accepted override does not say who accepted it"
    assert "startup_override_acknowledged" not in client.get("/api/v1/readiness").json()[
        "degraded_checks"
    ]

    _forced_up(short_ledger, monkeypatch, reason=SECOND_REASON)
    reopened = readiness._check_startup_override_acknowledged({})
    assert reopened.ok is False, (
        "an acknowledgement given for one reason answered a different override"
    )
    assert reopened.value["reason"] == SECOND_REASON
    assert reopened.value["alert_id"] != alert_id


def test_the_same_override_does_not_warn_about_a_level_database(level_ledger, monkeypatch):
    """No false positive: the warning reports the database, not the override.

    A gate that raised the warning whenever it had been forced up would be worse than no
    warning at all - an operator would learn to ignore it on the day it matters.
    """
    report, lines = _forced_up(level_ledger, monkeypatch)

    check = next(
        check
        for check in report.checks
        if check.name == "schema_version_ahead"
    )
    assert check.ok is True, check.detail
    assert check.value["database"] == migrations.SCHEMA_VERSION, check.value
    assert "schema_version_ahead" not in " ".join(
        line for line in lines if line.startswith("[startup] DEGRADED:")
    ), "a level database was warned about, with the override active:\n" + "\n".join(lines)
