"""Secret handling, exposed surfaces, and the attendance workflow itself.

Two jobs here:

* ``security_gap`` tests prove the leaked credentials and the permissive
  configuration are real (and keep them fixed once remediated).
* ``regression`` tests pin the behaviour the front-end depends on -- the SPA
  entry point, both static mounts, the geofence decision and the clock-in/out
  flow -- so the remediation cannot silently break the app while it hardens it.
"""

from __future__ import annotations

import re

import pytest
from harness import (
    ADMIN,
    MAIN_PY,
    MOALLEM,
    PASSWORDS,
    WORKER,
    bearer,
    clock_in,
    db_rows,
    db_scalar,
    import_app_in_subprocess,
)

MAIN_SOURCE = MAIN_PY.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# 1. Secrets must not be baked into the source
# ---------------------------------------------------------------------------
SECRET_PATTERNS = [
    (r"AC[0-9a-fA-F]{32}", "a Twilio Account SID"),
    (r"SK[0-9a-fA-F]{32}", "a Twilio API key (used as the auth token)"),
    (r"whatsapp:\+\d{7,}", "a hardcoded WhatsApp number (personal data)"),
    (r"testpassword", "a hardcoded demo password"),
]


@pytest.mark.security_gap
@pytest.mark.parametrize("pattern,label", SECRET_PATTERNS, ids=[label.split()[1] for _, label in SECRET_PATTERNS])
def test_main_py_contains_no_baked_in_credentials(pattern, label):
    matches = [match.group(0) for match in re.finditer(pattern, MAIN_SOURCE)]
    redacted = [f"{value[:4]}...({len(value)} chars)" for value in matches]
    assert not matches, (
        f"SECURITY GAP: backend/main.py still embeds {label}: {redacted}. "
        "Removing it from the file is not remediation on its own - the value is in git "
        "history and must be rotated as well."
    )


@pytest.mark.security_gap
def test_whatsapp_alerts_refuse_to_run_without_configured_credentials(app_module, outbound, monkeypatch):
    """With the Twilio environment variables unset the alert path must decline.

    Today it falls back to the hardcoded pair and calls api.twilio.com anyway,
    which is both a leaked credential in use and a live API call from a test run.
    """
    for variable in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "ADMIN_WHATSAPP_TO"):
        monkeypatch.delenv(variable, raising=False)
    outbound.calls.clear()

    app_module.send_whatsapp_alert(WORKER, "Seed Worker", "Downtown Tower A", 0.5)

    assert outbound.calls == [], (
        "SECURITY GAP: send_whatsapp_alert() made an outbound call with baked-in fallback "
        f"credentials instead of refusing: {outbound.urls()[:1]}"
    )


@pytest.mark.security_gap
def test_application_refuses_to_start_without_a_signing_secret():
    """A missing JWT secret must fail closed.

    A default signing key is equivalent to no authentication at all: anybody who
    reads the repository can mint a head-admin token.
    """
    result = import_app_in_subprocess(secret_key=None)
    combined = (result.stdout or "") + (result.stderr or "")
    assert result.returncode != 0, (
        "SECURITY GAP: the application started with no SECRET_KEY configured, so it will "
        "sign tokens with a built-in default anyone can read"
    )
    assert "SECRET_KEY" in combined, (
        "the startup failure must name the missing setting so an operator can fix it; "
        f"output was: {combined[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 2. What must stay reachable and what must stay hidden
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_spa_entry_point_still_serves(client):
    response = client.get("/")
    assert response.status_code == 200, "the SPA entry point must never break"
    assert "text/html" in response.headers["content-type"]
    assert 'id="app"' in response.text


@pytest.mark.regression
@pytest.mark.parametrize(
    "asset",
    [
        "style.css",
        "i18n.js",
        "frontendjavascript.js",
        "offline_queue.js",
        "worker_modules.js",
        "admin_modules.js",
    ],
)
def test_frontend_assets_load_from_the_site_root(client, asset):
    """index.html uses RELATIVE asset paths, so these must resolve at "/"."""
    assert client.get(f"/{asset}").status_code == 200


@pytest.mark.regression
def test_frontend_assets_are_revalidated_on_every_load(client):
    """A browser must not be free to keep running an old frontend.

    StaticFiles sends ETag/Last-Modified and no Cache-Control, which lets a client
    apply heuristic freshness and reuse a stale frontendjavascript.js without
    asking - so a fixed bug keeps reproducing on the device that has the stale copy.
    """
    for path in ("/", "/frontendjavascript.js", "/offline_queue.js", "/style.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-cache" in response.headers.get("cache-control", ""), path

    # API responses are deliberately untouched by that rule.
    api = client.get("/api/v1/status")
    assert "no-cache" not in api.headers.get("cache-control", "")


@pytest.mark.regression
def test_legacy_static_mount_still_works(client):
    """Bookmarks of /static/... are explicitly kept working by the plan."""
    assert client.get("/static/style.css").status_code == 200


@pytest.mark.regression
def test_health_endpoint_stays_public(client):
    response = client.get("/api/v1/status")
    assert response.status_code == 200 and response.json() == {"status": "active"}


@pytest.mark.regression
@pytest.mark.parametrize("path", ["/worker_photos/1.jpg", "/local_references/1.json", "/times.db"])
def test_biometric_and_database_files_are_not_web_exposed(client, path):
    """Face templates, enrollment photos and the database must never be served."""
    assert client.get(path).status_code == 404, f"{path} must not be reachable over HTTP"


# ---------------------------------------------------------------------------
# 3. The attendance workflow, end to end
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_clock_in_then_clock_out_works_when_authenticated(client, jpeg):
    """The core worker journey must survive every hardening change.

    A moallem (id 600) starts with no open session -- worker 1 is seeded as
    already clocked in, so 600 keeps this test independent of that.
    """
    headers = bearer(MOALLEM)

    clocked_in = clock_in(client, MOALLEM, image=jpeg, headers=headers)
    assert clocked_in.status_code == 200, f"clock-in failed: {clocked_in.status_code} {clocked_in.text[:200]}"
    assert clocked_in.json()["site"] == "Downtown Tower A"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1

    clocked_out = clock_in(
        client, MOALLEM, action="Clock Out", image=jpeg, headers=headers
    )
    assert clocked_out.status_code == 200, f"clock-out failed: {clocked_out.status_code} {clocked_out.text[:200]}"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'", (MOALLEM,)
    ) == 1


@pytest.mark.regression
def test_double_clock_in_is_still_refused(client, jpeg):
    """Worker 1 is seeded as clocked in, so a second Clock In must be rejected."""
    response = clock_in(client, WORKER, image=jpeg, headers=bearer(WORKER))
    assert response.status_code == 400, f"expected 400, got {response.status_code}: {response.text[:200]}"


@pytest.mark.regression
def test_flagged_worker_cannot_clock_out(client, jpeg):
    """The existing pending-review workflow: a flagged worker must be stopped."""
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND status = 'pending_review'", (WORKER,)) == 1
    response = clock_in(client, WORKER, action="Clock Out", image=jpeg, headers=bearer(WORKER))
    assert response.status_code == 403, f"expected 403, got {response.status_code}: {response.text[:200]}"


@pytest.mark.regression
def test_out_of_geofence_clock_in_is_refused(client, jpeg):
    response = clock_in(client, MOALLEM, coordinates="51.5074,-0.1278", image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 403, f"expected 403, got {response.status_code}: {response.text[:200]}"


@pytest.mark.security_gap
def test_implausible_coordinates_are_rejected_as_invalid(client, jpeg):
    """(0, 0) is the canonical fixed reading of a mock-location provider.

    It must be rejected as invalid input, not merely as "outside the geofence" --
    the distinction matters because a coordinate of exactly 0,0 is never a real
    device fix in this deployment.
    """
    response = clock_in(client, MOALLEM, coordinates="0,0", image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 400, (
        "SECURITY GAP: the server accepts (0,0) as a legitimate position and answered "
        f"{response.status_code}; mock-location coordinates must be rejected outright"
    )


@pytest.mark.regression
def test_a_face_that_does_not_match_is_refused(client, jpeg, face):
    """Refused as an unusable *frame*, not as an unusable session.

    401 in this app means one thing to the client - the token is dead, sign the worker
    out and repaint the login screen - so a mismatched selfie answering 401 cost the
    worker the very session they needed to try again, and told them "Your session
    expired" instead of the reason. It answers 422 with the liveness gate's
    ``{error_code, message}`` shape, which the client renders as a readable message.
    """
    face.FACE_MODE = "mismatch"
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 422, f"a mismatched face must be refused, got {response.status_code}"
    body = response.json()["detail"]
    assert body["error_code"] == "face_mismatch", body
    # The worker gets a sentence they can act on; the number stays for the record.
    assert "score" in body, body
    assert "0." not in body["message"] and "Score" not in body["message"], (
        f"a face-match score means nothing on a phone, got {body['message']!r}"
    )
    assert "fill the frame" in body["message"].lower(), body


@pytest.mark.regression
def test_a_photo_with_no_face_is_refused(client, jpeg, face):
    face.FACE_MODE = "none"
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 400, f"expected 400 for an undetectable face, got {response.status_code}"
    body = response.json()["detail"]
    assert body["error_code"] == "face_not_found", body
    assert "face" in body["message"].lower() and "again" in body["message"].lower(), body


@pytest.mark.regression
def test_multiple_faces_are_refused(client, jpeg, face):
    face.FACE_COUNT = 2
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 400, f"expected 400 for multiple faces, got {response.status_code}"
    body = response.json()["detail"]
    assert body["error_code"] == "multiple_faces", body
    assert "only person" in body["message"].lower(), body


@pytest.mark.regression
def test_a_broken_face_check_is_ours_and_says_so(client, jpeg, face, monkeypatch, app_module):
    """A failure inside the check must not be dressed as a bad photo.

    The reason used to reach the phone verbatim ("Internal processing error: ..."), which
    blamed the worker for a server fault and leaked an exception message; retrying a bug
    in words that say "your photo is wrong" is how such a bug survives a week of reports.
    """
    monkeypatch.setattr(
        app_module, "compare_faces_sync",
        lambda *a, **k: {"verified": False, "distance": 99.9, "error": "Internal processing error: boom"},
    )
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 500, f"expected 500 for our own failure, got {response.status_code}"
    body = response.json()["detail"]
    assert body["error_code"] == "face_check_failed", body
    assert "boom" not in response.text, "the exception text is not the worker's business"


@pytest.mark.regression
def test_a_borderline_match_is_flagged_for_review(client, jpeg, face):
    """The 0.41-0.60 band must route to pending_review without blocking the worker."""
    face.FACE_MODE = "review"
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 200, f"a borderline match must not hard-fail: {response.status_code}"
    assert response.json()["status"] == "flagged", response.json()
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND status = 'pending_review'", (MOALLEM,)
    ) == 1


@pytest.mark.broken_today
def test_site_delete_accepts_the_form_the_frontend_actually_sends(client):
    """Contract bug found while writing this suite: the Delete button in
    Admin -> Sites can never work.

    ``delete_site(site_name: str, admin_id: str = Form(...))`` declares
    ``site_name`` as a QUERY parameter (no ``Form(...)``), but
    ``frontend/admin_modules.js`` posts both fields as multipart form data, so
    every delete answers 422 regardless of who calls it.
    """
    response = client.post(
        "/api/v1/admin/sites/delete",
        headers=bearer(ADMIN),
        data={"site_name": "New Capital Zone B", "admin_id": ADMIN},
    )
    assert response.status_code == 200, (
        "the front-end posts site_name as a form field but the endpoint declares it as a "
        f"query parameter, so deleting a site always fails: {response.status_code} {response.text[:200]}"
    )
    assert db_scalar("SELECT COUNT(*) FROM construction_sites WHERE site_name = ?", ("New Capital Zone B",)) == 0


@pytest.mark.security_gap
def test_clock_in_records_the_submitted_coordinates(client, jpeg):
    """Disputed shifts need an evidence trail.

    Today the submitted position is verified against the geofence and then
    discarded, so a contested clock-in has no location evidence at all. This is
    expected to fail until the additive migration adds the columns.
    """
    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert response.status_code == 200, f"clock-in must succeed first: {response.status_code} {response.text[:200]}"

    columns = {row[1] for row in db_rows("PRAGMA table_info(attendance_logs)")}
    missing = {"lat", "lon"} - columns
    assert not missing, (
        "SECURITY GAP: the clock-in location is checked and then thrown away - "
        f"attendance_logs has no {sorted(missing)} column, so a disputed shift cannot be audited "
        "(historical rows must stay NULL; never backfill them with fabricated coordinates)"
    )

    latitude, longitude = db_rows(
        "SELECT lat, lon FROM attendance_logs WHERE worker_id = ? ORDER BY id DESC LIMIT 1", (MOALLEM,)
    )[0]
    assert abs(latitude - 30.05) < 0.001 and abs(longitude - 31.23) < 0.001, (
        f"stored coordinates {latitude},{longitude} do not match the submitted position 30.05,31.23"
    )
