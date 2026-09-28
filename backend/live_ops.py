"""The Live Ops board's own figures, counted rather than downloaded.

WHY THIS MODULE EXISTS
---------------------
The board is three numerals and a list: how many people are on site, how many places (or
categories) that is spread across, how many arrived late, and which shift has been open
longest. Every one of those figures used to be produced by **fetching payloads and counting
what was in them** - ``Promise.all`` of ``/admin/active_sessions``, ``/admin/users``,
``/admin/sites`` and ``/admin/shift_rules`` - and the roster read is the expensive half:
``/admin/users`` answers ``password_set`` per row and joins the audit log for every account's
last password change, so a board whose whole job is to say "12 on site" paid a price that
grew with the number of *accounts*, on every render, on every Refresh and inside its own
poll. It is the right shape for the screen that edits the roster and the wrong shape for the
screen that counts the gate.

So the figures are counted here, in SQL, and the board reads them. What is left of the four
payloads is what a board actually draws with: the open shifts themselves, the sites (a picker
and a category map, one row per *site*), the shift rules (one row), and - only when somebody
opens the force-in panel - the roster, which is the one payload that is allowed to be
roster-sized because that panel is a roster.

THE BOARD'S OWN JOIN, VERBATIM
------------------------------
Every query here is
``FROM active_sessions a JOIN users u ON a.worker_id = u.id`` - the same join
``GET /admin/active_sessions`` uses, row for row - so a session whose account has been deleted
is dropped by the figures and by the rows in the same breath and the two cannot report
different totals for the same afternoon. That is also why ``dashboard._now`` borrows
``summary`` for its ``on_shift``/``by_site`` instead of keeping its own copy of the join: it
used to hold the board's query verbatim, and a verbatim copy is a copy.

Two properties are inherited from the board rather than decided here, and both are deliberate:

* it is **not scoped by the concealment clause** the roster is scoped by. The board names who
  is standing at the gate, and a count that hid a session the board names would be two answers
  to one question on the same deployment (``dashboard._now``'s docstring says the same thing
  beside the figure it borrows);
* ``late`` is the **flag's own reading**, not the window rule. Whether an arrival was late is
  decided once, when the punch is filed, and what the row carries is that decision
  (``shift_windows.describe``, or ``NULL`` when the arrival was inside the window). A count
  that re-derived it here would be a second implementation of the shift window - and the one
  number that could disagree with the badge on the row.

WHAT IS *NOT* COUNTED, AND WHY
------------------------------
``longest`` cannot be an aggregate. "Which open shift has run longest" is a question about
*time elapsed*, and elapsed time is computed from a stored stamp by ``shift_hours`` (see
``seconds_on_site`` below for the zone it is stored in). So the query orders by the stored
stamp - which is monotone for the one format this application writes - and the seconds are
computed by the same function the card, the clock-out path and the recorded hours use. The
row is picked with the client's own rule: the first row whose stamp the shared arithmetic can
read, because a stamp nobody can parse is not a shift the board can show a duration for.

A NOTE FOR WHOEVER LANDS THE UTC MIGRATION
------------------------------------------
``clock_in_time`` is the zone-less wall-clock convention the attendance tables use today, and
``seconds_on_site`` is the same arithmetic ``main`` and ``shift_hours`` use, so this module
moves with them when ``docs/RUNBOOK_ATTENDANCE_UTC.md`` is executed - nothing here parses a
stamp on its own.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

import shift_hours
from database import db
from security import CurrentUser, admin_only

log = logging.getLogger("attendance.live_ops")

router = APIRouter(prefix="/admin", tags=["live ops"])

#: ``%Y-%m-%d %H:%M:%S`` - the format every stored timestamp in this application uses, and
#: therefore the format SQLite's own string comparison can be trusted to sort chronologically.
_TS = "%Y-%m-%d %H:%M:%S"

#: The board's own join, written once.
_BOARD_JOIN = "FROM active_sessions a JOIN users u ON a.worker_id = u.id"

#: How far down the clock-in order to look for a shift the board can measure. One row is the
#: answer - the oldest readable stamp - and the small window exists only so that a stamp
#: nothing can parse is stepped over rather than reported as the board's longest shift.
_LONGEST_SCAN = 5


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in (_TS, _TS + ".%f"):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def seconds_on_site(clock_in_time: Any, now: datetime | None = None) -> int | None:
    """Whole seconds since a stored clock-in, or ``None`` if it cannot be read.

    Sent with every open shift so a client that draws a *live* counter starts at the
    server's own figure. The stored clock-in is a zone-less wall-clock string
    (``%Y-%m-%d %H:%M:%S``, written by ``datetime.now()``, i.e. in *this* host's zone);
    a phone or a console in another zone reads those digits as its own local time, which
    puts a constant offset on the counter - three hours, for a server in UTC and staff in
    Kuwait, on a shift that has just started. Sending the count alongside the stamp means
    the client never has to know what zone the digits were written in: it starts from this
    number and adds only the seconds it has watched pass.

    "This host's zone" is the company's zone now: ``clock.py`` pins the process (and the
    container pins the image) to ``Asia/Kuwait``, so the stored digits, this counter and the
    wall clock at the gate all agree.

    The same function the clock-out path measures a closed shift with
    (``shift_hours.elapsed_seconds``), so the counter on the card and the hours that are
    actually recorded cannot disagree about what "so far" means.
    """
    clock_in = _parse_ts(clock_in_time)
    if clock_in is None:
        return None
    return shift_hours.elapsed_seconds(clock_in, now if now is not None else datetime.now())


def summary(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """The board's figures, on an open connection. One pass, three queries, no payload.

    ``worker_ids`` is the board's *identity* rather than one of its numerals: it is what the
    board's own poll compares to answer "has anything moved" without downloading the rows to
    find out, and it is the smallest thing that answers it - a handover is two ids and the
    same count. Names are deliberately not here; a renamed worker is picked up by the rows
    the poll then fetches, or by the operator's own Refresh. The ids come back in no
    particular order, so a reader that compares them sorts them first (the board does).
    """
    now = now if now is not None else datetime.now()
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS workers,
               SUM(CASE WHEN {_LATE_SQL} THEN 1 ELSE 0 END) AS late,
               GROUP_CONCAT(a.worker_id, ',') AS ids
        {_BOARD_JOIN}
        """
    ).fetchone()
    sites = [
        {"site_name": str(name), "workers": int(count)}
        for name, count in conn.execute(
            f"""
            SELECT a.site_name, COUNT(*) AS workers
            {_BOARD_JOIN}
            GROUP BY a.site_name
            ORDER BY workers DESC, a.site_name COLLATE NOCASE ASC
            """
        ).fetchall()
        if name is not None and str(name) != ""
    ]
    # The oldest *readable* clock-in. Ordered by the stored stamp and stepped past anything
    # this application's own arithmetic cannot read, so the figure cannot name a shift no
    # duration can be shown for (see the module docstring).
    longest: dict[str, Any] | None = None
    for row in conn.execute(
        f"""
        SELECT a.worker_id, u.name, a.site_name, a.clock_in_time
        {_BOARD_JOIN}
        WHERE a.clock_in_time IS NOT NULL AND TRIM(a.clock_in_time) <> ''
        ORDER BY a.clock_in_time ASC
        LIMIT ?
        """,
        (_LONGEST_SCAN,),
    ).fetchall():
        seconds = seconds_on_site(row["clock_in_time"], now)
        if seconds is None:
            continue
        longest = {
            "worker_id": str(row["worker_id"]),
            "name": row["name"],
            "site_name": row["site_name"],
            "clock_in_time": row["clock_in_time"],
            "seconds_on_site": seconds,
        }
        break
    return {
        "as_of": now.strftime(_TS),
        "on_site": int(totals["workers"] or 0),
        "late": int(totals["late"] or 0),
        "sites": sites,
        "worker_ids": [part for part in str(totals["ids"] or "").split(",") if part],
        "longest": longest,
    }


#: What "late" means, in SQL, exactly as the rows mean it on the client.
#:
#: ``active_sessions.late_flag`` is either ``NULL`` (the arrival was inside the site's window)
#: or the sentence ``shift_windows.describe`` wrote when it was not - a description for a
#: notification body, never a boolean. So the test is "somebody wrote that this was late",
#: and the two values that mean the opposite are named rather than assumed: a column that can
#: hold prose can also hold ``"0"``. The console's ``liveOpsIsLate`` is the same rule in the
#: reader's hands, and a test holds the two together.
_LATE_SQL = (
    "a.late_flag IS NOT NULL AND TRIM(a.late_flag) <> '' "
    "AND LOWER(TRIM(a.late_flag)) NOT IN ('0', 'false')"
)


@router.get("/live_ops/count")
async def count_live_ops(current: CurrentUser = Depends(admin_only)):
    """The board's headline figures, without the payloads it used to count them from.

    Read once per board render and once per poll instead of the roster, the site list and the
    open-shift rows. The rows are still fetched - a board is a list of who is at the gate -
    but the *figures* are counted here, and the poll asks this question ("has anything moved")
    rather than downloading the rows and counting them to decide it.
    """
    with db() as conn:
        return summary(conn)
