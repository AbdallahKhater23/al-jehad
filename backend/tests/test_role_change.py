"""Changing an existing account's role: ``POST /admin/users/role``.

WHY THIS EXISTS
---------------
The role used to be settable exactly once, when the account was created - and
``UserEditRequest`` said so on purpose: a promotion "is a new account", because the hours
belong to whoever worked them and a second account cannot inherit them. The reasoning was
sound about the history and it left the application with no way to express a promotion at
all: the console's answer to "this worker runs the crew now" was to create a second
account, and the first one went on holding the attendance that was actually worked.

So this endpoint exists, and the rules around it are the interesting part:

1. **The account survives.** The id is the subject of every attendance row, punch, device
   key and audit entry, so a change of role is a change of *role* - nothing else moves, and
   no second account appears.
2. **Authority mirrors account creation.** A standard admin may move an account among the
   tiers they may create in (``worker``, ``moallem``, ``off_office``) and no further. Only
   a head admin (or the root tier) may hand out ``admin``/``head_admin`` - and a standard
   admin may not touch an account that already holds one, which is the rule
   ``_guard_standard_admin`` applies to every other act on a peer's record.
3. **``developer`` is not a role anybody assigns.** Not as a new role, from any caller
   including the root tier, and not against the root account either - an administrator who
   could grant it could grant it to themselves, and one who could revoke it could remove
   the account that reads the audit trail. That is the same escalation from both ends.
4. **The session does not survive the widening.** A token minted while the account held the
   wider role must stop working, or the promotion is a privilege change that takes effect
   whenever the holder chooses to sign out. It is revoked the way every other credential
   revocation here works: ``token_version`` is bumped and the stale token fails its claim.
5. **A no-op is not a revocation.** Setting the role an account already has signs nobody out
   and writes nothing - a double click must not be indistinguishable from a real change.
"""

from __future__ import annotations

import json
import sqlite3

import developer
import harness
import pytest
import security
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    OFF_OFFICE,
    PASSWORDS,
    ROOT_PASSWORD,
    WORKER,
    assert_denied,
    bearer,
    db_scalar,
    token_version,
)

ROLE_ENDPOINT = "/api/v1/admin/users/role"


def change(client, *, user_id, role, as_role=HEAD_ADMIN, headers=None):
    return client.post(
        ROLE_ENDPOINT,
        headers=headers or bearer(as_role),
        json={"user_id": user_id, "role": role},
    )


def _role(user_id: str) -> str | None:
    return db_scalar("SELECT role FROM users WHERE id = ?", (user_id,))


def _write(sql: str, params: tuple = ()) -> None:
    """Write straight to the database, the way a suite seeds a state an endpoint refuses.

    An assignment that an endpoint would refuse to create is exactly the state worth
    changing a role *out of*, so it is planted here rather than reached through the API.
    """
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _role_change_rows(user_id: str) -> list[tuple]:
    return harness.db_rows(
        "SELECT actor_id, actor_role, before_json, after_json FROM audit_log "
        "WHERE action = 'role_change' AND entity_id = ?",
        (user_id,),
    )


def _developer_headers() -> dict[str, str]:
    developer.seed_developer_account(password=ROOT_PASSWORD, actor="test:role_change")
    return bearer(developer.DEVELOPER_ID_DEFAULT, role=security.DEVELOPER_ROLE)


def _seen_from(user_id: str, token: dict[str, str], client) -> str | None:
    """The role the *application* reads for this token, through ``/auth/me``."""
    response = client.get("/api/v1/auth/me", headers=token)
    if response.status_code != 200:
        return None
    return response.json()["role"]


# ---------------------------------------------------------------------------
# 1. The account, its history and its credential all survive the change
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_promotion_changes_the_role_and_keeps_the_account(client):
    shifts = db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,))
    name = db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,))
    credential = db_scalar("SELECT password_hash FROM users WHERE id = ?", (WORKER,))

    response = change(client, user_id=WORKER, role="moallem")

    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["previous_role"] == "worker", body
    assert body["role"] == "moallem", body
    assert _role(WORKER) == "moallem", "the role was not actually stored"

    # Everything the id is the subject of stays with the id.
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)) == shifts
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == name
    assert db_scalar("SELECT password_hash FROM users WHERE id = ?", (WORKER,)) == credential, (
        "a change of role must not touch the account's credential"
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1, (
        "a promotion must not create a second account"
    )


@pytest.mark.regression
def test_the_change_is_audited_with_both_roles_and_no_secret(client):
    assert change(client, user_id=WORKER, role="moallem").status_code == 200

    rows = _role_change_rows(WORKER)
    assert len(rows) == 1, f"expected one role_change row, got {len(rows)}"
    actor_id, actor_role, before_json, after_json = rows[0]
    assert actor_id == HEAD_ADMIN, "the trail must name who widened the account"
    assert actor_role == "head_admin"

    before, after = json.loads(before_json), json.loads(after_json)
    assert before["role"] == "worker", before
    assert after["role"] == "moallem", after
    # ``audit_log`` is append-only, so whatever lands there is there for good: a credential
    # in it would be a password with unlimited lifetime.
    assert "password" not in json.dumps(after).lower(), after
    assert PASSWORDS[WORKER] not in before_json and PASSWORDS[WORKER] not in after_json


# ---------------------------------------------------------------------------
# 2. The old session dies with the old role
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_token_minted_before_the_change_stops_working(client):
    stale = bearer(WORKER, role="worker")
    assert _seen_from(WORKER, stale, client) == "worker", "the token must work beforehand"
    version = token_version(WORKER)

    assert change(client, user_id=WORKER, role="moallem").status_code == 200

    assert token_version(WORKER) == version + 1, (
        "the role changed without revoking the sessions issued under the old one"
    )
    assert client.get("/api/v1/auth/me", headers=stale).status_code == 401, (
        "a token minted while the account was a worker still authenticates after the promotion"
    )

    # ...and a session issued after the change reads the role the account actually has.
    fresh = bearer(WORKER, role="moallem")
    assert _seen_from(WORKER, fresh, client) == "moallem"


# ---------------------------------------------------------------------------
# 3. Who may widen whom
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_standard_admin_may_move_an_account_among_the_tiers_they_may_create(client):
    for start, wanted in ((WORKER, "moallem"), (WORKER, "off_office"), (OFF_OFFICE, "moallem")):
        _write("UPDATE users SET role = ? WHERE id = ?", (start, WORKER if start == WORKER else OFF_OFFICE))
        subject = WORKER if start == WORKER else OFF_OFFICE
        response = change(client, user_id=subject, role=wanted, as_role=ADMIN)
        assert response.status_code == 200, f"{start} -> {wanted}: {response.text[:200]}"
        assert _role(subject) == wanted


@pytest.mark.regression
def test_a_standard_admin_cannot_grant_an_administrator_role(client, app_module):
    for wanted in ("admin", "head_admin"):
        response = change(client, user_id=WORKER, role=wanted, as_role=ADMIN)
        assert_denied(
            response,
            endpoint=ROLE_ENDPOINT,
            detail=f"a standard admin granted the {wanted} role",
        )
    assert _role(WORKER) == "worker", "the refusal still moved the account"
    assert _role_change_rows(WORKER) == [], "a refused change left an audit row claiming it happened"


#: A second standard admin, so "a standard admin may not act on an administrator's account"
#: can be asserted without the actor turning out to be the target - which is a different rule
#: (``_refuse_self_account``) and fires first.
PEER_ADMIN = "4001"


def _create_peer_admin(client) -> None:
    created = client.post(
        "/api/v1/admin/admins/add",
        headers=bearer(HEAD_ADMIN),
        json={
            "user_id": PEER_ADMIN,
            "name": "Peer Admin",
            "role": "admin",
            "password": "peer-admin-credential-01",
        },
    )
    assert created.status_code == 200, created.text[:300]
    assert _role(PEER_ADMIN) == "admin"


@pytest.mark.regression
def test_a_standard_admin_cannot_change_an_administrators_role(client):
    """The peer rule, in the direction the create endpoints do not have to think about."""
    _create_peer_admin(client)

    for subject, was in ((HEAD_ADMIN, "head_admin"), (PEER_ADMIN, "admin")):
        for wanted in ("worker", "moallem"):
            response = change(client, user_id=subject, role=wanted, as_role=ADMIN)
            assert_denied(
                response,
                endpoint=ROLE_ENDPOINT,
                detail=f"a standard admin moved a {was} account to {wanted}",
            )
        assert _role(subject) == was


@pytest.mark.regression
def test_a_head_admin_may_promote_into_and_out_of_the_admin_tier(client):
    assert change(client, user_id=WORKER, role="admin").status_code == 200
    assert _role(WORKER) == "admin"

    assert change(client, user_id=WORKER, role="moallem").status_code == 200
    assert _role(WORKER) == "moallem"


@pytest.mark.regression
def test_the_root_tier_is_neither_grantable_nor_revocable(client):
    """Both ends of the same escalation, refused before anything is written."""
    grant = change(client, user_id=WORKER, role="developer")
    assert grant.status_code == 403, grant.text[:200]
    assert _role(WORKER) == "worker"

    headers = _developer_headers()
    root_id = developer.DEVELOPER_ID_DEFAULT
    assert _role(root_id) == security.DEVELOPER_ROLE

    revoke = change(client, user_id=root_id, role="worker")
    assert revoke.status_code == 403, revoke.text[:200]
    assert _role(root_id) == security.DEVELOPER_ROLE, "the root account was demoted"

    # The root tier may not mint itself either: the role is provisioned by the seed tool.
    as_root = change(client, user_id=WORKER, role="developer", headers=headers)
    assert as_root.status_code == 403, as_root.text[:200]
    assert _role(WORKER) == "worker"


# ---------------------------------------------------------------------------
# 4. Refusals that must not half-happen
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_an_undefined_role_is_refused_and_nothing_is_written(client):
    response = change(client, user_id=WORKER, role="supervisor")
    assert response.status_code == 400, response.text[:200]
    assert _role(WORKER) == "worker"
    assert _role_change_rows(WORKER) == []


@pytest.mark.regression
def test_changing_your_own_role_is_refused(client):
    """Demoting yourself out of the console mid-session is the lockout, not a feature.

    Refused for the *actor's own* account whatever the new role is, and before the peer rule
    is consulted - so it is the same answer for the head admin, who is otherwise allowed to
    move any account in the deployment.
    """
    for actor in (HEAD_ADMIN, ADMIN):
        for wanted in ("worker", "head_admin"):
            response = change(client, user_id=actor, role=wanted, as_role=actor)
            assert response.status_code == 400, response.text[:200]
            assert "signed in with" in response.json()["detail"], response.json()
        assert _role(actor) == ("head_admin" if actor == HEAD_ADMIN else "admin")


@pytest.mark.regression
def test_an_unknown_account_is_a_404(client):
    assert change(client, user_id="424242", role="worker").status_code == 404


@pytest.mark.regression
def test_a_no_op_change_signs_nobody_out_and_writes_nothing(client):
    version = token_version(WORKER)
    response = change(client, user_id=WORKER, role="worker")

    assert response.status_code == 200, response.text[:200]
    assert response.json()["previous_role"] == "worker"
    assert token_version(WORKER) == version, "a no-op change revoked the account's sessions"
    assert _role_change_rows(WORKER) == [], "a no-op change was recorded as a change"


@pytest.mark.regression
def test_only_an_administrator_can_change_a_role(client):
    anonymous = client.post(ROLE_ENDPOINT, json={"user_id": WORKER, "role": "moallem"})
    assert_denied(anonymous, endpoint=ROLE_ENDPOINT, detail="an anonymous caller changed a role")

    for actor in (WORKER, MOALLEM, OFF_OFFICE):
        response = change(client, user_id=WORKER, role="moallem", as_role=actor)
        assert_denied(
            response,
            endpoint=ROLE_ENDPOINT,
            detail=f"a {harness.ROLES[actor]} changed a role",
        )
    assert _role(WORKER) == "worker"


# ---------------------------------------------------------------------------
# 5. The assignment rule, on both sides of the change
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_an_account_moved_out_of_worker_stops_carrying_an_assignment(client):
    """Only a worker reports to a moallem (``_moallem_assignment``)."""
    _write("UPDATE users SET moallem_id = ? WHERE id = ?", (MOALLEM, WORKER))

    response = change(client, user_id=WORKER, role="off_office")

    assert response.status_code == 200, response.text[:300]
    assert response.json()["assignment_cleared"] is True
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (WORKER,)) is None
    after = json.loads(_role_change_rows(WORKER)[0][3])
    assert after["moallem_id"] is None, "the trail shows an assignment the account can no longer hold"


@pytest.mark.regression
def test_a_moallem_who_stops_being_one_stops_supervising(client):
    """The crew is released, and the ids are named in the trail - as ``delete_user`` does."""
    _write("UPDATE users SET moallem_id = ? WHERE id = ?", (MOALLEM, WORKER))

    response = change(client, user_id=MOALLEM, role="worker")

    assert response.status_code == 200, response.text[:300]
    assert response.json()["workers_released"] == [WORKER], response.json()
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (WORKER,)) is None
    after = json.loads(_role_change_rows(MOALLEM)[0][3])
    assert after["workers_released"] == [WORKER], after


@pytest.mark.regression
def test_a_worker_keeps_its_assignment_when_it_stays_a_worker(client):
    """A change *between* roles a worker may hold must not quietly unassign them."""
    _write("UPDATE users SET moallem_id = ? WHERE id = ?", (MOALLEM, WORKER))

    assert change(client, user_id=WORKER, role="worker").status_code == 200
    assert db_scalar("SELECT moallem_id FROM users WHERE id = ?", (WORKER,)) == MOALLEM
