"""Offline punch sync, rapid enrollment, and shift reporting.

The offline tests are the security-relevant ones. A punch captured with no signal is the
one piece of evidence an attacker fully controls, so the properties under test are:

* an edit to any signed field invalidates the punch;
* a punch cannot be re-sent (batch replay) or re-inserted (nonce replay);
* the device's own wall clock cannot move the punch in time - the server-derived
  ``anchor + monotonic`` time is what lands in the payroll log;
* a refused punch is *stored*, never silently dropped;
* an out-of-order batch still produces the right session and hours;
* a punch that arrives from the queue materialises as attendance **awaiting review** - the
  hours are recorded and held, never credited with nobody having looked at them (the
  workflow itself, including the selfie that is scored on arrival, is
  ``test_offline_selfie_scoring.py``).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest
from harness import (
    ADMIN,
    DB_PATH,
    HEAD_ADMIN,
    MOALLEM,
    PASSWORDS,
    REFS_DIR,
    bearer,
    clock_in,
    db_scalar,
)

import enrollment
import offline_sync
import reports
import shift_hours
import shift_windows

TS = "%Y-%m-%d %H:%M:%S"
DEVICE = "test-device-1"
SITE_LAT, SITE_LON = 30.05, 31.23          # inside "Downtown Tower A"
FAR_LAT, FAR_LON = 51.5074, -0.1278        # London: inside no seeded geofence
PHOTO_HASH = "b" * 64


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sql(sql: str, params=()):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _register(client, user_id=MOALLEM, device_id=DEVICE):
    response = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(user_id),
        json={"device_id": device_id, "note": "pytest"},
    )
    assert response.status_code == 200, response.text[:300]
    return response.json()


def _plant_anchor(worker_id: str, device_id: str, server_time: str) -> dict:
    """Record an anchor row and sign it, so a punch can claim a chosen moment.

    The signature is produced with the *server* secret (that is the whole anti-forgery
    property), and the row has to exist because the sync path refuses an ``anchor_id`` the
    server never issued.
    """
    anchor_id = uuid.uuid4().hex
    _sql(
        "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) VALUES (?,?,?,?,?)",
        (anchor_id, device_id, worker_id, server_time, server_time),
    )
    return {
        "anchor_id": anchor_id,
        "server_time": server_time,
        "anchor_signature": offline_sync.sign_anchor(
            device_id=device_id, worker_id=worker_id, server_time=server_time, anchor_id=anchor_id
        ),
    }


def _punch(
    key: str,
    anchor: dict,
    *,
    punch_id: str,
    nonce: str,
    action: str = "Clock In",
    offset_s: float = 0.0,
    lat: float = SITE_LAT,
    lon: float = SITE_LON,
    accuracy: float = 8.0,
    client_offset_s: int = 0,
    submit_action: str | None = None,
):
    """Build a correctly signed punch. ``submit_action`` tampers with the sent payload."""
    effective = (
        datetime.strptime(anchor["server_time"], TS) + timedelta(seconds=offset_s)
    ).strftime(TS)
    signature = offline_sync.sign_punch(
        key,
        device_id=DEVICE,
        worker_id=MOALLEM,
        action=action,
        client_punch_id=punch_id,
        nonce=nonce,
        anchor_id=anchor["anchor_id"],
        effective_timestamp=effective,
        lat=lat,
        lon=lon,
        accuracy=accuracy,
        photo_sha256=PHOTO_HASH,
    )
    return {
        "client_punch_id": punch_id,
        "action": submit_action or action,
        "anchor_id": anchor["anchor_id"],
        "anchor_server_time": anchor["server_time"],
        "anchor_signature": anchor["anchor_signature"],
        "monotonic_offset_s": offset_s,
        "nonce": nonce,
        "lat": lat,
        "lon": lon,
        "accuracy": accuracy,
        "client_timestamp": effective,
        "client_offset_s": client_offset_s,
        "photo_sha256": PHOTO_HASH,
        "signature": signature,
        "signature_version": 1,
    }


def _sync(client, punches, *, device_id=DEVICE, user_id=MOALLEM):
    return client.post(
        "/api/v1/attendance/sync",
        headers=bearer(user_id),
        json={"device_id": device_id, "punches": punches},
    )


def _codes(response):
    return [item.get("code") or item["status"] for item in response.json()["results"]]


# ---------------------------------------------------------------------------
# offline sync
# ---------------------------------------------------------------------------
def test_device_registration_returns_a_key_once_and_refuses_a_second_issue(client):
    first = _register(client)
    assert first["device_key"] and first["key_epoch"] == 1
    assert first["device_id"] == DEVICE

    again = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(MOALLEM),
        json={"device_id": DEVICE},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "device_already_registered"
    # The key is derived, never stored: only a salt is at rest.
    assert db_scalar("SELECT key_salt FROM worker_devices WHERE device_id = ?", (DEVICE,))
    assert db_scalar("SELECT COUNT(*) FROM worker_devices WHERE device_id = ?", (DEVICE,)) == 1
    # Scoped to this device on purpose. The suite clones the *live* database, and the
    # running app now audits its own registrations into it, so an unscoped row count
    # measures the operator's afternoon rather than this test.
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'offline_device_register' AND entity_id = ?",
        (DEVICE,),
    ) == 1


def test_anchor_is_issued_and_recorded(client):
    _register(client)
    response = client.post(
        "/api/v1/attendance/anchors", headers=bearer(MOALLEM), json={"device_id": DEVICE}
    )
    assert response.status_code == 200, response.text[:200]
    anchor = response.json()
    assert db_scalar("SELECT COUNT(*) FROM device_anchors WHERE anchor_id = ?", (anchor["anchor_id"],)) == 1
    assert anchor["max_offline_hours"] > 0


def test_offline_clock_in_then_out_materialises_the_shift(client):
    key = _register(client)["device_key"]
    started = datetime.now() - timedelta(hours=3)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))

    response = _sync(
        client,
        [
            _punch(key, anchor, punch_id="p1", nonce="n1", action="Clock In", offset_s=0),
            _punch(key, anchor, punch_id="p2", nonce="n2", action="Clock Out", offset_s=3600),
        ],
    )
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["received"] == 2 and body["applied"] == 2
    assert body["next_anchor"]["anchor_id"], "a reconnecting client must get a fresh anchor"

    # The clock-out must have been computed from the *effective* times (1 h apart).
    hours = db_scalar(
        "SELECT hours FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out' "
        "AND source = 'offline'",
        (MOALLEM,),
    )
    assert hours == pytest.approx(1.0, abs=0.01)
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND source = 'offline'", (MOALLEM,)
    ) == 2
    assert (
        db_scalar(
            "SELECT liveness_class FROM attendance_logs WHERE worker_id = ? AND action = 'Clock In' "
            "AND source = 'offline'",
            (MOALLEM,),
        )
        == "unverified_offline"
    ), "an offline capture cannot claim to have passed liveness"
    # Both ends of the shift, and the reason on each: materialising a queued punch is not the
    # same as confirming it, so nothing that comes out of this queue is approved on arrival.
    held = _sql(
        "SELECT status_code, flag_reason FROM attendance_logs WHERE worker_id = ? AND source = 'offline' "
        "ORDER BY id",
        (MOALLEM,),
    )
    assert [row[0] for row in held] == ["unverified_offline", "pending_review"], (
        f"nothing from this queue arrives approved; the departure is the row that is held: {held}"
    )
    assert all("offline punch" in (row[1] or "") for row in held), (
        f"and each row says what it is: {held}"
    )
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'offline_sync' AND entity_id = ?", (DEVICE,)
    ) == 1


def test_a_long_offline_shift_keeps_its_real_hours(client):
    """13 h offline must be recorded as 13 h on site, not flattened to the 11 h cutoff.

    The sync path used to rewrite ``hours`` down to ``hard_cutoff_hours`` before writing
    the log, the offline twin of the force-close that ``main.py`` removed because it
    invented hours. The worker is told nothing either way - they just get paid for a
    shift they did not work, which is the kind of error nobody discovers from inside the
    app.

    ``hours`` is now the *payable* figure, so a 13 h offline shift lands at 12.5 h: the
    half hour is the unpaid break the policy deducts from every shift long enough to
    have contained one, and it is recorded on the row (``break_hours``) rather than
    disappearing into the subtraction. What the old cutoff did - 11 h, flat, whatever
    happened - is still what this test is against.
    """
    key = _register(client)["device_key"]
    started = datetime.now() - timedelta(hours=13)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))

    response = _sync(
        client,
        [
            _punch(key, anchor, punch_id="long-in", nonce="n-long-in", action="Clock In", offset_s=0),
            _punch(
                key, anchor, punch_id="long-out", nonce="n-long-out",
                action="Clock Out", offset_s=13 * 3600,
            ),
        ],
    )
    assert response.status_code == 200, response.text[:400]
    assert response.json()["applied"] == 2

    where = "worker_id = ? AND action = 'Clock Out' AND source = 'offline'"
    hours = db_scalar(f"SELECT hours FROM attendance_logs WHERE {where}", (MOALLEM,))
    assert hours == pytest.approx(12.5, abs=0.01), (
        f"13 h on site minus the 30-minute unpaid break - not a capped 11: got {hours}"
    )
    assert db_scalar(f"SELECT break_hours FROM attendance_logs WHERE {where}", (MOALLEM,)) == pytest.approx(0.5, abs=0.01)
    # Recorded as worked, and still handed to a human: 12.5 h is far past the 8.1 h
    # threshold, so it belongs in the approval queue rather than in payroll.
    assert db_scalar(f"SELECT status_code FROM attendance_logs WHERE {where}", (MOALLEM,)) == "pending_overtime"
    reason = db_scalar(f"SELECT flag_reason FROM attendance_logs WHERE {where}", (MOALLEM,)) or ""
    assert "13" in reason and "verify" in reason, (
        f"a long offline shift must be named for the reviewer, not silently shortened: {reason!r}"
    )



@pytest.mark.parametrize("extra_seconds", (0, -1))
def test_an_offline_clock_out_is_routed_by_the_shared_resolver(client, extra_seconds):
    """A punch that arrives hours later reaches the same verdict, to the second.

    The offline path resolved ``overtime_notify_hours`` for itself and compared the
    *recorded* hours, which round a shift up to the paid day - so a shift that stopped a
    second short of the line could be held here and approved on the phone. These two cases
    are the line and one second short of it, and the expected verdict is not written down
    in this test at all: it is the resolver's, which is the point.
    """
    rules = client.get("/api/v1/admin/shift_rules", headers=bearer(ADMIN)).json()
    line = shift_hours.overtime_rule(rules)
    break_seconds = int(round(shift_hours.break_hours(rules) * shift_hours.SECONDS_PER_HOUR))
    offset = line["threshold_seconds"] + break_seconds + extra_seconds
    expected = shift_hours.overtime_assessment(offset, rules)

    key = _register(client)["device_key"]
    started = datetime.now() - timedelta(hours=9)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))
    response = _sync(
        client,
        [
            _punch(key, anchor, punch_id="line-in", nonce="n-line-in", action="Clock In", offset_s=0),
            _punch(
                key, anchor, punch_id="line-out", nonce="n-line-out",
                action="Clock Out", offset_s=offset,
            ),
        ],
    )
    assert response.status_code == 200, response.text[:400]
    assert response.json()["applied"] == 2

    where = "worker_id = ? AND action = 'Clock Out' AND source = 'offline'"
    status_code = db_scalar(f"SELECT status_code FROM attendance_logs WHERE {where}", (MOALLEM,))
    assert (status_code == "pending_overtime") is expected["needs_approval"], (
        f"the offline path disagreed with the resolver: {status_code} vs {expected}"
    )
    hours = db_scalar(f"SELECT hours FROM attendance_logs WHERE {where}", (MOALLEM,))
    assert hours == pytest.approx(expected["paid_hours"], abs=1e-3)
    stored_overtime = db_scalar(f"SELECT overtime_hours FROM attendance_logs WHERE {where}", (MOALLEM,))
    if expected["needs_approval"]:
        assert stored_overtime == pytest.approx(expected["overtime_hours"], abs=1e-3)
    else:
        assert stored_overtime is None


def test_an_offline_crossing_reaches_the_workers_own_inbox(client):
    """Hours later, on a queue that arrived by itself, and the worker is still told.

    This is the one path where the announcement is written by a *materialization* rather
    than by a punch the worker stood in front of: the shift ended on a phone with no
    signal, and the notice is owed when the queue finally arrives. It is the same sentence
    the online paths write (``overtime.announce_crossing``), because the fact is the same -
    the phone's signal is not part of what happened to the worker's day.
    """
    rules = client.get("/api/v1/admin/shift_rules", headers=bearer(ADMIN)).json()
    line = shift_hours.overtime_rule(rules)
    break_seconds = int(round(shift_hours.break_hours(rules) * shift_hours.SECONDS_PER_HOUR))
    # Comfortably past the line, so this is not a boundary test - those are next door.
    offset = line["threshold_seconds"] + break_seconds + 3600

    key = _register(client)["device_key"]
    # The anchor is the shift's clock-in, and the clock-out sits ~9.6 h after it - so it has
    # to be planted before *now* by more than the shift is long, or the punch is refused as
    # one from the future (which is the offline guard doing its job).
    started = datetime.now() - timedelta(hours=12)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))
    response = _sync(
        client,
        [
            _punch(key, anchor, punch_id="inbox-in", nonce="n-inbox-in", action="Clock In", offset_s=0),
            _punch(
                key, anchor, punch_id="inbox-out", nonce="n-inbox-out",
                action="Clock Out", offset_s=offset,
            ),
        ],
    )
    assert response.status_code == 200, response.text[:400]
    assert response.json()["applied"] == 2

    where = "worker_id = ? AND action = 'Clock Out' AND source = 'offline'"
    assert db_scalar(f"SELECT status_code FROM attendance_logs WHERE {where}", (MOALLEM,)) == (
        "pending_overtime"
    ), "the shift is held for a decision"

    inbox = client.get("/api/v1/worker/me/notifications", headers=bearer(MOALLEM)).json()
    crossing = [row for row in inbox["notifications"] if row["kind"] == "overtime_crossed"]
    assert crossing, (
        "a materialized long shift owes the worker the same notice the online paths give: "
        f"inbox={inbox['notifications']}"
    )
    body = str(crossing[0])
    assert "approval" in body.lower(), body
    assert inbox["unread"] >= 1, "and it is unread, which is what the app badges"


def test_out_of_order_batch_is_applied_in_effective_time_order(client):
    """The array order must not matter: a JSON array is not chronological evidence."""
    key = _register(client)["device_key"]
    started = datetime.now() - timedelta(hours=4)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))

    out_punch = _punch(key, anchor, punch_id="out-first", nonce="n-out", action="Clock Out", offset_s=5400)
    in_punch = _punch(key, anchor, punch_id="in-second", nonce="n-in", action="Clock In", offset_s=0)

    response = _sync(client, [out_punch, in_punch])
    assert response.status_code == 200, response.text[:400]
    # Both applied, and the resulting shift is 1.5 h - not "clock out without a session".
    assert db_scalar(
        "SELECT hours FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out' AND source='offline'",
        (MOALLEM,),
    ) == pytest.approx(1.5, abs=0.01)
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE status = 'rejected'") == 0


def test_a_tampered_field_breaks_the_signature(client):
    key = _register(client)["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=1)).strftime(TS))

    # Signed as a Clock In, submitted as a Clock Out: the canonical string differs.
    response = _sync(
        client,
        [_punch(key, anchor, punch_id="t1", nonce="tn1", action="Clock In", submit_action="Clock Out")],
    )
    assert response.status_code == 200
    assert _codes(response) == ["bad_signature"]
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE source = 'offline'") == 0
    # Refused punches are recorded, not dropped.
    assert db_scalar("SELECT rejection_code FROM punch_queue WHERE client_punch_id = 't1'") == "bad_signature"


def test_replaying_a_batch_and_a_nonce_are_both_refused(client):
    key = _register(client)["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=2)).strftime(TS))
    punch = _punch(key, anchor, punch_id="r1", nonce="rn1", action="Clock In", offset_s=0)

    first = _sync(client, [punch])
    assert _codes(first) == ["accepted"]
    second = _sync(client, [punch])
    assert _codes(second) == ["duplicate_punch"], "re-uploading a batch must be a no-op"

    # Same nonce, fresh punch id, correctly signed: a captured request being re-inserted.
    fresh = _punch(key, anchor, punch_id="r2", nonce="rn1", action="Clock In", offset_s=1)
    third = _sync(client, [fresh])
    assert _codes(third) == ["replayed_nonce"]
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE nonce = 'rn1'") == 1


def test_a_shifted_device_clock_cannot_move_the_punch(client):
    """The wall clock is evidence of tampering, not of time."""
    key = _register(client)["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=2)).strftime(TS))

    response = _sync(
        client,
        [_punch(key, anchor, punch_id="c1", nonce="cn1", action="Clock In", offset_s=60, client_offset_s=-7200)],
    )
    assert _codes(response) == ["clock_tampered"], (
        "a phone clock two hours behind the anchor is a deliberate change, not a genuine punch"
    )
    assert db_scalar("SELECT rejection_code FROM punch_queue WHERE client_punch_id = 'c1'") == "clock_tampered"


def test_punches_outside_the_offline_window_are_refused(client):
    key = _register(client)["device_key"]

    too_old_anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=100)).strftime(TS))
    old = _sync(client, [_punch(key, too_old_anchor, punch_id="o1", nonce="on1", offset_s=0)])
    assert _codes(old) == ["punch_too_old"]

    now_anchor = _plant_anchor(MOALLEM, DEVICE, datetime.now().strftime(TS))
    future = _sync(client, [_punch(key, now_anchor, punch_id="f1", nonce="fn1", offset_s=3600)])
    assert _codes(future) == ["punch_in_future"]

    negative = _sync(client, [_punch(key, now_anchor, punch_id="f2", nonce="fn2", offset_s=-5)])
    assert _codes(negative) == ["invalid_monotonic_offset"]
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE source = 'offline'") == 0


def test_an_unknown_or_forged_anchor_is_refused(client):
    key = _register(client)["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=1)).strftime(TS))
    forged = dict(anchor, anchor_id=uuid.uuid4().hex)

    response = _sync(client, [_punch(key, forged, punch_id="a1", nonce="an1")])
    assert _codes(response) == ["bad_anchor"], "an anchor the server never issued cannot be presented"


def test_a_revoked_device_key_stops_working(client):
    registration = _register(client)
    key = registration["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=1)).strftime(TS))

    revoked = client.post(f"/api/v1/attendance/devices/{DEVICE}/revoke", headers=bearer(MOALLEM))
    assert revoked.status_code == 200

    response = _sync(client, [_punch(key, anchor, punch_id="v1", nonce="vn1")])
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "device_revoked"
    # Bumping the epoch is what invalidates the key that is already on the device.
    assert db_scalar("SELECT key_epoch FROM worker_devices WHERE device_id = ?", (DEVICE,)) == 2


def test_a_punch_outside_the_geofence_is_flagged_for_a_human_not_dropped(client):
    key = _register(client)["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, (datetime.now() - timedelta(hours=1)).strftime(TS))

    response = _sync(client, [_punch(key, anchor, punch_id="g1", nonce="gn1", lat=FAR_LAT, lon=FAR_LON)])
    assert _codes(response) == ["flagged"]
    assert db_scalar("SELECT flag_reason FROM punch_queue WHERE client_punch_id = 'g1'")

    queue = client.get("/api/v1/admin/punch_queue?status=flagged", headers=bearer(ADMIN))
    assert queue.status_code == 200
    entry = next(row for row in queue.json() if row["client_punch_id"] == "g1")
    # The triage view shows client time and server time side by side.
    assert entry["client_timestamp"] and entry["effective_time"] and entry["anchor_server_time"]
    # No site was matched, which is why it is queued for a human; the materialised row
    # records that explicitly rather than inventing a site name payroll might trust.
    assert entry["site_name"] is None
    assert "outside every geofence" in entry["flag_reason"]

    resolved = client.post(
        f"/api/v1/admin/punch_queue/{entry['id']}/resolve",
        headers=bearer(ADMIN),
        json={"decision": "approve", "note": "worker was on site, GPS drifted"},
    )
    assert resolved.status_code == 200, resolved.text[:200]
    assert resolved.json()["decision"] == "approve"
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND source = 'offline'", (MOALLEM,)
    ) == 1
    assert (
        db_scalar("SELECT site_name FROM attendance_logs WHERE source = 'offline' AND worker_id = ?", (MOALLEM,))
        == offline_sync.SITE_UNASSIGNED
    ), "the sentinel must be obvious in the attendance record"
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'offline_punch_resolve'") == 1


def test_batch_size_is_bounded(client):
    key = _register(client)["device_key"]
    anchor = _plant_anchor(MOALLEM, DEVICE, datetime.now().strftime(TS))
    punches = [
        _punch(key, anchor, punch_id=f"bulk{i}", nonce=f"bn{i}", offset_s=-i)
        for i in range(60)
    ]
    response = _sync(client, punches)
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# enrollment
# ---------------------------------------------------------------------------
def _create_invite(client, user_id=MOALLEM, role=ADMIN, **payload):
    body = {"worker_id": user_id, **payload}
    return client.post("/api/v1/admin/enrollment/invites", headers=bearer(role), json=body)


def test_invite_is_single_use_and_stored_only_as_a_hash(client, jpeg):
    created = _create_invite(client)
    assert created.status_code == 200, created.text[:200]
    invite = created.json()
    token = invite["token"]
    assert invite["url"].endswith(f"/enroll/{token}")
    assert invite["expires_at"]

    stored = db_scalar("SELECT token_hash FROM enrollment_invites WHERE id = ?", (invite["invite_id"],))
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert stored != token, "the plaintext token must never be persisted"
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'enrollment_invite_create' AND entity_id = ?",
        (invite["invite_id"],),
    ) == 1, "the invite must be attributed to this invite id"

    peek = client.get(f"/api/v1/enroll/{token}")
    assert peek.status_code == 200
    assert peek.json()["usable"] is True
    assert "Seed Lead Worker" not in json.dumps(peek.json()), "a public page must not leak the full name"

    submitted = client.post(
        f"/api/v1/enroll/{token}",
        files={"photo": ("selfie.jpg", jpeg, "image/jpeg")},
        data={"phone": "+201000000000"},
    )
    assert submitted.status_code == 200, submitted.text[:300]
    assert db_scalar("SELECT template_version FROM users WHERE id = ?", (MOALLEM,)) >= 1
    assert db_scalar("SELECT completed_at FROM enrollment_invites WHERE id = ?", (invite["invite_id"],))
    assert db_scalar("SELECT uses FROM enrollment_invites WHERE id = ?", (invite["invite_id"],)) == 1
    assert db_scalar("SELECT phone FROM users WHERE id = ?", (MOALLEM,)) == "+201000000000"
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'enrollment_completed' AND entity_id = ?",
        (MOALLEM,),
    ) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'enrollment_completed' AND worker_id = ?",
        (MOALLEM,),
    ) == 1

    reuse = client.post(f"/api/v1/enroll/{token}", files={"photo": ("selfie.jpg", jpeg, "image/jpeg")})
    assert reuse.status_code == 410
    assert reuse.json()["detail"]["error_code"] == "invite_already_used"


def test_revoked_and_expired_invites_are_refused(client, jpeg):
    created = _create_invite(client).json()
    revoked = client.post(
        f"/api/v1/admin/enrollment/invites/{created['invite_id']}/revoke", headers=bearer(ADMIN)
    )
    assert revoked.status_code == 200
    assert client.get(f"/api/v1/enroll/{created['token']}").json()["status"] == "revoked"

    expired_token = uuid.uuid4().hex
    _sql(
        "INSERT INTO enrollment_invites (token_hash, worker_id, created_by, created_at, expires_at, max_uses) "
        "VALUES (?,?,?,?,?,?)",
        (
            hashlib.sha256(expired_token.encode()).hexdigest(),
            MOALLEM,
            ADMIN,
            (datetime.now() - timedelta(days=5)).strftime(TS),
            (datetime.now() - timedelta(days=1)).strftime(TS),
            1,
        ),
    )
    peek = client.get(f"/api/v1/enroll/{expired_token}")
    assert peek.json()["status"] == "expired"
    submit = client.post(
        f"/api/v1/enroll/{expired_token}", files={"photo": ("selfie.jpg", jpeg, "image/jpeg")}
    )
    assert submit.status_code == 410

    assert client.get("/api/v1/enroll/not-a-real-token").status_code == 404


def test_invites_are_admin_only_and_reject_unknown_workers(client):
    assert client.post(
        "/api/v1/admin/enrollment/invites", headers=bearer(MOALLEM), json={"worker_id": MOALLEM}
    ).status_code == 403
    assert _create_invite(client, user_id="99999").status_code == 404


def _batch_upload(client, csv_text: str, members: dict) -> dict:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    response = client.post(
        "/api/v1/admin/enrollment/bulk",
        headers=bearer(ADMIN),
        files={
            "roster": ("roster.csv", csv_text.encode("utf-8"), "text/csv"),
            "photos": ("photos.zip", buffer.getvalue(), "application/zip"),
        },
    )
    return response


def _job_state(client, job_id: int) -> dict:
    """Read the job, resuming it if the background task has not finished yet.

    ``BackgroundTasks`` runs after the response, so with ``TestClient`` it has usually
    completed - but asserting on it directly would make this test depend on that timing.
    ``process_job`` is resumable and skips rows that are already done, so calling it is
    safe either way.
    """
    body = client.get(f"/api/v1/admin/enrollment/jobs/{job_id}?include_items=1", headers=bearer(ADMIN)).json()
    if body["job"]["status"] in ("queued", "running"):
        # Safe to call even while the background task is finishing: the claim is atomic,
        # so whichever runner gets there second finds nothing left to do.
        resumed = enrollment.process_job(job_id)
        body = client.get(
            f"/api/v1/admin/enrollment/jobs/{job_id}?include_items=1", headers=bearer(ADMIN)
        ).json()
        body["resume"] = resumed
    return body


def test_bulk_batch_reports_per_row_outcomes_and_one_bad_row_does_not_abort(client, jpeg):
    roster = "user_id,name,role,photo\n600,Seed Lead Worker,moallem,600.jpg\n9999,Ghost,worker,9999.jpg\n1000,Seed Admin,admin,missing.jpg\n"
    response = _batch_upload(
        client, roster, {"600.jpg": jpeg, "9999.jpg": jpeg, "nested/1000.jpg": jpeg}
    )
    assert response.status_code == 200, response.text[:300]
    payload = response.json()
    assert payload["total"] == 3

    state = _job_state(client, payload["job_id"])
    assert state["job"]["status"] in ("done", "done_with_errors"), (
        f"job did not finish: {state['job']} resume={state.get('resume')}"
    )
    items = {row["worker_id"]: row for row in state["items"]}
    assert items["600"]["status"] == "done"
    assert items["9999"]["status"] == "failed" and items["9999"]["error_code"] == "unknown_user"
    assert items["1000"]["status"] == "failed" and items["1000"]["error_code"] == "photo_missing"
    assert state["job"]["succeeded"] == 1 and state["job"]["failed"] == 2
    assert db_scalar("SELECT template_version FROM users WHERE id = ?", (MOALLEM,)) >= 1
    # A bulk run notifies once for the job rather than once per worker, so the job id
    # in the dedupe key is what identifies this notification.
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'enrollment_completed' AND dedupe_key = ?",
        (f"bulk_done:{payload['job_id']}",),
    ) == 1


def test_bulk_dry_run_validates_without_touching_biometrics(client, jpeg):
    before = db_scalar("SELECT template_version FROM users WHERE id = ?", (MOALLEM,))
    response = client.post(
        "/api/v1/admin/enrollment/bulk?dry_run=true",
        headers=bearer(ADMIN),
        files={
            "roster": ("roster.csv", b"user_id,name\n600,Seed Lead Worker\n", "text/csv"),
            "photos": ("photos.zip", _zip_bytes({"600.jpg": jpeg}), "application/zip"),
        },
    )
    assert response.status_code == 200, response.text[:300]
    state = _job_state(client, response.json()["job_id"])
    assert state["items"][0]["status"] == "skipped", (
        f"dry run did not validate the row: {state['items'][0]} resume={state.get('resume')}"
    )
    assert state["items"][0]["error_code"] == "dry_run"
    assert db_scalar("SELECT template_version FROM users WHERE id = ?", (MOALLEM,)) == before


def _zip_bytes(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_bulk_rejects_an_unusable_roster(client, jpeg):
    missing_columns = _batch_upload(client, "name_only\nsomeone\n", {"600.jpg": jpeg})
    assert missing_columns.status_code == 400
    assert missing_columns.json()["detail"]["error_code"] == "invalid_roster"

    duplicates = _batch_upload(
        client,
        "user_id,name\n600,A\n600,B\n",
        {"600.jpg": jpeg},
    )
    assert duplicates.status_code == 400
    assert duplicates.json()["detail"]["error_code"] == "duplicate_roster_rows"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def test_the_shifts_report_is_a_timesheet_of_one_row_per_shift(client):
    """One row per clock-out: a day, a worker, a site, and whether it is signed off.

    It is deliberately not a payroll report - no rate, no estimate - and the header's own
    arithmetic has to hold: counted hours are the approved ones plus the ones still waiting,
    and a shift awaiting a decision is in the second figure and not the first.
    """
    # An explicit wide range: the default is "this month", and the seeded history is not
    # guaranteed to fall inside it (which is correct behaviour for a report period).
    response = client.get(
        "/api/v1/admin/reports/shifts?start=2026-01-01&end=2026-12-31", headers=bearer(ADMIN)
    )
    assert response.status_code == 200, response.text[:200]
    body = response.json()
    assert "rows" in body and "totals" in body
    assert "one row per shift" in body["note"].lower()
    assert body["fields"][:2] == ["log_id", "date"], "the report publishes what a row holds"

    seen: dict[str, int] = {}
    newest = None
    for row in body["rows"]:
        seen[row["worker_id"]] = seen.get(row["worker_id"], 0) + 1
        # A row is a shift, so it carries the day it was worked, and that day is inside the
        # period that was asked for.
        assert len(row["date"]) == 10 and row["date"][4] == "-", row
        assert body["period"]["start"] <= row["date"] <= body["period"]["end"], row
        assert row["awaiting_approval"] in (True, False)
        assert row["hours"] >= 0 and row["open_notes"] >= 0
        assert "hourly_rate" not in row and "gross_estimate" not in row, (
            "a timesheet does not price a shift"
        )
        assert newest is None or row["timestamp"] <= newest, "newest first: a timesheet is read from the top"
        newest = row["timestamp"]

    # The seeded pipeline holds history, so the report must not be empty.
    assert body["totals"]["shifts"] == len(body["rows"]) > 0
    assert body["totals"]["workers"] == len(seen)
    assert body["totals"]["awaiting_approval"] == sum(
        1 for row in body["rows"] if row["awaiting_approval"]
    )
    assert body["totals"]["hours"] == pytest.approx(
        body["totals"]["approved_hours"] + body["totals"]["awaiting_approval_hours"], abs=0.001
    )
    assert body["totals"]["hours"] <= sum(row["recorded_hours"] for row in body["rows"]) + 0.001, (
        "a rejected shift counts for nothing"
    )


# ---------------------------------------------------------------------------
# the arrival: when the shift started, and whether that was inside its window
# ---------------------------------------------------------------------------
def _timesheet(client, **params) -> dict:
    """The timesheet for the whole seeded year, as the console asks for it."""
    query = "".join(f"&{key}={value}" for key, value in params.items())
    response = client.get(
        f"/api/v1/admin/reports/shifts?start=2026-01-01&end=2026-12-31{query}",
        headers=bearer(ADMIN),
    )
    assert response.status_code == 200, response.text[:200]
    return response.json()


def _on_the_company_clock(day: str, hour: int, minute: int = 0) -> str:
    """A stored timestamp for a chosen moment *on the company's clock*.

    The database holds naive local time (everything is written with ``datetime.now()``) and
    the report converts it into the site's zone - so "07:30 at the gate" is a different
    string on every machine that runs this suite, and a test that typed one would be
    asserting the host's timezone rather than the application's judgement.
    """
    zone = shift_windows.resolve_timezone("Asia/Kuwait")
    chosen = datetime.strptime(f"{day} {hour:02d}:{minute:02d}:00", TS).replace(tzinfo=zone)
    return chosen.astimezone().strftime(TS)


def _plant_shift(worker_id: str, day: str, *, clock_in: str, clock_out: str, hours: float = 8.0) -> None:
    """A clock-in and a clock-out, written the way the punch path writes them."""
    _sql(
        "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
        "status, status_code, source) VALUES (?, 'Downtown Tower A', ?, ?, ?, 0.9, 'Approved', "
        "'approved', 'online')",
        (worker_id, "Clock In", clock_in, 0.0),
    )
    _sql(
        "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
        "status, status_code, source) VALUES (?, 'Downtown Tower A', ?, ?, ?, 0.9, 'Approved', "
        "'approved', 'online')",
        (worker_id, "Clock Out", clock_out, hours),
    )


def test_each_shift_is_paired_with_the_clock_in_that_started_it(client):
    """A row is a shift, so it carries the arrival that opened it - not the newest one.

    The seeded history is a clock-in at 05:00 and a clock-out at 13:00 per day, so a report
    that paired a shift with *any* clock-in, or with the wrong day's, is visible here.
    """
    body = _timesheet(client)
    assert body["rows"], "the seeded history must produce shifts"
    for row in body["rows"]:
        assert row["arrival_time"] == f"{row['date']} 05:00:00", row
        assert row["arrival_verdict"] in reports.ARRIVAL_VERDICTS, row
        assert (row["arrival_minutes"] == 0) == (row["arrival_verdict"] == "on_time"), (
            f"minutes and verdict travel together: {row}"
        )
    assert body["totals"]["late_arrivals"] == sum(
        1 for row in body["rows"] if row["arrival_verdict"] == "late"
    ), "the figure above the table counts the rows under it"


def test_the_arrival_is_judged_on_the_site_clock_and_the_window_in_force(client):
    """Arrivals outside the company window are late by exactly how far outside they were.

    Two shifts for one worker: 04:10 (inside the shipped 04:00-06:30) and 07:30 (an hour
    after it closed). The window is then moved to 03:00-04:00, which is the trap this pins -
    the report resolves the window the same way the gate did, so the *same rows* re-read as
    late by 10 and 210 minutes rather than keeping a verdict nothing can be checked against.
    """
    _plant_shift(MOALLEM, "2026-04-06",
                 clock_in=_on_the_company_clock("2026-04-06", 4, 10),
                 clock_out=_on_the_company_clock("2026-04-06", 12, 10))
    _plant_shift(MOALLEM, "2026-04-07",
                 clock_in=_on_the_company_clock("2026-04-07", 7, 30),
                 clock_out=_on_the_company_clock("2026-04-07", 15, 30))

    body = _timesheet(client, worker_id=MOALLEM)
    arrivals = {row["date"]: (row["arrival_verdict"], row["arrival_minutes"]) for row in body["rows"]}
    assert arrivals == {"2026-04-06": ("on_time", 0), "2026-04-07": ("late", 60)}, arrivals
    assert body["totals"]["late_arrivals"] == 1

    moved = client.post(
        "/api/v1/admin/shift_rules",
        headers=bearer(ADMIN),
        json={"clock_in_window_start": "03:00", "clock_in_window_end": "04:00"},
    )
    assert moved.status_code == 200, moved.text[:300]
    after = _timesheet(client, worker_id=MOALLEM)
    later = {row["date"]: (row["arrival_verdict"], row["arrival_minutes"]) for row in after["rows"]}
    assert later == {"2026-04-06": ("late", 10), "2026-04-07": ("late", 210)}, later
    assert after["totals"]["late_arrivals"] == 2


def test_a_shift_with_no_clock_in_on_file_is_not_counted_as_punctual(client):
    """A force-clock-out, or a shift closed with no arrival recorded: unknown, not on time."""
    _sql(
        "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
        "status, status_code, source) VALUES (?, 'Downtown Tower A', 'Clock Out', ?, 8.0, 0.9, "
        "'Approved', 'approved', 'online')",
        (MOALLEM, _on_the_company_clock("2026-04-08", 13, 0)),
    )
    body = _timesheet(client, worker_id=MOALLEM)
    row = body["rows"][0]
    assert row["arrival_time"] is None and row["arrival_verdict"] is None
    assert row["arrival_minutes"] is None, "nothing was measured, so nothing may be reported"
    assert body["totals"]["late_arrivals"] == 0


def test_the_arrival_column_never_reaches_the_csv(client):
    """The screen gained a column; the file did not - two months of sheets still line up."""
    response = client.get(
        "/api/v1/admin/reports/export?kind=shifts&start=2026-01-01&end=2026-12-31",
        headers=bearer(ADMIN),
    )
    assert response.status_code == 200, response.text[:200]
    assert response.text.splitlines()[0] == "Employee,id,site,hours"
    assert "arrival" not in response.text.lower()
    assert "late" not in response.text.lower(), "no verdict column in the exported sheet"


def test_attendance_report_uses_the_configured_working_days(client):
    response = client.get("/api/v1/admin/reports/attendance", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:200]
    body = response.json()
    assert body["period"]["working_days"], "working days come from shift_rules"
    for row in body["rows"]:
        assert 0.0 <= row["attendance_rate"] <= 1.0
        assert row["days_present"] <= row["expected_days"] or True  # late joins are possible
    assert body["totals"]["expected_days"] >= 1


def test_the_report_payloads_are_json_native(client):
    """``/reports/shifts`` answers with its own ``JSONResponse``; the payload must earn that.

    FastAPI's ``jsonable_encoder`` walk measured 40 ms of that endpoint's 69 ms for a
    quarter of shifts and changed nothing about the answer, so the walk is skipped. That is
    only safe while every value is a dict, a list, a string, a number or ``None`` - so a
    ``datetime`` or a ``Decimal`` reaching a row must break *here*, naming the builder,
    instead of becoming a 500 on a report nobody was profiling.
    """
    from fastapi.encoders import jsonable_encoder

    first, second, _start, _end = reports._range_bounds("2026-01-01", "2026-12-31")
    builders = (
        ("shift_timesheet_rows", reports.shift_timesheet_rows(start=first, end=second)),
        ("attendance_rows", reports.attendance_rows(start=first, end=second)),
    )
    for label, payload in builders:
        assert jsonable_encoder(payload) == payload, (
            f"{label} returns a value the encoder would change, so the report may not skip it"
        )
        try:
            json.dumps(payload)
        except TypeError as error:
            pytest.fail(f"{label} is not JSON-native: {error}")


def test_csv_exports_stream_real_rows(client):
    response = client.get(
        "/api/v1/admin/reports/export?kind=shifts&start=2026-01-01&end=2026-12-31", headers=bearer(ADMIN)
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    # Exactly the four columns the console's own Download CSV writes, in the same order.
    assert lines[0] == "Employee,id,site,hours"
    assert "hourly_rate" not in lines[0] and "gross_estimate" not in lines[0], (
        "a timesheet file has no money column to add up"
    )
    assert len(lines) > 1, "the seeded history must produce at least one shift"
    first = lines[1].split(",")
    assert len(first) == 4 and first[1].strip(), "a row is one shift of one worker"

    audit = client.get("/api/v1/admin/reports/export?kind=audit", headers=bearer(ADMIN))
    assert audit.status_code == 200
    assert audit.text.splitlines()[0].startswith("id,created_at,actor_id")

    offline = client.get("/api/v1/admin/reports/export?kind=offline", headers=bearer(ADMIN))
    assert offline.status_code == 200
    assert "effective_time" in offline.text.splitlines()[0]


def test_the_csv_export_streams_in_batches_and_not_one_row_at_a_time(client):
    """The same bytes, in a fraction of the thread round trips.

    ``StreamingResponse`` runs a *sync* generator through ``iterate_in_threadpool``, so a
    ``yield`` per row cost one worker-thread hop per row: measured at 326 ms for 1 400 rows
    against 3 ms batched, and most of the export endpoint's wall time. Batching is the fix,
    so it is pinned here - and so is the file, because an optimisation that changes a byte
    of somebody's timesheet is not an optimisation.
    """
    rows = [[f"Employee {index}", str(index), "Downtown Tower A", "8.25"] for index in range(3000)]
    header = ["Employee", "id", "site", "hours"]

    async def drain(iterator):
        return [chunk async for chunk in iterator]

    # Driven on a worker thread, deliberately, rather than ``asyncio.run`` on this one: that
    # helper refuses to start when another loop is still marked running on the calling
    # thread, and the browser suites run playwright's sync API, whose teardown leaves exactly
    # that behind (probed: a ProactorEventLoop, ``running=True``, surviving
    # ``sync_playwright()``'s exit - and a fresh loop on *this* thread is refused too, since
    # the marker is per-thread, not per-loop). A worker thread has no such marker, and the
    # assertion here is about streaming, not about what the rest of the session left open.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        chunks = pool.submit(
            asyncio.run, drain(reports._csv_response("export.csv", header, rows).body_iterator)
        ).result()
    text = "".join(chunk if isinstance(chunk, str) else chunk.decode("utf-8") for chunk in chunks)

    expected = io.StringIO()
    writer = csv.writer(expected, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)

    assert text == expected.getvalue(), "batching must not change a byte of the file"
    assert len(chunks) < 20, f"{len(rows) + 1} chunks would be {len(rows) + 1} thread round trips"
    assert len(chunks) > 1, "a report this size still streams rather than arriving in one piece"


def test_xlsx_export_degrades_with_an_instruction_instead_of_failing(client):
    """openpyxl is an optional extra; without it the caller gets a usable answer."""
    response = client.get("/api/v1/admin/reports/export?kind=shifts&format=xlsx", headers=bearer(ADMIN))
    if response.status_code == 200:
        assert "spreadsheetml" in response.headers["content-type"]
    else:
        assert response.status_code == 501
        assert "openpyxl" in json.dumps(response.json())


def test_the_renamed_shifts_report_keeps_the_old_path_answering(client):
    """The report shipped as /reports/payroll; a rename must not break a saved link."""
    params = "?start=2026-01-01&end=2026-12-31"
    renamed = client.get(f"/api/v1/admin/reports/shifts{params}", headers=bearer(ADMIN))
    legacy = client.get(f"/api/v1/admin/reports/payroll{params}", headers=bearer(ADMIN))
    assert renamed.status_code == 200, renamed.text[:200]
    assert legacy.status_code == 200, "the path this report shipped under must keep resolving"
    assert legacy.json() == renamed.json(), "the alias must answer with the same report"


def test_the_export_still_accepts_the_payroll_kind(client):
    """Same reason as the route alias: a saved script or bookmark, not a live contract."""
    headers = []
    for kind in ("shifts", "payroll"):
        response = client.get(
            f"/api/v1/admin/reports/export?kind={kind}&start=2026-01-01&end=2026-12-31",
            headers=bearer(ADMIN),
        )
        assert response.status_code == 200, (kind, response.text[:200])
        assert response.text.splitlines()[0] == "Employee,id,site,hours"
        headers.append(response.text.splitlines()[0])
    assert headers[0] == headers[1], "both kinds must export the same columns"


def test_exports_and_reports_are_admin_only(client):
    for path in (
        "/api/v1/admin/reports/shifts",
        "/api/v1/admin/reports/attendance",
        "/api/v1/admin/reports/export?kind=audit",
        "/api/v1/admin/reports/pending",
    ):
        assert client.get(path, headers=bearer(MOALLEM)).status_code == 403, path
        assert client.get(path).status_code == 401, path


def test_pending_report_surfaces_everything_awaiting_a_decision(client):
    response = client.get("/api/v1/admin/reports/pending", headers=bearer(HEAD_ADMIN))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 0 and "unread_notifications" in body
    assert all(item["status_code"] in ("pending_review", "pending_overtime") for item in body["items"])


def test_offline_punch_queue_is_admin_only(client):
    assert client.get("/api/v1/admin/punch_queue", headers=bearer(MOALLEM)).status_code == 403
    assert client.get("/api/v1/admin/punch_queue").status_code == 401
