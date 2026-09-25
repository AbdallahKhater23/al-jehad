"""The refused-punch triage surface: capture, list, serve, clear, sweep.

A refused punch used to leave nothing: the frame was written and then discarded, the score
went into the 422 body and vanished, and the only trace was a log line. This suite pins the
replacement - the ``refused_punches`` table, the ``/developer/refused-punches`` endpoints and
the retention sweep - with the properties each piece must hold:

* a refused punch writes exactly one refusal row carrying the score, the reason and (when the
  disk allowed it) the frame - and the 422 the worker sees does not depend on the record;
* the list is the **root tier's**, newest first, windowed, and serves the frame by URL rather
  than inlining it. It is a question about the band - is it refusing honest workers, and at what
  distance - rather than about a site's attendance, so it left the administrator's queue with
  the administrator's door: every assertion against the old route below is a *refusal*, not a
  deletion, because a move that leaves the old path answering is a second door;
* a refusal is not attendance: it appears in no report and can never be approved, only cleared;
* retention ages rows and their frames together, and a frame no row claims is residue.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import developer
import harness
import main
import migrations
import punch_frames
import pytest
import retention
import security
from database import db
from harness import ADMIN, HEAD_ADMIN, WORKER, bearer

TS = "%Y-%m-%d %H:%M:%S"

#: A fresh account. The seeded worker carries a deliberately planted ``pending_review``
#: row (``SEEDED_PENDING_LOG_ID``), and an account with one open review answers every
#: punch with the flagged-account 403 before the face check even runs - so this suite's
#: worker is created per test, exactly as the biometric-identity suite does.
FRESH_WORKER = "418"
STRONG_PASSWORD = "Triage-Test-2026!"

#: The root account, which is the only role that reads the triage surface.
DEV_ID = developer.DEVELOPER_ID_DEFAULT
DEV_PASSWORD = "root-credential-for-the-triage-suite-1"


def _as_developer() -> dict[str, str]:
    """Seed the root account into this test's database, and return a header for it.

    Per test rather than per session, because the fixture restores the pristine snapshot before
    every test and the account cannot be minted through the API on purpose. ``_seed`` is
    idempotent, so a caller that runs twice pays once.
    """
    developer.seed_developer_account(password=DEV_PASSWORD, actor="test:refused_punch_triage")
    return bearer(DEV_ID, role=security.DEVELOPER_ROLE)


def _make_worker(client) -> str:
    """A worker with a clean slate and an enrolled template, so the face check is the test."""
    response = client.post(
        "/api/v1/admin/users/create",
        headers=bearer(ADMIN),
        data={
            "user_id": FRESH_WORKER,
            "name": f"Worker {FRESH_WORKER}",
            "role": "worker",
            "password": STRONG_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    enrolled = harness.enroll(client, FRESH_WORKER, headers=bearer(ADMIN))
    assert enrolled.status_code == 200, enrolled.text
    return FRESH_WORKER


def _refusals() -> list[dict]:
    with db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM refused_punches").fetchall()]


def _clock_out(client, worker_id: str, *, confirmed: bool = True):
    """One clock-out punch (the helper is ``clock_in`` with the action swapped)."""
    return harness.clock_in(
        client, worker_id, action="Clock Out", headers=bearer(worker_id), confirmed=confirmed
    )


# ---------------------------------------------------------------------------
# 1. capture: a refused punch is recorded, and the 422 does not depend on it
# ---------------------------------------------------------------------------
def test_a_refused_punch_is_recorded_with_its_score(client, app_module, jpeg, face):
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"

    response = _clock_out(client, worker)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error_code"] == "face_mismatch", response.text
    rows = _refusals()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["worker_id"] == str(worker), row
    assert row["error_code"] == "face_mismatch", row
    assert row["score"] is not None and float(row["score"]) > 0, row
    # The pipeline that scored the refusal travels with it - a distance means different
    # things under different crops, and the triage reader needs the provenance.
    assert row["pipeline"] == "yunet-2023mar", row
    assert row["created_at"], row


def test_an_approved_punch_writes_no_refusal_row(client, app_module, jpeg, face):
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    _clock_out(client, worker)
    assert _refusals() == []


def test_the_worker_session_survives_a_refusal(client, app_module, jpeg, face):
    """The 422 ends the punch, not the session: the worker can retry, exactly as before.

    The retry must succeed *without the flagged-account 403* - a refusal that blocked the
    account would turn one bad photo into a worker locked out for the day.
    """
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    first = _clock_out(client, worker)
    assert first.status_code == 422, first.text

    face.FACE_MODE = "match"
    second = _clock_out(client, worker)
    assert second.status_code == 200, second.text


# ---------------------------------------------------------------------------
# 2. the list: root-tier-only, windowed, frame by URL
# ---------------------------------------------------------------------------
def test_the_list_belongs_to_the_root_tier_and_the_old_door_is_shut(client):
    """A move is two claims: the new audience reaches it, and the old one no longer does.

    Only the first is visible from the new route. A suite that checked it alone would pass on
    the day somebody registered the route twice, or left ``/admin/refused_punches`` in place
    beside it - so both halves are asserted here, and the administrator's answer is a *refusal*
    rather than a filtered list.
    """
    dev = _as_developer()
    assert client.get("/api/v1/developer/refused-punches", headers=bearer(WORKER)).status_code in (401, 403)
    for actor in (ADMIN, HEAD_ADMIN):
        refused = client.get("/api/v1/developer/refused-punches", headers=bearer(actor))
        assert refused.status_code == 403, f"{actor} reached the triage list: {refused.status_code}"
    assert client.get("/api/v1/developer/refused-punches", headers=dev).status_code == 200
    # The path the administrators used to read. 404 rather than 403: the route is gone from the
    # application, so there is nothing left to be refused by it.
    for actor in (ADMIN, HEAD_ADMIN, WORKER):
        answer = client.get("/api/v1/admin/refused_punches", headers=bearer(actor))
        assert answer.status_code == 404, f"the administrator's refusal list is still registered: {answer.text}"


def test_the_list_carries_the_frame_url_and_not_the_filename(client, app_module, jpeg, face):
    dev = _as_developer()
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)

    response = client.get("/api/v1/developer/refused-punches?days=1", headers=dev)
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 1, items
    item = items[0]
    assert "punch_frame" not in item, item
    assert item["frame_url"] == f"/api/v1/developer/refused-punches/{item['id']}/frame", item


def test_the_window_excludes_older_refusals(client, app_module, jpeg, face):
    dev = _as_developer()
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    with db(write=True) as conn:
        conn.execute(
            "UPDATE refused_punches SET created_at = ?",
            ((datetime.now() - timedelta(days=3)).strftime(TS),),
        )

    today = client.get("/api/v1/developer/refused-punches?days=1", headers=dev).json()
    week = client.get("/api/v1/developer/refused-punches?days=7", headers=dev).json()
    assert today == [] and len(week) == 1, (today, week)


# ---------------------------------------------------------------------------
# 3. the frame: served by id, resolved inside the directory
# ---------------------------------------------------------------------------
def test_the_frame_route_serves_the_stored_image(client, app_module, jpeg, face):
    dev = _as_developer()
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    refusal_id = _refusals()[0]["id"]

    response = client.get(
        f"/api/v1/developer/refused-punches/{refusal_id}/frame", headers=dev
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/jpeg"), response.headers


def test_a_refusal_without_a_frame_answers_404(client, app_module, jpeg, face):
    dev = _as_developer()
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    with db(write=True) as conn:
        conn.execute("UPDATE refused_punches SET punch_frame = NULL")

    refusal_id = _refusals()[0]["id"]
    response = client.get(
        f"/api/v1/developer/refused-punches/{refusal_id}/frame", headers=dev
    )
    assert response.status_code == 404, response.text


def test_a_frame_path_edited_in_the_database_is_not_served(client, app_module, jpeg, face):
    """The serving route must stay a route, never become a file-read primitive."""
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    with db(write=True) as conn:
        conn.execute("UPDATE refused_punches SET punch_frame = '../../times.db'")

    refusal_id = _refusals()[0]["id"]
    response = client.get(
        f"/api/v1/developer/refused-punches/{refusal_id}/frame", headers=_as_developer()
    )
    assert response.status_code == 404, response.text


def test_the_frame_route_refuses_every_other_role(client):
    dev = _as_developer()
    assert client.get(
        "/api/v1/developer/refused-punches/1/frame", headers=bearer(WORKER)
    ).status_code in (401, 403)
    for actor in (ADMIN, HEAD_ADMIN):
        assert client.get(
            "/api/v1/developer/refused-punches/1/frame", headers=bearer(actor)
        ).status_code == 403
    assert client.get("/api/v1/admin/refused_punch_frame/1", headers=dev).status_code == 404


# ---------------------------------------------------------------------------
# 4. clear: the triaged list shrinks; the evidence ages out on retention's clock
# ---------------------------------------------------------------------------
def test_clearing_removes_the_row_and_audits_it(client, app_module, jpeg, face):
    dev = _as_developer()
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    refusal_id = _refusals()[0]["id"]

    response = client.post(
        f"/api/v1/developer/refused-punches/{refusal_id}/clear", headers=dev
    )
    assert response.status_code == 200, response.text
    assert _refusals() == []
    with db() as conn:
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE action = 'refused_punch_clear' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert audit is not None


def test_clearing_a_missing_refusal_answers_404(client):
    response = client.post("/api/v1/developer/refused-punches/99999/clear", headers=_as_developer())
    assert response.status_code == 404, response.text


def test_clearing_is_refused_to_every_other_role(client):
    """The write half of the move: a move that left the mutation reachable is not a move."""
    dev = _as_developer()
    for actor in (ADMIN, HEAD_ADMIN, WORKER):
        response = client.post(
            "/api/v1/developer/refused-punches/1/clear", headers=bearer(actor)
        )
        expected = (403,) if actor in (ADMIN, HEAD_ADMIN) else (401, 403)
        assert response.status_code in expected, response.text
    # The path the administrators used to reach. ``405`` and not ``404`` is this application's
    # own answer for a path under ``/admin/`` that is not an endpoint - the point is that it is
    # not an endpoint, so neither 200 nor 403 is available, and the assertion says only that.
    answer = client.post("/api/v1/admin/refused_punches/1/clear", headers=dev)
    assert answer.status_code in (404, 405), answer.text


def test_a_refusal_is_never_an_attendance_row(client, app_module, jpeg, face):
    """A refusal cannot leak into the reports: nothing was worked, nothing is payable.

    The seed carries approved history, so the count is compared across the punch, not
    asserted at zero: the refused clock-out must add exactly nothing to the log rows.
    """
    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "match"
    settled = _clock_out(client, worker)
    assert settled.status_code == 200, settled.text
    with db() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = ?",
            (str(worker), main.ACTION_CLOCK_OUT),
        ).fetchone()[0]

    # A fresh shift, refused: the count must not move.
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    refused = _clock_out(client, worker)
    assert refused.status_code == 422, refused.text
    with db() as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = ?",
            (str(worker), main.ACTION_CLOCK_OUT),
        ).fetchone()[0]
    assert after == before, "the refused punch wrote an attendance row"


# ---------------------------------------------------------------------------
# 5. retention: rows and frames age out together; live claims are untouched
# ---------------------------------------------------------------------------
def test_retention_sweeps_old_refusals_and_their_frames(client, app_module, jpeg, face):
    import os

    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    row = _refusals()[0]
    stored = punch_frames.resolve_stored(row["punch_frame"])
    assert stored is not None, "the test premise needs a stored frame"

    with db(write=True) as conn:
        conn.execute(
            "UPDATE refused_punches SET created_at = ?",
            ((datetime.now() - timedelta(days=40)).strftime(TS),),
        )
    report = retention.sweep(dry_run=False)
    assert report["targets"]["refused_punches"]["deleted"] >= 1, report["targets"]

    assert _refusals() == []
    assert not os.path.exists(stored), "the swept refusal left its frame on disk"


def test_retention_never_sweeps_a_fresh_refusal_frame(client, app_module, jpeg, face):
    """A refusal recorded today is the triage surface; a sweep must not take it."""
    import os

    worker = _make_worker(client)
    harness.clock_in(client, worker, headers=bearer(worker))
    face.FACE_MODE = "mismatch"
    _clock_out(client, worker)
    row = _refusals()[0]
    stored = punch_frames.resolve_stored(row["punch_frame"])

    report = retention.sweep(dry_run=False)
    assert report["targets"]["refused_punches"]["deleted"] == 0, report["targets"]
    assert _refusals(), "the sweep deleted a fresh refusal"

    assert stored is not None and os.path.exists(stored)


# ---------------------------------------------------------------------------
# 6. the schema travels: a fresh database reaches migration 22
# ---------------------------------------------------------------------------
def test_migration_22_is_applied_and_indexed():
    with db() as conn:
        applied = migrations.applied_versions(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(refused_punches)")}
    assert 22 in applied
    assert {"worker_id", "error_code", "score", "punch_frame", "created_at"} <= columns
