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
  ``overtime.scan_auto_close``).

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


def auto_close_enabled(values: dict | None) -> bool:
    """Whether the system closes a shift at the regular-hours boundary.

    Stored as SQLite-friendly 0/1, and any string that is not a recognised "off" reads
    as on: an unreadable setting must not quietly turn a policy off.
    """
    raw = (values or {}).get("auto_close_at_regular", DEFAULT_AUTO_CLOSE)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(1 if raw is None else int(raw))


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
    paid, taken = paid_hours(elapsed_hours, values)
    return {
        "elapsed_hours": round(max(0.0, float(elapsed_hours or 0.0)), HOUR_DECIMALS),
        "paid_hours": paid,
        "break_hours": taken,
        "regular_hours": regular_hours(values),
        "description": describe(round(max(0.0, float(elapsed_hours or 0.0)), HOUR_DECIMALS), paid, taken),
    }
