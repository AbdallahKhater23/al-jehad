"""The API cache-policy gate: every API answer is no-store, unless declared cacheable.

WHY THIS SUITE EXISTS
---------------------
``api_routes_authorised`` answers "*may this caller read it*" and says nothing about what
the answer may be *kept* as afterwards. A token, a punch log, somebody's hours - an answer
like that sitting in a browser's disk cache or an intermediary's is readable by the next
person at the device or the next tenant of the proxy, and it is the one kind of leak a
response body never shows: the endpoint returned the right thing to the right caller, and
then left a copy behind.

``readiness.api_response_cache_policy`` closes that. It leans on a *default* - the
``cache_policy`` middleware makes every ``/api/`` answer ``no-store`` unless its handler
says otherwise - and then audits the deliberate exceptions. This suite holds it to three
claims at once:

1. it **passes on the application as it actually is**, so the gate is not decoration;
2. it **fails** when the default middleware is gone, when a handler opts out undeclared,
   when a cacheable answer is attached to a session-guarded route, when a declaration has
   gone stale or unused, and when the traversal found nothing to inspect; and
3. the exception list is **small and honest**: the branding mark is the only thing cached
   without a session, and it really is a public route.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI, Response

import readiness
from security import any_authenticated


# ---------------------------------------------------------------------------
# synthetic applications, for the failure modes the shipped app does not have
# ---------------------------------------------------------------------------
def _install_default_policy(app: FastAPI) -> None:
    """Attach a stand-in for the real ``cache_policy`` middleware, marker and all.

    The check looks for the *marker*, not the middleware's name, so this is exactly what a
    renamed-but-still-defaulting middleware would look like. If the marker plumbing ever
    breaks, these tests fail rather than the gate silently passing.
    """

    @app.middleware("http")
    async def cache_policy(request, call_next):  # pragma: no cover - never dispatched
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    cache_policy._cache_policy_marker = readiness.CACHE_POLICY_MARKER


def _plain() -> dict:
    """A handler that sets no cache policy of its own: covered by the default."""
    return {"ok": True}


def _cacheable() -> Response:
    return Response(headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _private() -> Response:
    return Response(headers={"Cache-Control": "private, max-age=30"})


def _no_store() -> Response:
    return Response(headers={"Cache-Control": "no-store"})


def _synthetic(routes, *, default_policy: bool = True) -> FastAPI:
    """A minimal app of ``(method, path, handler, guarded)`` routes.

    Docs are switched off so the framework's own ``/docs``, ``/redoc`` and
    ``/openapi.json`` routes do not appear and get counted as unaccounted for: a fixture
    that fails for the wrong reason proves nothing about the check under test.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if default_policy:
        _install_default_policy(app)
    router = APIRouter()
    for method, path, handler, guarded in routes:
        # The guard is attached in the decorator here and in the signature in the real
        # app; both land in ``route.dependant.dependencies``, which is what the check
        # reads, so this also pins that the check is not looking for one style only.
        options = {"dependencies": [Depends(any_authenticated)]} if guarded else {}
        getattr(router, method)(path, **options)(handler)
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def declared(monkeypatch):
    """Empty the exception list, for tests that build their own application.

    The list describes *this* application's surface. A one-route fixture app does not
    contain the branding mark, so leaving the shipped entry in place would make every
    synthetic app report a stale declaration and fail for a reason the test is not about.
    Tests that need an exception declare only the one they mean, which also keeps them from
    passing by accident on an entry somebody adds later.
    """
    monkeypatch.setattr(readiness, "CACHEABLE_API_ROUTES", {})
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. It passes on the real application
# ---------------------------------------------------------------------------
def test_every_api_route_defaults_to_no_store(app_module):
    """The gate as shipped: the whole API surface is covered, and nothing opts out."""
    check = readiness._check_api_response_cache_policy({"app": app_module.app})
    assert check.ok, (
        "SECURITY GAP: an API answer may be stored and reused rather than revalidated:\n  "
        + check.detail
    )
    assert check.tier == readiness.TIER_FATAL, "a cacheable personal answer is not a warning"
    assert check.value["default_installed"] is True, (
        "the no-store default middleware is not installed, so no response is covered"
    )
    # The point of the check is that it looked at the whole surface, not a sliver of it.
    assert check.value["routes"] > 100, (
        f"only {check.value['routes']} API routes were enumerated; the traversal is not "
        "seeing the surface it claims to verify"
    )
    assert check.value["guarded"] > 50
    assert check.value["undeclared"] == []
    assert check.value["stale_exemptions"] == []
    assert check.value["unused_exemptions"] == []


def test_the_branding_mark_is_the_only_cached_answer(app_module):
    """A cacheable exception is a deliberate, reviewed act, and there is exactly one."""
    check = readiness._check_api_response_cache_policy({"app": app_module.app})
    assert check.value["cacheable"] == ["GET /api/v1/branding/logo"], (
        "the set of cacheable API answers changed; each addition must be declared in "
        "CACHEABLE_API_ROUTES with a reason:\n  "
        + "\n  ".join(check.value["cacheable"])
    )


def test_the_gate_is_registered_with_the_other_fatal_checks():
    names = {check.__name__ for check in readiness.CHECKS}
    assert "_check_api_response_cache_policy" in names, (
        "the check exists but nothing runs it; a gate that is not in CHECKS never fires"
    )


def test_every_cacheable_exception_carries_a_reason():
    """The constant itself, so a blank entry cannot be added by hand."""
    blank = sorted(key for key, reason in readiness.CACHEABLE_API_ROUTES.items() if not reason.strip())
    assert not blank, f"declared cacheable with no reason beside it: {blank}"


# ---------------------------------------------------------------------------
# 2. It bites
# ---------------------------------------------------------------------------
def test_the_gate_fails_without_the_default_middleware(declared):
    """The load-bearing claim: with no default, every answer is heuristically cacheable."""
    check = readiness._check_api_response_cache_policy(
        {"app": _synthetic([("get", "/worker/me/ledger", _plain, True)], default_policy=False)}
    )
    assert not check.ok
    assert check.value["default_installed"] is False
    assert "heuristically cacheable" in check.detail, check.detail


def test_the_gate_names_an_undeclared_cacheable_handler(declared):
    """The whole reason the check exists: a handler that opts out and tells nobody."""
    check = readiness._check_api_response_cache_policy(
        {"app": _synthetic([("get", "/branding/logo", _cacheable, False)])}
    )
    assert not check.ok
    assert any(
        "GET /api/v1/branding/logo" in entry for entry in check.value["undeclared"]
    ), f"the undeclared cacheable handler was not named: {check.detail}"


def test_the_gate_accepts_a_declared_cacheable_handler(declared):
    declared.setattr(
        readiness, "CACHEABLE_API_ROUTES", {"GET /api/v1/branding/logo": "a public mark"}
    )
    check = readiness._check_api_response_cache_policy(
        {"app": _synthetic([("get", "/branding/logo", _cacheable, False)])}
    )
    assert check.ok, check.detail


def test_a_no_store_handler_needs_no_declaration(declared):
    """Pinning the safe direction harder is always allowed, and never needs a reason."""
    check = readiness._check_api_response_cache_policy(
        {"app": _synthetic([("get", "/worker/me/ledger", _no_store, True)])}
    )
    assert check.ok, check.detail
    assert check.value["cacheable"] == []


def test_a_private_answer_on_a_guarded_route_is_accepted(declared):
    """The requirement's own alternative: ``private`` on personal data is a valid policy.

    It keeps the answer out of shared and intermediary caches, which is the leak the gate
    exists for, so it needs no declaration even on a session-guarded route.
    """
    check = readiness._check_api_response_cache_policy(
        {"app": _synthetic([("get", "/worker/me/ledger", _private, True)])}
    )
    assert check.ok, check.detail
    assert check.value["cacheable"] == []


def test_the_gate_refuses_a_cacheable_answer_on_a_guarded_route(declared):
    """A per-account answer stored under a key the whole deployment shares.

    Declaring it is not enough - the declaration is *itself* the finding, because the
    point of the exception list is "this holds nothing personal", and a session-guarded
    route is exactly where that cannot be assumed.
    """
    declared.setattr(
        readiness, "CACHEABLE_API_ROUTES", {"GET /api/v1/worker/me/ledger": "wishful thinking"}
    )
    check = readiness._check_api_response_cache_policy(
        {"app": _synthetic([("get", "/worker/me/ledger", _cacheable, True)])}
    )
    assert not check.ok
    assert "GET /api/v1/worker/me/ledger" in check.value["guarded_but_cacheable"], check.detail


def test_a_declaration_for_a_route_that_no_longer_exists_is_a_failure(app_module, monkeypatch):
    """A stale declaration reads as "cached on purpose" for a path nothing serves."""
    monkeypatch.setattr(
        readiness,
        "CACHEABLE_API_ROUTES",
        {**readiness.CACHEABLE_API_ROUTES, "GET /api/v1/branding/wordmark": "renamed away last month"},
    )
    check = readiness._check_api_response_cache_policy({"app": app_module.app})
    assert not check.ok
    assert "GET /api/v1/branding/wordmark" in check.value["stale_exemptions"], check.detail


def test_a_declaration_whose_route_stopped_being_cacheable_is_a_failure(app_module, monkeypatch):
    """An exception that is doing nothing but misinforming the next reader."""
    monkeypatch.setattr(
        readiness,
        "CACHEABLE_API_ROUTES",
        {**readiness.CACHEABLE_API_ROUTES, "GET /api/v1/status": "someone was being cautious"},
    )
    check = readiness._check_api_response_cache_policy({"app": app_module.app})
    assert not check.ok
    assert "GET /api/v1/status" in check.value["unused_exemptions"], check.detail


def test_the_gate_fails_when_it_could_enumerate_nothing():
    """A traversal that breaks must fail the gate, not vouch for a surface it never saw."""
    check = readiness._check_api_response_cache_policy(
        {"app": FastAPI(docs_url=None, redoc_url=None, openapi_url=None)}
    )
    assert not check.ok
    assert "verified nothing" in check.detail, check.detail


# ---------------------------------------------------------------------------
# 3. The directive vocabulary is honest about what it accepts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["no-store", "no-cache, must-revalidate", "private, max-age=0", "private"],
)
def test_a_non_storable_directive_is_not_cacheable(value):
    assert readiness._cacheable_directive(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "public, max-age=31536000, immutable",
        "max-age=60",
        "s-maxage=300",
        "stale-while-revalidate=86400",
    ],
)
def test_a_storable_directive_is_cacheable(value):
    assert readiness._cacheable_directive(value) is True


def test_a_handler_header_is_read_from_both_shapes_the_codebase_uses():
    """``inspect``-based reading only works if it survives both spellings in the tree."""
    header_style = (
        'return Response(headers={"Cache-Control": "public, max-age=31536000, immutable"})'
    )
    assignment_style = 'response.headers["Cache-Control"] = "no-store"'
    assert readiness._CACHE_CONTROL_IN_SOURCE.search(header_style).group(1) == (
        "public, max-age=31536000, immutable"
    )
    assert readiness._CACHE_CONTROL_IN_SOURCE.search(assignment_style).group(1) == "no-store"
