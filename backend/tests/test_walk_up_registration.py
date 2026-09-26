"""Walk-up registration: one permanent public link, and an administrator's decision in front of it.

WHY THIS EXISTS
---------------
The registration link that already ships is *per person*: an administrator types the name, the
role and the account id, and the link reserves that id when it is created. That is the right
shape for one named hire and the wrong one for a walk-up, where nobody has applied yet and so
there is no id to reserve. This suite is about the second shape, and every assertion in it is a
way that shape could be wrong and silent:

1. **Nothing exists before the decision.** A submission writes a request and nothing else - no
   ``users`` row, no roster entry, no shift, no payroll row. The only thing that creates an
   account is an administrator's approval, and it creates it atomically.
2. **The public route is a public route.** It is the one endpoint in the application that
   accepts a face from nobody in particular, so: off by default, rate limited, capped, and it
   runs no model at all (a public endpoint that runs an ONNX inference is a way to keep the one
   vCPU that also serves the gate busy for free).
3. **The upload obeys the one policy.** 5 MB, enforced *while reading*, and the type decided
   from the bytes - so a PDF renamed ``photo.jpg`` is refused and so is a 6 MB JPEG, on the
   surface that has no session behind it.
4. **One photograph is one pending request.** The same bytes cannot fill the review queue with
   copies of themselves, and a refusal leaves no file behind either.
5. **The number is allocated at approval, inside the transaction that creates the account**, and
   a second administrator clicking approve gets a conflict rather than a second account.
6. **The face is destroyed by the decision.** A rejected request's photo is wiped (an intake
   funnel that keeps the faces it turned down would contradict the retention story this system
   tells about every other face), and an approved one is destroyed too once the reference
   template has been written - the face lives in the biometric store, under an immutable id.
"""

from __future__ import annotations

import io
import json
import os
import random
from datetime import datetime

import pytest
from PIL import Image

import database
import harness
import registrations
from config import settings
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, assert_denied, bearer, db_scalar

STRONG_PASSWORD = "site-attendance-2026"
PUBLIC = "/api/v1/register"
REVIEW = "/api/v1/admin/registrations"


def photo(seed: int = 0, size: tuple[int, int] = (240, 240)) -> bytes:
    """A valid JPEG whose *bytes* differ per seed.

    Seeded noise rather than a flat colour, and that is load-bearing rather than fussy: a
    uniformly flat image quantises a shade apart to the *same* coefficients, so two "different"
    flat colours came out byte-identical and the duplicate-upload rule fired on them - which is
    how this helper was found to be wrong. The pending-photo unique index is keyed on the digest,
    so a helper that can collide quietly makes every test about that rule meaningless.
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


def stored_photo(request_id: int) -> str:
    return str(db_scalar("SELECT photo_path FROM registration_requests WHERE id = ?", (request_id,)) or "")


def lowest_free_worker() -> int:
    taken = {
        int(row[0])
        for row in harness.db_rows("SELECT id FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND 499")
    }
    return next(value for value in range(1, 500) if value not in taken)


def approve(client, request_id: int, *, as_user: str = ADMIN, note: str | None = None):
    payload = {} if note is None else {"note": note}
    return client.post(f"{REVIEW}/{request_id}/approve", headers=bearer(as_user), json=payload)


def reject(client, request_id: int, *, as_user: str = ADMIN, note: str | None = None):
    payload = {} if note is None else {"note": note}
    return client.post(f"{REVIEW}/{request_id}/reject", headers=bearer(as_user), json=payload)


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


def test_a_submission_creates_a_request_and_never_an_account(client, intake, app_module):
    response = submit(client)
    assert response.status_code == 200, response.text[:300]
    request_id = response.json()["request_id"]

    assert db_scalar("SELECT status FROM registration_requests WHERE id = ?", (request_id,)) == "PENDING_REVIEW"
    assert db_scalar("SELECT full_name FROM registration_requests WHERE id = ?", (request_id,)) == "Walk Up"
    assert db_scalar("SELECT requested_role FROM registration_requests WHERE id = ?", (request_id,)) == "worker"
    assert db_scalar("SELECT assigned_id FROM registration_requests WHERE id = ?", (request_id,)) is None

    # The credential is hashed on the way in and never stored in the clear.
    stored = db_scalar("SELECT password_hash FROM registration_requests WHERE id = ?", (request_id,))
    assert stored and stored != STRONG_PASSWORD
    assert app_module.pwd_context.verify(STRONG_PASSWORD, stored)

    # The photo is on disk, under a name that says nothing about the person.
    path = stored_photo(request_id)
    assert path and os.path.exists(path)
    assert os.path.basename(path).startswith("req-")
    assert os.path.dirname(path) == registrations.photos_dir()

    # **Nothing** became an employee: no account, and no face template anywhere.
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Walk Up'") == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND 499 AND name = 'Walk Up'"
    ) == 0

    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'registration_submitted' AND entity_id = ?",
        (str(request_id),),
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'registration_submitted'"
    ) == 1


def test_the_public_response_never_carries_a_credential(client, intake):
    body = submit(client).json()
    assert "password" not in json.dumps(body).lower()
    assert "hash" not in json.dumps(body).lower()
    assert not any("id" in key.lower() for key in body if key != "request_id"), (
        "a submission must not be told an account id; the number is allocated at approval"
    )


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
        "a refused upload left a file in the reviewers' directory"
    )
    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 0


def test_the_same_photograph_cannot_become_two_pending_requests(client, intake):
    """The unique index is what stops one JPEG filling the queue with copies of itself."""
    image = photo(7)
    first = submit(client, name="First", image=image)
    assert first.status_code == 200
    directory = registrations.photos_dir()
    files_after_first = set(os.listdir(directory))

    second = submit(client, name="Second", image=image)
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "registration_duplicate"

    assert set(os.listdir(directory)) == files_after_first, (
        "the duplicate submission's file was left behind, so the refusal leaked a face"
    )
    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 1


def test_consent_is_required_and_the_wording_is_recorded(client, intake):
    refused = client.post(
        PUBLIC,
        data={"full_name": "No Consent", "password": STRONG_PASSWORD, "role": "worker"},
        files={"photo": ("photo.jpg", photo(8), "image/jpeg")},
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["error_code"] == "consent_required"

    accepted = submit(client, name="Consenting", image=photo(9), consent="yes")
    assert accepted.status_code == 200
    request_id = accepted.json()["request_id"]
    assert db_scalar("SELECT consent_version FROM registration_requests WHERE id = ?", (request_id,)) == (
        registrations.CONSENT_VERSION
    )
    assert db_scalar("SELECT consent_at FROM registration_requests WHERE id = ?", (request_id,))


def test_the_link_cannot_ask_for_a_role_no_link_may_mint(client, intake):
    for role in ("admin", "head_admin", "developer"):
        refused = submit(client, name="Aspirant", role=role, image=photo(11))
        assert refused.status_code == 400, f"{role} was accepted by a public link"
        assert refused.json()["detail"]["error_code"] == "role_not_available"

    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 0


def test_a_closed_intake_refuses_the_submission_as_well_as_the_page(client):
    """The switch has to close the *route*, not only the message on it."""
    refused = submit(client)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "registration_closed"
    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 0
    assert os.listdir(registrations.photos_dir()) == [], (
        "a refused submission still wrote a photo to disk"
    )


def test_the_queue_cap_is_enforced_inside_the_insert(client, intake, monkeypatch):
    monkeypatch.setattr(settings, "registration_pending_cap", 1)
    first = submit(client, name="First", image=photo(21))
    assert first.status_code == 200
    directory = registrations.photos_dir()
    files = set(os.listdir(directory))

    second = submit(client, name="Second", image=photo(22))
    assert second.status_code == 429
    assert second.json()["detail"]["error_code"] == "registration_queue_full"
    assert set(os.listdir(directory)) == files
    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 1


# ---------------------------------------------------------------------------
# 2. the review surface
# ---------------------------------------------------------------------------
def test_the_queue_is_administrators_only(client, intake):
    request_id = submit(client).json()["request_id"]
    assert_denied(client.get(REVIEW), endpoint=REVIEW, detail="a worker read the review queue")
    for headers in (bearer(WORKER), bearer(MOALLEM)):
        assert_denied(client.get(REVIEW, headers=headers), endpoint=REVIEW, detail="a non-admin read the queue")
        assert_denied(
            client.get(f"{REVIEW}/{request_id}/photo", headers=headers),
            endpoint=f"{REVIEW}/{{id}}/photo",
            detail="a non-admin fetched an applicant's photograph",
        )
        assert_denied(
            client.post(f"{REVIEW}/{request_id}/approve", headers=headers, json={}),
            endpoint=f"{REVIEW}/{{id}}/approve",
            detail="a non-admin approved a registration",
        )
        assert_denied(
            client.post(f"{REVIEW}/{request_id}/reject", headers=headers, json={}),
            endpoint=f"{REVIEW}/{{id}}/reject",
            detail="a non-admin rejected a registration",
        )
    # A head administrator is an administrator: no second rule for the review queue.
    assert client.get(REVIEW, headers=bearer(HEAD_ADMIN)).status_code == 200


def test_the_queue_lists_what_is_pending_oldest_first(client, intake):
    ids = []
    for index in range(3):
        response = submit(client, name=f"Applicant {index}", image=photo(40 + index))
        assert response.status_code == 200, response.text[:300]
        ids.append(response.json()["request_id"])
    body = client.get(REVIEW, headers=bearer(ADMIN)).json()
    assert body["enabled"] is True
    assert body["pending"] == 3
    assert [item["id"] for item in body["requests"]] == ids, "the queue is a queue: oldest first"
    assert body["requests"][0]["has_photo"] is True
    # The credential is not selected at all, so there is no path on which it reaches a body.
    assert "password" not in json.dumps(body).lower()
    assert str(registrations.photos_dir()) not in json.dumps(body), (
        "the queue leaked the photo's path; the bytes are fetched through their own route"
    )


def test_the_photo_route_serves_the_bytes_and_stops_when_they_are_gone(client, intake):
    request_id = submit(client).json()["request_id"]
    served = client.get(f"{REVIEW}/{request_id}/photo", headers=bearer(ADMIN))
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["cache-control"] == "no-store"
    assert served.content == photo()

    assert reject(client, request_id).status_code == 200
    gone = client.get(f"{REVIEW}/{request_id}/photo", headers=bearer(ADMIN))
    assert gone.status_code == 404


# ---------------------------------------------------------------------------
# 3. approval
# ---------------------------------------------------------------------------
def test_approval_creates_the_account_with_the_lowest_free_id_and_files_the_face(client, intake, app_module):
    request_id = submit(client, name="Approved Worker", image=photo(51)).json()["request_id"]
    expected_id = lowest_free_worker()
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (str(expected_id),)) == 0

    response = approve(client, request_id, note="ID checked at the gate.")
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["worker_id"] == str(expected_id)
    assert body["role"] == "worker"
    assert body["template_written"] is True

    assert db_scalar("SELECT name FROM users WHERE id = ?", (str(expected_id),)) == "Approved Worker"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (str(expected_id),)) == "worker"
    assert db_scalar("SELECT status FROM users WHERE id = ?", (str(expected_id),)) == "active"
    assert db_scalar("SELECT biometric_id FROM users WHERE id = ?", (str(expected_id),)), (
        "the account was created without an immutable biometric id"
    )
    # The password the applicant chose is the password they sign in with.
    assert app_module.pwd_context.verify(
        STRONG_PASSWORD, db_scalar("SELECT password_hash FROM users WHERE id = ?", (str(expected_id),))
    )
    # The face is where a punch looks for it.
    assert harness.template_exists(str(expected_id))

    assert db_scalar("SELECT status FROM registration_requests WHERE id = ?", (request_id,)) == "APPROVED"
    assert db_scalar("SELECT assigned_id FROM registration_requests WHERE id = ?", (request_id,)) == str(expected_id)
    assert db_scalar("SELECT reviewed_by FROM registration_requests WHERE id = ?", (request_id,)) == ADMIN
    assert db_scalar("SELECT decision_note FROM registration_requests WHERE id = ?", (request_id,)) == (
        "ID checked at the gate."
    )
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'registration_approved' AND entity_id = ?",
        (str(request_id),),
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM registration_requests WHERE status = 'PENDING_REVIEW'"
    ) == 0, "the request is still in the review queue after being decided"


def issue_register_link(client, *, user_id, name="Invited Worker", role="worker", as_user=ADMIN):
    return client.post(
        "/api/v1/admin/enrollment/invites",
        headers=bearer(as_user),
        json={"worker_id": user_id, "kind": "register", "name": name, "role": role},
    )


def test_a_walk_up_approval_never_takes_an_id_a_live_invite_reserved(client, intake, jpeg):
    """Both creation flows share one allocator, so a number a link is holding is not free.

    The invite reserves an id at issue and keeps it only on the invite row; the walk-up
    allocates at approval and scans ``users``. Without a shared set of reservations the
    allocator calls the reserved number free and spends it, and the link the administrator
    already sent to a real person is a dead end by the time they open it.
    """
    taken = {
        int(row[0])
        for row in harness.db_rows(
            "SELECT id FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND 499"
        )
    }
    free = [value for value in range(1, 500) if value not in taken]
    reserved, expected = str(free[0]), str(free[1])

    issued = issue_register_link(client, user_id=reserved)
    assert issued.status_code == 200, issued.text[:300]
    token = issued.json()["token"]

    request_id = submit(client).json()["request_id"]
    approved = approve(client, request_id)
    assert approved.status_code == 200, approved.text[:300]
    assert approved.json()["worker_id"] == expected, (
        "the walk-up was handed the id a live registration link was holding"
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (reserved,)) == 0

    # The reservation survived the walk-up: the link still creates its own account, under
    # exactly the id it was promised, once its holder opens it.
    claimed = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert claimed.status_code == 200, claimed.text[:300]
    assert claimed.json()["worker_id"] == reserved


def test_approval_destroys_the_reviewed_photograph_once_the_face_has_a_home(client, intake):
    """The face now lives under an immutable id; a second copy nothing sweeps must not."""
    request_id = submit(client, name="Wiped", image=photo(61)).json()["request_id"]
    path = stored_photo(request_id)
    assert os.path.exists(path)

    assert approve(client, request_id).status_code == 200

    assert stored_photo(request_id) == ""
    assert not os.path.exists(path)
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'registration_photo_removed' AND entity_id = ?",
        (str(request_id),),
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'enrollment_completed'"
    ) == 0, "a successful approval is not a warning"


def test_an_approved_request_cannot_be_approved_or_rejected_again(client, intake):
    request_id = submit(client, name="Once", image=photo(71)).json()["request_id"]
    first = approve(client, request_id)
    assert first.status_code == 200
    worker_id = first.json()["worker_id"]

    again = approve(client, request_id)
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "already_reviewed"

    refused = reject(client, request_id)
    assert refused.status_code == 409

    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (worker_id,)) == 1, (
        "the second approval created a second account"
    )
    assert db_scalar("SELECT COUNT(*) FROM registration_requests") == 1


def test_a_moallem_is_approved_into_the_lead_worker_block(client, intake):
    """An applicant may ask to be a moallem, and a moallem's number is in the 500s."""
    request_id = submit(client, name="Lead Applicant", role="moallem", image=photo(81)).json()["request_id"]
    response = approve(client, request_id)
    assert response.status_code == 200, response.text[:300]
    worker_id = int(response.json()["worker_id"])
    assert 500 <= worker_id <= 749, "the lead-worker block ends where the new role's begins"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (str(worker_id),)) == "moallem"


def test_an_off_office_worker_is_approved_into_the_upper_block(client, intake):
    """The old moallem band's upper half is a role of its own, with numbers of its own."""
    request_id = submit(
        client, name="Off-Office Applicant", role="off_office", image=photo(82)
    ).json()["request_id"]
    response = approve(client, request_id)
    assert response.status_code == 200, response.text[:300]
    worker_id = int(response.json()["worker_id"])
    assert 750 <= worker_id <= 999, "an off-office worker is never handed a lead worker's number"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (str(worker_id),)) == "off_office"


def test_an_approved_worker_can_clock_in(client, intake):
    """The end of the chain: the account, the template, and a punch that is judged against it."""
    request_id = submit(client, name="Punching Worker", image=photo(91)).json()["request_id"]
    worker_id = approve(client, request_id).json()["worker_id"]

    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (worker_id,)) == 0
    punch = harness.clock_in(
        client, worker_id, headers=bearer(worker_id, role="worker")
    )
    assert punch.status_code == 200, punch.text[:300]
    assert punch.json()["status"] == "success"


# ---------------------------------------------------------------------------
# 4. rejection
# ---------------------------------------------------------------------------
def test_rejection_keeps_the_record_and_destroys_the_face_and_the_credential(client, intake):
    request_id = submit(client, name="Refused", image=photo(101)).json()["request_id"]
    path = stored_photo(request_id)
    assert os.path.exists(path)

    response = reject(client, request_id, note="Photograph is not clear enough.")
    assert response.status_code == 200, response.text[:300]
    assert response.json()["photo_destroyed"] is True

    assert db_scalar("SELECT status FROM registration_requests WHERE id = ?", (request_id,)) == "REJECTED"
    assert db_scalar("SELECT decision_note FROM registration_requests WHERE id = ?", (request_id,)) == (
        "Photograph is not clear enough."
    )
    assert db_scalar("SELECT password_hash FROM registration_requests WHERE id = ?", (request_id,)) == ""
    assert stored_photo(request_id) == ""
    assert not os.path.exists(path), "the photograph of a refused applicant is still on disk"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE name = 'Refused'") == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'registration_rejected' AND entity_id = ?",
        (str(request_id),),
    ) == 1


def test_a_refused_applicant_may_apply_again_with_the_same_photograph(client, intake):
    """The uniqueness rule stops at the decision - otherwise a refusal is permanent."""
    image = photo(111)
    first = submit(client, name="Try One", image=image).json()["request_id"]
    assert reject(client, first).status_code == 200

    again = submit(client, name="Try Two", image=image)
    assert again.status_code == 200, again.text[:300]
    assert again.json()["request_id"] != first


# ---------------------------------------------------------------------------
# 5. the sweep
# ---------------------------------------------------------------------------
def test_a_photo_with_no_request_behind_it_is_swept_and_a_live_one_is_not(app_module):
    directory = registrations.photos_dir()
    old = datetime.now().timestamp() - 30 * 24 * 3600
    orphan = os.path.join(directory, "orphan.jpg")
    live = os.path.join(directory, "live.jpg")
    for path in (orphan, live):
        with open(path, "wb") as handle:
            handle.write(photo())
        os.utime(path, (old, old))

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with database.db(write=True) as conn:
        conn.execute(
            "INSERT INTO registration_requests (status, full_name, requested_role, photo_path, "
            "photo_sha256, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("PENDING_REVIEW", "Live Applicant", "worker", live, "0" * 8, stamp, stamp),
        )

    assert registrations.sweep_orphan_photos() == 1
    assert not os.path.exists(orphan), "the leftover was not removed"
    assert os.path.exists(live), (
        "the sweep removed a photograph a pending request still points at - that is somebody's "
        "evidence"
    )
