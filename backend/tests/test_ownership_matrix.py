"""Object-level ownership: an id is not a permission.

The audience matrix (``test_role_audience.py``) settles which **roles** may reach a route. It
is blind to the layer underneath: a route that any worker may call, which takes an id, where
the id decides *whose* record is read or written. "May I call this endpoint" and "may I have
this record" are different questions, and a role guard only ever answers the first - which is
how an endpoint ships with the right guard and still lets one worker read a colleague's
attachment, hours or note by counting upwards from 1.

Two halves, because the property has two ways to fail:

* **structural** - every route whose path carries somebody's record is accounted for. Three
  legitimate answers exist and each is declared here: the audience is administrators only (so
  there is no cross-worker case to have), the path is a *credential* rather than an id (an
  invite or quick-link token, hashed at rest, single-use and revocable - guessing one is the
  attack they are built against), or it is listed with the mechanism that decides the owner.
  A new worker-reachable id route cannot ship unlisted.
* **live** - a real second session, aiming at a real record that belongs to somebody else.
  The refusal is asserted, **and the record is asserted unmoved**, because a 404 that still
  wrote the row is not a refusal. The owner's own access is asserted alongside it, so an
  endpoint that refuses everybody cannot pass this file.

There is a third form of the same mistake that a path-based list cannot see, so it is probed
directly: an id smuggled in through a **query string** or a **form field** on a self-scoped
route. Those routes carry no id to tamper with, which is only worth anything if a tampered id
is actually ignored - so each one is asked twice, once with a colleague's ``worker_id`` and
once without, and the two answers must be identical.

``test_worker_notes.py`` covers the notes half of this in depth (internal messages, the unread
counters, the reopen notification). This file holds the matrix-level probe, so that the list
of routes is complete in one place.
"""

from __future__ import annotations

import readiness
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, bearer, db_scalar
from security import DEVELOPER_ROLE

ADMIN_ROLES = ("admin", "head_admin")

#: Routes whose path carries somebody's record, and the one mechanism that decides whether the
#: caller may have it. Nothing here is derived from the guards - a table read back out of
#: ``_allowed_roles`` would agree with itself however wrong the ownership check was.
OWNERSHIP_CHECKED: dict[str, str] = {
    "GET /api/v1/worker/notes/{note_id}": (
        "notes._fetch_note(conn, note_id, worker_id=current.id): the owner is part of the "
        "WHERE clause, so somebody else's note is the same 404 as a note that does not exist"
    ),
    "POST /api/v1/worker/notes/{note_id}/replies": (
        "the same lookup before the insert - a reply is written into a thread only the "
        "owner can address"
    ),
    "POST /api/v1/worker/notes/{note_id}/close": "the same lookup before the status change",
    "POST /api/v1/attendance/devices/{device_id}/revoke": (
        "offline_sync._device_lookup(conn, current.id, device_id): a device is looked up "
        "*within* the caller's own devices, so a colleague's cannot be named"
    ),
}

#: Paths where the token is the permission rather than an id: one invite, one one-tap link.
#: Each is stored as a hash, dies on use or revocation, and expires - so a guessed token is
#: the whole attack model they already answer to, and there is no "owner" to check against.
TOKEN_SCOPED_ROUTES = frozenset(
    {
        "GET /api/v1/enroll/{token}",
        "POST /api/v1/enroll/{token}",
        "POST /api/v1/enroll/{token}/register",
        "GET /api/v1/q/{token}",
        "POST /api/v1/q/{token}",
        "GET /enroll/{token}",
        "GET /q/{token}",
    }
)

#: Self-scoped GETs - routes with no id in them at all. Called with a colleague's
#: ``worker_id`` appended to the query string, each must answer exactly as it does without it.
SELF_SCOPED_GETS = (
    "/api/v1/worker/me/logs",
    "/api/v1/worker/me/stats",
    "/api/v1/worker/me/report",
    "/api/v1/worker/me/notifications",
)


def _id_routes(app_module):
    """Every ``(method, path, roles)`` whose path carries a parameter."""
    for path, route in readiness.iter_api_routes(app_module.app):
        if "{" not in path:
            continue
        roles: tuple[str, ...] = ()
        for dependency in route.dependant.dependencies:
            if getattr(dependency.call, "_auth_marker", None) == "require_role":
                roles = tuple(sorted(getattr(dependency.call, "_allowed_roles", ())))
        for method in sorted(route.methods or []):
            yield method, path, roles


def _open_note(client, user_id: str) -> int:
    """Open a note as ``user_id`` and return its id."""
    response = client.post(
        "/api/v1/worker/notes",
        headers=bearer(user_id),
        json={
            "category": "other",
            "subject": "Ownership probe",
            "body": "Filed by the ownership matrix.",
            "priority": "normal",
        },
    )
    assert response.status_code == 200, response.text[:300]
    return int(response.json()["note"]["id"])


def _register_device(client, user_id: str, device_id: str) -> None:
    response = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(user_id),
        json={"device_id": device_id, "note": "ownership matrix"},
    )
    assert response.status_code == 200, response.text[:300]


def _my_devices(client, user_id: str) -> dict:
    response = client.get("/api/v1/attendance/devices", headers=bearer(user_id))
    assert response.status_code == 200, response.text[:300]
    return {row["device_id"]: row for row in response.json()}


# ---------------------------------------------------------------------------
# 1. Structural: every id-bearing route is accounted for
# ---------------------------------------------------------------------------
def test_every_id_bearing_route_is_accounted_for(app_module):
    """A route that takes somebody's id must say how the caller is allowed to have it."""
    offenders = []
    for method, path, roles in _id_routes(app_module):
        if set(roles) <= set(ADMIN_ROLES) | {DEVELOPER_ROLE}:
            # Administrators may read every record by design - that is the console - and the
            # root tier is the same case rather than a bigger one: nobody else can call a
            # developer route at all, which the audience matrix proves separately. Neither
            # audience has a cross-worker case to have, so there is no owner to check.
            continue
        key = f"{method} {path}"
        if key in TOKEN_SCOPED_ROUTES or key in OWNERSHIP_CHECKED:
            continue
        offenders.append(f"  {key} (reachable by {sorted(roles)})")
    assert not offenders, (
        "SCOPE GAP: these routes take a record id and are reachable by a non-administrator, "
        "and nothing here says who is allowed to have that record:\n" + "\n".join(offenders)
    )


def test_the_matrix_lists_only_routes_that_exist(app_module):
    """A stale entry would vouch for a route nothing serves any more."""
    live = {f"{method} {path}" for method, path, _ in _id_routes(app_module)}
    stale = sorted((set(OWNERSHIP_CHECKED) | TOKEN_SCOPED_ROUTES) - live)
    assert not stale, (
        f"these entries name routes that no longer exist, so they check nothing: {stale}"
    )


def test_the_worker_reachable_half_is_what_is_swept(app_module):
    """The live probes below must be reaching real routes, not an empty list."""
    swept = sorted(
        key for key in OWNERSHIP_CHECKED if not key.endswith("{token}") and key in {
            f"{method} {path}" for method, path, _ in _id_routes(app_module)
        }
    )
    assert len(swept) == len(OWNERSHIP_CHECKED), (
        f"only {len(swept)} of {len(OWNERSHIP_CHECKED)} declared routes are reachable in the "
        "application; the sweep is not covering what the table claims"
    )


# ---------------------------------------------------------------------------
# 2. Live: a colleague's id is not a permission
# ---------------------------------------------------------------------------
def test_one_worker_cannot_read_another_workers_note(client):
    """The read half, with the owner's own access as the control."""
    mine = _open_note(client, MOALLEM)

    theirs = client.get(f"/api/v1/worker/notes/{mine}", headers=bearer(WORKER))
    assert theirs.status_code == 404, (
        f"a worker read a colleague's note (HTTP {theirs.status_code}); 'not yours' and 'not "
        "there' have to be the same answer or the endpoint enumerates real ids"
    )
    # ...and it is not merely absent from the response: it is not in their list either.
    listed = client.get("/api/v1/worker/notes", headers=bearer(WORKER)).json()
    assert all(row["id"] != mine for row in listed["notes"]), (
        "a colleague's note appeared in the listing that carries the caller's token"
    )

    # The control: the owner is not refused their own note, so a blanket deny cannot pass.
    owner = client.get(f"/api/v1/worker/notes/{mine}", headers=bearer(MOALLEM))
    assert owner.status_code == 200, owner.text[:200]


def test_one_worker_cannot_write_into_another_workers_thread(client):
    """A refusal that still wrote the row is not a refusal, so the row is checked."""
    mine = _open_note(client, MOALLEM)
    before = (
        db_scalar("SELECT status FROM worker_notes WHERE id = ?", (mine,)),
        db_scalar("SELECT COUNT(*) FROM worker_note_messages WHERE note_id = ?", (mine,)),
    )

    assert client.post(
        f"/api/v1/worker/notes/{mine}/replies",
        headers=bearer(WORKER),
        json={"body": "and another thing"},
    ).status_code == 404
    assert client.post(
        f"/api/v1/worker/notes/{mine}/close", headers=bearer(WORKER)
    ).status_code == 404

    after = (
        db_scalar("SELECT status FROM worker_notes WHERE id = ?", (mine,)),
        db_scalar("SELECT COUNT(*) FROM worker_note_messages WHERE note_id = ?", (mine,)),
    )
    assert after == before, (
        f"the refused writes changed the note anyway: {before} -> {after}"
    )


def test_one_worker_cannot_revoke_another_workers_device(client):
    """A signing device is how a colleague's offline punches are trusted.

    Revoking somebody else's is the quiet one: it bumps their key epoch, so every punch their
    phone has queued stops verifying - and the worker finds out at the gate, not here. This
    case had no test before the matrix.
    """
    device = "ownership-probe-device"
    _register_device(client, MOALLEM, device)

    # The read half first: it is not in the other worker's list at all.
    assert device not in _my_devices(client, WORKER), (
        "a colleague's signing device was listed to another worker"
    )

    refused = client.post(
        f"/api/v1/attendance/devices/{device}/revoke", headers=bearer(WORKER)
    )
    assert refused.status_code == 404, (
        f"a worker reached a colleague's device (HTTP {refused.status_code})"
    )

    held = _my_devices(client, MOALLEM)[device]
    assert held["revoked_at"] is None, (
        "the refused revoke bumped the key epoch anyway, so the owner's queued punches would "
        f"stop verifying: {held}"
    )
    assert int(held["key_epoch"]) == 1, f"the refused revoke rotated the key: {held}"

    # The control: the owner can revoke their own device.
    assert client.post(
        f"/api/v1/attendance/devices/{device}/revoke", headers=bearer(MOALLEM)
    ).status_code == 200
    assert _my_devices(client, MOALLEM)[device]["revoked_at"] is not None


def test_a_self_scoped_route_ignores_a_colleague_s_worker_id(client):
    """An id smuggled in through the query string changes nothing.

    These routes have no id in their path, which is only worth something if one appended
    anyway is *ignored* rather than quietly honoured by a handler that reads it from the
    wrong place. Asserted as an equality between the two answers rather than against the
    payload's shape, so it cannot start passing because a response grew a field.
    """
    checked = 0
    for path in SELF_SCOPED_GETS:
        plain = client.get(path, headers=bearer(WORKER))
        tampered = client.get(f"{path}?worker_id={MOALLEM}", headers=bearer(WORKER))
        assert plain.status_code == 200, f"{path} -> {plain.status_code} {plain.text[:200]}"
        assert tampered.status_code == plain.status_code, (
            f"{path} answered {tampered.status_code} with a colleague's worker_id and "
            f"{plain.status_code} without it"
        )
        assert tampered.json() == plain.json(), (
            f"SCOPE GAP: {path} answered differently when handed worker_id={MOALLEM}, so the "
            "id is being read from the request and not from the token"
        )
        checked += 1
    assert checked == len(SELF_SCOPED_GETS), "the sweep did not run"


def test_attendance_verify_refuses_to_punch_for_somebody_else(client, jpeg):
    """The classic form-field id: the punch carries a ``worker_id`` and must be ignored.

    Sent with a *complete* form rather than a bodiless one, because this refusal lives in the
    handler (the endpoint cannot take a role guard - every role may punch) rather than in a
    dependency, so a bare request would answer 422 and prove nothing about the identity check.
    """
    before = db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)
    )
    response = client.post(
        "/api/v1/attendance/verify",
        headers=bearer(WORKER),
        data={"worker_id": MOALLEM, "action": "Clock In", "location_input": "30.06,31.24"},
        files={"selfie": ("probe.jpg", jpeg, "image/jpeg")},
    )
    assert response.status_code in (401, 403), (
        f"a worker punched on a colleague's account (HTTP {response.status_code}): "
        f"{response.text[:200]}"
    )
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)
    ) == before, "the refused punch wrote an attendance row for the other worker anyway"
