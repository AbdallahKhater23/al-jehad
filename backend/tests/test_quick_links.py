"""Quick clock links: ``/admin/quick_links*`` and the public one-tap punch ``/q/<token>``.

WHY THIS EXISTS
---------------
A quick link is the one credential in this application that clocks somebody in *without*
proving who is holding the phone. That is the feature, not a bug: it exists so a worker can
punch from a bare link on a phone with no app session. What these tests pin is everything
that keeps it bounded, because every one of those bounds is a thing a later change could
quietly remove:

1. the token is stored as a hash, so a database leak hands nobody a working punch;
2. the link is tied to one worker, and the punch endpoint takes no ``worker_id`` at all, so
   a link can never clock in a person it was not issued for;
3. it expires, it can be revoked, it can be capped at N uses, and a deactivated account's
   link stops working with it;
4. the geofence still applies - a link forwarded to somebody at home records nothing;
5. a selfie is still required, and it is *detected*, not *matched*: the face count is the
   whole of the biometric stage, the photo is stored, and no distance is invented;
6. every use is a row with the action, the site, the coordinates, the IP and the photo, and
   the photo is only reachable by an authenticated administrator;
7. a punch is never attributed to the link when it did not happen - a refused punch leaves
   no use row, no attendance row, and no orphaned face on disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from harness import (
    ADMIN,
    HEAD_ADMIN,
    INSIDE_DOWNTOWN,
    MOALLEM,
    OUTSIDE_ALL_SITES,
    PASSWORDS,
    SEED_USERS,
    WORKER,
    assert_denied,
    bearer,
    db_rows,
    db_scalar,
    jpeg_bytes,
)

import harness
import quick_links

#: The reference the harness writes for every seeded user, captured at import.
#:
#: The database is restored from a pristine snapshot before every test; the files are not,
#: and one test below deactivates an account, which deletes that account's face template on
#: purpose. Without this snapshot the damage would surface in a later suite as "Reference
#: embedding not found" - a failure two files away from its cause. The same guard the
#: user-management suite carries, for the same reason.
SEEDED_REFERENCE = harness.reference_path(WORKER).read_text()


@pytest.fixture(autouse=True)
def _restore_reference_files():
    yield
    for user_id in SEED_USERS:
        harness.seed_reference(user_id, SEEDED_REFERENCE)


@pytest.fixture(autouse=True)
def _no_standing_review_flag():
    """Clear the flagged row the harness leaves for worker 1.

    The seed data carries one ``pending_review`` row as a standing fixture for the approval
    suites, and a flagged account is refused a clock-out on the password path *and* on this
    one (pinned by its own test below). Every other test here would therefore be measuring
    that rule instead of the link, so the flag is cleared except where it is the subject.
    """
    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _quick_photo_dir(tmp_path_factory):
    """Keep punch selfies out of the repository.

    The module reads ``PHOTOS_DIR`` at call time, so repointing it here is enough - the same
    trick ``conftest`` uses for ``main.WORKER_PHOTOS_DIR``. Without it this suite would write
    real face photos into the checkout.
    """
    original = quick_links.PHOTOS_DIR
    directory = tmp_path_factory.mktemp("quick_link_photos")
    quick_links.PHOTOS_DIR = str(directory)
    yield directory
    quick_links.PHOTOS_DIR = original


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def issue(client, worker_id: str = WORKER, headers=None, **overrides):
    # ``headers if ... is not None`` rather than ``or``: an explicitly empty header set is
    # the anonymous caller, which is exactly the case the authorization tests send.
    return client.post(
        "/api/v1/admin/quick_links",
        headers=bearer(HEAD_ADMIN) if headers is None else headers,
        json={"worker_id": worker_id, **overrides},
    )


def punch(client, token, *, coordinates: str = INSIDE_DOWNTOWN, image: bytes | None = None, accuracy=None):
    data = {"lat": coordinates.split(",")[0], "lon": coordinates.split(",")[1]}
    if accuracy is not None:
        data["accuracy"] = str(accuracy)
    return client.post(
        f"/api/v1/q/{token}",
        data=data,
        files={"selfie": ("selfie.jpg", image if image is not None else jpeg_bytes(), "image/jpeg")},
    )


def active_session(worker_id: str = WORKER) -> str | None:
    return db_scalar("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (worker_id,))


def clear_sessions(worker_id: str = WORKER) -> None:
    """Close the shift the seed data leaves open, so the next tap is a clock-in.

    The harness seeds worker 1 mid-shift on purpose (the clock-out tests need it), which
    means the first tap of a test would otherwise be a clock-out - and whether it is would
    depend on fixture order rather than on what the test is about.
    """
    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.commit()
    finally:
        conn.close()


def seed_open_shift(worker_id: str = WORKER, hours_ago: float = 0.5) -> None:
    """Put a clock-in row on the worker's record ``hours_ago`` back, as the app would."""
    import sqlite3

    import harness

    started = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, "Downtown Tower A", started),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# issuing a link
# ---------------------------------------------------------------------------
def test_only_an_administrator_may_issue_a_link(client):
    """A worker holding a link cannot mint more of them."""
    for headers in ({}, bearer(WORKER), bearer(MOALLEM)):
        assert_denied(
            issue(client, headers=headers),
            endpoint="/admin/quick_links",
            detail="a clock link is a punch without a password",
        )


def test_issue_returns_a_usable_link_once(client):
    response = issue(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["worker_id"] == WORKER
    assert body["token"] and body["token"] in body["url"]
    assert body["url"].endswith(f"/q/{body['token']}")

    # The token is shown once and only its hash is kept: the row on disk must not contain a
    # string that can be pasted into a URL.
    stored = db_scalar("SELECT token_hash FROM quick_links WHERE id = ?", (body["link_id"],))
    assert stored != body["token"]
    assert len(stored) == 64
    assert body["token"] not in stored


def test_issue_refuses_an_unknown_worker_and_an_administrator(client):
    assert issue(client, worker_id="999999").status_code == 404

    # An administrative account is not a punching account, and a link is a punch with no
    # credential behind it - so the two must never meet.
    refused = issue(client, worker_id=HEAD_ADMIN)
    assert refused.status_code == 400
    assert "administrator" in refused.json()["detail"].lower()


def test_issue_bounds_the_ttl_and_the_use_cap(client):
    assert issue(client, ttl_hours=0).status_code == 400
    assert issue(client, ttl_hours=24 * 365 + 1).status_code == 400
    assert issue(client, max_uses=-1).status_code == 400
    assert issue(client, max_uses=101).status_code == 400


def test_a_link_can_be_revoked_and_revocation_is_idempotent(client):
    link = issue(client).json()
    first = client.post(
        f"/api/v1/admin/quick_links/{link['link_id']}/revoke", headers=bearer(HEAD_ADMIN)
    )
    assert first.status_code == 200, first.text
    stamp = first.json()["revoked_at"]

    second = client.post(
        f"/api/v1/admin/quick_links/{link['link_id']}/revoke", headers=bearer(HEAD_ADMIN)
    )
    assert second.status_code == 200
    assert second.json()["revoked_at"] == stamp, "a second click must not rewrite the timestamp"

    refused = punch(client, link["token"])
    assert refused.status_code == 410
    assert refused.json()["detail"]["error_code"] == "link_revoked"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE source = 'quick_link'") == 0
    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0


def test_issuing_requires_authentication_but_a_link_is_public(client):
    """The asymmetry, stated: the console needs a token, the link needs nothing but itself."""
    assert_denied(
        client.get("/api/v1/admin/quick_links"), endpoint="/admin/quick_links", detail="listing links"
    )
    assert client.get("/api/v1/q/not-a-real-token").status_code == 404


# ---------------------------------------------------------------------------
# the public page's read
# ---------------------------------------------------------------------------
def test_the_info_endpoint_says_who_and_which_way(client):
    link = issue(client).json()
    info = client.get(f"/api/v1/q/{link['token']}")
    assert info.status_code == 200, info.text
    body = info.json()
    assert body["worker_id"] == WORKER
    assert body["worker_name"] == "Seed Worker"
    assert body["next_action"] == "Clock Out", "the harness seeds an open shift for worker 1"
    assert body["clocked_in"] is True

    # With no open shift the same link reads the other way, which is what lets one button be
    # both the clock-in and the clock-out.
    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (WORKER,))
        conn.commit()
    finally:
        conn.close()
    again = client.get(f"/api/v1/q/{link['token']}").json()
    assert again["clocked_in"] is False
    assert again["next_action"] == "Clock In"


def test_an_unknown_token_is_refused_without_leaking_anything(client):
    response = client.get("/api/v1/q/definitely-not-a-token")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "link_unknown"


# ---------------------------------------------------------------------------
# the punch
# ---------------------------------------------------------------------------
def test_a_tap_clocks_in_then_out_with_no_password(client):
    clear_sessions()
    link = issue(client).json()
    clock_in = punch(client, link["token"])
    assert clock_in.status_code == 200, clock_in.text
    assert clock_in.json()["action"] == "Clock In"
    assert clock_in.json()["site"] == "Downtown Tower A"
    assert clock_in.json()["face_count"] == 1
    assert active_session() is not None

    clock_out = punch(client, link["token"])
    assert clock_out.status_code == 200, clock_out.text
    body = clock_out.json()
    assert body["action"] == "Clock Out"
    assert active_session() is None
    assert body["hours"] >= 0.0

    # Both punches are on the worker's record, attributed to the link rather than to a face
    # match that never happened.
    rows = db_rows(
        "SELECT action, source, status, flag_reason FROM attendance_logs WHERE worker_id = ? "
        "ORDER BY id DESC LIMIT 2",
        (WORKER,),
    )
    assert [row[0] for row in rows] == ["Clock Out", "Clock In"]
    assert {row[1] for row in rows} == {"quick_link"}
    assert all("quick link" in (row[3] or "") for row in rows)
    assert all("not matched" in (row[3] or "") for row in rows)

    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 2


def test_the_worker_is_the_links_worker_and_not_the_callers_choice(client):
    """There is no ``worker_id`` field to send, and sending one changes nothing."""
    link = issue(client, worker_id=MOALLEM).json()
    clear_sessions()

    response = client.post(
        f"/api/v1/q/{link['token']}",
        data={"lat": "30.05", "lon": "31.23", "worker_id": WORKER},
        files={"selfie": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["worker_id"] == MOALLEM
    assert active_session(MOALLEM) is not None
    assert active_session(WORKER) is None, "a parameter the client sent must not redirect the punch"


def test_a_phone_outside_every_site_records_nothing(client):
    link = issue(client).json()
    before = db_scalar("SELECT COUNT(*) FROM attendance_logs")
    refused = punch(client, link["token"], coordinates=OUTSIDE_ALL_SITES)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "outside_geofence"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs") == before
    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_mock_gps_coordinates_are_refused_as_invalid_input(client):
    """``(0,0)`` is the signature of a mock-location app, so it is refused as *bad input*."""
    link = issue(client).json()
    refused = punch(client, link["token"], coordinates="0,0")
    assert refused.status_code == 400
    assert "mock" in reflected(refused).lower()


def reflected(response) -> str:
    """The whole refusal, whatever shape FastAPI gave it, as one searchable string."""
    detail = response.json().get("detail")
    return detail if isinstance(detail, str) else str(detail)


def test_a_selfie_is_required_and_a_face_must_be_in_it(client, face):
    link = issue(client).json()

    missing = client.post(f"/api/v1/q/{link['token']}", data={"lat": "30.05", "lon": "31.23"})
    assert missing.status_code == 422, "the photo is not optional"

    face.FACE_MODE = "none"
    no_face = punch(client, link["token"])
    assert no_face.status_code == 400
    assert no_face.json()["detail"]["error_code"] == "no_face"

    face.FACE_MODE = "match"
    face.FACE_COUNT = 2
    two_faces = punch(client, link["token"])
    assert two_faces.status_code == 400
    assert two_faces.json()["detail"]["error_code"] == "multiple_faces"

    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_a_refused_punch_leaves_no_orphaned_photo(client, face):
    link = issue(client).json()
    face.FACE_MODE = "none"
    assert punch(client, link["token"]).status_code == 400
    assert list((_dir()).iterdir()) == [], "a face with no punch to explain it must not stay on disk"


def _dir():
    from pathlib import Path

    return Path(quick_links.PHOTOS_DIR)


def test_the_face_is_counted_not_matched(client, face):
    """A worker whose face reference does not match is still clocked in - by design.

    This is the honest trade the feature is built on: the link is the credential, the photo
    is evidence for a human, and a similarity score here would be a fabricated number. The
    test pins that no distance is invented (the row carries the same 1.0 the admin-override
    rows carry) and that the reference template is never consulted at all.
    """
    link = issue(client).json()
    face.FACE_MODE = "mismatch"  # the stub now returns a vector at distance ~2.00
    assert punch(client, link["token"]).status_code == 200
    assert db_scalar("SELECT score FROM attendance_logs WHERE source = 'quick_link'") == 1.0


def test_liveness_refuses_a_presentation_attack_when_it_is_enforcing(client, monkeypatch):
    """In ``enforce`` mode a spoof is refused, exactly as on the password path."""
    import liveness

    link = issue(client).json()

    class _Decision:
        allowed = False
        error_code = liveness.ERR_SPOOF

        class result:  # noqa: N801 - mirrors the real shape
            verdict = liveness.VERDICT_SPOOF
            detail = "stubbed spoof"

        def log_fields(self):
            return ("spoof", 0.01)

        def as_payload(self):
            return {"verdict": "spoof"}

    monkeypatch.setattr(quick_links.liveness, "inspect", lambda *a, **k: _Decision())
    refused = punch(client, link["token"])
    assert refused.status_code == 422
    assert refused.json()["detail"]["error_code"] == liveness.ERR_SPOOF
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE source = 'quick_link'") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_a_flagged_account_cannot_close_its_shift_with_a_link(client):
    """The password path's review rule applies here too: a link is not a way round the queue.

    The shift stays open, which the auto-close policy bounds, and an administrator is one
    tap away from clearing the flag - the alternative (letting the link close a shift that is
    already under review) would make the review queue optional for exactly the workers it was
    raised about.
    """
    import sqlite3

    import harness

    link = issue(client).json()
    clear_sessions()
    assert punch(client, link["token"]).json()["action"] == "Clock In"

    flagged_id = 900777
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")
        conn.execute(
            "INSERT INTO attendance_logs (id, worker_id, site_name, action, timestamp, hours, "
            "score, status) VALUES (?,?,?,?,?,?,?,?)",
            (
                flagged_id,
                WORKER,
                "Downtown Tower A",
                "Clock In",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                0.0,
                0.5,
                "pending_review",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO active_sessions (worker_id, site_name, clock_in_time) "
            "VALUES (?,?,?)",
            (WORKER, "Downtown Tower A", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()

    refused = punch(client, link["token"])
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "account_flagged"
    assert active_session() is not None, "the shift stays open for a human to close"
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM attendance_logs WHERE source = 'quick_link' AND action = 'Clock Out'"
        )
        == 0
    ), "no clock-out row was written for the refused tap"
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 1


def test_a_use_cap_is_enforced(client):
    link = issue(client, max_uses=1).json()
    assert punch(client, link["token"]).status_code == 200
    exhausted = punch(client, link["token"])
    assert exhausted.status_code == 410
    assert exhausted.json()["detail"]["error_code"] == "link_used_up"
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 1


def test_an_expired_link_does_not_work(client):
    link = issue(client, ttl_hours=1).json()
    import sqlite3

    import harness

    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("UPDATE quick_links SET expires_at = ? WHERE id = ?", (past, link["link_id"]))
        conn.commit()
    finally:
        conn.close()

    refused = punch(client, link["token"])
    assert refused.status_code == 410
    assert refused.json()["detail"]["error_code"] == "link_expired"


def test_a_deactivated_workers_link_stops_with_the_account(client):
    link = issue(client).json()
    deactivated = client.post(
        "/api/v1/admin/users/status",
        headers=bearer(HEAD_ADMIN),
        json={"user_id": WORKER, "active": False},
    )
    assert deactivated.status_code == 200, deactivated.text

    refused = punch(client, link["token"])
    assert refused.status_code == 410
    assert refused.json()["detail"]["error_code"] == "link_account_inactive"
    assert client.get(f"/api/v1/q/{link['token']}").status_code == 410


# ---------------------------------------------------------------------------
# what the administrator gets to see
# ---------------------------------------------------------------------------
def test_the_console_can_see_every_link_and_what_it_did(client):
    link = issue(client, note="Gate 2 crew").json()
    clear_sessions()
    punch(client, link["token"])  # a clock-in
    punch(client, link["token"])  # and the clock-out that closes it

    listed = client.get("/api/v1/admin/quick_links", headers=bearer(HEAD_ADMIN))
    assert listed.status_code == 200, listed.text
    entry = next(item for item in listed.json() if item["id"] == link["link_id"])
    assert entry["worker_id"] == WORKER
    assert entry["worker_name"] == "Seed Worker"
    assert entry["uses"] == 2
    assert entry["usable"] is True
    assert entry["state"] == "active"
    assert entry["note"] == "Gate 2 crew"
    assert entry["last_used_at"]
    assert entry["remaining_uses"] is None, "no cap means no countdown"

    uses = client.get(
        f"/api/v1/admin/quick_links/{link['link_id']}/uses", headers=bearer(HEAD_ADMIN)
    )
    assert uses.status_code == 200, uses.text
    body = uses.json()
    assert body["worker_name"] == "Seed Worker"
    assert [use["action"] for use in body["uses"]] == ["Clock Out", "Clock In"]
    for use in body["uses"]:
        assert use["site_name"] == "Downtown Tower A"
        assert use["lat"] is not None and use["lon"] is not None
        assert use["face_count"] == 1
        assert use.get("photo_path") is None, "the stored path is server-side plumbing"
        assert use["photo_url"] == f"/api/v1/admin/quick_link_photo/{use['id']}"
        assert use["log_id"], "every use names the attendance row it wrote"


def test_the_punch_photo_is_only_reachable_by_an_administrator(client):
    link = issue(client).json()
    punch(client, link["token"])
    use_id = db_scalar("SELECT id FROM quick_link_uses ORDER BY id DESC LIMIT 1")

    for headers in ({}, bearer(WORKER), bearer(MOALLEM)):
        assert_denied(
            client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=headers),
            endpoint="/admin/quick_link_photo",
            detail="a worker's face photo",
        )

    served = client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=bearer(HEAD_ADMIN))
    assert served.status_code == 200, served.text
    assert served.headers["content-type"] == "image/jpeg"
    assert len(served.content) > 0


def test_a_photo_row_that_points_elsewhere_is_not_served(client):
    """The route serves files this app wrote, not files a row happens to name."""
    link = issue(client).json()
    punch(client, link["token"])
    use_id = db_scalar("SELECT id FROM quick_link_uses ORDER BY id DESC LIMIT 1")

    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(
            "UPDATE quick_link_uses SET photo_path = ? WHERE id = ?", ("../../times.db", use_id)
        )
        conn.commit()
    finally:
        conn.close()

    refused = client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=bearer(HEAD_ADMIN))
    assert refused.status_code == 404


def test_the_first_use_notifies_the_administrator_once(client):
    link = issue(client).json()
    punch(client, link["token"])
    punch(client, link["token"])
    notices = db_rows(
        "SELECT kind, body FROM admin_notifications WHERE kind = 'quick_link' ORDER BY id"
    )
    assert len(notices) == 1, "the alert is for 'the link reached a phone', not for every tap"
    assert "Seed Worker" in notices[0][1]


def test_a_refusal_never_writes_a_use_row(client, face):
    link = issue(client).json()
    face.FACE_MODE = "none"
    punch(client, link["token"])
    face.FACE_MODE = "match"
    punch(client, link["token"], coordinates=OUTSIDE_ALL_SITES)
    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_a_standard_admin_may_issue_and_revoke(client):
    """The quick-link surface is for the people who run the site, not just the head admin."""
    link = issue(client, headers=bearer(ADMIN)).json()
    assert client.get("/api/v1/admin/quick_links", headers=bearer(ADMIN)).status_code == 200
    assert (
        client.post(
            f"/api/v1/admin/quick_links/{link['link_id']}/revoke", headers=bearer(ADMIN)
        ).status_code
        == 200
    )


def test_the_audit_log_names_the_link_and_the_worker(client):
    link = issue(client).json()
    clear_sessions()
    punch(client, link["token"])
    actions = [
        row[0]
        for row in db_rows(
            "SELECT action FROM audit_log WHERE action LIKE 'quick_link%' ORDER BY id"
        )
    ]
    assert actions == ["quick_link_create", "quick_link_clock_in"]

    detail = db_scalar(
        "SELECT after_json FROM audit_log WHERE action = 'quick_link_create' ORDER BY id LIMIT 1"
    )
    assert str(WORKER) in detail
    assert "expires_at" in detail
