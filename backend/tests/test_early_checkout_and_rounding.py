"""The last few minutes of a shift, and the question a short clock-out asks.

WHAT THIS FILE PINS
-------------------
Two halves of one policy, both of them ways to pay somebody the wrong amount if they are
got wrong:

1. **A shift within 15 minutes of the paid day is recorded as the day.** Somebody who
   worked 7.9 h of an 8 h day gets the 8. The rounding is applied by ``recorded_shift``,
   which the five *writing* paths ask, and **not** by ``paid_hours``, which the auto-close
   gate and the overtime watcher compare against - rounding there would close a shift a
   quarter of an hour early and write a clock-out at an instant that had not happened.
   That separation is the whole point of the feature, so it is tested directly.
2. **A shift further short is questioned, not silently recorded.** The server refuses
   before it writes a row or deletes the open shift, and the phone turns that refusal into
   two buttons. A cancel must therefore leave the shift exactly as it was - the test for
   that is not that the response was 409, but that nothing in the database moved.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import harness
import overtime
import shift_hours
from harness import MOALLEM, bearer, db_scalar

TS = "%Y-%m-%d %H:%M:%S"
RULES = {
    "regular_hours": 8.0,
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    "auto_close_at_regular": 1,
}


def plant_open_session(worker_id: str, hours_ago: float) -> str:
    """Open a shift that started ``hours_ago`` hours in the past, straight in the DB."""
    clock_in_time = (datetime.now() - timedelta(hours=hours_ago)).strftime(TS)
    conn = sqlite3.connect(str(harness.DB_PATH))
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, "Downtown Tower A", clock_in_time),
        )
        conn.commit()
    finally:
        conn.close()
    return clock_in_time


def clock_out(client, worker_id=MOALLEM, confirmed=False):
    return harness.clock_in(
        client, worker_id, action="Clock Out", headers=bearer(worker_id), confirmed=confirmed
    )


def open_shift_count(worker_id: str = MOALLEM) -> int:
    return db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (worker_id,))


def clock_out_rows(worker_id: str = MOALLEM) -> int:
    return db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'",
        (worker_id,),
    )


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------
def test_the_pure_function_does_not_round():
    """``paid_hours`` stays the real hours: decisions are made on what was worked.

    This is the guard the whole design rests on. 8.4 h on site less the half-hour break is
    7.9 h of work, and that is what the auto-close and the overtime watcher must see.
    """
    paid, taken = shift_hours.paid_hours(8.4, RULES)
    assert paid == pytest.approx(7.9, abs=1e-6)
    assert taken == pytest.approx(0.5, abs=1e-6)


@pytest.mark.parametrize(
    ("elapsed", "paid", "rounded"),
    [
        (8.4, 8.0, True),     # 7.9 h worked: eight minutes short of the day
        (8.25, 8.0, True),    # 7.75 h worked - exactly the window, and included
        (8.24, 7.74, False),  # 7.74 h worked - one hundredth outside it
        (8.0, 7.5, False),    # half an hour short is not a rounding error
        (9.5, 9.0, False),    # past the day: rounding never reaches up to touch this
        (2.0, 2.0, False),    # too short to have contained a break, and far from the day
    ],
)
def test_a_shift_within_the_window_is_recorded_as_the_full_day(elapsed, paid, rounded):
    record = shift_hours.recorded_shift(elapsed, RULES)
    assert record["paid_hours"] == pytest.approx(paid, abs=1e-6), record
    assert record["rounded_up"] is rounded
    assert record["needs_confirmation"] is (not rounded and paid < 8.0)


def test_the_rounding_can_be_turned_off():
    """Zero is a real setting - an operator who does not want the grace period gets none."""
    off = {**RULES, "round_up_within_hours": 0}
    record = shift_hours.recorded_shift(8.4, off)
    assert record["paid_hours"] == pytest.approx(7.9, abs=1e-6)
    assert record["rounded_up"] is False
    assert record["needs_confirmation"] is True


def test_the_written_arithmetic_says_why_the_number_is_not_the_sum():
    """The equation keeps the real hours; the rounding is explained beside it.

    Showing the rounded figure in the equation would read as arithmetic that does not add
    up, which is the opposite of the point of writing it out.
    """
    record = shift_hours.recorded_shift(8.4, RULES)
    said = record["description"]
    assert "8.40h on site" in said and "7.90h payable" in said
    assert "rounded up to the full 8.00h day" in said


def test_the_short_hours_are_reported_for_the_question():
    record = shift_hours.recorded_shift(7.7, RULES)
    assert record["paid_hours"] == pytest.approx(7.2, abs=1e-6)
    assert record["short_hours"] == pytest.approx(0.8, abs=1e-6)
    assert record["needs_confirmation"] is True


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------
def test_a_near_full_shift_is_recorded_as_the_day_without_a_question(client):
    plant_open_session(MOALLEM, 8.4)

    response = clock_out(client)
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["hours"] == pytest.approx(8.0, abs=0.02)
    assert body["break_hours"] == pytest.approx(0.5, abs=0.01)
    assert "rounded up to the full 8.00h day" in body["message"], body["message"]
    assert db_scalar(
        "SELECT hours FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'", (MOALLEM,)
    ) == pytest.approx(8.0, abs=0.02)


# ---------------------------------------------------------------------------
# the question
# ---------------------------------------------------------------------------
def test_a_short_clock_out_is_questioned_and_writes_nothing(client):
    plant_open_session(MOALLEM, 7.7)

    refused = clock_out(client)
    assert refused.status_code == 409, refused.text[:300]
    detail = refused.json()["detail"]
    assert detail["error_code"] == "confirm_early_checkout"
    # The numbers the phone renders its own sentence from, in the reader's language.
    assert detail["paid_hours"] == pytest.approx(7.2, abs=0.02)
    assert detail["regular_hours"] == 8.0
    assert detail["short_hours"] == pytest.approx(0.8, abs=0.02)

    # The refusal is the test that matters: a question must not close the shift, book the
    # hours, or leave a row behind - a worker who cancels has lost nothing.
    assert open_shift_count() == 1, "the question must leave the shift open"
    assert clock_out_rows() == 0, "nothing may be recorded before the worker answers"


def test_confirming_records_the_short_shift(client):
    plant_open_session(MOALLEM, 7.7)

    response = clock_out(client, confirmed=True)
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["hours"] == pytest.approx(7.2, abs=0.02)
    assert open_shift_count() == 0
    assert clock_out_rows() == 1
    assert db_scalar(
        "SELECT hours FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'", (MOALLEM,)
    ) == pytest.approx(7.2, abs=0.02)


def test_the_question_is_asked_of_the_whole_shift_not_the_attempt(client):
    """Two refusals in a row are still one open shift and no rows."""
    plant_open_session(MOALLEM, 7.7)

    for _ in range(2):
        assert clock_out(client).status_code == 409
    assert open_shift_count() == 1
    assert clock_out_rows() == 0

    assert clock_out(client, confirmed=True).status_code == 200
    assert open_shift_count() == 0
    assert clock_out_rows() == 1


def test_a_shift_already_closed_by_the_timer_is_not_questioned(client):
    """The auto-close answers first: there is nothing to clock out of, and it says so."""
    harness.use_auto_close(client)
    plant_open_session(MOALLEM, 12.0)
    overtime.scan_auto_close()

    response = clock_out(client)
    assert response.status_code == 400, response.text[:300]
    assert "closed automatically" in response.json()["detail"]


# ---------------------------------------------------------------------------
# the separation the design rests on
# ---------------------------------------------------------------------------
def test_the_timer_does_not_close_a_shift_a_quarter_of_an_hour_early(client):
    """8.4 h on site rounds up to 8.0 h *recorded* - and is still not at the limit.

    This is the regression the split between ``paid_hours`` and ``recorded_shift`` exists
    to prevent: if the auto-close gate read the rounded figure, this shift would look
    finished and be closed at a boundary 6 minutes in the future.
    """
    harness.use_auto_close(client)
    plant_open_session(MOALLEM, 8.4)

    summary = overtime.scan_auto_close()
    assert summary["closed"] == 0, "a short shift must not be auto-closed by its rounding"
    assert open_shift_count() == 1
    assert clock_out_rows() == 0
