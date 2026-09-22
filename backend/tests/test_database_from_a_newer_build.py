"""A database that belongs to a *newer* build must never be reported healthy.

WHY THIS EXISTS
---------------
The rollback direction. A newer build ran its migrations - the ledger sits at 19 - the
release is rolled back to a build that only knows 18, and the older process is started
against the file the newer one left behind. That is what a failed release looks like when
the database cannot be restored from backup.

It is also the one schema divergence the **drift guard cannot see**. Every table and column
the older code expects is still there, so ``schema_guard.inspect`` reports nothing fatal and
nothing is repaired; what the newer migration *added* is simply an extra object, which the
guard classifies as informational. Only the version comparison can catch this. That is why
the fixture below carries both halves - the ledger row and the object - and why the premise
test asserts the guard is quiet: the day the guard starts blocking on this, the assertions
below would be passing for a reason other than the one they were written for.

``readiness._check_schema_version`` does catch it (``live > expected`` is fatal) and
``test_migration_drift`` already pins that check on its own. What is pinned *here* is the
fact inside a deployment, at the three places an operator actually meets it:

* the **startup log** - a ``[startup] FATAL: schema_current:`` line naming both numbers and
  the *direction*, because "the code is older than the data" is a different instruction from
  "the migrations have not run", and it is the difference between rolling the code forward
  and re-running a migration;
* the **gate** - the run ends in a refusal, with no closing summary line. The summary is the
  shape a healthy boot leaves behind, so a run that reached this state must not have written
  one;
* the **readiness surfaces** - ``ok: false`` and a 503 from the live probe, with the admin
  surface showing ``schema.current`` ahead of ``schema.expected`` and every migration this
  build knows about marked applied, which is the trap this flag exists to spring.

The mirror case - the code *ahead* of the database - is ``test_forced_start_schema_ahead``'s
subject. The two are deliberately not folded together: that one is the state an override exists
for, and this one is a state an override cannot make *safe*, because an older build cannot know
what the newer migration added. The mechanism is not the judgement, though: nothing in
``NON_OVERRIDABLE`` is broken here, so ``STARTUP_OVERRIDE_REASON`` is accepted and the
deployment serves anyway. What that forced start has to leave behind - and what it must *not*
say - is ``test_forced_start_on_a_newer_database``'s subject, and the override is cleared below
so these tests are about the refusal. Both directions assert the *other* direction's check stays
quiet, so a future change that made one check answer for both would fail here.
"""

from __future__ import annotations

import sqlite3

import harness
import migrations
import pytest
import readiness
import schema_guard
from config import settings

#: The version a newer build recorded. One past this build is already unrecognisable to it:
#: ``SCHEMA_VERSION`` is the newest migration this code has, so anything above it is a
#: migration it has never heard of.
NEWER_VERSION = migrations.SCHEMA_VERSION + 1

#: The name that build gave its migration, for the ledger row it left behind.
NEWER_MIGRATION = "0019_shift_handover_notes"

#: The object that migration created and that is still in the file. The running code has no
#: idea what it is - which is precisely the risk the refusal is about.
NEWER_TABLE = "shift_handover_notes"


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


@pytest.fixture
def future_database(tmp_path):
    """The file a newer build left behind: its migration's ledger row *and* its object.

    Both halves are deliberate. The ledger row is what the version check reads, and the table
    is what a real rollback leaves lying around - and it is the reason the guard cannot help
    here, because an extra object is informational to it rather than drift worth repairing.
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


def _refuse(monkeypatch, path) -> tuple[RuntimeError, list[str]]:
    """Run the startup gate the way an unforced deployment does, and collect its log.

    ``log`` is the gate's own seam - every line an operator sees at boot goes through it - so
    these are the real lines, not a reconstruction of them. The override is cleared first: an
    operator who has ``STARTUP_OVERRIDE_REASON`` exported would otherwise make this test about
    the override, and the override is the other direction's business, not this one's.
    """
    monkeypatch.setattr(settings, "startup_override_reason", None, raising=False)
    monkeypatch.setattr(settings, "startup_override_until", None, raising=False)
    assert settings.startup_override_active is False, (
        "an override is active in this environment, so the gate would serve instead of "
        "refusing and these tests would be about the override rather than about the refusal"
    )

    lines: list[str] = []
    with pytest.raises(RuntimeError) as refusal:
        readiness.run_startup_gate(None, db_path=path, log=lines.append)
    return refusal.value, lines


def test_the_fixture_is_the_divergence_the_drift_guard_cannot_see(future_database):
    """The premise, asserted rather than assumed - and it is an uncomfortable one.

    The running code has *nothing* left to apply and the guard has *nothing* to block on, so
    neither of the two mechanisms an operator would expect to notice this actually does. If
    that stops being true the assertions below would be passing for the wrong reason, so this
    test fails first and says so.
    """
    conn = _connect(future_database)
    try:
        applied = migrations.applied_versions(conn)
        live = migrations.current_version(conn)
        pending = migrations.pending_migrations(conn)
    finally:
        conn.close()

    assert live == NEWER_VERSION, f"the ledger is at {live}, not at the newer build's {NEWER_VERSION}"
    assert NEWER_VERSION > migrations.SCHEMA_VERSION
    assert pending == [], (
        "the running code still has migrations to apply, so this fixture is the *other* "
        "divergence (a database behind the code) and every assertion below would be about a "
        "state no rollback produces"
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
        "the drift guard has something to block on, so it - not the version check - would be "
        f"what caught this: {[item.name for item in guard.blocking]}"
    )


def test_the_gate_refuses_to_serve_an_older_build_against_it(future_database, monkeypatch):
    """The unforced default, which is the refusal - and it is not a repair either.

    ``STARTUP_OVERRIDE_REASON`` buys traffic for a database that is *behind* the code - the
    columns the code reads may not exist yet, and serving is judged the lesser evil. Here the
    code cannot read what it has never heard of, so the advice is to roll the build forward.
    That is advice, not a mechanism: nothing in ``NON_OVERRIDABLE`` is broken, so a reason *does*
    start this deployment, and what it then has to report is
    ``test_forced_start_on_a_newer_database``'s subject. What is pinned here is that the default
    is a refusal, and that the refusal leaves the newer build's database exactly as it found it.
    """
    refusal, _ = _refuse(monkeypatch, future_database)

    message = str(refusal)
    assert "schema_current" in message, message
    assert str(NEWER_VERSION) in message and str(migrations.SCHEMA_VERSION) in message, (
        f"the refusal does not name both versions, so nobody can tell which side to move: {message}"
    )
    assert "refusing to serve traffic" in message, message

    # And it is a refusal, not a repair: the newer build's row and object survive the attempt,
    # so a rolled-back deployment that is restarted is refused again rather than quietly
    # accepted the second time.
    conn = _connect(future_database)
    try:
        assert migrations.current_version(conn) == NEWER_VERSION, (
            "the startup changed the ledger, so the divergence it refused to serve was "
            "quietly reinterpreted and the next boot would not be refused"
        )
    finally:
        conn.close()
    assert _table_exists(future_database, NEWER_TABLE), (
        "the startup removed the newer build's object, so whatever it was is gone before "
        "anyone could decide what to do with it"
    )


def test_the_log_names_the_direction_and_never_writes_a_healthy_summary(future_database, monkeypatch):
    """The line has to say *which way* the divergence runs, and the run has to end on it.

    Two ways to get this wrong that read the same in a log: reporting the older-build case
    with the "migrations have not been applied" sentence (which sends an operator to run a
    migration that is already applied), and printing the self-test summary anyway (which is
    the shape a healthy boot leaves behind, and the first thing a tired operator greps for).
    """
    _, lines = _refuse(monkeypatch, future_database)
    log = "\n".join(lines)

    refusal = [line for line in lines if line.startswith("[startup] FATAL: schema_current:")]
    assert refusal, "no fatal schema_current line in the startup log:\n" + log
    assert str(NEWER_VERSION) in refusal[0] and str(migrations.SCHEMA_VERSION) in refusal[0], (
        f"the line does not name both versions: {refusal[0]}"
    )
    assert "older build" in refusal[0], (
        f"the line does not say which side is older, so it is indistinguishable from the "
        f"migrations-have-not-run direction: {refusal[0]}"
    )

    assert "[startup] OVERRIDE ACTIVE:" not in log, (
        "an override was reported served for a divergence there is no override for:\n" + log
    )
    assert "self-test complete" not in log, (
        "the gate wrote its closing summary for a run that refused to serve - that summary is "
        "what a healthy boot leaves behind and the first line an operator greps for:\n" + log
    )

    # The mirror direction stays quiet. A code-ahead warning here would name the wrong
    # version and send the operator to apply migrations that are already applied.
    assert "schema_version_ahead" not in log, (
        "the code-ahead warning fired for the reverse direction:\n" + log
    )
    assert not any(line.startswith("[startup] FATAL: migrations_all_applied:") for line in lines), (
        "every migration this build knows about is applied, so reporting one as pending "
        "would be a lie about the fixture and a wrong instruction to the operator:\n" + log
    )


def test_the_readiness_surfaces_flag_it_and_report_unhealthy(future_database):
    """Anyone curling the probe gets a verdict; nobody gets a clean bill of health.

    The public body is the one a load balancer, a monitor and a curious colleague read, so
    ``ok: false`` there is the assertion that matters. The admin body is where the direction
    becomes legible: ``schema.current`` ahead of ``schema.expected``, with every migration this
    code knows about marked applied - the trap this check exists to spring.
    """
    report = readiness.build_report(None, db_path=future_database)

    fatal = [check.name for check in report.fatal_failures]
    assert "schema_current" in fatal, f"the readiness report lost the divergence: {fatal}"
    assert report.as_dict()["ready"] is False, report.as_dict()

    public = readiness._public_body(None, future_database)
    assert public["ok"] is False, f"a database from a newer build was reported healthy: {public}"
    assert "schema_current" in public["failed_checks"], public["failed_checks"]
    assert public["checks"]["schema_current"] == {"ok": False, "tier": "fatal"}, (
        f"the anonymous projection does not carry the fatal verdict: "
        f"{public['checks']['schema_current']}"
    )
    assert "schema_version_ahead" not in public["degraded_checks"], (
        "the code-ahead warning fired for the reverse direction, so the one sentence an "
        f"operator would act on points the wrong way: {public['degraded_checks']}"
    )

    internal = readiness._internal_body(report, future_database)
    assert internal["schema"]["current"] == NEWER_VERSION, internal["schema"]
    assert internal["schema"]["expected"] == migrations.SCHEMA_VERSION, internal["schema"]
    assert internal["hardened_build_live"] is False, (
        f"the admin surface called a deployment it is refusing to serve hardened: {internal['hardened_build_live']}"
    )
    assert all(row["applied"] for row in internal["migrations"]), (
        "the migration inventory shows an unapplied migration, which would read as an "
        f"unfinished upgrade rather than as a database from the future: "
        f"{[row['version'] for row in internal['migrations'] if not row['applied']]}"
    )

    check = next(item for item in report.checks if item.name == "schema_current")
    assert check.value == {"live": NEWER_VERSION, "expected": migrations.SCHEMA_VERSION}, check.value
    assert "diverged" in check.detail, (
        f"the admin detail does not describe a divergence, so an operator reading it cannot "
        f"tell this from an unfinished upgrade: {check.detail}"
    )


def test_the_live_probe_answers_503_while_the_database_belongs_to_a_newer_build(
    future_database, client, monkeypatch
):
    """The end of the chain: the real route, over HTTP, against the real divergence.

    Asserts the status code rather than only the body, because ``curl -f`` is how a deployment
    runbook asks - a 200 with ``ok: false`` inside would still read as up.
    """
    monkeypatch.setattr(settings, "database_path", future_database, raising=False)

    public = client.get("/api/v1/readiness")
    assert public.status_code == 503, public.text
    assert public.json()["ok"] is False, public.text
    assert "schema_current" in public.json()["failed_checks"], public.text

    admin = client.get("/api/v1/admin/readiness", headers=harness.bearer(harness.HEAD_ADMIN))
    assert admin.status_code == 503, admin.text
    schema = admin.json()["schema"]
    assert schema["current"] > schema["expected"], (
        f"the admin route does not show which side is ahead: {schema}"
    )
