"""Walk-up registration: one issued link, and an account held for approval.

WHY THIS EXISTS
---------------
The registration link that already ships is *per person*, for an account an administrator has
already created: whoever opens it registers their face against that account. That is the right
shape for one named hire and the wrong one for a walk-up, where nobody has applied yet. This
suite is about the second shape, and every assertion in it is a way that shape could be wrong
and silent. The form itself is one address the company hands out, and that address is the
*link*: the whole surface below is reached through it and refused without it, so "who can open
this form" is a question the console answers rather than the internet.

1. **The account exists the moment the form is sent**, and it is quarantined. The applicant is
   told their id on the spot, signs in with it immediately, and cannot record a single punch
   until an administrator approves them - which is refused at the punch itself, before the
   geofence and before the camera.
2. **The public route is a public route.** It is the one endpoint in the application that
   accepts a face from nobody in particular, so: off by default, rate limited, capped, and the
   one piece of model work it does runs in the gate's own bounded pool.
3. **The upload obeys the one policy.** 5 MB, enforced *while reading*, and the type decided
   from the bytes - so a PDF renamed ``photo.jpg`` is refused and so is a 6 MB JPEG, on the
   surface that has no session behind it.
4. **One photograph is one face, filed once.** The reference goes into the biometric store under
   an immutable id as part of the submission, and the staged upload is removed in the same
   request - so there is no second copy in an intake directory for anything to browse.
5. **The number is the lowest free one below the administrative tiers**, allocated inside the
   transaction that inserts the account, and a refusal deletes the account *and wipes its face*
   before that number can be handed to anybody else.
6. **The face is destroyed by a refusal**, and the decision survives it: ``audit_log`` keeps the
   name, the role and the reason, which is what a ``users`` row could never do.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

import biometrics
import harness
import registrations
from config import settings
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    OFF_OFFICE,
    WORKER,
    assert_denied,
    bearer,
    db_scalar,
)

STRONG_PASSWORD = "site-attendance-2026"
#: The link's own address, token and all. Generation 0 is the generation every deployment that
#: has never replaced its link carries, which is every database this suite runs against - so the
#: token can be computed here instead of fetched, and everything below goes through the same
#: address an applicant would have been sent. ``test_the_link_can_be_replaced`` rotates it and
#: builds the new one from the answer, which is the only test here that cannot use this constant.
PUBLIC = f"/api/v1/register/{registrations.link_token(0)}"
#: The bare path, with no token: what a link pasted without its last segment, or an address typed
#: from memory, actually asks for.
PUBLIC_BARE = "/api/v1/register"
REVIEW = "/api/v1/admin/registrations"
LINK = "/api/v1/admin/registrations/link"
LOGIN = "/api/v1/auth/login"


def photo(seed: int = 0, size: tuple[int, int] = (240, 240)) -> bytes:
    """A valid JPEG whose *bytes* differ per seed.

    Seeded noise rather than a flat colour, deliberately: a uniformly flat image quantises a
    shade apart to the *same* coefficients, and a helper that can collide quietly makes every
    test about "these are different photographs" meaningless.
    """
    rng = random.Random(seed)
    image = Image.new("RGB", size)
    image.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(size[0] * size[1])]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def intake(monkeypatch):
    """Intake open for one test. It ships **closed**, so this is the operator's switch."""
    monkeypatch.setattr(settings, "registration_enabled", True)
    return settings


def submit(client, *, name="Walk Up", password=STRONG_PASSWORD, role="worker", image=None, **extra):
    data = {"full_name": name, "password": password, "role": role, "consent": "true"}
    data.update({key: value for key, value in extra.items() if value is not None})
    return client.post(
        PUBLIC,
        data=data,
        files={"photo": ("photo.jpg", image if image is not None else photo(), "image/jpeg")},
    )


def lowest_free_id() -> int:
    """The number the next submission will be handed: the lowest free one in the band.

    The allocator's rule restated rather than a copy of its code, so a test that disagrees with
    it fails here instead of silently passing.
    """
    taken = {
        int(row[0])
        for row in harness.db_rows(
            "SELECT id FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND ?",
            (registrations.WORKFORCE_ID_CEILING,),
        )
    }
    candidate = 1
    while candidate in taken:
        candidate += 1
    return candidate


def sign_in(client, user_id: str, *, password: str = STRONG_PASSWORD):
    """The sign-in an applicant can make from the moment their account exists."""
    return client.post(
        LOGIN, json={"user_id": user_id, "email_or_phone": "", "password": password}
    )


def token_for(client, user_id: str) -> str:
    response = sign_in(client, user_id)
    assert response.status_code == 200, response.text[:300]
    return response.json()["token"]


def approve(client, user_id: str, *, as_user: str = ADMIN, note: str | None = None):
    payload = {} if note is None else {"note": note}
    return client.post(f"{REVIEW}/{user_id}/approve", headers=bearer(as_user), json=payload)


def reject(client, user_id: str, *, as_user: str = ADMIN, note: str | None = None):
    payload = {} if note is None else {"note": note}
    return client.post(f"{REVIEW}/{user_id}/reject", headers=bearer(as_user), json=payload)


# ---------------------------------------------------------------------------
# 1. the link itself
# ---------------------------------------------------------------------------
def test_the_link_says_whether_it_is_open_and_what_the_form_must_satisfy(client):
    """Closed by default, and a closed link still *answers* rather than 404-ing."""
    closed = client.get(PUBLIC)
    assert closed.status_code == 200
    assert closed.json()["enabled"] is False
    assert "closed" in closed.json()["message"].lower()


def test_the_open_link_describes_its_policy_and_no_data(client, intake):
    body = client.get(PUBLIC).json()
    assert body["enabled"] is True
    assert body["roles"] == ["worker", "moallem", "off_office"]
    assert body["photo_policy"]["max_bytes"] == 5 * 1024 * 1024
    assert body["min_password_length"] >= 8
    assert body["consent_version"] == registrations.CONSENT_VERSION
    # Policy, not data: nothing about the queue, nobody's name, no account id.
    assert set(body) == {
        "enabled",
        "roles",
        "photo_policy",
        "min_password_length",
        "consent_version",
        "message",
    }


def test_a_submission_creates_a_quarantined_account(client, intake, app_module):
    """The step that replaced the holding table: the account is real, and it is held back."""
    response = submit(client, work_details="Stone cutting")
    assert response.status_code == 200, response.text[:300]
    user_id = response.json()["user_id"]

    assert db_scalar("SELECT status FROM users WHERE id = ?", (user_id,)) == "pending_approval"
    assert db_scalar("SELECT name FROM users WHERE id = ?", (user_id,)) == "Walk Up"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (user_id,)) == "worker"
    assert db_scalar("SELECT registration_note FROM users WHERE id = ?", (user_id,)) == (
        "Stone cutting"
    ), "the reviewer's card has nothing to read if the applicant's own line is dropped"
    assert db_scalar("SELECT biometric_id FROM users WHERE id = ?", (user_id,)), (
        "the account was created without an immutable biometric id"
    )

    # The credential is hashed on the way in and never stored in the clear.
    stored = db_scalar("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    assert stored and stored != STRONG_PASSWORD
    assert app_module.pwd_context.verify(STRONG_PASSWORD, stored)

    # The face was filed as part of the submission: that is what lets an approval be a single
    # status change rather than a second walk through the model.
    assert harness.template_exists(user_id), "the reference was not written"

    # ... and the staged upload is gone with it. Nothing keeps a second copy of a face.
    assert os.listdir(registrations.photos_dir()) == [], (
        "the submission left its staged photograph in the intake directory"
    )

    assert int(user_id) <= registrations.WORKFORCE_ID_CEILING, (
        "a public form minted an id outside the workforce band"
    )
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'user_self_registered' AND entity_id = ?",
        (user_id,),
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'registration_submitted' "
        "AND worker_id = ?",
        (user_id,),
    ) == 1

    # The holding table migration 24 created is not written to by this build at all.
    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 0


def test_the_public_response_carries_the_id_and_no_credential(client, intake):
    """The applicant is told their own id, on this screen, and nothing else that is secret."""
    expected = str(lowest_free_id())
    body = submit(client).json()
    assert body["user_id"] == expected
    assert body["name"] == "Walk Up"
    assert body["approval_status"] == "pending_approval"
    assert body["role"] == "worker"
    assert set(body) == {
        "status",
        "user_id",
        "name",
        "role",
        "approval_status",
        "template_written",
        "message",
    }
    text = json.dumps(body).lower()
    assert "password" not in text and "hash" not in text


def test_the_upload_policy_is_the_one_every_other_surface_obeys(client, intake):
    """A PDF renamed ``photo.jpg``, and a 6 MB JPEG: both refused, and neither leaves a file."""
    directory = registrations.photos_dir()
    before = set(os.listdir(directory))

    document = client.post(
        PUBLIC,
        data={"full_name": "Doc Up", "password": STRONG_PASSWORD, "role": "worker", "consent": "true"},
        files={"photo": ("cv.jpg", b"%PDF-1.4 not a photo", "image/jpeg")},
    )
    assert document.status_code == 415
    assert document.json()["detail"]["error_code"] == "not_an_image"

    huge = client.post(
        PUBLIC,
        data={"full_name": "Big Up", "password": STRONG_PASSWORD, "role": "worker", "consent": "true"},
        files={"photo": ("huge.jpg", photo() + b"\x00" * (6 * 1024 * 1024), "image/jpeg")},
    )
    assert huge.status_code == 413

    assert set(os.listdir(directory)) == before, (
        "a refused upload left a file in the intake directory"
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name IN ('Doc Up', 'Big Up')") == 0


def test_consent_is_required_and_the_version_is_recorded(client, intake):
    """The one thing a person has to have agreed to, and the proof that they agreed to *it*.

    The version lives in ``audit_log`` rather than as a column on the account: this is the
    surface that already records who asked for what, from where and when, and it is the copy
    that survives a refusal deleting the account.
    """
    refused = client.post(
        PUBLIC,
        data={"full_name": "No Consent", "password": STRONG_PASSWORD, "role": "worker"},
        files={"photo": ("photo.jpg", photo(8), "image/jpeg")},
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["error_code"] == "consent_required"

    accepted = submit(client, name="Consenting", image=photo(9), consent="yes")
    assert accepted.status_code == 200
    user_id = accepted.json()["user_id"]
    rows = harness.db_rows(
        "SELECT after_json, ip FROM audit_log WHERE action = 'user_self_registered' "
        "AND entity_id = ?",
        (user_id,),
    )
    assert len(rows) == 1, rows
    after = json.loads(rows[0][0])
    assert after["consent_version"] == registrations.CONSENT_VERSION
    assert after["status"] == "pending_approval"


def test_the_link_cannot_ask_for_a_role_no_link_may_mint(client, intake):
    for role in ("admin", "head_admin", "developer"):
        refused = submit(client, name="Aspirant", role=role, image=photo(11))
        assert refused.status_code == 400, f"{role} was accepted by a public link"
        assert refused.json()["detail"]["error_code"] == "role_not_available"

    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Aspirant'") == 0


def test_a_closed_intake_refuses_the_submission_as_well_as_the_page(client):
    """The switch has to close the *route*, not only the message on it."""
    refused = submit(client)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "registration_closed"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Walk Up'") == 0
    assert os.listdir(registrations.photos_dir()) == [], (
        "a refused submission still wrote a photo to disk"
    )


# ---------------------------------------------------------------------------
# 1b. the intake switch: the console's, starting where the deployment left it
# ---------------------------------------------------------------------------
INTAKE = f"{REVIEW}/intake"


def intake_state(client, *, as_user: str = ADMIN) -> dict:
    response = client.get(INTAKE, headers=bearer(as_user))
    assert response.status_code == 200, response.text[:300]
    return response.json()


def set_intake(client, want_open: bool, *, as_user: str = ADMIN):
    return client.post(INTAKE, headers=bearer(as_user), json={"open": want_open})


def test_an_untouched_switch_follows_the_deployment_flag(client, monkeypatch):
    """The state before anybody opens the console, which is the state every database starts in.

    A ``NULL`` row is *no decision*, not "closed": the deployment's own flag is the position the
    switch starts at, so a deployment that never touches this screen behaves exactly as it did
    before the table existed - which is the only reason adding a switch is safe. The two closed
    codes are different sentences about the same state: nothing has opened it yet, versus
    somebody closed it.
    """
    monkeypatch.setattr(settings, "registration_enabled", False)
    closed = intake_state(client)
    assert closed["accepting"] is False
    assert closed["reason"] == "closed_by_default"
    assert closed["deployment_enabled"] is False
    assert closed["decided"] is False, "nothing has been decided on a fresh database"
    assert client.get(PUBLIC).json()["enabled"] is False

    monkeypatch.setattr(settings, "registration_enabled", True)
    opened = intake_state(client)
    assert opened["accepting"] is True
    assert opened["reason"] == "open"
    assert opened["decided"] is False
    assert client.get(PUBLIC).json()["enabled"] is True


def test_an_administrator_closes_and_reopens_the_permanent_link(client, intake):
    """The whole point of the control: the form starts and stops taking submissions at once.

    The public link is one URL the company prints, so the applicant cannot be asked to notice
    which switch moved - what they get is the same closed sentence either way. What changes is
    the *route*: a closed link refuses the submission rather than accepting one the console
    believes it will not take, and the queue read says so on the screen where an operator is
    standing.
    """
    assert client.get(PUBLIC).json()["enabled"] is True

    shut = set_intake(client, False)
    assert shut.status_code == 200, shut.text[:300]
    assert shut.json()["accepting"] is False
    assert shut.json()["reason"] == "closed_by_console"
    assert shut.json()["updated_by"] == ADMIN
    assert shut.json()["decided"] is True

    assert client.get(PUBLIC).json()["enabled"] is False
    assert client.get(REVIEW, headers=bearer(ADMIN)).json()["enabled"] is False
    refused = submit(client, name="Too Late", image=photo(31))
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "registration_closed"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Too Late'") == 0
    assert db_scalar("SELECT intake_open FROM registration_settings WHERE id = 1") == 0

    again = set_intake(client, True)
    assert again.status_code == 200, again.text[:300]
    assert again.json()["accepting"] is True
    assert again.json()["reason"] == "open"
    accepted = submit(client, name="In Time", image=photo(32))
    assert accepted.status_code == 200, accepted.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'In Time'") == 1

    # Both moves are on the record: "was the form open when they applied" is a question somebody
    # asks later, and this row can only answer it for the latest change.
    rows = harness.db_rows(
        "SELECT after_json FROM audit_log WHERE action = 'registration_intake_update' ORDER BY id"
    )
    assert len(rows) == 2, rows
    assert json.loads(rows[0][0])["intake_open"] == 0
    assert json.loads(rows[1][0])["intake_open"] == 1


def test_the_console_opens_a_deployment_that_ships_closed(client):
    """The switch the applicant's own sentence points at, and it works on a fresh deployment.

    The public form's refusal tells the person holding the phone to ask their site administrator
    to open it, so an administrator has to be able to - including on the deployment that has
    never run walk-up registration before, which is exactly the one that ships with the flag off
    and the one whose first applicant sees that sentence. ``REGISTRATION_ENABLED`` is where the
    switch *starts*, not a ceiling over it: ``test_an_untouched_switch_follows_the_deployment_flag``
    above is the half that keeps a deployment from discovering it is running a public
    face-collecting form, and this is the half that makes the console's lever real.
    """
    assert settings.registration_enabled is False, "this is the shipped-off deployment"

    answer = set_intake(client, True)
    assert answer.status_code == 200, answer.text[:300]
    body = answer.json()
    assert body["accepting"] is True, "the console clicked open and the form stayed shut"
    assert body["reason"] == "open"
    assert body["deployment_enabled"] is False, "the flag is still reported as the default"
    assert body["updated_by"] == ADMIN
    assert client.get(PUBLIC).json()["enabled"] is True
    assert db_scalar("SELECT intake_open FROM registration_settings WHERE id = 1") == 1
    accepted = submit(client, name="Walk Up")
    assert accepted.status_code == 200, accepted.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Walk Up'") == 1

    # ...and the same lever closes it again, with the flag off underneath it.
    shut = set_intake(client, False)
    assert shut.json()["accepting"] is False
    assert shut.json()["reason"] == "closed_by_console"
    assert submit(client).status_code == 403


def test_the_intake_switch_is_administrators_only(client, intake):
    """It opens and closes a public face-collecting endpoint, so it is not a worker's to move."""
    for headers in (bearer(WORKER), bearer(MOALLEM), bearer(OFF_OFFICE)):
        assert_denied(
            client.get(INTAKE, headers=headers),
            endpoint=INTAKE,
            detail="a worker read the intake switch",
        )
        assert_denied(
            client.post(INTAKE, headers=headers, json={"open": False}),
            endpoint=INTAKE,
            detail="a worker closed the public registration link",
        )
    assert_denied(client.get(INTAKE), endpoint=INTAKE, detail="an anonymous caller read it")
    assert client.get(INTAKE, headers=bearer(HEAD_ADMIN)).status_code == 200
    assert client.get(PUBLIC).json()["enabled"] is True, "a refused caller changed the link"


def test_every_surface_that_reports_the_link_agrees_about_it(client, intake):
    """One switch, one answer: the form, the queue, and the console's own read.

    A screen that disagrees with the route behind it is worse than no screen - the closed note
    on the queue is drawn from the *queue read*, so if the two reads could answer differently the
    console would be reporting a link state nobody wrote.
    """
    assert set_intake(client, False).status_code == 200
    assert client.get(PUBLIC).json()["enabled"] is False
    assert client.get(REVIEW, headers=bearer(ADMIN)).json()["enabled"] is False
    assert intake_state(client)["accepting"] is False


def test_the_queue_cap_is_enforced_inside_the_insert(client, intake, monkeypatch):
    """The cap counts accounts held for approval, and it is read inside the write lock.

    A count read before the write is advice: N concurrent submissions would each read
    ``cap - 1`` and all pass.
    """
    monkeypatch.setattr(settings, "registration_pending_cap", 1)
    first = submit(client, name="First", image=photo(21))
    assert first.status_code == 200
    directory = registrations.photos_dir()
    files = set(os.listdir(directory))

    second = submit(client, name="Second", image=photo(22))
    assert second.status_code == 429
    assert second.json()["detail"]["error_code"] == "registration_queue_full"
    assert set(os.listdir(directory)) == files, "the refused submission left its upload behind"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE status = 'pending_approval'") == 1
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Second'") == 0


# ---------------------------------------------------------------------------
# 2. the quarantine: signed in, and unable to punch
# ---------------------------------------------------------------------------
def test_a_pending_account_can_sign_in_and_cannot_clock_in(client, intake):
    """The contract in one test: the account is usable, and it cannot record attendance.

    Both halves matter, and they are why the quarantine is a *status* rather than an absence of
    rows. The applicant can sign in from the second the form is sent - which is how they find
    out whether they have been approved, and how they read the notice written when they were -
    and every punch is refused by the server with a reason an administrator's approval clears.
    """
    body = submit(client, name="Waiting Worker", image=photo(201)).json()
    user_id = body["user_id"]

    signed = sign_in(client, user_id)
    assert signed.status_code == 200, signed.text[:300]
    assert signed.json()["approval_status"] == "pending_approval"
    assert signed.json()["user"]["approval_status"] == "pending_approval", (
        "the clock panel is drawn from the client's own copy of the account, so the fact has "
        "to travel inside it"
    )
    assert signed.json()["welcome"] is None, "nothing has been decided yet, so nothing is waiting"
    headers = {"Authorization": f"Bearer {signed.json()['token']}"}

    # Inside the fence on purpose: the refusal has to be about the account, not the place.
    punch = harness.clock_in(client, user_id, headers=headers)
    assert punch.status_code == 403, punch.text[:300]
    assert punch.json()["detail"]["error_code"] == "account_pending_approval"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (user_id,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (user_id,)) == 0

    # And the panel can ask. ``/worker/me/stats`` carries the same fact, read fresh, so a
    # banner cannot outlive the approval it is about.
    stats = client.get("/api/v1/worker/me/stats", headers=headers)
    assert stats.status_code == 200, stats.text[:300]
    assert stats.json()["approval_status"] == "pending_approval"


def test_approval_lifts_the_quarantine_without_a_new_sign_in(client, intake):
    """The one status change, observed from the phone the worker is already holding.

    Deliberately the *same* token on both sides of the approval: the gate is the account's
    status, so nothing about the session has to be reissued for a worker to start working - and
    a worker standing at a gate when the approval lands is not asked to sign in again.
    """
    user_id = submit(client, name="Approved Later", image=photo(202)).json()["user_id"]
    headers = {"Authorization": f"Bearer {token_for(client, user_id)}"}
    assert harness.clock_in(client, user_id, headers=headers).status_code == 403

    assert approve(client, user_id).status_code == 200
    assert db_scalar("SELECT status FROM users WHERE id = ?", (user_id,)) == "active"

    punch = harness.clock_in(client, user_id, headers=headers)
    assert punch.status_code == 200, punch.text[:300]
    assert punch.json()["status"] == "success"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (user_id,)) == 1


# ---------------------------------------------------------------------------
# 3. the review surface
# ---------------------------------------------------------------------------
def test_the_queue_is_administrators_only(client, intake):
    user_id = submit(client).json()["user_id"]
    assert_denied(client.get(REVIEW), endpoint=REVIEW, detail="a worker read the review queue")
    for headers in (bearer(WORKER), bearer(MOALLEM)):
        assert_denied(client.get(REVIEW, headers=headers), endpoint=REVIEW, detail="a non-admin read the queue")
        assert_denied(
            client.get(f"{REVIEW}/{user_id}/photo", headers=headers),
            endpoint=f"{REVIEW}/{{id}}/photo",
            detail="a non-admin fetched an applicant's face",
        )
        assert_denied(
            client.post(f"{REVIEW}/{user_id}/approve", headers=headers, json={}),
            endpoint=f"{REVIEW}/{{id}}/approve",
            detail="a non-admin approved a registration",
        )
        assert_denied(
            client.post(f"{REVIEW}/{user_id}/reject", headers=headers, json={}),
            endpoint=f"{REVIEW}/{{id}}/reject",
            detail="a non-admin rejected a registration",
        )
    # A head administrator is an administrator: no second rule for the review queue.
    assert client.get(REVIEW, headers=bearer(HEAD_ADMIN)).status_code == 200


def test_the_queue_lists_the_pending_accounts_oldest_first(client, intake):
    ids = []
    for index in range(3):
        response = submit(
            client,
            name=f"Applicant {index}",
            image=photo(40 + index),
            work_details="Stone cutting" if index == 0 else None,
        )
        assert response.status_code == 200, response.text[:300]
        ids.append(response.json()["user_id"])
    body = client.get(REVIEW, headers=bearer(ADMIN)).json()
    assert body["enabled"] is True
    assert body["pending"] == 3
    assert [item["id"] for item in body["requests"]] == ids, "the queue is a queue: oldest first"
    assert [int(value) for value in ids] == sorted(int(value) for value in ids), (
        "the allocator hands out the lowest free number, so a lexical order would put 10 before 2"
    )
    assert body["requests"][0]["has_photo"] is True
    assert body["requests"][0]["full_name"] == "Applicant 0"
    assert body["requests"][0]["work_details"] == "Stone cutting"
    assert body["requests"][0]["requested_role"] == "worker"
    assert body["requests"][0]["created_at"], "the card's waiting clock has nothing to count from"
    # The credential is not selected at all, so there is no path on which it reaches a body.
    assert "password" not in json.dumps(body).lower()
    assert str(registrations.photos_dir()) not in json.dumps(body), (
        "the queue leaked a path; the bytes are fetched through their own route"
    )


#: The console's own read of this queue. ``test_frontend_registrations_queue`` owns the panel
#: and pins the status it asks for against the constants below; what no frontend suite can do is
#: ask the *real* endpoint, because it stubs a server. This is that half, and it is here because
#: the two sides disagreeing is not hypothetical: the panel once asked for ``status=PENDING``,
#: which was not one of the statuses the endpoint knew, so the screen an administrator opened
#: said "status must be one of ..." instead of drawing the queue.
CONSOLE = Path(__file__).resolve().parents[2] / "frontend" / "admin_modules.js"


def test_the_query_the_console_sends_is_one_this_endpoint_accepts(client, intake):
    source = CONSOLE.read_text(encoding="utf-8")
    statuses = sorted(set(re.findall(r"/admin/registrations\?status=([a-z_]+)", source)))
    page = re.search(r"REGISTRATIONS_LIMIT:\s*(\d+)", source)
    assert statuses, "the console names no status, so its queue read depends on the default"
    assert page, "the console's page size is gone and this test no longer knows what it asks for"
    user_id = submit(client, name="Query Check", image=photo(41)).json()["user_id"]

    for value in statuses:
        answer = client.get(
            f"{REVIEW}?status={value}&limit={page.group(1)}", headers=bearer(ADMIN)
        )
        assert answer.status_code == 200, (
            f"the console asks for status={value}&limit={page.group(1)} and the endpoint answers "
            f"{answer.status_code}: {answer.text[:200]}"
        )
        body = answer.json()
        assert [item["id"] for item in body["requests"]] == [user_id]
        assert body["pending"] == 1, "the count the tab's badge is painted from"
        # The fields every card reads. A rename on either side is a blank line on the screen,
        # and the panel's own suite cannot see this half of it either.
        for field in ("id", "full_name", "phone", "email", "requested_role", "work_details", "created_at"):
            assert field in body["requests"][0], f"the console reads {field} and the queue stopped sending it"


def test_the_photo_route_serves_the_reference_selfie_and_only_while_it_waits(client, intake):
    """What the reviewer judges, served once, and not served once the decision is made.

    What comes back is the *reference selfie* the biometric store holds - a re-encoded thumbnail
    of the upload, because that is the copy a face is kept as - so the check is that it is a
    JPEG, not that the bytes are the ones that were posted.
    """
    user_id = submit(client, image=photo(203)).json()["user_id"]
    served = client.get(f"{REVIEW}/{user_id}/photo", headers=bearer(ADMIN))
    assert served.status_code == 200, served.text[:300]
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["cache-control"] == "no-store"
    assert served.content[:2] == b"\xff\xd8", "what was served is not a JPEG"
    assert len(served.content) > 0

    # Decided is not on this surface any more: the reviewer's window is the application, and an
    # approved worker's face is not this screen's business. The console has one sentence for it.
    assert approve(client, user_id).status_code == 200
    assert client.get(f"{REVIEW}/{user_id}/photo", headers=bearer(ADMIN)).status_code == 404


# ---------------------------------------------------------------------------
# 4. approval
# ---------------------------------------------------------------------------
def test_approval_flips_the_status_and_keeps_the_face(client, intake, app_module):
    """One compare-and-set, and everything the submission already did is left alone."""
    body = submit(client, name="Approved Worker", image=photo(51)).json()
    user_id = body["user_id"]
    expected_id = user_id
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (expected_id,)) == 1

    response = approve(client, user_id, note="ID checked at the gate.")
    assert response.status_code == 200, response.text[:300]
    answer = response.json()
    assert answer["user_id"] == expected_id
    assert answer["role"] == "worker"
    assert answer["template_written"] is True

    assert db_scalar("SELECT name FROM users WHERE id = ?", (expected_id,)) == "Approved Worker"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (expected_id,)) == "worker"
    assert db_scalar("SELECT status FROM users WHERE id = ?", (expected_id,)) == "active"
    # The password the applicant chose is the password they sign in with - approval does not
    # touch the credential, and it does not re-file the face.
    assert app_module.pwd_context.verify(
        STRONG_PASSWORD, db_scalar("SELECT password_hash FROM users WHERE id = ?", (expected_id,))
    )
    assert harness.template_exists(expected_id)

    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'registration_approved' AND entity_id = ?",
        (expected_id,),
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM users WHERE status = 'pending_approval'"
    ) == 0, "the account is still in the review queue after being decided"


def test_an_approved_account_cannot_be_approved_or_rejected_again(client, intake):
    user_id = submit(client, name="Once", image=photo(71)).json()["user_id"]
    first = approve(client, user_id)
    assert first.status_code == 200

    again = approve(client, user_id)
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "already_reviewed"

    refused = reject(client, user_id)
    assert refused.status_code == 409

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)) == 1, (
        "the second decision deleted a working account"
    )


def test_approval_tells_the_worker_the_quarantine_is_over(client, intake):
    """The one thing a worker standing at a gate cannot work out: whether they are still waiting.

    The id is not what this carries - the applicant was given that on the form, and has been
    signing in with it. What it says is that the decision went their way, and it is written in
    the same transaction as the status it is about, so a notice can never exist for an account
    that was not approved. The password is deliberately not in it: what the row could carry is a
    bcrypt hash, and the only copy of the credential is in the worker's head.
    """
    body = submit(
        client, name="Welcomed Worker", phone="+965 555 0123", image=photo(111)
    ).json()
    user_id = body["user_id"]
    assert body["template_written"] is True
    answer = approve(client, user_id).json()

    # The answer carries the contact back, so the console can say what signs this worker in.
    assert answer["phone"] == "+965 555 0123"
    assert answer["email"] == ""

    rows = harness.db_rows(
        "SELECT kind, title, body, payload, dedupe_key FROM worker_notifications "
        "WHERE worker_id = ?",
        (user_id,),
    )
    assert len(rows) == 1, "an approval must leave the worker exactly one notice"
    kind, title, body_text, payload, dedupe_key = rows[0]
    assert kind == "account_approved"
    assert user_id in title
    assert user_id in body_text, "the notice never says which account it is about"
    assert "+965 555 0123" in body_text, (
        "the sign-in route matches the contact stored on the account, so the notice has to name "
        "the value that will actually match"
    )
    assert "password you chose" in body_text, (
        "the notice must say which password, or the worker looks for one nobody gave them"
    )
    assert json.loads(payload)["worker_id"] == user_id
    assert dedupe_key == f"registration_approved:{user_id}"
    # It is the worker's notice, not the operator's: the administrator reads what to hand over
    # off the console they are standing in, and this queue is about decisions, not welcomes.
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'account_approved'"
    ) == 0

    # And it is readable by its owner, which is the point of putting it in the inbox at all.
    inbox = client.get(
        "/api/v1/worker/me/notifications", headers=bearer(user_id, role="worker")
    )
    assert inbox.status_code == 200, inbox.text[:300]
    assert inbox.json()["unread"] == 1
    assert inbox.json()["notifications"][0]["body"] == body_text


def test_the_notice_says_what_to_do_when_the_form_gave_no_contact(client, intake):
    """Both contact columns are optional, and the sign-in route matches what is stored.

    So a worker who gave neither signs in with that box left empty - which is also why the
    form's own email-or-phone input is deliberately not ``required``. An instruction that named a
    contact they never gave would send them hunting for a value that does not exist, on an
    account that is in fact working exactly as designed.
    """
    user_id = submit(client, name="No Contact", image=photo(112)).json()["user_id"]
    assert approve(client, user_id).status_code == 200

    body = str(
        db_scalar("SELECT body FROM worker_notifications WHERE worker_id = ?", (user_id,))
    )
    assert user_id in body
    assert "empty" in body.lower(), f"nothing says what to do with the box: {body!r}"
    assert "did not give us one" in body, body


def test_the_sign_in_that_opens_the_account_hands_the_notice_over(client, intake):
    """The inbox waits behind a tab; the first screen of a decided account does not.

    A notice waiting in the inbox is the right shape for "your shift ran past the paid day" and
    the wrong one for the single fact a worker is standing at a gate wanting: that they are no
    longer waiting. ``POST /auth/login`` therefore answers with the notice itself while it is
    still unread (``notifications.worker_welcome``): signing in is the one moment this
    application knows the reader has just arrived, and the answer is what the first screen is
    painted from.

    Read state - not a login counter - is what "first" means here, so the same welcome comes back
    on a reload, a second phone or a reinstall, all of which a per-device flag would lose; and it
    stops for good the moment the notice is read, which is what keeps it from appearing on every
    sign-in for the rest of the account's life. A sign-in *before* the decision has nothing
    waiting, which is what ``test_a_pending_account_can_sign_in_and_cannot_clock_in`` pins.
    """
    user_id = submit(
        client, name="Handed Over", phone="+965 555 0199", image=photo(113)
    ).json()["user_id"]
    assert approve(client, user_id).status_code == 200

    def sign_in_again():
        return client.post(
            LOGIN,
            json={
                "user_id": user_id,
                "email_or_phone": "+965 555 0199",
                "password": STRONG_PASSWORD,
            },
        )

    first = sign_in_again()
    assert first.status_code == 200, first.text[:300]
    assert first.json()["approval_status"] == "active"
    welcome = first.json()["welcome"]
    assert welcome, "the first sign-in of an approved worker says nothing about the account"
    assert welcome["kind"] == "account_approved"
    assert user_id in welcome["title"], welcome
    assert "+965 555 0199" in welcome["body"], (
        "the card and the sentence have to agree about what signs this worker in"
    )
    notice_id = welcome["notice_id"]
    assert db_scalar(
        "SELECT COUNT(*) FROM worker_notifications WHERE id = ? AND worker_id = ? "
        "AND read_at IS NULL",
        (notice_id, user_id),
    ) == 1, "the welcome points at a row that is not this worker's own unread notice"

    # Unread is the gate, so a second sign-in hands over the same row rather than a second one:
    # nothing about signing in writes a notice, and the worker is told the same thing twice.
    again = sign_in_again().json()["welcome"]
    assert again and again["notice_id"] == notice_id, again
    assert db_scalar("SELECT COUNT(*) FROM worker_notifications WHERE worker_id = ?", (user_id,)) == 1

    # What the card's one button does. With the notice read there is nothing waiting, and the
    # answer says so - which is the whole reason the card appears once rather than every day.
    read = client.post(
        f"/api/v1/worker/me/notifications/read?notification_id={notice_id}",
        headers=bearer(user_id, role="worker"),
    )
    assert read.status_code == 200, read.text[:300]
    assert sign_in_again().json()["welcome"] is None

    # Not a channel for the administrative tiers: the console handed that account over in
    # person, and a welcome here would be the server inventing a first day somebody did not have.
    admin = client.post(
        LOGIN,
        json={
            "user_id": ADMIN,
            "email_or_phone": harness.EMAILS[ADMIN],
            "password": harness.PASSWORDS[ADMIN],
        },
    )
    assert admin.status_code == 200, admin.text[:300]
    assert admin.json()["welcome"] is None


def test_a_moallem_is_registered_with_the_role_it_asked_for(client, intake):
    """A walk-up may ask to be a moallem. Its id no longer says so - the row does."""
    user_id = submit(client, name="Lead Applicant", role="moallem", image=photo(81)).json()["user_id"]
    response = approve(client, user_id)
    assert response.status_code == 200, response.text[:300]
    assert response.json()["role"] == "moallem"
    assert int(user_id) < registrations.ADMIN_TIER_ID_FLOOR, (
        "a public form minted an id in an administrative tier"
    )
    assert db_scalar("SELECT role FROM users WHERE id = ?", (user_id,)) == "moallem"


def test_an_off_office_worker_is_registered_with_the_role_it_asked_for(client, intake):
    user_id = submit(client, name="Off-Office Applicant", role="off_office", image=photo(82)).json()[
        "user_id"
    ]
    response = approve(client, user_id)
    assert response.status_code == 200, response.text[:300]
    assert response.json()["role"] == "off_office"
    assert int(user_id) < registrations.ADMIN_TIER_ID_FLOOR
    assert db_scalar("SELECT role FROM users WHERE id = ?", (user_id,)) == "off_office"


def test_an_approved_worker_can_clock_in(client, intake):
    """The end of the chain: the account, the template, and a punch judged against it."""
    user_id = submit(client, name="Punching Worker", image=photo(91)).json()["user_id"]
    assert approve(client, user_id).status_code == 200

    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (user_id,)) == 0
    punch = harness.clock_in(client, user_id, headers=bearer(user_id, role="worker"))
    assert punch.status_code == 200, punch.text[:300]
    assert punch.json()["status"] == "success"


def test_an_approved_account_with_no_face_reference_is_reported(client, intake):
    """A reference that could not be written is a fact about the account, read at the decision.

    The submission's own failure path leaves the staged photograph in place and raises an
    operator alert (see ``submit_registration``), but the account is real either way - so the
    approval says whether it can actually be used, rather than reporting success and leaving
    somebody to discover it at a gate.
    """
    user_id = submit(client, name="Faceless", image=photo(204)).json()["user_id"]
    biometrics.remove_files(user_id)

    answer = approve(client, user_id)
    assert answer.status_code == 200, answer.text[:300]
    assert answer.json()["template_written"] is False
    assert db_scalar("SELECT status FROM users WHERE id = ?", (user_id,)) == "active", (
        "a missing face reference is a warning, not a reason to refuse the approval"
    )


# ---------------------------------------------------------------------------
# 5. rejection
# ---------------------------------------------------------------------------
def test_rejection_deletes_the_account_and_destroys_the_face(client, intake):
    """Not a product state on a row that survives - a deletion, with the reason kept.

    An account cannot be left behind the way a request was: it can sign in, it is a name on the
    roster, and it holds a face reference. So the refusal takes all of it, and the decision
    survives in ``audit_log`` - which is the copy a deleted row could never be.
    """
    user_id = submit(client, name="Refused", image=photo(101)).json()["user_id"]
    assert harness.template_exists(user_id)

    response = reject(client, user_id, note="Photograph is not clear enough.")
    assert response.status_code == 200, response.text[:300]
    assert response.json()["photo_destroyed"] is True

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)) == 0
    assert not harness.template_exists(user_id), "the refused applicant's face is still on disk"
    assert db_scalar("SELECT COUNT(*) FROM worker_notifications WHERE worker_id = ?", (user_id,)) == 0
    # The notice that somebody was waiting goes with the account: an alert nobody can act on any
    # more is how a queue screen comes to look broken.
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE worker_id = ? "
        "AND kind = 'registration_submitted'",
        (user_id,),
    ) == 0
    rows = harness.db_rows(
        "SELECT before_json, after_json FROM audit_log "
        "WHERE action = 'registration_rejected_purged' AND entity_id = ?",
        (user_id,),
    )
    assert len(rows) == 1, rows
    assert json.loads(rows[0][0])["name"] == "Refused"
    assert json.loads(rows[0][1])["note"] == "Photograph is not clear enough."
    # And the account is gone from the sign-in surface as well as the roster.
    assert sign_in(client, user_id).status_code == 401


def test_a_refused_applicant_may_apply_again_and_gets_the_number_back(client, intake):
    """The refusal is what frees the number, and what makes reusing it safe.

    Every other cell in the allocator's contract is downstream of this: it hands out the lowest
    free number, so the number a refusal released is the number the next applicant gets - and
    the reason that is not a face being inherited is that ``remove_files`` ran first.
    """
    first = submit(client, name="Try One", image=photo(111)).json()["user_id"]
    assert reject(client, first).status_code == 200

    again = submit(client, name="Try Two", image=photo(112))
    assert again.status_code == 200, again.text[:300]
    assert again.json()["user_id"] == first, "the number a refusal freed was not reused"
    assert db_scalar("SELECT name FROM users WHERE id = ?", (first,)) == "Try Two"
    assert harness.template_exists(first)
    # The face under that id is the new applicant's, not the one the refusal wiped.
    reference = json.loads(open(biometrics.resolve_reference(first), encoding="utf-8").read())
    assert len(reference["embedding"]) > 0


def test_the_lowest_free_number_is_handed_out(client, intake):
    """Not the highest-plus-one: a number a refusal freed is a number the next person gets."""
    taken = {
        int(row[0])
        for row in harness.db_rows(
            "SELECT id FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND ?",
            (registrations.WORKFORCE_ID_CEILING,),
        )
    }
    expected = 1
    while expected in taken:
        expected += 1
    assert expected <= registrations.WORKFORCE_ID_CEILING, (
        "the band has no room in this fixture, so this test would not be measuring the allocator"
    )
    body = submit(client, name="Numbered", image=photo(301)).json()
    assert body["user_id"] == str(expected)


# ---------------------------------------------------------------------------
# 6. the sweep
# ---------------------------------------------------------------------------
def test_an_abandoned_intake_photo_is_swept_and_a_fresh_one_is_not(app_module):
    """The intake directory is a staging area, and the window is what makes it survivable.

    Every submission removes its own upload once the reference has a home, so a file left here
    means a crash or a reference that could not be written - and the second is kept *on purpose*
    so the account can be enrolled from the console. The stale window is the line between the
    two: a photograph nothing is going to look at is residue, and a fresh one may be somebody's
    last chance to be enrolled.
    """
    directory = registrations.photos_dir()
    old = datetime.now().timestamp() - 30 * 24 * 3600
    abandoned = os.path.join(directory, "req-abandoned.jpg")
    fresh = os.path.join(directory, "req-fresh.jpg")
    for path in (abandoned, fresh):
        with open(path, "wb") as handle:
            handle.write(photo())
    os.utime(abandoned, (old, old))

    assert registrations.sweep_orphan_photos() == 1
    assert not os.path.exists(abandoned), "the leftover was not removed"
    assert os.path.exists(fresh), "the sweep removed an upload younger than the stale window"


# ---------------------------------------------------------------------------
# 7. the link is the door
# ---------------------------------------------------------------------------
def test_a_request_without_the_link_is_refused(client, intake):
    """No token is not "closed": it is an address that was never issued to anybody.

    The distinction is the feature. A closed form is a form, and the applicant is owed a
    sentence about it; a URL with no link behind it is a stranger who should not be looking at
    this surface at all, and it is refused before the policy is answered rather than after.
    """
    reading = client.get(PUBLIC_BARE)
    assert reading.status_code == 404
    assert reading.json()["detail"]["error_code"] == "registration_link_invalid"

    writing = client.post(
        PUBLIC_BARE,
        data={"full_name": "Nobody", "password": STRONG_PASSWORD, "role": "worker", "consent": "true"},
        files={"photo": ("photo.jpg", photo(900), "image/jpeg")},
    )
    assert writing.status_code == 404
    assert writing.json()["detail"]["error_code"] == "registration_link_invalid"
    assert harness.db_rows("SELECT id FROM users WHERE name = ?", ("Nobody",)) == [], (
        "a refused request with no link created something"
    )


def test_a_token_for_a_generation_that_was_never_current_is_refused(client, intake):
    """The signature is over the generation, so a fabricated token cannot be replayed.

    ``generation + 1`` is the next link rather than an arbitrary string, and it is the sharpest
    case: it is a *real* token shape for a link this deployment will mint the moment somebody
    replaces the current one, and it is refused until then.
    """
    future = f"/api/v1/register/{registrations.link_token(registrations.link_generation() + 1)}"
    assert client.get(future).status_code == 404
    assert client.get(PUBLIC).status_code == 200, (
        "refusing the next link must not have disturbed the current one"
    )


def test_the_link_is_readable_by_an_administrator_and_by_nobody_else(client, intake):
    """Handing out the company's own application form is an operator's decision.

    The read is the dangerous half of the pair - a link copied out of this response is how a
    stranger gets a form - so the audience is ``admin_only``, the same audience the switch beside
    it has, and a non-administrator is refused rather than shown a URL.
    """
    allowed = client.get(LINK, headers=bearer(ADMIN))
    assert allowed.status_code == 200, allowed.text[:300]
    body = allowed.json()
    assert body["url"].startswith("http")
    assert body["url"].endswith(registrations.link_token())
    assert body["accepting"] is True, "the switch is open in this fixture"
    assert body["reason"] == "open"

    for who in (WORKER, MOALLEM, OFF_OFFICE):
        headers = bearer(who)
        assert_denied(
            client.get(LINK, headers=headers),
            endpoint=LINK,
            detail=f"{who} read the registration link",
        )
        assert_denied(
            client.post(LINK, headers=headers),
            endpoint=LINK,
            detail=f"{who} replaced the registration link",
        )
    assert_denied(
        client.get(LINK), endpoint=LINK, detail="an anonymous caller read the link"
    )


def test_replacing_the_link_kills_every_copy_of_the_old_one_and_keeps_the_queue(
    client, intake
):
    """One action, and the two things it must not do are the two that matter.

    The link has no per-person identity to revoke against, so the revocation is the link: the
    old address stops answering, the new one answers, and nothing already submitted is touched -
    the applicants under the old link have accounts, photographs and a place in the queue, and
    replacing a URL must not be a way to lose them.
    """
    waiting = submit(client, name="Before Replace", image=photo(401)).json()["user_id"]

    replaced = client.post(LINK, headers=bearer(ADMIN))
    assert replaced.status_code == 200, replaced.text[:300]
    fresh = replaced.json()
    assert fresh["generation"] == registrations.link_generation()
    new_path = "/api/v1/register/" + fresh["url"].rsplit("/register/", 1)[1]
    assert new_path != PUBLIC

    assert client.get(PUBLIC).status_code == 404, "the link that was already sent still works"
    assert client.get(new_path).status_code == 200, "the replacement does not work"

    still_waiting = [row["id"] for row in client.get(REVIEW, headers=bearer(ADMIN)).json()["requests"]]
    assert waiting in still_waiting, (
        "replacing the link threw away the applications already waiting"
    )
    assert db_scalar("SELECT name FROM users WHERE id = ?", (waiting,)) == "Before Replace"


def test_a_lost_race_for_the_number_is_retried_not_handed_to_the_applicant(
    client, intake, monkeypatch
):
    """Ten phones on one link do not take turns, so the insert can be the loser.

    ``BEGIN IMMEDIATE`` serializes the submissions that go through it, so the scan-then-insert
    is safe in the common case and needs no retry at all. What this pins is the uncommon one: a
    number taken between the scan and the insert. The wrong answer there is ``409`` telling
    somebody who has sent the form once to send it again - the applicant has one photograph and
    one password already in hand, and the lost race was the server's to absorb (see
    ``ALLOCATION_ATTEMPTS``).
    """
    real = registrations._next_workforce_id
    asked: list[int] = []

    def racing(conn):
        asked.append(1)
        # The seeded worker's number: the roster already carries it, so the first insert is the
        # one that loses, exactly as it would if another writer had just committed it.
        return WORKER if len(asked) == 1 else real(conn)

    # Read before the submission: the account about to be created is what takes this number, so
    # asking afterwards would be asking for the one *after* it.
    expected = lowest_free_id()
    monkeypatch.setattr(registrations, "_next_workforce_id", racing)
    answer = submit(client, name="Racer", image=photo(402))
    assert answer.status_code == 200, answer.text[:300]
    assert len(asked) >= 2, "a lost race was not retried"
    assert answer.json()["user_id"] == str(expected), (
        "the retry did not hand out the lowest free number"
    )
