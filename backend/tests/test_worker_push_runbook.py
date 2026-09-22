"""The operator's runbook for worker push has to stay true to the code it describes.

WHY THIS EXISTS
---------------
``docs/RUNBOOK_WORKER_PUSH.md`` tells an operator to set a named list of variables, run two
commands, read three readiness checks, query four columns and act on six words of a dispatch
summary. Every one of those facts is owned by the code, and every one of them fails *quietly*
when it drifts:

* a **renamed variable** is a setting the operator exports and nothing reads - the deployment
  looks configured and the phone stays silent;
* a **changed default** is a plan built on a number that is no longer true (a fifteen-minute
  age window is the difference between "a notice waits for the next app open" and "a phone
  buzzes about yesterday");
* a **moved route** is a 404 at the exact moment the operator is verifying their own work, and
  a **renamed readiness check** is a name they grep for that no longer exists, which reads as
  "push is fine";
* a **renamed table or column** is a query that errors while the question being answered is
  whether push works at all;
* a **package named but not shipped** sends them to an install that does not add up.

None of that is visible to any other test - the code does not read its own runbook - so the
document is checked here, the way ``test_migration_drift`` checks a migration and
``test_deployment_manifest`` checks the files a host builds from. The rule in both directions:
every push setting the code reads is documented, and every ``PUSH_*`` / ``VAPID_*`` name the
document uses is one the code reads. A variable invented by a doc and a variable read by a
deployment's environment are the same mistake seen from opposite ends.
"""

from __future__ import annotations

import re

import pytest

from harness import PROJECT_ROOT

RUNBOOK = PROJECT_ROOT / "docs" / "RUNBOOK_WORKER_PUSH.md"
CONFIG = PROJECT_ROOT / "backend" / "config.py"
PUSH = PROJECT_ROOT / "backend" / "push.py"
MAIN = PROJECT_ROOT / "backend" / "main.py"
READINESS = PROJECT_ROOT / "backend" / "readiness.py"
MIGRATIONS = PROJECT_ROOT / "backend" / "migrations.py"
OPTIONAL = PROJECT_ROOT / "backend" / "requirements-optional.txt"
RUNTIME = PROJECT_ROOT / "requirements.txt"

#: Push settings, as ``config.py`` spells them: the ones this runbook is about. The two
#: directions of the check below are built from this shape rather than from a hand-kept list,
#: so a new ``PUSH_``/``VAPID_`` setting joins both sides by existing.
PUSH_SETTING = re.compile(r"^(?:PUSH|VAPID)_[A-Z0-9_]+$")

#: The three readiness checks that describe this channel: what it is configured to do, what it
#: is allowed to fetch, and - the only one that reads the channel's *output* - how much has been
#: left behind. The last is the one that catches a service which is configured, permitted and
#: silent, so the runbook has to name it too.
PUSH_CHECKS = ("worker_push_delivery", "push_endpoint_allowlist", "worker_notice_backlog")


@pytest.fixture(scope="module")
def runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _settings_read_by_config() -> set[str]:
    """Every environment variable ``config.py`` reads, by name."""
    return set(re.findall(r"_env_[a-z]+\(" + r'"([A-Z0-9_]+)"', CONFIG.read_text(encoding="utf-8")))


def _push_settings_read_by_config() -> set[str]:
    return {name for name in _settings_read_by_config() if PUSH_SETTING.match(name)}


def _push_settings_named_by_runbook(runbook: str) -> set[str]:
    return {name for name in re.findall(r"\b([A-Z][A-Z0-9_]+)\b", runbook) if PUSH_SETTING.match(name)}


def _routes() -> set[str]:
    """Every route path the application serves, with the mount prefix removed.

    Read from the decorators rather than from a running app: a route that exists only in a
    reference is exactly the thing being guarded against, and the routers are included with
    one prefix by hand (``main.py``) rather than by anything a test could introspect without
    starting the application.
    """
    paths: set[str] = set()
    for path in (MAIN, READINESS):
        source = path.read_text(encoding="utf-8")
        paths |= set(re.findall(r'@\w*router\.(?:get|post|put|patch|delete)\("([^"]+)"', source))
    return paths


def _check_names() -> set[str]:
    """The ids the readiness registry knows, as ``Check("name", ...)`` declares them."""
    return set(re.findall(r'Check\(\s*"([a-z0-9_]+)"', READINESS.read_text(encoding="utf-8")))


def _columns(table: str) -> set[str]:
    """The columns ``migrations.py`` creates for a table, from its own ``CREATE TABLE``."""
    source = MIGRATIONS.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+" + re.escape(table) + r"\s*\((.*?)\n\s*\)",
        source,
        re.DOTALL | re.IGNORECASE,
    )
    assert match, f"{table} is not created by migrations.py"
    return set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\s+(?:INTEGER|TEXT|DATETIME|REAL|BLOB)", match.group(1), re.MULTILINE))


def test_every_push_setting_the_code_reads_is_documented(runbook):
    """A setting a deployment can export must be a setting the runbook mentions."""
    documented = _push_settings_named_by_runbook(runbook)
    missing = sorted(_push_settings_read_by_config() - documented)
    assert missing == [], (
        f"{missing} are read by config.py and never mentioned in {RUNBOOK.name}: an operator "
        f"who needs to change one of them finds nothing, and the runbook describes a "
        f"deployment that is not the one the code runs"
    )


def test_every_push_setting_the_runbook_names_is_read_by_the_code(runbook):
    """And the other way: a name in the document has to be a name in the environment."""
    invented = sorted(_push_settings_named_by_runbook(runbook) - _settings_read_by_config())
    assert invented == [], (
        f"{invented} are named in {RUNBOOK.name} and read by nothing in config.py: an operator "
        f"follows the runbook, exports a variable no code reads, and the deployment looks "
        f"configured while nothing changes"
    )


def test_the_documented_defaults_are_the_codes_defaults(runbook):
    """The table's *Default* column is a promise about behaviour, so it is compared literally.

    Flags are documented as the ``.env`` spelling (``1``/``0``) rather than as ``True``, because
    that is what an operator types; integers and quoted strings are compared as written.
    """
    source = CONFIG.read_text(encoding="utf-8")
    found = re.findall(
        r'_(?:env_str|env_int|env_flag)\("((?:PUSH|VAPID)_[A-Z0-9_]+)",\s*([^)\n]+)\)', source
    )
    checked = 0
    for name, raw in found:
        default = raw.strip()
        if default in ("True", "False"):
            expected = "1" if default == "True" else "0"
        elif default.startswith(('"', "'")):
            expected = default.strip('"\'')
        elif re.fullmatch(r"-?\d+", default):
            expected = default
        else:  # a computed default (the endpoint allowlist) is not a literal to compare
            continue
        row = next((line for line in runbook.splitlines() if line.startswith(f"| `{name}`")), None)
        assert row is not None, f"{name} has no row in the runbook's variable table"
        assert f"`{expected}`" in row, (
            f"the runbook documents {name} as {row!r}, and the code's default is {expected!r}: "
            f"an operator plans against the number in the table"
        )
        checked += 1
    assert checked >= 4, f"only {checked} defaults were compared; the table or the parser moved"


def test_the_runbook_never_pastes_a_key(runbook):
    """The one document an operator is told to keep is the one that must not hold a key.

    The private half signs pushes as this deployment, so a runbook with a real-looking value
    after ``VAPID_PRIVATE_KEY=`` is a place people copy a key *from* (and, in this repository,
    a key committed to history). The examples are placeholders, and the secret is named as one.
    """
    for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"):
        pasted = re.search(re.escape(name) + r"=(\S{20,})", runbook)
        assert pasted is None, (
            f"{RUNBOOK.name} shows a value for {name} ({pasted.group(1)[:12]}...): an example "
            f"in a runbook is a value somebody copies, and the private half is a deployment secret"
        )
    assert "secret" in runbook.lower(), (
        "the runbook never says the private key is a secret, which is the one thing an operator "
        "must not have to infer"
    )


def test_the_commands_it_gives_an_operator_are_the_commands_that_exist(runbook):
    """Two commands, both of them real entry points, both of them checked here."""
    assert "python -m push --generate-keys" in runbook, runbook
    generator = PUSH.read_text(encoding="utf-8")
    assert '"--generate-keys" in sys.argv' in generator, (
        "the runbook's key-generation command is not handled by push.py's __main__ block"
    )
    assert "python -m config" in runbook, runbook
    config_source = CONFIG.read_text(encoding="utf-8")
    assert re.search(r'if __name__ == "__main__"', config_source) and "def main(" in config_source, (
        "the runbook tells an operator to run `python -m config`, which config.py does not implement"
    )


def test_every_endpoint_it_names_is_a_route_the_application_serves(runbook):
    """A path in a runbook is something an operator curls; a stale one is a 404 with no story.

    The document writes the paths as a caller does (``/api/v1/...``); the decorators carry the
    route without the prefix every router here is mounted under, so the prefix is stripped by
    the pattern rather than restated as a constant that could itself drift.
    """
    routes = _routes()
    named = set(re.findall(r"/api/v1(/[A-Za-z0-9_/{}\-\.]+)", runbook))
    assert named, "the runbook names no endpoint at all, which cannot be verified"
    unknown = sorted(path for path in named if path not in routes)
    assert unknown == [], (
        f"{unknown} are named in {RUNBOOK.name} and are not routes in this application: an "
        f"operator following the verification steps gets a 404 from a path the doc gave them"
    )


def test_the_readiness_checks_it_names_are_the_ones_the_code_reports(runbook):
    """The state of this channel is read off three checks; a rename makes the doc point nowhere."""
    known = _check_names()
    for name in PUSH_CHECKS:
        assert name in runbook, f"the runbook does not mention the {name} check"
        assert name in known, (
            f"{RUNBOOK.name} names the readiness check {name!r}, which readiness.py no longer "
            f"declares (it knows {sorted(known & set(PUSH_CHECKS)) or 'neither push check'})"
        )


def test_the_columns_it_tells_an_operator_to_query_are_real_columns(runbook):
    """The SQL is the verification step, so it is parsed and compared against the migrations."""
    selects = re.findall(r"SELECT\s+(.+?)\s+FROM\s+([a-z_]+)", runbook, re.IGNORECASE | re.DOTALL)
    assert selects, "the runbook offers no query to verify a delivery with"
    for columns, table in selects:
        if table not in ("worker_notifications", "worker_push_subscriptions"):
            continue
        asked = {name.strip() for name in columns.replace("\n", " ").split(",")}
        unknown = sorted(asked - _columns(table))
        assert unknown == [], (
            f"{RUNBOOK.name} queries {unknown} from {table}, which migrations.py does not "
            f"create: the operator's verification step errors while answering whether push works"
        )


def test_the_optional_sender_it_tells_you_to_install_is_optional_and_shipped():
    """``pywebpush`` degrades gracefully, so it belongs in the optional file and nowhere else.

    Pairing the document with the manifest is the point: a runbook that tells an operator to
    ``pip install`` something the repository does not ship is an operator on the phone to a
    vendor for a package that was never the plan.
    """
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "pywebpush" in runbook, "the runbook does not name the sender it depends on"
    optional = OPTIONAL.read_text(encoding="utf-8")
    assert re.search(r"^pywebpush[>=]", optional, re.MULTILINE), (
        "the runbook tells an operator to install pywebpush and requirements-optional.txt does "
        "not list it"
    )
    runtime = RUNTIME.read_text(encoding="utf-8")
    assert "pywebpush" not in runtime.lower(), (
        "pywebpush is in requirements.txt: it is an optional extra that degrades with a named "
        "reason, and pinning it turns that into a deploy that must install it"
    )
