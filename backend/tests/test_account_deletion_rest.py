"""Deleting your own account over REST: ``DELETE /api/v1/workers/me`` and ``/api/v1/admin/me``.

WHY THIS SUITE EXISTS, NEXT TO ``test_self_service_account_deletion.py``
-----------------------------------------------------------------------
That suite pins ``POST /auth/delete_account`` - the door the app's own screen uses, which proves
the password. This one pins the two doors a *store submission* asks for and a native HTTP client
speaks: ``DELETE /api/v1/workers/me`` for a worker, ``DELETE /api/v1/admin/me`` for an
administrator. Apple 5.1.1 and Google Play's data-safety policy both require that a person can
remove their account from inside the app, and "from inside the app" means an endpoint the app
can call - a form the console renders does not count.

Both doors land on the same implementation (``main._close_own_account``), and that is the property
these tests are really about. A second door that re-implemented "delete my account" is how
somebody's face stays in ``local_references`` while the row that owned it is gone, so the
outcomes below are asserted to be *identical* to the POST door's rather than merely similar.

Three claims matter more than the happy path:

1. **204 No Content is the answer.** A native client and a store reviewer's script both expect
   it, and it says the work is done by the time the response is written.
2. **The old token dies immediately.** ``token_version`` moves inside the same transaction, so
   the very next request with the same bearer token is a 401 - the session is not "eventually"
   revoked.
3. **The hours outlive the person.** An account that has clocked in is anonymised, not removed:
   ``attendance_logs`` keeps every row, and the name, contact details, per-language names and the
   face template go.
"""

from __future__ import annotations

import harness
import pytest
from harness import ADMIN, EMAILS, HEAD_ADMIN, MOALLEM, PASSWORDS, WORKER, bearer, db_scalar

WORKERS_ME = "/api/v1/workers/me"
ADMIN_ME = "/api/v1/admin/me"
WHO_AM_I = "/api/v1/auth/me"


def close_the_shift(client) -> None:
    """The harness leaves WORKER clocked in; both doors refuse while a shift is open."""
    closed = client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "site_name": ""},
    )
    assert closed.status_code == 200, closed.text[:300]


def create_account(client, user_id: str, *, password: str = "Fresh-Pass-123", role: str = "worker"):
    """A real account with no history: the branch that removes the row rather than anonymising."""
    response = client.post(
        "/api/v1/admin/users/create",
        headers=bearer(HEAD_ADMIN),
        data={"user_id": user_id, "name": "No Records", "role": role, "password": password},
    )
    assert response.status_code == 200, response.text[:300]
    return user_id


# ---------------------------------------------------------------------------
# 1. Who may call it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [WORKERS_ME, ADMIN_ME])
def test_an_anonymous_caller_is_refused(client, path):
    """Neither door is public: there is no way to delete an account without a session."""
    assert client.delete(path).status_code == 401


def test_the_worker_door_is_not_reachable_by_an_administrator_only_guard(client):
    """A signed-in worker keeps their own door, which is what the store requirement is about."""
    close_the_shift(client)
    assert client.delete(WORKERS_ME, headers=bearer(WORKER)).status_code == 204


def test_the_admin_door_refuses_an_ordinary_worker(client):
    """``/admin/me`` is the administrator surface, and its guard is the audience matrix's."""
    refused = client.delete(ADMIN_ME, headers=bearer(WORKER))
    assert refused.status_code == 403, refused.text[:200]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1


# ---------------------------------------------------------------------------
# 2. A shift still being worked is refused, not dissolved
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,actor", [(WORKERS_ME, WORKER)])
def test_an_open_shift_is_refused_with_a_reason(client, path, actor):
    """An account that no longer exists cannot clock out, so the hours would vanish."""
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (actor,)) == 1

    refused = client.delete(path, headers=bearer(actor))
    assert refused.status_code == 400, refused.text[:200]
    assert "Clock out first" in refused.json()["detail"], refused.json()
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (actor,)) == 1
    # And the session still works - a refused request must change nothing at all.
    assert client.get(WHO_AM_I, headers=bearer(actor)).status_code == 200


# ---------------------------------------------------------------------------
# 3. 204, and the token that asked is dead
# ---------------------------------------------------------------------------
def test_deleting_an_account_with_history_answers_204_and_kills_the_token(client):
    """The whole acceptance criterion in one request: 204, then 401 on the same token."""
    close_the_shift(client)
    shifts = int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)))
    assert shifts > 0, "the fixture must have history for this to be the branch under test"
    stale = bearer(WORKER)

    response = client.delete(WORKERS_ME, headers=stale)
    assert response.status_code == 204, response.text[:300]
    assert response.content == b"", (
        "a 204 carries no body: a client that reads one is reading a response this route must "
        "not be sending"
    )

    # Immediately, on the next request, with the credential that asked.
    assert client.get(WHO_AM_I, headers=stale).status_code == 401, (
        "token_version did not move inside the transaction, so the session outlived the account"
    )


def test_an_account_with_shifts_is_anonymised_and_keeps_them(client):
    """The row stays because the hours point at it; everything personal about it goes."""
    close_the_shift(client)
    shifts = int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)))
    assert shifts > 0

    assert client.delete(WORKERS_ME, headers=bearer(WORKER)).status_code == 204

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == "Deleted account"
    assert db_scalar("SELECT COALESCE(name_i18n, '') FROM users WHERE id = ?", (WORKER,)) == "", (
        "the per-language names go with the canonical one, or every screen that reads names per "
        "language still prints the person's real name"
    )
    assert db_scalar("SELECT email FROM users WHERE id = ?", (WORKER,)) == ""
    assert db_scalar("SELECT phone FROM users WHERE id = ?", (WORKER,)) == ""
    assert db_scalar("SELECT status FROM users WHERE id = ?", (WORKER,)) == "inactive"

    # The punch records, untouched. This is the whole reason the row still exists.
    assert int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,))) == shifts
    assert int(db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,))) == 0

    # The biometric half, which does not live in the database.
    assert harness.template_exists(WORKER) is False, "the face template is personal data"
    assert harness.stored_photo_path(WORKER) is None


def test_an_account_with_no_history_is_removed_outright(client):
    """Nothing to keep, so nothing is kept - including off the roster."""
    create_account(client, "4401", password="no-records-pass-1")
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", ("4401",)) == 0

    assert client.delete(WORKERS_ME, headers=bearer("4401")).status_code == 204

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("4401",)) == 0
    listed = {row["id"] for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert "4401" not in listed


def test_the_admin_door_deletes_the_administrator_that_asked(client):
    """The same outcome for an administrator's own account, through the admin audience.

    This account has no attendance records, so nothing has to be kept and the row goes - the
    branch ``test_an_account_with_no_history_is_removed_outright`` covers for a worker. What is
    being pinned here is the *door*: an administrator is not told to ask somebody else.
    """
    close_the_shift(client)
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (HEAD_ADMIN,)) == 1

    assert client.delete(ADMIN_ME, headers=bearer(HEAD_ADMIN)).status_code == 204

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (HEAD_ADMIN,)) == 0
    assert client.get(WHO_AM_I, headers=bearer(HEAD_ADMIN)).status_code == 401


# ---------------------------------------------------------------------------
# 4. The password is optional here, and never a way past the account
# ---------------------------------------------------------------------------
def test_a_password_sent_with_the_request_must_still_be_the_right_one(client):
    """Optional is not the same as ignored: a body that carries one has to carry the right one."""
    close_the_shift(client)
    # ``client.request`` rather than ``client.delete``: this Starlette version routes a
    # bodied DELETE through the general method, and the body is part of what is under test.
    refused = client.request(
        "DELETE",
        WORKERS_ME,
        headers=bearer(WORKER),
        json={"current_password": "not-the-password"},
    )
    assert refused.status_code == 401, refused.text[:200]
    assert "password" in refused.json()["detail"].lower(), refused.json()
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) == "Seed Worker"

    accepted = client.request(
        "DELETE",
        WORKERS_ME,
        headers=bearer(WORKER),
        json={"current_password": PASSWORDS[WORKER]},
    )
    assert accepted.status_code == 204, accepted.text[:300]


def test_the_query_parameter_spelling_works_too(client):
    """A DELETE body is legal but not universally sent, so the query string is accepted as well."""
    close_the_shift(client)
    assert client.delete(
        f"{WORKERS_ME}?current_password={PASSWORDS[WORKER]}", headers=bearer(WORKER)
    ).status_code == 204


# ---------------------------------------------------------------------------
# 5. The request cannot be pointed at somebody else
# ---------------------------------------------------------------------------
def test_the_subject_comes_from_the_token_not_the_payload(client):
    """An id in the body is ignored rather than honoured - the object-reference problem."""
    close_the_shift(client)
    response = client.request(
        "DELETE",
        WORKERS_ME,
        headers=bearer(MOALLEM),
        json={"user_id": WORKER, "current_password": PASSWORDS[MOALLEM]},
    )
    assert response.status_code == 204, response.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1, (
        "the id in the payload removed the account it named"
    )
    assert db_scalar("SELECT name FROM users WHERE id = ?", (WORKER,)) != "Deleted account"
    # ...and the caller's own account is the one that went.
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (MOALLEM,)) == 0


# ---------------------------------------------------------------------------
# 6. What the trail keeps, and what it must not
# ---------------------------------------------------------------------------
def test_the_audit_row_is_written_by_the_same_action_as_the_post_door(client):
    """One implementation means one action name, so a deletion is greppable however it arrived."""
    close_the_shift(client)
    email = EMAILS[WORKER]
    assert client.delete(WORKERS_ME, headers=bearer(WORKER)).status_code == 204

    rows = client.get(
        "/api/v1/admin/audit_log?action=account_delete_self", headers=bearer(ADMIN)
    ).json()
    assert len(rows) == 1, rows
    entry = rows[0]
    assert entry["entity_id"] == WORKER
    assert entry["actor_id"] == WORKER
    retained = (entry.get("before_json") or "") + (entry.get("after_json") or "")
    for secret in (email, "Seed Worker", PASSWORDS[WORKER]):
        assert secret not in retained, (
            f"the REST deletion kept {secret!r} in the append-only audit log"
        )
    assert '"outcome": "anonymised"' in entry["after_json"], entry["after_json"]
