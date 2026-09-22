"""The operator's runbook for the startup override has to stay true to the code it describes.

WHY THIS EXISTS
---------------
``docs/RUNBOOK_STARTUP_OVERRIDE.md`` tells an operator which checks a forced start can cover,
which two it can never cover, what the log and the alert queue will look like, and how to get
back to a healthy deployment. Every one of those facts is owned by the code, and every one of
them fails *quietly* when it drifts:

* a **renamed setting** is a variable an operator exports while ``config`` reads a different
  name: the gate keeps refusing, and the runbook looks followed;
* a **renamed or newly fatal check** is a row the table does not have, so the doc's "everything
  else can be forced" is a claim about a check nobody has judged;
* a **wrong severity or kind** sends an operator looking for a `critical` alert that arrives as
  a `warning`, in the console, at the moment they are trying to find out what they forced;
* a **moved route or renamed column** is the verification step itself failing while the question
  being answered is whether the deployment is being served safely;
* a **changed expiry rule** is the difference between an override that lapses and one that
  silently forces every future start - the failure mode the reason field exists to prevent.

None of that is visible to any other test - the code does not read its own runbook - so the
document is checked here, the way ``test_worker_push_runbook`` checks the push one and
``test_migration_drift`` checks a migration. Both directions, as there: every
``STARTUP_OVERRIDE_*`` variable the code reads must be documented, every name the document uses
must be one the code reads, and the table's verdicts must be the code's own.
"""

from __future__ import annotations

import re
import subprocess
import sys

import notifications
import pytest
import readiness
from config import settings
from harness import PROJECT_ROOT

RUNBOOK = PROJECT_ROOT / "docs" / "RUNBOOK_STARTUP_OVERRIDE.md"
README = PROJECT_ROOT / "README.md"
CONFIG = PROJECT_ROOT / "backend" / "config.py"
READINESS = PROJECT_ROOT / "backend" / "readiness.py"
MAIN = PROJECT_ROOT / "backend" / "main.py"
MIGRATIONS = PROJECT_ROOT / "backend" / "migrations.py"

#: The override settings, as ``config.py`` spells them. Both directions of the check below are
#: built from this shape rather than from a hand-kept list, so a new ``STARTUP_OVERRIDE_*``
#: setting joins both sides by existing.
OVERRIDE_SETTING = re.compile(r"^STARTUP_OVERRIDE_[A-Z0-9_]+$")

#: A row of the runbook's check table: ``| `name` | verdict | what forcing it means |``. The
#: verdict is what this suite compares against the code, so the three phrasings are constants
#: here and asserted against the table below.
CHECK_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
NEVER = "no - never"
NOT_IN_JUDGEMENT = "yes, but not in judgement"

#: The checks whose failure is a *contract* this build makes, not a fault to be deferred: an
#: admin route answered without its token, a route reachable through a path nobody reviewed, or
#: the admin guard not enforced at all. They are overridable by the mechanism - nothing in the
#: code stops them - which is precisely why the runbook has to say, in its own table, that they
#: are not overridable in judgement.
SECURITY_INVARIANTS = (
    "auth_enforced_on_admin_routes",
    "api_routes_authorised",
    "no_unprefixed_duplicate_routes",
)


@pytest.fixture(scope="module")
def runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _settings_read_by_config() -> set[str]:
    """Every environment variable ``config.py`` reads, by name."""
    source = CONFIG.read_text(encoding="utf-8")
    return set(re.findall(r"_env_[a-z]+\(" + r'"([A-Z0-9_]+)"', source))


def _override_settings_named_by_runbook(runbook: str) -> set[str]:
    return {
        name for name in re.findall(r"\b([A-Z][A-Z0-9_]+)\b", runbook) if OVERRIDE_SETTING.match(name)
    }


def _checks_the_code_can_make_fatal() -> set[str]:
    """Every check ``readiness.py`` declares outright as ``TIER_FATAL``.

    Read from the source rather than measured from a run, so a check added with a literal fatal
    tier has to be documented *before* anyone meets it in production. Two checks in this module
    decide their tier at run time (``network_policy`` is fatal only when the policy will not
    build; ``liveness_anti_spoofing`` is fatal only in ``enforce`` mode); the first of those
    returns a literal ``TIER_FATAL`` on the fatal branch and so is covered here, and the second
    is advisory in every shipped mode, which is why it is not in the table.
    """
    return set(re.findall(r'Check\(\s*"([a-z0-9_]+)",\s*TIER_FATAL', READINESS.read_text(encoding="utf-8")))


def _rows(runbook: str) -> dict[str, str]:
    found = {name: verdict.strip() for name, verdict in CHECK_ROW.findall(runbook)}
    assert len(found) >= 10, (
        f"only {len(found)} rows were parsed out of {RUNBOOK.name}'s check table; the table or "
        f"the parser moved, and a check with no row is a check nobody has judged: {sorted(found)}"
    )
    return found


def _routes() -> set[str]:
    """Every route path the application serves, with the mount prefix removed."""
    paths: set[str] = set()
    for path in (MAIN, READINESS):
        source = path.read_text(encoding="utf-8")
        paths |= set(re.findall(r'@\w*router\.(?:get|post|put|patch|delete)\("([^"]+)"', source))
    return paths


def _columns(table: str) -> set[str]:
    """Every column ``migrations.py`` gives a table: its ``CREATE TABLE`` *and* its ``ALTER``s.

    Both halves count. A column added by a later migration is created by this file just as
    surely as one declared inline, and this runbook has to be able to tell an operator to read
    it - a checker that read only the original ``CREATE TABLE`` would call a real column
    imaginary and send them looking for another way to see the record.
    """
    source = MIGRATIONS.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+" + re.escape(table) + r"\s*\((.*?)\n\s*\)",
        source,
        re.DOTALL | re.IGNORECASE,
    )
    assert match, f"{table} is not created by migrations.py"
    declared = set(
        re.findall(
            r"^\s*([a-z_][a-z0-9_]*)\s+(?:INTEGER|TEXT|DATETIME|REAL|BLOB)", match.group(1), re.MULTILINE
        )
    )
    added = {
        column
        for table_name, column in re.findall(
            r"add_column\(\s*conn\s*,\s*\"([a-z_]+)\"\s*,\s*\"([a-z_]+)\"", source
        )
        if table_name == table
    }
    return declared | added


def test_every_override_setting_the_code_reads_is_documented(runbook):
    """A setting a deployment can export has to be a setting the runbook mentions."""
    documented = _override_settings_named_by_runbook(runbook)
    read = {name for name in _settings_read_by_config() if OVERRIDE_SETTING.match(name)}
    assert read, "config.py reads no STARTUP_OVERRIDE_* setting at all: the runbook is describing nothing"
    missing = sorted(read - documented)
    assert missing == [], (
        f"{missing} are read by config.py and never mentioned in {RUNBOOK.name}: the operator "
        f"who needs the escape hatch finds nothing about it, and the deployment stays down"
    )


def test_every_override_setting_the_runbook_names_is_read_by_the_code(runbook):
    """And the other way: a name in the document has to be a name in the environment."""
    invented = sorted(_override_settings_named_by_runbook(runbook) - _settings_read_by_config())
    assert invented == [], (
        f"{invented} are named in {RUNBOOK.name} and read by nothing in config.py: an operator "
        f"exports them, the gate keeps refusing, and the runbook looks followed"
    )


def test_an_operator_can_find_the_document_from_the_two_places_they_look(runbook):
    """A runbook nobody links to is a runbook nobody reads at 06:50."""
    readme = README.read_text(encoding="utf-8")
    assert RUNBOOK.name in readme, (
        f"{RUNBOOK.name} is not linked from README.md: an operator reading the troubleshooting "
        f"section after a crash loop has no way to reach it"
    )
    config_source = CONFIG.read_text(encoding="utf-8")
    assert RUNBOOK.name in config_source, (
        f"the .env template config.py writes does not point at {RUNBOOK.name}, so the two "
        f"variables an operator has to edit are documented only in this document"
    )


def readiness_module_checks() -> set[str]:
    """Every check id ``readiness.py`` declares, fatal-tier or not."""
    return set(re.findall(r'Check\(\s*"([a-z0-9_]+)"', READINESS.read_text(encoding="utf-8")))


def _checks_with_a_runtime_tier() -> set[str]:
    """Checks whose tier is computed rather than declared, and so *can* be fatal.

    ``liveness_anti_spoofing`` takes its tier from ``liveness.readiness()``: in ``enforce`` mode
    with no usable evaluator it is fatal, and the name an operator then reads in the startup log
    appears nowhere as a literal ``TIER_FATAL``. Read by exclusion from the tier literal rather
    than assumed advisory, because the row is most needed on the day it *is* fatal.
    """
    return {
        name
        for name, tier in re.findall(
            r'Check\(\s*"([a-z0-9_]+)",\s*([^,\n]+),', READINESS.read_text(encoding="utf-8")
        )
        if not tier.strip().startswith("TIER_")
    }


def test_every_check_the_code_can_make_fatal_has_a_row(runbook):
    """The table is the judgement: a check missing from it is one nobody has decided about."""
    documented = _rows(runbook)
    runtime = _checks_with_a_runtime_tier()
    assert "liveness_anti_spoofing" in runtime, (
        "the runtime-tier reader no longer finds the liveness check, so it has silently stopped "
        "requiring a row for the one check whose fatal tier only the evaluator knows"
    )
    fatal = _checks_the_code_can_make_fatal() | runtime
    missing = sorted(fatal - set(documented))
    assert missing == [], (
        f"{missing} can be fatal and have no row in {RUNBOOK.name}'s table: an operator staring "
        f"at that check name in the startup log has no documented answer to "
        f"\"can I force this, and what am I accepting?\""
    )
    unknown = sorted(set(documented) - fatal - set(readiness_module_checks()))
    assert unknown == [], (
        f"{unknown} have rows and are not checks this module declares at all: the table is "
        f"describing a check the code does not have"
    )


def test_the_rows_marked_never_are_exactly_the_non_overridable_checks(runbook):
    """The two hard stops, in both directions, from the module's own constant."""
    rows = _rows(runbook)
    never = {name for name, verdict in rows.items() if verdict == NEVER}
    assert never == set(readiness.NON_OVERRIDABLE), (
        f"{RUNBOOK.name} marks {sorted(never) or 'nothing'} as never overridable, and the gate "
        f"refuses for {sorted(readiness.NON_OVERRIDABLE)}. A row that says \"yes\" beside a check "
        f"the gate will refuse anyway sends an operator to a variable that cannot save them"
    )
    assert "readiness.NON_OVERRIDABLE" in runbook, (
        "the runbook does not name the code's own constant, so a reader cannot check the list "
        "against the gate for themselves"
    )


def test_the_rows_marked_not_in_judgement_are_the_security_invariants(runbook):
    """Three checks are overridable by the mechanism and not by judgement; say so explicitly."""
    rows = _rows(runbook)
    flagged = {name for name, verdict in rows.items() if verdict == NOT_IN_JUDGEMENT}
    assert flagged == set(SECURITY_INVARIANTS), (
        f"{RUNBOOK.name} marks {sorted(flagged)} as not-overridable-in-judgement; the security "
        f"invariants whose failure means \"this is not the build you think it is\" are "
        f"{sorted(SECURITY_INVARIANTS)}"
    )
    for name in SECURITY_INVARIANTS:
        assert name not in readiness.NON_OVERRIDABLE, (
            f"{name} is now non-overridable in the code: the runbook's distinction between "
            f"\"the gate refuses\" and \"you must not\" has collapsed, and the table needs to say "
            f"which one applies"
        )


def test_the_commands_it_gives_an_operator_are_commands_that_exist(runbook):
    """The verification section is two shell commands, and an operator will paste both."""
    assert "python -m config" in runbook, runbook
    assert "python -m retention" in runbook, runbook
    for module in ("config", "retention"):
        source = (PROJECT_ROOT / "backend" / f"{module}.py").read_text(encoding="utf-8")
        assert "def main(" in source and 'if __name__ == "__main__"' in source, (
            f"the runbook tells an operator to run `python -m {module}`, which {module}.py does "
            f"not implement"
        )


def test_the_verification_commands_print_what_the_runbook_says_they_print():
    """Both halves of "confirm it is genuinely healthy", executed rather than described.

    The runbook's first step reads the flag out of ``python -m config``, and its last step offers
    ``python -m retention`` as a *report-only* sweep. Both are promises an operator acts on: the
    first is how they know the override is off, and the second is why they feel safe running a
    deletion tool on a live deployment. So both are run here, against the test's own database
    (``DATABASE_PATH`` is inherited, so this reads the generation the suite installed, and the
    dry run opens it read-only).
    """
    backend = str(PROJECT_ROOT / "backend")
    config_run = subprocess.run(
        [sys.executable, "-m", "config"], cwd=backend, capture_output=True, text=True, timeout=180
    )
    assert config_run.returncode == 0, config_run.stderr[-500:]
    assert "startup_override_active: " in config_run.stdout, (
        "`python -m config` no longer prints startup_override_active, which is the runbook's "
        "first verification step: an operator cannot tell whether the override is still on"
    )

    retention_run = subprocess.run(
        [sys.executable, "-m", "retention"], cwd=backend, capture_output=True, text=True, timeout=300
    )
    assert retention_run.returncode == 0, retention_run.stderr[-500:]
    assert "pass --apply to perform these deletions" in retention_run.stdout, (
        "the runbook tells an operator to run `python -m retention` as a report that deletes "
        "nothing; the bare command no longer announces that --apply is what deletes, so it is no "
        "longer the safe step the runbook offers"
    )


def test_every_endpoint_it_names_is_a_route_the_application_serves(runbook):
    """The verification step is a curl; a stale path is a 404 with no story."""
    routes = _routes()
    named = set(re.findall(r"/api/v1(/[A-Za-z0-9_/{}\-\.]+)", runbook))
    assert named, "the runbook names no endpoint at all, which cannot be verified"
    unknown = sorted(path for path in named if path not in routes)
    assert unknown == [], (
        f"{unknown} are named in {RUNBOOK.name} and are not routes in this application: the "
        f"operator's check of their own forced start returns a 404"
    )


def test_the_alert_rows_it_names_are_the_ones_the_gate_writes(runbook):
    """The console is where an operator finds what they did without reading a terminal."""
    assert notifications.KIND_STARTUP_OVERRIDE in runbook, runbook
    assert notifications.KIND_STARTUP_DEGRADED in runbook, runbook
    assert notifications.SEVERITY_CRITICAL in runbook, (
        f"{RUNBOOK.name} does not name the {notifications.SEVERITY_CRITICAL!r} severity: an "
        f"operator scanning the queue for the critical row does not know what to look for"
    )
    assert notifications.SEVERITY_WARNING in runbook, runbook
    body = READINESS.read_text(encoding="utf-8")
    for kind, severity in (
        (notifications.KIND_STARTUP_OVERRIDE, notifications.SEVERITY_CRITICAL),
        (notifications.KIND_STARTUP_DEGRADED, notifications.SEVERITY_WARNING),
    ):
        marker = re.search(
            r"_record\(\s*notifications\."
            + ("KIND_" + kind).upper()
            + r",\s*notifications\."
            + ("SEVERITY_" + severity).upper()
            + r',.*?dedupe_key=(?:f)?"([a-z_]+)',
            body,
            re.DOTALL,
        )
        assert marker, f"the gate no longer records {kind} at {severity}, which the runbook promises"
        assert marker.group(1).startswith(kind), (
            f"the gate records {kind} under a dedupe key beginning {marker.group(1)!r}: the key is "
            f"what the queue collapses repeats on, so a key unrelated to the kind is a queue that "
            f"fills one row per restart instead of one row per event"
        )


def test_the_columns_it_tells_an_operator_to_query_are_real_columns(runbook):
    """Every query in the runbook, against the schema the migrations create.

    Both of them, and this is the half that matters when one is *added*: the alert queue's
    columns were checked from the first version, but the *audit* query - the one that answers
    "who accepted this forced start, and why" - arrived with the acknowledgement work, where a
    renamed column would have left the runbook's most load-bearing query erroring out in the
    hands of the person trying to establish what happened.
    """
    selects = re.findall(r"SELECT\s+(.+?)\s+FROM\s+([a-z_]+)", runbook, re.IGNORECASE | re.DOTALL)
    tables = {table for _columns, table in selects}
    assert tables, "the runbook offers no query to read the alerts with"
    assert tables == {"admin_notifications", "audit_log"}, (
        f"the runbook queries {sorted(tables)}; the queue and the audit trail are both part of "
        f"what an operator has to be able to read after a forced start, and a query this suite "
        f"does not know about is one whose columns nothing checks"
    )
    for asked_columns, table in selects:
        asked = {name.strip() for name in asked_columns.replace("\n", " ").split(",")}
        unknown = sorted(asked - _columns(table))
        assert unknown == [], (
            f"{RUNBOOK.name} queries {unknown} from {table}, which migrations.py does not create: "
            f"the operator's first step is an error message"
        )


def test_the_acknowledgement_surface_it_describes_is_the_one_the_code_writes(runbook, app_module):
    """The recovery step depends on four names, and this is the one place they are compared.

    The runbook's answer to "who accepted this and why" is a specific audit action, a specific
    advisory check, and the acknowledge endpoint with the alert's own id in it. Every one of
    them is a string that a rename changes silently: the query would return nothing, the
    monitor would watch a name that never appears, and the operator would be left hand-building
    the URL. So the strings are read out of the document and compared with the code that writes
    them - and the endpoint is *exercised*, by leaving an unanswered forced start in the
    database and reading the sentence readiness reports.
    """
    action = re.findall(r"action\s*=\s*'([a-z_]+)'", runbook)
    assert action, "the runbook no longer shows how to read the acknowledgement out of audit_log"
    for name in action:
        assert f'action="{name}"' in MAIN.read_text(encoding="utf-8"), (
            f"the runbook tells an operator to look for audit action {name!r}, which nothing in "
            f"main.py writes: the record of who accepted the forced start would be unfindable"
        )

    acknowledged_check = "startup_override_acknowledged"
    assert acknowledged_check in runbook, (
        f"{RUNBOOK.name} no longer names the check that reports an unanswered forced start, so "
        f"the degraded line an operator sees has no explanation on the page"
    )
    assert acknowledged_check in readiness_module_checks(), (
        f"the runbook names {acknowledged_check!r} as the advisory a forced start raises; "
        f"readiness.py declares no such check, so the page describes a monitor entry that does "
        f"not exist"
    )

    reason = "runbook drill: the override that nobody answered"
    # ``write=True``: the transactional context only commits when it is asked to, which is the
    # difference between this row existing for the check below and being rolled back on close.
    with app_module.db(write=True) as conn:
        notifications.notify(
            conn,
            kind=notifications.KIND_STARTUP_OVERRIDE,
            severity=notifications.SEVERITY_CRITICAL,
            title="Startup gate overridden",
            body=f"The server started with failing checks ['schema_current']. Reason given: {reason}",
            dedupe_key=f"startup_override:{reason}",
            payload={"reason": reason, "override_until": None, "checks": []},
        )
        alert_id = conn.execute(
            "SELECT id FROM admin_notifications WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (notifications.KIND_STARTUP_OVERRIDE,),
        ).fetchone()["id"]

    check = readiness._check_startup_override_acknowledged({})
    assert check.ok is False, check.detail
    assert check.value["alert_id"] == alert_id, check.value
    assert check.value["reason"] == reason, check.value
    assert f"/api/v1/admin/notifications/{alert_id}/acknowledge" in check.detail, (
        f"the readiness sentence no longer names the endpoint with the alert's own id in it, so "
        f"the runbook's quickest-way-to-get-it-right instruction sends an operator to build the "
        f"URL by hand: {check.detail}"
    )


def test_the_expiry_rule_it_describes_is_the_rule_the_settings_implement(monkeypatch):
    """Three promises about the deadline, checked against the property they describe.

    A reason with no deadline, a deadline in the past, and a deadline that will not parse. The
    last two are the ones that matter: an override that outlives its incident, and one that
    *cannot be read* - which must count as expired, because the alternative is a typo that
    silently forces every future start.
    """
    monkeypatch.setattr(settings, "startup_override_reason", None, raising=False)
    monkeypatch.setattr(settings, "startup_override_until", None, raising=False)
    assert settings.startup_override_active is False, (
        "a start with no reason is forced: the runbook's whole safety argument is that an "
        "unattributed hatch is refused"
    )

    monkeypatch.setattr(settings, "startup_override_reason", "an incident", raising=False)
    assert settings.startup_override_active is True, (
        "a reasoned override with no deadline is inactive, so an operator who follows the "
        "runbook and omits the optional line cannot start"
    )

    monkeypatch.setattr(settings, "startup_override_until", "2000-01-01T00:00:00", raising=False)
    assert settings.startup_override_active is False, (
        "an expired override is still active: a forgotten .env line would disable the gate "
        "forever, which is the failure this deadline exists to prevent"
    )

    monkeypatch.setattr(settings, "startup_override_until", "when the shift ends", raising=False)
    assert settings.startup_override_active is False, (
        "an unparseable deadline is treated as active, so a typo forces every future start"
    )


def test_the_exit_code_it_promises_is_what_a_refused_start_really_exits_with(runbook):
    """The one number an operator greps for in a crash loop, measured rather than asserted.

    The gate raises inside the app's lifespan; uvicorn turns that into its own exit code. The
    runbook promises ``3``, so this runs uvicorn with a lifespan that fails the way the gate
    does and reads the code it exits with - a rollback of the framework, or a host that wraps
    the process differently, is a runbook that sends an operator looking for the wrong symptom.
    """
    assert "**3**" in runbook, (
        f"{RUNBOOK.name} does not state the exit code a refused start produces, which is the "
        f"first thing an operator has in a crash loop"
    )
    probe = (
        "import uvicorn\n"
        "from contextlib import asynccontextmanager\n"
        "from fastapi import FastAPI\n"
        "@asynccontextmanager\n"
        "async def lifespan(app):\n"
        "    raise RuntimeError('startup self-test failed, refusing to serve traffic')\n"
        "    yield\n"
        "try:\n"
        "    uvicorn.run(FastAPI(lifespan=lifespan), host='127.0.0.1', port=0, log_level='critical')\n"
        "    print('EXITED_NORMALLY')\n"
        "except SystemExit as exc:\n"
        "    print('EXIT=%s' % exc.code)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(PROJECT_ROOT / "backend"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "EXIT=3" in result.stdout, (
        f"a startup whose lifespan fails the way the gate fails exited with "
        f"{result.stdout.strip()!r} instead of 3 ({result.stderr[-300:]}). The runbook tells the "
        f"operator to expect 3, and a host's crash-loop alerting is built on that"
    )
