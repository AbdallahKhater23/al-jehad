"""The unpaid break, and the end of the paid day.

WHAT THIS FILE PINS
-------------------
A full day is **8 h of paid work plus a 30-minute break that is not paid** - 8.5 h on
site - and a shift ends when those 8 paid hours are up. That policy is small, and every
part of it is a way to pay somebody the wrong amount:

1. **The arithmetic, in one place.** ``shift_hours.paid_hours`` is asked by the online
   clock-out, the admin force-clock-out, the offline punch that materializes later, the
   auto-close and the reports. Two of those disagreeing is how a shift comes to be worth
   different money depending on which button closed it, so the pure function is tested
   directly, at the edges: a 40-minute shift takes no break, a 13 h shift is not capped,
   the deduction never makes hours negative, and turning the break off really turns it off.
2. **The break is recorded, not just subtracted.** ``attendance_logs.break_hours`` and the
   reports' ``unpaid_break_hours`` are what turn "why does my 8.5 h day read 8.0?" into a
   question with an answer.
3. **The close happens at the boundary, not when the timer noticed.** A server that was
   down for the afternoon must record the shift as ending at 8.5 h, not at the moment the
   watcher next ran.
4. **Nothing is closed silently.** The alert names what was closed, when, and - when the
   shift was still open well past its limit - how much time past it is *not* recorded.
5. **The worker is told, not left with an error.** The commonest reason a clock-out finds
   no open shift is now the policy itself, so it says so instead of "clock in first".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import harness
import overtime
import shift_hours
from harness import ADMIN, MOALLEM, bearer, db_scalar

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


def clock_out(client, worker_id=MOALLEM, action="Clock Out"):
    return harness.clock_in(
        client, worker_id, action=action, headers=bearer(worker_id)
    )


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("elapsed", "paid", "taken"),
    [
        (8.5, 8.0, 0.5),      # a full day: 8 paid plus the unpaid half hour
        (8.0, 7.5, 0.5),      # eight hours on site is not eight hours of work
        (9.5, 9.0, 0.5),      # long, and still not capped
        (13.0, 12.5, 0.5),    # 13 h on site: the real shift, less the break
        (4.0, 3.5, 0.5),      # the first shift long enough to have contained a break
        (3.99, 3.99, 0.0),    # one minute short: no break was taken, none is charged
        (0.75, 0.75, 0.0),    # sent home early, or a mis-tap
        (0.0, 0.0, 0.0),
    ],
)
def test_paid_hours_are_the_elapsed_time_less_one_unpaid_break(elapsed, paid, taken):
    assert shift_hours.paid_hours(elapsed, RULES) == (pytest.approx(paid, abs=1e-4), pytest.approx(taken, abs=1e-4))


def test_the_break_can_be_turned_off_or_charged_on_every_shift():
    off = {**RULES, "break_minutes": 0}
    assert shift_hours.paid_hours(8.5, off) == (pytest.approx(8.5), 0.0)

    always = {**RULES, "break_after_hours": 0}
    # Charged even to a shift that plainly could not have contained one - which is what
    # an operator asking for that behaviour is asking for.
    assert shift_hours.paid_hours(0.5, always) == (pytest.approx(0.0), pytest.approx(0.5))
    # And still never below zero.
    assert shift_hours.paid_hours(0.0, always) == (0.0, 0.0)


def test_a_long_break_cannot_pay_negative_hours():
    strange = {**RULES, "break_minutes": 600.0, "break_after_hours": 0.0}
    paid, taken = shift_hours.paid_hours(2.0, strange)
    assert paid == 0.0 and taken == pytest.approx(2.0, abs=1e-4)


def test_the_paid_limit_is_the_moment_the_eighth_hour_is_worked():
    clock_in = datetime(2026, 9, 15, 5, 0, 0)
    # Clocking in at 05:00, the paid day runs out at 13:30: eight hours worked and the
    # half-hour break in the middle of them.
    assert shift_hours.closes_at(clock_in, RULES) == datetime(2026, 9, 15, 13, 30, 0)

    # With no break charged, the same day ends at 13:00 - and the function must notice
    # that rather than always adding 30 minutes.
    assert shift_hours.closes_at(clock_in, {**RULES, "break_minutes": 0}) == datetime(2026, 9, 15, 13, 0, 0)


def test_auto_close_reads_its_switch_defensively():
    """An unreadable setting must not quietly turn a policy off."""
    assert shift_hours.auto_close_enabled(RULES) is True
    assert shift_hours.auto_close_enabled({**RULES, "auto_close_at_regular": 0}) is False
    assert shift_hours.auto_close_enabled({**RULES, "auto_close_at_regular": "0"}) is False
    assert shift_hours.auto_close_enabled({**RULES, "auto_close_at_regular": "false"}) is False
    assert shift_hours.auto_close_enabled({**RULES, "auto_close_at_regular": None}) is True
    assert shift_hours.auto_close_enabled({}) is True


def test_the_arithmetic_is_written_out_for_the_person_reading_it():
    paid, taken = shift_hours.paid_hours(8.5, RULES)
    said = shift_hours.describe(8.5, paid, taken)
    assert "8.50h on site" in said and "0.50h unpaid break" in said and "8.00h payable" in said
    assert "paid as worked" in shift_hours.describe(2.0, *shift_hours.paid_hours(2.0, RULES))


# ---------------------------------------------------------------------------
# the clock-out path
# ---------------------------------------------------------------------------
def test_a_normal_day_pays_eight_hours_and_records_the_break(client):
    plant_open_session(MOALLEM, 8.5)

    response = clock_out(client)
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["hours"] == pytest.approx(8.0, abs=0.02)
    assert body["break_hours"] == pytest.approx(0.5, abs=0.01)
    assert body["overtime_hours"] == 0, "a full day is not overtime"
    assert body["status"] == "success"
    assert "unpaid break" in body["message"], body["message"]

    log_id = db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "approved"
    assert db_scalar("SELECT break_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(0.5, abs=0.01)


def test_a_short_shift_is_paid_as_worked(client):
    """40 minutes is not a day with a break in it, so nothing is deducted."""
    plant_open_session(MOALLEM, 0.75)

    response = clock_out(client)
    assert response.status_code == 200, response.text[:300]
    assert response.json()["hours"] == pytest.approx(0.75, abs=0.02)
    assert response.json()["break_hours"] == 0
    log_id = db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT break_hours FROM attendance_logs WHERE id = ?", (log_id,)) is None, (
        "no break was taken, so the column records nothing rather than a zero"
    )


def test_the_break_can_be_switched_off_from_the_console(client):
    updated = client.post(
        "/api/v1/admin/shift_rules", headers=bearer(ADMIN), json={"break_minutes": 0}
    )
    assert updated.status_code == 200, updated.text[:200]
    assert updated.json()["rules"]["break_minutes"] == 0

    plant_open_session(MOALLEM, 8.5)
    response = clock_out(client)
    assert response.status_code == 200, response.text[:300]
    assert response.json()["hours"] == pytest.approx(8.5, abs=0.02)
    assert response.json()["break_hours"] == 0


def test_the_console_refuses_shift_rules_that_would_pay_nobody(client):
    for payload in (
        {"break_minutes": -30},
        {"break_minutes": 300},
        {"break_after_hours": -1},
        {"regular_hours": 0},
        {"regular_hours": 25},
    ):
        response = client.post("/api/v1/admin/shift_rules", headers=bearer(ADMIN), json=payload)
        assert response.status_code == 400, f"{payload} was accepted: {response.text[:200]}"

    rules = client.get("/api/v1/admin/shift_rules", headers=bearer(ADMIN)).json()
    assert rules["break_minutes"] == 30, "a refused change must not have been written"
    assert rules["regular_hours"] == 8.0


def test_the_worker_panel_is_told_where_the_paid_day_ends(client):
    """The clock panel ticks up from the clock-in time, so it needs the 8.5, not the 8."""
    stats = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM))
    assert stats.status_code == 200, stats.text[:200]
    body = stats.json()
    assert body["break_minutes"] == 30
    assert body["break_after_hours"] == 4
    assert body["paid_day_hours"] == 8.0
    assert body["on_site_day_hours"] == pytest.approx(8.5, abs=0.001)
    assert body["auto_close_at_regular"] == 1


# ---------------------------------------------------------------------------
# the automatic close
# ---------------------------------------------------------------------------
def test_a_forgotten_shift_is_closed_at_the_boundary(client):
    clock_in_time = plant_open_session(MOALLEM, 12.0)

    summary = overtime.scan_auto_close()
    assert summary["enabled"] is True
    assert summary["closed"] == 1
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0

    log_id = summary["shifts"][0]["log_id"]
    expected = (datetime.strptime(clock_in_time, TS) + timedelta(hours=8.5)).strftime(TS)
    assert db_scalar("SELECT timestamp FROM attendance_logs WHERE id = ?", (log_id,)) == expected, (
        "a shift must end when its paid hours ran out, not when the timer happened to notice"
    )
    assert db_scalar("SELECT hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(8.0, abs=0.01)
    assert db_scalar("SELECT break_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(0.5, abs=0.01)
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "auto_closed_8h"
    assert db_scalar("SELECT source FROM attendance_logs WHERE id = ?", (log_id,)) == "auto_close"
    assert db_scalar("SELECT action FROM attendance_logs WHERE id = ?", (log_id,)) == "Clock Out"

    # Nothing about it is silent: the administrator gets an alert that says how late the
    # watcher was and what that means for the hours that are not recorded.
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'shift_auto_closed' AND worker_id = ?",
        (MOALLEM,),
    ) == 1
    body = db_scalar(
        "SELECT body FROM admin_notifications WHERE kind = 'shift_auto_closed' AND worker_id = ?",
        (MOALLEM,),
    )
    assert "still open 3.50h past" in body, body
    assert "not recorded" in body

    reason = db_scalar("SELECT flag_reason FROM attendance_logs WHERE id = ?", (log_id,))
    assert reason and "still open" in reason

    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'shift_auto_closed' AND entity_id = ?", (MOALLEM,)
    ) == 1


def test_the_closed_shift_is_payable(client):
    """The eight recorded hours are work; the old 11 h rows needed a decision, this one doesn't."""
    plant_open_session(MOALLEM, 8.6)
    overtime.scan_auto_close()

    report = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    # Newest first, and the shift just closed is the newest thing on the timesheet.
    row = report["rows"][0]
    assert row["status_code"] == "auto_closed_8h"
    assert row["hours"] == pytest.approx(8.0, abs=0.01)
    assert row["recorded_hours"] == pytest.approx(8.0, abs=0.01)
    assert row["awaiting_approval"] is False, "the system closed it at the paid limit; nobody has to sign"
    assert row["break_hours"] == pytest.approx(0.5, abs=0.01)
    assert report["totals"]["approved_hours"] == pytest.approx(8.0, abs=0.01)
    assert report["totals"]["awaiting_approval_hours"] == 0


def test_a_shift_under_the_limit_is_left_alone():
    plant_open_session(MOALLEM, 7.0)

    summary = overtime.scan_auto_close()
    # ``skipped`` counts every session in the table, and the seed data opens one of its
    # own, so the assertion that matters is that this worker's shift survived untouched.
    assert summary["closed"] == 0
    assert summary["skipped"] >= 1
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'", (MOALLEM,)
    ) == 0


def test_closing_twice_is_impossible(client):
    plant_open_session(MOALLEM, 9.0)
    first = overtime.scan_auto_close()
    second = overtime.scan_auto_close()

    assert first["closed"] == 1 and second["closed"] == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'", (MOALLEM,)
    ) == 1, "the timer runs every minute; a shift must not be closed again on the next tick"
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'shift_auto_closed' AND worker_id = ?",
        (MOALLEM,),
    ) == 1


def test_an_operator_can_turn_the_close_off(client):
    """The alert is then the only thing watching a forgotten shift - which is what it is for."""
    updated = client.post(
        "/api/v1/admin/shift_rules", headers=bearer(ADMIN), json={"auto_close_at_regular": 0}
    )
    assert updated.status_code == 200, updated.text[:200]
    assert updated.json()["rules"]["auto_close_at_regular"] == 0

    plant_open_session(MOALLEM, 12.0)
    summary = overtime.scan_auto_close()
    assert summary["enabled"] is False and summary["closed"] == 0
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1

    alert = overtime.scan_overtime()
    assert alert["notified"] == 1, "with the close off, the administrator still has to be told"


def test_the_worker_who_comes_back_is_told_what_happened(client):
    """Not \"clock in first\" - the shift ended, and the answer says when and for how much."""
    plant_open_session(MOALLEM, 8.6)
    overtime.scan_auto_close()

    response = clock_out(client)
    assert response.status_code == 400, response.text[:300]
    detail = response.json()["detail"]
    assert "closed automatically" in detail, detail
    assert "8.00h paid" in detail, detail
    assert "30-minute unpaid break" in detail, detail
    assert "clocking in first" not in detail, "the old message would read as data loss"


def test_a_worker_with_no_shift_at_all_still_gets_the_plain_answer(client):
    response = clock_out(client)
    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot clock out without clocking in first."


def test_every_row_carries_the_unpaid_break_beside_the_hours(client):
    """Rows an administrator can reconcile against a door-to-door timesheet."""
    plant_open_session(MOALLEM, 8.5)
    clock_out(client)
    plant_open_session(MOALLEM, 9.5)
    clock_out(client)

    report = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    assert report["totals"]["shifts"] == 2, "one row per shift, both of them"
    newest, oldest = report["rows"][0], report["rows"][1]

    assert oldest["recorded_hours"] == pytest.approx(8.0, abs=0.05), "8.5 h on site, less the break"
    assert oldest["break_hours"] == pytest.approx(0.5, abs=0.02)
    assert oldest["awaiting_approval"] is False, "an ordinary day is nobody's decision"

    # The 9.0 h one is past the 8.1 h threshold, so it waits for approval instead - the
    # break changes what a shift is worth, not who may sign it off.
    assert newest["recorded_hours"] == pytest.approx(9.0, abs=0.05), "9.5 h on site, less the break"
    assert newest["break_hours"] == pytest.approx(0.5, abs=0.02)
    assert newest["awaiting_approval"] is True

    assert report["totals"]["break_hours"] == pytest.approx(1.0, abs=0.02), "two unpaid half hours"
    assert report["totals"]["approved_hours"] == pytest.approx(8.0, abs=0.05)
    assert report["totals"]["awaiting_approval_hours"] == pytest.approx(9.0, abs=0.05)
    # 17.0 h counted plus 1.0 h break is 18.0 h of shifts that ran 8.5 h and 9.5 h.
    assert report["totals"]["hours"] + report["totals"]["break_hours"] == pytest.approx(18.0, abs=0.05)
