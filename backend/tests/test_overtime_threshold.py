"""The overtime line: what it counts, where it is decided, and on what granularity.

WHY THIS EXISTS
---------------
``overtime_notify_hours`` is a single number that four clock-out paths and two watcher
scans all act on, and they used to act on it differently:

* the punch path, the quick-link path and the offline materialization each resolved
  ``rules["overtime_notify_hours"]`` themselves, and the administrator's force-clock-out
  resolved ``regular_hours`` instead;
* the gate was strict (``>``) where the watcher was inclusive (``>=``), so a shift that
  stopped exactly on the line was alerted about and then auto-approved;
* two of them compared the *recorded* hours - which round a shift up to the paid day -
  against a line meant for real paid hours, so with the line set below the paid day an
  ordinary full day was held for approval with **zero** overtime on it;
* the sentence two of them wrote into ``flag_reason`` called the overtime line the
  "regular threshold";
* and nothing said whether the figure was time on site or time paid, which differ by
  exactly the unpaid break - a half hour that moves every boundary in this file.

What this suite pins:

1. **the basis** - paid hours, stated as ``shift_hours.OVERTIME_BASIS`` and enforced by
   the arithmetic, with the on-site reading shown to be the misreading it is;
2. **the granularity** - the comparison is on *seconds*, demonstrated where a rounded
   hours figure cannot tell two shifts apart but the verdict must;
3. **the one place** - every path asks ``shift_hours.overtime_assessment`` and none of
   them reads the column, checked against the source of each path;
4. **the rule** - held when *at or past the line* **and** past the regular paid day, with
   the hours held measured from the paid day;
5. **the paths agreeing**, at the second, through the real HTTP punch path and the watcher
   in the same pass;
6. **the write** - the line is range-checked like the paid day is.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import textwrap
from datetime import datetime, timedelta

import pytest
import harness
from harness import ADMIN, DB_PATH, MOALLEM, bearer, db_scalar

import main
import offline_sync
import overtime
import quick_links
import shift_hours

TS = "%Y-%m-%d %H:%M:%S"

SHIPPED = {
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    "auto_close_at_regular": 1,
}


class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now`` is fixed, so a planted shift has an exact age.

    The punch path reads the clock itself, so pinning it is what makes "one second short
    of the line" a testable request rather than a race.
    """

    FIXED = datetime(2026, 9, 12, 20, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED if tz is None else cls.FIXED.astimezone(tz)


def _rules(**overrides) -> dict:
    values = dict(SHIPPED)
    values.update(overrides)
    return values


def _break_seconds(values: dict) -> int:
    """The unpaid break in whole seconds, from the public helper rather than a constant."""
    return int(round(shift_hours.break_hours(values) * shift_hours.SECONDS_PER_HOUR))


def _on_site_at_the_line(values: dict) -> int:
    """Seconds on site that put *paid* time exactly on the overtime line."""
    line = shift_hours.overtime_rule(values)
    return line["threshold_seconds"] + _break_seconds(values)


def _plant(worker_id: str, seconds_ago: int, *, now: datetime | None = None) -> str:
    """Open a shift that started ``seconds_ago`` seconds in the past, straight in the DB.

    The API has no way to backdate a clock-in, which is correct - and why aging a session
    for a test bypasses it. ``now`` is the clock the *path under test* will read: a frozen
    test still has to plant against the same instant, or the shift is planted days into the
    future and the punch is refused for being an early clock-out.
    """
    clock_in_time = ((now or datetime.now()) - timedelta(seconds=seconds_ago)).strftime(TS)
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


def _clock_out(client, worker_id: str = MOALLEM):
    return harness.clock_in(client, worker_id, action="Clock Out", headers=bearer(worker_id))


def _last_clock_out(worker_id: str = MOALLEM) -> dict:
    """The clock-out row the path just wrote, whatever way it was closed."""
    row = harness.db_rows(
        "SELECT status_code, hours, overtime_hours, flag_reason FROM attendance_logs "
        "WHERE worker_id = ? AND action = 'Clock Out' ORDER BY id DESC LIMIT 1",
        (worker_id,),
    )
    assert row, "the path wrote no clock-out"
    return dict(zip(("status_code", "hours", "overtime_hours", "flag_reason"), row[0]))


# ---------------------------------------------------------------------------
# 1. the basis
# ---------------------------------------------------------------------------
def test_the_overtime_line_counts_paid_hours_not_time_on_site():
    """The whole file's premise, named once so a reader can find it and a test can guard it."""
    rules = _rules()
    assert shift_hours.OVERTIME_BASIS == "paid"

    # A full day: 8.5 h on site, 30 minutes of it unpaid, so 8 h paid.
    assessment = shift_hours.overtime_assessment(8 * 3600 + _break_seconds(rules), rules)
    assert assessment["basis"] == "paid"
    assert assessment["paid_seconds"] == 8 * 3600
    assert assessment["paid_hours"] == 8.0
    assert assessment["break_taken_hours"] == 0.5
    assert assessment["reaches_threshold"] is False, "a normal full day is not overtime"

    # ... and the same shift read as *on-site* hours is past the line, which is the
    # misreading the basis rules out: the two numbers differ by exactly the break.
    assert 8.5 >= assessment["threshold_hours"], "the on-site reading is what would misfire"


def test_paid_seconds_and_paid_hours_are_the_same_rule_at_two_granularities():
    rules = _rules()
    for seconds_on_site in (0, 60, 4 * 3600 - 1, 4 * 3600, 8 * 3600, 9 * 3600 + 137):
        hours_paid, hours_break = shift_hours.paid_hours(seconds_on_site / 3600.0, rules)
        seconds_paid, seconds_break = shift_hours.paid_seconds(seconds_on_site, rules)
        assert hours_paid == pytest.approx(seconds_paid / 3600.0, abs=1e-4)
        assert hours_break == pytest.approx(seconds_break / 3600.0, abs=1e-4)


# ---------------------------------------------------------------------------
# 2. the granularity: seconds, not minutes and not rounded hours
# ---------------------------------------------------------------------------
def test_one_second_decides_the_verdict_where_the_rounded_hours_cannot():
    """Two shifts a second apart, identical to everything coarser, decided differently.

    ``8.03 h`` is 28908 seconds, which is *not* a whole number of minutes - so a comparison
    that rounded to minutes would put both shifts in the same bucket and decide them the same
    way. So would one that rounded to the two decimals the console and the timesheet show.
    The verdicts have to differ anyway, which is what "compared on seconds" means.
    """
    rules = _rules(overtime_notify_hours=8.03)
    at_the_line = _on_site_at_the_line(rules)

    under = shift_hours.overtime_assessment(at_the_line - 1, rules)
    over = shift_hours.overtime_assessment(at_the_line, rules)

    assert under["paid_seconds"] == over["paid_seconds"] - 1
    assert under["paid_seconds"] // 60 == over["paid_seconds"] // 60, (
        "a comparison on whole minutes cannot tell these two shifts apart"
    )
    assert round(under["paid_hours"], 2) == round(over["paid_hours"], 2) == 8.03, (
        "and neither can the hours figure as it is shown - two shifts, one number"
    )
    assert under["reaches_threshold"] is False and under["needs_approval"] is False
    assert over["reaches_threshold"] is True and over["needs_approval"] is True


def test_the_line_is_reached_not_passed():
    """``>=`` everywhere: the worker's card, the alert and the clock-out gate share a second."""
    rules = _rules()
    at_the_line = _on_site_at_the_line(rules)
    assessment = shift_hours.overtime_assessment(at_the_line, rules)
    assert assessment["paid_seconds"] == shift_hours.overtime_rule(rules)["threshold_seconds"]
    assert assessment["reaches_threshold"] is True


def test_the_threshold_is_a_whole_number_of_seconds():
    line = shift_hours.overtime_rule(_rules(overtime_notify_hours=8.1))
    assert line["threshold_seconds"] == 29160, "8.1 h of paid work is 29160 seconds"
    assert line["regular_seconds"] == 28800
    assert isinstance(line["threshold_seconds"], int) and isinstance(line["regular_seconds"], int)


# ---------------------------------------------------------------------------
# 3. one place
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    (
        main.verify_worker,
        main.force_clock_out,
        quick_links.submit_quick_punch,
        offline_sync.materialize_worker,
        overtime.scan_overtime,
    ),
)
def test_every_path_asks_the_one_resolver(path):
    """The guard that keeps the four paths from drifting apart again.

    ``inspect.unwrap`` because one of these carries the rate limiter's wrapper, whose source
    lives in slowapi - the assertion is about *this* application's function.
    """
    source = inspect.getsource(inspect.unwrap(path))
    assert "overtime_assessment" in source or "overtime_rule" in source, (
        f"{path.__name__} does not ask shift_hours for the overtime decision"
    )
    assert "overtime_notify_hours" not in source, (
        f"{path.__name__} reads the overtime column itself - that is how the paths came to "
        "disagree about the same shift"
    )


@pytest.mark.parametrize(
    "path",
    (
        main.verify_worker,
        main.force_clock_out,
        quick_links.submit_quick_punch,
        offline_sync.materialize_worker,
        overtime.scan_overtime,
    ),
)
def test_no_path_shadows_the_module_it_announces_through(path):
    """A clock-out path may not *bind* a local named ``overtime``.

    The three paths that hold hours used to keep them in a local called ``overtime`` - the
    obvious name, and a trap once the announcement moved next to that arithmetic: the local
    shadows the ``overtime`` module for the *whole* function, so ``overtime.announce_crossing``
    became an attribute lookup on a float. It failed loudly on the offline path, which is why
    this is a guard and not an incident report: the local is called ``overtime_hours`` now,
    and this is what says so. Read as a parse rather than as text, because these functions
    discuss overtime in prose as much as they do in code.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(path))))
    bound = [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "overtime")
        or (isinstance(node, ast.arg) and node.arg == "overtime")
    ]
    assert not bound, (
        f"{path.__name__} binds a local named 'overtime', which hides the module of the same "
        f"name for the whole function (line(s) {bound})"
    )


def test_the_line_resolves_the_same_way_for_rules_that_only_carry_what_matters():
    """A path that hands over a partial rules dict still gets the line, not a fallback.

    ``offline_sync`` and the watcher read the stored row, which always carries every column,
    but the resolver must not invent 8.1 for a row that has a line set to something else.
    """
    assert shift_hours.overtime_rule({"regular_hours": 8.0, "overtime_notify_hours": 7.5})[
        "threshold_seconds"
    ] == 27000
    assert shift_hours.overtime_rule({"regular_hours": 8.0})["threshold_hours"] == 8.1


# ---------------------------------------------------------------------------
# 4. the rule: at or past the line, and past the paid day
# ---------------------------------------------------------------------------
def test_a_line_below_the_paid_day_does_not_hold_an_ordinary_day():
    """The hole this closed: a warning line set early held every full day for approval.

    With the line at 7.5 h - which is how an operator restores the automatic close (see
    ``test_day_end_precedence.py``) - a normal 8 h paid day reaches the line. It is not
    overtime, and it must not arrive in the approval queue with a zero overtime figure
    beside it.
    """
    rules = _rules(overtime_notify_hours=7.5)
    full_day = shift_hours.overtime_assessment(8 * 3600 + _break_seconds(rules), rules)
    assert full_day["reaches_threshold"] is True, "the day does reach the early line"
    assert full_day["past_regular"] is False
    assert full_day["needs_approval"] is False, "nothing here needs approving"
    assert full_day["overtime_hours"] == 0.0

    # ... and a shift that really does run past the day is held, for the hours past the day.
    long_shift = shift_hours.overtime_assessment(9 * 3600 + _break_seconds(rules), rules)
    assert long_shift["needs_approval"] is True
    assert long_shift["overtime_hours"] == 1.0


def test_the_hours_held_are_measured_from_the_paid_day():
    rules = _rules()
    assessment = shift_hours.overtime_assessment(9 * 3600 + _break_seconds(rules), rules)
    assert assessment["paid_hours"] == 9.0
    assert assessment["overtime_hours"] == 1.0, "the time past the 8 h paid day, not past the line"
    assert assessment["overtime_seconds"] == 3600


def test_the_reason_written_on_the_row_names_the_line_and_the_day():
    """No more "exceeds the 8.1h regular threshold": one sentence, both figures, both named."""
    sentence = shift_hours.overtime_assessment(
        9 * 3600 + _break_seconds(_rules()), _rules()
    )["flag_sentence"]
    assert "paid reaches the 8.1h overtime line" in sentence, sentence
    assert "1.00h past the 8h paid day" in sentence, sentence
    assert "regular threshold" not in sentence


# ---------------------------------------------------------------------------
# 5. the paths agree, at the second, on the real request
# ---------------------------------------------------------------------------
def test_the_punch_path_decides_on_the_same_second_the_resolver_does(client, app_module, monkeypatch):
    """One second short of the line is approved; on the line is held. Through HTTP."""
    monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)
    rules = _rules()
    at_the_line = _on_site_at_the_line(rules)
    moment = _FrozenDatetime.now()

    _plant(MOALLEM, at_the_line - 1, now=moment)
    response = _clock_out(client)
    assert response.status_code == 200, response.text[:300]
    assert _last_clock_out()["status_code"] == "approved", (
        "a shift one second short of the line is an ordinary day"
    )
    assert _last_clock_out()["overtime_hours"] is None

    _plant(MOALLEM, at_the_line, now=moment)
    response = _clock_out(client)
    assert response.status_code == 200, response.text[:300]
    row = _last_clock_out()
    assert row["status_code"] == "pending_overtime", row
    assert row["overtime_hours"] == pytest.approx(0.1, abs=1e-4)
    assert row["hours"] == pytest.approx(8.1, abs=1e-4)


def test_the_watcher_and_the_gate_reach_the_same_verdict(client, app_module, monkeypatch):
    """The scan's ``>=`` and the clock-out gate's are the same decision, one second apart.

    The watcher runs its own pass first (there is nothing else to ask): a shift one second
    short of the line must be silent there, and the same shift placed on the line must be
    reported - which is exactly what the punch path did in the test above.
    """
    monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)
    rules = _rules()
    at_the_line = _on_site_at_the_line(rules)
    moment = _FrozenDatetime.now()

    _plant(MOALLEM, at_the_line - 1, now=moment)
    assert overtime.scan_overtime(now=moment)["notified"] == 0, (
        "the gate approves this shift, so the alert must not contradict it a second earlier"
    )

    _plant(MOALLEM, at_the_line, now=moment)
    summary = overtime.scan_overtime(now=moment)
    assert summary["notified"] == 1, summary
    assert summary["overtime_rule"]["basis"] == "paid"
    assert summary["overtime_rule"]["threshold_seconds"] == 29160


def test_the_administrator_override_prices_the_overtime_the_same_way(client, app_module, monkeypatch):
    """The force-clock-out writes the same split as the worker's own clock-out would.

    It records the row as approved - an administrator closing the shift is the person the
    review would be handed to - but the *overtime figure* is the shared one, so the report
    totals and the payroll sheet read the same split whichever way the shift ended.
    """
    monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)
    rules = _rules()
    _plant(MOALLEM, 9 * 3600 + _break_seconds(rules), now=_FrozenDatetime.now())

    forced = client.post(
        "/api/v1/admin/force_clock_out", headers=bearer(ADMIN), json={"worker_id": MOALLEM}
    )
    assert forced.status_code == 200, forced.text[:300]
    row = _last_clock_out()
    assert row["status_code"] == "approved", "the override is the approval"
    assert row["hours"] == pytest.approx(9.0, abs=1e-3)

    expected = shift_hours.overtime_assessment(
        9 * 3600 + _break_seconds(rules), rules
    )["overtime_hours"]
    assert row["overtime_hours"] == pytest.approx(expected, abs=1e-4) == pytest.approx(1.0)


def test_the_held_row_carries_the_shared_sentence(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)
    rules = _rules()
    _plant(MOALLEM, 9 * 3600 + _break_seconds(rules), now=_FrozenDatetime.now())

    assert _clock_out(client).status_code == 200
    reason = _last_clock_out()["flag_reason"]
    assert "8.1h overtime line" in reason, reason
    assert "1.00h past the 8h paid day" in reason, reason
    assert "regular threshold" not in reason


# ---------------------------------------------------------------------------
# 6. the write
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", (0, -1, 25, 900))
def test_a_line_that_cannot_mean_anything_is_refused(client, value):
    """A line at 0 holds every shift for approval; a line at 900 means nobody is ever late."""
    response = client.post(
        "/api/v1/admin/shift_rules", headers=bearer(ADMIN), json={"overtime_notify_hours": value}
    )
    assert response.status_code == 400, response.text[:200]
    stored = client.get("/api/v1/admin/shift_rules", headers=bearer(ADMIN)).json()
    assert float(stored["overtime_notify_hours"]) == 8.1, "a refused change must not be stored"


def test_a_line_an_operator_means_is_stored(client):
    response = client.post(
        "/api/v1/admin/shift_rules", headers=bearer(ADMIN), json={"overtime_notify_hours": 7.75}
    )
    assert response.status_code == 200, response.text[:200]
    assert float(response.json()["rules"]["overtime_notify_hours"]) == 7.75
    # Fractional hours survive, because the comparison is on seconds rather than on whole
    # minutes: 7.75 h is 27900 seconds, not "7 h 45 m rounded somewhere".
    assert shift_hours.overtime_rule({"regular_hours": 8.0, "overtime_notify_hours": 7.75})[
        "threshold_seconds"
    ] == 27900
