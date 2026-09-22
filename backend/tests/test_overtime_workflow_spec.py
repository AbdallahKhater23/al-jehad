"""The overtime workflow, stated as acceptance tests: refusal, approval, and the alert.

WHY THIS EXISTS
---------------
Three behaviours carry the whole workflow, and each of them is a sentence somebody can
disagree with rather than a function that either runs or crashes:

1. **Refusing a long shift pays the standard day.** The refusal is of the *extra* hours,
   never of the work: the shift settles at the policy's paid day, the overtime is not
   credited, and the manager's reason is kept. A refusal that silently drops the day, or
   that leaves the refused overtime standing, is the bug this half exists to catch - and
   it has happened here before: the console's Reject button used to POST to
   ``/admin/approve_review``, so pressing it approved the overtime the manager had just
   refused. The API could not notice, because the request it received was a valid approval.
2. **Approving part of a shift keeps ``approved_hours`` and ``overtime_hours``
   consistent.** Overtime is what was *authorised* past the paid day, not what the clock
   recorded, so the two figures on a row must never contradict each other - and neither
   may exceed what was actually worked.
3. **The crossing alert fires at the boundary.** At the exact second the paid hours reach
   the line, the administrator and the worker are both told; one second short, nobody is.

What this suite adds over ``test_phase02_liveness_and_shifts.py`` (which pins the same
endpoints individually) is the level the figures are read at:

* the refusal is checked in the **payroll totals** and in the worker's **own report**, not
  only on the row, so a refusal that pays on one screen and not another fails here;
* "the standard day" is shown to be the **policy's** day, by moving ``regular_hours`` off
  8.0 - a hard-coded 8 would pass every other test in the repository;
* the two figures are held to an **invariant** (``overtime == max(0, approved - day)`` and
  ``approved <= recorded``) rather than to one expected pair;
* the boundary is exercised at **one second** either side, addressed to **both**
  audiences, and asserted to fire **once**.

THE LAST SECTION (3b) IS ABOUT THE PATHS WHERE THE TIMER IS TOO LATE
Those were the tests that specified the gap, and they were red on purpose until it was
closed: the crossing alert used to be written *only* by ``overtime.scan_overtime``, the
timer that walks shifts that are still open. A worker who crossed the line and then clocked
out before the next pass was never announced to - the administrator got "Overtime requires
approval" and the worker got nothing, which is the exact asymmetry the worker channel was
built to remove. The announcement is now written where the overtime decision is made, by
``overtime.announce_crossing``: the watcher calls it for a shift that is still running, and
so does every path that can end one - the punch, a quick link, an offline punch
materialized hours later, and the clock-out an administrator forces. These tests pin that,
which is why they assert the worker's *inbox* rather than the function's return.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
import harness
from harness import ADMIN, DB_PATH, MOALLEM, bearer, clock_in, db_scalar

import overtime
import shift_hours

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"

#: The shipped rules: an 8 h paid day, a line at 8.1 h of paid work, a 30-minute unpaid
#: break. Named here so the arithmetic below reads against the numbers an operator sees.
REGULAR_HOURS = 8.0
OVERTIME_LINE_HOURS = 8.1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rules(client, **overrides) -> dict:
    """Store shift rules through the endpoint the console posts to."""
    response = client.post("/api/v1/admin/shift_rules", headers=bearer(ADMIN), json=overrides)
    assert response.status_code == 200, response.text[:300]
    return response.json()["rules"]


def _stored_rules() -> dict:
    # Closed explicitly: a connection left open by a ``with`` block (which manages the
    # transaction, not the file) can hold a lock that the per-test database reset trips over.
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        return overtime.rules(conn)
    finally:
        conn.close()


def _seconds(hours: float) -> int:
    return int(round(hours * shift_hours.SECONDS_PER_HOUR))


def _plant_open_shift(worker_id: str, seconds_on_site: int, *, now: datetime | None = None) -> str:
    """Open a shift that started ``seconds_on_site`` seconds ago, straight in the database.

    The API has no way to backdate a clock-in - correct, and the reason aging a session for
    a test has to bypass it. ``now`` is the clock the code under test will read, so a test
    that pins the boundary plants against the same instant it scans at.
    """
    clock_in_time = ((now or datetime.now()) - timedelta(seconds=seconds_on_site)).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, SITE, clock_in_time),
        )
        conn.commit()
    finally:
        conn.close()
    return clock_in_time


def _clock_in_time(log_id: int) -> str:
    return db_scalar("SELECT clock_in_time FROM attendance_logs WHERE id = ?", (log_id,))


def _shift_waiting_for_a_decision(client, *, on_site_hours: float = 9.5) -> int:
    """A clock-out past the line, sitting in the review queue. Returns its log id.

    9.5 h on site is 9.0 h paid: the 8 h day plus an hour that now requires a decision.
    """
    _plant_open_shift(MOALLEM, _seconds(on_site_hours))
    response = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:300]
    log_id = int(db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)))
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime", (
        "the shift must reach the queue before the decision can be made about it"
    )
    return log_id


def _decide(client, log_id: int, *, approve: bool, hours: float | None = None, note: str):
    payload = {"log_id": log_id, "note": note}
    if hours is not None:
        payload["approved_hours"] = hours
    path = "/admin/approve_review" if approve else "/admin/reject_review"
    return client.post(f"/api/v1{path}", headers=bearer(ADMIN), json=payload)


def _timesheet_row(client, log_id: int) -> dict:
    body = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    matching = [row for row in body["rows"] if row["log_id"] == log_id]
    assert matching, f"the shift must still be a row in the timesheet: {body['rows']}"
    return matching[0]


def _timesheet_totals(client) -> dict:
    return client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()["totals"]


def _my_totals(client) -> dict:
    """The worker's own figures, from the same rows the console reads."""
    return client.get("/api/v1/worker/me/report", headers=bearer(MOALLEM)).json()["totals"]


def _my_inbox(client, worker_id: str = MOALLEM) -> dict:
    """The worker's own notification channel: what the app has told *them*."""
    return client.get("/api/v1/worker/me/notifications", headers=bearer(worker_id)).json()


def _columns(log_id: int) -> dict:
    return {
        name: db_scalar(f"SELECT {name} FROM attendance_logs WHERE id = ?", (log_id,))
        for name in ("status_code", "hours", "approved_hours", "overtime_hours", "flag_reason")
    }


def _assert_two_figures_are_consistent(log_id: int, *, regular_hours: float = REGULAR_HOURS) -> dict:
    """The invariant every settled overtime row has to satisfy, whichever way it was settled.

    Written as a relation rather than an expected pair: it has to hold for the refusal, for
    a full approval and for a part approval alike, and the pair that satisfies it changes
    with the decision while the relation does not.
    """
    columns = _columns(log_id)
    approved = columns["approved_hours"]
    overtime_hours = columns["overtime_hours"]
    recorded = float(columns["hours"] or 0.0)

    assert approved is not None, "a settled row states the hours it credits"
    assert float(approved) <= recorded + 1e-6, (
        f"a decision cannot credit more than was worked: {approved} of {recorded}"
    )
    assert float(overtime_hours) >= 0, "overtime is never negative"
    assert float(overtime_hours) == pytest.approx(max(0.0, float(approved) - regular_hours), abs=0.01), (
        "overtime is what was authorised past the paid day: "
        f"{overtime_hours} h beside {approved} h approved of a {regular_hours:g} h day"
    )
    return columns


def _admin_alerts(kind: str) -> int:
    return int(db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = ?", (kind,)))


def _worker_alerts(kind: str) -> int:
    return int(
        db_scalar(
            "SELECT COUNT(*) FROM worker_notifications WHERE kind = ? AND worker_id = ?",
            (kind, MOALLEM),
        )
    )


def _on_site_at_the_line() -> int:
    """Seconds on site that put *paid* time exactly on the overtime line."""
    values = _stored_rules()
    line = shift_hours.overtime_rule(values)
    break_seconds = int(round(shift_hours.break_hours(values) * shift_hours.SECONDS_PER_HOUR))
    return line["threshold_seconds"] + break_seconds


# ---------------------------------------------------------------------------
# 0. what a shift is worth *before* anybody decides
# ---------------------------------------------------------------------------
def test_a_pending_overtime_shift_has_already_earned_its_standard_day(client):
    """The day is credited while the extra hour waits; only the extra is in question.

    The premise of the whole queue is that a worker/moallem works 8 h normally and the time
    *past* it needs a manager. Holding the entire shift meant an ordinary day was
    conditional on somebody pressing a button - and it contradicted the refusal beside it,
    which has always paid the standard day. So the split is asserted at every surface the
    hours reach, and the two columns are asserted to add up to what was worked.
    """
    log_id = _shift_waiting_for_a_decision(client)

    row = _timesheet_row(client, log_id)
    assert row["awaiting_approval"] is True, "the extra hour is still undecided"
    assert row["hours"] == pytest.approx(9.0, abs=0.01), "the row holds what was worked"
    assert row["recorded_hours"] == pytest.approx(9.0, abs=0.01), row
    assert row["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), (
        "the standard day is earned the moment the shift is filed"
    )
    assert row["awaiting_approval_hours"] == pytest.approx(1.0, abs=0.01), (
        "and only the hour past the standard day is waiting on a manager"
    )

    totals = _timesheet_totals(client)
    assert totals["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), totals
    assert totals["awaiting_approval_hours"] == pytest.approx(1.0, abs=0.01), totals
    assert totals["hours"] == pytest.approx(
        totals["approved_hours"] + totals["awaiting_approval_hours"], abs=0.001
    ), f"the two columns have to add up to the hours shown: {totals}"
    assert totals["awaiting_approval"] == 1, "the shift is still in the queue"

    # The worker's own copy of the same period cannot tell a different story, and neither
    # can the payroll figures an administrator exports.
    mine = _my_totals(client)
    assert mine["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), mine
    assert mine["awaiting_approval_hours"] == pytest.approx(1.0, abs=0.01), mine

    # The decision still overwrites the provisional day: it is a credit, not a floor.
    _decide(client, log_id, approve=True, hours=8.25, note="15 min authorised")
    settled = _timesheet_row(client, log_id)
    assert settled["approved_hours"] == pytest.approx(8.25, abs=0.01), (
        "an approval decides the whole row, including the day it had provisionally credited"
    )
    assert settled["awaiting_approval_hours"] == 0, settled
    assert settled["awaiting_approval"] is False, settled


def test_a_shift_held_for_review_credits_nothing_until_it_is_decided(client):
    """The other hold is not the same thing, and must not have been caught by the change.

    ``pending_review`` is a doubt about whether the work happened at all - a liveness
    verdict, a selfie that did not match, a punch with no face. There is no "standard day"
    to credit ahead of that decision, so the whole row stays in the awaiting column.
    """
    from reports import AWAITING_APPROVAL_CODES, PAYABLE_CODES, _shift_hours

    assert "pending_review" in AWAITING_APPROVAL_CODES
    assert "pending_review" not in PAYABLE_CODES
    counted, approved, awaiting = _shift_hours(
        {"hours": 9.0, "status_code": "pending_review", "approved_hours": None, "overtime_hours": 1.0}
    )
    assert (counted, approved, awaiting) == (9.0, 0.0, 9.0), (
        "a review hold credits nothing, whatever else the row carries"
    )


def test_an_overtime_hold_with_no_figure_credits_nothing(client):
    """Fail closed: a row held for overtime but carrying no figure for the hold.

    ``overtime_hours`` is what says which part of the shift is in question. Without it
    there is no standard day to separate out, and guessing one would credit hours on a
    number that is not there to be checked.
    """
    from reports import _shift_hours

    for held in (None, 0, -1):
        counted, approved, awaiting = _shift_hours(
            {"hours": 9.0, "status_code": "pending_overtime", "approved_hours": None, "overtime_hours": held}
        )
        assert (counted, approved, awaiting) == (9.0, 0.0, 9.0), (
            f"a hold of {held!r} leaves the whole row undecided"
        )


def test_an_overtime_hold_larger_than_the_shift_cannot_credit_more_than_was_worked(client):
    """The arithmetic cannot invent hours, whatever a row happens to carry."""
    from reports import _shift_hours

    counted, approved, awaiting = _shift_hours(
        {"hours": 8.5, "status_code": "pending_overtime", "approved_hours": None, "overtime_hours": 40}
    )
    assert counted == 8.5, counted
    assert approved == 0.0, f"a hold larger than the shift leaves nothing to credit: {approved}"
    assert awaiting == 8.5, awaiting


# ---------------------------------------------------------------------------
# 1. refusing a long shift pays the standard day
# ---------------------------------------------------------------------------
def test_rejecting_a_long_shift_pays_the_standard_day(client):
    """The requirement, read at every surface the hours appear on."""
    log_id = _shift_waiting_for_a_decision(client)

    refused = _decide(
        client, log_id, approve=False, note="no overtime was authorised for this shift"
    )
    assert refused.status_code == 200, refused.text[:300]

    # The row: the day is kept, the extra hour is not.
    columns = _assert_two_figures_are_consistent(log_id)
    assert columns["status_code"] == "overtime_rejected", columns
    assert columns["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), (
        "the refusal is of the extra hours, not of the work"
    )
    assert columns["overtime_hours"] == 0, "a refusal must not credit what it refused"
    assert db_scalar("SELECT reviewed_by FROM attendance_logs WHERE id = ?", (log_id,)) == ADMIN

    # ... and the money. The row alone can be right while the totals beside it are not,
    # which is why the refusal is asserted at the level a pay run reads.
    row = _timesheet_row(client, log_id)
    assert row["awaiting_approval"] is False, "a refusal is a decision, so it leaves the queue"
    assert row["hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), row
    assert row["recorded_hours"] == pytest.approx(9.0, abs=0.01), (
        "the clock is kept beside the decision: 8 h paid out of 9 h worked"
    )

    totals = _timesheet_totals(client)
    assert totals["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), totals
    assert totals["awaiting_approval_hours"] == 0, "nothing about this shift is still undecided"
    assert totals["awaiting_approval"] == 0, totals

    # The worker's own copy of the same period cannot tell a different story.
    mine = _my_totals(client)
    assert mine["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01), mine
    assert mine["awaiting_approval_hours"] == 0, mine


def test_a_refusal_pays_the_policy_day_not_a_hard_coded_eight(client):
    """'The standard day' is whatever the policy says it is, not the number 8.

    Every other refusal test in the repository leaves ``regular_hours`` at its shipped 8.0,
    where a hard-coded 8 and the policy agree - and so cannot fail.
    """
    _rules(client, regular_hours=7.5, overtime_notify_hours=7.6)
    # 8.5 h on site is 8.0 h paid: past the 7.6 h line, and half an hour past the 7.5 h day.
    log_id = _shift_waiting_for_a_decision(client, on_site_hours=8.5)

    refused = _decide(client, log_id, approve=False, note="refused under the short-day policy")
    assert refused.status_code == 200, refused.text[:300]

    columns = _assert_two_figures_are_consistent(log_id, regular_hours=7.5)
    assert columns["approved_hours"] == pytest.approx(7.5, abs=0.01), (
        f"the policy day is 7.5 h, not 8: {columns}"
    )
    assert columns["overtime_hours"] == 0
    assert _timesheet_row(client, log_id)["hours"] == pytest.approx(7.5, abs=0.01)


def test_a_refusal_credits_no_overtime_anywhere(client):
    """The refused hours are gone from every figure, the reason survives, the queue empties."""
    log_id = _shift_waiting_for_a_decision(client)
    reason = "the site closed at 16:00 - the foreman confirmed it"

    assert _decide(client, log_id, approve=False, note=reason).status_code == 200

    assert db_scalar(
        "SELECT COALESCE(SUM(overtime_hours), 0) FROM attendance_logs WHERE worker_id = ?",
        (MOALLEM,),
    ) == 0, "no row may carry the refused overtime"

    # Out of the queue, and out of the administrator's unread notifications.
    pending = client.get("/api/v1/admin/pending_reviews", headers=bearer(ADMIN)).json()
    assert all(row["id"] != log_id for row in pending), "the decision took it out of the queue"
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE log_id = ? AND read_at IS NULL", (log_id,)
    ) == 0, "the alert it came from is spent"

    # The reason is answerable next month - on the row, and in the append-only trail.
    assert db_scalar("SELECT flag_reason FROM attendance_logs WHERE id = ?", (log_id,)) == reason
    audit = client.get("/api/v1/admin/audit_log", headers=bearer(ADMIN)).json()
    entries = [
        entry
        for entry in audit
        if entry["action"] == "review_reject" and str(entry["entity_id"]) == str(log_id)
    ]
    assert entries, "the decision belongs in the audit log"
    assert reason in str(entries[0]["after_json"]), entries[0]["after_json"]


# ---------------------------------------------------------------------------
# 2. approving part of a shift keeps approved_hours and overtime_hours consistent
# ---------------------------------------------------------------------------
def test_approving_part_of_a_shift_keeps_the_two_figures_consistent(client):
    """15 minutes authorised out of the hour worked: the row states that, not the hour."""
    log_id = _shift_waiting_for_a_decision(client)  # 9.0 h recorded, 8.0 h day

    approved = _decide(client, log_id, approve=True, hours=8.25, note="authorised 15 min")
    assert approved.status_code == 200, approved.text[:300]

    columns = _assert_two_figures_are_consistent(log_id)
    assert columns["approved_hours"] == pytest.approx(8.25, abs=0.01), columns
    assert columns["overtime_hours"] == pytest.approx(0.25, abs=0.01), (
        "the authorised quarter hour - not the 1.0 h the clock recorded"
    )


def test_approving_the_paid_day_credits_no_overtime(client):
    """A full day signed off as a full day is not overtime."""
    log_id = _shift_waiting_for_a_decision(client)

    assert _decide(client, log_id, approve=True, hours=REGULAR_HOURS, note="day approved").status_code == 200

    columns = _assert_two_figures_are_consistent(log_id)
    assert columns["approved_hours"] == pytest.approx(REGULAR_HOURS, abs=0.01)
    assert columns["overtime_hours"] == 0, columns


def test_approving_below_the_paid_day_never_credits_overtime(client):
    """The relation is a maximum, not a difference: less than a day is never negative overtime."""
    log_id = _shift_waiting_for_a_decision(client)

    assert _decide(client, log_id, approve=True, hours=6.0, note="half day approved").status_code == 200

    columns = _assert_two_figures_are_consistent(log_id)
    assert columns["approved_hours"] == pytest.approx(6.0, abs=0.01)
    assert columns["overtime_hours"] == 0, columns


def test_approving_more_than_was_worked_is_refused(client):
    """The consistency rule is also a ceiling: a decision cannot credit hours nobody worked."""
    log_id = _shift_waiting_for_a_decision(client)

    over = _decide(client, log_id, approve=True, hours=12.0, note="generous")
    assert over.status_code == 400, over.text[:200]

    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime", (
        "a refused approval must leave the shift exactly where it was"
    )
    assert db_scalar("SELECT approved_hours FROM attendance_logs WHERE id = ?", (log_id,)) is None


def test_the_two_figures_agree_in_the_payroll_totals_and_the_worker_report(client):
    """One decision, four surfaces: the row, the timesheet, its totals, the worker's report."""
    log_id = _shift_waiting_for_a_decision(client)
    assert _decide(client, log_id, approve=True, hours=8.25, note="authorised 15 min").status_code == 200

    row = _timesheet_row(client, log_id)
    assert row["hours"] == pytest.approx(8.25, abs=0.01), "the approved figure is what is paid"
    assert row["approved_hours"] == pytest.approx(8.25, abs=0.01), row
    assert row["recorded_hours"] == pytest.approx(9.0, abs=0.01), "the clock is kept beside it"
    assert row["awaiting_approval"] is False

    totals = _timesheet_totals(client)
    assert totals["approved_hours"] == pytest.approx(8.25, abs=0.01), totals
    assert totals["awaiting_approval_hours"] == 0, totals
    assert totals["hours"] == pytest.approx(8.25, abs=0.01), (
        "hours == approved + awaiting holds by construction; 8.25 + 0 here"
    )

    mine = _my_totals(client)
    assert mine["approved_hours"] == pytest.approx(8.25, abs=0.01), mine
    assert mine["awaiting_approval_hours"] == 0, mine


# ---------------------------------------------------------------------------
# 3. the crossing alert fires at the boundary
# ---------------------------------------------------------------------------
def test_the_crossing_alert_fires_on_the_exact_second(client):
    """Paid hours exactly on the line: both audiences, in the same pass.

    The comparison is inclusive on purpose. A shift that stops *on* the line has crossed it
    in the only sense that matters to a payroll decision, and the strict/inclusive split
    this repository once had - the watcher alerting about a second the clock-out gate then
    auto-approved - is the disagreement this pins shut.
    """
    moment = datetime.now().replace(microsecond=0)
    _plant_open_shift(MOALLEM, _on_site_at_the_line(), now=moment)

    summary = overtime.scan_overtime(now=moment)

    assert summary["notified"] == 1, summary
    assert summary["worker_notified"] == 1, summary
    # The administrator is asked on a different surface now: a crossing is a *question* about an
    # open shift, so it lives in the Approvals queue (``overtime.open_crossings``) rather than in
    # the Alerts tab, where reading it answered nothing. The worker's half is unchanged, and the
    # crossing is still one event: the stamp below is what keeps it from repeating.
    assert _admin_alerts("overtime_exceeded") == 0, "the crossing is still arriving in Alerts"
    assert [item["worker_id"] for item in overtime.open_crossings()] == [MOALLEM], (
        "the crossing is in neither surface, so nobody is asked about it at all"
    )
    assert _worker_alerts("overtime_crossed") == 1, "and so is the person it happened to"
    assert db_scalar(
        "SELECT overtime_notified_at FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    ), "the session remembers it was reported, so it is reported once"

    # The worker can read it, which is what makes the alert delivered rather than written.
    inbox = client.get("/api/v1/worker/me/notifications", headers=bearer(MOALLEM)).json()
    crossing = [row for row in inbox["notifications"] if row["kind"] == "overtime_crossed"]
    assert crossing, inbox
    assert inbox["unread"] >= 1, "an alert the worker has not read is what the app badges"


def test_one_second_short_of_the_line_no_alert_fires(client):
    """The other side of the boundary, and the reason the shift is aged on whole seconds."""
    moment = datetime.now().replace(microsecond=0)
    _plant_open_shift(MOALLEM, _on_site_at_the_line() - 1, now=moment)

    summary = overtime.scan_overtime(now=moment)

    assert summary["notified"] == 0, summary
    assert summary["worker_notified"] == 0, summary
    assert _admin_alerts("overtime_exceeded") == 0, "one second is not a crossing"
    assert _worker_alerts("overtime_crossed") == 0
    assert db_scalar(
        "SELECT overtime_notified_at FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    ) is None


def test_the_crossing_is_reported_once(client):
    """The watcher runs every few seconds; a crossing is one event, not one per pass."""
    moment = datetime.now().replace(microsecond=0)
    _plant_open_shift(MOALLEM, _on_site_at_the_line(), now=moment)

    first = overtime.scan_overtime(now=moment + timedelta(seconds=30))
    assert first["notified"] == 1, first

    second = overtime.scan_overtime(now=moment + timedelta(seconds=60))
    assert second["notified"] == 0, second
    assert second["worker_notified"] == 0, second
    assert second["already_notified"] == 1, "the pass is not silent, it is spent"

    assert _admin_alerts("overtime_exceeded") == 0, "the crossing became a notification again"
    assert _worker_alerts("overtime_crossed") == 1


# ---------------------------------------------------------------------------
# 3b. the boundary alert on the paths where the shift is over before the watcher looks
#
# The watcher is a timer, and a timer can only see a shift that is still open - so the
# announcement cannot be its job alone. Every path that ends a shift announces the
# crossing at the moment it makes the overtime decision, through the one shared writer
# (``overtime.announce_crossing``), which is what these four tests hold in place. The
# event requirement 2 is about - "you have reached the threshold, the extra hours need
# review" - is the same sentence on all of them, and the administrator gets the alert
# they act on (``review_pending``) on the paths that actually hold hours for a decision.
# ---------------------------------------------------------------------------
def test_a_worker_who_clocks_out_above_the_line_is_told_they_crossed_it(client):
    """The punch path: the shift is over by the time the timer could have looked."""
    log_id = _shift_waiting_for_a_decision(client)

    # The crossing *was* detected - the shift is held for a decision, and the administrator
    # is told about it through the notification they act on.
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == (
        "pending_overtime"
    )
    assert _admin_alerts("review_pending") == 1, "the administrator is told"

    inbox = _my_inbox(client)
    crossing = [row for row in inbox["notifications"] if row["kind"] == "overtime_crossed"]
    assert crossing, (
        "the worker whose extra hours are being reviewed must be told they crossed the "
        "line on the device, not only on a screen they have to open: "
        f"inbox={inbox['notifications']}"
    )
    body = str(crossing[0])
    assert "8.1" in body and "approval" in body.lower(), (
        f"the alert has to name the line and what happens to the hours past it: {body}"
    )


def test_the_crossing_alert_survives_a_clock_out_taken_through_a_quick_link(client):
    """The other way a shift ends: a tap on a link, with no session and no watcher."""
    # The seed data carries one standing ``pending_review`` row for worker 1, and a flagged
    # account is refused a clock-out from a link (its own test pins that). Cleared here, as
    # the quick-link suite clears it, so this measures the alert and not the flag rule.
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")
        conn.commit()
    finally:
        conn.close()
    _plant_open_shift(harness.WORKER, _seconds(9.5))
    link = client.post(
        "/api/v1/admin/quick_links",
        headers=bearer(harness.HEAD_ADMIN),
        json={"worker_id": harness.WORKER},
    )
    assert link.status_code == 200, link.text[:300]

    lat, lon = harness.INSIDE_DOWNTOWN.split(",")
    response = client.post(
        f"/api/v1/q/{link.json()['token']}",
        data={"lat": lat, "lon": lon, "confirm_early_checkout": "1"},
        files={"selfie": ("selfie.jpg", harness.jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["action"] == "Clock Out"
    assert db_scalar(
        "SELECT status_code FROM attendance_logs WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
        (harness.WORKER,),
    ) == "pending_overtime", "the link's clock-out reaches the same decision"

    inbox = client.get(
        "/api/v1/worker/me/notifications", headers=bearer(harness.WORKER)
    ).json()
    assert [row for row in inbox["notifications"] if row["kind"] == "overtime_crossed"], (
        "the link path owes the worker the same sentence the app does: "
        f"inbox={inbox['notifications']}"
    )


def test_a_forced_clock_out_still_tells_the_worker_they_crossed_it(client):
    """The override: the hours are authorised, and the worker is told about their own day.

    A force clock-out is the administrator closing a shift *as the person the review would
    be handed to*, so it records the hours approved and owes no review alert - the audit row
    is the administrator's record of their own act. What it still owes is the worker: their
    shift is over, and they are the one who cannot see the timesheet.
    """
    _plant_open_shift(MOALLEM, _seconds(9.5))

    response = client.post(
        "/api/v1/admin/force_clock_out", headers=bearer(ADMIN), json={"worker_id": MOALLEM}
    )
    assert response.status_code == 200, response.text[:300]
    assert db_scalar(
        "SELECT status_code FROM attendance_logs WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
        (MOALLEM,),
    ) == "approved", "the override records the hours; nothing is held for a decision"
    assert _admin_alerts("review_pending") == 0, "the administrator was the one who closed it"

    inbox = _my_inbox(client)
    crossing = [row for row in inbox["notifications"] if row["kind"] == "overtime_crossed"]
    assert crossing, f"a forced clock-out owes the worker the same notice: {inbox['notifications']}"
    assert "8.1" in str(crossing[0])


def test_a_clock_out_over_the_alert_line_but_under_the_paid_day_is_not_announced(client):
    """The line can sit below the paid day; the notice follows the *hours*, not the line.

    With the alert set at 7.5 h and the day at 8, a shift that stops at 7.75 h of paid work
    has crossed the alert line - but it is paid in full, nothing is held and there is nothing
    to review. The watcher would have alerted it mid-shift ("you are still working past the
    line"); a clock-out has no such message to deliver, so the announcement is made where the
    hours are actually held. This test exists so that "call it unconditionally" is a decision
    somebody has to make deliberately rather than a tidy-up.
    """
    _rules(client, overtime_notify_hours=7.5)
    _plant_open_shift(MOALLEM, _seconds(8.25))          # 7.75 h paid: over the line, under the day

    response = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:300]
    assert db_scalar(
        "SELECT status_code FROM attendance_logs WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
        (MOALLEM,),
    ) == "approved", "under the paid day: paid, not held"
    assert _worker_alerts("overtime_crossed") == 0, (
        "nothing is being reviewed, so the worker is told nothing about their pay"
    )


def test_a_worker_warned_on_site_and_then_clocking_out_is_told_once(client):
    """One crossing is one notice, whichever of the two writers saw it first.

    The watcher alerts a worker whose shift is *still running*; that worker then clocks out
    before the shift is closed. Both writers describe one event, so the second one has to
    find the announcement already there - if the dedupe key named the caller rather than the
    shift, a worker alerted on site would be told twice for the same crossing the moment
    they finished.
    """
    moment = datetime.now().replace(microsecond=0)
    _plant_open_shift(MOALLEM, _on_site_at_the_line(), now=moment)
    assert overtime.scan_overtime(now=moment)["worker_notified"] == 1
    assert _worker_alerts("overtime_crossed") == 1

    response = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:300]
    assert db_scalar(
        "SELECT status_code FROM attendance_logs WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
        (MOALLEM,),
    ) == "pending_overtime", "the shift is still held for the same decision"

    assert _worker_alerts("overtime_crossed") == 1, (
        "the clock-out announced the crossing the watcher had already announced"
    )


def test_the_alert_names_the_crossing_time_the_clock_will_agree_with(client):
    """Telling somebody 'you crossed at ...' is only useful if the time is the real one."""
    moment = datetime.now().replace(microsecond=0)
    planted = _plant_open_shift(MOALLEM, _on_site_at_the_line(), now=moment)

    assert overtime.scan_overtime(now=moment)["notified"] == 1

    # The crossing is at clock-in plus the line plus the break inside the day: the same
    # instant the clock-out path would judge the shift on.
    expected = (
        datetime.strptime(planted, TS) + timedelta(seconds=_on_site_at_the_line())
    ).strftime(TS)
    row = db_scalar(
        "SELECT payload FROM worker_notifications WHERE kind = 'overtime_crossed' AND worker_id = ?",
        (MOALLEM,),
    )
    assert expected in str(row), (
        f"the crossing time the worker is told must match the clock: expected {expected} in {row}"
    )
