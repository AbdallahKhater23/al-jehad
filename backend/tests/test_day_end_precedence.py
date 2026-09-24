"""Which rule ends a day, and how the application says so.

THE BUG THIS GUARDS
-------------------
The overtime watcher runs two rules in one pass: ``scan_auto_close`` ends a shift when its
*paid* hours reach ``regular_hours`` (8), and ``scan_overtime`` alerts when they reach
``overtime_notify_hours`` (8.1). The close deletes the session it closed, so a shift the
close has ended can never be observed crossing the alert line - and with the shipped
numbers it ended 0.1 h *before* the alert was due. The crossing was therefore
unobservable, silently: the watcher was running, the settings looked deliberate, and "the
overtime crossing is being watched" was true in every respect except that no crossing
could ever be seen. The requirement it implements ("tell me when a worker passes the
threshold and is still working") was unmeetable.

THE RECONCILIATION
------------------
Which rule acts is decided in one place, ``shift_hours.day_end_rules``:

* alert **below** the paid day -> the crossing is reported while the shift is open, so the
  close still owns the end of the day;
* alert **above** the paid day -> a shift closed at 8 h could never reach 8.1 h, so the
  **close stands down** (``close_defers``): the shift runs on, the crossing is reported, and
  the hours past the paid day are clocked out into overtime review. Nothing is auto-closed at
  8 h. That is what the shipped alert line (8.1 h) asks of a close, so the close itself now
  ships **off**: the same arrangement, stated honestly rather than as a switch that reads as
  *on* while nothing is ever closed;
* alert **on** the paid day -> nothing to observe ("crossed the limit and is still
  working" is false at that figure, and alerting on every full day is not a crossing), so
  the close acts and the scans report that the alert cannot fire;
* close **off** (the shipped state) -> a clock-out ends the day and the alert always fires.

So this suite pins four things:

1. **the precedence**, as a table, including the equality case;
2. **the behaviour**, through the two scans in the order the timer really calls them: the
   shipped defaults observe the crossing and leave the day to a clock-out; switching the close
   on with its alert line above the paid day stands it down; putting the alert below the paid
   day gets the close back;
3. **the reporting** - the scan summary, ``GET/POST /admin/shift_rules`` and readiness, so
   an operator is told which rule wins instead of having to read two modules to find out;
4. **the money**, for the deferral: the shift is clocked out by the worker, and the hours
   past the overtime line wait for an administrator rather than being paid automatically;
5. **what the worker is told**, which is the half of a day-end rule that lands on the person
   the rule ends the day for: the close records the standard day and owes no crossing notice
   (there are no hours past it to hold), while the deferral hands the shift back to a
   clock-out, which does announce it.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

import pytest
import harness
from harness import ADMIN, DB_PATH, MOALLEM, bearer, db_scalar

import database
import overtime
import shift_hours

TS = "%Y-%m-%d %H:%M:%S"

#: The rules a fresh deployment is born with, key for key with ``main.DEFAULT_SHIFT_RULES``.
#: The automatic close is **off**: with the alert line above the paid day the close would stand
#: down anyway, so shipping it on was shipping a switch that reads as *on* while nothing is
#: closed - and a new volume was born failing the ``overtime_close_deferred`` advisory.
#: A case about a close that acts says so, by passing ``auto_close_at_regular=1``.
SHIPPED = {
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    "auto_close_at_regular": 0,
}


def _rules(**overrides) -> dict:
    """The shipped rules with whatever the case is about replaced."""
    values = dict(SHIPPED)
    values.update(overrides)
    return values


def _plant_open_session(worker_id: str, hours_ago: float) -> str:
    """Open a shift that started ``hours_ago`` hours in the past.

    Written straight into the database because the API has no way to backdate a clock-in -
    which is the correct design, and exactly why aging a session for a test bypasses it.
    """
    clock_in_time = (datetime.now() - timedelta(hours=hours_ago)).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH))
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


def _set_rules(client, **overrides) -> dict:
    """Apply rules through the API, which is the path an operator takes."""
    response = client.post(
        "/api/v1/admin/shift_rules", headers=bearer(ADMIN), json=_rules(**overrides)
    )
    assert response.status_code == 200, response.text[:300]
    return response.json()["rules"]


def _open_shift_count(worker_id: str = MOALLEM) -> int:
    return db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (worker_id,))


# ---------------------------------------------------------------------------
# 1. the precedence, as a table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "notify,close_on,reachable,defers,close_at,ended_by",
    (
        # The alert sits below the paid day: it fires while the shift is open, and the
        # close still ends the standard day. The two rules cooperate.
        (7.5, True, True, False, 8.0, shift_hours.DAY_ENDED_BY_CLOSE),
        (0.5, True, True, False, 8.0, shift_hours.DAY_ENDED_BY_CLOSE),
        # The close switched on with its alert line above the paid day: it would end the day
        # before the alert could fire, so it stands down and the overtime workflow owns the
        # end of the day.
        (8.1, True, True, True, None, shift_hours.DAY_ENDED_BY_OVERTIME),
        (8.03, True, True, True, None, shift_hours.DAY_ENDED_BY_OVERTIME),
        (12.0, True, True, True, None, shift_hours.DAY_ENDED_BY_OVERTIME),
        # Exactly on the paid day: there is nothing to observe, so the close acts and the
        # verdict says the alert cannot fire.
        (8.0, True, False, False, 8.0, shift_hours.DAY_ENDED_BY_CLOSE),
        # No close: nothing else ends the day, so the alert always gets its turn.
        (8.1, False, True, False, None, shift_hours.DAY_ENDED_BY_HUMAN),
        (12.0, False, True, False, None, shift_hours.DAY_ENDED_BY_HUMAN),
    ),
)
def test_the_resolver_says_who_ends_the_day(notify, close_on, reachable, defers, close_at, ended_by):
    day_end = shift_hours.day_end_rules(
        _rules(overtime_notify_hours=notify, auto_close_at_regular=1 if close_on else 0)
    )
    assert day_end["alert_reachable"] is reachable, day_end["detail"]
    assert day_end["close_defers"] is defers, day_end["detail"]
    assert day_end["close_at_paid_hours"] == close_at, day_end["detail"]
    assert day_end["day_ended_by"] == ended_by, day_end["detail"]
    assert day_end["notify_hours"] == notify
    assert day_end["regular_hours"] == 8.0


def test_the_detail_names_both_numbers_and_the_way_out():
    """The sentence is the fix, not just the diagnosis: it names both settings to change."""
    # The deferral's sentence: the close on, its alert line above the paid day.
    deferred = shift_hours.day_end_rules(_rules(auto_close_at_regular=1))["detail"]
    assert "8.1" in deferred and "8" in deferred, deferred
    assert "stand" in deferred.lower() and "below" in deferred.lower(), deferred

    on_the_line = shift_hours.day_end_rules(
        _rules(overtime_notify_hours=8.0, auto_close_at_regular=1)
    )["detail"]
    assert "8" in on_the_line, on_the_line
    assert "strictly" in on_the_line.lower(), on_the_line


def test_a_deferral_is_not_an_unreachable_alert():
    """They are different states and must not be reported as each other.

    Under the deferral the crossing *is* observed - that is the whole point of standing the
    close down. Only the equality case leaves nothing to see. The shipped pair adds a third: the
    close is off, so it has not stood down to anything, and the alert is reachable because a
    clock-out is what ends the day.
    """
    deferring = shift_hours.day_end_rules(_rules(auto_close_at_regular=1))
    assert deferring["close_defers"] is True
    assert deferring["alert_reachable"] is True

    on_the_line = shift_hours.day_end_rules(
        _rules(overtime_notify_hours=8.0, auto_close_at_regular=1)
    )
    assert on_the_line["close_defers"] is False
    assert on_the_line["alert_reachable"] is False

    shipped = shift_hours.day_end_rules(_rules())
    assert shipped["close_defers"] is False
    assert shipped["alert_reachable"] is True


# ---------------------------------------------------------------------------
# 2. the behaviour, through the scans in the order the timer calls them
# ---------------------------------------------------------------------------
def test_the_close_stands_down_when_its_alert_line_sits_above_the_paid_day(client):
    """The reconciliation, at the level of what actually happens to a shift.

    Nine paid hours is well past both lines. Before this change the close ended the day at
    the 8 h boundary and the crossing was never reported; with the close switched on and the
    alert line above it, the close stands down, the alert fires while the worker is still on
    site, and the shift is still open afterwards - the alert observes a shift, it does not end
    one.

    The close ships *off*, which is the shipped pair's own reconciliation (see
    ``test_the_shipped_defaults_leave_the_day_to_a_clock_out`` below), so this is the case an
    operator lands on by switching it back on without moving the alert line.
    """
    _set_rules(client, auto_close_at_regular=1)  # the close on: 8.1 above 8.0
    _plant_open_session(MOALLEM, 9.5)

    closed = overtime.scan_auto_close()
    assert closed["enabled"] is True, "the switch is on"
    assert closed["deferred"] is True, closed
    assert closed["closed"] == 0, "the close must not end a day it would hide the crossing of"
    assert "stand" in closed["deferred_reason"].lower(), closed["deferred_reason"]
    assert _open_shift_count() == 1, "the shift is still open for the overtime workflow"

    alerted = overtime.scan_overtime()
    assert alerted["notified"] == 1, alerted
    assert alerted["alert_reachable"] is True
    assert alerted["day_end"]["close_defers"] is True
    # The crossing is an item in the Approvals queue now, not an administrator notification:
    # it is a question about a shift that is still running, and reading it never answered it.
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'overtime_exceeded' AND worker_id = ?",
        (MOALLEM,),
    ) == 0, "the crossing is still a notification"
    # Nothing else will end this shift, and the cost of leaving it is not obvious: an open
    # shift refuses the worker's *next* clock-in. Whichever surface asks the operator has to
    # say so, and the field that says it is the queue's own ``close_defers``.
    item = next(
        row for row in overtime.open_crossings() if row["worker_id"] == MOALLEM
    )
    assert item["close_defers"] is True, item

    # A deferral is not a licence to forget the day entirely: the shift ends when the worker
    # says so, and the hours past the line wait for an administrator.
    response = harness.clock_in(
        client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM)
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["hours"] == pytest.approx(9.0, abs=0.05)
    row = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()["rows"][0]
    assert row["status_code"] == "pending_overtime", row
    assert row["awaiting_approval"] is True


def test_the_close_still_ends_the_day_when_the_alert_is_below_it(client):
    """Both rules on, alert first, close second: the pair working as intended."""
    harness.use_auto_close(client)  # 7.5 h, below the 8 h paid day
    _plant_open_session(MOALLEM, 9.5)

    alerted = overtime.scan_overtime()
    assert alerted["notified"] == 1, alerted
    assert alerted["alert_reachable"] is True
    assert alerted["day_end"]["close_defers"] is False
    assert _open_shift_count() == 1, "the alert observes; it does not close"
    # The close is still coming for this shift, so the queue must not warn about a lockout that
    # cannot happen - the same statement, read off the surface that now carries it.
    item = next(row for row in overtime.open_crossings() if row["worker_id"] == MOALLEM)
    assert item["close_defers"] is False, item

    closed = overtime.scan_auto_close()
    assert closed["deferred"] is False
    assert closed["closed"] == 1, closed
    assert _open_shift_count() == 0
    # ... and the day it wrote is the standard day, not the 9 hours on the clock.
    assert db_scalar(
        "SELECT hours FROM attendance_logs WHERE worker_id = ? AND status_code = 'auto_closed_8h'",
        (MOALLEM,),
    ) == pytest.approx(8.0, abs=0.01)


def test_the_close_owes_the_worker_no_crossing_notice(client):
    """The worker's half of the reconciliation, on the one path that ends a shift by itself.

    Every path a *person* ends a shift through announces the crossing where it makes the
    overtime decision (see ``overtime.announce_crossing``), and the deferral above leaves the
    end of the day to exactly such a path. The close is the other candidate: it also ends a
    shift, unattended - and it must not write that notice, because it records exactly the
    paid day, so there are no hours past it to hold. A crossing notice here would tell a
    worker their extra hours need approval on a day with no extra hours on it, and the
    figure in it would contradict the timesheet.

    This is asserted rather than assumed because the tidier-looking change - "the close ends
    shifts too, have it announce as well" - is exactly the one that gets made by accident.
    Pinned as *no crossing notice*: whether the worker should be told the shift was closed
    automatically is a separate question, and it is answered in the close's own channel
    (``shift_auto_closed``, asserted below) rather than by re-using this sentence.
    """
    harness.use_auto_close(client)          # 7.5 h line: the alert fires first, the close acts
    _plant_open_session(MOALLEM, 9.5)

    closed = overtime.scan_auto_close()
    assert closed["deferred"] is False and closed["closed"] == 1, closed
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'shift_auto_closed' AND worker_id = ?",
        (MOALLEM,),
    ) == 1, "the close's own alert belongs to the administrator, and it is written"

    # The day it recorded is the standard day, with nothing past it: there is no overtime
    # for a crossing notice to be about.
    row = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()["rows"][0]
    assert row["status_code"] == "auto_closed_8h", row
    assert row["hours"] == pytest.approx(8.0, abs=0.01)
    assert row["recorded_hours"] == pytest.approx(8.0, abs=0.01), (
        f"nothing was recorded past the paid day: {row}"
    )
    assert db_scalar(
        "SELECT COALESCE(SUM(overtime_hours), 0.0) FROM attendance_logs WHERE worker_id = ?",
        (MOALLEM,),
    ) == 0.0, "and nothing was held as overtime either - which is what the notice would claim"

    inbox = client.get(
        "/api/v1/worker/me/notifications", headers=bearer(MOALLEM)
    ).json()["notifications"]
    assert [row for row in inbox if row["kind"] == "overtime_crossed"] == [], (
        f"a close at the paid limit owes no crossing notice: {inbox}"
    )
    # The worker is not left in the dark, though - the close writes its own notice, naming the
    # day it recorded rather than hours it held back.
    assert [row for row in inbox if row["kind"] == "shift_auto_closed"], (
        f"the close still tells the worker their day was ended for them: {inbox}"
    )


def test_an_alert_line_on_the_paid_day_cannot_be_observed(client):
    """The one genuinely unreachable case: the close acts, and the scan says why."""
    _set_rules(client, overtime_notify_hours=8.0, auto_close_at_regular=1)
    _plant_open_session(MOALLEM, 9.5)

    closed = overtime.scan_auto_close()
    assert closed["deferred"] is False
    assert closed["closed"] == 1, closed

    alerted = overtime.scan_overtime()
    assert alerted["notified"] == 0, "there is nothing left to alert about"
    assert alerted["notifications"] == []
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'overtime_exceeded'"
    ) == 0, "no alert may claim a crossing that was never observed"
    assert alerted["alert_reachable"] is False, "and the scan says so rather than looking quiet"


def test_the_shipped_defaults_leave_the_day_to_a_clock_out(client):
    """Out of the box, and the arrangement a fresh volume is born in.

    The close ships *off*, which is the honest form of what the alert line already asked for:
    with 8.1 above the paid day the close would stand down anyway, so the switch was shipped as
    *on* while closing nothing. What ends a day here is the worker's clock-out - and the hours
    past the alert line then wait for an administrator rather than being paid automatically.
    """
    stored = _set_rules(client)["day_end"]  # exactly the shipped rules
    assert stored["auto_close"] is False, stored
    assert stored["close_defers"] is False, stored
    assert stored["close_at_paid_hours"] is None, stored
    assert stored["day_ended_by"] == shift_hours.DAY_ENDED_BY_HUMAN, stored
    assert stored["alert_reachable"] is True, stored

    _plant_open_session(MOALLEM, 9.5)
    closed = overtime.scan_auto_close()
    assert closed["enabled"] is False, "the close is off"
    assert closed["closed"] == 0 and closed["deferred"] is False

    alerted = overtime.scan_overtime()
    assert alerted["notified"] == 1, alerted
    assert alerted["day_end"]["day_ended_by"] == shift_hours.DAY_ENDED_BY_HUMAN
    assert _open_shift_count() == 1, (
        "the shift is still open - the alert observes it, it does not end it"
    )
    item = next(row for row in overtime.open_crossings() if row["worker_id"] == MOALLEM)
    assert item["close_defers"] is False, "and no close is coming for it either"

    # A clock-out ends the day: the standard day is paid and the hours past the line wait for an
    # administrator - the same end of the day the deferral produces, reached by a person instead
    # of by a rule standing aside.
    response = harness.clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:300]
    assert response.json()["hours"] == pytest.approx(9.0, abs=0.05)
    row = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()["rows"][0]
    assert row["status_code"] == "pending_overtime", row
    assert row["awaiting_approval"] is True, row


def test_a_shift_that_survives_past_the_line_is_still_reported(client):
    """The alert is not skipped when it is unreachable: a real crossing is a real crossing.

    Reachability is about a shift that has yet to cross, so a session already past both
    lines is still reported rather than silently left alone.
    """
    _set_rules(client, overtime_notify_hours=7.5, auto_close_at_regular=1)
    _plant_open_session(MOALLEM, 9.0)
    _set_rules(client, overtime_notify_hours=8.0, auto_close_at_regular=1)  # the line on the paid day
    alerted = overtime.scan_overtime()
    assert alerted["notified"] == 1, alerted
    assert alerted["alert_reachable"] is False, "with these rules no new crossing could happen"


# ---------------------------------------------------------------------------
# 3. the reporting: the summary, the console and readiness
# ---------------------------------------------------------------------------
def test_the_scan_summary_carries_the_verdict(client):
    _set_rules(client, auto_close_at_regular=1)
    summary = overtime.scan_overtime()
    assert summary["alert_reachable"] is True
    day_end = summary["day_end"]
    assert day_end["close_at_paid_hours"] is None
    assert day_end["notify_hours"] == 8.1
    assert day_end["close_defers"] is True
    assert day_end["day_ended_by"] == shift_hours.DAY_ENDED_BY_OVERTIME


def test_the_console_can_see_which_rule_wins(client):
    """``day_end`` travels with the rules, so the panel can warn where they are typed."""
    stored = _set_rules(client, auto_close_at_regular=1)["day_end"]
    assert stored["close_defers"] is True
    assert stored["regular_hours"] == 8.0
    assert stored["notify_hours"] == pytest.approx(8.1)

    read = client.get("/api/v1/admin/shift_rules", headers=bearer(ADMIN)).json()
    assert read["day_end"]["close_defers"] is True
    # The rules themselves are unchanged and still there: ``day_end`` is a verdict about
    # them, not a setting.
    assert read["regular_hours"] == 8.0
    assert float(read["overtime_notify_hours"]) == pytest.approx(8.1)

    # And it follows the settings, so an operator who fixes the pair sees it fixed.
    fixed = _set_rules(client, overtime_notify_hours=7.5, auto_close_at_regular=1)["day_end"]
    assert fixed["close_defers"] is False
    assert fixed["close_at_paid_hours"] == 8.0
    assert fixed["day_ended_by"] == shift_hours.DAY_ENDED_BY_CLOSE


def test_readiness_reports_the_deferral_and_the_unreachable_alert(client):
    """The advisories, and never a repair: the settings are the operator's.

    Three states, in the order an operator meets them. The shipped pair first, because it is the
    one a deployment boots on: both advisories have to pass on a fresh volume, and that is what
    shipping the close off bought - the pair these checks were written about (the close on, its
    own alert line above it) used to *be* the shipped pair, so every new deployment was born
    reporting ``overtime_close_deferred``. Then the deferral, which is now a setting an operator
    reaches by switching the close back on; then the alert line exactly on the paid day, where
    there is nothing to observe at all.
    """
    import readiness

    def check_for(name):
        checks, _ = readiness.run_checks()
        return next(check for check in checks if check.name == name)

    # The shipped pair: nothing contradicts anything, so nothing is reported. This is the state
    # every fresh volume starts in, and it is green.
    _set_rules(client)
    fresh = check_for("overtime_close_deferred")
    assert fresh.tier == readiness.TIER_ADVISORY
    assert fresh.ok is True, fresh.detail
    # ``None`` with ``close_defers`` false is the close being *off* rather than standing down:
    # the deferral carries its own flag, and the two are reported as each other by nothing.
    assert fresh.value["close_defers"] is False, fresh.value
    assert fresh.value["close_at_paid_hours"] is None, fresh.value
    assert fresh.value["day_ended_by"] == shift_hours.DAY_ENDED_BY_HUMAN, fresh.value
    assert check_for("overtime_alert_reachable").ok is True

    # An operator who switches the close back on under the shipped alert line lands on the
    # deferral: the crossing is observable (the close stands down), but the switch that reads as
    # "on" is no longer closing anything, which is worth saying out loud.
    _set_rules(client, auto_close_at_regular=1)
    assert check_for("overtime_alert_reachable").ok is True
    deferred = check_for("overtime_close_deferred")
    assert deferred.tier == readiness.TIER_ADVISORY
    assert deferred.ok is False, deferred.detail
    assert deferred.value["close_defers"] is True
    assert "stand" in deferred.detail.lower(), deferred.detail

    # Putting the line below the paid day brings the close back...
    _set_rules(client, overtime_notify_hours=7.5, auto_close_at_regular=1)
    assert check_for("overtime_close_deferred").ok is True
    assert check_for("overtime_close_deferred").value["close_at_paid_hours"] == 8.0
    assert check_for("overtime_alert_reachable").ok is True

    # ... and putting it exactly on the paid day leaves nothing to observe, whichever way
    # the operator meant it.
    _set_rules(client, overtime_notify_hours=8.0, auto_close_at_regular=1)
    assert check_for("overtime_alert_reachable").ok is False
    assert check_for("overtime_close_deferred").ok is True


# ---------------------------------------------------------------------------
# 6. the reader itself
# ---------------------------------------------------------------------------
def test_the_rules_reader_owns_its_connection_the_way_the_database_hands_one_out(
    client, caplog, monkeypatch
):
    """``overtime.rules()`` with no connection is the startup path, and it used to fail there, quietly.

    ``database.db()`` is a **context manager**, not a connection: it opens, commits or rolls back and
    closes. The reader treated what it returned as a connection and closed it in a ``finally``, which
    raised ``AttributeError: '_GeneratorContextManager' object has no attribute 'close'``. Its only
    no-connection caller is ``start_watcher``, inside the ``except`` that exists so the timer starts
    regardless - so every startup logged one warning line and nothing else, and the watcher went on to
    announce the *shipped* precedence while the deployment's settings said something else. A rule
    reader that cannot read the rules is the one failure in this module that must not be quiet.

    Asserted from both ends, because either half alone would pass over the bug: the reader returns the
    **stored** row rather than the shipped default, and the startup announcement carries the stored
    number rather than the shipped one.
    """
    _set_rules(client, overtime_notify_hours=7.5)  # deliberately not the shipped 8.1

    # 1. the reader, standing on its own - this is the call that raised
    stored = overtime.rules()
    assert stored["overtime_notify_hours"] == pytest.approx(7.5), "the stored row wins"
    assert stored["regular_hours"] == pytest.approx(8.0), (
        "columns the row leaves alone keep the shipped default"
    )

    # 2. a connection the caller owns stays the caller's: both scans read the rules in the middle of
    #    their own transaction, and a reader that closed their handle would end it under them.
    with database.db() as conn:
        assert overtime.rules(conn)["overtime_notify_hours"] == pytest.approx(7.5)
        conn.execute("SELECT 1 FROM shift_rules").fetchone()

    # 3. the startup announcement, which is where the warning appeared on every boot. The scans are
    #    stubbed out: the pass is another suite's subject, and a real sweep here would make this test
    #    about the clock rather than about the reader.
    monkeypatch.setattr(overtime, "scan_auto_close", lambda: {"closed": 0, "deferred": False})
    monkeypatch.setattr(overtime, "scan_overtime", lambda: {"notified": 0})
    caplog.set_level(logging.INFO, logger="attendance.overtime")
    caplog.clear()
    try:
        assert overtime.start_watcher(interval=3600, enabled=True) is True
    finally:
        overtime.stop_watcher(timeout=10)

    messages = [record.getMessage() for record in caplog.records]
    assert not any("could not read the day-end rules" in message for message in messages), messages
    assert any("7.5" in message for message in messages), (
        "the watcher announces the deployment's stored numbers: " + repr(messages)
    )
    assert not any("8.1" in message for message in messages), (
        "the shipped default leaked into the announcement: " + repr(messages)
    )
