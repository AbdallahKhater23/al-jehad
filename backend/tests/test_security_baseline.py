"""Authorization baseline: what an anonymous or under-privileged caller can do TODAY.

Every test marked ``security_gap`` is expected to FAIL against the current
``backend/main.py``. That failure *is* the finding: it names the endpoint, the
action that was allowed, and the credential that should have been required. The
same test turns green when the remediation lands, which is how this file doubles
as the executable acceptance criteria for the plan's Steps 2.2 and 2.3.

Tests marked ``regression`` pass today and must keep passing afterwards.

    pytest backend/tests/test_security_baseline.py -q
    pytest -m regression -q
"""

from __future__ import annotations

import pytest
from harness import (
    ADMIN,
    EMAILS,
    HEAD_ADMIN,
    MOALLEM,
    PASSWORDS,
    SEEDED_PENDING_LOG_ID,
    WORKER,
    assert_denied,
    bearer,
    clock_in,
    db_scalar,
    enroll,
    send,
    token_signed_with_a_different_key,
    unsigned_token,
)

# ---------------------------------------------------------------------------
# The admin surface, as an attacker would probe it.
#
# ``SPOOFED_IDENTITY_CALLS`` reproduces the realistic attack: no credential of
# any kind, but the payload claims to be the head admin (id 5000) -- exactly the
# payload the shipped front-end sends today.
#
# ``CONTRACT_CALLS`` is the post-fix wire contract: no identity field at all,
# because identity will come from the session. Today these fail differently
# (422 "you forgot admin_id"), which is itself a finding: authorization is being
# enforced as payload validation rather than as a credential check.
# ---------------------------------------------------------------------------
SPOOFED_IDENTITY_CALLS: list[tuple[str, str, dict, str]] = [
    ("GET", "/api/v1/admin/users", {}, "read every user, with emails and phone numbers"),
    ("GET", "/api/v1/admin/sites", {}, "read the site configuration"),
    ("GET", "/api/v1/admin/active_sessions", {}, "read who is on shift right now"),
    ("GET", "/api/v1/admin/pending_reviews", {}, "read flagged attendance records"),
    ("GET", "/api/v1/admin/logs", {}, "read the full attendance history"),
    ("GET", "/api/v1/admin/workers_live/Downtown Tower A", {}, "read who is on a given site"),
    ("GET", f"/api/v1/worker/stats/{WORKER}", {}, "read another user's monthly hours"),
    ("POST", "/api/v1/admin/sites/add", {"json_body": {"site_name": "Rogue Site", "location_input": "30.9,31.9", "radius": 50, "admin_id": HEAD_ADMIN}}, "create a construction site"),
    ("POST", "/api/v1/admin/sites/edit", {"json_body": {"site_name": "Downtown Tower A", "location_input": "31.9,32.9", "radius": 50, "admin_id": HEAD_ADMIN}}, "move a construction site"),
    ("POST", "/api/v1/admin/sites/delete", {"data": {"site_name": "Downtown Tower A", "admin_id": HEAD_ADMIN}}, "delete a construction site"),
    ("POST", "/api/v1/admin/users/add", {"json_body": {"user_id": "321", "name": "Rogue Worker", "email": "rogue@example.test", "phone": "+200000000003", "password": "Rogue-Pass-123", "role": "worker", "creator_id": HEAD_ADMIN}}, "create a user account"),
    ("POST", "/api/v1/admin/users/edit_password", {"json_body": {"worker_id": WORKER, "new_password": "Hijacked-Pass-123", "admin_id": HEAD_ADMIN}}, "reset any user's password"),
    ("POST", "/api/v1/admin/admins/add", {"json_body": {"user_id": "1600", "name": "Rogue Admin", "email": "rogue.admin@example.test", "phone": "", "password": "Rogue-Pass-123", "role": "admin", "creator_id": HEAD_ADMIN}}, "create a new administrator"),
    ("POST", "/api/v1/admin/force_clock_in", {"json_body": {"worker_id": MOALLEM, "site_name": "Downtown Tower A", "admin_id": HEAD_ADMIN}}, "clock somebody in"),
    ("POST", "/api/v1/admin/force_clock_out", {"json_body": {"worker_id": WORKER, "site_name": "", "admin_id": HEAD_ADMIN}}, "clock somebody out"),
    ("POST", "/api/v1/admin/approve_review", {"json_body": {"log_id": SEEDED_PENDING_LOG_ID, "admin_id": HEAD_ADMIN}}, "approve a flagged attendance record"),
    ("POST", "/api/v1/admin/reject_review", {"json_body": {"log_id": SEEDED_PENDING_LOG_ID, "note": "refused", "admin_id": HEAD_ADMIN}}, "refuse a flagged attendance record"),
]

CONTRACT_CALLS: list[tuple[str, str, dict, str]] = [
    ("GET", "/api/v1/admin/users", {}, "read every user"),
    ("GET", "/api/v1/admin/sites", {}, "read the site configuration"),
    ("GET", "/api/v1/admin/active_sessions", {}, "read who is on shift"),
    ("GET", "/api/v1/admin/pending_reviews", {}, "read flagged records"),
    ("GET", "/api/v1/admin/logs", {}, "read the attendance history"),
    ("GET", "/api/v1/admin/workers_live/Downtown Tower A", {}, "read on-site workers"),
    ("POST", "/api/v1/admin/sites/add", {"json_body": {"site_name": "Rogue Site", "location_input": "30.9,31.9", "radius": 50}}, "create a site without claiming an identity"),
    ("POST", "/api/v1/admin/users/add", {"json_body": {"user_id": "321", "name": "Rogue", "email": "r@example.test", "phone": "", "password": "Rogue-Pass-123", "role": "worker"}}, "create a user without claiming an identity"),
    ("POST", "/api/v1/admin/users/edit_password", {"json_body": {"worker_id": WORKER, "new_password": "Hijacked-Pass-123"}}, "reset a password without claiming an identity"),
    ("POST", "/api/v1/admin/force_clock_out", {"json_body": {"worker_id": WORKER, "site_name": ""}}, "clock somebody out without claiming an identity"),
    ("POST", "/api/v1/admin/approve_review", {"json_body": {"log_id": SEEDED_PENDING_LOG_ID}}, "approve a record without claiming an identity"),
    # A refusal has to be as guarded as the approval beside it: it is the decision that
    # takes hours away from somebody, so an anonymous caller reaching it is the worse gap.
    ("POST", "/api/v1/admin/reject_review", {"json_body": {"log_id": SEEDED_PENDING_LOG_ID, "note": "refused"}}, "refuse a record without claiming an identity"),
]


def _failures(client, calls, *, headers=None) -> list[str]:
    problems = []
    for method, path, kwargs, label in calls:
        response = send(client, method, path, headers=headers, **kwargs)
        if response.status_code not in (401, 403):
            problems.append(
                f"  {method:<5} {path:<44} -> HTTP {response.status_code:<3} ({label})"
            )
    return problems


# ---------------------------------------------------------------------------
# 1. Anonymous access to the admin surface
# ---------------------------------------------------------------------------
@pytest.mark.security_gap
def test_anonymous_callers_cannot_reach_the_admin_surface(client):
    """No credential at all must yield 401/403 -- never the data, never the action."""
    failures = _failures(client, SPOOFED_IDENTITY_CALLS)
    assert not failures, (
        "SECURITY GAP: these admin endpoints served an ANONYMOUS caller "
        "(no Authorization header, identity claimed only inside the payload):\n"
        + "\n".join(failures)
    )


@pytest.mark.security_gap
def test_authorization_is_not_just_payload_validation(client):
    """Authorization must come from a credential, not from filling in a form field.

    Today the admin endpoints fall back to `if the caller sent admin_id of an
    admin user, allow it`. That is not a control: the field is attacker-chosen.
    A 422 here means "you forgot to claim an identity", which anyone can fix.
    """
    failures = _failures(client, CONTRACT_CALLS)
    assert not failures, (
        "SECURITY GAP: an anonymous caller reached these endpoints by simply "
        "selecting the identity claim it liked (an HTTP 422 here means the "
        "identity field was merely required, not verified):\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# 2. Under-privileged (but authenticated) callers
# ---------------------------------------------------------------------------
@pytest.mark.security_gap
@pytest.mark.parametrize("user_id", [WORKER, MOALLEM], ids=["worker", "moallem"])
def test_non_admin_roles_cannot_reach_the_admin_surface(client, user_id):
    failures = _failures(client, SPOOFED_IDENTITY_CALLS, headers=bearer(user_id))
    assert not failures, (
        f"SECURITY GAP: a '{user_id}' caller reached these admin endpoints; the "
        "Authorization header is ignored, so a worker's own session is enough:\n"
        + "\n".join(failures)
    )


@pytest.mark.security_gap
def test_worker_token_plus_spoofed_admin_id_is_rejected(client):
    """The literal attack the front-end can mount today: keep your own session,
    then add `admin_id` of the head admin to the payload."""
    headers = bearer(WORKER)
    response = send(
        client,
        "POST",
        "/api/v1/admin/sites/add",
        headers=headers,
        json_body={
            "site_name": "Worker Spoof Site",
            "location_input": "30.9,31.9",
            "radius": 50,
            "admin_id": HEAD_ADMIN,
        },
    )
    assert_denied(
        response,
        endpoint="POST /api/v1/admin/sites/add",
        detail="a worker's request was authorized by the admin_id it supplied itself",
    )
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = ?", ("Worker Spoof Site",)) == 0


# ---------------------------------------------------------------------------
# 3. The loud ones: a real privileged action performed for no credential at all
# ---------------------------------------------------------------------------
@pytest.mark.security_gap
def test_anonymous_caller_cannot_create_an_administrator(client):
    """Privilege escalation: unauthenticated creation of an admin account."""
    response = send(
        client,
        "POST",
        "/api/v1/admin/admins/add",
        json_body={
            "user_id": "1600",
            "name": "Rogue Admin",
            "email": "rogue.admin@example.test",
            "phone": "",
            "password": "Rogue-Pass-123",
            "role": "admin",
            "creator_id": HEAD_ADMIN,
        },
    )
    assert_denied(
        response,
        endpoint="POST /api/v1/admin/admins/add",
        detail="an anonymous caller created an administrator account",
    )
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("1600",)) == 0, (
        "an administrator account was created without any credential"
    )


@pytest.mark.security_gap
def test_anonymous_caller_cannot_reset_a_password(client):
    """Credential takeover: an anonymous caller can lock a worker out or,
    worse, set a password it knows and then clock in as that worker."""
    before = db_scalar("SELECT password_hash FROM users WHERE id = ?", (WORKER,))
    response = send(
        client,
        "POST",
        "/api/v1/admin/users/edit_password",
        json_body={"worker_id": WORKER, "new_password": "Hijacked-Pass-123", "admin_id": HEAD_ADMIN},
    )
    assert_denied(
        response,
        endpoint="POST /api/v1/admin/users/edit_password",
        detail="an anonymous caller reset another user's password",
    )
    after = db_scalar("SELECT password_hash FROM users WHERE id = ?", (WORKER,))
    assert after == before, "the stored password hash was replaced without any credential"


@pytest.mark.security_gap
def test_anonymous_caller_cannot_create_a_site(client):
    before = db_scalar("SELECT COUNT(*) FROM construction_sites")
    response = send(
        client,
        "POST",
        "/api/v1/admin/sites/add",
        json_body={"site_name": "Rogue Site", "location_input": "30.9,31.9", "radius": 50, "admin_id": HEAD_ADMIN},
    )
    assert_denied(response, endpoint="POST /api/v1/admin/sites/add", detail="an anonymous caller created a site")
    assert db_scalar("SELECT COUNT(*) FROM construction_sites") == before


@pytest.mark.security_gap
def test_anonymous_caller_cannot_force_a_clock_out(client):
    before = db_scalar("SELECT COUNT(*) FROM active_sessions")
    response = send(
        client,
        "POST",
        "/api/v1/admin/force_clock_out",
        json_body={"worker_id": WORKER, "site_name": "", "admin_id": HEAD_ADMIN},
    )
    assert_denied(
        response,
        endpoint="POST /api/v1/admin/force_clock_out",
        detail="an anonymous caller ended somebody else's shift (payroll impact)",
    )
    assert db_scalar("SELECT COUNT(*) FROM active_sessions") == before


@pytest.mark.security_gap
def test_anonymous_caller_cannot_approve_a_flagged_record(client):
    response = send(
        client,
        "POST",
        "/api/v1/admin/approve_review",
        json_body={"log_id": SEEDED_PENDING_LOG_ID, "admin_id": HEAD_ADMIN},
    )
    assert_denied(
        response,
        endpoint="POST /api/v1/admin/approve_review",
        detail="an anonymous caller cleared a biometric-review flag",
    )
    assert db_scalar("SELECT status FROM attendance_logs WHERE id = ?", (SEEDED_PENDING_LOG_ID,)) == "pending_review"


@pytest.mark.security_gap
def test_anonymous_caller_cannot_enroll_biometrics(client, jpeg):
    """Enrollment has no credential check *and* no identity field: it accepts a
    face for any user id, which can be used to replace a colleague's template."""
    response = enroll(client, WORKER, image=jpeg)
    assert_denied(
        response,
        endpoint="POST /api/v1/admin/enroll",
        detail="an anonymous caller overwrote a worker's facial template",
    )


@pytest.mark.security_gap
def test_clock_in_requires_a_session_credential(client, jpeg):
    """/attendance/verify authenticated from form fields only.

    Anyone who knew a worker's password -- a colleague who watched them type
    it, a password reused elsewhere, or an attacker who reset it via the
    endpoint above -- could create attendance records with no session at all.
    That is closed: the token is required, and since the clock-in prompt stopped
    asking for a password it is also the *only* credential in the request, so
    there is nothing left in the body to guess.
    """
    response = clock_in(client, WORKER, image=jpeg)
    assert response.status_code in (401, 403), (
        "SECURITY GAP: /attendance/verify performed a biometric clock-in with no "
        f"session credential (got {response.status_code}: {response.text[:200]!r})"
    )


def test_the_session_is_the_whole_credential_for_a_punch(client, jpeg):
    """The other half of that rule: a live session is enough, and is what is checked.

    The clock prompt used to demand the worker's password a second time, seconds after
    they signed in with it. It is gone, so this pins what remains - a punch with no
    password field at all is recorded, and one whose token has been revoked is not.
    """
    # Minted once, before the rotation below: ``bearer()`` signs with the ``token_version``
    # the database holds *now*, so a token asked for afterwards is a token nobody was ever
    # issued - it has to be captured here to be the worker's actual credential.
    issued = bearer(MOALLEM)
    allowed = clock_in(client, MOALLEM, image=jpeg, headers=issued)
    assert allowed.status_code == 200, allowed.text[:300]
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock In'", (MOALLEM,)
    ) >= 1, "a session, a geofence and a face are enough - nothing else is asked for"

    # Rotating the password (or deactivating the account) bumps ``token_version``, which
    # is what makes the token the credential worth having: the punch is refused.
    rotated = send(
        client,
        "POST",
        "/api/v1/admin/users/edit_password",
        headers=bearer(HEAD_ADMIN),
        json_body={"worker_id": MOALLEM, "new_password": "Rotated-Pass-123", "admin_id": HEAD_ADMIN},
    )
    assert rotated.status_code == 200, rotated.text[:200]
    revoked = clock_in(client, MOALLEM, image=jpeg, headers=issued)
    assert revoked.status_code == 401, revoked.text[:200]
    assert "sign in again" in revoked.json()["detail"].lower()


@pytest.mark.security_gap
def test_identity_comes_from_the_session_not_the_form(client, jpeg):
    """A valid session must not be able to act as a different subject."""
    response = clock_in(
        client,
        MOALLEM,
        headers=bearer(WORKER),
        image=jpeg,
    )
    assert_denied(
        response,
        endpoint="POST /api/v1/attendance/verify",
        detail="a worker's session submitted attendance as a different worker id",
    )


# ---------------------------------------------------------------------------
# 4. Token handling
# ---------------------------------------------------------------------------
@pytest.mark.security_gap
@pytest.mark.parametrize(
    "case,token",
    [
        ("placeholder-garbage", "not-a-real-token"),
        ("signed-with-another-key", token_signed_with_a_different_key()),
        ("unsigned-alg-none", unsigned_token(WORKER)),
    ],
    ids=["placeholder-garbage", "signed-with-another-key", "unsigned-alg-none"],
)
def test_forged_tokens_are_rejected(client, case, token):
    response = send(client, "GET", "/api/v1/admin/logs", headers={"Authorization": f"Bearer {token}"})
    assert_denied(
        response,
        endpoint="GET /api/v1/admin/logs",
        detail=f"a {case} token was accepted (the header is never verified)",
    )


@pytest.mark.security_gap
def test_expired_token_is_rejected(client):
    response = send(client, "GET", "/api/v1/admin/logs", headers=bearer(HEAD_ADMIN, expired=True))
    assert_denied(
        response,
        endpoint="GET /api/v1/admin/logs",
        detail="an expired token was accepted",
    )


@pytest.mark.security_gap
def test_login_returns_a_signed_access_token(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"user_id": WORKER, "email_or_phone": EMAILS[WORKER], "password": PASSWORDS[WORKER]},
    )
    assert response.status_code == 200, f"login failed: {response.status_code} {response.text[:200]}"
    payload = response.json()
    token = payload.get("token") or payload.get("access_token")
    assert token, (
        "SECURITY GAP: login returns no access token, so no endpoint can authenticate "
        "the caller and every authorization decision falls back to client-supplied ids"
    )
    import jwt as pyjwt

    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims.get("sub") == WORKER
    assert "exp" in claims and "role" in claims


@pytest.mark.security_gap
def test_login_is_rate_limited_against_password_guessing(client):
    """Nothing throttles /auth/login today: unlimited offline-speed guessing."""
    codes = [
        client.post(
            "/api/v1/auth/login",
            json={"user_id": WORKER, "email_or_phone": EMAILS[WORKER], "password": f"guess-{attempt}"},
        ).status_code
        for attempt in range(16)
    ]
    assert 429 in codes, (
        "SECURITY GAP: 16 consecutive failed logins were all processed (no 429); "
        f"observed codes: {sorted(set(codes))}"
    )


# ---------------------------------------------------------------------------
# 5. Route surface
# ---------------------------------------------------------------------------
@pytest.mark.security_gap
def test_the_admin_surface_is_exposed_only_under_the_versioned_prefix(app_module, client):
    """`app.include_router(router)` is called a second time without a prefix, so
    every endpoint exists twice. Same handler today, so it is not an escalation
    by itself -- but it doubles the surface that any proxy/WAF rule and any
    future guard must cover, and it is exactly where a guard gets forgotten."""
    duplicated = sorted(
        {
            getattr(route, "path", "")
            for route in app_module.app.routes
            if getattr(route, "path", "").startswith("/admin")
        }
    )
    assert duplicated == [], (
        "SECURITY GAP: the admin API is also mounted without the /api/v1 prefix:\n  "
        + "\n  ".join(duplicated)
    )
    assert client.get("/admin/users").status_code == 404, (
        "the unversioned duplicate path served a live response"
    )


@pytest.mark.security_gap
def test_interactive_api_docs_are_not_publicly_exposed(client):
    """The generated docs enumerate every admin route and schema."""
    exposed = [path for path in ("/docs", "/redoc", "/openapi.json") if client.get(path).status_code == 200]
    assert not exposed, (
        "SECURITY GAP: the API documentation is public and lists the whole admin "
        f"surface for an anonymous visitor: {exposed}"
    )


# ---------------------------------------------------------------------------
# 6. Regressions -- green today, must stay green after the remediation
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_login_still_succeeds_for_seeded_users(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"user_id": HEAD_ADMIN, "email_or_phone": EMAILS[HEAD_ADMIN], "password": PASSWORDS[HEAD_ADMIN]},
    )
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "head_admin"


@pytest.mark.regression
def test_login_failures_stay_indistinguishable(client):
    """A wrong password and an unknown user must look identical, or the endpoint
    is a user-enumeration oracle."""
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"user_id": WORKER, "email_or_phone": EMAILS[WORKER], "password": "definitely-wrong"},
    )
    unknown_user = client.post(
        "/api/v1/auth/login",
        json={"user_id": "424242", "email_or_phone": "nobody@example.test", "password": "definitely-wrong"},
    )
    assert wrong_password.status_code == 401, f"expected 401, got {wrong_password.status_code}"
    assert unknown_user.status_code == 401, f"expected 401, got {unknown_user.status_code}"
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"], (
        "the two failure modes are distinguishable, which leaks whether a user id exists"
    )


@pytest.mark.regression
def test_legacy_payloads_that_still_send_admin_id_keep_working_for_an_admin(client):
    """The shipped front-end sends admin_id/creator_id on every call; the fix must
    IGNORE those fields rather than reject them, or the existing UI breaks."""
    response = send(
        client,
        "POST",
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json_body={
            "site_name": "Compat Site",
            "location_input": "30.06,31.24",
            "radius": 60,
            "admin_id": ADMIN,
        },
    )
    assert response.status_code == 200, (
        f"an admin could not create a site: {response.status_code} {response.text[:200]}"
    )
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = ?", ("Compat Site",)) == 1


@pytest.mark.regression
def test_approving_a_flagged_record_still_works_for_an_admin(client):
    response = send(
        client,
        "POST",
        "/api/v1/admin/approve_review",
        headers=bearer(ADMIN),
        json_body={"log_id": SEEDED_PENDING_LOG_ID, "admin_id": ADMIN},
    )
    assert response.status_code == 200, f"{response.status_code} {response.text[:200]}"
    assert db_scalar("SELECT status FROM attendance_logs WHERE id = ?", (SEEDED_PENDING_LOG_ID,)) != "pending_review"
