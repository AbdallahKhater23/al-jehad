"""Correcting the hours of a shift that has already been worked.

WHY THIS EXISTS
---------------
Every path that touched a filed clock-out row's hours was a *decision* path - the review
approve, the review reject, the force-out at the moment of closing. A settled shift
carrying a wrong number had no correction at all: the fix was raw SQL against the live
database, with no audit trail and no guard rails, which is exactly how a timesheet stops
being a record. ``POST /admin/shifts/{log_id}/hours`` is the ordinary audited way.

What the suite pins:

* the correction lands - ``hours`` and ``approved_hours`` move together (a record that
  disagreed with its own approval would repeat the contradiction in every report) and
  ``overtime_hours`` is **re-derived** from the corrected figure against the regular day,
  never typed;
* the edit is appended to the audit trail as ``shift_hours_edit`` with the before/after
  figures and the acting administrator - a timesheet that changed without a trail is a
  rumour;
* a shift still waiting on a decision (``pending_review``, ``pending_overtime``,
  ``unverified_offline``, ``auto_closed``) is refused: the edit must not stand in for the
  decision the Approvals queue exists to record;
* a clock-in row carries no shift hours and is refused - there is nothing on it to
  correct;
* the figure is bounded (0 to ``MAX_AUTHORISED_HOURS``, the same 24 h typo detector every
  other hours field answers to) and a refused request writes nothing;
* only an administrator reaches it at all.

    pytest backend/tests/test_shift_hours_edit.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from harness import ADMIN, WORKER, bearer, db_scalar, db_rows

import main
import overtime
from harness import db_rows

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"


@pytest.fixture(scope="module")
def client(app_module):
    """This module's own client, without the lifespan that boots the deployment.

    Same reasoning as the overtime suites: the startup gate is not this file's subject,
    but the requests below must go through the real ASGI stack, the route's own guard
    and its status codes.
    """
    instance = TestClient(app_module.app)
    try:
        yield instance
    finally:
        instance.close()


def _plant_closed_shift(worker_id: str = WORKER, *, paid_hours: float = 9.5, status_code: str = "approved") -> int:
    """A worked-and-filed shift, as a clock-out leaves it: the clock-in and the closed row."""
    clock_in = (datetime.now() - timedelta(hours=paid_hours + 0.5)).strftime(TS)
    clock_out = (datetime.now() - timedelta(days=1)).strftime(TS)
    with main.db(write=True) as conn:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        main._insert_log(
            conn,
            worker_id=worker_id,
            site_name=SITE,
            action=main.ACTION_CLOCK_IN,
            timestamp=clock_in,
            hours=0.0,
            score=1.0,
            status="approved",
            status_code="approved",
            source="online",
        )
        log_id = main._insert_log(
            conn,
            worker_id=worker_id,
            site_name=SITE,
            action=main.ACTION_CLOCK_OUT,
            timestamp=clock_out,
            hours=paid_hours,
            score=1.0,
            status="Approved by Admin",
            status_code=status_code,
            source="online",
            approved_hours=paid_hours,
            overtime_hours=max(0.0, paid_hours - 8.0),
        )
    return int(log_id)


def _edit(client, log_id: int, hours: float, note: str | None = None):
    body: dict = {"hours": hours}
    if note is not None:
        body["note"] = note
    return client.post(f"/api/v1/admin/shifts/{log_id}/hours", headers=bearer(ADMIN), json=body)


def test_a_settled_shifts_hours_can_be_corrected(client):
    log_id = _plant_closed_shift(paid_hours=9.5)

    response = _edit(client, log_id, 7.5, "punch pair covered a mid-shift errand")
    assert response.status_code == 200, response.text

    row = client.get(
        "/api/v1/admin/logs", headers=bearer(ADMIN)
    ).json()
    record = next(item for item in row if item["id"] == log_id)
    # The corrected figure is both the recorded and the approved hours: a record that
    # disagreed with its own approval would repeat the contradiction in every report.
    assert record["hours"] == pytest.approx(7.5, abs=0.01), record
    assert record["approved_hours"] == pytest.approx(7.5, abs=0.01), record
    # The overtime is re-derived, not typed: 7.5 h against an 8 h day credits none.
    assert record["overtime_hours"] == pytest.approx(0.0, abs=0.001), record
    # The row keeps its settled status: the edit corrects figures, it does not reopen
    # or rewrite the decision that was made.
    assert record["status_code"] == "approved", record


def test_the_correction_re_derives_overtime_above_the_regular_day(client):
    log_id = _plant_closed_shift(paid_hours=8.0, status_code="overtime_rejected")

    response = _edit(client, log_id, 10.0)
    assert response.status_code == 200, response.text

    rows = client.get("/api/v1/admin/logs", headers=bearer(ADMIN)).json()
    record = next(item for item in rows if item["id"] == log_id)
    assert record["overtime_hours"] == pytest.approx(2.0, abs=0.01), record


def test_the_edit_is_appended_to_the_audit_trail(client):
    log_id = _plant_closed_shift(paid_hours=9.5)

    _edit(client, log_id, 8.0, "typo on the approval")

    edits = db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = ? AND entity_id = ?",
        ("shift_hours_edit", log_id),
    )
    assert edits == 1, "the correction must leave an audited before/after pair"
    rows = db_rows(
        "SELECT actor_id, before_json, after_json FROM audit_log "
        "WHERE action = ? AND entity_id = ? ORDER BY id DESC LIMIT 1",
        ("shift_hours_edit", log_id),
    )
    assert rows, "the audit event vanished between the count and the read"
    _actor_id, before_json, after_json = rows[0]
    assert _actor_id == ADMIN, "the edit is attributed to the administrator who made it"
    assert '"hours": 9.5' in str(before_json), before_json
    assert '"hours": 8.0' in str(after_json), after_json


def test_a_shift_awaiting_a_decision_is_refused(client):
    """The edit must not stand in for the decision the Approvals queue exists to record."""
    log_id = _plant_closed_shift(paid_hours=9.5, status_code="pending_review")

    response = _edit(client, log_id, 8.0)
    assert response.status_code == 400, response.text
    assert "review" in response.json()["detail"].lower(), response.text


def test_a_clock_in_row_is_refused(client):
    """Only a clock-out row carries a shift's hours; a clock-in has nothing to correct."""
    with main.db(write=True) as conn:
        log_id = main._insert_log(
            conn,
            worker_id=WORKER,
            site_name=SITE,
            action=main.ACTION_CLOCK_IN,
            timestamp=datetime.now().strftime(TS),
            hours=0.0,
            score=1.0,
            status="approved",
            status_code="approved",
            source="online",
        )

    response = _edit(client, int(log_id), 8.0)
    assert response.status_code == 400, response.text
    assert "clock-out" in response.json()["detail"], response.text


def test_an_hours_figure_that_cannot_be_meant_is_refused_and_writes_nothing(client):
    log_id = _plant_closed_shift(paid_hours=9.5)
    before_hours = db_scalar("SELECT hours FROM attendance_logs WHERE id = ?", (log_id,))

    response = _edit(client, log_id, overtime.MAX_AUTHORISED_HOURS + 1)
    assert response.status_code == 400, response.text
    assert "between 0 and" in response.json()["detail"], response.text

    after_hours = db_scalar("SELECT hours FROM attendance_logs WHERE id = ?", (log_id,))
    assert after_hours == before_hours, "the refused edit changed the row"


def test_a_negative_correction_is_refused(client):
    log_id = _plant_closed_shift(paid_hours=9.5)
    response = _edit(client, log_id, -1.0)
    assert response.status_code == 400, response.text


def test_an_unknown_log_id_answers_404(client):
    response = _edit(client, 99_999_999, 8.0)
    assert response.status_code == 404, response.text


def test_only_an_administrator_may_correct_hours(client):
    log_id = _plant_closed_shift(paid_hours=9.5)
    assert client.post(
        f"/api/v1/admin/shifts/{log_id}/hours", headers=bearer(WORKER), json={"hours": 8.0}
    ).status_code == 403
    assert client.post(
        f"/api/v1/admin/shifts/{log_id}/hours", json={"hours": 8.0}
    ).status_code == 401
