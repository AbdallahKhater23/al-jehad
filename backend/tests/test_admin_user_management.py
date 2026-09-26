"""Editing, deactivating and deleting an account: ``/admin/users/{edit,status,delete}``.

WHY THIS EXISTS
---------------
The console could create an account and set its password, and that was the whole of it. A
name typed wrong, a rate nobody had entered, a worker who had left, an id entered for the
wrong man - each of those meant editing SQLite by hand, which is not something an
administrator standing on a building site is going to do.

Each endpoint has one rule that matters more than its happy path:

1. **Editing is an update to a person, not a new identity for one.** The id cannot change
   (it is the key every attendance row, punch, device key and audit entry is written
   against) and neither can the role (the id ranges make the role a function of the id:
   1-499 worker, 500-749 lead worker, 750-999 off-office worker, 1000-4999 admin, 5000+
   head admin). A promotion is a
   new account in the right id block; the old account keeps the hours, which are the part
   that must not move.
2. **Deactivating removes access and keeps history.** No sign-in, no live token, no offline
   signing key on the phone, no face template left enrolled - and every shift exactly
   where it was.
3. **Deleting refuses when there is history.** An account's attendance records are what an
   approved report is paid from, so deleting it would take a person out of a report that
   is still being paid; the endpoint says so, names the count, and points at deactivation.
   What it does delete is the case it exists for: an account with nothing attached.
"""

from __future__ import annotations

from datetime import date, timedelta

import harness
import pytest
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    PASSWORDS,
    SEED_USERS,
    WORKER,
    assert_denied,
    bearer,
    db_scalar,
)

#: The seeded lead worker, renamed and given a contact - the ordinary edit.
EDITED = {
    "user_id": MOALLEM,
    "name": "Ana Torres",
    "email": "ana.torres@example.test",
    "phone": "+201111111111",
}


def edit(client, headers=None, **overrides):
    return client.post(
        "/api/v1/admin/users/edit",
        headers=headers or bearer(HEAD_ADMIN),
        json={**EDITED, **overrides},
    )


#: The reference the harness writes for every seeded user, captured at import.
#:
#: The database is restored from a pristine snapshot before every test; the files are not,
#: and deactivation deletes a template on purpose - so a test that needs one to exist puts
#: it back rather than depending on which test ran first.
SEEDED_REFERENCE = harness.reference_path(WORKER).read_text()


def enroll_reference(user_id: str = WORKER) -> None:
    harness.seed_reference(user_id, SEEDED_REFERENCE)


@pytest.fixture(autouse=True)
def _restore_reference_files():
    """Put back what a deactivation deleted on purpose.

    The database is snapshotted and restored around every test; the reference files are
    not, and both the stubbed face model and the following tests read them - so a test
    that deletes one is responsible for the suite being able to run after it.
    """
    yield
    for user_id in SEED_USERS:
        enroll_reference(user_id)


def enroll_face(client, user_id: str, *, as_user: str = HEAD_ADMIN, image: bytes):
    """The console's write-a-face path: ``POST /admin/enroll``, a file from disk.

    The same endpoint the Credentials tab's edit panel posts to, and the same one the bulk
    roster import uses - which is why the rule it enforces is asserted here rather than in
    a suite of its own: this is the endpoint that decides what a punch is checked against.
    """
    return client.post(
        "/api/v1/admin/enroll",
        headers=bearer(as_user),
        data={"worker_id": user_id},
        files={"photo": ("reference.jpg", image, "image/jpeg")},
    )


def template_version(user_id: str) -> int:
    return int(db_scalar("SELECT template_version FROM users WHERE id = ?", (user_id,)) or 0)


def new_account(client, user_id: str = "321", name: str = "Nguyen Van A", role: str = "worker", image: bytes = b""):
    """A real account, created the way the console creates one, with a face."""
    files = {"photo": ("photo.jpg", image, "image/jpeg")} if image else None
    response = client.post(
        "/api/v1/admin/users/create",
        headers=bearer(HEAD_ADMIN),
        data={"user_id": user_id, "name": name, "role": role, "password": "Fresh-Pass-123"},
        files=files,
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["face_enrolled"] is True, "the create flow enrolled a template"
    return user_id


def login(client, user_id: str, password: str, email_or_phone: str):
    return client.post(
        "/api/v1/auth/login",
        json={"user_id": user_id, "email_or_phone": email_or_phone, "password": password},
    )


# ---------------------------------------------------------------------------
# 1. Editing an account
# ---------------------------------------------------------------------------
def test_the_admin_can_change_a_name_a_contact_and_a_rate(client):
    """The reason the endpoint exists: the roster said 'Ana Torrez'."""
    response = edit(client)
    assert response.status_code == 200, response.text[:300]
    assert response.json()["user"]["name"] == "Ana Torres"

    assert db_scalar("SELECT name FROM users WHERE id = ?", (MOALLEM,)) == "Ana Torres"
    assert db_scalar("SELECT email FROM users WHERE id = ?", (MOALLEM,)) == "ana.torres@example.test"
    assert db_scalar("SELECT phone FROM users WHERE id = ?", (MOALLEM,)) == "+201111111111"

    listed = {row["id"]: row for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert listed[MOALLEM]["name"] == "Ana Torres", "the roster shows the change, not the old row"
    assert listed[MOALLEM]["email"] == "ana.torres@example.test"

    # The login contact really moved: the account signs in with the new email.
    signed_in = login(client, MOALLEM, PASSWORDS[MOALLEM], "ana.torres@example.test")
    assert signed_in.status_code == 200, signed_in.text[:200]


def test_an_edit_is_audited_before_and_after_without_the_password(client):
    """Append-only history is the only place 'who renamed this, and when' can be read."""
    edit(client)

    row = client.get("/api/v1/admin/audit_log?action=user_edit", headers=bearer(ADMIN)).json()[0]
    assert row["actor_id"] == HEAD_ADMIN and row["entity"] == "users" and row["entity_id"] == MOALLEM
    assert '"name": "Seed Lead Worker"' in row["before_json"]
    assert '"name": "Ana Torres"' in row["after_json"]
    for forbidden in ("password_hash", "$2b$", PASSWORDS[MOALLEM]):
        assert forbidden not in row["before_json"] and forbidden not in row["after_json"], (
            "the audit trail is append-only, so a hash written into it is a credential forever"
        )


def test_an_edit_leaves_the_id_and_the_password_alone(client, app_module):
    before = db_scalar("SELECT password_hash FROM users WHERE id = ?", (MOALLEM,))
    edit(client)

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (MOALLEM,)) == 1
    after = db_scalar("SELECT password_hash FROM users WHERE id = ?", (MOALLEM,))
    assert after == before, "a name change must not rotate a credential"
    assert app_module.pwd_context.verify(PASSWORDS[MOALLEM], after)
    assert login(client, MOALLEM, PASSWORDS[MOALLEM], "ana.torres@example.test").status_code == 200


def test_a_nameless_account_is_refused(client):
    """An empty name is not a rename, it is a blank row in a payroll report."""
    refused = edit(client, name="   ")
    assert refused.status_code == 400, refused.text[:200]
    assert "Name must not be empty" in refused.json()["detail"]
    assert db_scalar("SELECT name FROM users WHERE id = ?", (MOALLEM,)) == "Seed Lead Worker"


def test_a_role_is_not_editable_and_a_payload_naming_one_changes_nothing(client):
    """The id block decides the role; an update is not the way around it."""
    for wanted in ("admin", "head_admin", "worker"):
        response = client.post(
            "/api/v1/admin/users/edit",
            headers=bearer(HEAD_ADMIN),
            json={**EDITED, "role": wanted},
        )
        assert response.status_code == 200, response.text[:200]
        assert (
            db_scalar("SELECT role FROM users WHERE id = ?", (MOALLEM,)) == "moallem"
        ), f"a payload naming role={wanted!r} changed the role"

    # ... and the id itself cannot be moved either, whatever the payload says.
    moved = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(HEAD_ADMIN),
        json={**EDITED, "id": "999", "worker_id": "999"},
    )
    assert moved.status_code == 200, moved.text[:200]
    assert db_scalar("SELECT name FROM users WHERE id = ?", (MOALLEM,)) == "Ana Torres"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("999",)) == 0


def test_the_rate_is_still_editable_but_the_timesheet_never_prices_a_shift(client):
    """``u.hourly_rate`` is a field on the worker, not a number any report multiplies.

    The shifts report used to build a ``gross_estimate`` out of it. It is a timesheet now,
    and a sheet that priced a shift invites whoever receives it to read the answer as a
    payroll figure this app never computed - so the rate must not be in it at all.
    """
    assert client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(HEAD_ADMIN),
        json={"user_id": WORKER, "name": "Seed Worker", "hourly_rate": 12.5},
    ).status_code == 200
    assert db_scalar("SELECT hourly_rate FROM users WHERE id = ?", (WORKER,)) == pytest.approx(12.5)

    detail = client.get(f"/api/v1/admin/users/{WORKER}", headers=bearer(ADMIN)).json()
    assert detail["hourly_rate"] == pytest.approx(12.5), "the rate is still the admin's to set"

    # A period built around the shifts that actually exist: the endpoint limits a range to
    # three years, and the cloned database's rows are wherever its own history left them.
    newest = date.fromisoformat(str(db_scalar("SELECT MAX(timestamp) FROM attendance_logs"))[:10])
    period = f"start={(newest - timedelta(days=366)).isoformat()}&end={newest.isoformat()}"
    report = client.get(
        f"/api/v1/admin/reports/shifts?{period}&worker_id={WORKER}", headers=bearer(ADMIN)
    ).json()
    assert report["rows"], "the seeded shifts are what this is about"
    assert any(row["hours"] > 0 for row in report["rows"]), "and the hours are still reported"
    for row in report["rows"]:
        assert "hourly_rate" not in row, "the timesheet does not know what anybody earns"
        assert "gross_estimate" not in row
        assert "overtime_gross_estimate" not in row
    assert "gross_estimate" not in report["totals"]


def test_an_impossible_rate_is_refused_and_zero_means_no_rate_at_all(client):
    assert edit(client, hourly_rate=-3).status_code == 400
    assert edit(client, hourly_rate=99999).status_code == 400

    edit(client, hourly_rate=0)
    assert db_scalar("SELECT hourly_rate FROM users WHERE id = ?", (MOALLEM,)) is None, (
        "a rate of zero is a rate nobody agreed, not a worker who costs nothing"
    )


def test_an_omitted_rate_is_left_alone(client):
    """The edit form sends what it shows; a field nobody touched must not be cleared."""
    edit(client, hourly_rate=7)
    edit(client, name="Ana Torres")
    assert db_scalar("SELECT hourly_rate FROM users WHERE id = ?", (MOALLEM,)) == pytest.approx(7)


def test_the_detail_read_carries_what_the_roster_contract_does_not(client):
    """``/admin/users`` is pinned by its own test; the hourly rate is read from here."""
    edit(client, hourly_rate=9)

    detail = client.get(f"/api/v1/admin/users/{MOALLEM}", headers=bearer(ADMIN))
    assert detail.status_code == 200, detail.text[:200]
    body = detail.json()
    assert body["id"] == MOALLEM and body["role"] == "moallem"
    assert body["hourly_rate"] == pytest.approx(9)
    assert "password_hash" not in body, "the detail read is not a dump"

    roster = {row["id"]: row for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert "hourly_rate" not in roster[MOALLEM], "the list keeps its published shape"

    assert client.get("/api/v1/admin/users/424242", headers=bearer(ADMIN)).status_code == 404


# ---------------------------------------------------------------------------
# 2. Who may touch whose account
# ---------------------------------------------------------------------------
def test_a_standard_admin_cannot_touch_an_administrators_account(client, jpeg):
    """The rule the password endpoint already had, applied to the whole account.

    Including the account's *face*, which is the one of the four that is not a field: a name
    or a rate is an identity on a screen, and the reference photo is the identity the gate
    checks. An administrator whose face a standard admin may write is an administrator whose
    punches that standard admin can make, so writing one is the same escalation as rewriting
    the account - and it is refused by the same function.
    """
    # A template that cannot be mistaken for the stub's embedding, so "the file is unchanged"
    # is evidence rather than a coincidence of the model being deterministic.
    harness.seed_reference(HEAD_ADMIN, "the-seeded-template-must-survive")
    seeded_face = harness.reference_path(HEAD_ADMIN).read_text()
    versions = {user_id: template_version(user_id) for user_id in (HEAD_ADMIN, ADMIN)}
    assert_denied(
        client.post(
            "/api/v1/admin/users/edit",
            headers=bearer(ADMIN),
            json={"user_id": HEAD_ADMIN, "name": "Hijack"},
        ),
        endpoint="POST /api/v1/admin/users/edit",
        detail="role escalation: a standard admin rewriting the head admin's account",
    )
    assert_denied(
        client.post(
            "/api/v1/admin/users/delete", headers=bearer(ADMIN), json={"user_id": HEAD_ADMIN}
        ),
        endpoint="POST /api/v1/admin/users/delete",
        detail="role escalation: a standard admin removing the head admin",
    )
    assert_denied(
        client.post(
            "/api/v1/admin/users/status",
            headers=bearer(ADMIN),
            json={"user_id": HEAD_ADMIN, "active": False},
        ),
        endpoint="POST /api/v1/admin/users/status",
        detail="role escalation: a standard admin deactivating the head admin",
    )
    assert_denied(
        enroll_face(client, HEAD_ADMIN, as_user=ADMIN, image=jpeg),
        endpoint="POST /api/v1/admin/enroll",
        detail="role escalation: a standard admin writing the head admin's face",
    )
    assert db_scalar("SELECT name FROM users WHERE id = ?", (HEAD_ADMIN,)) == "Seed Head Admin"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (HEAD_ADMIN,)) == 1, (
        "an administrator's account survived all four attempts"
    )
    assert harness.reference_path(HEAD_ADMIN).read_text() == seeded_face, (
        "the photo every punch is verified against was not rewritten"
    )
    assert {user_id: template_version(user_id) for user_id in versions} == versions, (
        "and no account was marked as re-enrolled"
    )


def test_a_standard_admin_still_edits_the_workers_that_are_the_job(client):
    assert edit(client, headers=bearer(ADMIN)).status_code == 200
    assert db_scalar("SELECT name FROM users WHERE id = ?", (MOALLEM,)) == "Ana Torres"


def test_a_standard_admin_still_enrolls_the_workers_that_are_the_job(client, jpeg):
    """The other half of the boundary, and the reason it is a boundary and not a lock.

    A worker's photo is the ordinary case the endpoint exists for - it is the answer on the
    day somebody's capture stops matching - and the administrator who works that site is the
    one who has to be able to give it, without a head admin being called to the gate.
    """
    before = template_version(MOALLEM)
    response = enroll_face(client, MOALLEM, as_user=ADMIN, image=jpeg)
    assert response.status_code == 200, response.text[:300]
    assert template_version(MOALLEM) == before + 1
    assert harness.template_exists(MOALLEM)


def test_a_head_admin_replaces_an_administrators_face(client, jpeg):
    """Which is what the console offers: Credentials, the row's edit panel, a photo.

    An administrator's template goes stale exactly like a worker's (the detector changed and
    every template written before it has to be taken again - see ``needs_reenrollment``), so
    the account that *cannot* be photographed again by itself is the one that needs this
    most: a head admin is the only other person who may write it.
    """
    before = template_version(ADMIN)
    response = enroll_face(client, ADMIN, as_user=HEAD_ADMIN, image=jpeg)
    assert response.status_code == 200, response.text[:300]
    assert template_version(ADMIN) == before + 1
    assert harness.template_exists(ADMIN)


# ---------------------------------------------------------------------------
# 3. Deactivating: remove the access, keep the hours
# ---------------------------------------------------------------------------
def test_deactivating_removes_every_way_in_and_keeps_the_shifts(client):
    shifts_before = int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)))
    assert shifts_before > 0, "the seeded worker has history - that is the point of this test"
    enroll_reference()
    # Minted *before* the change: ``bearer`` reads the live version, so a header built
    # afterwards would carry the new one and prove nothing.
    stale = bearer(WORKER)

    response = client.post(
        "/api/v1/admin/users/status",
        headers=bearer(ADMIN),
        json={"user_id": WORKER, "active": False},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["active"] is False
    assert response.json()["face_removed"], "the template is reported as removed, not assumed"

    assert db_scalar("SELECT status FROM users WHERE id = ?", (WORKER,)) == "inactive"
    assert not harness.template_exists(WORKER), "a departed worker's face is not left enrolled"

    # The hours are untouched: this is the whole difference between this and deleting.
    assert (
        int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)))
        == shifts_before
    )

    # Sign-in is refused, and says why rather than pretending the password was wrong.
    refused = login(client, WORKER, PASSWORDS[WORKER], "seed1@example.test")
    assert refused.status_code == 403, refused.text[:200]
    assert "deactivated" in refused.json()["detail"]

    # Every token issued before the change is dead.
    assert client.get("/api/v1/worker/me/stats", headers=stale).status_code == 401

    listed = {row["id"]: row for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert listed[WORKER]["status"] == "inactive", "the roster is where the state is read"


def test_a_deactivated_account_is_told_apart_from_an_unknown_one_only_with_the_password(client):
    """The refusal is safe: it is only ever reached by somebody who has the password."""
    client.post(
        "/api/v1/admin/users/status", headers=bearer(ADMIN), json={"user_id": WORKER, "active": False}
    )
    guessed = login(client, WORKER, "not-the-password", "seed1@example.test")
    unknown = login(client, "424242", "not-the-password", "nobody@example.test")
    assert guessed.status_code == unknown.status_code == 401
    assert guessed.json()["detail"] == unknown.json()["detail"], (
        "a deactivated account must not be an enumeration oracle"
    )


def test_reactivating_restores_the_sign_in_and_says_the_face_is_gone(client):
    client.post(
        "/api/v1/admin/users/status", headers=bearer(ADMIN), json={"user_id": WORKER, "active": False}
    )
    back = client.post(
        "/api/v1/admin/users/status", headers=bearer(ADMIN), json={"user_id": WORKER, "active": True}
    )
    assert back.status_code == 200, back.text[:300]
    assert "face" in back.json()["message"], "the console is told what reactivating did not restore"

    assert login(client, WORKER, PASSWORDS[WORKER], "seed1@example.test").status_code == 200
    listed = {row["id"]: row for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert listed[WORKER]["status"] == "active"
    assert listed[WORKER]["face_enrolled"] is False, "they cannot clock in until they are enrolled again"


def test_deactivating_twice_is_not_a_second_revocation(client):
    first = client.post(
        "/api/v1/admin/users/status", headers=bearer(ADMIN), json={"user_id": WORKER, "active": False}
    )
    version = int(db_scalar("SELECT COALESCE(token_version, 0) FROM users WHERE id = ?", (WORKER,)))
    second = client.post(
        "/api/v1/admin/users/status", headers=bearer(ADMIN), json={"user_id": WORKER, "active": False}
    )
    assert first.status_code == second.status_code == 200
    assert "already deactivated" in second.json()["message"]
    assert int(db_scalar("SELECT COALESCE(token_version, 0) FROM users WHERE id = ?", (WORKER,))) == version


def test_deactivating_yourself_is_refused(client):
    """The only account that could be the last head admin is the one asking."""
    refused = client.post(
        "/api/v1/admin/users/status",
        headers=bearer(HEAD_ADMIN),
        json={"user_id": HEAD_ADMIN, "active": False},
    )
    assert refused.status_code == 400, refused.text[:200]
    assert db_scalar("SELECT status FROM users WHERE id = ?", (HEAD_ADMIN,)) == "active"
    assert int(db_scalar("SELECT COUNT(*) FROM users WHERE role = 'head_admin'")) == 1, (
        "the console can never be left without a head admin"
    )


# ---------------------------------------------------------------------------
# 4. Deleting: only an account with nothing attached
# ---------------------------------------------------------------------------
def test_an_account_with_no_history_is_really_deleted(client, jpeg):
    """The case the endpoint exists for: an id typed for the wrong man."""
    user_id = new_account(client, "321", image=jpeg)
    assert harness.template_exists(user_id), "a template was written for the new account"
    assert harness.stored_photo_path(user_id) is not None, "the console kept the selfie beside the row"

    # Everything that only makes sense beside an account: a device key and a live link.
    device = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(user_id, role="worker"),
        json={"device_id": f"web-{user_id}-test", "note": "site phone"},
    )
    assert device.status_code == 200, device.text[:300]
    invite = client.post(
        "/api/v1/admin/enrollment/invites",
        headers=bearer(HEAD_ADMIN),
        json={"worker_id": user_id, "kind": "enroll"},
    )
    assert invite.status_code in (200, 201), invite.text[:300]

    response = client.post(
        "/api/v1/admin/users/delete", headers=bearer(ADMIN), json={"user_id": user_id}
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["deleted"] is True
    assert response.json()["face_removal_failed"] == []

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)) == 0
    assert not harness.template_exists(user_id), "the template goes with the account"
    assert harness.stored_photo_path(user_id) is None, "and so does the selfie"
    assert db_scalar("SELECT COUNT(*) FROM worker_devices WHERE worker_id = ?", (user_id,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM enrollment_invites WHERE worker_id = ?", (user_id,)) == 0

    listed = {row["id"] for row in client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()}
    assert user_id not in listed, "the roster is the contract: a deleted account is not on it"

    logged = client.get("/api/v1/admin/audit_log?action=user_delete", headers=bearer(ADMIN)).json()[0]
    assert logged["entity_id"] == user_id and '"deleted": true' in logged["after_json"]
    assert '"attendance_records": 0' in logged["after_json"], "the audit says why it was allowed"


def test_an_account_with_shifts_cannot_be_deleted(client):
    """The hours win: they are what an approved report is paid from."""
    # Close the seeded open shift first, so the refusal under test is the history and not
    # the clock-in (which is asserted on its own below).
    closed = client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER, "site_name": ""},
    )
    assert closed.status_code == 200, closed.text[:300]
    shifts = int(db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (WORKER,)))
    assert shifts > 0
    had_reference = harness.template_exists(WORKER)

    refused = client.post(
        "/api/v1/admin/users/delete", headers=bearer(ADMIN), json={"user_id": WORKER}
    )
    assert refused.status_code == 409, refused.text[:300]
    detail = refused.json()["detail"]
    assert str(shifts) in detail and "Deactivate" in detail, (
        "the refusal names the count and points at the endpoint that can do it"
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1
    assert harness.template_exists(WORKER) == had_reference, (
        "a refused delete removes nothing, on disk either"
    )
    assert client.get("/api/v1/admin/audit_log?action=user_delete", headers=bearer(ADMIN)).json() == []


def test_an_account_that_is_clocked_in_cannot_be_deleted(client):
    """The harness leaves a shift open for WORKER: somebody is on site right now."""
    assert int(db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (WORKER,))) == 1

    refused = client.post(
        "/api/v1/admin/users/delete", headers=bearer(ADMIN), json={"user_id": WORKER}
    )
    assert refused.status_code == 400, refused.text[:200]
    assert "close that shift first" in refused.json()["detail"], (
        "the open shift is named before the history, because it is the thing to fix first"
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (WORKER,)) == 1


def test_deleting_yourself_is_refused(client):
    refused = client.post(
        "/api/v1/admin/users/delete", headers=bearer(HEAD_ADMIN), json={"user_id": HEAD_ADMIN}
    )
    assert refused.status_code == 400, refused.text[:200]
    assert "signed in with" in refused.json()["detail"]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (HEAD_ADMIN,)) == 1, (
        "the console cannot be left without a head admin"
    )


def test_deleting_an_unknown_account_is_a_404(client):
    assert (
        client.post(
            "/api/v1/admin/users/delete", headers=bearer(ADMIN), json={"user_id": "424242"}
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 5. The surface itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/v1/admin/users/edit", EDITED),
        ("post", "/api/v1/admin/users/status", {"user_id": MOALLEM, "active": False}),
        ("post", "/api/v1/admin/users/delete", {"user_id": MOALLEM}),
        ("get", f"/api/v1/admin/users/{MOALLEM}", None),
    ],
    ids=["edit", "status", "delete", "detail"],
)
def test_the_new_endpoints_are_admin_only(client, method, path, body):
    call = getattr(client, method)
    anonymous = call(path) if body is None else call(path, json=body)
    assert anonymous.status_code == 401, anonymous.text[:200]

    worker = (
        call(path, headers=bearer(WORKER))
        if body is None
        else call(path, json=body, headers=bearer(WORKER))
    )
    assert worker.status_code == 403, worker.text[:200]
