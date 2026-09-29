"""The console's front door: what this deployment *is*, counted rather than downloaded.

WHY THIS MODULE EXISTS
---------------------
A console with eleven tabs and no front door makes an administrator open three screens to
learn that nothing is wrong. This is the screen that answers "how many sites, how many
accounts, what is waiting on a person" in the five seconds before somebody decides whether
to keep reading.

The screen is trivial. The read behind it is not, and the whole of this module is about one
decision:

    **Every number here is counted in SQL. None of them is derived by fetching rows and
    counting them in Python.**

None of these figures existed as a read before this endpoint. They were - and, on every
other screen, still are - computed by downloading lists: ``/admin/users`` computes
``password_set`` per row and joins the audit log for every account's last password change,
which is the right shape for the screen that *edits* the roster and the wrong shape for a
screen that counts it. A dashboard built on those reads pays the cost it exists to remove,
and its price grows with the roster rather than with the number of panels.

``Live Ops`` was the screen that proved the point - its three numerals were counted out of
four full payloads on a poll - and it has since been given a counted read of its own
(``live_ops.summary``), which this module borrows for ``now.on_shift``/``now.by_site`` rather
than holding a second copy of the board's join. What is left of the argument here is the rest
of the payload: no other screen counts accounts, sites, waiting notes or the day.

THE ONE FIGURE THAT IS NOT A COUNT OF A TABLE
--------------------------------------------
``now.overtime_open`` asks a question - how many open shifts are past the line with nobody having
answered for them - that is *computed* per shift rather than stored, because the threshold lives
in ``shift_rules``, the break arithmetic in ``shift_hours``, and the standing answer in
``overtime_authorisations``. Counting it in SQL would mean writing the overtime rule a second
time, which is exactly what this module refuses to do anywhere else. So it is borrowed from
``overtime.open_crossings`` and counted inPython - and it is bounded by the number of open shifts, not by the roster, which is the property
the cost test actually holds. ``_now``'s own docstring says the same thing, beside the query.

The rest of the ``now`` panel *is* counted, and one of its predicates is still borrowed: the
offline backlog is selected with ``offline_sync.PENDING_STATUSES``, the same pair that module's
materialiser picks rows up by, so "handed over and not yet a record" cannot come to mean two
different things on two screens.

THE SECOND HALF OF THE SAME DECISION
------------------------------------
**Every total is scoped by the rules of the list it summarizes**, in the query.
``developer.visibility_clause`` hides the root account *inside* the SQL precisely because a
filter applied in Python still hands back the count of what it removed, and a count is an
enumeration - the roster closed that hole and a dashboard that ignored it would re-open it.
So the clause travels with every user-shaped count here: accounts, active, enrolled, by
role, never-clocked-in, joined-this-week, onboarding and dormant.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not re-implement what the reports already decide. ``waiting.reviews`` uses
``reports.PENDING_CODES`` - the same tuple ``/admin/reports/pending`` filters by, so the
number on the dashboard and the rows on the queue screen cannot drift; the note count uses
``notes.OPEN_STATUSES``; the walk-up count uses ``registrations.STATUS_PENDING_APPROVAL``, the
status the quarantined account carries until somebody decides it. And where a predicate is a
*rule* rather than a list - an open shift past the overtime line - it is not restated at all but
borrowed whole (see below). A second copy of a predicate is how two screens start disagreeing
about the same queue.

THE PERIOD PANEL IS THE LONGEST-ARMED VERSION OF THAT RULE
----------------------------------------------------------
``period`` summarizes the attendance report over a window, and it does not count anything itself:
the whole panel is ``reports.attendance_period``, the counted twin of ``reports.attendance_rows``,
run through *this* connection (so the cost test below can trace it). What the dashboard decides is
which window ``?days=`` asked for, and the window travels back with the figures so the range
control and the extremes' links can describe exactly the period the numbers came from. A dashboard
that re-derived an attendance rate from a count, or a shift's hours from its own copy of the 8.1 h
gate, would be the second implementation of attendance the plan spends a page refusing.

It also does not fetch a face template to answer "is this account enrolled". The roster
screen asks ``biometrics.is_enrolled`` per row, which is a filesystem stat each: correct for
the screen that hands out access one account at a time, and absurd for a count. What is
counted here is *recorded* enrollment - ``enrolled_at`` and ``biometric_id`` both set - and
the difference is real and deliberate: a template deleted from disk by hand is still recorded
as enrolled here, and the Credentials tab is where that account gets re-enrolled. The two
numbers are allowed to differ because they answer different questions; the module docstring
of the plan (``docs/DASHBOARD_PLAN.md``) states the same rule.

UNKNOWN IS NOT ZERO
-------------------
Each panel is computed in its own ``try``: an installation whose schema is older than a table
this read touches gets ``null`` for that panel and every other panel filled in, and the
console draws "could not be read" over it. A zero standing in for "could not tell" is the one
output a page like this must never produce - it reads as good news, and it is the state an
administrator would act on.

A NOTE FOR WHOEVER LANDS THE UTC MIGRATION
-----------------------------------------
Timestamps are compared two ways here, and both follow the convention the rest of the app
uses today:

* ``attendance_logs.timestamp`` is compared against a Kuwait *wall-clock* day window to
  answer "has anybody clocked in at this site today" (migration 28's comments call this out:
  the attendance tables become UTC in that release);
* ``worker_notes.created_at``, ``users.enrolled_at``, ``refused_punches.created_at`` and the
  audit log stay in the system tables' Kuwait-local convention.

When ``docs/RUNBOOK_ATTENDANCE_UTC.md`` is executed, the attendance half of this module moves
with it - the two ``clock.to_kuwait`` boundaries are marked below - and the day window above
has to move with the columns it filters. A dashboard that silently shifted every "today"
figure by three hours is exactly the failure that runbook is written to avoid.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

import developer
import live_ops
import notes
import offline_sync
import overtime
import registrations
import reports
from database import db
from security import CurrentUser, admin_only

log = logging.getLogger("attendance.dashboard")

router = APIRouter(prefix="/admin/dashboard", tags=["dashboard"])

#: ``%Y-%m-%d %H:%M:%S`` - the format every stored timestamp in this application uses.
_TS = "%Y-%m-%d %H:%M:%S"

#: How far back "joined this week" looks.
NEW_ACCOUNT_DAYS = 7

#: How long an active account may go without a punch before it is reported as *dormant*.
#:
#: A month, and the length is chosen rather than picked: thirty days is the payroll cycle, so
#: an account this quiet has missed one whole pay run - long enough that a worker on a
#: fortnight's leave is not named, short enough that somebody who stopped coming in is caught
#: while somebody still remembers them turning up. It is *not* the same question as
#: ``never_clocked_in``: that account has no punch at all and is a hire who never started,
#: where this one worked and then stopped, which is the harder fact to notice. The two figures
#: are deliberately disjoint, and the queries below are what keeps them so.
DORMANT_DAYS = 30

#: The period panel's default window: the last seven days, today included.
#:
#: Seven because it is the span an administrator can still act on - yesterday's missing approval
#: is answerable, last month's is archaeology - and because it is short enough that the shifts
#: behind it are the ones still in somebody's memory. The other window on offer is the calendar
#: month, for the administrator who is closing a pay run rather than watching a week.
DEFAULT_PERIOD_DAYS = 7

#: The longest window the period panel will build from ``days``.
#:
#: The reports module's own ceiling, named rather than repeated: it is the ``LIMIT`` on the
#: per-day breakdown the panel draws, so a route that allowed a longer window than the query
#: counts would leave the strip silently short instead of failing. The month preset is bounded by
#: the calendar and never comes near it.
PERIOD_MAX_DAYS = reports.MAX_WINDOW_DAYS

#: What ``?days=`` may say: a number of days, or the word ``month``.
#:
#: A pattern rather than a parse, for the reason ``/admin/reports/export``'s two query parameters
#: are patterns: a junk value is refused at the door with a 422 naming the parameter, instead of
#: silently becoming the default window - a dashboard quietly showing seven days because somebody
#: typed ``days=7d`` is a wrong answer that looks like a right one.
PERIOD_PATTERN = r"^(?:month|\d{1,3})$"

#: How far back "refused in the last day" looks.
#:
#: A day rather than an hour because the panel is answering *is that normal*: refusals arrive in
#: bursts (a bad light at one gate, a model that lost its band), and an hour is short enough that
#: a quiet hour reads the same as a healthy one.
REFUSED_WINDOW_HOURS = 24

#: The audit actions that mean **an account joined this deployment**.
#:
#: There is no ``users.created_at`` column, so the audit log - append-only, and the record the
#: console already treats as the truth about an account's history - is the only evidence of
#: when an account came into being. Three paths write one of these: ``user_create`` for the
#: credentials console, ``admin_create`` for a head administrator adding an administrator, and
#: ``registration_approved`` for a walk-up.
#:
#: WHY THE WALK-UP COUNTS AT ITS *DECISION* RATHER THAN ITS ARRIVAL. A submission writes a real
#: quarantined account (``users.status = registrations.STATUS_PENDING_APPROVAL``) but is filed by
#: an anonymous visitor, so it writes no audit row: the public form has no actor to name, and the
#: registry this reads is a record of what *people here* did. So a walk-up joins on the day an
#: administrator decided it - which is also the day it can clock in, and the day the reader of
#: this panel would say the hire happened. The gap that leaves is worth stating rather than
#: hiding: an application submitted this week and still waiting is not counted anywhere in this
#: figure, and the queue itself (`waiting.registrations`) is where it is visible.
#:
#: A path that created an account without writing one of these rows would undercount here, which
#: is why the tuple is named rather than inlined at the query.
ACCOUNT_CREATION_ACTIONS = ("user_create", "admin_create", "registration_approved")

#: The actions that create an account, as a SQL list, kept in step with the tuple above.
_CREATION_SQL = ", ".join("?" for _ in ACCOUNT_CREATION_ACTIONS)

#: ``status`` means "active" unless it says otherwise, and a missing value means active.
#:
#: This is the status that may **record attendance**, which is the line that matters on this
#: screen: ``main.verify_worker`` refuses a punch from anything else. It is deliberately not
#: "the status that may sign in" - that set is wider by exactly one value, the quarantine a
#: walk-up submission writes (see ``_QUARANTINE_SQL``), because an applicant has to be able to
#: sign in and read the decision before it is made. The column is ``NOT NULL DEFAULT 'active'``,
#: so the missing-value half is belt and braces for a row written by hand or restored from a
#: dump. What it must not do is disagree with the gate: an account the dashboard calls
#: deactivated and the login route lets in is a number that sends somebody looking for a bug
#: that is not there.
_ACTIVE_SQL = "LOWER(TRIM(COALESCE(status, 'active'))) = 'active'"

#: The status a walk-up submission writes and an approval lifts: a real account that can sign in
#: and may not yet punch. Sprayed with its own name rather than folded into ``_ACTIVE_SQL``'s
#: complement, because the two are opposite facts - one is waiting for a person, the other was
#: turned off - and a roster that lumped them together would make a deployment with a queue of
#: walk-ups look like one full of deactivated accounts.
_QUARANTINE_SQL = "LOWER(TRIM(COALESCE(status, ''))) = ?"


# ---------------------------------------------------------------------------
# the day this read is about
# ---------------------------------------------------------------------------
def _day_bounds(now: datetime) -> tuple[str, str, date]:
    """``(start, end, date)`` for the current company day, end-exclusive.

    The company clock is the host clock: ``clock.py`` pins the container to Kuwait and every
    stored timestamp is written with ``datetime.now()``, so "today" is the date in this
    process. ``end`` is the *next* midnight rather than ``23:59:59``, because a half-open
    range cannot lose a punch recorded in the last second of the day - the same convention
    ``reports._range_bounds`` uses for every range it builds.
    """
    today = now.date()
    return (
        today.strftime("%Y-%m-%d 00:00:00"),
        (today + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
        today,
    )


def _age_seconds(stamp: Any, now: datetime) -> int | None:
    """How long ago ``stamp`` was, in whole seconds, or ``None`` if it is not a timestamp.

    Never negative: a row written a second into the future (a device clock, a stamp from a
    machine slightly ahead) reads as "just now" rather than as a wait that has not started.
    """
    if stamp is None or str(stamp).strip() == "":
        return None
    try:
        moment = datetime.strptime(str(stamp), _TS)
    except (TypeError, ValueError):
        return None
    return max(0, int((now - moment).total_seconds()))


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------
def _people(conn: sqlite3.Connection, current: CurrentUser, now: datetime) -> dict[str, Any]:
    """Accounts, scoped to what this reader may see - in the query, never after it.

    THREE STATES, THREE FIGURES, AND THEY ADD UP: ``active`` (the one status that may record
    attendance), ``pending_approval`` (a walk-up submission nobody has decided) and
    ``deactivated`` (everything else). The middle one is the state this panel had to be taught:
    an account in quarantine can sign in, so it is not gone, and it cannot punch, so it is not
    active - and the panel that folded it in with the deactivated ones was reporting a queue as
    a roster somebody had switched off.

    TWO OF THE FIGURES ARE *WATCHES* RATHER THAN COUNTS OF A STATE - ``dormant`` and
    ``onboarding`` - and they exist because the roster counts above describe a deployment
    without ever saying that anything about it has gone wrong. Dormant is an account that worked
    here and has not been seen for ``DORMANT_DAYS``; onboarding is one that joined inside
    ``NEW_ACCOUNT_DAYS`` and has never clocked in at all. They are the two ways an account stops
    being a worker without anybody deciding anything, and both would otherwise be noticed by
    payroll or a headcount review rather than by the front door. Neither is a count of a status,
    so both carry the window they were counted over; and both are disjoint from
    ``never_clocked_in``, for the reason that query's comment gives.
    """
    hide_sql, hide_params = developer.visibility_clause(current, column="id")

    # One pass for the four account-level figures: they are four predicates over the same
    # rows, and four separate scans of the roster to answer four questions about it is the
    # pattern this endpoint exists to stop.
    row = conn.execute(
        "SELECT COUNT(*) AS accounts, "
        f"SUM(CASE WHEN {_ACTIVE_SQL} THEN 1 ELSE 0 END) AS active, "
        # Waiting for a decision, and counted apart from ``deactivated`` - see
        # ``_QUARANTINE_SQL``. It is a *sub-count of the queue* the waiting panel reports as
        # ``waiting.registrations``, read from the same status constant ``registrations`` owns,
        # so the two panels cannot disagree about how many applications are open.
        f"SUM(CASE WHEN {_QUARANTINE_SQL} THEN 1 ELSE 0 END) AS pending_approval, "
        # Recorded enrollment, not a template file on disk: see the module docstring.
        "SUM(CASE WHEN COALESCE(enrolled_at, '') <> '' AND COALESCE(biometric_id, '') <> '' "
        "THEN 1 ELSE 0 END) AS enrolled, "
        # An account that cannot sign in at all, which is the state this panel exists to
        # surface: the row is there, the door is not.
        "SUM(CASE WHEN COALESCE(password_hash, '') = '' THEN 1 ELSE 0 END) AS no_password "
        "FROM users WHERE 1 = 1" + hide_sql,
        (registrations.STATUS_PENDING_APPROVAL, *hide_params),
    ).fetchone()

    accounts = int(row["accounts"] or 0)
    active = int(row["active"] or 0)
    pending = int(row["pending_approval"] or 0)
    enrolled = int(row["enrolled"] or 0)

    by_role = {
        str(role): int(count)
        for role, count in conn.execute(
            "SELECT role, COUNT(*) FROM users WHERE 1 = 1" + hide_sql + " GROUP BY role",
            hide_params,
        ).fetchall()
        if role is not None and str(role) != ""
    }

    # "Never clocked in" is exact rather than approximate, and it is worth saying why:
    # ``retention`` never deletes an ``attendance_logs`` row (it is the record of hours owed -
    # see that module's "what is never deleted"), so the absence of a row really does mean a
    # worker who has never punched rather than one whose punches aged out.
    #
    # The same fact is what makes ``dormant`` below answerable at all, and that is why this
    # query is spelled out rather than folded into it: this is the complement of "has punched",
    # and dormant is the complement of "has punched *lately*" over the accounts that have.
    never = conn.execute(
        "SELECT COUNT(*) FROM users u WHERE "
        + _ACTIVE_SQL.replace("status", "u.status")
        + " AND u.id NOT IN (SELECT DISTINCT worker_id FROM attendance_logs "
        "WHERE worker_id IS NOT NULL)"
        + hide_sql,
        hide_params,
    ).fetchone()[0]

    # Accounts that joined in the last week, from the audit log - see
    # ``ACCOUNT_CREATION_ACTIONS`` for the three paths that write there. The join back to
    # ``users`` is what keeps this a count of *accounts* rather than of events: a deleted
    # account's audit row cannot inflate it. ``audit_days`` (365 by default) prunes audit rows,
    # which is why this asks a seven-day question of a one-year log.
    #
    # Two figures from the one scan, because the second is a *sub-question* of the first: how
    # many of this week's accounts have never clocked in is the onboarding follow-up the plan
    # asks for ("they never signed in" / "their face was never enrolled"), and counting it here
    # rather than in a statement of its own is what makes ``onboarding <= new_this_week`` true by
    # construction instead of by careful upkeep.
    since = (now - timedelta(days=NEW_ACCOUNT_DAYS)).strftime(_TS)
    joined = conn.execute(
        "SELECT COUNT(*) AS new_this_week, "
        "SUM(CASE WHEN u.id NOT IN (SELECT DISTINCT worker_id FROM attendance_logs "
        "WHERE worker_id IS NOT NULL) THEN 1 ELSE 0 END) AS onboarding "
        "FROM users u WHERE u.id IN ("
        "SELECT entity_id FROM audit_log WHERE entity = 'users' AND entity_id IS NOT NULL "
        f"AND action IN ({_CREATION_SQL}) AND created_at >= ? "
        ")" + hide_sql,
        (*ACCOUNT_CREATION_ACTIONS, since, *hide_params),
    ).fetchone()

    # Dormant: an account that may record attendance, has worked here, and has not been seen for
    # ``DORMANT_DAYS``. The first half of the predicate is what keeps it disjoint from
    # ``never_clocked_in`` - an account with no punch at all is not dormant, it is a hire who
    # never started, and a figure that counted both would report a roster of dormant staff on a
    # deployment that has simply not opened yet. The window is on
    # ``attendance_logs.timestamp``: see the UTC note in the module docstring for the boundary
    # this moves with.
    dormant_since = (now - timedelta(days=DORMANT_DAYS)).strftime(_TS)
    dormant = conn.execute(
        "SELECT COUNT(*) FROM users u WHERE "
        + _ACTIVE_SQL.replace("status", "u.status")
        + " AND u.id IN (SELECT DISTINCT worker_id FROM attendance_logs "
        "WHERE worker_id IS NOT NULL)"
        " AND u.id NOT IN (SELECT DISTINCT worker_id FROM attendance_logs "
        "WHERE worker_id IS NOT NULL AND timestamp >= ?)"
        + hide_sql,
        (dormant_since, *hide_params),
    ).fetchone()[0]

    return {
        "accounts": accounts,
        "active": active,
        "pending_approval": pending,
        # Three states, three figures, and they add up: active (may record attendance), waiting
        # for an administrator, and switched off. ``deactivated`` is the complement over the
        # other two rather than over ``active`` alone, so a queue of applications can never make
        # the roster look deactivated - which is what sends somebody looking for who disabled
        # accounts nobody disabled.
        "deactivated": accounts - active - pending,
        "by_role": by_role,
        "enrolled": enrolled,
        # The complement of ``enrolled`` over the same rows, so the two add up to
        # ``accounts`` and a reader never has to work out what the remainder was.
        "no_face": accounts - enrolled,
        "no_password": int(row["no_password"] or 0),
        "new_this_week": int(joined["new_this_week"] or 0),
        "never_clocked_in": int(never or 0),
        "onboarding": int(joined["onboarding"] or 0),
        "dormant": int(dormant or 0),
        # The two windows the figures above were counted over, sent *with* the figures for the
        # reason the period panel's window travels with its own: "dormant: 4" is not a fact
        # until the reader knows what "dormant" meant, and a label that says thirty days while
        # the query counted forty-five is a number nobody can check. They are not counts and are
        # named apart from them - but a window that is not on the wire is a window the console
        # would have to keep its own copy of, which is how the two start disagreeing.
        "dormant_days": DORMANT_DAYS,
        "onboarding_days": NEW_ACCOUNT_DAYS,
    }


# ---------------------------------------------------------------------------
# places
# ---------------------------------------------------------------------------
def _places(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Sites, categories, and the one site-shaped fact that is about today."""
    sites = int(conn.execute("SELECT COUNT(*) FROM construction_sites").fetchone()[0] or 0)
    categories = int(conn.execute("SELECT COUNT(*) FROM site_categories").fetchone()[0] or 0)

    # Every category, including one with no sites inside it. It is a real answer - "this
    # category has nothing in it" is a configuration leftover somebody may want to delete -
    # and it keeps ``len(by_category)`` equal to ``categories``, which is an invariant a test
    # can hold rather than a coincidence.
    by_category = [
        {"category": str(name), "sites": int(count)}
        for name, count in conn.execute(
            "SELECT c.name, COUNT(s.site_name) FROM site_categories c "
            "LEFT JOIN construction_sites s ON s.category_id = c.category_id "
            "GROUP BY c.category_id, c.name ORDER BY c.name COLLATE NOCASE ASC"
        ).fetchall()
    ]
    no_category = int(
        conn.execute(
            "SELECT COUNT(*) FROM construction_sites WHERE category_id IS NULL"
        ).fetchone()[0]
        or 0
    )

    # A site with its own hours or its own zone: it does not follow its category or the
    # company, so this is the list an administrator checks when somebody is flagged late at
    # one site and not at another. The predicate is ``shift_windows``': an empty string means
    # "inherit" exactly as ``NULL`` does, which is why this cannot be ``IS NOT NULL`` alone.
    # ``test_admin_dashboard.py`` holds this count against
    # ``shift_windows.Window.is_site_specific`` on the same fixture, so the two definitions
    # cannot drift apart silently.
    overriding = int(
        conn.execute(
            "SELECT COUNT(*) FROM construction_sites WHERE "
            "(COALESCE(clock_in_window_start, '') <> '' OR COALESCE(clock_in_window_end, '') <> '' "
            "OR COALESCE(site_timezone, '') <> '')"
        ).fetchone()[0]
        or 0
    )

    start, end, _ = _day_bounds(now)
    # A site nobody has punched into today. ``HAVING COUNT(a.id) = 0`` on a LEFT JOIN is the
    # whole answer, and it returns only the sites that qualify rather than every site with a
    # tally attached - the difference between a count and a download.
    #
    # The day window is on ``attendance_logs.timestamp``: see the UTC note in the module
    # docstring for the boundary this moves with.
    unmanned = [
        str(row[0])
        for row in conn.execute(
            "SELECT s.site_name FROM construction_sites s "
            "LEFT JOIN attendance_logs a ON a.site_name = s.site_name "
            "AND a.timestamp >= ? AND a.timestamp < ? "
            "GROUP BY s.site_name HAVING COUNT(a.id) = 0 "
            "ORDER BY s.site_name COLLATE NOCASE ASC",
            (start, end),
        ).fetchall()
    ]

    return {
        "sites": sites,
        "categories": categories,
        "by_category": by_category,
        "no_category": no_category,
        "overriding_window": overriding,
        "unmanned_today": unmanned,
    }


# ---------------------------------------------------------------------------
# period
# ---------------------------------------------------------------------------
def _period_window(days: str, now: datetime) -> tuple[date, date, str]:
    """``(first_day, last_day, preset)`` for the window ``?days=`` asked for, today inclusive.

    Two windows, and they are the two an administrator asks a week of attendance in: the last N
    days (default seven, counted so that ``days=7`` means today and the six days before it rather
    than eight days of data) and the calendar month so far. The month is spelled ``month``
    rather than sent as an elapsed day count, because a bookmark or a saved link has to mean the
    same window next week: ``days=11`` on the 11th would pin a link to one particular morning.

    Nothing is refused here - the route's pattern has already seen to that - and a number is
    clamped rather than rejected, so a link from a build that offered a longer window still
    answers with the longest one this build counts.
    """
    today = now.date()
    token = str(days or "").strip().lower()
    if token == "month":
        return today.replace(day=1), today, "month"
    try:
        count = int(token)
    except (TypeError, ValueError):
        count = DEFAULT_PERIOD_DAYS
    count = min(max(1, count), PERIOD_MAX_DAYS)
    return today - timedelta(days=count - 1), today, "days"



def _period(conn: sqlite3.Connection, now: datetime, days: str) -> dict[str, Any]:
    """The period the reader asked for: presence, hours and the two extremes worth opening.

    **The reports module owns every number here** - ``reports.attendance_period`` counts them
    through this same connection, with ``reports``' own definitions of "present", "expected"
    and "what a shift is worth". This function decides only *which window* was asked for and
    which of the panel's keys is the window itself; it does no arithmetic, because a dashboard
    that re-derived a rate from a count would be the second implementation of attendance the
    plan refuses.

    The window travels back with the figures (``start``, ``end``, ``days``, ``preset``) and that
    is load-bearing rather than decoration: the panel's range control marks itself from
    ``preset``, and each extreme's link opens *that person's* attendance over exactly the window
    the figure beside it describes. A link into a different period than the number it came from
    is a number nobody can check.

    ``current`` is deliberately not a parameter: see ``reports.attendance_period`` for why the
    attendance half of this page is not scoped by the concealment clause.
    """
    first, last, preset = _period_window(days, now)
    # Half-open at the top end, like every other range in this application: a punch recorded in
    # the last second of the last day belongs to this window and not to the next one.
    figures = reports.attendance_period(
        conn,
        start=first.strftime("%Y-%m-%d 00:00:00"),
        end=(last + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
    )
    return {
        "days": (last - first).days + 1,
        "preset": preset,
        "start": first.isoformat(),
        "end": last.isoformat(),
        **figures,
    }


# ---------------------------------------------------------------------------
# now
# ---------------------------------------------------------------------------
def _now(conn: sqlite3.Connection, current: CurrentUser, now: datetime) -> dict[str, Any]:
    """This moment: who is on site, and the three things waiting to become records.

    The three are the *backlog* half of the panel and each is a different kind of waiting: a
    punch a phone handed over that has not been filed (``offline_sync``'s own guard, see below),
    a refusal in the last day, and an open shift past the overtime line that nobody has answered
    for. The refusals are the one figure here that is a **rate rather than a queue**: a refused
    punch has no triage state anybody can clear (``refused_punches`` owns that decision), so it
    is drawn to answer "is that normal", never to be worked down.

    TWO DELIBERATE EXCEPTIONS TO "SCOPED IN THE QUERY", and both are worth naming rather than
    leaving to be discovered by whoever next compares this panel with the screen it summarizes:

    * **``on_shift`` and ``by_site`` are the Live Ops board's own reading** - the same join, so
      the same rows. Both figures are borrowed from ``live_ops.summary`` rather than restated
      here, because "the same join" written twice is two joins the day one of them changes. The
      board is not scoped by the concealment clause either (it is a board of *the gate*, and it
      names who is standing at it), and a dashboard that hid a session the board names would be
      two answers to one question on the same deployment. That is the stronger rule here: the
      plan defines this figure as the board's, and ``test_admin_dashboard.py`` holds the two
      equal on the same fixture. If the board should conceal, that is a change to the board -
      and this panel moves with it, in one place.
    * **``overtime_open`` is borrowed from ``overtime.open_crossings``**, not recast as SQL. "Past
      the line" is a decision per open shift - the threshold comes from ``shift_rules``, the
      break arithmetic from ``shift_hours``, and whether a shift is still a *question* from a
      standing authorisation - so a counted version of it would be a second implementation of
      the overtime rule, which is the one thing this module must not create. What it costs is
      bounded by the number of open shifts (people on site right now), not by the roster.
      ``needs_answer`` is the same guard the Approvals badge counts, so the two agree.
    """
    # The board's own read, borrowed whole rather than copied - the join, the two figures and
    # the rows they agree with all live in ``live_ops.summary``. It answers one question this
    # panel does not draw (which shift has been open longest), which is a third pass over the
    # same open shifts on a connection this panel already holds: the price of the two screens
    # being unable to disagree about who is on the gate, and bounded by people on site rather
    # than by the roster.
    board = live_ops.summary(conn, now=now)
    on_shift = board["on_site"]
    by_site = board["sites"]

    # A punch a phone handed over that has not become an attendance record yet - counted by
    # ``offline_sync``'s own predicate, not by a lookalike. The materialiser picks up exactly
    # ``status IN PENDING_STATUSES AND materialized_log_id IS NULL``, so the backlog this panel
    # reports is the work that module will actually do.
    #
    # NOT ``processed_at IS NULL``, which is the near-miss this replaces: that column is stamped
    # on *rejection* too, so counting it would report a signature this deployment already refused
    # as work somebody still owes, and a punch refused the moment it arrived (never processed at
    # all) as waiting. "Handed over, not yet filed" is what the materialiser selects on.
    #
    # Scoped with the clause the way the roster is: ``worker_id`` is NOT NULL here, so the plain
    # column is exact.
    hide_sql, hide_params = developer.visibility_clause(current, column="worker_id")
    waiting_statuses = ", ".join("?" for _ in offline_sync.PENDING_STATUSES)
    offline = conn.execute(
        "SELECT COUNT(*) FROM punch_queue WHERE materialized_log_id IS NULL "
        f"AND status IN ({waiting_statuses})" + hide_sql,
        (*offline_sync.PENDING_STATUSES, *hide_params),
    ).fetchone()[0]

    # Refusals are scoped through ``COALESCE`` because this table is the one place a punch is
    # recorded with *no* worker at all: a face the engine could not match names nobody, and a
    # row with a NULL worker is exactly the refusal an operator most needs to see. The plain
    # clause would drop every one of them, because ``NULL NOT IN (...)`` is not true.
    refused_sql, refused_params = developer.visibility_clause(
        current, column="COALESCE(worker_id, '')"
    )
    since = (now - timedelta(hours=REFUSED_WINDOW_HOURS)).strftime(_TS)
    refused = conn.execute(
        "SELECT COUNT(*) FROM refused_punches WHERE created_at >= ?" + refused_sql,
        (since, *refused_params),
    ).fetchone()[0]

    # Borrowed, for the reason in the docstring. ``open_crossings`` never raises - its own
    # contract, because a tab reads it - so a rules table it cannot read answers 0 here rather
    # than degrading the panel; that is that module's documented choice, not this one's.
    crossings = overtime.open_crossings(now=now)

    return {
        "on_shift": on_shift,
        "by_site": by_site,
        "overtime_open": sum(1 for item in crossings if item.get("needs_answer")),
        "offline_waiting": int(offline or 0),
        "refused_24h": int(refused or 0),
    }


# ---------------------------------------------------------------------------
# waiting
# ---------------------------------------------------------------------------
def _waiting(
    conn: sqlite3.Connection, current: CurrentUser, now: datetime
) -> dict[str, Any]:
    """What is waiting on a person, and how long the oldest of it has been waiting.

    Four queues, each counted by the predicate its own screen uses, and each scoped to the
    accounts this reader may see. The alert queue is the deployment's own and is read only by
    the root tier: ``alerts`` is ``null`` for an administrator rather than ``0``, because the
    queue is not theirs and a zero would say "nothing is waiting" about a surface they cannot
    read at all.

    THE WALK-UP QUEUE IS A SET OF *ACCOUNTS*, which is the one place this panel's scoping is
    load-bearing rather than belt and braces: a submission writes a real ``users`` row in
    quarantine (``registrations.STATUS_PENDING_APPROVAL``), so the count is a count of accounts
    and the concealment clause travels with it exactly as it does over the roster. Only the
    alert queue is not an account-shaped count.
    """
    hide_sql, hide_params = developer.visibility_clause(current, column="worker_id")
    account_sql, account_params = developer.visibility_clause(current, column="id")
    pending_codes = ", ".join("?" for _ in reports.PENDING_CODES)
    open_note_statuses = ", ".join("?" for _ in notes.OPEN_STATUSES)

    # ``attendance_logs.timestamp`` is the punch this decision is about, so its age is how
    # long the shift has been waiting for somebody - not when a reviewer opened it.
    # ``reports.PENDING_CODES`` rather than the literal pair: ``/admin/reports/pending``
    # filters by that tuple, and a dashboard that listed its own copy would be a second
    # answer to "what is pending" on the same deployment.
    reviews = conn.execute(
        "SELECT COUNT(*), MIN(l.timestamp) FROM attendance_logs l WHERE "
        f"l.status_code IN ({pending_codes})" + hide_sql,
        (*reports.PENDING_CODES, *hide_params),
    ).fetchone()
    # The quarantine: accounts a walk-up submission created and no administrator has decided
    # yet. Counted by the status constant ``registrations`` owns, so the number here and the
    # rows on the Registrations tab cannot drift.
    #
    # The age is ``enrolled_at``, and that is not a convenience: a submission files the face as
    # it writes the row (see ``registrations.submit_registration``), so for a *quarantined*
    # account that column is the moment the form arrived - the account is created by the
    # application itself, and nothing else has happened to it yet. A decided account's
    # ``enrolled_at`` means something else entirely (when its face was filed), which is why this
    # reads it only over rows that are still waiting.
    registrations_pending = conn.execute(
        "SELECT COUNT(*), MIN(enrolled_at) FROM users WHERE status = ?" + account_sql,
        (registrations.STATUS_PENDING_APPROVAL, *account_params),
    ).fetchone()
    open_notes = conn.execute(
        "SELECT COUNT(*), MIN(created_at) FROM worker_notes WHERE "
        f"status IN ({open_note_statuses})",
        notes.OPEN_STATUSES,
    ).fetchone()

    payload: dict[str, Any] = {
        "reviews": int(reviews[0] or 0),
        "registrations": int(registrations_pending[0] or 0),
        "notes": int(open_notes[0] or 0),
        "alerts": None,
        "oldest_seconds": None,
    }

    ages: list[int] = []
    for stamp in (reviews[1], registrations_pending[1], open_notes[1]):
        age = _age_seconds(stamp, now)
        if age is not None:
            ages.append(age)

    if current.is_developer:
        # The root tier's own queue, counted here rather than through
        # ``notifications.unacknowledged_count`` - that function answers ``0`` when the
        # query fails, which is the one output this module must never produce (see the
        # docstring), and this panel also needs the *age* of the oldest alert, which the
        # count does not return. The predicate is the same one that function uses.
        unacknowledged = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM admin_notifications "
            "WHERE acknowledged_at IS NULL"
        ).fetchone()
        payload["alerts"] = int(unacknowledged[0] or 0)
        age = _age_seconds(unacknowledged[1], now)
        if age is not None:
            ages.append(age)

    # One number for the whole panel: the longest anything here has been waiting. It is the
    # figure that decides whether somebody opens the queues now or after lunch, and it is
    # deliberately the *maximum* rather than an average - an average across four queues
    # describes none of them.
    payload["oldest_seconds"] = max(ages) if ages else None
    return payload


# ---------------------------------------------------------------------------
# the read
# ---------------------------------------------------------------------------
def _panel(
    name: str,
    compute: Callable[[], dict[str, Any]],
    degraded: list[str],
) -> dict[str, Any] | None:
    """Run one panel, or answer ``None`` and remember why.

    A panel that cannot be computed must not take the page with it, and must not answer zero:
    ``None`` is "could not be read", which the console draws as such. The reason is logged
    rather than returned - the reader cannot act on an ``sqlite3`` error message, and the
    operator who can reads it in the log.
    """
    try:
        return compute()
    except sqlite3.Error as exc:
        log.warning("dashboard panel %s could not be read: %s", name, exc)
        degraded.append(name)
        return None


def build_dashboard(
    current: CurrentUser, *, now: datetime | None = None, days: str = str(DEFAULT_PERIOD_DAYS)
) -> dict[str, Any]:
    """The whole payload, from one connection.

    ``now`` is injectable for the same reason every other clock in this application is
    reachable from a test: "today", "this week" and "how long has that been waiting" are all
    measured against it, and a suite that cannot move it can only test the numbers that do not
    depend on the time of day. ``days`` is the period panel's window (see ``_period_window``)
    and travels only that far: no other panel is about a range, and a window that silently
    re-scoped the day's figures too would be a second thing to explain on the stamp.
    """
    moment = now or datetime.now()
    start, end, today = _day_bounds(moment)
    degraded: list[str] = []
    with db() as conn:
        people = _panel("people", lambda: _people(conn, current, moment), degraded)
        places = _panel("places", lambda: _places(conn, moment), degraded)
        now_panel = _panel("now", lambda: _now(conn, current, moment), degraded)
        waiting = _panel("waiting", lambda: _waiting(conn, current, moment), degraded)
        period = _panel("period", lambda: _period(conn, moment, days), degraded)
    return {
        "status": "success",
        # The stamp the console draws, because this is a snapshot a human refreshes rather
        # than a live board: Live Ops polls, this does not, and a total that silently drifts
        # is worse than one that admits it is a minute old.
        "as_of": moment.strftime(_TS),
        "day": {
            "start": start[:10],
            "end": end[:10],
            "timezone": "Asia/Kuwait",
            "today": today.isoformat(),
        },
        "people": people,
        "places": places,
        "now": now_panel,
        "waiting": waiting,
        "period": period,
    }


@router.get("")
async def admin_dashboard(
    days: str = Query(default=str(DEFAULT_PERIOD_DAYS), pattern=PERIOD_PATTERN),
    current: CurrentUser = Depends(admin_only),
) -> dict[str, Any]:
    """The console's front door: counts for people, places, what is waiting and the period.

    ``admin_only``, so a site administrator reads it and the root tier reaches it through the
    same wildcard it reaches every other administrative surface with. Nothing in the payload
    is scoped by the *caller's* role beyond that: the concealment is by account (the root
    account is excluded in every query above) and by tier (the alert queue is the root tier's,
    and is ``null`` for anybody else).

    ``?days=`` is the period panel's window - a number of days, or ``month`` for the calendar
    month so far. It is the endpoint's only parameter and it is deliberately not called
    ``start``/``end``: the panel offers two windows rather than a date picker, and a relative
    window is what makes a saved dashboard link still mean the last seven days next week.
    """
    return build_dashboard(current, days=days)
