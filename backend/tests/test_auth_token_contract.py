"""The login response, from the client's point of view.

``/auth/login`` returns the access token at the **top level**::

    {"status": "success", "user": {...}, "token": "...", "access_token": "..."}

The frontend stores ``res.user``. That mismatch is not visible from the server
side at all, and it broke the whole app in a way that looked like a server bug:
the client fell back to ``Bearer dummy``, so every authenticated call - including
clock-in - answered::

    401 {"detail": "Invalid token"}

which the worker saw as ``Attendance error: Invalid token``.

These tests pin both halves: the shape of the login response (so a future change
cannot quietly nest or rename the token) and the exact broken request, so the
symptom stays reproducible instead of a mystery.
"""

from __future__ import annotations

import json

from harness import EMAILS, MOALLEM, PASSWORDS, bearer, clock_in, db_scalar

#: Any authenticated role may read this, so a 403 from a role rule can never be
#: mistaken for an authentication failure.
AUTHENTICATED_PATH = "/api/v1/auth/me"


def _login(client, user_id: str = MOALLEM):
    return client.post(
        "/api/v1/auth/login",
        json={
            "user_id": user_id,
            "email_or_phone": EMAILS[user_id],
            "password": PASSWORDS[user_id],
        },
    )


def test_login_returns_the_token_beside_the_user_not_inside_it(client):
    response = _login(client)
    assert response.status_code == 200, response.text[:300]
    body = response.json()

    assert body["token"], "the access token is the point of logging in"
    assert body["access_token"] == body["token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert set(body["user"]) >= {"id", "name", "role"}
    assert "token" not in body["user"], (
        "the client stores res.user, so a token nested there would be missed"
    )


def test_the_token_login_returns_is_what_authenticates_the_next_request(client):
    token = _login(client).json()["token"]
    allowed = client.get(AUTHENTICATED_PATH, headers={"Authorization": f"Bearer {token}"})
    assert allowed.status_code == 200, allowed.text[:200]


def test_a_placeholder_token_fails_exactly_how_the_broken_client_failed(client):
    """The symptom, kept reproducible: this is what "Bearer dummy" earns."""
    placeholder = client.get(AUTHENTICATED_PATH, headers={"Authorization": "Bearer dummy"})
    assert placeholder.status_code == 401
    assert placeholder.json()["detail"] == "Invalid token"

    missing = client.get(AUTHENTICATED_PATH)
    assert missing.status_code == 401
    assert missing.json()["detail"] == "Not authenticated"


def test_clocking_in_with_the_login_token_works_and_with_the_placeholder_it_does_not(client):
    """The worker-visible end of it: attendance only records under a real token."""
    token = _login(client).json()["token"]

    broken = clock_in(client, MOALLEM, headers={"Authorization": "Bearer dummy"})
    assert broken.status_code == 401, broken.text[:200]
    assert json.loads(broken.text)["detail"] == "Invalid token"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)) == 0

    ok = clock_in(client, MOALLEM, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200, ok.text[:400]
    assert ok.json()["status"] in {"success", "flagged"}
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock In'", (MOALLEM,)
    ) == 1
