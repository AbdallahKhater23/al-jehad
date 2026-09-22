"""What a shift is worth: the unpaid break, and the hour the day ends at.

THE POLICY, IN ONE PLACE
------------------------
A full day on this site is **8 h of paid work plus a 30-minute break that is not paid**.
A full day therefore runs 8.5 h from clock-in to clock-out, and the two numbers are kept
separate on purpose:

* ``regular_hours`` (8.0) is what is paid;
* ``break_minutes`` (30) is what is *deducted and recorded*. The break is stored on the
  shift (``attendance_logs.break_hours``) rather than silently subtracted, because an
  administrator looking at a shift that ran 8.5 h on site needs to see where the half
  hour went, and a worker asking why their day reads 8.0 h deserves a better answer than
  "the system says so";
* ``auto_close_at_regular`` decides whether the system closes the shift when the paid
  hours reach ``regular_hours``, or leaves it open for a human (see
  ``overtime.scan_auto_close``);
* ``overtime_notify_hours`` is the overtime **line**, and it counts the same paid hours
  ``regular_hours`` does (see ``OVERTIME_BASIS``) - not time on site, which is the same
  number plus the break. It is resolved once, in seconds, by :func:`overtime_rule` /
  :func:`overtime_assessment`, and every clock-out path and both watcher scans read that
  rather than reading the column themselves.

WHY A SHORT SHIFT IS NOT CHARGED A BREAK
----------------------------------------
A fixed deduction on every shift is nonsense at the edges: somebody who works 40 minutes
and then clocks out (a mistake, a test, a worker sent home early) would be paid for ten
minutes. So the break is charged only to a shift long enough to have contained one -
``break_after_hours``, 4 h by default, half a working day. Below that the shift is paid
exactly as worked. Set ``break_after_hours`` to 0 to charge it on every shift.

The deduction is also capped at the elapsed time, so paid hours can never go negative.

WHY THIS IS A MODULE AND NOT A LINE IN EACH ENDPOINT
----------------------------------------------------
Paid hours are computed in five places: the online clock-out, the admin force-clock-out,
the offline punch that materializes later, the auto-close watcher, and the monthly
reports. A policy that lives in five places is a policy that disagrees with itself, and
the disagreement would show up as two workers on the same shift being paid differently
depending on which button closed it. So every one of them asks this module.

ROUNDING THE LAST FEW MINUTES UP
--------------------------------
A shift that stops a few minutes short of the day is still a day's work: somebody who
worked 7.9 h of an 8 h day, then clocked out, was paid 7.9 for having finished. So a
shift within ``round_up_within_hours`` of the paid day (15 minutes by default) is
*recorded* as the full day. Anything further short keeps its real hours - and the worker
is asked to confirm that early clock-out before it is recorded, because "I was paid for
7.2 h of an 8 h day" is a conversation the phone should start, not the payslip.

The rounding is applied by :func:`recorded_shift`, which is what the five *writing*
paths ask, and deliberately **not** by :func:`paid_hours`. The pure function is also what
the auto-close gate and the overtime watcher compare against, and rounding there would
close a shift 15 minutes early and record a clock-out at a moment that had not happened.
Deciding on the real hours and writing down the rounded ones is the whole point of
keeping the two apart.

Round-tripping: hours are rounded to four decimals, the precision the rest of this
application already stores in (see the overtime comparisons in ``main.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

#: Shipped defaults, mirrored in ``migrations.DEFAULT_SHIFT_RULES``. An operator can
#: change all three from the console; these are what a database that has never been
#: configured runs with.
DEFAULT_BREAK_MINUTES = 30.0
DEFAULT_BREAK_AFTER_HOURS = 4.0
DEFAULT_AUTO_CLOSE = True

#: How close to the paid day a shift has to stop before it is recorded as the full day:
#: 0.25 h is 15 minutes. Read from the rules when they carry it, so it can be tuned from
#: the database without a code change, and defaulted here so a database that has never
#: been configured still rounds.
DEFAULT_ROUND_UP_WITHIN_HOURS = 0.25

HOUR_DECIMALS = 4


def _number(values: dict | None, key: str, default: float) -> float:
    try:
        return float((values or {})[key])
    except (KeyError, TypeError, ValueError):
        return default


def regular_hours(values: dict | None) -> float:
    """The paid length of a day, from the shift rules."""
    return _number(values, "regular_hours", 8.0)


def break_hours(values: dict | None) -> float:
    """The unpaid break, in hours."""
    minutes = _number(values, "break_minutes", DEFAULT_BREAK_MINUTES)
    return max(0.0, minutes) / 60.0


def break_after_hours(values: dict | None) -> float:
    """How long a shift must run before a break is assumed to have been taken."""
    return max(0.0, _number(values, "break_after_hours", DEFAULT_BREAK_AFTER_HOURS))


def round_up_within_hours(values: dict | None) -> float:
    """How close to the paid day a shift rounds up to it, from the shift rules."""
    return max(0.0, _number(values, "round_up_within_hours", DEFAULT_ROUND_UP_WITHIN_HOURS))


def auto_close_enabled(values: dict | None) -> bool:
    """Whether the system closes a shift at the regular-hours boundary.

    Stored as SQLite-friendly 0/1, and any string that is not a recognised "off" reads
    as on: an unreadable setting must not quietly turn a policy off.
    """
    raw = (values or {}).get("auto_close_at_regular", DEFAULT_AUTO_CLOSE)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(1 if raw is None else int(raw))


#: Who ends a day, once the two watchers have been read against each other.
DAY_ENDED_BY_CLOSE = "auto_close"
DAY_ENDED_BY_HUMAN = "clock_out"
#: The automatic close stands down and the overtime workflow ends the day: the shift runs on
#: past the paid day, the crossing is reported, and the hours past it wait for approval.
DAY_ENDED_BY_OVERTIME = "overtime_review"


def day_end_rules(values: dict | None) -> dict[str, Any]:
    """The two watchers of an open shift, and which one wins.

    ONE TIMER, TWO RULES, AND ONLY ONE OF THEM CAN END A DAY
    --------------------------------------------------------
    ``overtime.scan_auto_close`` ends a day when its *paid* hours reach ``regular_hours``
    (8 by default). ``overtime.scan_overtime`` alerts when they reach
    ``overtime_notify_hours`` (8.1). They run in one pass, and the close deletes the session
    it closed - so a shift the close has ended can no longer be observed crossing the alert
    line. Whichever rule is allowed to act first therefore decides whether the crossing is
    ever reported at all, and with the shipped numbers (8.1 > 8.0) the close acting first
    made the alert **unreachable**: the day ended 0.1 h before the alert was due, and the
    watcher was indistinguishable from one with nothing to report. This function is that
    decision, stated once, for both scans to read.

    The precedence, and why each case is what it is:

    * **the alert is below the paid day** (``notify < regular``) - the crossing is observed
      *before* the day ends, so the close keeps owning the end of the day. The alert fires
      while the shift is still open at 7.5 h, and the shift is closed at 8 h paid: the
      manager is warned, and the standard day is still the standard day.
    * **the alert is above the paid day** (``notify > regular``) - a shift that the close
      ended at 8 h could never reach 8.1 h, so the crossing would never be observed. The
      close therefore **stands down** (``close_defers``): the shift runs on, the alert fires
      at the threshold while the worker is still working, and the hours past ``regular_hours``
      are clocked out into overtime review rather than being truncated at the boundary. This
      is what the shipped 8.1/8.0 pair asks for, and it is why nothing is auto-closed at 8 h
      under those numbers - the overtime workflow owns the end of the day.
    * **the alert is on the paid day** (``notify == regular``) - there is nothing to observe:
      the alert says a shift has crossed the paid limit and is *still working*, and at that
      figure the day is ending, so it would page the manager for every ordinary full day.
      The close acts at the boundary and the alert cannot fire, which every scan reports.
      Set the line strictly below 8 h to be warned before the day ends, or strictly above it
      to let the day run into overtime.
    * ``auto_close_at_regular`` off - **a human ends the day** (or nobody does, which is the
      documented cost of switching the close off). The alert is the only thing watching, and
      it always fires.

    ``close_defers`` is the field a caller should branch on when it means "will this shift be
    closed by the system?", and ``day_ended_by`` names the owner of the day in one word.
    """
    regular = regular_hours(values)
    notify = max(0.0, _number(values, "overtime_notify_hours", 8.1))
    close_on = auto_close_enabled(values)
    # The close may end the day only when the crossing is already observable before it does.
    close_defers = bool(close_on and notify > regular)
    alert_reachable = (not close_on) or close_defers or notify < regular

    if not close_on:
        ended_by = DAY_ENDED_BY_HUMAN
        detail = (
            f"The auto-close is off, so only a clock-out ends a shift. The overtime alert "
            f"fires at {notify:g}h paid and keeps watching until somebody clocks out."
        )
    elif close_defers:
        ended_by = DAY_ENDED_BY_OVERTIME
        detail = (
            f"A shift is closed at {regular:g}h paid, but the overtime alert is set to "
            f"{notify:g}h - above it - so the close stands down: the shift runs on, the "
            f"crossing is reported at {notify:g}h, and the hours past {regular:g}h go to "
            f"overtime review. Nothing is auto-closed at {regular:g}h. To get the automatic "
            f"close back, set the alert strictly below {regular:g}h; to be warned before the "
            f"day ends, that is the same change."
        )
    elif alert_reachable:
        ended_by = DAY_ENDED_BY_CLOSE
        detail = (
            f"A shift is closed at {regular:g}h paid. The overtime alert fires first, at "
            f"{notify:g}h, so a worker still on site then is reported before the day ends."
        )
    else:
        ended_by = DAY_ENDED_BY_CLOSE
        detail = (
            f"The overtime alert is set to {notify:g}h and the paid day is {regular:g}h, so "
            f"the alert can never fire: at that figure the shift is being closed, not still "
            f"being worked, and alerting on every full day is not a crossing. Set it strictly "
            f"below {regular:g}h for a warning before the day ends, or strictly above it to "
            f"let the day run on into overtime review."
        )

    return {
        "regular_hours": regular,
        "notify_hours": notify,
        "auto_close": close_on,
        #: The paid figure the close acts on, or ``None`` when the close is off or has stood
        #: down to let the shift run into overtime review.
        "close_at_paid_hours": None if (not close_on or close_defers) else regular,
        #: Whether the close is yielding the end of the day to the overtime workflow.
        "close_defers": close_defers,
        "alert_reachable": alert_reachable,
        "day_ended_by": ended_by,
        "detail": detail,
    }


def paid_hours(elapsed_hours: float, values: dict | None) -> tuple[float, float]:
    """``(paid, break_taken)`` for a shift that ran ``elapsed_hours`` on site.

    Returns the two numbers rather than only the paid one because the break is part of
    what the shift *was*, not an implementation detail: it is written to the shift, shown
    on the timesheet, and summed in the reports.
    """
    elapsed = max(0.0, float(elapsed_hours or 0.0))
    taken = 0.0
    if elapsed >= break_after_hours(values):
        taken = min(break_hours(values), elapsed)
    return round(elapsed - taken, HOUR_DECIMALS), round(taken, HOUR_DECIMALS)


def rounded_paid(paid: float, values: dict | None) -> tuple[float, bool]:
    """``(paid, rounded_up)``: a shift within the window of the paid day is paid the day.

    Only ever rounds *up*, and only a shift that fell short. A shift already at or past
    the paid day comes back untouched, so this cannot turn a short day into a longer one
    or a full day into overtime.
    """
    value = max(0.0, float(paid or 0.0))
    within = round_up_within_hours(values)
    regular = regular_hours(values)
    if within > 0 and value < regular and (regular - value) <= within:
        return round(regular, HOUR_DECIMALS), True
    return round(value, HOUR_DECIMALS), False


def recorded_shift(elapsed_hours: float, values: dict | None) -> dict[str, Any]:
    """What a shift is *written down* as, rounding and all - the five writers ask this.

    ``needs_confirmation`` is the early clock-out question: the shift is short of the
    paid day even after rounding, so the worker is asked before it is recorded. It is the
    server's answer, and the phone only renders it - a client that decided for itself
    would be a second copy of the policy, and the two copies would disagree the first time
    an operator retuned the day.
    """
    elapsed = round(max(0.0, float(elapsed_hours or 0.0)), HOUR_DECIMALS)
    before_rounding, taken = paid_hours(elapsed, values)
    paid, rounded = rounded_paid(before_rounding, values)
    regular = regular_hours(values)
    short = round(max(0.0, regular - paid), HOUR_DECIMALS)
    return {
        "elapsed_hours": elapsed,
        "paid_before_rounding": before_rounding,
        "paid_hours": paid,
        "break_hours": taken,
        "regular_hours": regular,
        "rounded_up": rounded,
        "short_hours": short,
        "needs_confirmation": short > 0,
        "description": _recorded_description(elapsed, before_rounding, paid, taken, rounded, regular),
    }


def _recorded_description(
    elapsed: float,
    before_rounding: float,
    paid: float,
    break_taken: float,
    rounded_up: bool,
    regular: float,
) -> str:
    """The arithmetic, plus - when it happened - why the number paid is not it."""
    if break_taken <= 0:
        said = f"{elapsed:.2f}h on site, paid as worked (no break on a shift this short)"
    else:
        said = (
            f"{elapsed:.2f}h on site - {break_taken:.2f}h unpaid break "
            f"= {before_rounding:.2f}h payable"
        )
    if rounded_up:
        # The equation above still shows the hours actually worked; this says why the day
        # is paid as the full one anyway. Printing the rounded figure in the equation
        # instead would make it read as arithmetic that does not add up.
        said += f", rounded up to the full {regular:.2f}h day"
    return said


# ---------------------------------------------------------------------------
# the overtime line
# ---------------------------------------------------------------------------
#: What the overtime threshold counts, in one word, for the API, the console and the tests
#: to share. **Paid** hours: time on site less the unpaid break - the same figure the
#: timesheet pays, the reports total, and every clock-out path stores. It is deliberately
#: not time on site: a worker on site for 8.5 h has been paid for 8, so a threshold read as
#: on-site hours would hold every ordinary full day for overtime approval, and the two
#: numbers disagree by exactly the break.
OVERTIME_BASIS = "paid"

SECONDS_PER_HOUR = 3600


def elapsed_seconds(clock_in: datetime, clock_out: datetime) -> int:
    """Whole seconds on site between two stored timestamps.

    Whole seconds because that is the resolution every timestamp in this application is
    stored and compared at (``%Y-%m-%d %H:%M:%S``), so it is the finest unit in which two
    shifts can actually be told apart. A threshold compared in fractional hours is a
    threshold compared to a number that needs rounding first, and a shift one second short
    of the line would then be decided the same way as one that reached it.
    """
    return max(0, int(round((clock_out - clock_in).total_seconds())))


def _whole_seconds(hours: float) -> int:
    return max(0, int(round(float(hours) * SECONDS_PER_HOUR)))


def paid_seconds(seconds_on_site: int, values: dict | None) -> tuple[int, int]:
    """``(paid_seconds, break_seconds)`` - the seconds-level twin of :func:`paid_hours`.

    Deliberately the same break rule at the same granularity, so a shift cannot be paid by
    one function and judged overtime by the other. The previous shape of this code did
    exactly that: the clock-out gate compared the *rounded-up recorded* hours against the
    threshold while the watcher compared the real paid hours against it, and a shift that
    rounded up to the paid day was then held for approval with zero overtime on it.
    """
    elapsed = max(0, int(seconds_on_site or 0))
    taken = 0
    if elapsed >= _whole_seconds(break_after_hours(values)):
        taken = min(_whole_seconds(break_hours(values)), elapsed)
    return elapsed - taken, taken


def overtime_rule(values: dict | None) -> dict[str, Any]:
    """The overtime line resolved once: what it counts, and where it sits, in seconds.

    Both scans, all four clock-out paths and the console read this, so there is one answer
    to "has this shift crossed the overtime line?" rather than one per call site.
    """
    line_hours = max(0.0, _number(values, "overtime_notify_hours", 8.1))
    day_hours = regular_hours(values)
    return {
        "basis": OVERTIME_BASIS,
        "threshold_hours": round(line_hours, HOUR_DECIMALS),
        "threshold_seconds": _whole_seconds(line_hours),
        "regular_hours": day_hours,
        "regular_seconds": _whole_seconds(day_hours),
    }


def overtime_assessment(seconds_on_site: int, values: dict | None) -> dict[str, Any]:
    """Everything a clock-out path (or the watcher) needs to decide about overtime.

    ONE DECISION, ONE PLACE
    -----------------------
    The punch path, the quick-link path, the offline materialization and the clock-out an
    administrator forces all used to resolve ``overtime_notify_hours`` for themselves and
    compare it their own way, and they did not agree: the gate was strict (``>``) where the
    watcher was inclusive (``>=``), two of them compared the *recorded* hours (which round
    a shift up to the paid day) against a threshold meant for real paid hours, and the
    sentence they wrote into ``flag_reason`` called the overtime line the "regular
    threshold". This is the one answer they all read now.

    THE RULE IT ENCODES
    -------------------
    * the comparison is on **paid** seconds (see ``OVERTIME_BASIS``);
    * a shift **needs approval** when its paid time has *reached* the line and it also
      leaves time past the regular paid day (``needs_approval``). Both halves matter: an
      alert line set below the paid day warns *early*, and without the second half every
      ordinary full day would be held for approval with a zero overtime figure on it;
    * the hours held back are the paid time **past the regular day** (``overtime_hours``),
      which is what the approval queue, the payroll totals and the rejection arithmetic
      all read;
    * the line is *reached*, not passed (``>=``): the same second lights the worker's own
      card, raises the administrator's alert and routes the clock-out, so the three cannot
      disagree about a shift that stops on the line.

    ``flag_sentence`` is the one-line reason the paths attach to the log row, so the record
    reads the same whichever way the shift was closed.
    """
    rule = overtime_rule(values)
    elapsed = max(0, int(seconds_on_site or 0))
    paid, taken = paid_seconds(elapsed, values)
    overtime_seconds = max(0, paid - rule["regular_seconds"])
    reaches_line = paid >= rule["threshold_seconds"]
    past_regular = paid > rule["regular_seconds"]
    overtime_hours = round(overtime_seconds / SECONDS_PER_HOUR, HOUR_DECIMALS)
    return {
        **rule,
        "seconds_on_site": elapsed,
        "elapsed_hours": round(elapsed / SECONDS_PER_HOUR, HOUR_DECIMALS),
        "break_seconds": taken,
        "break_taken_hours": round(taken / SECONDS_PER_HOUR, HOUR_DECIMALS),
        "paid_seconds": paid,
        "paid_hours": round(paid / SECONDS_PER_HOUR, HOUR_DECIMALS),
        "reaches_threshold": reaches_line,
        "past_regular": past_regular,
        "needs_approval": reaches_line and past_regular,
        "overtime_seconds": overtime_seconds,
        "overtime_hours": overtime_hours,
        "flag_sentence": (
            f"{paid / SECONDS_PER_HOUR:.2f}h paid reaches the {rule['threshold_hours']:g}h "
            f"overtime line, {overtime_hours:.2f}h past the {rule['regular_hours']:g}h paid day"
        ),
    }


def paid_limit_at(clock_in: datetime, limit_hours: float, values: dict | None) -> datetime:
    """The moment the paid hours of a shift that started at ``clock_in`` reach a limit.

    With the shipped numbers, the 8 h limit is clock-in plus 8.5 h - eight paid hours
    and the 30-minute break. The two edge cases are handled explicitly rather than by a
    formula that happens to work for the default settings:

    * if the limit is shorter than ``break_after_hours``, no break has been taken by the
      time it is reached, so it is reached after ``limit_hours``;
    * otherwise the break is inside the day, so it is reached after
      ``limit_hours + break``.

    This is what lets the clock-out, the alert and the auto-close all name the same
    instant: an alert that says "passed 8.1 h at 12:06" has to mean the same moment the
    shift's paid hours crossed it, not the moment the timer next happened to run.
    """
    limit = max(0.0, float(limit_hours))
    if limit >= break_after_hours(values):
        return clock_in + timedelta(hours=limit + break_hours(values))
    return clock_in + timedelta(hours=limit)


def closes_at(clock_in: datetime, values: dict | None) -> datetime:
    """The moment a shift reaches the paid length of a day (``regular_hours``)."""
    return paid_limit_at(clock_in, regular_hours(values), values)


def describe(elapsed_hours: float, paid: float, break_taken: float) -> str:
    """The one-line arithmetic an administrator (or a worker) can check by hand.

    ``8.50h on site - 0.50h unpaid break = 8.00h payable``. Short shifts say they were
    paid as worked, because "0.00h unpaid break" on a two-hour shift invites the
    question of why.
    """
    if break_taken <= 0:
        return f"{elapsed_hours:.2f}h on site, paid as worked (no break on a shift this short)"
    return (
        f"{elapsed_hours:.2f}h on site - {break_taken:.2f}h unpaid break "
        f"= {paid:.2f}h payable"
    )


def summary(elapsed_hours: float, values: dict | None) -> dict[str, Any]:
    """Everything a caller needs about one shift's hours, in one object."""
    return recorded_shift(elapsed_hours, values)
