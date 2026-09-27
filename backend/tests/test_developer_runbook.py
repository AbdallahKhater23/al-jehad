"""The root account's runbook has to stay true to the code it describes.

WHY THIS EXISTS
---------------
``docs/RUNBOOK_DEVELOPER_ACCOUNT.md`` tells an operator what the ``developer`` role can reach,
how to use it day to day, and how to rotate or revoke it. Every one of those facts is owned by
the code, and every one of them fails *quietly* when it drifts:

* a **moved or renamed route** is a verification curl that returns 404 while the operator reads
  it as "the tier is down";
* a **runtime key the store no longer holds** is a flag set that changes nothing, on the one
  screen an incident is worked from;
* an **alert kind the hub no longer accepts** is a filter that silently matches nothing;
* a **seeder flag that moved** is a rotation command that exits non-zero at 03:00, or worse,
  runs without rotating;
* a **renamed column** in the suspend/retire SQL is an error message in place of a revocation;
* an **unlinked document** is a runbook nobody finds.

None of that is visible to any other test - the code does not read its own runbook - so the
document is checked here, the way ``test_startup_override_runbook`` checks the startup one and
``test_worker_push_runbook`` checks the push one. Both directions where a set exists: every
surface, key and kind the code declares must be documented, and every one the document names
must be one the code declares.
"""

from __future__ import annotations

import re

import database
import developer
import pytest
import security
from harness import PROJECT_ROOT

RUNBOOK = PROJECT_ROOT / "docs" / "RUNBOOK_DEVELOPER_ACCOUNT.md"
README = PROJECT_ROOT / "README.md"
DEVELOPER_PY = PROJECT_ROOT / "backend" / "developer.py"
MAIN_PY = PROJECT_ROOT / "backend" / "main.py"
SEED_PY = PROJECT_ROOT / "backend" / "tools" / "seed_developer.py"

#: A declared route: ``@router.get("/developer/...")`` in either module that serves one.
ROUTE = re.compile(r'@\w*router\.(?:get|post|put|patch|delete)\("(/developer[^"]*)"')

#: A full path as the document writes it, so the operator can paste it under the host.
NAMED_PATH = re.compile(r"/api/v1(/developer[A-Za-z0-9_/{}\-\.]*)")

#: The first cell of a Markdown table row, when it is a backticked identifier. Only table rows
#: are read, so the prose that names a constant in backticks is never mistaken for a row.
TABLE_KEY = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|", re.MULTILINE)


@pytest.fixture(scope="module")
def runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The document's text from ``### heading`` up to the next ``### ``, if any.

    The tables are read out of their own section rather than the whole document: ```` `slow_query`
    ```` is a runtime *kind* and ``roster`` is an *explainable query*, and a parser that read
    every backticked table cell in the file would compare two different vocabularies at once.
    """
    start = text.index(f"### {heading}")
    rest = text[start + len(f"### {heading}"):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def _declared_routes() -> set[str]:
    routes: set[str] = set()
    for path in (DEVELOPER_PY, MAIN_PY):
        routes |= set(ROUTE.findall(path.read_text(encoding="utf-8")))
    assert routes, "no /developer route was found in the code: the runbook is describing nothing"
    return routes


def _route_pattern(route: str) -> str:
    """A declared route as a regex: ``{name}`` matches any one path segment."""
    escaped = re.escape(route)
    return re.sub(r"\\\{[^}]+\\\}", "[^/]+", escaped)


def test_every_surface_the_code_serves_is_documented(runbook):
    """Both directions of the route inventory: nothing missing, nothing invented."""
    documented = set(NAMED_PATH.findall(runbook))
    declared = _declared_routes()
    missing = sorted(route for route in declared if route not in runbook)
    assert missing == [], (
        f"{missing} are served under /developer and never appear in {RUNBOOK.name}: an operator "
        f"has no documented way to reach a surface the tier exists for"
    )
    patterns = [_route_pattern(route) for route in declared]
    unknown = sorted(
        named for named in documented
        if not any(re.fullmatch(pattern, named) for pattern in patterns)
    )
    assert unknown == [], (
        f"{unknown} are named in {RUNBOOK.name} and match no route this application serves: the "
        f"operator's own verification step answers 404"
    )


def test_the_runtime_keys_it_documents_are_the_ones_the_store_holds(runbook):
    """A flag an operator can set has to be a flag the store reads, and the reverse."""
    documented = set(TABLE_KEY.findall(_section(runbook, "Runtime store")))
    declared = set(developer.RUNTIME_KEYS)
    assert documented == declared, (
        f"{RUNBOOK.name}'s runtime table lists {sorted(documented)}; the store holds "
        f"{sorted(declared)}. A key in the table and not the store is a change that does nothing, "
        f"and one in the store and not the table is a lever nobody is told about"
    )


def test_the_alert_kinds_it_documents_are_the_ones_the_hub_accepts(runbook):
    """The hub's vocabulary, from the document against the code that raises it."""
    documented = set(TABLE_KEY.findall(_section(runbook, "The alert hub")))
    declared = set(developer.ALERT_KINDS)
    assert documented == declared, (
        f"{RUNBOOK.name} lists alert kinds {sorted(documented)}; the hub accepts "
        f"{sorted(declared)}. A kind in the table and not the code is a filter that matches "
        f"nothing; one in the code and not the table is an alert an operator cannot interpret"
    )


def test_the_diagnostics_vocabulary_it_documents_is_the_code_declared_one(runbook):
    """Both allowlists in the diagnostics section - the explainable queries and the caches."""
    documented = set(TABLE_KEY.findall(_section(runbook, "Diagnostics")))
    declared = set(developer.EXPLAINABLE) | set(developer.CACHES)
    assert documented == declared, (
        f"{RUNBOOK.name}'s diagnostics tables list {sorted(documented)}; the code declares "
        f"{sorted(declared)}. This is the difference between a diagnostic that reports and one "
        f"the operator believes ran"
    )


def test_the_rotation_commands_it_gives_are_the_tool_s_current_flags(runbook):
    """Every flag the runbook tells an operator to type has to exist in the seeder."""
    seed = SEED_PY.read_text(encoding="utf-8")
    for flag in ("--rotate-password", "--generate", "--json", "--id"):
        assert flag in runbook, f"{RUNBOOK.name} no longer shows the {flag} flag"
        assert f'"{flag}"' in seed, (
            f"{RUNBOOK.name} tells an operator to pass {flag}, which seed_developer.py does not "
            f"define: the rotation command exits with an argparse error instead of rotating"
        )
    assert "DEVELOPER_PASSWORD" in runbook and "DEVELOPER_PASSWORD" in seed, (
        "the password is read from $DEVELOPER_PASSWORD on purpose (never an argument); the "
        "runbook or the seeder has stopped naming it, so an operator cannot supply one"
    )


def test_the_identity_it_publishes_is_the_seeded_one(runbook):
    """The id an operator types at the login form, against the constant the seed uses."""
    assert developer.DEVELOPER_ID_DEFAULT in runbook, (
        f"{RUNBOOK.name} does not name the seeded id {developer.DEVELOPER_ID_DEFAULT}, so the "
        f"login and revocation commands name an account that does not exist"
    )
    assert str(security.DEVELOPER_ID_FLOOR) in runbook, (
        f"{RUNBOOK.name} does not state the root band's floor ({security.DEVELOPER_ID_FLOOR}), so "
        f"an operator cannot tell which ids belong to this tier"
    )
    assert security.DEVELOPER_ROLE in runbook and "UNASSIGNABLE_ROLES" in runbook, (
        "the runbook does not name the role and the refusal that keeps it out of the API: the "
        "reason the account cannot be created from the console is the page's own claim to make"
    )


def test_the_suspension_sql_names_real_columns(runbook):
    """The revocation writes are SQL, so their columns are checked against the live schema.

    Read from ``PRAGMA table_info`` rather than parsed out of ``migrations.py``: ``users`` gains
    its ``status`` column through a loop over a table of ``(column, ddl)`` pairs, which a source
    reader cannot resolve to a name - and a revocation that errors out is a revocation that did
    not happen.
    """
    updates = re.findall(r"UPDATE\s+users\s+SET\s+(.+?)\s+WHERE", runbook, re.IGNORECASE | re.DOTALL)
    assert updates, f"{RUNBOOK.name} shows no UPDATE of the account row"
    with database.db() as conn:
        users = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        logs = {row[1] for row in conn.execute("PRAGMA table_info(attendance_logs)")}
    for clause in updates:
        # The assignment *targets*, not a split on commas: a value can be ``COALESCE(a, b)``,
        # whose own comma is not a column separator.
        columns = set(re.findall(r"([a-z_][a-z0-9_]*)\s*=", clause))
        unknown = sorted(columns - users)
        assert unknown == [], (
            f"{RUNBOOK.name} sets {unknown} on users, which the schema does not have: the "
            f"suspend step errors and the account keeps working"
        )
    assert "status" in users and "token_version" in users, (
        "the suspension step moves status and token_version; both have to exist for it to revoke "
        "sign-in and every live session at once"
    )
    assert "worker_id" in logs and "attendance_logs" in runbook, (
        "the runbook's safety check is that the account has no shifts; it has to query "
        "attendance_logs.worker_id for that claim to mean anything"
    )


def test_an_operator_can_find_the_document_from_the_two_places_they_look(runbook):
    """A runbook nobody links to is a runbook nobody reads."""
    assert RUNBOOK.name in README.read_text(encoding="utf-8"), (
        f"{RUNBOOK.name} is not linked from README.md: the person who has to rotate or revoke the "
        f"root account has no way to reach the procedure"
    )
    assert RUNBOOK.name in SEED_PY.read_text(encoding="utf-8"), (
        f"seed_developer.py - the one tool that manages this account - does not point at "
        f"{RUNBOOK.name}, so the operator who runs it finds only a usage string"
    )
