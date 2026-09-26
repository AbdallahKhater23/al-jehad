"""Authorising hours ends the day - the operator's answer, not the switch.

WHY THIS EXISTS
---------------
The automatic close ships **off** and stands down whenever the alert line sits above the paid
day, because ending a shift unattended is the one thing that must not hide a crossing. But an
*answered* crossing is not in that position: an administrator has looked at the shift, named a
ceiling, and thereby said when the day ends. The operator clicking Approve is the human the
plain "the close is off" rule defers to - and expecting them to also flip
``auto_close_at_regular`` (and move the alert line) before the answer does anything is the state
this suite was written about.

So the rule pinned here is:

* an **approval** arms the close at its ceiling regardless of the switch and the deferral -
  the day ends at the ceiling, now if the hours have reached it, otherwise when they do;
* a shift with no answer still obeys the old table: with the shipped rules the close stands
  down for the deferral and is simply off otherwise, and a **refusal** is not an approval.

The two halves of the user's sentence are the two closes below: a worker still on the clock at
14 h authorised for 12 is checked out at the 12 h boundary; a worker at 9 h authorised for 12 is
left running and closed when they reach it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import harness
from database import db
from harness import HEAD_ADMIN, WORKER, bearer

import main
import migrations
import overtime
import shift_hours

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"

#: The shipped rules put the alert line (8.1) above the paid day (8.0): ``close_defers`` for the
#: ordinary close, and the switch itself ships off. Neither stops an approval.
SHIPPED = dict(migrations.DEFAULT_SHIFT_RULES)


@pytest.fixture(scope="module")
def client(app_module):
    """A client without the lifespan: no startup gate, no timers started behind the tests."""
    instance = TestClient(app_module.app)
    try:
        yield instance
    finally:
        instance.close()


def _rules(**overrides) -> dict:
    values = dict(SHIPPED)
    values.update(overrides)
    return values


def _store_rules(values: dict) -> None:
    with db(write=True) as conn:
        cursor = conn.execute(
            "UPDATE shift_rules SET regular_hours = ?, overtime_notify_hours = ?, "
            "break_minutes = ?, break_after_hours = ?, auto_close_at_regular = ? WHERE id = 1",
            (
                values["regular_hours"],
                values["overtime_notify_hours"],
                values["break_minutes"],
                values["break_after_hours"],
                values["auto_close_at_regular"],
            ),
        )
        assert cursor.rowcount == 1, "there is no shift_rules row to set"


def _plant_open_shift(worker_id: str = WORKER, *, hours_on_site: float = 9.5) -> str:
    """An open shift as the application leaves it mid-shift. Returns the stored clock-in."""
    clock_in = (datetime.now() - timedelta(hours=hours_on_site)).strftime(TS)
    with db(write=True) as conn:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute("DELETE FROM overtime_authorisations WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source) "
            "VALUES (?, ?, ?, 'online')",
            (worker_id, SITE, clock_in),
        )
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
    return clock_in


def _authorise(
    worker_id: str,
    clock_in: str,
    hours: float,
    *,
    decision: str = overtime.DECISION_AUTHORISED,
    by: str = HEAD_ADMIN,
) -> None:
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO overtime_authorisations (worker_id, clock_in_time, decision, "
            "authorised_hours, recorded_hours_at_decision, decided_by, decided_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worker_id,
                clock_in,
                decision,
                hours,
                max(0.0, hours - 2.0),
                by,
                datetime.now().strftime(TS),
                "the pour ran long",
            ),
        )


def _stored_rules() -> dict:
    with db() as conn:
        return overtime.rules(conn)


def _open_shifts(worker_id: str = WORKER) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (worker_id,)
        ).fetchone()[0]


def _last_clock_out(worker_id: str = WORKER):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM attendance_logs WHERE worker_id = ? AND action = ? "
            "ORDER BY id DESC LIMIT 1",
            (worker_id, main.ACTION_CLOCK_OUT),
        ).fetchone()


def _decision(worker_id: str = WORKER):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM overtime_authorisations WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
            (worker_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# 1. the shipped rules: the answer, not the switch, ends the day
# ---------------------------------------------------------------------------
def test_the_shipped_rules_close_an_authorised_shift_at_its_ceiling():
    """14 h on the clock, authorised for 12: checked out at the 12 h boundary, with the switch off.
    """
    clock_in = _plant_open_shift(hours_on_site=14.0)
    _authorise(WORKER, clock_in, 12.0)
    boundary = shift_hours.paid_limit_at(datetime.strptime(clock_in, TS), 12.0, _stored_rules())

    summary = overtime.scan_auto_close()

    assert summary["enabled"] is False, "the automatic close is off in the shipped rules"
    assert summary["closed"] == 1, (
        f"an approval with the close switched off ended nothing: {summary}"
    )
    row = _last_clock_out()
    assert row is not None, "the shift was reported closed but no clock-out row was written"
    assert row["hours"] == pytest.approx(12.0, abs=0.01), row
    assert row["timestamp"] == boundary.strftime(TS), (
        f"the day ended when the watcher ran rather than when the authorised hours ran out: {dict(row)}"
    )
    assert _open_shifts() == 0
    assert _decision()["consumed_by_log_id"] == row["id"], (
        "the close ended the shift on an answer and left that answer live"
    )
    with db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM worker_notifications WHERE kind = 'shift_auto_closed' "
            "AND worker_id = ?",
            (WORKER,),
        ).fetchone()[0] == 1, "the worker whose day it ended was not told"


def test_an_authorised_shift_inside_its_window_is_held_until_it_reaches_it():
    """9 h on the clock, authorised for 12: left running, then closed the moment it hits 12."""
    clock_in = _plant_open_shift(hours_on_site=9.5)
    _authorise(WORKER, clock_in, 12.0)
    boundary = shift_hours.paid_limit_at(datetime.strptime(clock_in, TS), 12.0, _stored_rules())

    early = overtime.scan_auto_close()
    assert early["closed"] == 0, "the close ended a shift before the authorised hours ran out"
    assert early["holding"] == 1, early
    assert early["authorised"][0]["authorised_hours"] == 12.0, early["authorised"]
    assert early["authorised"][0]["closes_at"] == boundary.strftime(TS), early["authorised"]
    assert _open_shifts() == 1, "the worker has no shift left to work into the authorised window"

    reached = overtime.scan_auto_close(now=boundary)
    assert reached["closed"] == 1, reached
    assert _last_clock_out()["hours"] == pytest.approx(12.0, abs=0.01), _last_clock_out()


def test_a_deferral_no_longer_stops_an_answered_shift_from_ending():
    """Close switched on under the shipped alert line: the close defers, and still ends the day
    of a shift somebody answered."""
    _store_rules(_rules(auto_close_at_regular=1))
    clock_in = _plant_open_shift(hours_on_site=14.0)
    _authorise(WORKER, clock_in, 12.0)

    summary = overtime.scan_auto_close()
    assert summary["deferred"] is True, "the ordinary close no longer stands down"
    assert summary["closed"] == 1, (
        f"the deferral stopped an approved shift from ending: {summary}"
    )
    assert _last_clock_out()["hours"] == pytest.approx(12.0, abs=0.01)


# ---------------------------------------------------------------------------
# 2. the controls: nothing else ends a day
# ---------------------------------------------------------------------------
def test_the_shipped_rules_still_leave_an_unanswered_shift_to_a_clock_out():
    """The control for the first test: no answer, no close."""
    _store_rules(_rules())
    _plant_open_shift(hours_on_site=14.0)

    summary = overtime.scan_auto_close()
    assert summary["enabled"] is False
    assert summary["closed"] == 0 and summary["deferred"] is False
    assert _open_shifts() == 1, "a shift nobody answered was closed by the watcher"


def test_a_declined_crossing_does_not_end_the_day_with_the_close_off():
    """A refusal records the paid day, which a clock-out already reaches - it is not an ending."""
    _store_rules(_rules())
    clock_in = _plant_open_shift(hours_on_site=14.0)
    _authorise(
        WORKER,
        clock_in,
        _stored_rules()["regular_hours"],
        decision=overtime.DECISION_DECLINED,
    )

    summary = overtime.scan_auto_close()
    assert summary["closed"] == 0, (
        f"declining a crossing ended a shift the operator only refused overtime for: {summary}"
    )
    assert _open_shifts() == 1


def test_a_deferral_still_stands_down_for_an_unanswered_shift():
    """The control for the deferral case: the reconciliation survives for shifts nobody answered."""
    _store_rules(_rules(auto_close_at_regular=1))
    _plant_open_shift(hours_on_site=14.0)

    summary = overtime.scan_auto_close()
    assert summary["deferred"] is True
    assert summary["closed"] == 0
    assert _open_shifts() == 1


# ---------------------------------------------------------------------------
# 3. the whole path: the queue's answer, then the watcher
# ---------------------------------------------------------------------------
def test_answering_the_queue_arms_the_checkout_the_watcher_performs(client, app_module):
    """Approve 12 for a shift at 14 h: the request records the ceiling, the watcher ends the day.

    Deliberately two steps. Answering a crossing is not itself a clock-out, and a decision that
    settled a shift by being written would make the verdict depend on which of two writes landed
    first - so the request records the answer and the watcher's next pass ends the day at it.
    """
    clock_in = _plant_open_shift(hours_on_site=14.0)
    boundary = shift_hours.paid_limit_at(datetime.strptime(clock_in, TS), 12.0, _stored_rules())

    response = client.post(
        f"/api/v1/admin/overtime/crossings/{WORKER}/accept",
        headers=bearer(HEAD_ADMIN),
        json={"authorised_hours": 12.0, "note": "the pour ran long"},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["authorised_hours"] == pytest.approx(12.0, abs=0.01)
    assert _open_shifts() == 1, "the answer checked the worker out before the watcher ran"

    summary = overtime.scan_auto_close()
    assert summary["closed"] == 1, summary
    row = _last_clock_out()
    assert row["hours"] == pytest.approx(12.0, abs=0.01), row
    assert row["timestamp"] == boundary.strftime(TS), dict(row)
