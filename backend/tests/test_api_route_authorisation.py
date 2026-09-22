"""The API authorization gate: every route is guarded, or declared open with a reason.

WHY THIS SUITE EXISTS
---------------------
``auth_enforced_on_admin_routes`` walks every path containing ``/admin/`` and fails the
startup gate when one of them has no role guard. It is a good check with a hole in the
middle of it: the eighty-odd routes *outside* the admin surface were covered only by
convention. They happened to carry ``Depends(any_authenticated)`` because whoever wrote
them was careful - and an endpoint added under ``/api/v1/worker/`` with no ``Depends`` at
all would have been readable by anybody who could reach the port, with the gate reporting
a clean bill of health.

``readiness.api_routes_authorised`` closes that, and this suite holds it to three claims
at once:

1. it **passes on the application as it actually is**, so the gate is not decoration;
2. it **fails** on an unguarded route, on an exemption that has gone stale, on an
   exemption that is doing nothing, and on a traversal that found nothing to inspect; and
3. its exemption lists are **honest about what they claim**: a route declared public
   really does answer without a credential, and a route declared self-gated really does
   refuse an anonymous caller.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI

import readiness
from security import any_authenticated


# ---------------------------------------------------------------------------
# synthetic applications, for the failure modes the shipped app does not have
# ---------------------------------------------------------------------------
def _synthetic(routes: list[tuple[str, str, bool]]) -> FastAPI:
    """A minimal app of ``(method, path, guarded)`` routes.

    Docs are switched off so the framework's own ``/docs``, ``/redoc`` and
    ``/openapi.json`` routes do not appear and get counted as unaccounted for: a fixture
    that fails for the wrong reason proves nothing about the check under test.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()

    def handler() -> dict:  # pragma: no cover - the gate never calls it
        return {"ok": True}

    for method, path, guarded in routes:
        # The guard is attached in the decorator here and in the signature in the real
        # app; both land in ``route.dependant.dependencies``, which is what the check
        # reads, so this also pins that the check is not looking for one style only.
        options = {"dependencies": [Depends(any_authenticated)]} if guarded else {}
        getattr(router, method)(path, **options)(handler)

    app.include_router(router, prefix="/api/v1")
    return app


def _unaccounted(check) -> list[str]:
    return list(check.value["unaccounted"])


@pytest.fixture
def declared(monkeypatch):
    """Empty exemption lists, for tests that build their own application.

    The three lists describe *this* application's surface. A four-route fixture app does
    not contain those routes, so leaving them in place would make every shipped entry read
    as stale and the fixture would fail for a reason the test is not about. Tests that
    need an exemption declare only the one they mean, which also keeps them from passing
    by accident on an entry somebody adds later.
    """
    monkeypatch.setattr(readiness, "PUBLIC_ROUTES", {})
    monkeypatch.setattr(readiness, "SELF_GATED_ROUTES", {})
    monkeypatch.setattr(readiness, "PAGE_ROUTES", frozenset())
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. It passes on the real application
# ---------------------------------------------------------------------------
def test_every_route_is_guarded_or_declared_open(app_module):
    """The gate as shipped: every route on the real app is accounted for."""
    check = readiness._check_api_routes_authorised({"app": app_module.app})
    assert check.ok, (
        "SECURITY GAP: the API surface has routes that are neither guarded by a role "
        "nor declared open with a reason:\n  " + "\n  ".join(_unaccounted(check))
    )
    assert check.tier == readiness.TIER_FATAL, "an unaccounted route is not a warning"
    # The point of the check is that it looked at the whole surface, not the admin sliver.
    assert check.value["guarded"] > 50, (
        f"only {check.value['guarded']} guarded routes were enumerated; the traversal is "
        "not seeing the surface it claims to verify"
    )
    assert check.value["unaccounted"] == []
    assert check.value["stale_exemptions"] == []
    assert check.value["redundant_exemptions"] == []


def test_the_gate_is_registered_with_the_other_fatal_checks():
    names = {check.__name__ for check in readiness.CHECKS}
    assert "_check_api_routes_authorised" in names, (
        "the check exists but nothing runs it; a gate that is not in CHECKS never fires"
    )


# ---------------------------------------------------------------------------
# 2. It bites
# ---------------------------------------------------------------------------
def test_the_gate_names_a_route_with_no_guard(declared):
    """The whole reason the check exists: a new endpoint with no ``Depends`` at all."""
    check = readiness._check_api_routes_authorised(
        {"app": _synthetic([("get", "/worker/me/ledger", False)])}
    )
    assert not check.ok
    assert "GET /api/v1/worker/me/ledger" in _unaccounted(check), (
        f"the unguarded route was not named in the finding: {check.detail}"
    )


def test_the_gate_accepts_a_guarded_route(declared):
    check = readiness._check_api_routes_authorised(
        {"app": _synthetic([("get", "/worker/me/ledger", True)])}
    )
    assert check.ok, check.detail


def test_a_new_verb_does_not_inherit_its_neighbours_exemption(declared):
    """Exemptions are keyed by method *and* path, and this is what that buys.

    ``/api/v1/auth/login`` is public because signing in cannot require a session. That is
    a fact about ``POST``, not about the path: a ``DELETE`` on the same path would be a way
    to remove somebody's credential, and it must not ride in on the exemption.
    """
    declared.setattr(readiness, "PUBLIC_ROUTES", {"POST /api/v1/auth/login": "sign-in"})
    check = readiness._check_api_routes_authorised(
        {"app": _synthetic([("post", "/auth/login", False), ("delete", "/auth/login", False)])}
    )
    assert not check.ok
    unaccounted = _unaccounted(check)
    assert "DELETE /api/v1/auth/login" in unaccounted
    assert "POST /api/v1/auth/login" not in unaccounted, (
        "the exempt verb was reported as unaccounted, so the lookup is not reading the list"
    )


def test_the_gate_fails_when_it_could_enumerate_nothing():
    """A traversal that breaks must fail the gate, not vouch for a surface it never saw.

    This is the exact way the sibling check failed before: it reported "0 admin routes
    guarded, 0 missing" after a refactor routed the endpoints through a wrapper, and the
    gate believed the whole admin surface had been verified when nothing had been looked
    at.
    """
    check = readiness._check_api_routes_authorised(
        {"app": FastAPI(docs_url=None, redoc_url=None, openapi_url=None)}
    )
    assert not check.ok
    assert "verified nothing" in check.detail, check.detail


def test_an_exemption_for_a_route_that_no_longer_exists_is_a_failure(app_module, monkeypatch):
    """A stale exemption reads as "public on purpose" for a path nothing serves.

    It also covers for the rename: the new path is unaccounted for, while the list still
    advertises an intent that no longer applies to anything.
    """
    monkeypatch.setattr(
        readiness,
        "PUBLIC_ROUTES",
        {**readiness.PUBLIC_ROUTES, "GET /api/v1/branding/wordmark": "renamed away last month"},
    )
    check = readiness._check_api_routes_authorised({"app": app_module.app})
    assert not check.ok
    assert "GET /api/v1/branding/wordmark" in check.value["stale_exemptions"], check.detail


def test_an_exemption_on_an_already_guarded_route_is_a_failure(app_module, monkeypatch):
    """An exemption that is doing nothing but misinforming the next reader."""
    monkeypatch.setattr(
        readiness,
        "PUBLIC_ROUTES",
        {**readiness.PUBLIC_ROUTES, "GET /api/v1/admin/users": "someone was being cautious"},
    )
    check = readiness._check_api_routes_authorised({"app": app_module.app})
    assert not check.ok
    assert "GET /api/v1/admin/users" in check.value["redundant_exemptions"], check.detail


def test_an_exemption_without_a_reason_is_refused(app_module, monkeypatch):
    """``""`` is not a reason, and the list is only worth reading if it is filled in."""
    monkeypatch.setattr(
        readiness, "PUBLIC_ROUTES", {**readiness.PUBLIC_ROUTES, "GET /api/v1/status": "   "}
    )
    check = readiness._check_api_routes_authorised({"app": app_module.app})
    assert not check.ok
    assert any(
        "no reason" in problem for problem in _unaccounted(check)
    ), check.detail


def test_every_exemption_carries_a_reason():
    """The constants themselves, so a blank entry cannot be added by hand."""
    for declared in (readiness.PUBLIC_ROUTES, readiness.SELF_GATED_ROUTES):
        blank = sorted(key for key, reason in declared.items() if not reason.strip())
        assert not blank, f"declared open with no reason beside it: {blank}"
    assert readiness.PAGE_ROUTES, "the page routes should be an explicit list, not implied"


# ---------------------------------------------------------------------------
# 3. The exemption lists are honest about what they claim
# ---------------------------------------------------------------------------
def _probe(client, key: str):
    method, path = key.split(" ", 1)
    # A token placeholder resolves to a token nothing was issued for, which is the case
    # worth probing: it exercises the endpoint's own refusal rather than a guard's.
    path = path.replace("{token}", "no-such-token")
    return client.request(method, path)


def test_declared_public_routes_answer_without_a_credential(client):
    """Every route on the public list really is reachable with no session.

    If one of them started refusing anonymous callers, the entry would be a lie that the
    gate happily keeps reporting as verified.
    """
    wrong = []
    for key in sorted(readiness.PUBLIC_ROUTES):
        response = _probe(client, key)
        if response.status_code in (401, 403):
            wrong.append(f"  {key} -> HTTP {response.status_code}")
    assert not wrong, (
        "these routes are declared public but refused an anonymous caller, so the "
        "exemption no longer describes the endpoint:\n" + "\n".join(wrong)
    )


def test_self_gated_routes_do_not_answer_an_anonymous_caller(client):
    """"Self-gated" means it refuses you itself - so it must actually refuse.

    ``/metrics`` carries no role guard because a scrape token is one of its two accepted
    credentials, which makes its handler the only thing standing between an anonymous
    caller and the numbers. A 200 here is the failure this test exists for; 404 when the
    surface is switched off and 501 when the library is absent are both refusals.
    """
    for key in sorted(readiness.SELF_GATED_ROUTES):
        response = _probe(client, key)
        assert response.status_code != 200, (
            f"SECURITY GAP: {key} served its payload to an anonymous caller "
            f"(HTTP {response.status_code})"
        )
        assert response.status_code in (401, 404, 501), (
            f"{key} answered HTTP {response.status_code}; expected a refusal "
            "(401 without a token, 404 when disabled, 501 when unavailable)"
        )


def test_the_public_surface_is_small_enough_to_read():
    """A reviewer must be able to check the whole list in one sitting.

    Not a security property in itself, but the list only works as a review artifact while
    it stays short: the moment it becomes something people skim, an entry gets added
    without being read.
    """
    total = len(readiness.PUBLIC_ROUTES) + len(readiness.SELF_GATED_ROUTES)
    assert total <= 20, (
        f"{total} routes answer without a session; the exemption lists have stopped being "
        "reviewable and should be reconsidered"
    )


def test_the_admin_surface_shares_the_same_check(app_module):
    """The new check subsumes the old one's scope without weakening it."""
    admin = readiness._check_admin_routes_guarded({"app": app_module.app})
    assert admin.ok, admin.detail
    assert admin.value["missing"] == []
