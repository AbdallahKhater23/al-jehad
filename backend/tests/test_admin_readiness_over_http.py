"""The forced-start warning as an operator meets it: over HTTP, from a cloned database.

WHY THIS EXISTS
---------------
``test_forced_start_schema_ahead`` proves the warning is *produced*: the gate logs it, the
checks raise it, the alerts record it. It proves that through the objects those surfaces are
built from - ``run_startup_gate``, ``_public_body``, ``_internal_body(report, path)``. What was
never covered is the route itself: the wiring between a bearer token, ``build_report(request.app)``,
the JSON a console renders and the status code a monitor acts on. A payload that is right in the
object and wrong over the wire - a check the route drops, a verdict inverted by how it is
wrapped, a ``200`` where the answer has to be ``503`` - passes every assertion above.

The database is a **clone** on purpose, and not only because a copy is safer than the suite's
throwaway file: it is a clone because the state under test *is the database*. The clone is
complete in schema and one migration short in its ledger, so the route reports a divergence
while the database every other test uses stays level. Two consequences are therefore asserted
rather than assumed:

* **the route names the database it judged** (``admin.database_path``). A test that pointed the
  wrong file at readiness would otherwise pass while proving nothing about the clone.
* **a GET does not repair what it reports on.** ``run_checks`` has no repair loop by design -
  repairs live in ``run_startup_gate``, which runs once at boot - and that is worth pinning at
  the HTTP surface, because "the probe quietly fixed it" is a read that wrote.

    pytest backend/tests/test_admin_readiness_over_http.py -q
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from config import settings
from fastapi.testclient import TestClient
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer, current_db_path

import migrations

#: The reason an operator typed. Asserted inside the warning's own sentence, because the check
#: exists to tell an operator that the hatch they opened is still open.
OVERRIDE_REASON = "rollback drill: serving while a newer build's migrations are unapplied"


def _clone(live: Path, target: Path) -> Path:
    """Copy a database with SQLite's own backup API, WAL content included.

    Not ``shutil.copy``: a byte-for-byte copy of a live SQLite file can capture a torn page
    state, and the copy would then be a database whose *corruption* is the thing under test.
    ``backup()`` takes a consistent snapshot through the engine.
    """
    source = sqlite3.connect(str(live))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def _shorten_ledger(path: Path) -> None:
    """Delete the newest applied migration: a complete schema, a short ledger.

    The row is deleted rather than the migration un-applied, so the database keeps every column
    this build reads while its ledger stops one short of ``SCHEMA_VERSION``. That is the state a
    forced start is for - an interrupted upgrade, or a rollback - and the state a level database
    would hide.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migrations.SCHEMA_VERSION,))
        assert migrations.current_version(conn) < migrations.SCHEMA_VERSION
    finally:
        conn.close()


def _clone_of_the_suite_database(tmp_path, *, shorten: bool) -> Path:
    """A copy of the live test database, optionally one migration short.

    The *live* database is left exactly as it was: the point of the clone is that a test can put
    a database into a state the rest of the suite must never be in.
    """
    clone = _clone(current_db_path(), tmp_path / "readiness_clone.db")
    if shorten:
        _shorten_ledger(clone)
    return clone


@pytest.fixture(scope="module")
def route_client(app_module):
    """A client for the route, deliberately without the lifespan that boots the deployment.

    Used without the context-manager protocol on purpose, as ``test_network_hardening`` uses its
    peer clients: entering it runs the startup gate and the timers again, and *this* file is
    about what the route answers, not about whether the deployment is allowed to start at all.
    The distinction is not academic - the gate reads the face-match bands of whatever detector
    and model are active, so a checkout mid-experiment with those leaves every test that needs a
    booted app unable to run, and the forced-start warning is exactly the surface an operator
    reaches for when a deployment is in that state. What must not happen is this file passing
    because it never sent a request: the assertions below go through the real ASGI stack, the
    route's own dependency, its response model and its status code.
    """
    instance = TestClient(app_module.app)
    try:
        yield instance
    finally:
        instance.close()


def _level(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return migrations.current_version(conn)
    finally:
        conn.close()


def _pointed_at(monkeypatch, clone: Path, *, forced_up: bool = True) -> None:
    """Make this process read the clone, and (optionally) be a deployment that was forced up.

    ``settings.database_path`` is the seam every path-reading check goes through
    (``readiness._open``), so one attribute is enough for the verdict to come from the clone.
    The override reason is set as well, because the warning's own text speaks to the operator
    who used that hatch: a test with the reason unset would be asserting the sentence against a
    deployment that never opened it.
    """
    monkeypatch.setattr(settings, "database_path", clone, raising=False)
    if forced_up:
        monkeypatch.setattr(settings, "startup_override_reason", OVERRIDE_REASON, raising=False)
        monkeypatch.setattr(settings, "startup_override_until", None, raising=False)
        assert settings.startup_override_active is True, (
            "the reason did not activate the override, so this would be a test about a level "
            "deployment that happens to be behind its code"
        )


def _warning(body: dict) -> dict:
    """The schema-ahead check as the admin surface publishes it."""
    return next(check for check in body["admin"]["checks"] if check["name"] == "schema_version_ahead")


# ---------------------------------------------------------------------------
# the route, against a database that is behind the code
# ---------------------------------------------------------------------------
def test_the_admin_readiness_route_reports_a_forced_start_from_a_cloned_database(
    route_client, monkeypatch, tmp_path
):
    clone = _clone_of_the_suite_database(tmp_path, shorten=True)
    live = current_db_path()
    assert _level(live) == migrations.SCHEMA_VERSION, (
        "the suite's own database is not level, so the clone would prove nothing about where "
        "the verdict came from"
    )
    _pointed_at(monkeypatch, clone)

    # The credential boundary first: the internals are behind it, and a sweep that only ever
    # calls the route with an administrator's token would not notice if it stopped being.
    assert route_client.get("/api/v1/admin/readiness").status_code == 401
    for role in (WORKER, MOALLEM):
        refused = route_client.get("/api/v1/admin/readiness", headers=bearer(role))
        assert refused.status_code == 403, f"{role} read the internals: {refused.status_code}"

    response = route_client.get("/api/v1/admin/readiness", headers=bearer(HEAD_ADMIN))
    # 503, not 200-with-a-warning-field: the status code is what a monitor acts on, and the
    # whole point of the override is that the deployment serving traffic is not healthy.
    assert response.status_code == 503, response.text
    body = response.json()

    assert body["ok"] is False, body["ok"]
    assert "schema_current" in body["failed_checks"], body["failed_checks"]
    assert "schema_version_ahead" in body["degraded_checks"], (
        f"the route an operator opens does not list the schema-ahead warning among the "
        f"degraded checks: {body['degraded_checks']}"
    )

    # ...from the clone, named. This is the assertion that makes the rest of the test about the
    # database it claims: a body assembled from the level database would fail here.
    assert body["admin"]["database_path"] == str(clone), body["admin"]["database_path"]
    assert body["admin"]["startup_override_active"] is True, body["admin"]

    assert body["schema"]["current"] < body["schema"]["expected"], body["schema"]
    assert body["schema"]["expected"] == migrations.SCHEMA_VERSION, body["schema"]

    check = _warning(body)
    assert check["ok"] is False, check
    assert check["value"]["missing"] == [migrations.SCHEMA_VERSION], check["value"]
    # Both numbers, over the wire: what the code expects (``code``) and how far the database
    # actually got (``database``). One without the other is a warning an operator cannot act on.
    assert check["value"]["code"] == migrations.SCHEMA_VERSION, check["value"]
    assert check["value"]["database"] < check["value"]["code"], check["value"]
    assert migrations.SCHEMA_VERSION not in check["value"]["applied"], check["value"]
    # The sentence, over the wire, still names the hatch and both numbers: this is the part the
    # object-level tests cannot see being dropped in serialization.
    assert "STARTUP_OVERRIDE_REASON" in check["detail"], check["detail"]
    assert str(migrations.SCHEMA_VERSION) in check["detail"], check["detail"]

    # A probe is a read. ``run_checks`` has no repair loop, and if that ever changes this is
    # where it shows up as a GET that rewrote the database it was reporting on.
    assert _level(clone) < migrations.SCHEMA_VERSION, (
        "the readiness route repaired the database it was reporting on; a GET must not write"
    )
    assert _level(live) == migrations.SCHEMA_VERSION, "the clone was not the only database read"


def test_the_public_probe_carries_the_same_verdict_with_far_less(route_client, monkeypatch, tmp_path):
    """One database, two trust levels: the verdict everyone gets, the internals nobody does.

    Both routes are called in the same state, so this is a comparison rather than two
    independent assertions - an operator with a token and a monitor without one are looking at
    the same deployment from two sides.
    """
    clone = _clone_of_the_suite_database(tmp_path, shorten=True)
    _pointed_at(monkeypatch, clone)

    anonymous = route_client.get("/api/v1/readiness")
    assert anonymous.status_code == 503, anonymous.text
    public = anonymous.json()
    assert public["ok"] is False
    assert "schema_version_ahead" in public["degraded_checks"], public["degraded_checks"]
    # The warning is public vocabulary; its detail is not. The check's anonymous projection is a
    # verdict and a tier, because a ``detail`` string routinely carries an absolute path.
    assert set(public["checks"]["schema_version_ahead"]) == {"ok", "tier"}, public["checks"][
        "schema_version_ahead"
    ]
    assert "admin" not in public and "schema" not in public, sorted(public)
    assert str(clone) not in str(public), "the anonymous probe leaked the database path"

    internal = route_client.get("/api/v1/admin/readiness", headers=bearer(ADMIN))
    assert internal.status_code == 503
    admin = internal.json()
    # Same verdict, more evidence: the two bodies must agree, or one of them is lying about a
    # deployment somebody is about to make a decision about.
    assert {key: admin[key] for key in ("ok", "degraded", "failed_checks", "degraded_checks")} == {
        key: public[key] for key in ("ok", "degraded", "failed_checks", "degraded_checks")
    }, (admin["failed_checks"], public["failed_checks"])
    assert "detail" in _warning(admin), "the admin surface dropped the check's own sentence"


def test_a_level_clone_is_not_warned_about_even_with_the_override_active(
    route_client, monkeypatch, tmp_path
):
    """The HTTP form of the false-positive guard: the warning reports the database, not the hatch.

    A route that raised this warning whenever the deployment had been forced up would be worse
    than one that never did - an operator would learn to skip it, and skip it on the day the
    code really is ahead of the data. So the same request, the same override reason, and a
    database that is level, must come back clean.
    """
    clone = _clone_of_the_suite_database(tmp_path, shorten=False)
    _pointed_at(monkeypatch, clone)

    response = route_client.get("/api/v1/admin/readiness", headers=bearer(HEAD_ADMIN))
    body = response.json()

    # Deliberately *not* "this is a 200": the route publishes every check, and this environment
    # has other fatal failures (a face-match band the checkout's detector/model pair has no
    # thresholds for), which are none of this test's business. What is this test's business is
    # that the *schema* verdict is clean on a level database with the hatch already open.
    assert "schema_current" not in body["failed_checks"], body["failed_checks"]
    assert "migrations_all_applied" not in body["failed_checks"], body["failed_checks"]
    assert "schema_version_ahead" not in body["degraded_checks"], body["degraded_checks"]
    assert _warning(body)["ok"] is True, _warning(body)
    assert _warning(body)["value"]["missing"] == [], _warning(body)
    assert body["schema"]["current"] == body["schema"]["expected"] == migrations.SCHEMA_VERSION, body["schema"]
    assert body["admin"]["database_path"] == str(clone), body["admin"]["database_path"]

    # The status code is the verdict and nothing else, which is what a monitor acts on. Pinned
    # as an agreement rather than as a literal so it keeps testing the contract in an
    # environment where an unrelated check is failing.
    assert response.status_code == (200 if body["ok"] else 503), response.status_code
