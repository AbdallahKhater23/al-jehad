"""Deleting your own account from the app: ``POST /auth/delete_account``.

WHY THIS SUITE EXISTS
---------------------
A product that lets somebody create an account has to let them remove it **without leaving the
app**, and until this endpoint the only way to remove one was ``/admin/users/delete`` - which
needs an administrator to open the door, and which refuses any account that has ever clocked in.
So for a worker who had worked a shift the answer to "delete my account" was "ask your former
employer", and for a worker who had not it was "ask your administrator". Neither is the door the
stores require, and neither is one somebody can use after they have left.

Two rules matter more than the happy path, and the tests below are built around them:

1. **The password authorises it.** A stolen session must not be enough to remove an account any
   more than it is enough to seize one, so ``/auth/change_password``'s rule is repeated here.
2. **The hours outlive the person.** An account with attendance records is **anonymised** - the
   name, the per-language names, the contact details, the rate, the face template and the device
   keys all go, and every shift stays exactly where it was, because an approved report is paid
   from them. An account with no records is deleted outright.

There is also an assertion about the *absence* of something, which is the one worth keeping: the
audit trail for this action must not carry the details it was asked to remove. ``audit_log``
rejects UPDATE and DELETE by trigger, so a name written into it is a name kept for good.
"""

from __future__ import annotations

import harness
import pytest
from harness import ADMIN, EMAILS, HEAD_ADMIN, MOALLEM, PASSWORDS, WORKER, bearer, db_scalar

DELETION = "/api/v1/auth/delete_account"
AUDIT = "/api/v1/admin/audit_log?action=account_delete_self"


def delete_own(client, user_id: str, *, password: str | None = None, **extra):
    """The endpoint called by ``user_id``'s own session, with the password it confirms with."""
    return client.post(
        DELETION,
        headers=bearer(user_id),
        json={
            "current_password": password if password is not None else PASSWORDS[user_id],
            **extra,
        },
    )


def close_the_shift(client) -> None:
    """The harness leaves WORKER clocked in; the endpoint refuses while a shift is open."""
    closed = client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "site_name": ""},
    )
    assert closed.status_code == 200, closed.text[:300]


def create_account(client, user_id: str, *, password: str = "Fresh-Pass-123", name: str = "No Records"):
    """A real account with no history, created the way the console creates one.

    No ``photo`` is sent, so nothing is enrolled: the point of this account is that it has no
    attendance records, which is the branch that removes the row rather than anonymising it.
    """
    response = client.post(
        "/api/v1/admin/users/create",
        headers=bearer(HEAD_ADMIN),
        data={"user_id": user_id, "name": name, "role": "worker", "password": password},
    )
    assert response.status_code == 200, response.text[:300]
    return user_id


def audit_rows(client) -> list[dict]:
    response = client.get(AUDIT, headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


# ---------------------------------------------------------------------------
# 1. Who may ask, and what proves it is them
# ---------------------------------------------------------------------------
def test_a_session_is_required(client):
    """It is not a public route: there is no anonymous way to remove an account."""
    assert client.post(DELETION, json={"current_password": "anything"}).status_code == 401


def test_the_password_is_what_authorises_it(client):
    """A stolen token is not enough - the same bargain ``/auth/change_password`` strikes."""
    refused = delete_own(client, WORKER, password="not-the-password")
    assert refused.status_code == 401, refused.text[:200]
    assert "password" in refused.json()["detail"].lower(), refused.json()
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == "Seed Worker"
    assert audit_rows(client) == [], "a refused deletion is not an event"


def test_the_endpoint_cannot_be_pointed_at_somebody_else(client):
    """The subject comes from the token, so a ``user_id`` in the payload is ignored, not honoured.

    This is the object-reference problem in its cheapest form: an id-addressed "delete an
    account" is a way for any signed-in caller to remove a colleague's. Naming one here must
    change nothing about it.
    """
    close_the_shift(client)
    responded = client.post(
        DELETION,
        headers=bearer(MOALLEM),
        json={"current_password": PASSWORDS[MOALLEM], "user_id": WORKER},
    )
    assert responded.status_code == 200, responded.text[:300]
    assert responded.json()["outcome"] in ("deleted", "anonymised")
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1, (
        "the id in the payload removed the account it named"
    )
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) != "Deleted account"


# ---------------------------------------------------------------------------
# 2. A shift still being worked is refused, not dissolved
# ---------------------------------------------------------------------------
def test_an_account_that_is_clocked_in_cannot_delete_itself(client):
    """A deleted account cannot clock out, so the hours would be recorded nowhere."""
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,)) == 1

    refused = delete_own(client, WORKER)
    assert refused.status_code == 400, refused.text[:200]
    assert "Clock out first" in refused.json()["detail"], refused.json()
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == "Seed Worker"
    assert audit_rows(client) == []


# ---------------------------------------------------------------------------
# 3. With history: anonymised, and the shifts stay
# ---------------------------------------------------------------------------
def test_an_account_with_shifts_is_anonymised_and_keeps_them(client):
    """The branch ``/admin/users/delete`` answers with a 409: here there is a third option."""
    close_the_shift(client)
    shifts = int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)))
    assert shifts > 0, "the fixture must have history for this to be the branch under test"
    stale = bearer(WORKER)

    response = delete_own(client, WORKER)
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["outcome"] == "anonymised"
    assert body["attendance_records"] == shifts
    assert body["face_removal_failed"] == [], "a silent failure here is invisible forever"
    assert "kept" in body["message"], body["message"]

    # The row stays, because the shifts point at it and the reports join it for a name.
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == "Deleted account"
    assert db_scalar("SELECT COALESCE(name_i18n, '') FROM users WHERE id = ?", (WORKER,)) == "", (
        "the per-language names go with the canonical one, or ``names.display`` still prints "
        "the person's real name on every screen that reads names per language"
    )
    assert db_scalar("SELECT email FROM users WHERE id = ?", (WORKER,)) == ""
    assert db_scalar("SELECT phone FROM users WHERE id = ?", (WORKER,)) == ""
    assert db_scalar("SELECT role FROM users WHERE id = ?", (WORKER,)) == "worker", (
        "the role is not personal data and the shifts' audience depends on nothing here"
    )
    assert db_scalar("SELECT status FROM users WHERE id = ?", (WORKER,)) == "inactive"

    # The hours, untouched. This is the whole reason the row exists at all.
    assert int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,))) == shifts
    assert int(db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,))) == 0

    # The biometric half, which is not in the database.
    assert harness.template_exists(WORKER) is False, "the face template is personal data"
    assert harness.stored_photo_path(WORKER) is None

    # Every session, including the one that asked.
    assert client.get("/api/v1/auth/me", headers=stale).status_code == 401, (
        "token_version moved, so an outstanding token is dead - the account is signed out "
        "everywhere, not only on this phone"
    )


def test_an_anonymised_account_cannot_be_signed_into_with_a_blank_contact(client):
    """The cleared columns are ``''``, not NULL, and ``''`` is a value a lookup can match.

    So the thing that refuses an empty contact submission is not the lookup - it is the status.
    That is worth pinning: if a cleared account were left ``active``, the emptiness itself would
    be the way in.
    """
    close_the_shift(client)
    assert delete_own(client, WORKER).status_code == 200

    found_by_nothing = client.post(
        "/api/v1/auth/login",
        json={"user_id": WORKER, "email_or_phone": "", "password": PASSWORDS[WORKER]},
    )
    assert found_by_nothing.status_code == 403, (
        "an anonymised account was reached by submitting an empty contact "
        f"({found_by_nothing.text[:200]})"
    )

    by_its_old_contact = client.post(
        "/api/v1/auth/login",
        json={"user_id": WORKER, "email_or_phone": EMAILS[WORKER], "password": PASSWORDS[WORKER]},
    )
    assert by_its_old_contact.status_code == 401, (
        "the contact details the row is found by are gone, so it is not findable at all - "
        f"({by_its_old_contact.text[:200]})"
    )


# ---------------------------------------------------------------------------
# 4. With nothing attached: gone
# ---------------------------------------------------------------------------
def test_an_account_with_no_history_is_deleted_outright(client):
    """A duplicate, a typo'd id, somebody who never worked a shift: nothing to keep."""
    create_account(client, "4321", password="typo-pass-123", name="Typo Account")
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", ("4321",)) == 0

    response = delete_own(client, "4321", password="typo-pass-123")
    assert response.status_code == 200, response.text[:400]
    assert response.json()["outcome"] == "deleted"
    assert response.json()["attendance_records"] == 0

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("4321",)) == 0
    assert harness.template_exists("4321") is False
    listed = {row["id"] for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert "4321" not in listed, "the roster is the contract: a deleted account is not on it"


# ---------------------------------------------------------------------------
# 5. What the trail keeps, and what it must not
# ---------------------------------------------------------------------------
def test_the_trail_records_the_shape_and_not_the_person(client):
    """``audit_log`` is append-only, so anything written there is written for good.

    Recording the name, the email or the phone would mean the deletion kept precisely what it was
    asked to remove, in the one table nothing can correct afterwards. What survives is the part
    an investigation needs, and the actor id and role ``audit.record`` writes for itself.
    """
    close_the_shift(client)
    email = EMAILS[WORKER]

    assert delete_own(client, WORKER).status_code == 200

    rows = audit_rows(client)
    assert len(rows) == 1, rows
    entry = rows[0]
    assert entry["entity_id"] == WORKER
    assert entry["actor_id"] == WORKER, "the event is attributable to the account that asked"

    retained = (entry.get("before_json") or "") + (entry.get("after_json") or "")
    for secret in (email, "Seed Worker", PASSWORDS[WORKER]):
        assert secret not in retained, (
            f"the anonymising delete kept {secret!r} in the append-only audit log"
        )
    assert '"outcome": "anonymised"' in entry["after_json"], entry["after_json"]
    assert '"had_contact": true' in entry["before_json"], (
        "the trail stops saying whether there was anything personal to remove"
    )
    assert '"assigned_to_a_moallem":' in entry["before_json"], entry["before_json"]


def test_a_deleted_account_records_that_outcome(client):
    """The other branch writes its own outcome, so the trail says which one happened."""
    create_account(client, "4322", password="no-records-pass-1")
    assert delete_own(client, "4322", password="no-records-pass-1").status_code == 200

    entry = audit_rows(client)[0]
    assert '"outcome": "deleted"' in entry["after_json"], entry["after_json"]
    assert '"attendance_records": 0' in entry["after_json"], entry["after_json"]
