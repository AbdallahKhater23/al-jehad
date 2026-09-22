"""Shift and attendance reporting, with CSV/Excel export.

THE ONE RULE THAT MATTERS
-------------------------
**A shift is only worth its hours once somebody has approved it.** The 8.1 h gate
exists so that unauthorized overtime cannot count by itself, so every row the report
is still waiting on is marked ``awaiting_approval`` and its hours are kept out of
``approved_hours`` until an administrator approves it through ``/admin/approve_review``.
Nothing is hidden: an unapproved shift is a row in the timesheet with its own hours.

An approved row reports the *approved* figure, not the recorded one: an administrator
who approves 8.5 h of a 9.5 h shift has decided what that shift is worth, and the
timesheet must agree with them rather than with the raw clock.

THIS IS A TIMESHEET, NOT A PAYROLL REPORT
-----------------------------------------
This report used to sum hours per worker and carry each worker's ``hourly_rate`` and a
``gross_estimate``. It does not any more. What is left is a timesheet - one row per
shift, the day it was worked, who worked it, where, for how long, and whether an
administrator has signed it off - because an app that pays nobody should not look like
it is about to. ``hourly_rate`` is still a column on ``users`` (the Credentials tab
edits it); no report multiplies it.

Report shapes
-------------
* ``/admin/reports/shifts``    - the timesheet: one row per shift in the period.
* ``/admin/reports/attendance``- presence, lateness and flags per worker, with the
  attendance rate measured against the ``working_days`` in ``shift_rules``.
* ``/admin/reports/export``    - the same data as a streamed CSV, or XLSX when the
  optional ``openpyxl`` extra is installed (a documented 501 otherwise, rather than a
  crash or a silently empty file).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

import shift_windows
from database import db
from notes import OPEN_STATUSES as OPEN_NOTE_STATUSES
from security import CurrentUser, admin_only

router = APIRouter(prefix="/admin/reports", tags=["reports"])

#: Statuses whose hours count.
#:
#: ``auto_closed_8h`` is here because that close *is* a decision: the system ended the
#: shift at the paid limit the policy sets, wrote the exact hours, and said so in an
#: alert. Leaving it out would mean a worker's eight real hours were not counted until
#: somebody noticed - which is the opposite of what closing a shift is for.
#:
#: The 11 h-era ``auto_closed`` is deliberately *not* here: those rows were closed by an
#: old cutoff and nobody ever decided what they were worth, so they stay in the
#: awaiting-approval column instead of quietly becoming hours somebody earned.
#:
#: ``overtime_rejected`` is payable too, and that is the whole point of it: refusing the
#: hours past the regular day does not refuse the day. The administrator left the standard
#: hours in ``approved_hours``, the overtime is recorded as zero, and the timesheet pays
#: what is there - exactly like an ordinary approved shift, which is what the worker worked.
#: It is a distinct code from ``approved`` only so that a refusal stays readable as one.
PAYABLE_CODES = ("approved", "auto_closed_8h", "overtime_rejected")
#: Statuses that are waiting for a human decision and must not count yet.
PENDING_CODES = ("pending_review", "pending_overtime")
#: The one hold that is about the *extra* hours rather than about the work. See
#: ``_shift_hours``: the standard day is credited while this is pending, and only the hours
#: past it wait.
PENDING_OVERTIME_CODE = "pending_overtime"
#: Ends a shift with no decision behind it - counted in the attendance report so the
#: number of shifts an administrator still has to look at stays visible, and marked as
#: awaiting approval in the timesheet for the same reason.
UNDECIDED_AUTO_CLOSE_CODES = ("auto_closed", "auto_closed_8h")

#: The statuses that mean "nobody has signed this off". The last close mints rows the
#: system wrote by itself, and a human still has to say what they are worth.
AWAITING_APPROVAL_CODES = PENDING_CODES + ("auto_closed",)

#: One definition of "hours a shift counts for", shared by the rows, the totals and the
#: export, because two copies of this arithmetic is how a total stops matching the
#: column above it.

def _shift_hours(record: dict) -> tuple[float, float, float]:
    """``(counted_hours, approved_hours, awaiting_hours)`` for one clock-out row.

    A rejected shift counts for nothing - an administrator decided the work was not
    done, and a timesheet that showed its hours anyway would read as work awaiting
    payment that will never come.

    ``pending_overtime`` is the one hold that is about the **extra** hours rather than
    about the work. The eight hours before the standard day happened and nobody is
    questioning them, so the day is credited as soon as the shift is filed and only the
    time past it waits for a manager. Withholding the whole shift would make an ordinary
    8 h day conditional on somebody pressing a button, which is not what "overtime needs
    approval" means - and the refusal path beside it in the same queue has always paid the
    standard day, so this makes the two halves of that queue agree.

    Every status still satisfies ``counted == approved + awaiting`` by construction, which
    is what lets one ``approved_hours`` column and one ``awaiting_approval_hours`` column
    add up to the hours the timesheet shows.
    """
    recorded = float(record["hours"] or 0.0)
    code = str(record["status_code"] or "")
    if code in PAYABLE_CODES:
        approved = float(record["approved_hours"]) if record["approved_hours"] is not None else recorded
        return approved, approved, 0.0
    if code == PENDING_OVERTIME_CODE:
        held = record["overtime_hours"]
        if held is None or float(held) <= 0:
            # The row is held for overtime but carries no figure for the hold. Fail closed:
            # keep the whole thing undecided rather than credit a day on a number that is
            # not there to be checked.
            return recorded, 0.0, recorded
        held = min(max(0.0, float(held)), recorded)
        return recorded, recorded - held, held
    if code in AWAITING_APPROVAL_CODES:
        return recorded, 0.0, recorded
    return 0.0, 0.0, 0.0

_TS = "%Y-%m-%d %H:%M:%S"


def _parse_date(value: str | None, *, default: date) -> date:
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    raise HTTPException(status_code=400, detail=f"Invalid date {value!r}; expected YYYY-MM-DD.")


def _range_bounds(start: str | None, end: str | None) -> tuple[str, str, date, date]:
    today = datetime.now().date()
    first = _parse_date(start, default=today.replace(day=1))
    last = _parse_date(end, default=today)
    if last < first:
        raise HTTPException(status_code=400, detail="end must not be before start.")
    if (last - first).days > 366 * 3:
        raise HTTPException(status_code=400, detail="Date range is limited to 3 years.")
    return (
        first.strftime("%Y-%m-%d 00:00:00"),
        (last + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
        first,
        last,
    )


def _shift_rules(conn: sqlite3.Connection) -> dict:
    import migrations

    values = dict(migrations.DEFAULT_SHIFT_RULES)
    try:
        row = conn.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        for key in values:
            try:
                if row[key] is not None:
                    values[key] = row[key]
            except (IndexError, KeyError):
                continue
    return values


def _working_days(conn: sqlite3.Connection) -> set[int]:
    """Weekday numbers (Monday = 0) the site operates on."""
    raw = str(_shift_rules(conn).get("working_days") or "6,0,1,2,3,4")
    days: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            days.add(int(part) % 7)
        except ValueError:
            continue
    return days or {0, 1, 2, 3, 4, 5}


#: The regular-day length is deliberately *not* read here any more. It used to split each
#: worker's hours into regular and overtime; the timesheet does not price a shift, so the
#: only place the rule matters is the approval gate itself (``overtime.py``). What it does
#: read is the figure the row was *held* for (``attendance_logs.overtime_hours``, selected
#: by ``SHIFT_TIMESHEET_SQL``), because that is what separates a pending overtime shift
#: into the standard day it has earned and the extra hours waiting on a manager - and it
#: describes the shift that was worked even if the policy has moved since.


# ---------------------------------------------------------------------------
# shifts
# ---------------------------------------------------------------------------
SHIFT_TIMESHEET_SQL = """
    SELECT l.id AS log_id,
           l.worker_id,
           u.name AS worker_name,
           u.role AS role,
           l.site_name,
           l.timestamp,
           l.hours,
           l.approved_hours,
           -- The figure the row itself was held for. Read here rather than re-derived from
           -- the *current* rule, which may have been changed since the shift: the split
           -- has to describe the shift that was worked, not today's policy.
           l.overtime_hours,
           l.break_hours,
           l.status_code,
           l.status,
           ci.timestamp AS clock_in_time,
           COALESCE(notes.open_notes, 0) AS open_notes
    FROM attendance_logs l
    LEFT JOIN users u ON l.worker_id = u.id
    -- The arrival that started the shift this row closes: the worker's own latest Clock In
    -- before it, paired on the log id. The id is insertion order, which is the order these
    -- punches have always been paired in - ``active_sessions`` holds exactly one open shift
    -- per worker, so the Clock In before a Clock Out *is* the shift's, however late the row
    -- around them was written (an offline punch is inserted when it is replayed).
    LEFT JOIN attendance_logs ci ON ci.id = (
        SELECT MAX(prev.id) FROM attendance_logs prev
        WHERE prev.worker_id = l.worker_id
          AND prev.action = 'Clock In'
          AND prev.id < l.id
    )
    -- The worker's open notes, not the period's: "this person has something outstanding
    -- with us" travels with every row they appear in, which is where an admin reads it.
    -- The statuses come from ``notes.py`` so a rename there cannot leave this column
    -- counting the wrong thing.
    LEFT JOIN (
        SELECT worker_id, COUNT(*) AS open_notes
        FROM worker_notes
        WHERE status IN ({note_statuses})
        GROUP BY worker_id
    ) notes ON notes.worker_id = l.worker_id
    WHERE l.action = 'Clock Out'
      AND l.timestamp >= ? AND l.timestamp < ?
      {extra}
    ORDER BY l.timestamp DESC, l.id DESC
"""


#: The timesheet's own column order, published by the report so a client does not have
#: to guess what is in a row. Not the CSV's order and not the screen's order: the screen's
#: is the administrator's to rearrange, and this is the wire format.
TIMESHEET_FIELDS = (
    "log_id", "date", "timestamp", "worker_id", "worker_name", "role", "site_name",
    "arrival_time", "arrival_verdict", "arrival_minutes",
    "hours", "recorded_hours", "approved_hours", "break_hours", "status_code", "status",
    "awaiting_approval", "open_notes",
)


#: How many minutes a shift's arrival was outside its window, and on which side. The three
#: words are ``shift_windows``' own - the same vocabulary the worker's punch card is sent -
#: so a client never has to learn a second set of names for one idea.
ARRIVAL_VERDICTS = (
    shift_windows.VERDICT_ON_TIME,
    shift_windows.VERDICT_EARLY,
    shift_windows.VERDICT_LATE,
)


def _parse_stored_timestamp(value: Any) -> datetime | None:
    """A stored ``YYYY-MM-DD HH:MM:SS`` timestamp, or ``None`` when it cannot be read.

    Tolerant on purpose. This is a *report*: one hand-written row, or one written by an
    older build, must not take the whole timesheet down - an unreadable arrival is reported
    as unknown rather than guessed at.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for pattern, width in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
    ):
        try:
            return datetime.strptime(text[:width], pattern)
        except ValueError:
            continue
    return None


def _arrival_fields(
    clock_in: Any,
    site_name: Any,
    sites: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> tuple[str | None, int | None, str | None]:
    """``(verdict, minutes_off, clock_in_time)`` for one shift's arrival.

    The verdict comes from the function the punch itself was judged by -
    ``shift_windows.effective_window(...).arrival(...)`` - resolved from the site's own
    columns with the global rule as the *per field* fallback, exactly as the gate resolved
    it. A timesheet that answered "late" for an arrival the worker's own card called on
    time would be two implementations of one policy, which is the bug this module's window
    helper already exists to prevent.

    ``minutes_off`` is how late or how early and is 0 exactly when the verdict is "on
    time"; both are ``None`` for a shift whose Clock In is not on file (a force-clock-out,
    an auto-close whose Clock In predates the pairing). Unknown is not "on time": the
    column says so rather than inventing punctuality.

    The window applied is the one in force *now*. The application has never stored the
    window a punch was judged against - the recorded flag on the row is a sentence, not a
    number - so a site that has since changed its hours is judged by the new ones. That is
    also what the supervisor re-reading the sheet is looking at.
    """
    if clock_in is None or clock_in == "":
        return None, None, None
    stamp = str(clock_in)
    moment = _parse_stored_timestamp(clock_in)
    if moment is None:
        return None, None, stamp
    window = shift_windows.effective_window(sites.get(str(site_name or "")), rules)
    arrival = window.arrival(moment)
    return arrival.verdict, int(arrival.minutes_off), stamp


# ---------------------------------------------------------------------------
# the columns a worker may keep in their own files
# ---------------------------------------------------------------------------

#: Every column a *self* report may carry, in the order they are written.
#:
#: One vocabulary, owned here, because a saved choice has to be checked against something -
#: and "what does a timesheet row have" is a question this module already answers: every id
#: below is a field ``shift_timesheet_rows`` puts on the row. The frontend draws the labels
#: (fixed English headers for the CSV, translated ones for the sheet) and owns the cell
#: values; the *set* it may offer is this. So a choice saved on a phone is still a choice
#: when the row shape changes, and text under the column in the database can never reach a
#: file as a column nobody can fill.
#:
#: ``hours`` beside ``recorded`` is the pair a door-to-door timesheet exists for - 8.5 h on
#: site, 8 h counted - and they are two columns rather than one because a worker reconciling
#: a day needs both numbers on the same line.
REPORT_COLUMNS: tuple[str, ...] = (
    "date",
    "site",
    "arrival",
    "break",
    "hours",
    "recorded",
    "approved",
    "status",
)

#: What an account that has never chosen gets: the columns these files have always carried,
#: so no worker's download changes shape because the setting appeared.
DEFAULT_REPORT_COLUMNS: tuple[str, ...] = (
    "date",
    "site",
    "arrival",
    "break",
    "hours",
    "approved",
    "status",
)


def report_columns_choice(value: object) -> tuple[str, ...]:
    """Read a chosen set of column ids out of a stored string or a posted list.

    Known ids only, each once, in the vocabulary's own order - so the file reads the same
    way however the choice was expressed, and "which column comes first" never depends on
    the order boxes happened to be ticked in. An unknown id is dropped rather than refused:
    a client one release ahead of this server would otherwise be unable to save at all, and
    the column it knows about is one this build cannot fill anyway.

    May return nothing at all, which is the caller's problem to refuse: a file with no
    columns is not a report.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        items: list = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return ()
    wanted = {str(item or "").strip().lower() for item in items}
    return tuple(column for column in REPORT_COLUMNS if column in wanted)


def report_columns(value: object) -> tuple[str, ...]:
    """The columns to *use*: what was chosen, or the default when nothing usable was.

    Both the report and the reply that saves a choice read through this, so what a worker
    is shown is what the server will draw from - never the list they posted.
    """
    return report_columns_choice(value) or DEFAULT_REPORT_COLUMNS


def stored_report_columns(columns: object) -> str | None:
    """What to write: the choice as text, or ``None`` when it is the default choice.

    Choosing everything and never choosing are the same file today. Storing the default as
    *nothing* keeps them the same file tomorrow too: the account that has narrowed nothing
    gains a column in the release that adds one, while the account that deliberately
    dropped "Status" keeps a file without it. The other way round would freeze a worker's
    file at the shape it had on the day they first pressed Save.
    """
    chosen = report_columns_choice(columns)
    if not chosen or chosen == DEFAULT_REPORT_COLUMNS:
        return None
    return ",".join(chosen)


def shift_timesheet_rows(
    *, start: str, end: str, site: str | None = None, worker_id: str | None = None
) -> dict:
    """One period of shifts, one row per shift.

    Newest first: a timesheet is read from the top, and the thing an administrator
    opens it for is almost always yesterday rather than the first of the month. The
    totals are sums over the rows beside them, never a separate query, so the header
    cannot disagree with the table under it.
    """
    extra = ""
    params: list = [start, end]
    if site:
        extra += " AND l.site_name = ?"
        params.append(site)
    if worker_id:
        extra += " AND l.worker_id = ?"
        params.append(worker_id)

    sql = SHIFT_TIMESHEET_SQL.format(
        extra=extra,
        note_statuses=",".join("?" * len(OPEN_NOTE_STATUSES)),
    )
    # The note statuses are placeholders ahead of the range's own parameters, in the
    # order the statement mentions them.
    params = list(OPEN_NOTE_STATUSES) + params

    with db() as conn:
        # The cursor's own rows, not a dict each: every read below is string-keyed, and
        # ``sqlite3.Row`` answers that in C without materialising ~1 400 dicts per quarter.
        records = conn.execute(sql, tuple(params)).fetchall()
        # The window each arrival is judged against, in two reads rather than one per row:
        # the sites' own columns and the global rule are the same for every row of the
        # period, and a query per shift would be a query per shift.
        rules = _shift_rules(conn)
        try:
            sites = {
                str(row["site_name"]): row
                for row in conn.execute(
                    "SELECT site_name, clock_in_window_start, clock_in_window_end, site_timezone "
                    "FROM construction_sites"
                ).fetchall()
            }
        except sqlite3.Error:
            # A database old enough to have no window columns at all still reports: every
            # arrival is then judged by the global rule, which is what it was judged by.
            sites = {}

    rows: list[dict] = []
    approved_total = 0.0
    awaiting_total = 0.0
    break_total = 0.0
    for record in records:
        hours, approved, awaiting_hours = _shift_hours(record)
        code = str(record["status_code"] or "")
        awaiting = code in AWAITING_APPROVAL_CODES
        verdict, minutes_off, arrival_time = _arrival_fields(
            record["clock_in_time"], record["site_name"], sites, rules
        )
        approved_total += approved
        break_total += float(record["break_hours"] or 0.0)
        awaiting_total += awaiting_hours
        rows.append(
            {
                "log_id": int(record["log_id"]),
                # The day the shift ended, which is the day a timesheet is filed under.
                # Sent as a plain date as well as a full timestamp: a date column is the
                # first thing an administrator reads and the first thing they sort by.
                "date": str(record["timestamp"])[:10],
                "timestamp": record["timestamp"],
                "worker_id": str(record["worker_id"]),
                "worker_name": record["worker_name"],
                "role": record["role"],
                "site_name": record["site_name"],
                # Where this shift's arrival fell relative to the site's window. The clock-in
                # time travels with the verdict so an administrator can check the judgement
                # against the clock rather than trusting it - and so "late by 12 min" can be
                # read next to the 04:12 that produced it.
                "arrival_time": arrival_time,
                "arrival_verdict": verdict,
                "arrival_minutes": minutes_off,
                "hours": round(hours, 4),
                "recorded_hours": round(float(record["hours"] or 0.0), 4),
                # The hours credited *now*. A settled row states the decision; an
                # overtime row states the standard day it has already earned, because that
                # is what a pay run would credit today. Any other undecided row has no
                # answer to give and stays null.
                "approved_hours": (
                    round(float(record["approved_hours"]), 4)
                    if record["approved_hours"] is not None
                    else (round(approved, 4) if code == PENDING_OVERTIME_CODE else None)
                ),
                # Beside it, so the two columns add up to the hours shown and the console
                # can total a filtered view the same way the server totals the period.
                "awaiting_approval_hours": round(awaiting_hours, 4),
                # Beside the hours, because the two together are what a door-to-door
                # timesheet is reconciled against: 8.0 h counted out of 8.5 h on site.
                "break_hours": round(float(record["break_hours"] or 0.0), 4),
                "status_code": code,
                "status": record["status"],
                "awaiting_approval": awaiting,
                "open_notes": int(record["open_notes"] or 0),
            }
        )

    totals = {
        "shifts": len(rows),
        "workers": len({row["worker_id"] for row in rows}),
        # The hours the timesheet shows, split by whether somebody has signed for them.
        # ``hours == approved_hours + awaiting_approval_hours`` holds by construction - and
        # for a pending overtime shift that split is the standard day against the extra,
        # not the whole shift against nothing.
        "hours": round(approved_total + awaiting_total, 4),
        "approved_hours": round(approved_total, 4),
        "awaiting_approval_hours": round(awaiting_total, 4),
        "awaiting_approval": sum(1 for row in rows if row["awaiting_approval"]),
        "break_hours": round(break_total, 4),
        # Counted per worker, not per row: four shifts by one person who has a request
        # open is one outstanding note, not four.
        "workers_with_open_notes": len(
            {row["worker_id"] for row in rows if row["open_notes"] > 0}
        ),
        # The one figure an administrator asks a timesheet for that is not about hours:
        # how much of the period walked in after its window. Counted per shift, so a worker
        # who was late twice is two - it is a count of arrivals, not of people.
        "late_arrivals": sum(1 for row in rows if row["arrival_verdict"] == shift_windows.VERDICT_LATE),
    }
    return {"rows": rows, "totals": totals}


# ---------------------------------------------------------------------------
# attendance rate
# ---------------------------------------------------------------------------
ATTENDANCE_SQL = """
    SELECT l.worker_id,
           u.name AS worker_name,
           u.role AS role,
           l.timestamp,
           l.status_code,
           l.flag_reason,
           l.hours
    FROM attendance_logs l
    LEFT JOIN users u ON l.worker_id = u.id
    WHERE l.action = 'Clock In'
      AND l.timestamp >= ? AND l.timestamp < ?
      {extra}
"""


def attendance_rows(*, start: str, end: str, worker_id: str | None = None) -> dict:
    extra = ""
    params: list = [start, end]
    if worker_id:
        extra += " AND l.worker_id = ?"
        params.append(worker_id)

    first = datetime.strptime(start, _TS).date()
    last = datetime.strptime(end, _TS).date() - timedelta(days=1)

    with db() as conn:
        working_days = _working_days(conn)
        records = conn.execute(ATTENDANCE_SQL.format(extra=extra), tuple(params)).fetchall()
        # The name *and* the role, because an attendance row has to say who the person on
        # the other end of it is: an administrator who worked a site belongs in these figures
        # with the role that explains why they are in them, and a row that named only the
        # name would leave the reader to guess whether that was a worker or the office.
        known = {
            str(row["id"]): {"name": row["name"], "role": row["role"]}
            for row in conn.execute("SELECT id, name, role FROM users").fetchall()
        }

    expected_days = sum(1 for offset in range((last - first).days + 1) if (first + timedelta(days=offset)).weekday() in working_days)
    expected_days = max(1, expected_days)

    buckets: dict[str, dict] = {}
    for record in records:
        key = str(record["worker_id"])
        bucket = buckets.setdefault(
            key,
            {
                "worker_id": key,
                "worker_name": record["worker_name"] or (known.get(key) or {}).get("name"),
                # ``None`` when the account is gone: the attendance facts are still the
                # worker's own - deleting the login does not delete the days they worked -
                # so the row is reported with a role the client prints as unknown rather
                # than dropped from the report.
                "role": record["role"] or (known.get(key) or {}).get("role"),
                "days_present": set(),
                "late_arrivals": 0,
                "auto_closed": 0,
                "flagged": 0,
                "hours": 0.0,
            },
        )
        stamp = str(record["timestamp"])[:10]
        bucket["days_present"].add(stamp)
        reason = str(record["flag_reason"] or "")
        code = str(record["status_code"] or "")
        if "clock-in window" in reason or "late" in reason.lower():
            bucket["late_arrivals"] += 1
        if code in UNDECIDED_AUTO_CLOSE_CODES:
            bucket["auto_closed"] += 1
        if code in PENDING_CODES or code == "rejected":
            bucket["flagged"] += 1
        bucket["hours"] += float(record["hours"] or 0.0)

    rows = []
    for bucket in buckets.values():
        present = len(bucket["days_present"])
        rows.append(
            {
                "worker_id": bucket["worker_id"],
                "worker_name": bucket["worker_name"],
                "role": bucket["role"],
                "days_present": present,
                "expected_days": expected_days,
                "attendance_rate": round(min(1.0, present / expected_days), 4),
                "attendance_percent": round(min(100.0, 100.0 * present / expected_days), 2),
                "late_arrivals": bucket["late_arrivals"],
                "auto_closed_shifts": bucket["auto_closed"],
                "flagged": bucket["flagged"],
            }
        )
    rows.sort(key=lambda row: (-row["attendance_rate"], row["worker_id"]))
    return {
        "rows": rows,
        "period": {"start": first.isoformat(), "end": last.isoformat(), "working_days": sorted(working_days)},
        "totals": {
            "workers": len(rows),
            "expected_days": expected_days,
            "average_attendance_rate": round(sum(row["attendance_rate"] for row in rows) / len(rows), 4) if rows else 0.0,
        },
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
def _encoded(payload: dict) -> JSONResponse:
    """Hand back a payload that is already JSON, without a second copy of itself.

    FastAPI runs every non-``Response`` return value through ``jsonable_encoder``, which
    walks the whole structure one value at a time (an ``isinstance``/``is_dataclass`` call
    per leaf). A quarter of shifts is 1 413 rows and 452 KB, and that walk measured **40 ms
    of the endpoint's 69 ms** - for a payload that is nothing but dicts, lists, strings,
    numbers and ``None``, so the walk changes no byte of the answer.

    Safe exactly while that stays true, which is why the row builders are asserted to be
    encoder-neutral in the tests (``jsonable_encoder(payload) == payload``): a ``datetime``
    or a ``Decimal`` arriving in a row must fail there and be dealt with at the source,
    rather than silently turning into a 500 here.
    """
    return JSONResponse(content=payload)


@router.get("/shifts")
async def shifts_report(
    start: str | None = None,
    end: str | None = None,
    site: str | None = None,
    worker_id: str | None = None,
    current: CurrentUser = Depends(admin_only),
):
    first, second, start_date, end_date = _range_bounds(start, end)
    result = shift_timesheet_rows(start=first, end=second, site=site, worker_id=worker_id)
    return _encoded({
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "filters": {"site": site, "worker_id": worker_id},
        "fields": list(TIMESHEET_FIELDS),
        "note": (
            "A timesheet: one row per shift. Only hours an administrator has approved "
            "count: a row with awaiting_approval=true is still waiting for a decision, "
            "and its hours are in awaiting_approval_hours rather than approved_hours. "
            "arrival_verdict is where the shift's clock-in fell relative to the window its "
            "site applies - on_time, early or late - with arrival_minutes how far off it "
            "was and arrival_time the clock-in it was measured from."
        ),
        **result,
    })


@router.get("/payroll", include_in_schema=False, deprecated=True)
async def payroll_report_alias(
    start: str | None = None,
    end: str | None = None,
    site: str | None = None,
    worker_id: str | None = None,
    current: CurrentUser = Depends(admin_only),
):
    """The path this report shipped under, kept as an alias.

    This project's own integration rule is that new endpoints never move old ones: a page
    cached on a phone, a bookmark, or a colleague's saved curl command keeps answering
    while the app, the docs and the tests all use ``/shifts``.
    """
    return await shifts_report(
        start=start, end=end, site=site, worker_id=worker_id, current=current
    )


@router.get("/attendance")
async def attendance_report(
    start: str | None = None, end: str | None = None, worker_id: str | None = None,
    current: CurrentUser = Depends(admin_only),
):
    first, second, start_date, end_date = _range_bounds(start, end)
    result = attendance_rows(start=first, end=second, worker_id=worker_id)
    result["period"]["start"] = start_date.isoformat()
    result["period"]["end"] = end_date.isoformat()
    return result


@router.get("/pending")
async def pending_report(current: CurrentUser = Depends(admin_only)):
    """Everything waiting on an administrator, in one place."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.worker_id, u.name AS worker_name, u.role AS role, l.site_name, l.action, l.timestamp, l.hours,
                   l.approved_hours, l.overtime_hours, l.status, l.status_code, l.flag_reason,
                   l.source, l.liveness_class, l.score
            FROM attendance_logs l
            LEFT JOIN users u ON l.worker_id = u.id
            WHERE l.status_code IN ('pending_review', 'pending_overtime')
            ORDER BY l.timestamp ASC
            """
        ).fetchall()
        notifications_unread = conn.execute(
            "SELECT COUNT(*) FROM admin_notifications WHERE read_at IS NULL"
        ).fetchone()[0]
    return {
        "count": len(rows),
        "unread_notifications": int(notifications_unread or 0),
        "items": [dict(row) for row in rows],
    }


#: How much CSV text is accumulated before a chunk is yielded.
#:
#: Starlette runs a *sync* generator through ``iterate_in_threadpool``, so every ``yield``
#: costs a worker-thread round trip. Yielding one row at a time meant ~1 400 hops for a
#: quarter of shifts - more than a third of that request's wall time, while the query and
#: the formatting together were a fraction of it. Batching keeps the property this response
#: exists for (memory bounded by the chunk, not by the report) and makes each hop carry
#: something worth sending.
CSV_CHUNK_BYTES = 64 * 1024


def _csv_response(filename: str, header: list[str], rows: list[list]) -> StreamingResponse:
    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            if buffer.tell() >= CSV_CHUNK_BYTES:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        remainder = buffer.getvalue()
        if remainder:
            yield remainder

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _xlsx_response(filename: str, sheet: str, header: list[str], rows: list[list]) -> StreamingResponse:
    try:
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail=(
                "Excel export needs the optional 'openpyxl' package. Install it with "
                "'pip install openpyxl' (see backend/requirements.txt), or use format=csv."
            ),
        )
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet[:31]
    worksheet.append(header)
    for row in rows:
        worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export")
async def export_report(
    kind: str = Query(default="shifts", pattern="^(shifts|payroll|attendance|audit|offline)$"),
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    start: str | None = None,
    end: str | None = None,
    site: str | None = None,
    worker_id: str | None = None,
    limit: int = 5000,
    current: CurrentUser = Depends(admin_only),
):
    """Stream a report as CSV (always) or XLSX (when ``openpyxl`` is installed)."""
    first, second, start_date, end_date = _range_bounds(start, end)
    limit = max(1, min(int(limit), 50000))
    stamp = f"{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}"

    # ``payroll`` is accepted for the same reason ``/admin/reports/payroll`` still is: a
    # saved link or script must not start failing because a label was renamed.
    if kind in ("shifts", "payroll"):
        result = shift_timesheet_rows(start=first, end=second, site=site, worker_id=worker_id)
        # Four columns, in this order, and the same four the admin console's own Download
        # CSV button writes: who, their id, where, and how long. No rate, no estimate -
        # this is a timesheet, and a sheet that carries a money column invites somebody to
        # add it up and treat the answer as a payroll figure nobody computed.
        header = ["Employee", "id", "site", "hours"]
        rows = [
            [row["worker_name"] or row["worker_id"], row["worker_id"], row["site_name"], row["hours"]]
            for row in result["rows"]
        ]
        filename = f"shifts_{stamp}"
        sheet = "Shifts"
    elif kind == "attendance":
        result = attendance_rows(start=first, end=second, worker_id=worker_id)
        # ``role`` sits beside the name, where a reader attributing the figures looks for
        # it: an administrator who worked the site is in this file on purpose, and a file
        # that carries their days without saying which of them is the administrator is a
        # file somebody has to reconcile by hand.
        header = [
            "worker_id", "worker_name", "role", "days_present", "expected_days",
            "attendance_percent", "attendance_rate", "late_arrivals", "auto_closed_shifts",
            "flagged",
        ]
        rows = [[row[column] for column in header] for row in result["rows"]]
        filename = f"attendance_{stamp}"
        sheet = "Attendance"
    elif kind == "audit":
        with db() as conn:
            records = conn.execute(
                "SELECT id, created_at, actor_id, actor_role, action, entity, entity_id, ip, before_json, after_json "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        header = [
            "id", "created_at", "actor_id", "actor_role", "action", "entity", "entity_id", "ip",
            "before_json", "after_json",
        ]
        rows = [[row[column] for column in header] for row in records]
        filename = f"audit_log_{stamp}"
        sheet = "Audit"
    else:  # offline
        from offline_sync import _effective_sql

        with db() as conn:
            records = conn.execute(
                "SELECT p.id, p.worker_id, u.name AS worker_name, p.action, p.client_timestamp, "
                "p.anchor_server_time, p.monotonic_offset_s, p.client_offset_s, "
                f"{_effective_sql('p')} AS effective_time, p.status, p.rejection_code, p.flag_reason "
                "FROM punch_queue p LEFT JOIN users u ON p.worker_id = u.id "
                "ORDER BY p.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        header = [
            "id", "worker_id", "worker_name", "action", "client_timestamp", "anchor_server_time",
            "monotonic_offset_s", "client_offset_s", "effective_time", "status", "rejection_code",
            "flag_reason",
        ]
        rows = [[row[column] for column in header] for row in records]
        filename = f"offline_punches_{stamp}"
        sheet = "Offline"

    if format == "xlsx":
        return _xlsx_response(f"{filename}.xlsx", sheet, header, rows)
    return _csv_response(f"{filename}.csv", header, rows)
