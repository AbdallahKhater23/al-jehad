"""Session and credential integrity across restarts and across worker processes.

These tests target the ``USER_CACHE`` design and the database bootstrap routine.
They are the ones that fail in ways an operator would never notice by hand: a
password rotation that does not actually revoke the old password, and a restart
that silently rewrites a credential.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from harness import (
    ADMIN,
    MOALLEM,
    PASSWORDS,
    WORKER,
    assert_denied,
    bearer,
    clock_in,
    db_scalar,
    load_second_app_instance,
)


@pytest.mark.security_gap
def test_startup_does_not_rewrite_an_existing_users_password(app_module):
    """``init_db()`` must be idempotent.

    Today it re-hashes a hardcoded demo password into an existing user row on
    every application start, so the operator's stored credential is replaced
    behind their back by anything that imports the module.
    """
    before = db_scalar("SELECT password_hash FROM users WHERE id = ?", (WORKER,))
    app_module.init_db()
    after = db_scalar("SELECT password_hash FROM users WHERE id = ?", (WORKER,))

    assert after == before, (
        "SECURITY GAP: importing/starting the app rewrote an existing user's password hash "
        "(the stored credential changed without any request)"
    )
    assert app_module.pwd_context.verify(PASSWORDS[WORKER], after), (
        "SECURITY GAP: after a restart the user's real password no longer verifies; the "
        "startup routine replaced it with a hardcoded default"
    )


@pytest.mark.security_gap
def test_password_change_reaches_a_second_worker_process(client, jpeg):
    """Two ASGI workers share one SQLite file but each keeps its own cache.

    A rotation performed through worker A must be visible to worker B, otherwise
    the *old* password continues to authenticate -- and clock in workers -- on
    every other worker until it is restarted.
    """
    # The subject is the moallem (600) rather than worker 1: the harness deliberately
    # seeds worker 1 *already clocked in* (and with a pending_review row), so a clock-in
    # for worker 1 can only ever answer 400 "Already clocked in!". That would make the
    # positive assertion below unsatisfiable and unrelated to what this test is about,
    # which is whether a rotation travels between worker processes.
    second_worker = load_second_app_instance()
    new_password = "rotated-pass-456"
    # The credential a punch carries is its token, so "the old credential" is a token
    # minted *before* the rotation (``bearer`` stamps the user's current ``token_version``
    # from the database at the moment it is called).
    token_before_rotation = bearer(MOALLEM)

    changed = client.post(
        "/api/v1/admin/users/edit_password",
        headers=bearer(ADMIN),
        json={"worker_id": MOALLEM, "new_password": new_password, "admin_id": ADMIN},
    )
    assert changed.status_code == 200, (
        f"the admin password change itself must work first: {changed.status_code} {changed.text[:200]}"
    )

    with TestClient(second_worker.app) as second_client:
        # No password argument: the punch stopped taking one, so the pair of claims is
        # "a token minted after the rotation works here" and "one minted before it does
        # not" - the same two a per-process cache would break, measured with the
        # credential the app actually uses.
        accepted = clock_in(second_client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
        assert accepted.status_code == 200, (
            "SECURITY GAP: a password rotated on one worker process is not honoured by a "
            "second worker process, because clock-in authenticates against a per-process "
            f"in-memory dictionary instead of the database (got {accepted.status_code}: "
            f"{accepted.text[:200]!r})"
        )

        revoked = clock_in(second_client, MOALLEM, image=jpeg, headers=token_before_rotation)
        assert revoked.status_code == 401, (
            "SECURITY GAP: a token minted before the rotation still authenticates on the "
            "second worker process, so a rotation does not revoke the old credential "
            f"(got {revoked.status_code}: {revoked.text[:200]!r})"
        )


@pytest.mark.security_gap
def test_pre_rotation_token_is_revoked_by_a_password_change(client, jpeg):
    """A token minted before the rotation must stop working afterwards.

    Without a per-user token version (or a session table) a stolen or leaked
    token survives the very remediation an operator performs after a compromise.
    """
    token_before_rotation = bearer(WORKER)

    changed = client.post(
        "/api/v1/admin/users/edit_password",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "new_password": "rotated-pass-456", "admin_id": ADMIN},
    )
    assert changed.status_code == 200, f"{changed.status_code} {changed.text[:200]}"

    after = clock_in(client, WORKER, image=jpeg, headers=token_before_rotation)
    assert after.status_code == 401, (
        "SECURITY GAP: a token issued before the password rotation still works, so "
        "rotating a password does not terminate the attacker's session"
    )


@pytest.mark.security_gap
def test_deleting_a_user_immediately_ends_their_access(app_module, client):
    """Role and existence must be re-checked per request, not cached at login."""
    import sqlite3

    from harness import DB_PATH

    token = bearer(WORKER)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM users WHERE id = ?", (WORKER,))
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/v1/worker/stats/1", headers=token)
    assert_denied(
        response,
        endpoint="GET /api/v1/worker/stats/1",
        detail="a deleted user's existing token still returns data",
    )


@pytest.mark.security_gap
def test_role_change_takes_effect_without_re_login(app_module, client):
    """A demoted admin must lose admin access on the next request."""
    import sqlite3

    from harness import DB_PATH

    admin_token = bearer(ADMIN, role="admin")
    allowed = client.get("/api/v1/admin/users", headers=admin_token)
    assert allowed.status_code == 200, "the seeded admin should be able to read users before being demoted"

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE users SET role = 'worker' WHERE id = ?", (ADMIN,))
        conn.commit()
    finally:
        conn.close()

    demoted = client.get("/api/v1/admin/users", headers=admin_token)
    assert_denied(
        demoted,
        endpoint="GET /api/v1/admin/users",
        detail="a demoted admin's stale token still grants admin access",
    )
