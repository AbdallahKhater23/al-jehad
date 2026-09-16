"""The account roster behind the admin Credentials tab, and its one hard rule.

The tab lists accounts and sets passwords. Two of its claims can only be checked here:

1. the roster reports *state* - account status, whether a face reference is on file,
   whether a password exists, when it last changed, how many sessions a reset killed;
2. it never reports a credential. No ``password_hash``, and no password value, for any
   role, in any form. The screen says "Set" or "Never set" because that is all that
   exists to say: the hash is one-way, and a hash in a JSON response is a credential
   handed to whoever can read that response.
"""

from __future__ import annotations

import re

import harness
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    PASSWORDS,
    WORKER,
    assert_denied,
    bearer,
    db_scalar,
)

ROSTER_FIELDS = {
    "id", "name", "email", "phone", "role", "status",
    "face_enrolled", "enrolled_at", "password_set", "password_changed_at",
    "sessions_revoked",
}


def roster(client, headers=None) -> dict[str, dict]:
    response = client.get("/api/v1/admin/users", headers=headers or bearer(ADMIN))
    assert response.status_code == 200, response.text[:200]
    return {row["id"]: row for row in response.json()}


def test_the_roster_answers_what_an_admin_has_to_know_about_an_account(client):
    """Role and contact are not enough: the reason a worker cannot clock in is here."""
    rows = roster(client)
    assert {WORKER, MOALLEM, ADMIN, HEAD_ADMIN} <= set(rows)

    worker = rows[WORKER]
    assert set(worker) == ROSTER_FIELDS, "the payload is a contract, not a dump"
    assert worker["role"] == "worker"
    assert worker["status"] == "active"
    assert worker["password_set"] is True, "seeded accounts have a password"
    assert worker["password_changed_at"] is None, "never reset since it was created"
    assert worker["sessions_revoked"] == 0
    assert worker["face_enrolled"] is True, "the harness seeds a reference for every user"
    assert worker["phone"] == "" and worker["email"].endswith("@example.test")

    # The order is the one an admin scans in: numeric id, not string id.
    assert [row["id"] for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()] == \
        sorted(rows, key=int)


def test_a_face_reference_is_reported_from_the_file_that_actually_gates_clock_in(client):
    """``/attendance/verify`` answers 404 without the reference file, so that is the truth."""
    rows = roster(client)
    assert rows[WORKER]["face_enrolled"] is True

    # Remove the reference the way idleness/cleanup would, and the roster has to say so:
    # otherwise the admin is told the worker is fine while clock-in refuses them.
    harness.reference_path(WORKER).unlink()
    assert roster(client)[WORKER]["face_enrolled"] is False

    harness.seed_reference(WORKER, harness.reference_path(MOALLEM).read_text())


def test_no_hash_and_no_password_value_ever_reaches_the_response(client):
    response = client.get("/api/v1/admin/users", headers=bearer(ADMIN))
    body = response.text

    assert "password_hash" not in body, "the column name alone is a smell worth failing on"
    assert not re.search(r"\$2[aby]\$", body), "no bcrypt hash, in any field"
    for user_id, password in PASSWORDS.items():
        assert password not in body, f"{user_id}'s password is readable in the roster"
        stored = db_scalar("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        assert stored and stored not in body, "the stored hash may not be shipped to the browser"


def test_the_last_password_change_and_the_revocation_are_reported_after_a_reset(client, app_module):
    """Both come from the reset itself, not from a field the caller could set."""
    before = roster(client)[MOALLEM]
    assert before["password_changed_at"] is None
    assert before["sessions_revoked"] == 0

    reset = client.post(
        "/api/v1/admin/users/edit_password",
        headers=bearer(ADMIN),
        json={"worker_id": MOALLEM, "new_password": "roster-test-pass-456", "admin_id": ADMIN},
    )
    assert reset.status_code == 200, reset.text[:200]

    after = roster(client)[MOALLEM]
    assert after["password_changed_at"] is not None
    logged = db_scalar(
        "SELECT MAX(created_at) FROM audit_log WHERE action = 'password_reset' AND entity_id = ?",
        (MOALLEM,),
    )
    assert after["password_changed_at"] == logged, "the time shown is the audited one"
    assert after["sessions_revoked"] == 1, "a reset revokes the sessions it invalidates"

    # And the number means something: the old password no longer verifies.
    assert app_module.pwd_context.verify(
        "roster-test-pass-456", db_scalar("SELECT password_hash FROM users WHERE id = ?", (MOALLEM,))
    )
    assert not app_module.pwd_context.verify(
        PASSWORDS[MOALLEM], db_scalar("SELECT password_hash FROM users WHERE id = ?", (MOALLEM,))
    )


def test_a_standard_admin_may_not_reset_an_administrators_password(client):
    """The rule the Credentials tab hides a button for - enforced here, not there."""
    assert_denied(
        client.post(
            "/api/v1/admin/users/edit_password",
            headers=bearer(ADMIN),
            json={"worker_id": HEAD_ADMIN, "new_password": "head-hijack-pass-1", "admin_id": ADMIN},
        ),
        endpoint="POST /api/v1/admin/users/edit_password",
        detail="role escalation: a standard admin resetting the head admin's password",
    )

    # ... but the same admin may reset a worker's, which is the whole point of the screen.
    allowed = client.post(
        "/api/v1/admin/users/edit_password",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "new_password": "worker-rotated-pass-1", "admin_id": ADMIN},
    )
    assert allowed.status_code == 200, allowed.text[:200]


def test_the_roster_is_admin_only(client):
    """A worker asking for every colleague's phone number is not a request to answer."""
    assert client.get("/api/v1/admin/users", headers=bearer(WORKER)).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 401
