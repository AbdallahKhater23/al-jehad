"""The Android client's punch action resolution, held to the server's own rules.

WHY THIS TEST EXISTS
--------------------
``mobile-client/src/ui/punch.ts`` decides which of three actions a single tap means:

    no shift                      -> "Clock In"
    an unconfirmed travel shift   -> "Transit Checkpoint"
    a confirmed shift             -> "Clock Out"

Getting that wrong is not a cosmetic bug. Sending "Clock Out" for an unconfirmed travel
shift is refused with ``off_site_checkout_needs_admin``; sending "Clock In" while already on
shift is refused with ``already_clocked_in``; and the arrival is the one action answered
**without a photograph**, so a client that sends a selfie for it wastes a liveness pass and
a client that sends none for a real punch gets a 422.

The rule is small enough to re-derive by hand and wrong enough to matter, so it is checked
here two ways: the TypeScript rule itself is bundled with esbuild (the same tool Vite uses)
and run under Node, and the refusals the app branches on are produced by the *real*
endpoints rather than restated.

Node and esbuild are optional; without them the suite skips rather than fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from harness import (
    ADMIN,
    MOALLEM,
    PROJECT_ROOT,
    bearer,
    clock_in,
    db_scalar,
    jpeg_bytes,
)

import offline_sync

NODE = shutil.which("node")
MOBILE_DIR = PROJECT_ROOT / "mobile-client"
ESBUILD_JS = MOBILE_DIR / "node_modules" / "esbuild" / "bin" / "esbuild"
PUNCH_TS = MOBILE_DIR / "src" / "ui" / "punch.ts"

TS = "%Y-%m-%d %H:%M:%S"
SITE_LAT, SITE_LON = 30.05, 31.23          # inside the seeded "Downtown Tower A" geofence
AWAY_LAT, AWAY_LON = 31.99, 35.99          # outside every seeded fence

pytestmark = pytest.mark.skipif(
    NODE is None or not ESBUILD_JS.exists() or not PUNCH_TS.exists(),
    reason="node + mobile-client/node_modules are required to bundle the TypeScript module",
)

#: Import the real resolution rule and answer with it. Nothing here re-implements the rule.
HARNESS_TS = """
import { resolveAction, actionNeedsSelfie } from './punch.ts';

const chunks: Buffer[] = [];
process.stdin.on('data', (c) => chunks.push(c as Buffer));
process.stdin.on('end', () => {
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const action = resolveAction(payload.shift);
  process.stdout.write(JSON.stringify({
    action,
    needs_selfie: actionNeedsSelfie(action),
  }));
});
"""


@pytest.fixture(scope="module")
def node_harness(tmp_path_factory) -> Path:
    """Bundle the real module out of its own tree, so its relative imports resolve."""
    bundle = tmp_path_factory.mktemp("mobile_punch") / "harness.mjs"
    harness = MOBILE_DIR / "src" / "ui" / "__punch_harness.ts"
    harness.write_text(HARNESS_TS, encoding="utf-8")
    try:
        completed = subprocess.run(
            [NODE, str(ESBUILD_JS), str(harness), "--bundle", "--platform=node",
             "--format=esm", f"--outfile={bundle}", "--log-level=warning"],
            capture_output=True, text=True, timeout=180,
        )
    finally:
        harness.unlink(missing_ok=True)
    assert completed.returncode == 0, f"esbuild failed:\n{completed.stderr}"
    return bundle


def resolve(node_harness: Path, shift) -> dict:
    completed = subprocess.run(
        [NODE, str(node_harness)], input=json.dumps({"shift": shift}),
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, f"harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def _grant_transit(client, user_id: str = MOALLEM) -> None:
    """Hand the off-geofence privilege to one account, the way an administrator does it.

    Through the real endpoint rather than a direct ``UPDATE``: the privilege is a human
    decision, and a test that wrote the column itself would be testing the column.
    """
    name = db_scalar("SELECT name FROM users WHERE id = ?", (user_id,))
    response = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(ADMIN),
        json={"user_id": user_id, "name": name, "email": "", "phone": "", "transit_enabled": True},
    )
    assert response.status_code == 200, response.text[:300]


def _revoke_transit(client, user_id: str = MOALLEM) -> None:
    name = db_scalar("SELECT name FROM users WHERE id = ?", (user_id,))
    client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(ADMIN),
        json={"user_id": user_id, "name": name, "email": "", "phone": "", "transit_enabled": False},
    )


def _clear_session() -> None:
    from harness import current_db_path
    import sqlite3

    conn = sqlite3.connect(str(current_db_path()))
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
        conn.commit()
    finally:
        conn.close()


def _open_travel_shift(client, *, hours: float = 3.0) -> None:
    """Open a genuine travel shift the way the app does: a departure from outside every fence."""
    _clear_session()
    _grant_transit(client)
    response = clock_in(
        client, MOALLEM, headers=bearer(MOALLEM), coordinates=f"{AWAY_LAT},{AWAY_LON}"
    )
    assert response.status_code == 200, response.text[:300]
    assert db_scalar(
        "SELECT is_transit FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    ) == 1, "a departure from outside every fence opens a travel shift"

    # Backdate so the shift has real travel time to credit.
    from harness import current_db_path
    import sqlite3

    conn = sqlite3.connect(str(current_db_path()))
    try:
        stamp = (datetime.now() - timedelta(hours=hours)).strftime(TS)
        conn.execute(
            "UPDATE active_sessions SET clock_in_time = ?, transit_start_time = ? WHERE worker_id = ?",
            (stamp, stamp, MOALLEM),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "shift, expected",
    [
        pytest.param(None, "Clock In", id="no-shift-opens-one"),
        pytest.param({"active": False, "inTransit": False}, "Clock In", id="inactive-opens-one"),
        pytest.param({"active": True, "inTransit": True}, "Transit Checkpoint", id="travel-arrival"),
        pytest.param({"active": True, "inTransit": False}, "Clock Out", id="confirmed-shift-closes"),
    ],
)
def test_the_action_a_tap_means(node_harness, shift, expected):
    assert resolve(node_harness, shift)["action"] == expected


def test_only_the_arrival_is_answered_without_a_photograph(node_harness):
    """The arrival is a fact about a place, not a second identity check.

    Every other action requires a frame — the server answers 422 without one — so this
    predicate is what decides whether the capture overlay opens at all.
    """
    assert resolve(node_harness, {"active": True, "inTransit": True})["needs_selfie"] is False
    assert resolve(node_harness, {"active": True, "inTransit": False})["needs_selfie"] is True
    assert resolve(node_harness, None)["needs_selfie"] is True


# ---------------------------------------------------------------------------
# the rule, held to the endpoints
# ---------------------------------------------------------------------------
def test_the_server_agrees_the_arrival_needs_no_selfie(client):
    """``Transit Checkpoint`` away from every fence is refused for *place*, not for a frame.

    If the server demanded a photograph for the arrival, the client's ``needs_selfie: false``
    would make every arrival a 422. This is the two halves of the same rule meeting.
    """
    _open_travel_shift(client)
    response = client.post(
        "/api/v1/attendance/verify",
        headers=bearer(MOALLEM),
        data={
            "worker_id": MOALLEM,
            "action": "Transit Checkpoint",
            "location_input": f"{AWAY_LAT},{AWAY_LON}",
        },
        # No file at all.
    )
    assert response.status_code == 422, response.text[:400]
    assert response.json()["detail"]["error_code"] == "arrival_outside_geofence"
    _revoke_transit(client)
    _clear_session()


def test_the_arrival_is_confirmed_at_a_fence_with_no_selfie_at_all(client):
    """The positive half of the transit branch, and the one the whole rule exists for.

    A travel shift that reaches a real fence is confirmed with **no photograph**: the worker's
    own app sends ``Transit Checkpoint`` and no file, and the response is an arrival with the
    credited travel time. If this needed a frame, every arrival would be a 422 and the client's
    ``needs_selfie: false`` would be a bug rather than a rule.
    """
    _open_travel_shift(client, hours=3.0)

    response = client.post(
        "/api/v1/attendance/verify",
        headers=bearer(MOALLEM),
        data={
            "worker_id": MOALLEM,
            "action": "Transit Checkpoint",
            "location_input": f"{SITE_LAT},{SITE_LON}",
        },
        # Deliberately no file: this is the action answered without biometrics.
    )
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["status"] == "arrived"
    assert body["site"], "the arrival names the site it was confirmed at"
    assert body["transit_hours"] >= 3.0, "the travel time is credited to the shift"
    assert body["liveness"] is None, "no face was taken, so there is no verdict to report"

    # The shift is now a *site* shift: the next tap must be a Clock Out, not another arrival.
    assert db_scalar("SELECT is_transit FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    _revoke_transit(client)
    _clear_session()


def test_a_real_punch_without_a_frame_is_refused_by_name(client):
    """The mirror of the rule: no selfie where one is required is a 422 that says so."""
    _clear_session()
    response = client.post(
        "/api/v1/attendance/verify",
        headers=bearer(MOALLEM),
        data={"worker_id": MOALLEM, "action": "Clock In", "location_input": f"{SITE_LAT},{SITE_LON}"},
    )
    assert response.status_code == 422
    assert "selfie is required" in response.text.lower()


def test_a_clock_out_from_an_unconfirmed_travel_shift_carries_the_code_the_app_keys_on(client):
    """The branch the app must not send ``Clock Out`` for.

    The refusal carries ``off_site_checkout_needs_admin``, ``open_hours`` and ``can_request``,
    which is what lets the app offer "ask an administrator" instead of a dead end — and what
    its ``needs_admin`` outcome is matched on.

    The status is asserted exactly, and it is **409**: the client first gated this branch on
    403 and was wrong, which sent the worker a plain error toast instead of the offer. A test
    that accepted either status would not have caught it.
    """
    _open_travel_shift(client, hours=3.0)
    response = client.post(
        "/api/v1/attendance/verify",
        headers=bearer(MOALLEM),
        data={
            "worker_id": MOALLEM,
            "action": "Clock Out",
            "location_input": f"{AWAY_LAT},{AWAY_LON}",
        },
        files={"selfie": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 409, (
        f"the client matches this branch on 409; the server answered {response.status_code}"
    )
    detail = response.json()["detail"]
    assert detail["error_code"] == "off_site_checkout_needs_admin"
    assert detail["can_request"] is True
    assert detail["open_hours"] > 0, "the shift is still open and still counting"
    _revoke_transit(client)
    _clear_session()


def test_an_early_clock_out_returns_the_figures_the_app_renders(client):
    """The early-checkout question, and the numbers the app builds its sentence from.

    The API's ``message`` is English; the app renders the same question from
    ``paid_hours``/``regular_hours``/``elapsed_hours``. Losing those fields would leave the
    app showing the server's English to a worker who may not read it.
    """
    _clear_session()
    started = (datetime.now() - timedelta(hours=1)).strftime(TS)
    from harness import current_db_path
    import sqlite3

    conn = sqlite3.connect(str(current_db_path()))
    try:
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source, "
            "is_transit, liveness_class) VALUES (?,?,?,?,0,?)",
            (MOALLEM, "Downtown Tower A", started, "web", "verified"),
        )
        conn.commit()
    finally:
        conn.close()

    response = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert response.status_code == 409, response.text[:400]
    detail = response.json()["detail"]
    assert detail["error_code"] == "confirm_early_checkout"
    for field in ("paid_hours", "regular_hours", "elapsed_hours", "break_hours"):
        assert field in detail, f"the app renders the question from {field}"
    assert detail["regular_hours"] > detail["paid_hours"], "an hour in is a short shift"
    _clear_session()


def test_the_same_clock_out_is_recorded_once_the_worker_confirms(client):
    """The confirmation is the *same* punch restated, not a second one.

    This is the property that makes the app's re-post safe: one shift, one row, and the hours
    that were spelled out in the question.
    """
    _clear_session()
    started = (datetime.now() - timedelta(hours=1)).strftime(TS)
    from harness import current_db_path
    import sqlite3

    conn = sqlite3.connect(str(current_db_path()))
    try:
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source, "
            "is_transit, liveness_class) VALUES (?,?,?,?,0,?)",
            (MOALLEM, "Downtown Tower A", started, "web", "verified"),
        )
        conn.commit()
    finally:
        conn.close()

    refused = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert refused.status_code == 409

    confirmed = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM), confirmed=True)
    assert confirmed.status_code == 200, confirmed.text[:400]

    outs = db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'",
        (MOALLEM,),
    )
    assert outs == 1, "one shift, one clock-out row"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    _clear_session()


def test_the_offline_contract_carries_exactly_the_two_punch_actions(client):
    """There is no signature format for an arrival, so the client must refuse to queue one.

    ``offline_sync.verify_punch`` accepts exactly ``Clock In`` and ``Clock Out``; a queued
    arrival would be stored and then refused as ``invalid_action``. The app refuses it at
    capture time instead, which is the only point at which the worker can be told why.
    """
    assert offline_sync.ACTION_CLOCK_IN == "Clock In"
    assert offline_sync.ACTION_CLOCK_OUT == "Clock Out"
    assert not hasattr(offline_sync, "ACTION_TRANSIT_CHECKPOINT"), (
        "the offline contract has no arrival action; the client's refusal depends on this"
    )


def test_a_queued_punch_survives_a_sync_round_trip(client):
    """The offline branch end to end: signed on the phone, accepted by the server.

    This is the path a worker in a dead spot takes, and the one that has to work when nothing
    else does — so the payload the queue builds goes through the real endpoint.
    """
    _clear_session()
    device_id = f"android-punch-{uuid.uuid4().hex[:8]}"
    registration = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(MOALLEM),
        json={"device_id": device_id, "note": "punch flow test"},
    )
    assert registration.status_code == 200, registration.text[:300]
    device_key = registration.json()["device_key"]

    anchor_ms = (datetime.now() - timedelta(minutes=30)).strftime(TS)
    anchor_id = uuid.uuid4().hex
    from harness import current_db_path
    import sqlite3

    conn = sqlite3.connect(str(current_db_path()))
    try:
        conn.execute(
            "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) "
            "VALUES (?,?,?,?,?)",
            (anchor_id, device_id, MOALLEM, anchor_ms, anchor_ms),
        )
        conn.commit()
    finally:
        conn.close()

    anchor_signature = offline_sync.sign_anchor(
        device_id=device_id, worker_id=MOALLEM, server_time=anchor_ms, anchor_id=anchor_id
    )

    offset_s = 60.0
    effective = (datetime.strptime(anchor_ms, TS) + timedelta(seconds=offset_s)).strftime(TS)
    punch_id = str(uuid.uuid4())
    fields = {
        "device_id": device_id,
        "worker_id": MOALLEM,
        "action": "Clock In",
        "client_punch_id": punch_id,
        "nonce": uuid.uuid4().hex,
        "anchor_id": anchor_id,
        "effective_timestamp": effective,
        "lat": SITE_LAT,
        "lon": SITE_LON,
        "accuracy": 8.0,
        "photo_sha256": None,
    }
    signature = offline_sync.sign_punch(device_key, **fields)

    response = client.post(
        "/api/v1/attendance/sync",
        headers=bearer(MOALLEM),
        json={
            "device_id": device_id,
            "punches": [
                {
                    "client_punch_id": punch_id,
                    "action": "Clock In",
                    "anchor_id": anchor_id,
                    "anchor_server_time": anchor_ms,
                    "anchor_signature": anchor_signature,
                    "monotonic_offset_s": offset_s,
                    "nonce": fields["nonce"],
                    "lat": SITE_LAT,
                    "lon": SITE_LON,
                    "accuracy": 8.0,
                    "client_timestamp": effective,
                    "client_offset_s": 0,
                    "photo_sha256": None,
                    "signature": signature,
                    "signature_version": offline_sync.SIGNATURE_VERSION,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text[:400]
    assert [item["status"] for item in response.json()["results"]] == ["accepted"]
    assert response.json()["next_anchor"]["anchor_id"], "a fresh anchor comes back for the queue"
    _clear_session()
