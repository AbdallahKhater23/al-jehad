"""What happens to a shift while it is still open: close at 8 h paid, alert past that.

TWO JOBS, ONE TIMER - AND ONLY ONE OF THEM CAN END A DAY
--------------------------------------------------------
``scan_overtime()`` is the *alert*: it walks ``active_sessions`` and, for each shift
whose elapsed time has reached ``overtime_notify_hours`` (8.1 h by default), stamps
``overtime_notified_at`` once, inserts an ``overtime_exceeded`` notification carrying
the exact crossing moment, and appends a system action to the append-only
``audit_log``.

It writes **two** notifications for that crossing, in the same transaction: the
administrator's (the queue above) and the worker's own
(``notifications.KIND_WORKER_OVERTIME_CROSSED``, in ``worker_notifications``). The
worker's half is what stops this being an alert *about* somebody that the somebody is
never told; ``push.dispatch_async()`` then tries to reach their phone after the
transaction commits, which is the only part of this that depends on a third party and
the only part that is allowed to fail quietly.

The worker's half is **not the watcher's alone**. A timer can only see a shift that is
still open, so a worker who crossed the line and then clocked out before the next pass
was never told - the administrator's review alert arrived and theirs did not. Every path
that can end a shift announces the crossing at the moment it makes the overtime decision
(the punch, the quick link, the offline punch materialized later, and the clock-out an
administrator forces), through the same ``announce_crossing`` this scan calls: one
sentence, one set of figures taken from the shift itself, one ``dedupe_key`` naming the
shift - so a worker alerted while still on site and then clocking out is told once. The
administrator's half stays here, because ``KIND_OVERTIME_EXCEEDED`` describes a shift that
is *still open*; a clock-out leaves them the alert they act on instead
(``KIND_REVIEW_PENDING``, the review queue).

``scan_auto_close()`` is the *rule*: a day is ``regular_hours`` of paid work plus an
unpaid break (see ``shift_hours.py``), so when an open shift reaches that many paid
hours the system writes the clock-out itself - at the boundary, not at the moment the
watcher happened to run - marks it ``auto_closed_8h``, and tells the administrator *and
the worker whose day it ended* (``KIND_SHIFT_AUTO_CLOSED`` and
``KIND_WORKER_SHIFT_AUTO_CLOSED``, in the same transaction, exactly as the crossing does).
The close is the one path that ends a shift unattended, so it is the one path whose
worker notice has to say what happened; without it the worker only learned the day was
over when their next clock-out refused them.

They run in one pass, and the close deletes the session it closed - so a shift the close has
ended can no longer be observed crossing the alert line. Whichever rule acts first therefore
decides whether the crossing is ever reported, and that decision is stated once, in
``shift_hours.day_end_rules(values)``, which both scans read:

* **the alert is below the paid day** (``notify < regular``) - the crossing is observed
  while the shift is open, so the close still owns the end of the day;
* **the alert is above it** (``notify > regular``, the shipped 8.1 vs 8.0) - the close
  **stands down**. It would otherwise end the day 0.1 h before the alert was due, which is
  how the crossing became unreportable in the first place. The shift then runs on, the
  alert fires at the threshold while the worker is still working, and the hours past the
  paid day are clocked out into overtime review instead of being truncated at the boundary;
* **the alert is on the paid day** - there is nothing to observe ("crossed the paid limit
  and is still working" is false at that figure, and alerting on every full day is not a
  crossing), so the close acts and the scans report that the alert cannot fire;
* **the close is off** - only a clock-out ends a shift, and the alert always fires.

The verdict travels: ``start_watcher`` logs it once at startup, ``GET /admin/shift_rules``
publishes it to the console, and ``readiness`` reports both halves of it as advisory checks
(the alert reachable, and the close standing down). A deferral is never silent, because "the
automatic close is on" would otherwise be the only thing the settings said.

Neither job touches a session that is under its limit.

WHAT THE CLOSE COSTS, SAID OUT LOUD
-----------------------------------
A shift that is still open when the watcher runs *after* the boundary loses the time
past it - that is what "the day ends at 8 paid hours" means, and it is why this
writes an alert rather than closing quietly. The alert names the excess ("still open
1.2 h past the limit"), and the log row keeps the exact closing moment, so the
administrator can see that a worker was on site longer than the recorded hours. The
worker is told the same thing in their own inbox, because the person who may have
worked that unrecorded hour is the last one who should have to infer it from a
timesheet. The closed shift's hours are **payable** (``auto_closed_8h``), unlike the
11 h-era ``auto_closed`` rows, which are not: nobody ever decided what those were worth.

IDEMPOTENCE
-----------
``overtime_notified_at IS NULL`` guards the alert, and the notification additionally
carries a ``dedupe_key`` on ``admin_notifications`` (UNIQUE). Both matter: the watcher
runs on a timer, several ASGI workers may each run one, and a restart must not
re-alert the same shift. Re-alerting is not harmless - an administrator who is paged
twice an hour for the same worker stops reading the alerts. The close needs no such
guard: it deletes the session it closed, so there is nothing left to close twice.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta

import migrations
import notifications
import push
import shift_hours
from config import settings
from database import db

log = logging.getLogger("attendance.overtime")

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def rules(conn: sqlite3.Connection | None = None) -> dict:
    """Current shift rules, falling back to the shipped defaults.

    Read directly here rather than imported from ``main`` so this module has no
    dependency on the application module (``main`` imports *this*)."""
    values = dict(migrations.DEFAULT_SHIFT_RULES)
    owns = conn is None
    handle = conn or db()
    try:
        try:
            row = handle.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            for key in values:
                try:
                    value = row[key]
                except (IndexError, KeyError):
                    continue
                if value is not None:
                    values[key] = value
    finally:
        if owns:
            handle.close()
    return values


def _audit_system(conn: sqlite3.Connection, *, action: str, entity_id: str, after: dict) -> None:
    """Append a system-attributed event (no human actor) to the audit trail."""
    try:
        import json

        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, after_json, created_at) "
            "VALUES (NULL, 'system', ?, 'active_sessions', ?, ?, ?)",
            (action, str(entity_id), json.dumps(after, default=str), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    except sqlite3.Error:
        pass


def announce_crossing(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    site_name: str | None,
    clock_in_time: str | datetime | None,
    values: dict | None,
    moment: datetime | None = None,
    still_open: bool = False,
) -> dict:
    """Tell the worker their shift has passed the overtime line. One crossing, one notice.

    TWO KINDS OF CALLER, ONE SENTENCE
    ---------------------------------
    ``scan_overtime`` writes this for a shift that is **still running** (``still_open``),
    and every path that can *end* a shift writes it at the moment it makes the overtime
    decision: the punch, the quick link, the offline punch materialized hours later, and
    the clock-out an administrator forces.

    The watcher used to be the only writer, and that left a hole exactly the width of a
    timer: a worker who crossed the line and then clocked out before the next pass was
    never told at all. The administrator got their review alert (``review_pending``, which
    every one of those paths raises) and the worker got nothing - which is the asymmetry
    this channel exists to remove, and the way extra hours could be reviewed without the
    person who worked them ever learning they had.

    WHY THE SENTENCE STATES A RULE AND NOT AN OUTCOME
    --------------------------------------------------
    It names the event - you passed the line, at this instant - and the rule that follows -
    time past the paid day needs approval before it is paid. It deliberately does not claim
    what has happened to *these* hours, because on the force-clock-out path they have
    already been authorised by the administrator who ended the shift, and a notice reading
    "waiting for approval" would be false there. The one thing the two kinds of caller say
    differently is whether the shift is over: a shift still running says to clock out when
    they finish.

    THE ADMINISTRATOR'S HALF IS NOT WRITTEN HERE, ON PURPOSE
    --------------------------------------------------------
    ``KIND_OVERTIME_EXCEEDED`` is about a shift that is *still open* - its body says so
    ("the shift is still open and tracking", and, when the close has stood down, that
    nothing else will end it) - so it belongs to the watcher alone. A clock-out gives the
    administrator the alert they act on instead (``review_pending``, the review queue), and
    writing both would page them twice for one event.

    The figures are derived *here*, from the shift's own stored clock-in, rather than taken
    from what the caller says the shift was: the number in the sentence the worker reads has
    to be the one the gate, the queue and the report used, and the ``dedupe_key`` - the same
    key the watcher uses, because it names the shift rather than the caller - is what makes
    "a worker alerted on site and then clocking out is told once, not twice" true across the
    two writers.

    WHO CALLS IT, AND WHEN - THE ONE DIFFERENCE BETWEEN THE TWO KINDS OF CALLER
    ---------------------------------------------------------------------------
    The *watcher* announces whenever the line is reached, because its shift is still running
    and the point of telling somebody mid-shift is that they are still on site. A clock-out
    path announces when the hours are actually **held for a decision** (``needs_approval``),
    because the rule it states is about money: with an alert line set *below* the paid day, a
    shift that stops between the two crosses the line and is paid in full - nothing is being
    reviewed, so there is nothing to tell the worker about their pay. That is a deliberate
    difference and not an inconsistency: mid-shift the subject is "you are still working",
    at a clock-out it is "these hours need approval".

    Never raises: this runs inside a punch, and a notice that cannot be written must not
    fail the clock-out that produced it. ``push.dispatch_async`` is the caller's second
    step (see ``deliver_worker_notices``), after their transaction commits.
    """
    result = {
        "crossing": False,
        "announced": False,
        "worker_notified": False,
        "crossed_at": None,
        "threshold_hours": None,
        "paid_hours": None,
    }
    clock_in = _parse_ts(clock_in_time)
    if clock_in is None:
        return result
    stamp = moment or datetime.now()
    assessment = shift_hours.overtime_assessment(
        shift_hours.elapsed_seconds(clock_in, stamp), values
    )
    if not assessment["reaches_threshold"]:
        # Not short of the line but *at* it counts: ``>=`` in the resolver, the same second
        # the worker's own card and the administrator's alert go by.
        return result

    threshold = assessment["threshold_hours"]
    crossing = shift_hours.paid_limit_at(clock_in, threshold, values)
    crossing_str = crossing.strftime(_TS)
    result["crossing"] = True
    result["crossed_at"] = crossing_str
    result["threshold_hours"] = threshold
    result["paid_hours"] = assessment["paid_hours"]

    result["worker_notified"] = bool(
        notifications.notify_worker(
            conn,
            worker_id=worker_id,
            kind=notifications.KIND_WORKER_OVERTIME_CROSSED,
            title=f"You have reached {threshold:g} hours of work today",
            body=(
                f"Your shift on '{site_name}' passed {threshold:g} h of paid work at "
                f"{crossing_str}. Time past the {assessment['regular_hours']:g} h paid day "
                "needs your administrator's approval before it is paid"
                + (" - clock out when you finish." if still_open else ".")
            ),
            severity=notifications.SEVERITY_WARNING,
            payload={
                "basis": assessment["basis"],
                "threshold_hours": threshold,
                "regular_hours": assessment["regular_hours"],
                "paid_hours": assessment["paid_hours"],
                "overtime_hours": assessment["overtime_hours"],
                "crossed_at": crossing_str,
                "site_name": site_name,
                "still_open": bool(still_open),
            },
            # The shift, not the caller: ``YYYY-MM-DD HH:MM:SS`` is the format the clock-in
            # is stored in, so the watcher and a clock-out produce the identical key.
            dedupe_key=f"worker_overtime_live:{worker_id}:{clock_in.strftime(_TS)}",
        )
    )
    result["announced"] = True
    return result


def deliver_worker_notices(results) -> bool:
    """Try to reach the worker's phone, *after* the transaction that wrote the notice.

    Separate from the writer for a reason that is easy to get wrong: ``push`` selects rows
    that are not yet delivered, and inside the transaction that wrote one it cannot see it.
    So a caller writes inside its transaction and delivers after it commits - one notice (a
    punch, a forced clock-out) or a whole pass (the watcher, an offline batch), which is why
    this takes either. Every worker notice goes through here, whatever wrote it: the
    crossing (``announce_crossing``) and the auto-close, whose results look alike because
    both say whether a row was actually written (``worker_notified``).

    Returns whether a dispatch was started. Push not being configured is not an error and
    not a failure of the alert: the inbox row is the record, the push is only how it reaches
    a pocket (see ``push``'s own docstring).
    """
    if results is None:
        return False
    if isinstance(results, dict):
        results = [results]
    if not any(result and result.get("worker_notified") for result in results):
        return False
    return push.dispatch_async()


# ---------------------------------------------------------------------------
# the crossing as a question: the Approvals queue, and the answer to it
# ---------------------------------------------------------------------------
DECISION_AUTHORISED = "authorised"
DECISION_DECLINED = "declined"

#: The most one decision may authorise for one shift. Not a policy limit on overtime - it is the
#: width of a typo detector: 12 h where 1-2 was meant is recoverable by a second decision, and a
#: stray digit that would authorise three days of pay is not.
MAX_AUTHORISED_HOURS = 24.0


def live_decision(conn: sqlite3.Connection, worker_id: str, clock_in_time) -> sqlite3.Row | None:
    """The standing decision for *this* shift, or ``None``.

    Matched on the pair the shift is identified by everywhere else - the worker and the clock-in
    they are still inside - which is the same pair the old crossing notification's dedupe key
    used. That pairing is what stops a decision taken during yesterday's shift being read as
    authorisation for the one running now: the clock-in time differs, so it does not match, and
    the new shift is a new question.
    """
    return conn.execute(
        "SELECT * FROM overtime_authorisations WHERE worker_id = ? AND clock_in_time = ? "
        "AND consumed_by_log_id IS NULL AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
        (str(worker_id), str(clock_in_time)),
    ).fetchone()


#: How far past a ceiling a shift has to be before the hours beyond it are a *new question*.
#:
#: A ceiling authorises an amount measured at an instant, and the amount is stored to four decimal
#: places while time runs continuously. Without a stated tolerance the two would meet at the
#: sub-second level: a blank acceptance ("authorise the hours worked so far") would re-open its own
#: question within the same second, the queue would show a form for an answer just given, and the
#: operator would read that as the answer not having taken. Three minutes is the resolution a human
#: decision is made at here - the same step the band's decision lines are rounded to, stated
#: separately because the two are the same number for unrelated reasons - and it is deliberately far
#: below the smallest ceiling anybody says out loud (the console's own field steps by 0.25 h). It is
#: *not* a money tolerance: what a shift is paid stays ``paid - ceiling`` to four decimals, because
#: a tolerance on somebody's wages is a different decision from this one.
COVER_TOLERANCE_HOURS = 0.05


def _announce_decision(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    site_name: str | None,
    decision: str,
    ceiling: float,
    recorded: float,
    regular: float,
    actor_id: str,
    authorisation_id: int,
    superseded: dict | None,
    moment: datetime,
) -> bool:
    """Tell the worker what was decided about their shift, in their own inbox.

    THE NOTICE THE WORKER HAD WAS ABOUT THE WRONG EVENT
    ---------------------------------------------------
    ``announce_crossing`` says a line was crossed and that the time past it needs approval. That
    is a statement about the *shift* - true, and the last word the worker had until their
    clock-out, which is hours of not knowing whether staying on was paid. The decision is the
    answer, so it gets its own notice, and a distinct kind per answer: "authorised up to 10.5 h"
    and "no extra time is authorised" are opposite statements, and one kind carrying both could
    be neither counted nor read as either.

    THE OPERATOR'S NOTE IS DELIBERATELY NOT IN HERE
    ----------------------------------------------
    It is written for the record. The console says so on the refusal form, and ``approvalsHint``
    promises the worker never sees a reviewer's note - so shipping one to the worker through a
    channel built later would break that promise without anybody editing it. What the worker
    needs from a decision is the figure and who set it; *why* stays in ``overtime_authorisations``,
    where the operator wrote it knowing who reads it.

    The ``dedupe_key`` names the **decision**, not the shift. A ceiling an operator extends has to
    reach the worker twice, because the second answer is the one their clock-out settles at; a
    replay of the same request must not, and cannot, because the row it wrote is the same row.
    """
    name = _person_name(conn, actor_id) or str(actor_id)
    stamp = moment.strftime(_TS)
    if decision == DECISION_DECLINED:
        kind = notifications.KIND_WORKER_OVERTIME_DECLINED
        title = "No extra time was authorised for your shift"
        body = (
            f"{name} declined overtime for your shift on '{site_name}' at {stamp}: nothing past "
            f"the {regular:g} h paid day is authorised for it. You had worked {recorded:g} h when "
            "they answered, and any hours past the paid day are decided when you clock out."
        )
    else:
        kind = notifications.KIND_WORKER_OVERTIME_AUTHORISED
        title = f"Your extra time is authorised up to {ceiling:g} h"
        # The ceiling and the hours already worked are two different facts, and which one is
        # larger is what tells the worker whether the rest of their night is covered. A blank
        # acceptance authorises exactly what was worked, so that half of the sentence is the
        # one that tells them to ask for more if they are staying on.
        covered = (
            f" The rest of your shift is covered up to {ceiling:g} h."
            if ceiling > recorded + COVER_TOLERANCE_HOURS
            else " That is everything worked so far - ask for a larger figure if you are staying on."
        )
        body = (
            f"{name} authorised up to {ceiling:g} h of paid work for your shift on '"
            f"{site_name}' at {stamp}. You had worked {recorded:g} h when they answered."
            f"{covered} Hours past the ceiling are not authorised and come back for another "
            "decision. Clock out when you finish."
        )
    return notifications.notify_worker(
        conn,
        worker_id=worker_id,
        kind=kind,
        title=title,
        body=body,
        payload={
            "decision": decision,
            "authorised_hours": round(ceiling, 4),
            "recorded_hours_at_decision": recorded,
            "decided_by": str(actor_id),
            "decided_by_name": name,
            "decided_at": stamp,
            "authorisation_id": int(authorisation_id),
            "superseded_authorisation_id": (superseded or {}).get("id"),
            "site_name": site_name,
        },
        dedupe_key=f"worker_overtime_decision:{int(authorisation_id)}",
    )


def authorisation_covers(decision: sqlite3.Row | None, paid_hours: float) -> bool:
    """Whether a live decision still authorises everything this shift has worked.

    This is the one place that decides "does this shift need an answer?", and the queue and the
    writer both read it, because a rule stated twice is a rule the two can disagree about: the
    tab would show a form for a question a POST then answers with ``already_decided``, or hide
    the form for a question nothing else would ever ask again.

    AN AUTHORISATION IS AN AMOUNT, AND AN AMOUNT RUNS OUT
    ----------------------------------------------------
    Once the shift has worked past the ceiling somebody named, the hours beyond it are not
    authorised by anybody - which is a new question rather than a repeat of the answered one. So
    a blank acceptance ("authorise what has been worked") is a real answer that stops covering the
    shift once it has worked past that figure (the small tolerance in ``COVER_TOLERANCE_HOURS`` is
    the allowance for the ceiling having been measured at an instant), and a ceiling with room in
    it is what covers the rest of a night. Both are the same rule read at different moments.

    A REFUSAL IS A DECISION ABOUT THE DAY, AND A DAY DOES NOT RUN OUT
    -----------------------------------------------------------------
    ``declined`` covers the shift for its whole length, however long it runs. It answered the only
    question a refusal can answer - is this shift's overtime authorised - and the answer is no: the
    hours past the regular day are priced after the fact by the ordinary review, which is what the
    refusal said should happen. It is deliberately *not* the ceiling that makes a refusal cover, or
    a refused shift would re-ask the queue every few minutes and read to the operator as a system
    that had not heard them.
    """
    if decision is None:
        return False
    if decision["decision"] != DECISION_AUTHORISED:
        return True
    return float(paid_hours) <= float(decision["authorised_hours"]) + COVER_TOLERANCE_HOURS


def _person_name(conn: sqlite3.Connection | None, user_id) -> str | None:
    """The name behind an id, or the id itself when nobody holds it any more.

    The *id* is what the record keeps and what an audit answers with, so it is never replaced -
    this is the same value resolved for a human to read next to it.
    """
    if user_id is None:
        return None
    if conn is None:
        return str(user_id)
    row = conn.execute("SELECT name FROM users WHERE id = ?", (str(user_id),)).fetchone()
    # A decision outlives the account that made it (a deleted administrator still authorised
    # that overtime), so the id is the fallback rather than a blank.
    return row["name"] if row and row["name"] else str(user_id)


def _decision_payload(
    decision: sqlite3.Row | None, conn: sqlite3.Connection | None = None
) -> dict | None:
    """One decision for the wire: the answer, the amount, and the evidence it rested on."""
    if decision is None:
        return None
    return {
        "id": int(decision["id"]),
        "decision": decision["decision"],
        "authorised_hours": float(decision["authorised_hours"]),
        "recorded_hours_at_decision": float(decision["recorded_hours_at_decision"]),
        "decided_by": decision["decided_by"],
        # The queue prints this line to an operator, and "answered by 5000" is an id they would
        # have to look up - which is the one thing this queue exists to save them.
        "decided_by_name": _person_name(conn, decision["decided_by"]),
        "decided_at": decision["decided_at"],
        "note": decision["note"],
    }


def apply_authorisation(
    conn: sqlite3.Connection,
    worker_id: str,
    clock_in_time,
    assessment: dict,
    values: dict | None = None,
) -> dict:
    """Settle a clock-out at the ceiling somebody authorised, instead of at the regular day.

    With no decision this returns the assessment **unchanged**, and that is the property which
    makes the feature safe to ship: the ceiling defaults to the regular paid day, so
    ``max(0, paid - regular)`` is exactly what ``overtime_assessment`` already computed. Every
    existing clock-out path, and every test that pins its arithmetic, keeps the same numbers
    until an administrator actually answers a crossing.

    The alert line still gates the decision (``reaches_threshold``): the shipped configuration
    sits that line *above* the paid day on purpose, so a shift that stops between the two is paid
    in full and nothing is held. What changes is only the quantity held - past the ceiling rather
    than past the regular day - so a shift somebody authorised does not come back to the queue as
    the question they already answered.
    """
    decision = live_decision(conn, worker_id, clock_in_time)
    ceiling = float(decision["authorised_hours"]) if decision else float(assessment["regular_hours"])
    paid = float(assessment["paid_hours"] or 0.0)
    held = round(max(0.0, paid - ceiling), 4)
    settled = dict(assessment)
    settled["overtime_hours"] = held
    settled["needs_approval"] = bool(assessment["reaches_threshold"]) and held > 0
    settled["authorised_hours"] = float(decision["authorised_hours"]) if decision else None
    settled["authorisation"] = _decision_payload(decision, conn)
    return settled


def consume_authorisation(
    conn: sqlite3.Connection, worker_id: str, clock_in_time, log_id: int
) -> int | None:
    """Mark the decision for this shift as cashed in by the clock-out that settled it.

    WHY IT IS A STAMP AND NOT A DELETE
    ----------------------------------
    ``live_decision`` matches only a row nothing has consumed, so this is what stops a
    standing answer being read as authorisation for a shift that has already been paid - the
    clock-in pair keeps that boundary explicit, so the closing of *this* shift consumes *this*
    decision and the worker's next shift is a new question. The stamp also keeps the record:
    which clock-out cashed the answer in is answerable from the authorisation row itself,
    months later, without joining a change log.

    Returns the log id when it stamped something, ``None`` when there was nothing to stamp -
    which is the ordinary case, since most shifts are closed without anybody answering
    anything about them. A decision already consumed, or superseded by a later one, is left
    alone: the first answer is the one that settled the shift.
    """
    cursor = conn.execute(
        "UPDATE overtime_authorisations SET consumed_by_log_id = ? "
        "WHERE worker_id = ? AND clock_in_time = ? AND consumed_by_log_id IS NULL "
        "AND superseded_by IS NULL",
        (int(log_id), str(worker_id), str(clock_in_time)),
    )
    return int(log_id) if cursor.rowcount else None


def open_crossings(*, now: datetime | None = None) -> list[dict]:
    """Every open shift past the overtime line, as the Approvals queue's own items.

    DERIVED, NOT STORED
    -------------------
    An open shift's crossing state is already computable - from the shift and the rules, through
    the same ``overtime_assessment`` the gate, the watcher and the alert read - so the queue asks
    the shift rather than a stored copy of the answer. A stored row would be a second answer that
    can disagree with the first, and it would need a dismissal, an expiry and a sweeper to stop
    it outliving the shift it describes.

    Oldest crossing first: the item that has been waiting longest is the one an operator should
    see first, and waiting time is the only ordering that is stable while the figures move. Never
    raises, because a tab reads it and a queue that 500s is one nobody can use to find out what
    is wrong.
    """
    moment = now or datetime.now()
    items: list[dict] = []
    try:
        with db() as conn:
            values = rules(conn)
            line = shift_hours.overtime_rule(values)
            day_end = shift_hours.day_end_rules(values)
            sessions = conn.execute(
                "SELECT worker_id, site_name, clock_in_time FROM active_sessions"
            ).fetchall()
            for session in sessions:
                clock_in = _parse_ts(session["clock_in_time"])
                if clock_in is None:
                    continue
                assessment = shift_hours.overtime_assessment(
                    shift_hours.elapsed_seconds(clock_in, moment), values
                )
                if not assessment["reaches_threshold"]:
                    continue
                decision = live_decision(conn, session["worker_id"], session["clock_in_time"])
                paid = float(assessment["paid_hours"] or 0.0)
                covered = authorisation_covers(decision, paid)
                unauthorised = (
                    round(paid - float(decision["authorised_hours"]), 4)
                    if decision is not None and decision["decision"] == DECISION_AUTHORISED and not covered
                    else None
                )
                person = conn.execute(
                    "SELECT name, role FROM users WHERE id = ?", (session["worker_id"],)
                ).fetchone()
                crossing = shift_hours.paid_limit_at(clock_in, line["threshold_hours"], values)
                items.append(
                    {
                        "worker_id": session["worker_id"],
                        "worker_name": person["name"] if person else session["worker_id"],
                        "role": person["role"] if person else None,
                        "site_name": session["site_name"],
                        "clock_in_time": session["clock_in_time"],
                        "crossed_at": crossing.strftime(_TS),
                        "waiting_seconds": int((moment - crossing).total_seconds()),
                        "elapsed_hours": round(float(assessment["elapsed_hours"]), 4),
                        "paid_hours": round(float(assessment["paid_hours"]), 4),
                        "break_hours": round(float(assessment["break_taken_hours"]), 4),
                        "threshold_hours": line["threshold_hours"],
                        "regular_hours": assessment["regular_hours"],
                        "overtime_hours": round(float(assessment["overtime_hours"]), 4),
                        "basis": assessment["basis"],
                        # Whether anything but a human will end this shift. With the close
                        # standing down, the answer is no - and an open shift refuses the next
                        # clock-in, so the operator is told rather than left to discover it.
                        "close_defers": bool(day_end["close_defers"]),
                        "decision": _decision_payload(decision, conn),
                        # Whether this row is a question right now. A crossing with a decision that
                        # still covers the shift is information rather than a decision, and the two
                        # states have to be told apart on the wire: the card draws its answer either
                        # way, and offers the form only where the answer has run out.
                        "needs_answer": not covered,
                        # What nobody has authorised yet - the figure the operator is being asked
                        # about. ``None`` on a first question, where nothing was ever authorised (the
                        # answer there is about the whole crossing, not about the excess), and on a
                        # refusal, whose excess is the ordinary post-hoc review's business.
                        "unauthorised_hours": unauthorised,
                    }
                )
    except sqlite3.Error as exc:  # pragma: no cover - a queue must not 500 the tab
        log.warning("could not list open crossings: %s", exc)
        return []
    items.sort(key=lambda item: item["crossed_at"])
    return items


def decide_crossing(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    accept: bool,
    authorised_hours: float | None = None,
    note: str | None = None,
    actor_id: str,
    now: datetime | None = None,
) -> dict:
    """Answer a crossing: authorise a ceiling for this shift, or refuse one.

    THE FIRST ANSWER IS THE ONE KEPT, UNTIL THE SHIFT OUTGROWS IT
    ------------------------------------------------------------
    A second decision while the standing one still covers the shift is neither an error nor an
    overwrite: the standing decision is returned with ``already_decided`` set, the same way the
    forced-start acknowledgement answers a second attempt. Silently replacing it would leave the
    queue describing a decision nobody made and the trail unable to say who authorised what.

    Past the ceiling it is a second *question*, not a repeat. The boundary between the two is
    ``authorisation_covers`` and it carries a stated tolerance (``COVER_TOLERANCE_HOURS``): the
    ceiling is an amount measured at an instant and the comparison is continuous, so without one
    a blank acceptance would reopen its own question within the same second. Past the boundary the
    new row is written and the row it replaces stays in the trail with ``superseded_by`` pointing
    at it. Append-only, because what an extension was measured against is the ceiling it extended -
    a row updated in place would answer "who authorised 12 h" with one name and no history of the
    decision that was there first.

    THE CEILING DEFAULTS TO WHAT HAS BEEN WORKED
    --------------------------------------------
    ``accept`` without a number authorises the paid hours recorded *at that moment* - the rule
    this application applies to money elsewhere: approve what there is evidence for. An operator
    who wants the rest of the night covered says so with a number, and that is a deliberate act
    with a figure on it rather than a blanket. Hours past the ceiling are not silently paid: they
    queue again as a second, distinct question, with this decision on the record as what they
    were measured against.

    A refusal is stored as a ceiling of the regular paid day rather than as a missing row:
    "nobody has answered" and "the answer is no" are different states of the same queue, and only
    one of them should keep asking.

    Raises ``LookupError`` for a shift that cannot be answered (no open shift, an unreadable
    clock-in, one that has not crossed the line) and ``ValueError`` for a ceiling that cannot be
    meant - both are the caller's to translate into an HTTP answer.
    """
    moment = now or datetime.now()
    session = conn.execute(
        "SELECT worker_id, site_name, clock_in_time FROM active_sessions WHERE worker_id = ?",
        (str(worker_id),),
    ).fetchone()
    if session is None:
        raise LookupError("this worker has no open shift")
    clock_in = _parse_ts(session["clock_in_time"])
    if clock_in is None:
        raise LookupError("this shift's clock-in time cannot be read")
    values = rules(conn)
    assessment = shift_hours.overtime_assessment(
        shift_hours.elapsed_seconds(clock_in, moment), values
    )
    if not assessment["reaches_threshold"]:
        raise LookupError("this shift has not crossed the overtime line")

    standing = live_decision(conn, worker_id, session["clock_in_time"])
    recorded = round(float(assessment["paid_hours"]), 4)
    if standing is not None and authorisation_covers(standing, recorded):
        answered = _decision_payload(standing, conn) or {}
        answered.update(
            {"worker_id": str(worker_id), "clock_in_time": session["clock_in_time"], "already_decided": True}
        )
        return answered
    if accept:
        ceiling = float(authorised_hours) if authorised_hours is not None else recorded
        if ceiling < recorded:
            raise ValueError(
                f"authorised hours ({ceiling:g}) are less than the {recorded:.2f} h already worked"
            )
        if ceiling > MAX_AUTHORISED_HOURS:
            raise ValueError(
                f"authorised hours ({ceiling:g}) are past the {MAX_AUTHORISED_HOURS:g} h limit "
                "for one shift"
            )
        decision = DECISION_AUTHORISED
    else:
        ceiling = float(assessment["regular_hours"])
        decision = DECISION_DECLINED

    stamp = moment.strftime(_TS)
    superseded = None
    successor_id = None
    if standing is not None:
        # The standing answer is replaced as *live* and kept as history. The id has to exist before
        # the row does, because the table allows exactly one live decision per shift (the partial
        # unique index) and the incoming row would collide with the outgoing one: the earlier
        # answer stands down first, and it is stamped with the id the next row is about to take
        # rather than with a placeholder that was never a decision. Reserved inside the caller's
        # write transaction, which is the same lock every other writer here takes.
        successor_id = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM overtime_authorisations"
            ).fetchone()[0]
        )
        superseded = {
            "id": int(standing["id"]),
            "decision": standing["decision"],
            "authorised_hours": float(standing["authorised_hours"]),
            "recorded_hours_at_decision": float(standing["recorded_hours_at_decision"]),
            "decided_by": standing["decided_by"],
        }
        conn.execute(
            "UPDATE overtime_authorisations SET superseded_by = ? WHERE id = ?",
            (successor_id, int(standing["id"])),
        )

    # ``successor_id`` is ``None`` for a first answer, and NULL into an INTEGER PRIMARY KEY is how
    # SQLite is told to assign one - so the first decision and a superseding one are one statement.
    cursor = conn.execute(
        "INSERT INTO overtime_authorisations "
        "(id, worker_id, clock_in_time, decision, authorised_hours, recorded_hours_at_decision, "
        "decided_by, decided_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            successor_id,
            str(worker_id),
            session["clock_in_time"],
            decision,
            round(ceiling, 4),
            recorded,
            str(actor_id),
            stamp,
            note,
        ),
    )
    row_id = int(successor_id) if successor_id is not None else int(cursor.lastrowid)

    # The worker is told, in the same transaction as the decision itself: an answer written and
    # not delivered is the state this notice exists to end, and the push (a network call to a
    # third party) is what the caller does after this commits - see ``deliver_worker_notices``.
    worker_notified = _announce_decision(
        conn,
        worker_id=str(worker_id),
        site_name=session["site_name"],
        decision=decision,
        ceiling=round(ceiling, 4),
        recorded=recorded,
        regular=float(assessment["regular_hours"]),
        actor_id=str(actor_id),
        authorisation_id=row_id,
        superseded=superseded,
        moment=moment,
    )
    return {
        "worker_id": str(worker_id),
        "clock_in_time": session["clock_in_time"],
        "decision": decision,
        "authorised_hours": round(ceiling, 4),
        "recorded_hours_at_decision": recorded,
        "decided_by": str(actor_id),
        "decided_at": stamp,
        "note": note,
        "already_decided": False,
        # What this answer replaced, when the shift had outgrown an earlier one: the endpoint puts
        # it in the audit's ``before``, so the trail says what the new ceiling was measured against.
        "superseded": superseded,
        # Whether the worker's own notice was a new row, which is what the caller reads to decide
        # whether to dispatch a push after committing (``deliver_worker_notices``).
        "authorisation_id": row_id,
        "worker_notified": worker_notified,
    }


def scan_overtime(*, now: datetime | None = None) -> dict:
    """Notify once per open shift that has crossed the overtime threshold.

    Returns a summary; never raises, because it runs from a timer thread where an
    exception would silently stop all future monitoring.
    """
    moment = now or datetime.now()
    summary = {
        "scanned": 0,
        "notified": 0,
        "already_notified": 0,
        "errors": 0,
        "notifications": [],
        #: Whether a crossing can be reached at all under the current rules. Reported rather
        #: than used as a guard: a shift that somehow *is* past the threshold (the close was
        #: switched on after it started) should still be reported, and silently skipping the
        #: scan is how a watcher stops watching without anybody noticing.
        "alert_reachable": True,
        "day_end": None,
        #: How many *worker* notifications this pass wrote - the other half of the crossing
        #: alert, addressed to the person it happened to (see ``notifications.notify_worker``).
        "worker_notified": 0,
    }
    #: The worker notices this pass wrote, so ``deliver_worker_notices`` can push them once
    #: the transaction below has committed.
    crossings: list[dict] = []
    try:
        with db(write=True) as conn:
            values = rules(conn)
            # One statement of which rule owns the end of the day, shared with the close.
            day_end = shift_hours.day_end_rules(values)
            # ... and one statement of what the overtime line counts and where it sits,
            # shared with every clock-out path. ``day_end`` names the line; the assessment
            # is what decides whether a given shift has reached it, so the alert and the
            # approval gate cannot disagree about the same second.
            line = shift_hours.overtime_rule(values)
            threshold = line["threshold_hours"]
            summary["alert_reachable"] = day_end["alert_reachable"]
            summary["day_end"] = day_end
            summary["overtime_rule"] = line

            sessions = conn.execute(
                "SELECT worker_id, site_name, clock_in_time, overtime_notified_at FROM active_sessions"
            ).fetchall()
            for session in sessions:
                summary["scanned"] += 1
                clock_in = _parse_ts(session["clock_in_time"])
                if clock_in is None:
                    continue
                # Whole seconds on site, and the paid figure derived from them, because
                # that is the resolution the timestamps have and the basis every clock-out
                # path judges a shift on: 8.6 h on site with a 30-minute unpaid break is
                # 8.1 h of work, and a normal full day (8.5 h on site) must never page
                # anybody. Reading this from ``shift_hours.overtime_assessment`` rather
                # than recomputing it here is what keeps this scan and the clock-out gate
                # agreeing about a shift that stops within a second of the line.
                assessment = shift_hours.overtime_assessment(
                    shift_hours.elapsed_seconds(clock_in, moment), values
                )
                elapsed = assessment["elapsed_hours"]
                paid = assessment["paid_hours"]
                break_taken = assessment["break_taken_hours"]
                if not assessment["reaches_threshold"]:
                    continue
                if session["overtime_notified_at"]:
                    summary["already_notified"] += 1
                    continue

                crossing = shift_hours.paid_limit_at(clock_in, threshold, values)
                crossing_str = crossing.strftime("%Y-%m-%d %H:%M:%S")
                # The crossing is deliberately *not* written as an administrator notification
                # any more (``KIND_OVERTIME_EXCEEDED``, retired below). It was a live question
                # sitting in the tab built for notices: reading it changed nothing, the shift
                # stayed open whether or not anybody looked, and the answer it needed had no
                # place to be recorded. It now appears in the Approvals queue as what it is -
                # an open shift past the line, waiting for a decision - read from the shift
                # itself (``open_crossings``) and answered in ``overtime_authorisations``.
                # The event is still audited below and the worker is still told; what moved is
                # only where the *administrator* is asked.
                created = False

                # ... and the worker is told too, in their own inbox. Until this existed the
                # only way a worker learned they had crossed the line was to be looking at
                # their own clock card at the moment it tipped - and the administrator was
                # told about their shift while they themselves never were, which is the one
                # asymmetry this channel exists to remove.
                #
                # Written in the same transaction as the crossing's audit row, on purpose: two
                # events that describe the same crossing must appear together or not at all. The
                # sentence, the figures and the dedupe key are ``announce_crossing``'s, shared
                # with every clock-out path - this is the caller for a shift that is *still
                # running*, which is the one thing the two kinds of caller say differently.
                crossing_result = announce_crossing(
                    conn,
                    worker_id=session["worker_id"],
                    site_name=session["site_name"],
                    clock_in_time=session["clock_in_time"],
                    values=values,
                    moment=moment,
                    still_open=True,
                )
                crossings.append(crossing_result)
                if crossing_result["worker_notified"]:
                    summary["worker_notified"] += 1

                # Stamped even when the notification was deduped, so the watcher does
                # not re-evaluate this shift every interval.
                conn.execute(
                    "UPDATE active_sessions SET overtime_notified_at = ? WHERE worker_id = ?",
                    (crossing_str, session["worker_id"]),
                )
                _audit_system(
                    conn,
                    action="overtime_detected",
                    entity_id=session["worker_id"],
                    after={
                        "threshold_hours": threshold,
                        "elapsed_hours": round(elapsed, 4),
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "crossed_at": crossing_str,
                        "site": session["site_name"],
                        "notification_created": bool(created),
                    },
                )
                summary["notified"] += 1
                summary["notifications"].append(
                    {
                        "worker_id": session["worker_id"],
                        "crossed_at": crossing_str,
                        "elapsed_hours": round(elapsed, 4),
                        "created": bool(created),
                    }
                )
    except Exception as exc:  # pragma: no cover - defensive: keep the timer alive
        log.warning("overtime scan failed: %s", exc)
        summary["errors"] += 1
        summary["error"] = f"{type(exc).__name__}: {exc}"

    if summary["worker_notified"]:
        # *After* the transaction that wrote them has committed, and on its own thread: a
        # push is an HTTPS call to a phone vendor's service, and this pass may be holding a
        # worker's clock-out or the SQLite writer lock. Nothing here waits for it, and a
        # push that fails changes nothing about the notification - the inbox row is the
        # record, the push is only how it reaches a pocket.
        deliver_worker_notices(crossings)
    return summary


#: How late the watcher may be before a close is worth flagging on the shift. The
#: timer runs every minute, so a few seconds of lateness is the timer working normally;
#: quarter of an hour means the shift was left open long enough that somebody might have
#: been on site past the recorded hours, which is the case the administrator should see.
LATE_TOLERANCE_HOURS = 0.25

_TS = "%Y-%m-%d %H:%M:%S"


def _insert_auto_close_log(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    site_name: str,
    timestamp: str,
    hours: float,
    break_hours: float,
    flag_reason: str | None,
) -> int:
    """Write the clock-out the system is making on the worker's behalf.

    Its own INSERT rather than ``main._insert_log`` because ``main`` imports *this*
    module, and a real clock-out row - not a marker - is the point: the timesheet, the
    monthly report and the payroll query all read one table, and a synthetic "the system
    closed this" row somewhere else would have to be joined back into all three.
    """
    cursor = conn.execute(
        """
        INSERT INTO attendance_logs
            (worker_id, site_name, action, timestamp, hours, score, status, status_code,
             source, flag_reason, break_hours)
        VALUES (?, ?, 'Clock Out', ?, ?, 0.0, ?, ?, 'auto_close', ?, ?)
        """,
        (
            worker_id,
            site_name,
            timestamp,
            hours,
            migrations.STATUS_AUTO_CLOSED_LABEL,
            migrations.STATUS_CODE_AUTO_CLOSED_8H,
            flag_reason,
            break_hours or None,
        ),
    )
    return int(cursor.lastrowid or 0)


def scan_auto_close(*, now: datetime | None = None) -> dict:
    """End every open shift whose paid hours have reached the limit that shift may run to.

    That limit is the paid day, or the ceiling somebody authorised for the shift while it was
    running: an answer to a crossing is a permission to work past the paid day, so the close
    waits inside the window it was given and ends the day at the ceiling when the window runs
    out. Standing down entirely would be the wrong reading of the same answer - a shift that
    nothing can bound is how a session nobody remembers runs for a week.

    Stands down entirely when ``day_end_rules`` says the close defers to the overtime
    workflow (``close_defers``): closing at the paid limit would end the shift before it
    could be observed crossing the alert line, and observing that crossing is the other
    rule's whole job. The summary says so (``deferred``, with the reason) rather than
    returning a quiet zero that looks like a watcher with nothing to do - and a shift being
    held inside an authorised window is reported the same way (``holding``, with the window).

    Returns a summary; never raises, for the same reason ``scan_overtime`` does not: it
    runs in a timer thread, and an exception would stop the monitoring for good. The
    clock-out is written at the *boundary* - the moment the limit ran out - rather
    than at the moment this happened to run, so a server restarted at lunchtime does not
    record an eight-hour shift as ending at noon.

    Both halves of the event are written here, in one transaction: the administrator's
    alert and the worker's own notice (``KIND_WORKER_SHIFT_AUTO_CLOSED``). That is the
    whole point of the second one - a worker whose day the system ended must not have to
    find out from their next clock-out being refused, which reads like data loss.
    """
    moment = now or datetime.now()
    summary = {
        "enabled": True,
        "scanned": 0,
        "closed": 0,
        "skipped": 0,
        "errors": 0,
        "shifts": [],
        #: How many *worker* notices this pass wrote - the person whose day it ended, told
        #: directly instead of being left to the clock-out refusal that follows a close.
        "worker_notified": 0,
        #: Set when the close yields the end of the day to the overtime workflow because the
        #: alert line sits above the paid day: closing at the paid limit would end the shift
        #: before it could ever be observed crossing that line. See ``day_end_rules``.
        "deferred": False,
        "day_end": None,
        #: Shifts left open because somebody authorised them to run past the paid day: the close
        #: is *waiting* for the window it was given, not idle. Counted, and described below,
        #: because a watcher holding a shift on purpose looks exactly like a watcher with
        #: nothing to do - and the summary is how an operator tells those apart.
        "holding": 0,
        "authorised": [],
    }
    #: The worker notices this pass wrote, so ``deliver_worker_notices`` can push them once
    #: the transaction below has committed.
    closures: list[dict] = []
    try:
        with db(write=True) as conn:
            values = rules(conn)
            day_end = shift_hours.day_end_rules(values)
            summary["day_end"] = day_end
            if not shift_hours.auto_close_enabled(values):
                summary["enabled"] = False
                return summary
            if day_end["close_defers"]:
                # Standing down is the whole reconciliation: a shift ended here could not be
                # reported as crossing the alert line, and the crossing is the point of the
                # other rule. The shifts stay open for the overtime workflow to see.
                summary["deferred"] = True
                summary["deferred_reason"] = day_end["detail"]
                return summary

            regular = shift_hours.regular_hours(values)
            sessions = conn.execute(
                "SELECT worker_id, site_name, clock_in_time FROM active_sessions"
            ).fetchall()
            for session in sessions:
                summary["scanned"] += 1
                clock_in = _parse_ts(session["clock_in_time"])
                if clock_in is None:
                    continue
                # The end of the day this shift is allowed to reach. Without an answer it is
                # the paid day, which is the boundary this rule has always used. With one, the
                # ceiling somebody named for *this* shift moves the end of the day out to it -
                # a decline records the paid day, so a refused crossing ends the day exactly
                # where it would have ended anyway, while an approval is a shift the operator
                # said could run on. The close therefore waits *inside the window* rather than
                # standing down entirely: an answer that nothing could ever bound is how a
                # session nobody remembers runs for a week.
                decision = live_decision(conn, session["worker_id"], session["clock_in_time"])
                # Never below the paid day: the hours are already worked, and no answer makes
                # a worked day worth less than the standard one.
                limit = max(regular, float(decision["authorised_hours"])) if decision else regular
                elapsed = max(0.0, (moment - clock_in).total_seconds() / 3600.0)
                # Deliberately the *unrounded* hours: this is a gate, not a record. With
                # the rounding added in, a shift 15 minutes short of the day would look
                # like it had reached the limit and be closed at a boundary still in the
                # future. The rounding is applied below, where the hours are written.
                paid_now, _ = shift_hours.paid_hours(elapsed, values)
                if paid_now < limit:
                    summary["skipped"] += 1
                    if decision is not None:
                        summary["holding"] += 1
                        summary["authorised"].append(
                            {
                                "worker_id": session["worker_id"],
                                "clock_in_time": session["clock_in_time"],
                                "paid_hours": round(paid_now, 4),
                                "authorised_hours": limit,
                                "closes_at": shift_hours.paid_limit_at(
                                    clock_in, limit, values
                                ).strftime(_TS),
                                "decided_by": decision["decided_by"],
                            }
                        )
                    continue

                boundary = shift_hours.paid_limit_at(clock_in, limit, values)
                closed_at = min(boundary, moment)
                close_elapsed = max(0.0, (closed_at - clock_in).total_seconds() / 3600.0)
                record = shift_hours.recorded_shift(close_elapsed, values)
                paid = record["paid_hours"]
                break_taken = record["break_hours"]
                late_by = max(0.0, round((moment - boundary).total_seconds() / 3600.0, 4))
                closed_str = closed_at.strftime(_TS)

                name = conn.execute(
                    "SELECT name FROM users WHERE id = ?", (session["worker_id"],)
                ).fetchone()
                display = name["name"] if name else session["worker_id"]

                flag = None
                if late_by > LATE_TOLERANCE_HOURS:
                    flag = (
                        f"shift auto-closed at the {limit:g}h paid limit; still open "
                        f"{late_by:.2f}h later when the watcher ran, so time past "
                        f"{closed_str} is not recorded"
                    )

                conn.execute(
                    "DELETE FROM active_sessions WHERE worker_id = ?", (session["worker_id"],)
                )
                log_id = _insert_auto_close_log(
                    conn,
                    worker_id=session["worker_id"],
                    site_name=session["site_name"],
                    timestamp=closed_str,
                    hours=paid,
                    break_hours=break_taken,
                    flag_reason=flag,
                )
                # The shift the decision answered has settled, so the decision is cashed in by
                # the very row written for it - which is also what stops it being read as an
                # answer to the worker's *next* shift. Nothing to stamp when no answer was
                # given, which is most closes.
                consume_authorisation(
                    conn, session["worker_id"], session["clock_in_time"], log_id
                )
                notifications.notify(
                    conn,
                    kind=notifications.KIND_SHIFT_AUTO_CLOSED,
                    severity=notifications.SEVERITY_INFO,
                    title=f"Shift closed automatically at {limit:g} paid hours",
                    body=(
                        f"{display} (id {session['worker_id']}) reached {limit:g}h paid on "
                        f"'{session['site_name']}' and the shift was closed at {closed_str} "
                        f"({record['description']}). "
                        + (
                            "That limit is the ceiling somebody authorised for this shift while "
                            "it was still running. "
                            if decision is not None
                            else ""
                        )
                        + (
                            f"It was still open {late_by:.2f}h past that limit when the watcher "
                            "ran, so any time after it is not recorded - adjust the shift if it "
                            "was worked."
                            if late_by > LATE_TOLERANCE_HOURS
                            else "Nothing more will be recorded for it."
                        )
                    ),
                    worker_id=session["worker_id"],
                    site_name=session["site_name"],
                    log_id=log_id,
                    payload={
                        "log_id": log_id,
                        "regular_hours": regular,
                        "authorised_hours": limit if decision is not None else None,
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "elapsed_hours": round(close_elapsed, 4),
                        "closed_at": closed_str,
                        "clock_in_time": session["clock_in_time"],
                        "late_by_hours": late_by,
                    },
                    dedupe_key=f"auto_close:{session['worker_id']}:{session['clock_in_time']}",
                )

                # ... and the worker whose day this was. The close is the one path that ends
                # a shift with nobody asking it to, so it is the one path whose notice has to
                # *say so*: every other ending is a person watching their own clock-out. Told
                # here rather than discovered at the next clock-out, where the refusal ("no
                # open shift") arrives to somebody who believes they are still working.
                #
                # Same transaction as the administrator's alert, for the same reason the
                # crossing writes both together: two notices about one closing must appear
                # together or not at all. Addressed by kind, not by the crossing channel -
                # the closing figures are the paid day, so a crossing sentence (which is
                # about hours past the day held for approval) would contradict the timesheet.
                closed_notice = notifications.notify_worker(
                    conn,
                    worker_id=session["worker_id"],
                    kind=notifications.KIND_WORKER_SHIFT_AUTO_CLOSED,
                    title=f"Your shift was closed automatically at the {limit:g}h paid limit",
                    body=(
                        f"The system closed your shift on '{session['site_name']}' at "
                        f"{closed_str}: {record['description']}. Nothing more is recorded "
                        "for it, and your next clock-out will find no open shift - this one "
                        "is already on your timesheet."
                        + (
                            f" You were still on the clock {late_by:.2f}h past the "
                            f"{limit:g}h limit, so time after {closed_str} is not "
                            "recorded - tell your administrator if you kept working."
                            if late_by > LATE_TOLERANCE_HOURS
                            else " Clock in again when you start your next shift."
                        )
                    ),
                    # No ``severity``: the worker inbox has no such column (see
                    # ``notifications.notify_worker``), so the distinction between an
                    # ordinary close and one that dropped worked time is carried by the
                    # sentence, which is what the worker actually reads.
                    payload={
                        "log_id": log_id,
                        "site_name": session["site_name"],
                        "regular_hours": regular,
                        "authorised_hours": limit if decision is not None else None,
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "elapsed_hours": round(close_elapsed, 4),
                        "closed_at": closed_str,
                        "clock_in_time": session["clock_in_time"],
                        "late_by_hours": late_by,
                    },
                    # The shift, like the crossing's key - so a watch that re-runs over the
                    # same closing cannot tell the worker twice.
                    dedupe_key=f"worker_auto_close:{session['worker_id']}:{session['clock_in_time']}",
                )
                closures.append({"worker_notified": bool(closed_notice), "worker_id": session["worker_id"]})
                if closed_notice:
                    summary["worker_notified"] += 1

                _audit_system(
                    conn,
                    action="shift_auto_closed",
                    entity_id=session["worker_id"],
                    after={
                        "log_id": log_id,
                        "site": session["site_name"],
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "elapsed_hours": round(close_elapsed, 4),
                        "closed_at": closed_str,
                        "late_by_hours": late_by,
                    },
                )
                summary["closed"] += 1
                summary["shifts"].append(
                    {
                        "worker_id": session["worker_id"],
                        "log_id": log_id,
                        "closed_at": closed_str,
                        "paid_hours": paid,
                        "break_hours": break_taken,
                        "late_by_hours": late_by,
                        "worker_notified": bool(closed_notice),
                    }
                )
    except Exception as exc:  # pragma: no cover - defensive: keep the timer alive
        log.warning("auto-close scan failed: %s", exc)
        summary["errors"] += 1
        summary["error"] = f"{type(exc).__name__}: {exc}"

    if summary["worker_notified"]:
        # *After* the transaction that wrote them has committed, and on its own thread, for
        # the reasons ``scan_overtime`` does the same: a push is an HTTPS call to a phone
        # vendor's service, and this pass may be holding the SQLite writer lock. Nothing
        # here waits for it, and a push that fails changes nothing about the notification -
        # the inbox row is the record, the push is only how it reaches a pocket.
        deliver_worker_notices(closures)
    return summary


def _loop(interval: int) -> None:
    """Timer body: closes shifts that reached the paid limit, then alerts on the rest.

    That order is deliberate. The close runs first because it is the rule that owns the end
    of the day when it is allowed to act at all, and the alert looks at what is left. A
    deferral is the one case where nothing is closed, and it is reported here rather than
    left to look like an idle pass.
    """
    while not _stop_event.is_set():
        try:
            closed = scan_auto_close()
            if closed.get("deferred"):
                log.info(
                    "auto-close standing down for the overtime workflow: %s",
                    closed.get("deferred_reason", "the alert line is above the paid day"),
                )
            elif closed.get("closed"):
                log.info("auto-close ended %s shift(s) at the paid limit", closed["closed"])
            summary = scan_overtime()
            if summary.get("notified"):
                log.info("overtime watcher raised %s notification(s)", summary["notified"])
            # The other clock the push backlog is read on (``push.dispatch`` is the first). It
            # has to be here as well as there, because a channel that has gone quiet is exactly
            # the case where nobody is doing anything: the punches that would otherwise take the
            # reading stop arriving at the same moment the notices do.
            backlog = push.alert_stranded_notices()
            if backlog.get("alerted"):
                log.warning(
                    "worker push has left %s notification(s) undelivered past the %s-minute window "
                    "(oldest %s); the operator has been told",
                    backlog["notices"],
                    backlog["window_minutes"],
                    backlog["age"],
                )
        except Exception as exc:  # pragma: no cover
            log.warning("overtime watcher pass failed: %s", exc)
        _stop_event.wait(max(5, int(interval)))


def start_watcher(*, interval: int | None = None, enabled: bool | None = None) -> bool:
    """Start the daemon timer. Returns whether it is running.

    Skipped when ``OVERTIME_WATCHER_ENABLED=0``, which is what the test suite and a
    read-only replica want: the watcher writes to the database, and a test run must
    not mutate state on a timer.

    The precedence is announced here, once, for the same reason it is published to the
    console and to readiness: an alert that cannot fire is indistinguishable from an alert
    that has nothing to report, and the difference is entirely in the settings. Logged at
    WARNING when unreachable, because "the overtime crossing is being watched" would
    otherwise be the only thing this line ever said.
    """
    global _thread
    on = settings.overtime_watcher_enabled if enabled is None else enabled
    if not on:
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event.clear()
    seconds = int(interval if interval is not None else settings.overtime_watcher_interval_seconds)

    try:
        day_end = shift_hours.day_end_rules(rules())
        if not day_end["alert_reachable"]:
            log.warning(
                "overtime watcher started (interval %ss) but the crossing alert is "
                "unreachable: %s",
                seconds,
                day_end["detail"],
            )
        elif day_end["close_defers"]:
            # Not a failure - it is the reconciliation - but it changes what the settings do,
            # so it is not logged at INFO where "the watcher is running" would be the only
            # thing an operator ever saw.
            log.warning(
                "overtime watcher started (interval %ss); the automatic close has stood "
                "down for the overtime workflow: %s",
                seconds,
                day_end["detail"],
            )
        else:
            log.info("overtime watcher started (interval %ss): %s", seconds, day_end["detail"])
    except Exception as exc:  # pragma: no cover - the timer must start regardless
        log.warning("could not read the day-end rules: %s", exc)

    _thread = threading.Thread(target=_loop, args=(seconds,), name="overtime-watcher", daemon=True)
    _thread.start()
    return True


def stop_watcher(*, timeout: float = 2.0) -> None:
    """Signal the timer to stop and wait briefly for it (used at shutdown)."""
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
        _thread = None


def watcher_running() -> bool:
    return _thread is not None and _thread.is_alive()
