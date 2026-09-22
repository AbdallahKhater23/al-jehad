"""Materialised offline punches: held for review, and scored when their selfie arrives.

TWO PROPERTIES, AND WHY THEY ARE ONE FILE
-----------------------------------------
``materialize_worker`` turns a verified queue row into attendance. That is entirely
different from *confirming* it: the punch was taken with no signal, so nothing about the
face, the frame or even the site was checked when it happened, and the only witness is a
photo sitting on the worker's own phone. Two consequences follow, and they are tested here
together because either one alone is a half-truth:

1. **Nothing arrives approved.** Every materialised punch lands in ``pending_review`` - or
   ``pending_overtime`` when the shift crossed the line, the same hold with a sharper reason
   - with its provenance recorded in ``flag_reason``, so the hours are not payable until a
   human says so and the review queue explains itself.
2. **The frame is scored when it reaches the server.** ``POST /attendance/sync/photo`` takes
   the selfie the phone kept and runs the online punch's own checks over it - MiniFASNet
   liveness, then the FaceNet-128 comparison - writing the verdict onto that review row. The
   photo is bound to the punch by the ``photo_sha256`` inside its signature, which is why a
   swapped image is refused rather than scored.

What scoring may **not** do is the third thing, and it is the one most likely to be added by
mistake: it cannot approve a punch, and it cannot refuse one either. An offline punch is in
review because nobody was there to watch it, and a model that runs hours later is not a
witness - it is evidence for the human who is. The tests that pin that are
``test_scoring_neither_approves_nor_rejects_the_punch`` and
``test_a_spoof_found_at_sync_is_recorded_not_turned_into_a_rejection``.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest
from harness import ADMIN, DB_PATH, MOALLEM, WORKER, bearer, db_scalar, jpeg_bytes

import face_detector
import face_engine
import liveness
import offline_sync

TS = "%Y-%m-%d %H:%M:%S"
DEVICE = "selfie-test-device-1"
SITE_LAT, SITE_LON = 30.05, 31.23          # inside "Downtown Tower A"

#: A real JPEG, because this endpoint decodes what it is given - the synthetic frame the rest
#: of the offline suite only ever hashes is not enough here.
SELFIE = jpeg_bytes()
SELFIE_SHA = hashlib.sha256(SELFIE).hexdigest()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _different_jpeg() -> bytes:
    """A second valid photo whose bytes differ from ``SELFIE``.

    Built from different pixels on purpose: ``harness.jpeg_bytes`` is deterministic, so two
    calls return the same image and the same hash, and a test that swapped "a photo" for "a
    photo" would be asserting nothing.
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (240, 240), (10, 200, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


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
        json={"device_id": device_id, "note": "offline selfie suite"},
    )
    assert response.status_code == 200, response.text[:300]
    return response.json()


def _plant_anchor(worker_id: str, device_id: str, server_time: str) -> dict:
    anchor_id = uuid.uuid4().hex
    _sql(
        "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) "
        "VALUES (?,?,?,?,?)",
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
    photo_sha256: str | None = SELFIE_SHA,
    worker_id: str = MOALLEM,
    device_id: str = DEVICE,
):
    """A correctly signed queued punch, for whichever moment ``offset_s`` names."""
    effective = (
        datetime.strptime(anchor["server_time"], TS) + timedelta(seconds=offset_s)
    ).strftime(TS)
    signature = offline_sync.sign_punch(
        key,
        device_id=device_id,
        worker_id=worker_id,
        action=action,
        client_punch_id=punch_id,
        nonce=nonce,
        anchor_id=anchor["anchor_id"],
        effective_timestamp=effective,
        lat=SITE_LAT,
        lon=SITE_LON,
        accuracy=8.0,
        photo_sha256=photo_sha256,
    )
    return {
        "client_punch_id": punch_id,
        "action": action,
        "anchor_id": anchor["anchor_id"],
        "anchor_server_time": anchor["server_time"],
        "anchor_signature": anchor["anchor_signature"],
        "monotonic_offset_s": offset_s,
        "nonce": nonce,
        "lat": SITE_LAT,
        "lon": SITE_LON,
        "accuracy": 8.0,
        "client_timestamp": effective,
        "client_offset_s": 0,
        "photo_sha256": photo_sha256,
        "signature": signature,
        "signature_version": 1,
    }


def _sync(client, punches, *, user_id=MOALLEM):
    return client.post(
        "/api/v1/attendance/sync",
        headers=bearer(user_id),
        json={"device_id": DEVICE, "punches": punches},
    )


def _upload(client, client_punch_id: str, blob: bytes = SELFIE, *, user_id=MOALLEM):
    return client.post(
        "/api/v1/attendance/sync/photo",
        headers=bearer(user_id),
        data={"client_punch_id": client_punch_id},
        files={"photo": ("selfie.jpg", blob, "image/jpeg")},
    )


def _sync_shift(client, tag: str, *, minutes: int = 60, photo: str | None = SELFIE_SHA) -> dict:
    """A whole offline shift (arrival + departure), materialised. Returns its punch ids.

    The anchor is planted before the shift began, and a long shift gets an older one - a
    punch whose effective time is in the future is refused by the offline guards (rightly),
    and the anchor is what the effective time is measured from.
    """
    key = _register(client)["device_key"]
    started = datetime.now() - timedelta(hours=max(5, minutes / 60 + 1))
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))
    response = _sync(
        client,
        [
            _punch(key, anchor, punch_id=f"{tag}-in", nonce=f"n-{tag}-in",
                   action="Clock In", offset_s=0, photo_sha256=photo),
            _punch(key, anchor, punch_id=f"{tag}-out", nonce=f"n-{tag}-out",
                   action="Clock Out", offset_s=minutes * 60, photo_sha256=photo),
        ],
    )
    assert response.status_code == 200, response.text[:400]
    assert response.json()["applied"] == 2, response.text[:400]
    return {"in": f"{tag}-in", "out": f"{tag}-out"}


def _sync_clock_in(client, punch_id: str, *, photo: str | None = SELFIE_SHA, hours_ago: int = 3) -> str:
    """One lone arrival: an open shift with no departure, for the one-row assertions."""
    key = _register(client)["device_key"]
    started = datetime.now() - timedelta(hours=hours_ago)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))
    response = _sync(
        client,
        [_punch(key, anchor, punch_id=punch_id, nonce=f"n-{punch_id}",
                action="Clock In", photo_sha256=photo)],
    )
    assert response.status_code == 200, response.text[:400]
    assert response.json()["applied"] == 1, response.text[:400]
    return punch_id


_LOG_COLUMNS = (
    "id", "action", "status", "status_code", "hours", "approved_hours", "overtime_hours",
    "score", "liveness_class", "liveness_score", "flag_reason",
)


def _log(client_punch_id: str) -> dict:
    """The materialised attendance row for one queued punch."""
    row = _sql(
        f"SELECT {', '.join(_LOG_COLUMNS)} FROM attendance_logs WHERE request_id = ? "
        "AND source = 'offline'",
        (client_punch_id,),
    )
    assert row, f"no materialised log for {client_punch_id}"
    return dict(zip(_LOG_COLUMNS, row[0]))


def _stats(client) -> dict:
    """This account's month, as the worker's own panel reads it.

    ``regular_hours`` is the payable figure and ``pending_hours`` the held one - the endpoint's
    names, not this suite's (see ``get_worker_stats``).
    """
    return client.get(f"/api/v1/worker/stats/{MOALLEM}", headers=bearer(ADMIN)).json()


def _forged_key(key: str) -> str:
    """The right length and alphabet, the wrong key.

    A signature over a key that is not even base64 raises before it can be wrong, which would
    test the decoder instead of the verification - so the forged key is derived from a real
    one rather than typed in.
    """
    replacement = "A" if key[10] != "A" else "B"
    return key[:10] + replacement + key[11:]


# ---------------------------------------------------------------------------
# nothing arrives approved
# ---------------------------------------------------------------------------
def test_a_materialised_shift_is_held_for_review_with_its_reason_recorded(client):
    """The row that carries the hours, and the arrival that carries none.

    Neither is approved, and they are not the same status - which is the distinction this test
    exists for. The departure is *held*, because that is where the money is and an administrator
    has to decide it. The arrival is *unverified*: there is nothing for anybody to decide, and a
    ``pending_review`` row would stop this worker closing their shift at all (both the online
    clock-out and the quick links refuse while one exists), so an arrival during a dead spot would
    leave the day unrecorded. Both rows say what they are, in words.
    """
    ids = _sync_shift(client, "review")
    arrival = _log(ids["in"])
    departure = _log(ids["out"])

    assert departure["status_code"] == "pending_review"
    assert departure["status"] == "pending_review"
    assert arrival["status_code"] == "unverified_offline", (
        f"the arrival is recorded, not checked - and not held either: {arrival['status_code']!r}"
    )
    assert arrival["status"] != "approved" and arrival["status_code"] != "approved"
    for log in (arrival, departure):
        assert "offline punch" in (log["flag_reason"] or ""), (
            f"the row has to say what it is: {log['flag_reason']!r}"
        )
    # 1 h on site is nothing like the overtime line, so this is the plain hold.
    assert departure["overtime_hours"] is None


def test_an_offline_arrival_does_not_stop_the_worker_clocking_out(client):
    """The regression the arrival's own status exists to avoid.

    ``/attendance/verify`` (clock-out) and the quick links both refuse while *any*
    ``pending_review`` row exists for the worker, and that guard is right: an unresolved flag is
    unresolved. But it would make an offline *arrival* - a row no administrator needs to decide,
    with no hours on it - lock the worker out of closing the shift they are standing in. The day
    would then never be recorded at all, and the dead spot would cost them their hours. So the
    arrival is unverified rather than held, and the shift still closes.
    """
    from harness import clock_in

    arrival = _sync_clock_in(client, "arrival-only", hours_ago=5)
    assert _log(arrival)["status_code"] == "unverified_offline"

    queue = client.get("/api/v1/admin/pending_reviews", headers=bearer(ADMIN)).json()
    assert all(str(row["id"]) != str(_log(arrival)["id"]) for row in queue), (
        "a row with no hours and no decision belongs to nobody's queue"
    )

    # ``confirmed=True`` is the worker answering the "you are clocking out early" question, which
    # is a different guard and not the one under test: it is answered *after* the flag check, so a
    # held arrival would still be a 403 rather than a confirmation prompt.
    closed = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM), confirmed=True)
    assert closed.status_code == 200, (
        f"an offline arrival must not block the worker's own clock-out: {closed.text[:300]}"
    )
    day = _sql(
        "SELECT status_code, hours FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out' "
        "AND source = 'online'",
        (MOALLEM,),
    )
    assert day, "and the day is recorded"
    assert day[0][1] and day[0][1] > 0


def test_a_held_shift_is_not_payable_until_an_administrator_approves_it(client):
    """The whole point of the hold, read through the payroll arithmetic and the queue.

    Compared against this account's own totals before the shift rather than against zero: the
    suite runs on a clone of the live database, and a number this test does not own is a
    number it cannot assert.
    """
    baseline = _stats(client)
    ids = _sync_shift(client, "payhold")
    hours = _log(ids["out"])["hours"]
    assert hours and hours > 0, "the hours are recorded, not discarded"

    held = _stats(client)
    assert held["regular_hours"] == pytest.approx(baseline["regular_hours"], abs=0.01), (
        "an offline shift nobody has approved must not read as payable - that is what 'held "
        "for review' has to mean in the money"
    )
    assert held["pending_hours"] == pytest.approx(baseline["pending_hours"] + hours, abs=0.01)

    queue = client.get("/api/v1/admin/pending_reviews", headers=bearer(ADMIN)).json()
    held_rows = [
        row for row in queue
        if row["worker_id"] == MOALLEM and str(row["id"]) == str(_log(ids["out"])["id"])
    ]
    assert held_rows, "an offline shift has to be visible in the review queue"

    # ... and approving it is what makes it payable. The queue is not a dead end.
    approved = client.post(
        "/api/v1/admin/approve_review",
        headers=bearer(ADMIN),
        json={"log_id": held_rows[0]["id"], "note": "saw the punches, hours are right"},
    )
    assert approved.status_code == 200, approved.text[:300]
    after = _stats(client)
    assert after["regular_hours"] == pytest.approx(baseline["regular_hours"] + hours, abs=0.01)
    assert after["pending_hours"] == pytest.approx(baseline["pending_hours"], abs=0.01)


def test_the_review_queue_is_told_about_an_offline_clock_out_exactly_once(client):
    """One notice per shift, on the row that carries the money, and none for the arrival.

    Admins do not watch the queue; they read notifications. An offline shift with no notice is
    a shift nobody reviews, and two notices for one shift is how a queue stops being read.
    """
    ids = _sync_shift(client, "notice")
    notices = _sql(
        "SELECT title, log_id FROM admin_notifications WHERE dedupe_key LIKE ?",
        (f"offline_review:{MOALLEM}:%",),
    )
    assert len(notices) == 1, f"one shift, one notice - the arrival sends none: {notices}"
    title, log_id = notices[0]
    assert str(log_id) == str(_log(ids["out"])["id"]), "the notice has to name the row it is about"
    assert "offline" in title.lower()


def test_a_long_offline_shift_keeps_the_overtime_hold_and_only_that_notice(client):
    """Past the line, the overtime hold wins - the same review with a sharper reason.

    It sends the overtime notice rather than the generic one: the overtime sentence is the one
    that says what to do about an amount of money, and two notices for one shift would make the
    review queue louder rather than more useful.
    """
    ids = _sync_shift(client, "long", minutes=13 * 60)
    log = _log(ids["out"])
    assert log["status_code"] == "pending_overtime"
    reason = log["flag_reason"] or ""
    assert "offline punch" in reason, f"the provenance is still recorded: {reason!r}"
    assert "reaches the" in reason and "overtime line" in reason, (
        f"and so is the rule the shift crossed: {reason!r}"
    )
    assert _sql("SELECT COUNT(*) FROM admin_notifications WHERE dedupe_key LIKE ?",
                (f"offline_overtime:{MOALLEM}:%",))[0][0] == 1
    assert _sql("SELECT COUNT(*) FROM admin_notifications WHERE dedupe_key LIKE ?",
                (f"offline_review:{MOALLEM}:%",))[0][0] == 0, "one shift, one notice"


# ---------------------------------------------------------------------------
# the queued selfie, scored at sync
# ---------------------------------------------------------------------------
def test_an_uploaded_selfie_is_scored_onto_the_review_row(client):
    """The frame the phone kept, through the same checks an online punch runs."""
    ids = _sync_shift(client, "score")
    upload = _upload(client, ids["out"])
    assert upload.status_code == 200, upload.text[:400]
    body = upload.json()
    assert body["status"] == "success"
    assert body["score"] is not None, "the comparison ran, so there is a distance"
    assert body["liveness_class"] is not None

    log = _log(ids["out"])
    assert log["score"] == pytest.approx(body["score"], abs=1e-6)
    assert log["liveness_class"] == body["liveness_class"]
    assert "sync selfie" in (log["flag_reason"] or ""), (
        f"the reviewer reads what the scoring found: {log['flag_reason']!r}"
    )
    assert "offline punch" in (log["flag_reason"] or ""), "and the provenance is still there"
    assert db_scalar("SELECT photo_scored_at FROM punch_queue WHERE client_punch_id = ?", (ids["out"],))
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'offline_selfie_scored'") == 1
    assert log["status_code"] == "pending_review", "and the shift is still held"


def test_scoring_neither_approves_nor_rejects_the_punch(client, monkeypatch):
    """A clean frame with a clean match is evidence. It is still not a decision.

    This is the property most tempting to "improve": both models agreed, so why hold the
    shift? Because the reason it is held is that nobody watched it happen, and a verdict
    produced hours later by a model that never saw the worker is not a witness. The row stays
    in the queue, the hours stay unpayable, and no administrator needs waking up.
    """
    monkeypatch.setattr(liveness, "inspect", _live_stub)
    ids = _sync_shift(client, "clean")
    upload = _upload(client, ids["out"])
    assert upload.status_code == 200, upload.text[:400]
    assert upload.json()["liveness_class"] == liveness.CLASS_LIVE

    log = _log(ids["out"])
    assert log["status_code"] == "pending_review", "a scored punch is still a held punch"
    assert log["liveness_class"] == liveness.CLASS_LIVE
    assert (log["score"] or 0.0) < 0.1, "the stub matches the enrolled template"
    assert log["approved_hours"] is None, "nothing is settled by a score"
    assert _sql(
        "SELECT COUNT(*) FROM admin_notifications WHERE log_id = ? AND kind IN "
        "('liveness_spoof', 'liveness_degraded')", (log["id"],)
    )[0][0] == 0, "a frame that was confirmed is not something to notify about"


def test_a_spoof_found_at_sync_is_recorded_not_turned_into_a_rejection(client, monkeypatch):
    """The frame is hours old by then - the punch has already happened, so it cannot be refused.

    What the administrator gets is the finding: the row is marked with the attack class, a
    critical notification names the punch, and the shift stays in review for a human. Refusing
    it from here would be a model deciding about pay with nobody told.
    """
    monkeypatch.setattr(liveness, "inspect", _spoof_stub)
    ids = _sync_shift(client, "spoof")
    upload = _upload(client, ids["out"])
    assert upload.status_code == 200, upload.text[:400]
    assert upload.json()["liveness_class"] == liveness.CLASS_PRINT

    log = _log(ids["out"])
    assert log["status_code"] == "pending_review", "a finding, not a decision"
    assert log["liveness_class"] == liveness.CLASS_PRINT
    assert log["liveness_score"] == pytest.approx(0.93, abs=0.01)

    notices = _sql(
        "SELECT severity, body FROM admin_notifications WHERE kind = 'liveness_spoof' AND log_id = ?",
        (log["id"],),
    )
    assert len(notices) == 1, "the finding has to reach an administrator"
    severity, body = notices[0]
    assert severity == "critical"
    assert "spoof" in body.lower() or "print" in body.lower()


def test_the_selfie_must_hash_to_what_the_punch_was_signed_with(client):
    """A frame is evidence for *this* punch or it is nothing.

    ``photo_sha256`` is inside the HMAC the device signed, so a photo that does not hash to it
    was not the photo taken at that punch - swapping it would let the evidence describe a
    different moment. The refusal happens before anything is decoded or scored.
    """
    other = _different_jpeg()
    assert hashlib.sha256(other).hexdigest() != SELFIE_SHA, "the two frames must differ"
    ids = _sync_shift(client, "binding")

    wrong = _upload(client, ids["out"], blob=other)
    assert wrong.status_code == 422, wrong.text[:300]
    assert wrong.json()["detail"]["error_code"] == offline_sync.ERR_PHOTO_MISMATCH

    log = _log(ids["out"])
    assert "sync selfie" not in (log["flag_reason"] or ""), "nothing may be written from a refused photo"
    assert db_scalar(
        "SELECT photo_scored_at FROM punch_queue WHERE client_punch_id = ?", (ids["out"],)
    ) is None
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'offline_selfie_scored'") == 0

    # The right photo, afterwards, is still accepted: the refusal was about the bytes.
    assert _upload(client, ids["out"]).status_code == 200


def test_a_punch_that_carried_no_selfie_has_nothing_to_score(client):
    punch_id = _sync_clock_in(client, "bare", photo=None)
    response = _upload(client, punch_id)
    assert response.status_code == 409, response.text[:300]
    assert response.json()["detail"]["error_code"] == offline_sync.ERR_PUNCH_NO_SELFIE


def test_a_selfie_cannot_be_attached_to_somebody_elses_punch(client):
    """Scoped to the caller: a punch id is not a capability."""
    ids = _sync_shift(client, "mine")
    stranger = _upload(client, ids["out"], user_id=WORKER)
    assert stranger.status_code == 404, stranger.text[:300]
    assert stranger.json()["detail"]["error_code"] == offline_sync.ERR_PUNCH_UNKNOWN
    assert db_scalar(
        "SELECT photo_scored_at FROM punch_queue WHERE client_punch_id = ?", (ids["out"],)
    ) is None


def test_an_unknown_punch_and_a_refused_one_are_both_answered(client):
    """Unknown, and known-but-refused: two different answers, neither of them a 500."""
    key = _register(client)["device_key"]
    unknown = _upload(client, "never-seen-this-id")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error_code"] == offline_sync.ERR_PUNCH_UNKNOWN

    # A punch signed with a key that is not this device's is stored as refused, with no row.
    started = datetime.now() - timedelta(hours=3)
    anchor = _plant_anchor(MOALLEM, DEVICE, started.strftime(TS))
    _sync(client, [_punch(_forged_key(key), anchor, punch_id="refused-1", nonce="n-r",
                          action="Clock Out")])
    assert db_scalar(
        "SELECT status FROM punch_queue WHERE client_punch_id = ?", ("refused-1",)
    ) == "rejected"

    refused = _upload(client, "refused-1")
    assert refused.status_code == 409, refused.text[:300]
    assert refused.json()["detail"]["error_code"] == offline_sync.ERR_PUNCH_NOT_ACCEPTED


def test_scoring_the_same_selfie_twice_is_idempotent(client):
    """A phone that lost the response uploads again; the evidence is written once.

    Re-running the models would rewrite the same row and re-notify an administrator who has
    already been told, so the second attempt is answered from the row instead.
    """
    ids = _sync_shift(client, "twice")
    first = _upload(client, ids["out"])
    assert first.status_code == 200, first.text[:400]
    before = _log(ids["out"])

    again = _upload(client, ids["out"])
    assert again.status_code == 200, again.text[:300]
    assert again.json()["status"] == "already_scored"
    assert again.json()["score"] == pytest.approx(before["score"], abs=1e-6)

    after = _log(ids["out"])
    assert after["flag_reason"] == before["flag_reason"], (
        "the sentence is written once; a retry must not stack a second copy of it"
    )
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'offline_selfie_scored'") == 1


def test_a_busy_face_engine_leaves_the_photo_for_next_time(client, monkeypatch):
    """503, not a verdict: the server could not do the work, so nothing is recorded.

    Recording a verdict here would leave the phone with no reason to keep the frame, and
    "unavailable" on the row would be a fact about the server that a reviewer would read as a
    fact about the worker.
    """
    ids = _sync_shift(client, "busy")

    def _busy(*args, **kwargs):
        raise face_engine.FaceEngineBusy("the face engine queue stayed full")

    monkeypatch.setattr(face_engine.ENGINE, "run_async", _busy)
    response = _upload(client, ids["out"])
    assert response.status_code == 503, response.text[:300]
    assert response.headers.get("retry-after")

    assert db_scalar(
        "SELECT photo_scored_at FROM punch_queue WHERE client_punch_id = ?", (ids["out"],)
    ) is None
    assert _log(ids["out"])["liveness_class"] == offline_sync.LIVENESS_OFFLINE, (
        "still unverified - not 'unavailable', which would read as a verdict"
    )

    # The retry, once the engine has room, is a first attempt rather than a second one.
    monkeypatch.undo()
    assert _upload(client, ids["out"]).status_code == 200
    assert _log(ids["out"])["liveness_class"] != offline_sync.LIVENESS_OFFLINE


def test_an_oversized_or_impostor_photo_is_refused_by_the_shared_upload_policy(client):
    """One policy for every upload in this app - this endpoint does not get its own.

    ``uploads.read_photo`` enforces the ceiling while it is still reading and decides the type
    from the bytes, which is why a PDF named ``selfie.jpg`` never reaches a decoder.
    """
    ids = _sync_shift(client, "policy")

    not_an_image = _upload(client, ids["out"], blob=b"%PDF-1.7 not a selfie at all\n")
    assert not_an_image.status_code == 415, not_an_image.text[:300]
    assert not_an_image.json()["detail"]["error_code"] == "not_an_image"

    assert _upload(client, ids["out"], blob=b"").status_code == 400
    assert db_scalar(
        "SELECT photo_scored_at FROM punch_queue WHERE client_punch_id = ?", (ids["out"],)
    ) is None


def test_the_scoring_path_needs_a_session(client):
    """No token, no scoring: a selfie is personal data and this endpoint is a worker's own."""
    response = client.post(
        "/api/v1/attendance/sync/photo",
        data={"client_punch_id": "anything"},
        files={"photo": ("selfie.jpg", SELFIE, "image/jpeg")},
    )
    assert response.status_code == 401


def test_a_frame_with_no_face_is_recorded_as_evidence_not_as_a_number(client):
    """The online path answers this with a 400. Here it cannot.

    Online, "no face in the frame" sends the worker back to the camera, because they are still
    standing there. An offline punch has already happened, so the finding is written down - and
    the score stays the "no score" sentinel rather than the comparison's 99.9, because a
    sentinel is not a distance and a reviewer must not read one as a near miss.
    """
    from harness import FAKE_ENGINE

    original = FAKE_ENGINE.FACE_MODE
    FAKE_ENGINE.FACE_MODE = "none"
    try:
        ids = _sync_shift(client, "noface")
        upload = _upload(client, ids["out"])
        assert upload.status_code == 200, upload.text[:300]
        assert upload.json()["score"] is None
        assert "no face" in (upload.json()["match_error"] or "").lower()

        log = _log(ids["out"])
        assert log["score"] == offline_sync.OFFLINE_SCORE, "0.0 is 'no score', never a match"
        assert "no face match" in (log["flag_reason"] or ""), log["flag_reason"]
        assert log["status_code"] == "pending_review"
    finally:
        FAKE_ENGINE.FACE_MODE = original


def test_a_mismatch_at_sync_is_named_as_a_distance_not_as_a_decision(client):
    """The band that an online punch refuses outright is recorded here, with its number.

    A reviewer can act on "distance 2.00, outside the approval band". They cannot act on a
    boolean, and refusing the shift from here would be spending the worker's day on a model's
    opinion with nobody told.
    """
    from harness import FAKE_ENGINE

    original = FAKE_ENGINE.FACE_MODE
    FAKE_ENGINE.FACE_MODE = "mismatch"
    try:
        ids = _sync_shift(client, "farband")
        upload = _upload(client, ids["out"])
        assert upload.status_code == 200, upload.text[:300]
        assert upload.json()["match"] == face_detector.MATCH_REFUSED

        log = _log(ids["out"])
        assert log["status_code"] == "pending_review"
        assert "outside the approval band" in (log["flag_reason"] or ""), log["flag_reason"]
        assert _sql(
            "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'liveness_degraded' AND log_id = ?",
            (log["id"],),
        )[0][0] == 1
    finally:
        FAKE_ENGINE.FACE_MODE = original


def test_the_materialised_row_keeps_the_score_it_can_have_and_no_more(client):
    """Before the selfie arrives the row carries no distance at all.

    ``score`` is NOT NULL, so the sentinel is 0.0 - and 0.0 is also what a perfect match
    produces, which is exactly why the reason on the row matters: a reviewer reads "the queued
    selfie has not arrived" rather than a number that looks like a match.
    """
    ids = _sync_clock_in(client, "unscored")
    log = _log(ids)
    assert log["score"] == offline_sync.OFFLINE_SCORE
    assert log["liveness_class"] == offline_sync.LIVENESS_OFFLINE
    assert "queued selfie" in (log["flag_reason"] or ""), log["flag_reason"]
    assert "sync selfie" not in (log["flag_reason"] or "")


# ---------------------------------------------------------------------------
# liveness stubs, in the shape the engine really returns
# ---------------------------------------------------------------------------
def _live_stub(image, **kwargs) -> liveness.GateDecision:
    return liveness.GateDecision(
        allowed=True,
        result=liveness.LivenessResult(
            verdict=liveness.VERDICT_LIVE,
            is_live=True,
            available=True,
            genuine_prob=0.98,
            model="stub",
        ),
        note="stubbed for this test",
    )


def _spoof_stub(image, **kwargs) -> liveness.GateDecision:
    return liveness.GateDecision(
        allowed=True,   # advisory mode: record and notify, never block
        result=liveness.LivenessResult(
            verdict=liveness.VERDICT_SPOOF,
            is_live=False,
            available=True,
            spoof_class=liveness.CLASS_PRINT,
            confidence=0.93,
            detail="a print held up to the camera",
            model="stub",
        ),
        note="stubbed for this test",
    )
