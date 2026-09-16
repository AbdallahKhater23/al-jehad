"""The client half of the offline contract, run where it actually runs.

WHY THIS TEST EXISTS
--------------------
``offline_sync.py`` says it plainly: "``canonical_punch()`` is the reference
implementation; a client port must reproduce it byte for byte." A comment cannot
hold that contract - a single extra space, a coordinate formatted to five
decimals instead of six, or a Python ``None`` that reaches JavaScript as the
string ``"null"`` produces a signature the server rejects forever.

So this suite executes ``frontend/offline_queue.js`` under Node (the same file the
browser loads) and puts its output through the real endpoint. Nothing here is a
re-implementation of the client logic: the numbers compared against the server's
come out of the browser module itself.

What is covered:
* the canonical field string, for every field shape the attendance flow can
  produce (including missing GPS/photo, which must be an empty string);
* the HMAC signature, compared against ``offline_sync.sign_punch``;
* wall-clock arithmetic on "YYYY-MM-DD HH:MM:SS" - the client must add the
  monotonic offset exactly as Python's ``datetime`` + ``timedelta`` does;
* the SHA-256 photo hash that binds an offline punch to the image on the phone;
* end to end: a punch signed by the browser module is accepted by
  ``POST /api/v1/attendance/sync`` and lands in ``attendance_logs``.

Node is optional; without it the suite skips rather than fails, because a missing
JavaScript runtime says nothing about whether the attendance system works.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from harness import DB_PATH, MOALLEM, PROJECT_ROOT, bearer

import offline_sync

NODE = shutil.which("node")
CLIENT_JS = PROJECT_ROOT / "frontend" / "offline_queue.js"

DEVICE = "node-client-device"
TS = "%Y-%m-%d %H:%M:%S"
SITE_LAT, SITE_LON = 30.05, 31.23          # inside the seeded "Downtown Tower A" geofence

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The harness: load the very file the browser loads, ask it for the signed values,
# print them as JSON. `require` is why offline_queue.js exports anything at all.
NODE_HARNESS = """
const { OFFLINE_CRYPTO } = require(process.argv[2]);
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
    const out = {};
    if (payload.action_mode === 'time') {
        const base = OFFLINE_CRYPTO.parseTs(payload.base);
        out.effective_timestamp = OFFLINE_CRYPTO.formatTs(base + payload.offset_s * 1000);
        out.client_timestamp = OFFLINE_CRYPTO.formatTs(base + payload.wall_elapsed_s * 1000);
        out.client_offset_s = Math.round(payload.wall_elapsed_s - payload.offset_s);
    } else {
        out.canonical = OFFLINE_CRYPTO.canonicalPunch(payload.fields);
        out.signature = await OFFLINE_CRYPTO.signPunch(payload.device_key, payload.fields);
        out.canonical_fields = OFFLINE_CRYPTO.canonicalPunch(payload.fields);
        if (payload.blob_hex) {
            const bytes = Uint8Array.from(Buffer.from(payload.blob_hex, 'hex'));
            out.photo_sha256 = await OFFLINE_CRYPTO.sha256Hex(bytes);
        }
    }
    process.stdout.write(JSON.stringify(out));
})().catch((err) => {
    console.error((err && err.stack) || String(err));
    process.exit(1);
});
"""


@pytest.fixture(scope="module")
def node_harness(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("offline_client") / "harness.js"
    path.write_text(NODE_HARNESS, encoding="utf-8")
    return path


def run_client(node_harness: Path, payload: dict) -> dict:
    """Execute the client module under Node and return its JSON answer."""
    completed = subprocess.run(
        [NODE, str(node_harness), str(CLIENT_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, f"client harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def _sql(sql: str, params=()) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _scalar(sql: str, params=()):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the canonical string and the signature
# ---------------------------------------------------------------------------
FIELDS = {
    "device_id": DEVICE,
    "worker_id": MOALLEM,
    "action": "Clock In",
    "client_punch_id": "punch-0001",
    "nonce": "b8b1f0f1c0d2e3a4",
    "anchor_id": "anchor-0001",
    "effective_timestamp": "2026-09-13 05:12:30",
    "lat": SITE_LAT,
    "lon": SITE_LON,
    "accuracy": 8.0,
    "photo_sha256": "a" * 64,
}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="all-fields"),
        pytest.param({"lat": None, "lon": None}, id="no-gps"),
        pytest.param({"accuracy": None}, id="no-accuracy"),
        pytest.param({"photo_sha256": None}, id="no-photo-hash"),
        pytest.param({"lat": None, "lon": None, "accuracy": None, "photo_sha256": None}, id="bare-punch"),
        pytest.param({"lat": 30.05, "lon": 31.0, "accuracy": 8.25}, id="exact-formatting"),
        pytest.param({"lat": -1.0, "lon": -0.000001, "accuracy": 100.0}, id="negative-and-small"),
        pytest.param({"action": "Clock Out", "client_punch_id": str(uuid.uuid4())}, id="clock-out"),
    ],
)
def test_canonical_string_and_signature_match_the_server(node_harness, overrides):
    """The server's reference implementation and the browser module must agree.

    Compared as raw strings, not as a hash of them: when they differ, the diff is
    the bug report.
    """
    fields = {**FIELDS, **overrides}
    key = offline_sync.device_key_token(MOALLEM, DEVICE, 1, "salt-for-the-test")

    answer = run_client(node_harness, {"device_key": key, "fields": fields})

    assert answer["canonical"] == offline_sync.canonical_punch(**fields)
    assert answer["signature"] == offline_sync.sign_punch(key, **fields)


def test_the_photo_hash_matches_python(node_harness):
    """photo_sha256 binds the punch to the selfie kept on the phone."""
    blob = bytes(range(256)) * 4
    answer = run_client(
        node_harness,
        {"device_key": "unused", "fields": FIELDS, "blob_hex": blob.hex()},
    )
    assert answer["photo_sha256"] == hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# time arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "base, offset_s",
    [
        ("2026-09-13 05:00:00", 0.0),
        ("2026-09-13 05:00:00", 3599.5),
        ("2026-09-13 05:00:00", 8.1 * 3600),
        ("2026-09-13 23:30:00", 5400.0),          # crosses midnight
        ("2026-02-28 23:59:59", 2.0),             # crosses a month
        ("2026-04-24 00:30:00", 3600.0),          # a DST transition in most zones
        ("2026-12-31 23:00:00", 7200.0),          # crosses a year
    ],
)
def test_the_client_adds_offsets_the_way_the_server_does(node_harness, base, offset_s):
    """effective_time = anchor + monotonic offset, computed identically on both sides.

    The client does this arithmetic in UTC on purpose: parsing the naive string as
    local time would shift a punch by an hour across a DST boundary, and the punch
    would land at a time the server never verified.
    """
    answer = run_client(
        node_harness,
        {"action_mode": "time", "base": base, "offset_s": offset_s, "wall_elapsed_s": offset_s},
    )
    expected = datetime.strptime(base, TS) + timedelta(seconds=offset_s)
    assert answer["effective_timestamp"] == expected.strftime(TS)
    assert answer["client_offset_s"] == 0, "an honest phone reports no drift from the anchor"


def test_a_moved_phone_clock_shows_up_as_drift(node_harness):
    """The wall clock is a tamper signal, not a time source.

    Two hours of wall-clock drift with no monotonic elapsed time is a phone whose
    clock was just moved; the client reports it and the server refuses it
    (``clock_tampered``) instead of relocating the punch.
    """
    answer = run_client(
        node_harness,
        {"action_mode": "time", "base": "2026-09-13 05:00:00", "offset_s": 60.0, "wall_elapsed_s": -7140.0},
    )
    assert answer["client_offset_s"] == -7200


# ---------------------------------------------------------------------------
# end to end: a browser-signed punch through the real endpoint
# ---------------------------------------------------------------------------
def test_the_endpoint_accepts_a_punch_signed_by_the_browser_module(client, node_harness):
    """The whole point: the server stores what the phone signed, and pays for it.

    The signature comes from the browser module, the device key comes from the real
    registration endpoint, and the payload goes to the real sync endpoint - so a
    client/server disagreement about any single byte fails right here.
    """
    registration = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(MOALLEM),
        json={"device_id": DEVICE, "note": "signed by frontend/offline_queue.js"},
    )
    assert registration.status_code == 200, registration.text[:300]
    device_key = registration.json()["device_key"]

    # Plant the anchor the phone would have been holding: an hour ago.
    anchor_ms = (datetime.now() - timedelta(hours=1)).strftime(TS)
    anchor_id = uuid.uuid4().hex
    _sql(
        "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) "
        "VALUES (?,?,?,?,?)",
        (anchor_id, DEVICE, MOALLEM, anchor_ms, anchor_ms),
    )
    anchor_signature = offline_sync.sign_anchor(
        device_id=DEVICE, worker_id=MOALLEM, server_time=anchor_ms, anchor_id=anchor_id
    )

    punch_id = str(uuid.uuid4())
    fields = {
        **FIELDS,
        "client_punch_id": punch_id,
        "nonce": uuid.uuid4().hex,
        "anchor_id": anchor_id,
    }
    offset_s = 60.0
    # The client derives this from its monotonic clock; the harness computes it with
    # the same code path queuePunch() uses.
    clock = run_client(
        node_harness,
        {"action_mode": "time", "base": anchor_ms, "offset_s": offset_s, "wall_elapsed_s": offset_s},
    )
    fields["effective_timestamp"] = clock["effective_timestamp"]

    signed = run_client(node_harness, {"device_key": device_key, "fields": fields})

    payload = {
        "client_punch_id": punch_id,
        "action": fields["action"],
        "anchor_id": anchor_id,
        "anchor_server_time": anchor_ms,
        "anchor_signature": anchor_signature,
        "monotonic_offset_s": offset_s,
        "nonce": fields["nonce"],
        "lat": fields["lat"],
        "lon": fields["lon"],
        "accuracy": fields["accuracy"],
        "client_timestamp": clock["client_timestamp"],
        "client_offset_s": clock["client_offset_s"],
        "photo_sha256": fields["photo_sha256"],
        "signature": signed["signature"],
        "signature_version": offline_sync.SIGNATURE_VERSION,
    }

    response = client.post(
        "/api/v1/attendance/sync",
        headers=bearer(MOALLEM),
        json={"device_id": DEVICE, "punches": [payload]},
    )
    assert response.status_code == 200, response.text[:400]
    assert [item["status"] for item in response.json()["results"]] == ["accepted"]
    assert response.json()["next_anchor"]["anchor_id"], "a fresh anchor must come back"

    # The server verified and stored exactly the signature the browser produced.
    assert _scalar(
        "SELECT signature FROM punch_queue WHERE client_punch_id = ?", (punch_id,)
    ) == signed["signature"]
    assert _scalar(
        "SELECT status FROM punch_queue WHERE client_punch_id = ?", (punch_id,)
    ) == "accepted"
    assert _scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE source = 'offline' AND worker_id = ?", (MOALLEM,)
    ) == 1
    assert _scalar(
        "SELECT liveness_class FROM attendance_logs WHERE source = 'offline' AND worker_id = ?",
        (MOALLEM,),
    ) == "unverified_offline", "an offline capture cannot claim to have passed liveness"
