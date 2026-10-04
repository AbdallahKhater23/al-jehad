"""The Android client's signing module, run against the real endpoint.

WHY THIS TEST EXISTS
--------------------
``mobile-client/src/offline/signing.ts`` is a second implementation of the offline
contract, and ``offline_sync.py`` states the rule it has to satisfy: ``canonical_punch()``
is the reference and a client port must reproduce it **byte for byte**. A single byte of
disagreement - a coordinate at five decimals instead of six, a Python ``None`` that reaches
JavaScript as the string ``"null"``, a tie rounded half-up where Python rounds half-even -
produces a signature the server rejects forever, on a punch taken where nobody could look.

``test_offline_client_signing.py`` already holds ``frontend/offline_queue.js`` to that
contract. This suite holds the *Android* client to it, which matters because the two are
independent codebases that will drift: the phone is the client that captures punches in
dead spots, and it is the one that cannot be fixed with a page reload.

How it runs: ``esbuild`` bundles the TypeScript module into a Node harness (the same
esbuild Vite uses), the harness signs a punch, and the payload goes through
``POST /api/v1/attendance/sync`` with a device key from the real registration endpoint.

Rounding is covered hardest, because it is where the two languages genuinely differ. The
module computes ``round_half_even`` on the double's exact IEEE-754 value rather than
multiplying by a power of ten first: ``0.05 * 10`` is exactly ``0.5`` in binary floating
point, so a multiply-then-round implementation sees a tie and answers ``"0.0"`` where
Python answers ``"0.1"``. Accuracy readings like ``8.25`` are ordinary GPS output, so that
is a live bug rather than a contrived one.

Node and esbuild are optional; without them the suite skips rather than fails, because a
missing JavaScript toolchain says nothing about whether the attendance system works.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from harness import DB_PATH, MOALLEM, PROJECT_ROOT, bearer

import offline_sync

NODE = shutil.which("node")
MOBILE_DIR = PROJECT_ROOT / "mobile-client"
ESBUILD_JS = MOBILE_DIR / "node_modules" / "esbuild" / "bin" / "esbuild"
SIGNING_TS = MOBILE_DIR / "src" / "offline" / "signing.ts"

DEVICE = "android-client-device"
TS = "%Y-%m-%d %H:%M:%S"
SITE_LAT, SITE_LON = 30.05, 31.23          # inside the seeded "Downtown Tower A" geofence

pytestmark = pytest.mark.skipif(
    NODE is None or not ESBUILD_JS.exists() or not SIGNING_TS.exists(),
    reason="node + mobile-client/node_modules are required to bundle the TypeScript module",
)

#: The harness: import the real module, answer one JSON request, print one JSON reply.
HARNESS_TS = """
import { canonicalPunch, signPunch, sha256Hex, parseTs, formatTs, fmtCoord, fmtAccuracy }
  from './signing.ts';

const chunks: Buffer[] = [];
process.stdin.on('data', (c) => chunks.push(c as Buffer));
process.stdin.on('end', () => {
  void (async () => {
    const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const out: Record<string, unknown> = {};
    if (payload.mode === 'format') {
      out.formatted = payload.values.map((v: number) => ({ coord: fmtCoord(v), accuracy: fmtAccuracy(v) }));
    } else if (payload.mode === 'time') {
      const base = parseTs(payload.base);
      out.effective_timestamp = formatTs(base + payload.offset_s * 1000);
      out.client_timestamp = formatTs(base + payload.wall_elapsed_s * 1000);
      out.client_offset_s = Math.round(payload.wall_elapsed_s - payload.offset_s);
    } else {
      out.canonical = canonicalPunch(payload.fields);
      out.signature = await signPunch(payload.device_key, payload.fields);
      if (payload.blob_hex) {
        const bytes = Uint8Array.from(Buffer.from(payload.blob_hex, 'hex'));
        out.photo_sha256 = await sha256Hex(bytes);
      }
    }
    process.stdout.write(JSON.stringify(out));
  })().catch((err) => { console.error((err && err.stack) || String(err)); process.exit(1); });
});
"""


@pytest.fixture(scope="module")
def node_harness(tmp_path_factory) -> Path:
    """Bundle the TypeScript signing module once for the whole module."""
    workdir = tmp_path_factory.mktemp("mobile_signing")
    harness = workdir / "harness.ts"
    harness.write_text(HARNESS_TS, encoding="utf-8")
    (workdir / "signing.ts").write_text(SIGNING_TS.read_text(encoding="utf-8"), encoding="utf-8")
    bundle = workdir / "harness.mjs"
    completed = subprocess.run(
        [NODE, str(ESBUILD_JS), str(harness), "--bundle", "--platform=node", "--format=esm",
         f"--outfile={bundle}", "--log-level=warning"],
        capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, f"esbuild failed:\n{completed.stderr}"
    return bundle


def run_client(node_harness: Path, payload: dict) -> dict:
    completed = subprocess.run(
        [NODE, str(node_harness)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=120,
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


# ---------------------------------------------------------------------------
# the canonical string and the signature
# ---------------------------------------------------------------------------
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
        # The cases a multiply-then-round implementation gets wrong: the product lands on an
        # exact midpoint even though the double never was one.
        pytest.param({"lat": 1.0000005, "lon": -2.0000005}, id="spurious-tie-at-6dp"),
        pytest.param({"accuracy": 0.05}, id="accuracy-half-up-not-even"),
        pytest.param({"lat": 1.0000015, "lon": 2.0000015, "accuracy": 0.15}, id="near-tie-magnitudes"),
        pytest.param({"lat": 89.9999999, "lon": -179.9999999, "accuracy": 1234.567}, id="extreme-magnitudes"),
    ],
)
def test_canonical_string_and_signature_match_the_server(node_harness, overrides):
    """Compared as raw strings, not as a hash of them: when they differ, the diff is the bug report."""
    fields = {**FIELDS, **overrides}
    key = offline_sync.device_key_token(MOALLEM, DEVICE, 1, "salt-for-the-test")

    answer = run_client(node_harness, {"device_key": key, "fields": fields})

    assert answer["canonical"] == offline_sync.canonical_punch(**fields)
    assert answer["signature"] == offline_sync.sign_punch(key, **fields)


def test_the_photo_hash_matches_python(node_harness):
    """photo_sha256 binds the punch to the selfie kept on the phone."""
    blob = bytes(range(256)) * 4
    answer = run_client(node_harness, {"device_key": "unused", "fields": FIELDS, "blob_hex": blob.hex()})
    assert answer["photo_sha256"] == hashlib.sha256(blob).hexdigest()


def test_rounding_matches_python_across_a_random_sample(node_harness):
    """Rounding is the one place the two languages genuinely differ, so it is sampled hard.

    Values are shaped like readings (coordinates, accuracies) and deliberately placed near
    decimal midpoints. A mismatch here is a punch the phone can sign and the server can
    never verify.
    """
    import random

    rng = random.Random(20261003)
    values: list[float] = []
    for _ in range(1500):
        kind = rng.random()
        if kind < 0.5:
            values.append(rng.uniform(-90.0, 90.0))
        elif kind < 0.7:
            values.append(rng.uniform(0.0, 100.0))
        elif kind < 0.85:
            decimals = rng.choice([6, 1])
            half = 10 ** (-decimals) / 2
            base = rng.randint(-2000, 2000) / 10 ** rng.choice([0, 1, 2, 3])
            values.append(base + rng.choice([half, -half]) * rng.choice([1.0, 1.0, 0.5, 2.0, 0.1]))
        else:
            values.append(rng.choice([0.0, -0.0, 1e-9, -1e-9, 0.05, 8.25, 0.15, 1.0000005, -2.0000005]))

    answer = run_client(node_harness, {"mode": "format", "values": values})

    mismatches = [
        (value, got, offline_sync._fmt_coord(value), offline_sync._fmt_accuracy(value))
        for value, got in zip(values, answer["formatted"])
        if got["coord"] != offline_sync._fmt_coord(value)
        or got["accuracy"] != offline_sync._fmt_accuracy(value)
    ]
    assert not mismatches, f"{len(mismatches)} rounding mismatch(es), first few: {mismatches[:5]}"


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

    The client does this arithmetic in UTC on purpose: parsing the naive string as local
    time would shift a punch by an hour across a DST boundary, and the punch would land at
    a time the server never verified.
    """
    answer = run_client(
        node_harness,
        {"mode": "time", "base": base, "offset_s": offset_s, "wall_elapsed_s": offset_s},
    )
    expected = datetime.strptime(base, TS) + timedelta(seconds=offset_s)
    assert answer["effective_timestamp"] == expected.strftime(TS)
    assert answer["client_offset_s"] == 0, "an honest phone reports no drift from the anchor"


def test_a_moved_phone_clock_shows_up_as_drift(node_harness):
    """The wall clock is a tamper signal, not a time source."""
    answer = run_client(
        node_harness,
        {"mode": "time", "base": "2026-09-13 05:00:00", "offset_s": 60.0, "wall_elapsed_s": -7140.0},
    )
    assert answer["client_offset_s"] == -7200


# ---------------------------------------------------------------------------
# end to end: an Android-signed punch through the real endpoint
# ---------------------------------------------------------------------------
def test_the_endpoint_accepts_a_punch_signed_by_the_android_module(client, node_harness):
    """The whole point: the server stores what the phone signed, and pays for it.

    The signature comes from the TypeScript module, the device key comes from the real
    registration endpoint, and the payload goes to the real sync endpoint - so a
    client/server disagreement about any single byte fails right here.
    """
    registration = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(MOALLEM),
        json={"device_id": DEVICE, "note": "signed by mobile-client/src/offline/signing.ts"},
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
    fields = {**FIELDS, "client_punch_id": punch_id, "nonce": uuid.uuid4().hex, "anchor_id": anchor_id}
    offset_s = 60.0
    clock = run_client(
        node_harness,
        {"mode": "time", "base": anchor_ms, "offset_s": offset_s, "wall_elapsed_s": offset_s},
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

    # The server verified and stored exactly the signature the TypeScript module produced.
    assert _scalar(
        "SELECT signature FROM punch_queue WHERE client_punch_id = ?", (punch_id,)
    ) == signed["signature"]
    assert _scalar(
        "SELECT status FROM punch_queue WHERE client_punch_id = ?", (punch_id,)
    ) == "accepted"
    assert _scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE source = 'offline' AND worker_id = ?", (MOALLEM,)
    ) == 1


def test_a_tampered_punch_from_the_android_module_is_refused(client, node_harness):
    """A signature that verifies for the wrong bytes must be refused, not stored.

    This is the negative half of the contract: the module signs honestly, so a payload whose
    *content* was edited after signing has to fail verification.
    """
    registration = client.post(
        "/api/v1/attendance/devices/register",
        headers=bearer(MOALLEM),
        json={"device_id": f"{DEVICE}-tamper", "note": "tamper case"},
    )
    assert registration.status_code == 200, registration.text[:300]
    device_key = registration.json()["device_key"]
    device_id = registration.json()["device_id"]

    anchor_ms = (datetime.now() - timedelta(hours=1)).strftime(TS)
    anchor_id = uuid.uuid4().hex
    _sql(
        "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) "
        "VALUES (?,?,?,?,?)",
        (anchor_id, device_id, MOALLEM, anchor_ms, anchor_ms),
    )
    anchor_signature = offline_sync.sign_anchor(
        device_id=device_id, worker_id=MOALLEM, server_time=anchor_ms, anchor_id=anchor_id
    )

    punch_id = str(uuid.uuid4())
    fields = {
        **FIELDS,
        "device_id": device_id,
        "client_punch_id": punch_id,
        "nonce": uuid.uuid4().hex,
        "anchor_id": anchor_id,
        "lat": SITE_LAT,
        "lon": SITE_LON,
    }
    clock = run_client(
        node_harness,
        {"mode": "time", "base": anchor_ms, "offset_s": 60.0, "wall_elapsed_s": 60.0},
    )
    fields["effective_timestamp"] = clock["effective_timestamp"]
    signed = run_client(node_harness, {"device_key": device_key, "fields": fields})

    payload = {
        "client_punch_id": punch_id,
        "action": fields["action"],
        "anchor_id": anchor_id,
        "anchor_server_time": anchor_ms,
        "anchor_signature": anchor_signature,
        "monotonic_offset_s": 60.0,
        # The clock-out that never happened: the hours are what an attacker would move.
        "action_changed_after_signing": None,
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
    payload["action"] = "Clock Out"          # edited after signing

    response = client.post(
        "/api/v1/attendance/sync",
        headers=bearer(MOALLEM),
        json={"device_id": device_id, "punches": [payload]},
    )
    assert response.status_code == 200, response.text[:400]
    assert [item["status"] for item in response.json()["results"]] == ["rejected"]
    assert _scalar(
        "SELECT rejection_code FROM punch_queue WHERE client_punch_id = ?", (punch_id,)
    ) == offline_sync.ERR_BAD_SIGNATURE
    assert _scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE source = 'offline' AND request_id = ?", (punch_id,)
    ) == 0, "a refused punch must not become a payable row"
