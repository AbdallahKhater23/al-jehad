"""Creating an account: in the console, or from a link the admin sends.

WHY THIS EXISTS
---------------
Two doors into the same room. An administrator can create an account in the Credentials
tab - the id, the name, the role and a generated password, plus a photo that becomes the
face reference that lets the person clock in at all. Or the administrator can send a
one-time link and let the person do it themselves: they choose their own password and
take their own photo, while the id and the role stay the administrator's decision.

What this file pins, because each of these is a way the feature could be wrong and silent:

1. **Every upload obeys one policy - 5 MB, and only a real photo.** ``uploads.py`` decides
   from the bytes, not from the ``Content-Type`` or the filename, and it refuses *while
   reading* rather than after buffering the whole body. So a PDF renamed ``photo.jpg`` is
   refused, and so is a 6 MB JPEG, on every surface that takes a photo.
2. **A created account is a working account.** The reference file lands where
   ``/attendance/verify`` looks for it, and the worker can clock in - or, with no photo,
   the account exists and honestly cannot clock in yet.
3. **A failure costs a retry, not the link.** A link is consumed by a *successful*
   registration, so a rejected password or an unreadable photo leaves it usable.
4. **The link cannot mint what the admin would not hand out.** The id and the role come
   from the row the administrator wrote; the visitor supplies only a password and a face,
   and a link can never create an administrator or take an id that is already in use.
5. **The public peek leaks nothing.** ``GET /enroll/{token}`` masks the name, because
   that endpoint is reachable by anyone holding a forwarded link and the URL sits in a
   browser history.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3

import pytest
from PIL import Image

import harness
from harness import ADMIN, HEAD_ADMIN, MOALLEM, WORKER, assert_denied, bearer, db_scalar

#: The policy, restated here so a change to it fails a test rather than a deployment.
LIMIT_BYTES = 5 * 1024 * 1024

CREATED_WORKER = "42"
#: Its own id: the reference directory outlives the database reset between tests (it is a
#: directory of files, not a snapshot), so "no template on file" only means anything for an
#: id no other test has enrolled.
UNENROLLED_WORKER = "43"
CREATED_MOALLEM = "642"
LINK_WORKER = "77"
STRONG_PASSWORD = "site-attendance-2026"


def photo(size=(240, 240), fmt="JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (128, 132, 136)).save(buffer, format=fmt)
    return buffer.getvalue()


def a_pdf() -> bytes:
    """A document, wearing a .jpg filename and an image/jpeg Content-Type."""
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def oversize() -> bytes:
    """A valid JPEG that is one byte over the ceiling.

    A real image with padding, rather than 5 MB of noise: the point is that the size
    check refuses something the type check would happily accept.
    """
    return photo() + b"\x00" * (LIMIT_BYTES + 1 - len(photo()))


def _sql(statement: str, params: tuple = ()) -> None:
    connection = sqlite3.connect(harness.DB_PATH)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def create_account(client, *, user_id=CREATED_WORKER, name="New Worker", role="worker",
                   password=STRONG_PASSWORD, image=None, as_role=ADMIN, filename="photo.jpg",
                   content_type="image/jpeg"):
    files = {}
    if image is not None:
        files = {"photo": (filename, image, content_type)}
    return client.post(
        "/api/v1/admin/users/create",
        headers=bearer(as_role),
        data={"user_id": user_id, "name": name, "role": role, "password": password},
        files=files or None,
    )


def issue_link(client, *, user_id=LINK_WORKER, name="Link Worker", role="worker", as_role=ADMIN, **extra):
    return client.post(
        "/api/v1/admin/enrollment/invites",
        headers=bearer(as_role),
        json={"worker_id": user_id, "kind": "register", "name": name, "role": role, **extra},
    )


# ---------------------------------------------------------------------------
# the upload policy
# ---------------------------------------------------------------------------
def test_the_policy_is_five_megabytes_and_images_only(client, jpeg):
    import uploads

    policy = uploads.policy()
    assert policy["max_bytes"] == LIMIT_BYTES
    assert policy["accepted"] == ["image/jpeg", "image/png", "image/webp"]

    assert uploads.sniff_photo(jpeg) == "image/jpeg"
    assert uploads.sniff_photo(photo(fmt="PNG")) == "image/png"
    assert uploads.sniff_photo(a_pdf()) is None, "a document is not a photo, whatever it is named"


@pytest.mark.parametrize(
    "surface",
    ["/api/v1/admin/users/create", "/api/v1/admin/enroll"],
)
def test_an_oversized_photo_is_refused_with_the_size_stated(client, surface):
    """413, and the message says what the limit is: somebody has to act on it."""
    payload = oversize()
    if surface.endswith("/create"):
        response = create_account(client, image=payload)
    else:
        response = client.post(
            "/api/v1/admin/enroll",
            headers=bearer(ADMIN),
            data={"worker_id": WORKER},
            files={"photo": ("photo.jpg", payload, "image/jpeg")},
        )
    assert response.status_code == 413, response.text[:300]
    detail = response.json()["detail"]
    assert detail["error_code"] == "photo_too_large"
    assert detail["limit_bytes"] == LIMIT_BYTES
    assert "5 MB" in detail["message"]


def test_a_document_is_refused_even_when_it_claims_to_be_a_photo(client):
    response = create_account(client, image=a_pdf(), filename="portrait.jpg", content_type="image/jpeg")
    assert response.status_code == 415, response.text[:300]
    assert response.json()["detail"]["error_code"] == "not_an_image"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (CREATED_WORKER,)) == 0, (
        "a refused photo must not leave an account behind"
    )


def test_an_empty_upload_is_refused(client):
    response = create_account(client, image=b"", filename="empty.jpg")
    assert response.status_code == 400, response.text[:300]
    assert response.json()["detail"]["error_code"] == "empty_upload"


def test_a_png_and_a_webp_are_both_accepted(client):
    """The policy is 'a real photo', not 'a JPEG': phones produce all three."""
    png = photo(fmt="PNG")
    first = create_account(client, user_id=CREATED_WORKER, image=png, filename="me.png", content_type="image/png")
    assert first.status_code == 200, first.text[:300]
    assert first.json()["face_enrolled"] is True


# ---------------------------------------------------------------------------
# creating an account in the console
# ---------------------------------------------------------------------------
def test_a_created_account_has_a_reference_and_can_clock_in(client, jpeg, app_module):
    response = create_account(client, image=jpeg)
    assert response.status_code == 200, response.text[:300]
    answer = response.json()
    assert answer["face_enrolled"] is True
    assert answer["user_id"] == CREATED_WORKER

    row = db_scalar("SELECT name FROM users WHERE id = ?", (CREATED_WORKER,))
    assert row == "New Worker"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (CREATED_WORKER,)) == "worker"
    assert db_scalar("SELECT template_version FROM users WHERE id = ?", (CREATED_WORKER,)) >= 1
    assert db_scalar("SELECT enrolled_at FROM users WHERE id = ?", (CREATED_WORKER,))

    # The reference is written where ``/attendance/verify`` reads it, which is the only
    # thing that makes this account able to clock in at all - and it is *not* named after
    # the account: the name is the immutable biometric id on the row, so this asks the
    # application's own resolver rather than rebuilding it (which is the point of the
    # test below asserting the account id is not in the filename at all).
    reference = harness.reference_path(CREATED_WORKER)
    assert reference.exists(), f"no template at {reference.name}"
    assert json.loads(reference.read_text())

    stored = db_scalar("SELECT password_hash FROM users WHERE id = ?", (CREATED_WORKER,))
    assert app_module.pwd_context.verify(STRONG_PASSWORD, stored)
    assert STRONG_PASSWORD not in json.dumps(answer), "the password is not echoed back"

    audit = db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'user_create' AND entity_id = ?",
        (CREATED_WORKER,),
    )
    assert audit == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'biometric_enroll' AND entity_id = ?",
        (CREATED_WORKER,),
    ) == 1

    # The proof that matters: the account works. ``/attendance/verify`` takes a token as
    # well, so this signs in the way the app does - a token for the account that was just
    # created, whose ``token_version`` is 0 because no password has been rotated yet.
    punch = harness.clock_in(client, CREATED_WORKER, headers=bearer(CREATED_WORKER))
    assert punch.status_code == 200, punch.text[:300]


def test_creating_without_a_photo_is_allowed_and_cannot_clock_in(client):
    response = create_account(client, user_id=UNENROLLED_WORKER)
    assert response.status_code == 200, response.text[:300]
    assert response.json()["face_enrolled"] is False
    assert db_scalar("SELECT enrolled_at FROM users WHERE id = ?", (UNENROLLED_WORKER,)) is None
    assert db_scalar("SELECT template_version FROM users WHERE id = ?", (UNENROLLED_WORKER,)) == 0
    assert not harness.template_exists(UNENROLLED_WORKER)

    punch = harness.clock_in(client, UNENROLLED_WORKER, headers=bearer(UNENROLLED_WORKER))
    assert punch.status_code == 404, "no reference, no clock-in - and said so plainly"


def test_a_moallem_can_be_created_in_its_own_id_block(client, jpeg):
    response = create_account(client, user_id=CREATED_MOALLEM, name="Lead", role="moallem", image=jpeg)
    assert response.status_code == 200, response.text[:300]
    assert db_scalar("SELECT role FROM users WHERE id = ?", (CREATED_MOALLEM,)) == "moallem"


def test_creation_refuses_what_the_console_would_not_offer(client, jpeg):
    wrong_block = create_account(client, user_id="900", role="worker", image=jpeg)
    assert wrong_block.status_code == 400
    assert "1-499" in wrong_block.json()["detail"]

    weak = create_account(client, user_id=CREATED_WORKER, password="1234", image=jpeg)
    assert weak.status_code == 400
    assert "at least" in weak.json()["detail"]
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (CREATED_WORKER,)) == 0

    duplicate = create_account(client, user_id=WORKER, image=jpeg)
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.json()["detail"]

    by_standard_admin = create_account(
        client, user_id="1001", role="admin", as_role=ADMIN, image=jpeg
    )
    assert by_standard_admin.status_code == 403

    by_head_admin = create_account(
        client, user_id="1001", name="Second Admin", role="admin", as_role=HEAD_ADMIN, image=jpeg
    )
    assert by_head_admin.status_code == 200, by_head_admin.text[:300]


def test_only_an_admin_can_create_an_account(client, jpeg):
    for role in (WORKER, MOALLEM):
        denied = create_account(client, as_role=role, image=jpeg)
        assert_denied(denied, endpoint="/admin/users/create", detail=f"{role} created an account")
    anonymous = client.post(
        "/api/v1/admin/users/create",
        data={"user_id": CREATED_WORKER, "name": "X", "role": "worker", "password": STRONG_PASSWORD},
    )
    assert_denied(anonymous, endpoint="/admin/users/create", detail="an anonymous caller created an account")


# ---------------------------------------------------------------------------
# the registration link
# ---------------------------------------------------------------------------
def test_a_registration_link_creates_its_account(client, jpeg, app_module):
    created = issue_link(client)
    assert created.status_code == 200, created.text[:300]
    invite = created.json()
    token = invite["token"]
    assert invite["kind"] == "register"
    assert invite["max_uses"] == 1, "a link that creates accounts is single use, whatever was asked"
    assert invite["url"].endswith(f"/enroll/{token}")
    assert db_scalar("SELECT token_hash FROM enrollment_invites WHERE id = ?", (invite["invite_id"],)) == (
        hashlib.sha256(token.encode()).hexdigest()
    )
    assert db_scalar("SELECT kind FROM enrollment_invites WHERE id = ?", (invite["invite_id"],)) == "register"
    assert db_scalar("SELECT pending_name FROM enrollment_invites WHERE id = ?", (invite["invite_id"],)) == "Link Worker"
    assert db_scalar("SELECT pending_role FROM enrollment_invites WHERE id = ?", (invite["invite_id"],)) == "worker"

    # The public peek says what kind of page to be, and masks the reserved name.
    peek = client.get(f"/api/v1/enroll/{token}")
    assert peek.status_code == 200
    body = peek.json()
    assert body["kind"] == "register"
    assert body["usable"] is True
    assert body["role"] == "worker"
    assert body["photo_policy"]["max_bytes"] == LIMIT_BYTES
    assert body["min_password_length"] >= 8
    assert "Link Worker" not in json.dumps(body), "a forwarded link must not leak the reserved name"

    registered = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD, "phone": "+201000000077"},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert registered.status_code == 200, registered.text[:300]
    assert registered.json()["worker_id"] == LINK_WORKER

    assert db_scalar("SELECT name FROM users WHERE id = ?", (LINK_WORKER,)) == "Link Worker"
    assert db_scalar("SELECT role FROM users WHERE id = ?", (LINK_WORKER,)) == "worker"
    assert db_scalar("SELECT phone FROM users WHERE id = ?", (LINK_WORKER,)) == "+201000000077"
    assert app_module.pwd_context.verify(
        STRONG_PASSWORD, db_scalar("SELECT password_hash FROM users WHERE id = ?", (LINK_WORKER,))
    ), "the password is the one the visitor chose, not a generated one"
    assert harness.template_exists(LINK_WORKER)
    assert db_scalar("SELECT uses FROM enrollment_invites WHERE id = ?", (invite["invite_id"],)) == 1
    assert db_scalar("SELECT completed_at FROM enrollment_invites WHERE id = ?", (invite["invite_id"],))
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'user_create' AND entity_id = ?", (LINK_WORKER,)
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'enrollment_completed' AND worker_id = ?",
        (LINK_WORKER,),
    ) == 1, "the administrator who sent the link should be told it was used"

    reuse = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert reuse.status_code == 410
    assert reuse.json()["detail"]["error_code"] == "invite_already_used"


def test_a_link_is_burned_by_success_only(client, jpeg):
    """Every rejection has to leave the link usable, or one typo costs a new link."""
    token = issue_link(client).json()["token"]
    invite_id = db_scalar("SELECT id FROM enrollment_invites WHERE worker_id = ?", (LINK_WORKER,))

    short = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": "1234"},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert short.status_code == 400
    assert "at least" in short.json()["detail"]

    document = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("cv.jpg", a_pdf(), "image/jpeg")},
    )
    assert document.status_code == 415
    assert document.json()["detail"]["error_code"] == "not_an_image"

    huge = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("huge.jpg", oversize(), "image/jpeg")},
    )
    assert huge.status_code == 413

    no_face = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert no_face.status_code == 200, no_face.text[:300]

    assert db_scalar("SELECT uses FROM enrollment_invites WHERE id = ?", (invite_id,)) == 1, (
        "three refusals and one success: the link was used exactly once"
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (LINK_WORKER,)) == 1


def test_a_link_cannot_reserve_or_mint_what_it_should_not(client, jpeg):
    existing = issue_link(client, user_id=WORKER, name="Taken")
    assert existing.status_code == 409
    assert "already exists" in existing.json()["detail"]

    as_admin = issue_link(client, role="admin")
    assert as_admin.status_code == 400
    assert "worker or a moallem" in as_admin.json()["detail"]

    nameless = issue_link(client, name="   ")
    assert nameless.status_code == 400
    assert "name" in nameless.json()["detail"].lower()

    wrong_block = issue_link(client, user_id="900", role="worker")
    assert wrong_block.status_code == 400
    assert "1-499" in wrong_block.json()["detail"]


def test_two_registration_links_cannot_reserve_one_id(client):
    """The reservation lives on the invite row until the link is used, so a check that only
    looked at ``users`` would let two live links promise the same number - and the second
    visitor to arrive would be told their id is taken."""
    first = issue_link(client, user_id=LINK_WORKER, name="First")
    assert first.status_code == 200, first.text[:300]

    second = issue_link(client, user_id=LINK_WORKER, name="Second")
    assert second.status_code == 409, second.text[:300]
    assert "reserved" in second.json()["detail"].lower()
    assert db_scalar(
        "SELECT COUNT(*) FROM enrollment_invites WHERE worker_id = ? AND kind = 'register'",
        (LINK_WORKER,),
    ) == 1

    # Revoking the first releases the number, and the link can then be issued cleanly.
    invite_id = db_scalar("SELECT id FROM enrollment_invites WHERE worker_id = ?", (LINK_WORKER,))
    revoked = client.post(
        f"/api/v1/admin/enrollment/invites/{invite_id}/revoke", headers=bearer(ADMIN)
    )
    assert revoked.status_code == 200, revoked.text[:300]
    third = issue_link(client, user_id=LINK_WORKER, name="Third")
    assert third.status_code == 200, third.text[:300]


def test_an_enrollment_link_cannot_be_used_to_register(client, jpeg):
    """The two kinds are not interchangeable: one creates, the other must not."""
    enrollment_token = client.post(
        "/api/v1/admin/enrollment/invites",
        headers=bearer(ADMIN),
        json={"worker_id": WORKER},
    ).json()["token"]

    attempt = client.post(
        f"/api/v1/enroll/{enrollment_token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert attempt.status_code == 409
    assert attempt.json()["detail"]["error_code"] == "invite_kind_mismatch"

    # And the enrollment path still works afterwards: the refused call changed nothing.
    submitted = client.post(
        f"/api/v1/enroll/{enrollment_token}", files={"photo": ("photo.jpg", jpeg, "image/jpeg")}
    )
    assert submitted.status_code == 200, submitted.text[:300]


def test_an_expired_link_cannot_create_an_account(client, jpeg):
    from datetime import datetime, timedelta

    token = "expired-registration-token"
    _sql(
        "INSERT INTO enrollment_invites (token_hash, worker_id, created_by, created_at, expires_at, "
        "max_uses, kind, pending_name, pending_role) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            hashlib.sha256(token.encode()).hexdigest(),
            LINK_WORKER,
            ADMIN,
            (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            1,
            "register",
            "Late Worker",
            "worker",
        ),
    )
    peek = client.get(f"/api/v1/enroll/{token}")
    assert peek.json()["status"] == "expired"

    attempt = client.post(
        f"/api/v1/enroll/{token}/register",
        data={"password": STRONG_PASSWORD},
        files={"photo": ("photo.jpg", jpeg, "image/jpeg")},
    )
    assert attempt.status_code == 410
    assert attempt.json()["detail"]["error_code"] == "invite_expired"
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", (LINK_WORKER,)) == 0


def test_a_worker_cannot_issue_a_registration_link(client):
    denied = issue_link(client, as_role=WORKER)
    assert_denied(denied, endpoint="/admin/enrollment/invites", detail="a worker issued a registration link")
    denied_moallem = issue_link(client, as_role=MOALLEM)
    assert_denied(
        denied_moallem, endpoint="/admin/enrollment/invites", detail="a moallem issued a registration link"
    )
