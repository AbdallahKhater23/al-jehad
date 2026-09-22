"""The phone's half of the selfie contract: the frame goes up, then leaves the phone.

WHY THIS RUNS THE REAL FILE
---------------------------
``offline_queue.js`` keeps the photo the worker took next to the punch they signed, and the
server can only score a frame it has. Whether that upload happens is a property of the client
and of nothing else: the backend suites can prove the endpoint scores what it is given, but not
that anything ever gives it. A comment saying "then we upload the photo" cannot hold that
either, so this suite loads the file a browser loads, gives it an IndexedDB and a fetch whose
answers the test chooses, and drives ``OFFLINE.syncNow()`` for real.

What is pinned, and why each one is load-bearing:

* the frame is uploaded **after** the batch is accepted, to ``/attendance/sync/photo``,
  multipart, with ``client_punch_id`` naming the punch it belongs to;
* the client sets no ``Content-Type`` (only ``fetch`` knows the boundary it generated);
* a photo the server confirmed is **deleted from the phone** - a face does not sit in a
  worker's browser storage after it has served its purpose;
* a photo that could not be delivered is **kept**, with the punch still settled, so the next
  sync tries again;
* a *rejected* punch sends nothing, because there is no review row for it to be evidence for;
* the punch batch itself carries no photo: the frame is a separate request by design (see
  ``uploads.py``), and a batch that quietly started carrying base64 would be a body no
  handler can measure as it arrives.

Node is optional; without it the suite skips rather than fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from harness import PROJECT_ROOT

NODE = shutil.which("node")
CLIENT_JS = PROJECT_ROOT / "frontend" / "offline_queue.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

WORKER = {"id": "600", "token": "test-token"}
DEVICE_ROW = {
    "key": "device:600",
    "value": {"device_key": "a2V5", "worker_id": "600", "device_id": "web-600-test"},
}
ANCHOR_ROW = {
    "key": "anchor:600",
    "value": {
        "anchor_id": "anchor-1",
        "server_time": "2026-09-20 05:00:00",
        "anchor_signature": "sig",
        "signature_version": 1,
        "max_offline_hours": 72,
        "mono_ms": 0,
        "wall_ms": 0,
    },
}


def _punch(punch_id: str, *, photo_sha256: str | None = "a" * 64, status: str = "queued") -> dict:
    """A queued punch exactly as ``queuePunch`` stores one.

    ``worker_id`` is here because every read in the module is scoped by it - the queue for this
    worker, the photos to keep - and a record without it is invisible to the code under test in
    a way that looks like a broken feature rather than a broken fixture.
    """
    return {
        "client_punch_id": punch_id,
        "worker_id": WORKER["id"],
        "action": "Clock Out",
        "anchor_id": "anchor-1",
        "anchor_server_time": "2026-09-20 05:00:00",
        "anchor_signature": "sig",
        "monotonic_offset_s": 3600.0,
        "nonce": f"nonce-{punch_id}",
        "lat": 30.05,
        "lon": 31.23,
        "accuracy": 8.0,
        "client_timestamp": "2026-09-20 06:00:00",
        "client_offset_s": 0,
        "photo_sha256": photo_sha256,
        "signature": "f" * 64,
        "signature_version": 1,
        "status": status,
    }


def _photo(punch_id: str, size: int = 2048) -> dict:
    return {"client_punch_id": punch_id, "blob": {"__blob__": "image/jpeg", "size": size}}


SYNC_OK = {
    "match": "/attendance/sync",
    "status": 200,
    "body": {
        "status": "success",
        "applied": 1,
        "results": [
            {
                "client_punch_id": "p1",
                "status": "accepted",
                "effective_time": "2026-09-20 06:00:00",
                "site": "Downtown Tower A",
                "flagged": False,
            }
        ],
        "next_anchor": {
            "anchor_id": "anchor-2",
            "server_time": "2026-09-20 09:00:00",
            "anchor_signature": "sig2",
            "signature_version": 1,
            "max_offline_hours": 72,
        },
    },
}
PHOTO_OK = {"match": "/attendance/sync/photo", "status": 200, "body": {"status": "success"}}

NODE_HARNESS = r"""
const { OFFLINE } = require(process.argv[2]);
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));

// ---- a small IndexedDB: exactly the calls OFFLINE_DB makes, and no more ----
const data = { meta: new Map(), punches: new Map(), photos: new Map() };
function req(result) {
    const request = { result };
    queueMicrotask(() => { if (request.onsuccess) request.onsuccess(); });
    return request;
}
function storeFor(name) {
    const rows = () => [...data[name].values()];
    return {
        get: (key) => req(data[name].get(key)),
        put: (value) => {
            const key = value.client_punch_id !== undefined ? value.client_punch_id : value.key;
            data[name].set(key, value);
            return req(value);
        },
        delete: (key) => { data[name].delete(key); return req(undefined); },
        getAll: () => req(rows()),
        index: () => ({
            getAll: (range) => req(rows().filter((row) => {
                if (range === undefined) return true;
                if (range && range.only) {
                    return row.worker_id === range.only[0] && row.status === range.only[1];
                }
                return row.worker_id === range;
            }))
        })
    };
}
globalThis.IDBKeyRange = { only: (value) => ({ only: value }) };
globalThis.indexedDB = {
    open: () => {
        const database = {
            objectStoreNames: { contains: () => true },
            createObjectStore: () => ({ createIndex: () => {} }),
            transaction: (name) => ({ objectStore: () => storeFor(name) })
        };
        const request = { result: database };
        queueMicrotask(() => {
            if (request.onupgradeneeded) request.onupgradeneeded();
            if (request.onsuccess) request.onsuccess();
        });
        return request;
    }
};
globalThis.navigator = { onLine: payload.online !== false };
globalThis.window = { location: { origin: 'http://attendance.test' }, addEventListener: () => {} };
globalThis.document = { addEventListener: () => {}, visibilityState: 'visible' };
globalThis.State = { user: payload.worker };
globalThis.API = { baseURL: 'http://attendance.test/api/v1' };

const requests = [];
globalThis.fetch = async (url, options = {}) => {
    const body = options.body;
    const isForm = body && typeof body.entries === 'function';
    requests.push({
        url: String(url),
        method: options.method,
        headers: { ...(options.headers || {}) },
        form: isForm
            ? [...body.entries()].map(([key, value]) => [
                key,
                typeof value === 'string' ? value : `blob:${value.type}:${value.size}`
            ])
            : null,
        json: (!isForm && typeof body === 'string') ? JSON.parse(body || '{}') : null
    });
    const target = String(url);
    const answer = (payload.answers || []).find((one) => target.endsWith(one.match));
    if (!answer || answer.network === 'down') throw new TypeError('network down');
    return {
        ok: Number(answer.status) < 400,
        status: Number(answer.status),
        json: async () => (answer.body || {})
    };
};

(async () => {
    for (const row of payload.meta || []) data.meta.set(row.key, row);
    // The capture clock is stamped here, the way ``queuePunch`` stamps it: retention is
    // measured from it, so a fixture that left it undefined would be testing the pruning of a
    // punch that was never captured. A row may name its own moment (an old punch).
    for (const row of payload.punches || []) {
        data.punches.set(row.client_punch_id, {
            captured_wall_ms: Date.now() - 3600000,
            captured_mono_ms: 1000,
            ...row
        });
    }
    for (const row of payload.photos || []) {
        const blob = new Blob([new Uint8Array(row.blob.size)], { type: row.blob.__blob__ });
        data.photos.set(row.client_punch_id, { client_punch_id: row.client_punch_id, blob });
    }
    const summary = await OFFLINE.syncNow();
    process.stdout.write(JSON.stringify({
        summary,
        requests,
        punches: [...data.punches.values()],
        photos: [...data.photos.keys()]
    }));
})().catch((err) => {
    console.error((err && err.stack) || String(err));
    process.exit(1);
});
"""


@pytest.fixture(scope="module")
def node_harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("offline_upload") / "harness.js"
    path.write_text(NODE_HARNESS, encoding="utf-8")
    return path


def run_offline(node_harness, *, punches, photos=(), answers=(SYNC_OK, PHOTO_OK), **extra) -> dict:
    payload = {
        "worker": WORKER,
        "meta": [DEVICE_ROW, ANCHOR_ROW],
        "punches": list(punches),
        "photos": list(photos),
        "answers": list(answers),
        **extra,
    }
    completed = subprocess.run(
        [NODE, str(node_harness), str(CLIENT_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, f"client harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def _photo_requests(result: dict) -> list[dict]:
    return [row for row in result["requests"] if row["url"].endswith("/attendance/sync/photo")]


def _sync_requests(result: dict) -> list[dict]:
    return [row for row in result["requests"] if row["url"].endswith("/attendance/sync")]


# ---------------------------------------------------------------------------
# the upload itself
# ---------------------------------------------------------------------------
def test_a_settled_punch_uploads_its_selfie_and_then_drops_it(node_harness):
    """The frame goes to the endpoint that scores it, and the phone stops holding a face."""
    result = run_offline(node_harness, punches=[_punch("p1")], photos=[_photo("p1")])

    uploads = _photo_requests(result)
    assert len(uploads) == 1, f"exactly one frame for one punch: {result['requests']}"
    upload = uploads[0]
    assert upload["method"] == "POST"
    assert upload["headers"].get("Authorization") == f"Bearer {WORKER['token']}"
    assert not any(key.lower() == "content-type" for key in upload["headers"]), (
        "the client must not set Content-Type on a multipart body: only fetch knows the "
        "boundary it generated, and a hand-written header makes the body unparseable"
    )
    parts = dict(upload["form"] or [])
    assert parts.get("client_punch_id") == "p1", f"the frame names its punch: {upload['form']}"
    assert parts.get("photo", "").startswith("blob:image/jpeg:"), upload["form"]

    assert result["summary"]["photos"]["uploaded"] == 1
    assert result["summary"]["photos"]["pending"] == 0
    assert result["photos"] == [], "the server has it - a face does not stay in browser storage"
    punch = next(row for row in result["punches"] if row["client_punch_id"] == "p1")
    assert punch["photo_uploaded_at"], "and the punch remembers that its frame was scored"
    assert punch["status"] == "synced", "the punch itself is settled as before"


def test_the_punch_batch_does_not_carry_the_photo(node_harness):
    """The frame is a separate request on purpose - a batch of base64 would be unmeasurable.

    ``uploads.read_photo`` enforces the size ceiling *while the body arrives*; a JSON batch is
    parsed in full before any handler runs, so a photo hidden in the payload would be allocated
    before anybody could check it. This pins the client's half of that decision.
    """
    result = run_offline(node_harness, punches=[_punch("p1")], photos=[_photo("p1")])
    body = _sync_requests(result)[0]["json"]
    punch = body["punches"][0]
    assert "photo" not in punch, f"the batch carries punches, not frames: {sorted(punch)}"
    assert all(len(value) < 512 for value in punch.values() if isinstance(value, str)), (
        f"a base64 frame in the batch is the thing this shape exists to avoid: {punch}"
    )
    assert punch["photo_sha256"] == "a" * 64, (
        "the hash is what binds the uploaded frame to this punch, and the hash is signed"
    )


def test_a_photo_the_server_could_not_score_yet_is_kept_for_next_time(node_harness):
    """503 is "the server could not do it", not "the answer is no".

    The photo stays on the phone and the punch stays settled: the queue must not be disturbed
    by a face that has not been scored, and the worker is not asked to do anything about it.
    """
    answers = [SYNC_OK, {**PHOTO_OK, "status": 503, "body": {"detail": {"error_code": "face_check_busy"}}}]
    result = run_offline(node_harness, punches=[_punch("p1")], photos=[_photo("p1")], answers=answers)

    assert len(_photo_requests(result)) == 1
    assert result["summary"]["photos"]["uploaded"] == 0
    assert result["summary"]["photos"]["pending"] == 1
    assert result["summary"]["photos"]["error"], "and the reason travels with the summary"
    assert result["photos"] == ["p1"], "kept"
    punch = result["punches"][0]
    assert not punch.get("photo_uploaded_at")
    assert punch["status"] == "synced", "the punch is settled; only its evidence is owed"


def test_a_photo_that_found_no_network_keeps_the_photo_and_stops_the_replay(node_harness):
    """The upload runs on the same connection the punches do, so losing it means stop.

    A batch POST that is certain to fail is not worth making, and the photo has to survive for
    the attempt that will work.
    """
    answers = [SYNC_OK, {**PHOTO_OK, "network": "down"}]
    result = run_offline(node_harness, punches=[_punch("p1")], photos=[_photo("p1")], answers=answers)

    assert result["summary"]["offline"] is True
    assert result["summary"]["photos"]["pending"] == 1
    assert result["photos"] == ["p1"]
    assert len(_sync_requests(result)) == 1, "no second batch once the connection is gone"


# ---------------------------------------------------------------------------
# what is not uploaded
# ---------------------------------------------------------------------------
def test_a_rejected_punch_uploads_nothing(node_harness):
    """No review row, no evidence to give: a rejected punch's frame is not worth a request."""
    refused = {
        "match": "/attendance/sync",
        "status": 200,
        "body": {
            "applied": 0,
            "results": [{"client_punch_id": "p1", "status": "rejected", "code": "bad_signature"}],
            "next_anchor": SYNC_OK["body"]["next_anchor"],
        },
    }
    result = run_offline(
        node_harness, punches=[_punch("p1")], photos=[_photo("p1")], answers=[refused, PHOTO_OK]
    )
    assert _photo_requests(result) == []
    assert result["summary"]["photos"] == {"uploaded": 0, "pending": 0, "error": None}
    assert result["photos"] == ["p1"], "the phone keeps it; nothing asked for it"


def test_a_punch_signed_without_a_selfie_uploads_nothing(node_harness):
    """A punch with no ``photo_sha256`` has no frame to send, and no hash to bind one with."""
    result = run_offline(
        node_harness,
        punches=[_punch("p1", photo_sha256=None)],
        photos=[],   # a photo the module never recorded for this punch
    )
    assert _photo_requests(result) == []
    assert result["summary"]["photos"]["uploaded"] == 0
    assert result["summary"]["photos"]["pending"] == 0


def test_a_punch_whose_photo_is_already_gone_is_still_synced(node_harness):
    """The queue is the durable artifact, the photo is not: a lost frame must not block a punch."""
    result = run_offline(node_harness, punches=[_punch("p1")], photos=[])
    assert result["summary"]["applied"] == 1
    assert _photo_requests(result) == []
    assert result["punches"][0]["status"] == "synced"


def test_the_prune_keeps_an_unscored_photo_and_drops_a_scored_one(node_harness):
    """Retention follows the evidence, not the clock.

    A settled punch inside the retention window keeps its frame only while the server has not
    got it; once ``photo_uploaded_at`` is set there is nothing left for the copy to do.
    """
    scored = {**_punch("p1"), "status": "synced", "photo_uploaded_at": "2026-09-20 09:00:00"}
    owed = {**_punch("p2"), "status": "synced"}
    result = run_offline(
        node_harness,
        punches=[scored, owed],
        photos=[_photo("p1"), _photo("p2")],
        answers=[{**SYNC_OK, "body": {**SYNC_OK["body"], "applied": 0, "results": []}}, PHOTO_OK],
    )
    assert result["photos"] == ["p2"], (
        "the frame the server has was dropped; the one it is still owed was kept"
    )
