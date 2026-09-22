"""The audience matrix: every guarded route, and which roles are meant to reach it.

WHY THIS SUITE EXISTS
---------------------
``test_api_route_authorisation.py`` proves every route *has* an authorization decision.
It cannot tell whether the decision is the **right** one. A route guarded by
``any_authenticated`` satisfies that gate perfectly - even when the route is an
administrator's. The gap that leaves is the one that costs the most: a payroll, review or
account endpoint that any worker can call because whoever wrote it reached for the
convenient guard instead of the correct one. Nothing in the codebase caught that, and
nothing structurally *could* - a guard cannot be wrong against itself.

So this suite states the audience **independently of the guards**, and then holds the guards
to it. ``_intended_roles()`` derives the audience from the shape of the API - what a path
*is*, not what it currently declares - with a short list of documented exceptions where a
path's audience is deliberately not what its prefix implies. That independence is the whole
point: a table read back out of ``_allowed_roles`` would agree with itself however wrong the
guards were.

Two halves:

* **static** - every guarded route's declared roles must equal its intended audience. Not a
  subset alone: a route that is *narrower* than it should be is a real defect too (an
  administrator who cannot reach the screen built for them, refused with a 403 nobody reads),
  and it is exactly how ``/worker/me/enroll`` shipped with its role list wrong for a while.
* **live** - a request from a role outside the audience must be refused, and one from
  nobody at all must be too. These are sent **with no body**, which is not a shortcut:
  FastAPI resolves dependencies before it validates a request, so the refusal is decided from
  the credential alone and the sweep cannot write anything.
"""

from __future__ import annotations

import re

import readiness
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer
from security import DEVELOPER_ROLE

#: The audience is expressed in **role names**, because that is what a guard compares and
#: therefore what ``_allowed_roles`` holds. A live probe needs a *session*, which needs an
#: account id - a different thing, and mixing the two silently is how a sweep ends up
#: presenting tokens for accounts that do not exist and reporting a clean pass.
ROLE_ACCOUNTS: dict[str, str] = {
    "worker": WORKER,
    "moallem": MOALLEM,
    "admin": ADMIN,
    "head_admin": HEAD_ADMIN,
}

#: Ordered for readable failure messages.
ALL_ROLES = tuple(ROLE_ACCOUNTS)
ADMIN_ROLES = ("admin", "head_admin")

#: Paths whose audience is not what their prefix implies. Every entry is a decision already
#: recorded in the code, named here so it cannot be satisfied by quietly widening a guard:
#: this suite fails if one of these lists changes without somebody editing this table too.
AUDIENCE_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    # Only a head administrator may create another administrator.
    "/api/v1/admin/admins/add": ("head_admin",),
    # Describes the deployment's own defences, so it is not for every signed-in account.
    "/api/v1/status/detail": ADMIN_ROLES,
    # Another worker's monthly hours: the one read under /worker/ that is an admin's.
    "/api/v1/worker/stats/{worker_id}": ADMIN_ROLES,
    # Deliberately admin-only rather than every administrator: a head_admin owns the
    # deployment rather than a rota, so there is no punch card and no template to need
    # (see ``SELF_ENROLL_ROLES`` in main.py).
    "/api/v1/worker/me/enroll": ("admin",),
}

_PARAM = re.compile(r"\{[^}]*\}")


def _intended_roles(path: str) -> tuple[str, ...]:
    """The audience a path's *shape* implies, before any guard is consulted."""
    if path in AUDIENCE_EXCEPTIONS:
        return AUDIENCE_EXCEPTIONS[path]
    if path.startswith("/api/v1/admin/"):
        return ADMIN_ROLES
    # The root tier's own surface. Its audience is expressed as one role and not as "every
    # administrator": a developer route is built from ``require_developer``, and the wildcard
    # that lets the root tier reach an administrator route deliberately does not run the other
    # way, so an administrator reaching one of these would be the separation failing.
    #
    # ``ALL_ROLES`` holds no developer account, so the live half of this suite probes these
    # routes as worker/moallem/admin/head_admin and requires the refusal, which is the half
    # that matters here. That a *developer* session is served is asserted in
    # ``test_developer_role.py``, where a root account is seeded for it.
    if path.startswith("/api/v1/developer/"):
        return (DEVELOPER_ROLE,)
    return ALL_ROLES


def _guarded_routes(app_module):
    """Every ``(method, path, declared_roles)`` the application actually serves.

    ``declared_roles`` comes from the guard's own ``_allowed_roles`` marker, which is the
    thing under test - the intended audience above is not.
    """
    for path, route in readiness.iter_api_routes(app_module.app):
        declared = None
        for dependency in route.dependant.dependencies:
            if getattr(dependency.call, "_auth_marker", None) == "require_role":
                declared = tuple(sorted(getattr(dependency.call, "_allowed_roles", ())))
        if declared is None:
            continue
        for method in sorted(route.methods or []):
            yield method, path, declared


def _concrete(path: str) -> str:
    """A path parameter resolved to something harmless.

    The value never matters: the refusal is decided by the guard, which runs before the
    parameter is validated. This only has to produce a path the router can match.
    """
    return _PARAM.sub("1", path)


# ---------------------------------------------------------------------------
# 1. Static: the guards match the stated audience
# ---------------------------------------------------------------------------
def test_the_matrix_actually_covers_the_api(app_module):
    """Guards against a traversal that silently stops finding routes."""
    routes = list(_guarded_routes(app_module))
    assert len(routes) > 50, (
        f"only {len(routes)} guarded routes were enumerated; the sweep is not seeing the "
        "surface it claims to verify"
    )


def test_no_guarded_route_is_broader_than_its_audience(app_module):
    """The finding this suite exists for: ``any_authenticated`` standing in for ``admin_only``.

    An admin route guarded by "any signed-in account" passes every other test in the repo -
    it is guarded, and the guard works. It is still wrong, and it is wrong in the direction
    that matters.
    """
    offenders = []
    for method, path, declared in _guarded_routes(app_module):
        intended = set(_intended_roles(path))
        strangers = sorted(set(declared) - intended)
        if strangers:
            offenders.append(
                f"  {method:<5} {path:<52} reaches {strangers} - audience is "
                f"{sorted(intended)}"
            )
    assert not offenders, (
        "SECURITY GAP: these routes admit a role their own path says they must not:\n"
        + "\n".join(offenders)
    )


def test_no_guarded_route_is_narrower_than_its_audience(app_module):
    """The other direction, which is a usability defect rather than a security one.

    A route that is *narrower* than its path implies refuses somebody it was built for.
    That is how a 403 nobody reads gets shipped: the guard was correct when written, the
    route was later given to another role, and the list was never updated.
    """
    offenders = []
    for method, path, declared in _guarded_routes(app_module):
        intended = set(_intended_roles(path))
        missing = sorted(intended - set(declared))
        if missing:
            offenders.append(
                f"  {method:<5} {path:<52} refuses {missing} - audience is "
                f"{sorted(intended)}"
            )
    assert not offenders, (
        "these routes refuse a role their own path says they serve:\n" + "\n".join(offenders)
    )


def test_every_audience_exception_names_a_route_that_exists(app_module):
    """A stale exception would silently widen the audience of a path by name."""
    live = {path for _, path, _ in _guarded_routes(app_module)}
    stale = sorted(set(AUDIENCE_EXCEPTIONS) - live)
    assert not stale, (
        "these audience exceptions name routes that are no longer guarded or no longer "
        f"exist, so they widen nothing and hide the drift: {stale}"
    )


def test_the_admin_surface_is_exactly_the_admin_roles(app_module):
    """A direct statement of the invariant, independent of the per-route comparison."""
    wrong = []
    for method, path, declared in _guarded_routes(app_module):
        if not path.startswith("/api/v1/admin/"):
            continue
        if path in AUDIENCE_EXCEPTIONS:
            continue
        if set(declared) != set(ADMIN_ROLES):
            wrong.append(f"  {method:<5} {path:<52} declares {sorted(declared)}")
    assert not wrong, (
        "SECURITY GAP: admin routes that do not declare exactly the administrator roles:\n"
        + "\n".join(wrong)
    )


# ---------------------------------------------------------------------------
# 2. Live: the refusal is real, decided from the credential
# ---------------------------------------------------------------------------
def _refusals(client, method: str, path: str, headers=None) -> int:
    """The status of one bodiless request. Returns it so the caller can assert on it."""
    return client.request(method, path, headers=headers).status_code


def test_no_guarded_route_serves_an_anonymous_caller(app_module, client):
    """Every route with a guard is refused when there is no credential at all.

    Sent with no body: dependencies are resolved before request validation, so a 403 here
    is decided from the credential rather than from a malformed payload - and so this sweep
    cannot write anything to the database.
    """
    served = []
    for method, path, _ in _guarded_routes(app_module):
        status = _refusals(client, method, _concrete(path))
        if status not in (401, 403):
            served.append(f"  {method:<5} {_concrete(path):<52} -> HTTP {status}")
    assert not served, (
        "SECURITY GAP: these guarded routes answered an ANONYMOUS caller (no Authorization "
        "header at all):\n" + "\n".join(served)
    )


def test_a_role_outside_the_audience_is_refused(app_module, client):
    """A worker's own valid token must not open an administrator's route.

    This is the live half of ``test_no_guarded_route_is_broader_than_its_audience``: the
    static half reads the guard's declaration, this one presents a real session and watches
    what the application actually does with it.
    """
    served = []
    checked = 0
    for method, path, _ in _guarded_routes(app_module):
        intended = set(_intended_roles(path))
        for role in ALL_ROLES:
            if role in intended:
                continue
            checked += 1
            status = _refusals(
                client, method, _concrete(path), headers=bearer(ROLE_ACCOUNTS[role])
            )
            if status not in (401, 403):
                served.append(
                    f"  {role:<10} {method:<5} {_concrete(path):<52} -> HTTP {status}"
                )
    assert checked > 50, (
        f"only {checked} out-of-audience combinations were exercised; the matrix is not "
        "being applied"
    )
    assert not served, (
        "SECURITY GAP: these routes were reached by a role their path says they must not "
        "admit:\n" + "\n".join(served)
    )


def test_a_self_scoped_route_still_serves_its_owner(app_module, client):
    """The sweep's control: a route in everyone's audience must not refuse its own worker.

    Without this, a sweep that refused everything would look like a pass - a guard that
    denies all four roles satisfies every assertion above.
    """
    status = _refusals(
        client, "GET", "/api/v1/worker/me/stats", headers=bearer(ROLE_ACCOUNTS["worker"])
    )
    assert status == 200, (
        f"a worker was refused their own monthly totals (HTTP {status}); the audience "
        "matrix is refusing too much to be meaningful"
    )
