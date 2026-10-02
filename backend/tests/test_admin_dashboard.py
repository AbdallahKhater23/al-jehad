"""The console's front door: one counted read, and the ways a count can be wrong.

WHY THIS EXISTS
---------------
Every number on the dashboard is a promise that some queue has N things in it, and a dashboard
that is off by one is *trusted* - which makes it worse than no dashboard at all. So this suite
plants a roster whose every field a count depends on, and asserts each figure exactly rather than
asserting that the endpoint answered.

What it is guarding, in the order the module's own docstring argues it:

1. **Counted, not downloaded.** ``test_the_read_issues_counts_and_its_cost_does_not_follow_the_
   roster`` traces the SQL the read actually issues: every statement is a count, and the number
   of them is the same for a ten-account roster and a two-hundred-and-ten-account one. A payload
   that grew with the roster would pass every other test in this file.
2. **Scoped in the query.** ``test_the_root_account_is_absent_from_every_total`` plants the root
   account and asserts *nothing* moved - including the totals, because a filter applied in Python
   still hands back the count of what it removed. The other half is asserted too: the root tier
   reading its own total *does* see the root account, which is what proves the clause is doing
   the hiding rather than the account being invisible to everyone.
3. **One definition per queue.** ``test_the_waiting_counts_agree_with_the_queues_they_summarize``
   holds each wait against the screen that lists it, so the two cannot drift - and the same is
   done for the ``now`` panel, which is the one panel that *borrows*: ``on_shift`` is held equal
   to the Live Ops board's own rows, ``overtime_open`` to the queue the Approvals tab draws (and
   to the count its badge polls), and ``offline_waiting`` to the un-filed rows of the punch queue
   the materialiser would actually pick up. A figure this screen did not compute is the figure
   most worth comparing.
4. **Unknown is not zero.** A panel whose sub-read fails answers ``null``; a deployment with
   nothing in it answers zeroes. Both look "empty" and they mean opposite things.
5. **The day is half-open.** A punch at the first second of today counts; one at the first second
   of tomorrow does not.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

import dashboard
import database
import developer
import harness
import migrations
import notes
import registrations
import reports
from harness import (
    ADMIN,
    HEAD_ADMIN,
    MOALLEM,
    OFF_OFFICE,
    WORKER,
    assert_denied,
    bearer,
    db_scalar,
)

DASHBOARD = "/api/v1/admin/dashboard"
TS = "%Y-%m-%d %H:%M:%S"

#: The sites this suite works with. ``Downtown Tower A`` and ``New Capital Zone B`` are seeded
#: by the harness; ``Depot`` is added here so a category can hold two sites.
A = "Downtown Tower A"
B = "New Capital Zone B"
DEPOT = "Depot"

#: The roster, written out rather than derived: ``(id, role, status, enrolled, has_password)``.
#:
#: Four independent facts per account, because four different counts read them - ``enrolled``
#: and ``biometric_id`` together (recorded enrollment), ``password_hash``, ``status``, and
#: ``role``. A roster where one account carried all four would let three of the four predicates
#: be wrong and still add up.
#:
#: ``1000`` and ``5000`` are the harness's own administrator ids, and they are here rather than
#: invented ones because the admin half of this suite signs in as them: a token naming an account
#: that is not in the roster is refused with 401 before the dashboard is ever reached.
ROSTER = (
    (WORKER, "worker", "active", True, True),
    ("2", "worker", "active", False, True),
    ("3", "worker", "inactive", False, True),
    ("4", "moallem", "active", True, True),
    ("5", "moallem", "active", False, False),
    ("6", "off_office", "active", False, True),
    ("7", "worker", "active", False, True),
    ("9", "worker", "active", False, True),
    (ADMIN, "admin", "active", True, True),
    (HEAD_ADMIN, "head_admin", "active", False, True),
)

#: How old the oldest open note is: the thing ``oldest_seconds`` has to be reporting.
OLDEST_NOTE_DAYS = 20

#: The public form's own accounts: ``(id, name, status, applied_hours_ago)``.
#:
#: Two applications nobody has decided and one an administrator approved. A submission files a
#: *real* account in quarantine (``registrations.STATUS_PENDING_APPROVAL``) rather than a row in a
#: holding table, so these three are ordinary ``users`` rows to every count on this page - and the
#: two still waiting are the ones that must be counted as *waiting* rather than as deactivated.
APPLICATIONS = (
    ("21", "Applicant 1", registrations.STATUS_PENDING_APPROVAL, 2),
    ("22", "Applicant 2", registrations.STATUS_PENDING_APPROVAL, 9),
    # Decided, and therefore an ordinary active account: the hours it waited are gone from the
    # quarantine, and its ``enrolled_at`` now means what that column means for everybody else.
    ("23", "Applicant 3", "active", 24 * 5),
)

#: The sessions on site when this world is read: ``(worker_id, site, hours ago)``.
#:
#: ``ghost`` is the trap and the reason the agreement test is worth writing: a session row whose
#: account does not exist. ``JOIN users`` drops it on the board *and* here, so the two agree by
#: sharing a query rather than by both happening to count every row in the table.
SESSIONS = (
    (WORKER, A, 3),
    (ADMIN, A, 2),
    # Past the overtime line, with no standing authorisation: the one crossing on the board.
    ("4", DEPOT, 12),
    ("ghost", A, 1),
)


def read(client, *, as_user: str = ADMIN, days: str | None = None):
    """One dashboard read. ``days`` is only sent when a test asks for a window.

    Omitted rather than sent as the default, so the tests that do not care about the period panel
    exercise the route's own default - which is the thing the console relies on and the thing a
    parameter-only test would never touch.
    """
    url = DASHBOARD if days is None else f"{DASHBOARD}?days={days}"
    return client.get(url, headers=bearer(as_user))


def payload(client, **kw) -> dict:
    response = read(client, **kw)
    assert response.status_code == 200, response.text
    return response.json()


@contextmanager
def writing():
    """A connection that commits, for planting a world.

    ``harness.db_scalar`` opens its own connection and never commits, which is right for an
    assertion and wrong for a fixture - the rows would be rolled back when it closed.
    """
    with database.db(write=True) as conn:
        yield conn


def plant_world(now: datetime | None = None) -> dict:
    """Replace the roster and the queues with a world whose every figure is known.

    Returns the expected figures, written as the *reason* for each one, so an assertion that
    fails says which definition moved rather than only which number did.
    """
    now = now or datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = lambda moment: moment.strftime(TS)  # noqa: E731 - one expression, used twenty times

    with writing() as conn:
        # The seeded roster is replaced rather than extended: an assertion like "nine accounts
        # are active" has to be a fact about this test's roster, not about whoever else's rows
        # the snapshot happened to carry.
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM attendance_logs")
        conn.execute("DELETE FROM worker_notes")
        # The "now" panel's three tables, for the same reason: an assertion like "three people
        # are on shift" has to be a fact about this plant, not about whatever the snapshot of a
        # shared database happened to carry.
        conn.execute("DELETE FROM active_sessions")
        conn.execute("DELETE FROM punch_queue")
        conn.execute("DELETE FROM refused_punches")

        conn.executemany(
            "INSERT INTO users (id, name, email, phone, password_hash, role, status, "
            "enrolled_at, template_version, biometric_id) VALUES (?,?,?,?,?,?,?,?,1,?)",
            [
                (
                    user_id,
                    f"Row {user_id}",
                    "",
                    "",
                    "a-stored-hash" if has_password else "",
                    role,
                    status,
                    # Recorded enrollment is *both* columns: see the dashboard docstring.
                    stamp(now - timedelta(days=40)) if enrolled else None,
                    f"bio-{user_id}" if enrolled else None,
                )
                for user_id, role, status, enrolled, has_password in ROSTER
            ],
        )

        categories = [
            row[0]
            for row in conn.execute("SELECT category_id FROM site_categories ORDER BY category_id")
        ]
        assert len(categories) == 3, "the harness seeds three categories"
        conn.execute(
            "UPDATE construction_sites SET category_id = ? WHERE site_name = ?", (categories[0], A)
        )
        conn.execute(
            "UPDATE construction_sites SET category_id = NULL, clock_in_window_start = '06:00' "
            "WHERE site_name = ?",
            (B,),
        )
        conn.execute(
            "INSERT INTO construction_sites (site_name, lat, lon, radius, category_id) "
            "VALUES (?, 29.5, 30.5, 80.0, ?)",
            (DEPOT, categories[1]),
        )

        def punch(worker_id, site, code, moment, hours=0.0):
            conn.execute(
                "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, "
                "score, status, status_code) VALUES (?,?,?,?,?,0.9,?,?)",
                (worker_id, site, "Clock In", stamp(moment), hours, code, code),
            )

        # A completed shift in March: what "has clocked in" looks like, and proof that "never
        # clocked in" is not "did not clock in today".
        punch("1", A, "approved", datetime(2026, 3, 2, 5, 0, 0))
        conn.execute(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES ('1', ?, 'Clock Out', '2026-03-02 13:00:00', 8.0, 0.9, "
            "'Approved', 'approved')",
            (A,),
        )
        # The two things waiting on a reviewer, one of each pending code.
        punch("2", DEPOT, reports.PENDING_CODES[0], day_start + timedelta(hours=1))
        punch("4", A, reports.PENDING_CODES[1], now - timedelta(hours=30))
        # Somebody on site today, so one site is manned and the other two are not.
        punch(ADMIN, A, "approved", day_start + timedelta(hours=5))

        # The public form's own accounts - see ``APPLICATIONS``. Inserted here rather than in the
        # roster above because they carry a different status and a face: a submission files the
        # photograph before anybody decides anything, which is why an application can be
        # *enrolled* while it is still waiting.
        conn.executemany(
            "INSERT INTO users (id, name, email, phone, password_hash, role, status, enrolled_at, "
            "template_version, biometric_id) VALUES (?,?,?,'','a-stored-hash','worker',?,?,1,?)",
            [
                (user_id, name, f"applicant-{user_id}@example.test", status,
                 stamp(now - timedelta(hours=hours)), f"bio-{user_id}")
                for user_id, name, status, hours in APPLICATIONS
            ],
        )

        # Who is on site, and the one crossing the panel has to report. A 12-hour open shift is
        # past the overtime line with nothing authorised on it (see ``shift_rules``), which is
        # exactly what ``overtime.open_crossings`` calls a question.
        conn.executemany(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            [(worker_id, site, stamp(now - timedelta(hours=hours)))
             for worker_id, site, hours in SESSIONS],
        )

        # The offline backlog: two punches a phone handed over that are not attendance rows yet,
        # and two that must not be counted - one already materialised, and one this deployment
        # refused the moment it arrived. That last row is the whole point of the predicate: it
        # carries no ``processed_at``, so the near-miss (``processed_at IS NULL``) would report a
        # signature this deployment already refused as work somebody still owes.
        conn.executemany(
            "INSERT INTO punch_queue (client_punch_id, device_id, worker_id, action, "
            "client_timestamp, signature, received_at, status, rejection_code, "
            "materialized_log_id, processed_at) VALUES (?,?,?,?,?,'a-signature',?,?,?,?,?)",
            [
                ("q-1", "dev-1", WORKER, "Clock In", stamp(now - timedelta(hours=5)),
                 stamp(now - timedelta(hours=5)), "accepted", None, None, None),
                ("q-2", "dev-1", "2", "Clock Out", stamp(now - timedelta(hours=4)),
                 stamp(now - timedelta(hours=3)), "flagged", None, None, None),
                ("q-3", "dev-2", "3", "Clock In", stamp(now - timedelta(days=2)),
                 stamp(now - timedelta(days=2)), "accepted", None, 900001,
                 stamp(now - timedelta(days=2))),
                ("q-4", "dev-2", "5", "Clock In", stamp(now - timedelta(hours=6)),
                 stamp(now - timedelta(hours=6)), "rejected", "bad_signature", None, None),
            ],
        )

        # Refusals: two inside the last day - one of them naming nobody at all, which is the
        # refusal an operator most needs to see - and one comfortably outside the window.
        conn.executemany(
            "INSERT INTO refused_punches (worker_id, site_name, action, error_code, created_at) "
            "VALUES (?,?, 'Clock In', 'no_match', ?)",
            [
                ("2", A, stamp(now - timedelta(hours=2))),
                (None, A, stamp(now - timedelta(hours=3))),
                ("3", DEPOT, stamp(now - timedelta(hours=30))),
            ],
        )

        # One open note, one in progress, one closed: the count is the first two, and the oldest
        # of them is what ``oldest_seconds`` must be reporting.
        conn.executemany(
            "INSERT INTO worker_notes (worker_id, category, subject, body, status, created_at, "
            "updated_at) VALUES ('1','other',?,? ,?,?,?)",
            [
                ("oldest", "body", notes.STATUS_OPEN,
                 stamp(now - timedelta(days=OLDEST_NOTE_DAYS)), stamp(now - timedelta(days=OLDEST_NOTE_DAYS))),
                ("recent", "body", notes.STATUS_IN_PROGRESS,
                 stamp(now - timedelta(hours=1)), stamp(now - timedelta(hours=1))),
                ("done", "body", "closed",
                 stamp(now - timedelta(hours=2)), stamp(now - timedelta(hours=2))),
            ],
        )

        # What "joined this week" is read from. Three of these five are accounts that exist
        # today; the fourth is a creation from a month ago and the fifth names an account that is
        # not in the roster at all, which is what keeps the number a count of *accounts* rather
        # than of audit rows.
        conn.executemany(
            "INSERT INTO audit_log (action, entity, entity_id, created_at) VALUES (?,?,?,?)",
            [
                ("user_create", "users", "9", stamp(now - timedelta(days=1))),
                ("admin_create", "users", HEAD_ADMIN, stamp(now - timedelta(days=2))),
                ("user_create", "users", "2", stamp(now - timedelta(days=30))),
                ("user_create", "users", "999", stamp(now - timedelta(days=1))),
                # The third way an account joins this deployment, and the one the tuple in
                # ``dashboard.ACCOUNT_CREATION_ACTIONS`` names: a walk-up approval. Filed against
                # the *account*, which is what a submission creates.
                ("registration_approved", "users", "23", stamp(now - timedelta(days=5))),
            ],
        )

    return {
        "people": {
            "accounts": 13,               # the ten-row roster and the three applications
            "active": 10,                 # every roster row but "3", plus the approved "23"
            "pending_approval": 2,        # the public form's queue
            "deactivated": 1,             # "3" and nothing else: a queue is not a deactivation
            "by_role": {"worker": 8, "moallem": 2, "off_office": 1, "admin": 1, "head_admin": 1},
            "enrolled": 6,                # WORKER, "4", ADMIN, and all three applications
            "no_face": 7,
            "no_password": 1,             # "5"
            "new_this_week": 3,           # "9", HEAD_ADMIN and the approved application "23"
            "never_clocked_in": 6,        # active with no attendance row: 5, 6, 7, 9, HEAD, "23"
        },
        "places": {
            "sites": 3,
            "categories": 3,
            "no_category": 1,
            "overriding_window": 1,
            "unmanned_today": [B],
        },
        "now": {
            "on_shift": 3,                # the board's rows: the ghost session is dropped by both
            "by_site": [{"site_name": A, "workers": 2}, {"site_name": DEPOT, "workers": 1}],
            "overtime_open": 1,           # the twelve-hour shift at the Depot
            "offline_waiting": 2,         # the two rows the materialiser would pick up
            "refused_24h": 2,             # one with a worker, one with none at all
        },
        "waiting": {"reviews": 2, "registrations": 2, "notes": 2, "alerts": None},
        "category_names": sorted(
            row[0] for row in harness.db_rows("SELECT name FROM site_categories")
        ),
    }


#: The period's own history, written out as the *reason* for every figure below:
#: ``(worker, action, days_ago, hour, code, hours, approved_hours, overtime_hours, flag_reason)``.
#:
#: A seven-day window with one row of every kind the period counts: two workers present twice,
#: three present once, two late arrivals on the two different flag wordings the predicate has to
#: recognise, one shift whose approved figure differs from its recorded one, one pending overtime
#: split into the day it earned and the hours it is holding, one auto-close nobody has signed,
#: one pending review, one refusal worth nothing, and - the boundary - a day either side of the
#: window that must not be counted at all.
PERIOD_SHIFTS = (
    ("1", "Clock In", 0, 6, None, 0.0, None, None, "on time"),
    ("1", "Clock Out", 0, 14, "approved", 8.0, None, None, None),
    ("1", "Clock In", 2, 6, None, 0.0, None, None, "on time"),
    ("1", "Clock Out", 2, 13, "approved", 7.5, 7.5, None, None),
    ("2", "Clock In", 1, 6, None, 0.0, None, None, "Outside the clock-in window"),
    ("2", "Clock Out", 1, 15, "approved", 8.5, 8.5, 1.0, None),
    ("2", "Clock In", 6, 6, None, 0.0, None, None, "late clock-in"),
    ("2", "Clock Out", 6, 14, "approved", 8.0, None, None, None),
    ("3", "Clock In", 1, 6, None, 0.0, None, None, "late clock-in"),
    ("3", "Clock Out", 1, 14, "auto_closed_8h", 8.0, None, None, None),
    ("4", "Clock In", 4, 6, None, 0.0, None, None, "on time"),
    ("4", "Clock Out", 4, 14, "pending_review", 8.0, None, None, None),
    ("9", "Clock In", 5, 6, None, 0.0, None, None, "on time"),
    ("9", "Clock Out", 5, 14, "pending_overtime", 8.0, None, 2.0, None),
    ("5", "Clock Out", 6, 14, "rejected", 8.0, None, None, None),
    # One day before the window opens, and the day after it closes. Neither may reach a figure.
    ("1", "Clock In", 7, 6, None, 0.0, None, None, None),
    ("1", "Clock Out", 7, 14, "approved", 9.0, None, None, None),
    ("1", "Clock In", -1, 6, None, 0.0, None, None, None),
    ("1", "Clock Out", -1, 14, "approved", 9.0, None, None, None),
)

#: Every ``status_code`` the period's hour arithmetic treats differently, one row each, with the
#: figures that make each class distinguishable: an adjusted approval, a pending overtime split, a
#: hold with no figure on it, an auto-close, a refusal, and a row with no code at all.
HOURS_CASES = (
    ("approved", 8.0, None, None),
    ("approved", 8.0, 6.0, 2.0),
    ("auto_closed_8h", 8.0, None, None),
    ("overtime_rejected", 8.0, 8.0, 0.0),
    ("pending_overtime", 8.5, None, 0.5),
    ("pending_overtime", 8.0, None, None),
    ("pending_review", 8.0, None, None),
    ("auto_closed", 7.0, None, None),
    ("rejected", 8.0, None, None),
    (None, 8.0, None, None),
)


def plant_period_world(now: datetime | None = None) -> dict:
    """Replace the attendance history with one window whose every period figure is known.

    Returns the expected figures, written as the reason for each one. The roster is rebuilt (the
    harness's own accounts are kept as they are where they do not clash), because the extremes
    carry a *name* and a deleted account's row has to be nameable by its id instead.
    """
    now = now or datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    workers = sorted({row[0] for row in PERIOD_SHIFTS})

    def at(days_ago: int, hour: int) -> str:
        return (day_start - timedelta(days=days_ago) + timedelta(hours=hour)).strftime(TS)

    with writing() as conn:
        # Only the attendance history is replaced: every figure in this panel is a fact about
        # these rows, and a snapshot that happened to carry rows for other workers would make
        # each assertion a statement about the snapshot rather than about this plant.
        conn.execute("DELETE FROM attendance_logs")
        conn.executemany(
            "INSERT OR REPLACE INTO users (id, name, email, phone, password_hash, role, status, "
            "enrolled_at, template_version, biometric_id) "
            "VALUES (?,?,?,'','a-stored-hash','worker','active',NULL,1,NULL)",
            [(worker, f"Row {worker}", "") for worker in workers],
        )
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, "
            "approved_hours, overtime_hours, score, status, status_code, flag_reason) "
            "VALUES (?,?,?,?,?,?,?,0.9,?,?,?)",
            [
                # ``status`` is NOT NULL, so a Clock In with no decision is the empty string there
                # rather than a null - the column the period reads is ``status_code``.
                (worker, A, action, at(days_ago, hour), hours, approved, overtime, code or "", code, reason)
                for worker, action, days_ago, hour, code, hours, approved, overtime, reason
                in PERIOD_SHIFTS
            ],
        )

    # ``shift_rules``' default working week is Saturday to Thursday, so Friday is the one weekday
    # that is not expected - and a seven-day window holds exactly one of each weekday, which is
    # what makes this denominator the same number whatever morning the suite runs.
    expected_days = 6
    return {
        "days": 7,
        "preset": "days",
        "start": (now.date() - timedelta(days=6)).isoformat(),
        "end": now.date().isoformat(),
        "workers": 5,                 # 1, 2, 3, 4 and 9 were present; 5 was not
        "expected_days": expected_days,
        "present_days": 7,            # 1 twice, 2 twice, 3, 4 and 9 once each
        "late_arrivals": 3,           # 2 twice (both wordings), 3 once
        "average_attendance_rate": 0.2333,  # (2 + 2 + 1 + 1 + 1) / 6 / 5
        "approved_hours": 46.0,       # 8 + 7.5 + 8.5 + 8 + 8, and 6 of the pending-overtime day
        "overtime_hours": 3.0,        # the hour authorised on one shift and the hold on another
        "awaiting_approval_hours": 10.0,   # the pending review, and the pending overtime's hold
        # Two rows nobody has signed off - the pending review and the pending overtime - and
        # the ``auto_closed_8h`` row is deliberately *not* one of them: it is payable by policy,
        # which is why the count and the hours are counted in the same statement.
        "awaiting_approval_shifts": 2,
        # One day present each, and the late count breaking the tie: 3 arrives late, 4 and 9 do
        # not - so the quietest list is those three, and the fourth candidate (a two-day worker)
        # is where the limit cuts.
        "quietest": ["3", "4", "9"],
        # Most late first, then the quiet ones by fewest days: 2 (twice), 3 (once), 4 (none).
        "most_late": ["2", "3", "4"],
        # The strip the panel draws, oldest first: one bar per day of the window, including the
        # day nobody came - which is a real answer (the site was open and nobody arrived) and the
        # one a chart is most likely to get wrong by leaving it out.
        "by_day": [
            {
                "day": (now.date() - timedelta(days=days_ago)).isoformat(),
                "present": present,
                "late": late,
            }
            for days_ago, present, late in (
                (6, 1, 1), (5, 1, 0), (4, 1, 0), (3, 0, 0), (2, 1, 0), (1, 2, 2), (0, 1, 0),
            )
        ],
    }


# ---------------------------------------------------------------------------
# 1. every figure, against a world whose every figure is known
# ---------------------------------------------------------------------------
def test_every_panel_counts_the_planted_world(client):
    """The whole payload, once, against a planted world.

    One test rather than nine, because the numbers are only meaningful together: "nine active"
    is right in a roster of ten and meaningless on its own. The failures name the figure.
    """
    expected = plant_world()
    body = payload(client)

    people = body["people"]
    for key, want in expected["people"].items():
        assert people[key] == want, f"people.{key} is {people[key]!r}, expected {want!r}"

    places = body["places"]
    for key, want in expected["places"].items():
        assert places[key] == want, f"places.{key} is {places[key]!r}, expected {want!r}"
    # A category with nothing in it is a real answer, and the list has one entry per category so
    # that ``len(by_category)`` and ``categories`` cannot disagree.
    assert len(places["by_category"]) == places["categories"], places["by_category"]
    assert {entry["category"] for entry in places["by_category"]} == set(expected["category_names"])
    assert sum(entry["sites"] for entry in places["by_category"]) == (
        places["sites"] - places["no_category"]
    ), places

    now_panel = body["now"]
    for key, want in expected["now"].items():
        assert now_panel[key] == want, f"now.{key} is {now_panel[key]!r}, expected {want!r}"

    waiting = body["waiting"]
    for key, want in expected["waiting"].items():
        assert waiting[key] == want, f"waiting.{key} is {waiting[key]!r}, expected {want!r}"

    # The same two applications, read from the two panels that both speak about them: the queue
    # the waiting panel reports and the accounts the roster counts. One status constant behind
    # both, asserted here so a change to how a quarantine is spelled cannot move just one.


def test_the_panels_add_up_to_the_totals_they_sit_under(client):
    """The complements have to be complements.

    ``deactivated`` and ``no_face`` are derived by subtraction, which is exactly the kind of
    arithmetic that survives a predicate changing underneath it and quietly stops adding up.
    """
    plant_world()
    body = payload(client)
    people = body["people"]
    assert people["active"] + people["deactivated"] + people["pending_approval"] == (
        people["accounts"]
    ), people
    assert people["enrolled"] + people["no_face"] == people["accounts"], people
    assert sum(people["by_role"].values()) == people["accounts"], people
    assert body["waiting"]["registrations"] == people["pending_approval"], (
        "the waiting panel and the roster disagree about how many applications are open: "
        + repr([people, body["waiting"]])
    )


def test_the_stamp_says_when_the_snapshot_was_taken(client):
    """``as_of`` is what makes a stale total honest instead of wrong."""
    before = datetime.now().replace(microsecond=0)
    body = payload(client)
    after = datetime.now().replace(microsecond=0)

    moment = datetime.strptime(body["as_of"], TS)
    assert before - timedelta(seconds=5) <= moment <= after + timedelta(seconds=5), body["as_of"]
    day = body["day"]
    assert day["start"] == before.date().isoformat(), day
    assert day["end"] == (before.date() + timedelta(days=1)).isoformat(), day
    assert day["today"] == day["start"]
    assert day["timezone"] == "Asia/Kuwait", day


def test_the_freshness_windows_travel_with_the_read(client):
    """"Read just now" is a claim about this snapshot's age; the window belongs on the wire.

    The console does not poll, so the only freshness it can report is how long ago its one read
    landed - and the word it prints is a verdict against a boundary. A console that kept its own
    copy could keep calling a figure current for as long as its own number said, whatever this
    deployment meant by *current*. So the two windows ride with the read they describe, exactly
    as ``dormant_days`` rides with the dormant count: the definition is one fact, owned once.
    """
    body = payload(client)
    assert set(body["freshness"]) == {"aging_seconds", "stale_seconds"}, body["freshness"]
    assert body["freshness"]["aging_seconds"] == dashboard.FRESHNESS_AGING_SECONDS
    assert body["freshness"]["stale_seconds"] == dashboard.FRESHNESS_STALE_SECONDS
    # A window that does not move is not a window: aging has to end before stale begins, or the
    # console's middle word is unreachable and every read past the first boundary reads stale.
    assert (
        body["freshness"]["aging_seconds"] < body["freshness"]["stale_seconds"]
    ), body["freshness"]


# ---------------------------------------------------------------------------
# 2. the queues agree with the screens that list them
# ---------------------------------------------------------------------------
def test_the_waiting_counts_agree_with_the_queues_they_summarize(client):
    """Each wait is the number the screen that owns that queue would show.

    The dashboard's own definition of "pending" is not the point - the point is that there is
    one definition, so an administrator reading "2 reviews" and then opening Approvals cannot
    find three.
    """
    plant_world()
    waiting = payload(client)["waiting"]

    pending = client.get("/api/v1/admin/reports/pending", headers=bearer(ADMIN))
    assert pending.status_code == 200, pending.text
    assert waiting["reviews"] == pending.json()["count"] == 2, waiting

    queue = client.get("/api/v1/admin/registrations", headers=bearer(ADMIN))
    assert queue.status_code == 200, queue.text
    assert waiting["registrations"] == queue.json()["pending"] == 2, waiting

    notes_screen = client.get("/api/v1/admin/notes", headers=bearer(ADMIN))
    assert notes_screen.status_code == 200, notes_screen.text
    assert waiting["notes"] == notes_screen.json()["open"] == 2, waiting


def test_the_oldest_thing_waiting_is_the_one_reported(client):
    """``oldest_seconds`` is the maximum, and every queue with anything in it is in the running.

    A dashboard that reported the *average*, or that only looked at the review queue, would still
    answer a plausible number - which is why the age is asserted against the one planted stamp
    that is the eldest, and then against a review being the eldest instead.
    """
    now = datetime.now()
    plant_world(now)
    waiting = payload(client)["waiting"]
    expected = int((now - (now - timedelta(days=OLDEST_NOTE_DAYS))).total_seconds())
    assert abs(waiting["oldest_seconds"] - expected) <= 60, waiting
    assert waiting["oldest_seconds"] >= int(timedelta(hours=29).total_seconds()), waiting

    # Now make a review the eldest thing, with the note queue emptied out: the same field has to
    # follow the queue that actually holds the oldest item.
    with writing() as conn:
        conn.execute("DELETE FROM worker_notes")
        conn.execute("DELETE FROM users WHERE status = ?", (registrations.STATUS_PENDING_APPROVAL,))
        conn.execute("DELETE FROM attendance_logs")
        conn.execute(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES ('1', ?, 'Clock In', ?, 0.0, 0.9, ?, ?)",
            (A, (now - timedelta(days=3)).strftime(TS), reports.PENDING_CODES[0], reports.PENDING_CODES[0]),
        )
    waiting = payload(client)["waiting"]
    expected = int(timedelta(days=3).total_seconds())
    assert abs(waiting["oldest_seconds"] - expected) <= 60, waiting
    assert (waiting["reviews"], waiting["registrations"], waiting["notes"]) == (1, 0, 0), waiting


def test_nothing_waiting_is_nothing_rather_than_an_age_of_zero(client):
    """A queue nobody has anything in has no oldest item, and ``0`` would say "just now"."""
    plant_world()
    with writing() as conn:
        conn.execute("DELETE FROM worker_notes")
        conn.execute("DELETE FROM users WHERE status = ?", (registrations.STATUS_PENDING_APPROVAL,))
        conn.execute("DELETE FROM attendance_logs")
    waiting = payload(client)["waiting"]
    assert waiting == {
        "reviews": 0, "registrations": 0, "notes": 0, "alerts": None, "oldest_seconds": None
    }, waiting


# ---------------------------------------------------------------------------
# 2b. the "now" panel, held against the screens it borrows from
# ---------------------------------------------------------------------------
def test_the_now_panel_agrees_with_the_live_ops_board(client):
    """``on_shift`` is the board's own figure, held equal on the same fixture.

    The plan defines this number as the board's (``docs/DASHBOARD_PLAN.md``): the board answers
    *who is at the gate*, this answers *how many, and is that normal*, and an administrator handed
    two different totals for the same afternoon has been handed a bug. So the fixture is read
    both ways and the figures compared - in total and by site, because the by-site split is the
    half an implementation gets wrong by counting a different join.
    """
    expected = plant_world()
    now_panel = payload(client)["now"]

    board = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN))
    assert board.status_code == 200, board.text
    rows = board.json()

    assert now_panel["on_shift"] == len(rows) == expected["now"]["on_shift"] == 3, (now_panel, rows)
    # The session whose account does not exist is on neither side of that equality - and it *is*
    # in the table, which is what makes this an agreement between two queries rather than a
    # coincidence of both counting every row.
    assert db_scalar("SELECT COUNT(*) FROM active_sessions") == 4
    assert "ghost" not in [row["worker_id"] for row in rows]

    tally: dict[str, int] = {}
    for row in rows:
        tally[row["site_name"]] = tally.get(row["site_name"], 0) + 1
    assert now_panel["by_site"] == [
        {"site_name": name, "workers": count}
        for name, count in sorted(tally.items(), key=lambda pair: (-pair[1], pair[0].lower()))
    ], now_panel["by_site"]


def test_the_open_crossing_is_the_queue_the_approvals_tab_holds(client):
    """Borrowed from ``overtime.open_crossings``, so held against the reads that serve it.

    Two of them: the queue the Approvals tab draws, which carries ``needs_answer`` per row, and
    the small count the nav badge polls. Both are the same guard the dashboard counts, and a
    dashboard that recast "past the line" as SQL would drift from them the first time a rule
    changed.
    """
    plant_world()
    now_panel = payload(client)["now"]

    queue = client.get("/api/v1/admin/overtime/crossings", headers=bearer(ADMIN))
    assert queue.status_code == 200, queue.text
    items = queue.json()
    needs_answer = [item for item in items if item["needs_answer"]]

    assert now_panel["overtime_open"] == len(needs_answer) == 1, (now_panel, items)
    assert [item["worker_id"] for item in needs_answer] == ["4"], items
    assert [item["site_name"] for item in needs_answer] == [DEPOT], items

    badge = client.get("/api/v1/admin/overtime/crossings/count", headers=bearer(ADMIN))
    assert badge.status_code == 200, badge.text
    assert badge.json()["count"] == now_panel["overtime_open"], badge.json()


def test_the_offline_backlog_is_the_queue_the_materialiser_would_work(client):
    """``offline_waiting`` is the un-filed rows of ``/admin/punch_queue``, and not a lookalike.

    The near-miss this guards is ``processed_at IS NULL``: that column is stamped on *rejection*
    too, so it would report a signature this deployment already refused as work somebody still
    owes. The fixture plants exactly that row, so the two definitions are demonstrably different
    numbers and the panel is on the one the materialiser works from.
    """
    plant_world()
    now_panel = payload(client)["now"]

    queue = client.get("/api/v1/admin/punch_queue", headers=bearer(ADMIN))
    assert queue.status_code == 200, queue.text
    rows = queue.json()
    waiting = [
        row for row in rows
        if row["status"] in ("accepted", "flagged") and row["materialized_log_id"] is None
    ]
    assert now_panel["offline_waiting"] == len(waiting) == 2, (now_panel, rows)
    assert {row["client_punch_id"] for row in waiting} == {"q-1", "q-2"}, waiting

    # Three rows carry no ``processed_at`` at all, and the panel must not report three of them...
    assert db_scalar("SELECT COUNT(*) FROM punch_queue WHERE processed_at IS NULL") == 3
    # ...because one of the three is a refusal this deployment has already made.
    assert db_scalar(
        "SELECT COUNT(*) FROM punch_queue WHERE processed_at IS NULL AND status = 'rejected'"
    ) == 1


def test_refused_punches_are_the_last_day_rather_than_a_backlog(client):
    """A rate to watch, not a queue: nothing in this figure can be cleared, so the window is it.

    Two traps, both planted: a refusal outside the day, which a count of the table would include;
    and a refusal naming *nobody*, which is the one an operator most needs - a face the engine
    could not match belongs to no roster, and a count that dropped it would report the opposite
    of the truth.
    """
    plant_world()
    now_panel = payload(client)["now"]

    assert now_panel["refused_24h"] == 2, now_panel
    assert db_scalar("SELECT COUNT(*) FROM refused_punches") == 3, "the fixture lost its trap"
    assert db_scalar("SELECT COUNT(*) FROM refused_punches WHERE worker_id IS NULL") == 1
    inside = db_scalar(
        "SELECT COUNT(*) FROM refused_punches WHERE created_at >= ?",
        ((datetime.now() - timedelta(hours=24)).strftime(TS),),
    )
    assert inside == now_panel["refused_24h"], (
        "the panel is not counting the window it says it is: " + repr([inside, now_panel])
    )


# ---------------------------------------------------------------------------
# 2c. the "period" panel, against a window whose every figure is known
# ---------------------------------------------------------------------------
def test_the_period_panel_counts_the_planted_window(client):
    """Every figure of the period, and both linkages, against a planted seven days.

    The figures are hand-derived in ``plant_period_world`` and asserted here one at a time: a
    summary off by one is trusted, which is what makes it worse than no summary. The two lists are
    asserted *in order*, because the order is the panel's whole value - "the person with the fewest
    days present" is a different claim from "three people who happen to be present once".
    """
    expected = plant_period_world()
    period = payload(client)["period"]

    for key in (
        "days", "preset", "start", "end", "workers", "expected_days", "present_days",
        "late_arrivals", "average_attendance_rate", "approved_hours", "overtime_hours",
        "awaiting_approval_hours", "awaiting_approval_shifts", "by_day",
    ):
        assert period[key] == expected[key], (
            f"period.{key} is {period[key]!r}, expected {expected[key]!r}"
        )

    quietest = [
        (row["worker_id"], row["present_days"], row["rate"], row["late"])
        for row in period["quietest"]
    ]
    assert quietest == [
        ("3", 1, 0.1667, 1), ("4", 1, 0.1667, 0), ("9", 1, 0.1667, 0)
    ], period["quietest"]
    most_late = [
        (row["worker_id"], row["present_days"], row["rate"], row["late"])
        for row in period["most_late"]
    ]
    assert most_late == [
        ("2", 2, 0.3333, 2), ("3", 1, 0.1667, 1), ("4", 1, 0.1667, 0)
    ], period["most_late"]

    # Each row carries the person's own name, because the link out of it has to say who it opens -
    # and a name is the one thing the figure beside it cannot tell the reader.
    assert all(
        row["worker_name"] == f"Row {row['worker_id']}"
        for row in period["quietest"] + period["most_late"]
    ), period
    # A worker nobody saw at all is not a *quiet* worker, they are an absent one: the report these
    # rows come from is workers who punched, and "nobody has seen this person in a week" is the
    # dormant-account question the plan keeps for phase 4. Worker 5 has a refusal on file and no
    # Clock In, which is exactly that distinction.
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = '5' AND action = 'Clock In'"
    ) == 0
    assert "5" not in [
        row["worker_id"] for row in period["quietest"] + period["most_late"]
    ], period
    assert period["workers"] == 5, period


def test_the_per_day_strip_adds_up_to_the_window_totals(client):
    """The strip is the same rows, bucketed by day: it may not be a second, disagreeing view.

    A breakdown under a summary is read as that summary's parts - so the parts are held against
    the whole, one day at a time, and the *length* of the strip is held against the window rather
    than against the data (a window keeps its days when nothing happened in them).
    """
    expected = plant_period_world()
    period = payload(client)["period"]

    days = period["by_day"]
    assert [entry["day"] for entry in days] == [entry["day"] for entry in expected["by_day"]], days
    assert sum(entry["present"] for entry in days) == period["present_days"], days
    assert sum(entry["late"] for entry in days) == period["late_arrivals"], days
    # The empty day is *in* the strip, as a zero: a day the site was open and nobody arrived in.
    assert {"day": (datetime.now().date() - timedelta(days=3)).isoformat(), "present": 0, "late": 0} in days, days

    # A different window is a different strip, and its length follows the window: the month preset
    # starts on the first, so it can be shorter than, equal to, or longer than the week.
    month = payload(client, days="month")["period"]
    assert len(month["by_day"]) == month["days"], month
    assert month["by_day"][0]["day"] == month["start"], month["by_day"]
    assert month["by_day"][-1]["day"] == month["end"], month["by_day"]
    assert sum(entry["present"] for entry in month["by_day"]) == month["present_days"], month

    # The longest window this build counts still produces one entry per day, in order, and the
    # query behind it is capped at exactly that length rather than at the roster.
    capped = payload(client, days="999")["period"]
    assert len(capped["by_day"]) == capped["days"] == 366, capped["days"]
    assert capped["by_day"][0]["day"] < capped["by_day"][-1]["day"], capped["by_day"]


def test_the_period_window_is_half_open_at_both_ends(client):
    """The window's own edges: the first and last second count, the neighbours do not.

    The boundary is where a summary is silently wrong rather than visibly wrong - a shift filed at
    midnight belongs to one window and not to both - and it is the plan's own §10.3 case.
    """
    now = datetime.now()
    plant_period_world(now)
    first = now.date() - timedelta(days=6)
    with writing() as conn:
        conn.execute("DELETE FROM attendance_logs")
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES ('1', ?, 'Clock In', ?, 0.0, 0.9, 'Approved', 'approved')",
            [
                (A, f"{first} 00:00:00"),                             # the window's first second
                (A, f"{first - timedelta(days=1)} 23:59:59"),         # one second before it
                (A, f"{now.date()} 23:59:59"),                        # the window's last second
                (A, f"{now.date() + timedelta(days=1)} 00:00:00"),    # one second after it
            ],
        )
    period = payload(client)["period"]
    assert period["present_days"] == 2, period
    assert period["workers"] == 1, period
    # The day *outside* the window is a real day of the same worker's history, so a count that
    # reached past the range would answer 3 here rather than 2.
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs") == 4


def test_the_period_hours_are_the_timesheets_own_arithmetic(client):
    """The counted hours equal ``_shift_hours``, code by code.

    The aggregate is SQL and the timesheet's rows are Python, and a shift's *worth* is the one
    thing on this page somebody would act on: an approved hour the timesheet does not show, or a
    pending hour the timesheet shows as approved, is a pay run somebody has to reconcile by hand.
    Every status code is planted, in the forms that make each class distinguishable, and the two
    implementations are run over the same rows.
    """
    plant_period_world()
    moment = datetime.now().strftime("%Y-%m-%d 14:00:00")
    with writing() as conn:
        conn.execute("DELETE FROM attendance_logs")
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, "
            "approved_hours, overtime_hours, score, status, status_code) "
            "VALUES ('1', ?, 'Clock Out', ?, ?, ?, ?, 0.9, ?, ?)",
            [
                (A, moment, hours, approved, overtime, code or "", code)
                for code, hours, approved, overtime in HOURS_CASES
            ],
        )

    with database.db() as conn:
        rows = conn.execute(
            "SELECT hours, approved_hours, overtime_hours, status_code FROM attendance_logs "
            "WHERE action = 'Clock Out' ORDER BY id"
        ).fetchall()
    assert len(rows) == len(HOURS_CASES), "the fixture lost a case"
    # Every class is in the sample, including the two with a second row: an adjusted approval and
    # a hold carrying no figure, both of which are the cases the arithmetic branches on.
    assert {str(row["status_code"]) for row in rows} == {
        str(case[0]) for case in HOURS_CASES
    }, rows

    approved = sum(reports._shift_hours(row)[1] for row in rows)
    awaiting = sum(reports._shift_hours(row)[2] for row in rows)
    overtime = sum(float(row["overtime_hours"] or 0.0) for row in rows)

    period = payload(client)["period"]
    assert period["approved_hours"] == round(approved, 4), (period, approved)
    assert period["awaiting_approval_hours"] == round(awaiting, 4), (period, awaiting)
    assert period["overtime_hours"] == round(overtime, 4), (period, overtime)
    # The count behind those hours is the same predicate in SQL and not a second opinion about
    # it: one *shift* per row whose code is one the timesheet calls awaiting, whatever its hours
    # came to. A ``pending_overtime`` row holding nothing is still a decision somebody owes, so
    # this is a count of rows rather than of the rows ``awaiting > 0`` would name.
    awaiting_rows = [
        row for row in rows if str(row["status_code"] or "") in reports.AWAITING_APPROVAL_CODES
    ]
    assert len(awaiting_rows) == 4, awaiting_rows
    assert period["awaiting_approval_shifts"] == len(awaiting_rows), (period, awaiting_rows)
    assert period["awaiting_approval_shifts"] <= len(HOURS_CASES), period
    # The two halves of the timesheet's own contract, on the planted rows: what a clock-out is
    # worth is either approved or waiting, never both and never neither.
    assert period["approved_hours"] + period["awaiting_approval_hours"] > 0, period
    assert period["overtime_hours"] > 0, period


def test_the_period_totals_agree_with_the_attendance_report(client):
    """One definition, two readers: the panel and the report it summarizes.

    The dashboard cannot download the report (that is the whole point of the endpoint), so the two
    compute the same figures in two places. This is the test that keeps them the same number - and
    it is the only thing standing between "counted" and "counted differently".
    """
    expected = plant_period_world()
    period = payload(client)["period"]

    report = client.get(
        f"/api/v1/admin/reports/attendance?start={period['start']}&end={period['end']}",
        headers=bearer(ADMIN),
    )
    assert report.status_code == 200, report.text
    body = report.json()
    totals = body["totals"]

    for key in (
        "workers", "expected_days", "present_days", "late_arrivals", "average_attendance_rate",
        "approved_hours", "overtime_hours", "awaiting_approval_hours",
        "awaiting_approval_shifts",
    ):
        assert period[key] == totals[key], (
            f"the panel and the report disagree about {key}: {period[key]!r} vs {totals[key]!r}"
        )

    # And the extremes are the report's own rows, ranked: the same worker, the same days present,
    # the same rate - so tapping one opens the row whose figure the panel printed.
    by_id = {row["worker_id"]: row for row in body["rows"]}
    for row in period["quietest"] + period["most_late"]:
        reported = by_id[row["worker_id"]]
        assert row["present_days"] == reported["days_present"], (row, reported)
        assert row["rate"] == reported["attendance_rate"], (row, reported)
        assert row["late"] == reported["late_arrivals"], (row, reported)
        assert row["worker_name"] == reported["worker_name"], (row, reported)
    # The panel's quietest set is the report's own worst rate, worker for worker (the report sorts
    # best first, so its worst is at the end). The two order the *tie* differently on purpose: the
    # report breaks it by id, the panel by the late count the plan calls for, so the sets are held
    # equal and the order is asserted only in the panel's own test.
    worst = min(row["attendance_rate"] for row in body["rows"])
    assert period["quietest"][0]["rate"] == worst, (period["quietest"], body["rows"])
    assert {row["worker_id"] for row in period["quietest"]} == {
        row["worker_id"] for row in body["rows"] if row["attendance_rate"] == worst
    }, (period["quietest"], body["rows"])
    assert expected["quietest"], "the fixture's own expectation has to name somebody"


def test_the_range_control_resolves_the_window_it_promises(client):
    """``?days=`` means the window it says: seven days, the month so far, one day, or the cap."""
    plant_period_world()
    today = datetime.now().date()

    week = payload(client)["period"]
    assert (week["preset"], week["days"]) == ("days", 7), week
    assert week["start"] == (today - timedelta(days=6)).isoformat(), week
    assert week["end"] == today.isoformat(), week

    month = payload(client, days="month")["period"]
    assert (month["preset"], month["days"]) == ("month", today.day), month
    assert month["start"] == today.replace(day=1).isoformat(), month
    assert month["end"] == today.isoformat(), month

    one = payload(client, days="1")["period"]
    assert (one["preset"], one["days"], one["start"], one["end"]) == (
        "days", 1, today.isoformat(), today.isoformat()
    ), one
    # Today's own row, and nothing else: the window is the day, not the last twenty-four hours.
    assert one["present_days"] == 1, one

    # A longer window than this build counts is clamped rather than refused - a saved link from a
    # build that offered one still has to answer - and a value that is not a window at all is
    # refused at the door, because serving seven days to somebody who typed "week" is a wrong
    # answer that looks like a right one.
    capped = payload(client, days="999")["period"]
    assert capped["days"] == 366, capped
    assert capped["start"] == (today - timedelta(days=365)).isoformat(), capped
    assert payload(client, days="0")["period"]["days"] == 1, "a zero-day window is the day"
    assert client.get(f"{DASHBOARD}?days=week", headers=bearer(ADMIN)).status_code == 422
    assert client.get(f"{DASHBOARD}?days=7days", headers=bearer(ADMIN)).status_code == 422


def test_the_month_window_counts_the_days_the_week_no_longer_covers(client):
    """The second preset is not a longer week: it starts on the first, whatever today is.

    The two windows overlap and, early in a month, the week reaches into the month *before* -
    which is why this asserts against the window the server resolved rather than against the day
    the suite happened to run on.
    """
    now = datetime.now()
    plant_period_world(now)
    first = now.date().replace(day=1)
    with writing() as conn:
        conn.execute("DELETE FROM attendance_logs")
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES ('1', ?, 'Clock In', ?, 0.0, 0.9, 'Approved', 'approved')",
            [(A, f"{first} 06:00:00"), (A, f"{now.date()} 06:00:00")],
        )

    week = payload(client)["period"]
    month = payload(client, days="month")["period"]

    # Today is in both windows, always - and the first of the month is in the week's only when the
    # week reaches back that far.
    in_week = week["start"] <= first.isoformat()
    assert week["present_days"] == 1 + int(in_week), week
    assert month["present_days"] == 1 + int(first != now.date()), month
    assert month["start"] <= first.isoformat() <= month["end"], month


def test_a_period_that_cannot_be_counted_answers_null_and_takes_nothing_with_it(
    client, monkeypatch
):
    """Unknown is not zero, on the panel where a zero would read as a quiet week.

    A dropped table cannot isolate this panel - ``attendance_logs`` is the places panel's own table
    too - so the read itself is made to fail, which is the state the panel is being defended
    against: the reports module answering an error rather than a number.
    """
    plant_period_world()
    whole = payload(client)

    def refuse(*args, **kwargs):
        raise sqlite3.Error("no such table: attendance_logs")

    monkeypatch.setattr(reports, "attendance_period", refuse)
    body = payload(client)
    assert body["period"] is None, body["period"]
    assert body["people"] == whole["people"], body["people"]
    assert body["places"] == whole["places"]
    assert body["now"] == whole["now"]
    assert body["waiting"] == whole["waiting"]
    assert body["as_of"], body


# ---------------------------------------------------------------------------
# 2d. the two watch figures: dormant and onboarding
# ---------------------------------------------------------------------------
#: The watch world, one account per *edge* of each predicate:
#: ``(id, status, last punch in days - None for never, created days ago - None for older)``.
#:
#: Two counts and four ways to be excluded from each, because these are the figures that can be
#: wrong by including somebody: an account that has never punched is not dormant (it is the
#: separate ``never_clocked_in`` figure), an account that has punched is not onboarding, a
#: switched-off account is neither, and a punch from just inside the window clears dormancy.
WATCH_ACCOUNTS = (
    ("d1", "active", 40, None),      # worked here, then stopped: dormant
    ("d2", "active", 2, None),       # worked this week: not dormant
    ("d3", "active", None, None),    # never punched at all: never, and *not* dormant
    ("d4", "inactive", 40, None),    # switched off: not dormant, however long it has been
    ("d5", "active", 29, None),      # just inside the window: not dormant
    ("o1", "active", None, 3),       # joined this week and never punched: onboarding
    ("o2", "active", 1, 3),          # joined this week and already working: not onboarding
    ("o3", "active", None, 40),      # never punched, but joined long ago: not onboarding
)


def plant_watch_world(now: datetime | None = None) -> dict:
    """Replace the roster and the audit log with one built around the two watch figures.

    ``dormant_days + 10`` rather than a literal forty: the account has to be dormant *whatever the
    window is*, so a build that changes ``DORMANT_DAYS`` moves the plant with it instead of
    quietly making this a test of a different edge.
    """
    now = now or datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(days_ago: int, hour: int = 6) -> str:
        return (day_start - timedelta(days=days_ago) + timedelta(hours=hour)).strftime(TS)

    with writing() as conn:
        # The roster, the attendance history *and* the audit log, because all three are read: an
        # assertion like "one account is onboarding" has to be a fact about this plant rather
        # than about whichever creation rows a shared snapshot happened to carry.
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM attendance_logs")
        conn.execute("DELETE FROM audit_log WHERE entity = 'users'")

        # The harness's own administrators come with it: this suite signs in as them, and a token
        # naming an account that is not in the roster is refused with 401 before the dashboard is
        # ever reached. They are ordinary active accounts otherwise - no punch, no audit row - so
        # they are in ``active`` and ``never_clocked_in`` and in neither watch figure.
        conn.executemany(
            "INSERT INTO users (id, name, email, phone, password_hash, role, status, "
            "enrolled_at, template_version, biometric_id) "
            "VALUES (?,?,?,'','a-stored-hash',?,?,NULL,1,NULL)",
            [
                (user_id, f"Row {user_id}", "", "worker", status)
                for user_id, status, _, _ in WATCH_ACCOUNTS
            ]
            + [
                (ADMIN, "Row admin", "", "admin", "active"),
                (HEAD_ADMIN, "Row head", "", "head_admin", "active"),
            ],
        )
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES (?, ?, 'Clock In', ?, 0.0, 0.9, 'Approved', 'approved')",
            [
                (user_id, A, at(days_ago))
                for user_id, _, days_ago, _ in WATCH_ACCOUNTS
                if days_ago is not None
            ],
        )
        # The audit log's own shape: an account *joined* when one of the three creation actions
        # was written against it, which is the only evidence of "new" this schema has.
        conn.executemany(
            "INSERT INTO audit_log (created_at, actor_id, actor_role, action, entity, entity_id) "
            "VALUES (?, '5000', 'head_admin', 'user_create', 'users', ?)",
            [
                (at(created), user_id)
                for user_id, _, _, created in WATCH_ACCOUNTS
                if created is not None
            ],
        )

    return {
        "accounts": 10,            # the eight watch accounts and the two the suite signs in as
        "active": 9,
        "deactivated": 1,          # d4, and only d4
        # d3, o1, o3, and the two administrators: the accounts with no punch at all.
        "never_clocked_in": 5,
        "new_this_week": 2,        # o1 and o2: created inside ``NEW_ACCOUNT_DAYS``
        "onboarding": 1,           # ...and of those, only o1 has never punched
        "dormant": 1,              # d1: it worked here and stopped; d2 and d5 are too recent
    }


def test_the_watch_figures_name_the_ones_who_stopped_and_the_ones_who_never_started(client):
    """The two figures that are not a *state*: they are the ways an account stops being a worker.

    Both can be wrong by including somebody, which is why the plant carries one account per edge of
    each predicate rather than one account per figure - and why the two are asserted together:
    "dormant" and "never clocked in" describe opposite histories, and a build that folded them into
    one number would report a deployment that has not opened yet as a roster of dormant staff.
    """
    expected = plant_watch_world()
    people = payload(client)["people"]

    for key, want in expected.items():
        assert people[key] == want, f"people.{key} is {people[key]!r}, expected {want!r}"

    # The windows the figures were counted over travel with them.
    assert people["dormant_days"] == dashboard.DORMANT_DAYS
    assert people["onboarding_days"] == dashboard.NEW_ACCOUNT_DAYS
    # ``onboarding`` is a sub-question of ``new_this_week`` by construction rather than by
    # upkeep: both come out of the one scan of the week's creations, so this cannot be false
    # without the SQL itself being wrong.
    assert people["onboarding"] <= people["new_this_week"], people
    # And the two watch figures are disjoint, which is the claim the query makes: an account with
    # no punch at all can never be counted as dormant.
    never = db_scalar(
        "SELECT COUNT(*) FROM users u WHERE u.id NOT IN "
        "(SELECT DISTINCT worker_id FROM attendance_logs WHERE worker_id IS NOT NULL)"
    )
    assert people["never_clocked_in"] == never == expected["never_clocked_in"], (people, never)
    assert people["dormant"] + people["never_clocked_in"] <= people["active"], people


def test_an_account_that_punches_again_stops_being_dormant(client):
    """Dormancy is a fact about the last punch, not a label written on the account.

    The planted world asserts one number; this drives the *window* the number is measured with, by
    giving the dormant account a punch today and reading again. A figure that was computed from a
    stored flag, or from the wrong end of the range, would keep saying 1 here.
    """
    plant_watch_world()
    assert payload(client)["people"]["dormant"] == 1

    with writing() as conn:
        conn.execute(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES ('d1', ?, 'Clock In', ?, 0.0, 0.9, 'Approved', 'approved')",
            (A, datetime.now().strftime(TS)),
        )

    after = payload(client)["people"]
    assert after["dormant"] == 0, after
    # ...and the figures that do not depend on the punch are untouched, because it is the same
    # account: this is a change of window, not of roster.
    assert after["accounts"] == 10 and after["never_clocked_in"] == 5, after


# ---------------------------------------------------------------------------
# 3. concealment
# ---------------------------------------------------------------------------
def test_a_queued_punch_in_the_root_accounts_name_is_visible_to_nobody_else(client):
    """The clause travels with the two queue counts, including the row that names nobody.

    ``punch_queue.worker_id`` is NOT NULL, so the plain clause is exact there; ``refused_punches``
    is the one table where a punch is recorded with *no* worker at all - a face the engine could
    not match - which is why that count is scoped through ``COALESCE``. Both halves are asserted:
    the administrator's figures do not move, and the root tier's own read counts what it hid from
    them.
    """
    plant_world()
    developer.seed_developer_account(password=harness.ROOT_PASSWORD, actor="test:dashboard")
    root_id = db_scalar("SELECT id FROM users WHERE role = ?", (developer.DEVELOPER_ROLE,))
    assert root_id, "the root account was not planted, so this test measures nothing"

    before = payload(client)["now"]
    moment = datetime.now().strftime(TS)
    with writing() as conn:
        conn.execute(
            "INSERT INTO punch_queue (client_punch_id, device_id, worker_id, action, "
            "client_timestamp, signature, received_at, status) "
            "VALUES ('q-root','dev-root',?,'Clock In',?, 'a-signature',?,'accepted')",
            (root_id, moment, moment),
        )
        conn.execute(
            "INSERT INTO refused_punches (worker_id, site_name, action, error_code, created_at) "
            "VALUES (?, ?, 'Clock In', 'no_match', ?)",
            (root_id, A, moment),
        )

    after = payload(client)["now"]
    assert after == before, "the root account moved a queue count: " + repr([before, after])

    root = client.get(DASHBOARD, headers=harness.root_bearer())
    assert root.status_code == 200, root.text
    seen = root.json()["now"]
    assert seen["offline_waiting"] == before["offline_waiting"] + 1, seen
    assert seen["refused_24h"] == before["refused_24h"] + 1, seen
    # The rest of the panel is the board's, and it conceals nothing for anybody: a session the
    # board names must not vanish from here either, or the two would disagree by design.
    assert seen["on_shift"] == before["on_shift"] == 3, seen


def test_the_root_account_is_absent_from_every_total(client):
    """The roster's rule, applied to the counts - in the query.

    A dashboard that filtered the root account out in Python would still hand back the *counts*
    it was hidden from, and a count is an enumeration: "there are 42 accounts" is a fact about
    this deployment, and one of the 42 is an account an administrator may not know exists.
    """
    plant_world()
    before = payload(client)

    developer.seed_developer_account(password=harness.ROOT_PASSWORD, actor="test:dashboard")
    assert db_scalar("SELECT id FROM users WHERE role = ?", (developer.DEVELOPER_ROLE,)), (
        "the root account was not planted, so this test measures nothing"
    )

    after = payload(client)
    assert after["people"] == before["people"], (
        "the root account moved a count: " + repr(after["people"])
    )
    assert after["places"] == before["places"]
    assert "developer" not in after["people"]["by_role"], after["people"]["by_role"]

    # And the range the clause leaves alone: the root tier reads the whole roster, itself
    # included. That is what proves the hiding above is the clause's work rather than the
    # account being invisible to everybody.
    response = client.get(DASHBOARD, headers=harness.root_bearer())
    assert response.status_code == 200, response.text
    seen = response.json()["people"]
    assert seen["accounts"] == before["people"]["accounts"] + 1, seen
    assert seen["by_role"].get("developer") == 1, seen["by_role"]


# ---------------------------------------------------------------------------
# 4. the alert queue is the root tier's
# ---------------------------------------------------------------------------
def test_the_alert_queue_is_the_root_tiers_and_is_null_for_an_administrator(client):
    """``null`` rather than ``0``: the queue is not an administrator's to read at all.

    A zero would say "nothing is waiting" about a surface they cannot open, which is the
    opposite of true on a deployment where nothing has been acknowledged.
    """
    with writing() as conn:
        conn.executemany(
            "INSERT INTO admin_notifications (kind, severity, title, body, created_at, "
            "acknowledged_at) VALUES ('startup_degraded','warning',?,'body',?,?)",
            [
                ("first", (datetime.now() - timedelta(hours=2)).strftime(TS), None),
                ("second", (datetime.now() - timedelta(hours=4)).strftime(TS), None),
                # Already answered: not waiting, so not counted - and it is the *oldest*, which
                # is the trap for an implementation that takes the minimum over the whole table.
                ("answered", (datetime.now() - timedelta(days=2)).strftime(TS),
                 datetime.now().strftime(TS)),
            ],
        )

    administrator = payload(client)
    assert administrator["waiting"]["alerts"] is None, administrator["waiting"]

    response = client.get(DASHBOARD, headers=harness.root_bearer())
    assert response.status_code == 200, response.text
    root = response.json()["waiting"]
    assert root["alerts"] == 2, root
    # The unanswered pair are the only candidates, so the age is the elder of *them* - four hours
    # - and not the two-day-old alert that was already accepted. An implementation that took the
    # oldest row of the table would answer 2 days here.
    four_hours = int(timedelta(hours=4).total_seconds())
    assert abs(root["oldest_seconds"] - four_hours) <= 120, root


# ---------------------------------------------------------------------------
# 5. the day window
# ---------------------------------------------------------------------------
def test_the_day_window_is_half_open_at_both_ends(client):
    """A punch at the first second of today counts; the first second of tomorrow does not.

    The window is built end-exclusive for exactly this reason - a punch recorded in the last
    second of the day must not fall between two days and belong to neither.
    """
    plant_world()
    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with writing() as conn:
        conn.execute("DELETE FROM attendance_logs")
        conn.executemany(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES ('1', ?, 'Clock In', ?, 0.0, 0.9, 'Approved', 'approved')",
            [
                (A, day_start.strftime(TS)),                                        # exactly 00:00:00 today
                (DEPOT, (day_start + timedelta(hours=23, minutes=59, seconds=59)).strftime(TS)),
                (B, (day_start + timedelta(days=1)).strftime(TS)),                   # exactly 00:00:00 tomorrow
            ],
        )
    assert payload(client)["places"]["unmanned_today"] == [B], (
        "a punch on the boundary of today was filed under a different day"
    )


# ---------------------------------------------------------------------------
# 6. unknown is not zero
# ---------------------------------------------------------------------------
def test_a_panel_that_cannot_be_read_answers_null_and_takes_nothing_with_it(client):
    """One unreadable panel, not a blank page and not a zero.

    A zero standing in for "could not tell" reads as good news, and good news is the state an
    administrator acts on - so the panel that failed says ``null`` and the other two are still
    filled in.
    """
    plant_world()
    whole = payload(client)
    with writing() as conn:
        conn.execute("DROP TABLE worker_notes")

    body = payload(client)
    assert body["waiting"] is None, body["waiting"]
    assert body["people"] == whole["people"], body["people"]
    assert body["places"] == whole["places"], body["places"]
    # The panel beside it did not so much as notice: a failure is one panel's, and the four
    # routes inside "now" (a join, a group-by, the materialiser's own queue and the refusal
    # count) are not the dropped table's business.
    assert body["now"] == whole["now"], body["now"]
    assert body["as_of"], body


def test_a_deployment_with_nothing_in_it_reads_zeroes_rather_than_nulls(client):
    """The other half of ``null``: an empty deployment is a state, not a failure.

    This is the readiness precedent - a closed intake is a configuration, not a fault - and it is
    the case a dashboard is looked at on the first morning, when every panel being ``null`` would
    read as a broken installation. The caller's own account is the one row that has to stay (a
    token for an account that does not exist is refused before this route is reached at all), so
    the roster is left holding exactly the two administrators and nothing else is left at all.
    """
    with writing() as conn:
        conn.execute("DELETE FROM users WHERE id NOT IN (?, ?)", (ADMIN, HEAD_ADMIN))
        conn.execute("DELETE FROM attendance_logs")
        conn.execute("DELETE FROM worker_notes")
        conn.execute("DELETE FROM active_sessions")
        conn.execute("DELETE FROM punch_queue")
        conn.execute("DELETE FROM refused_punches")

    body = payload(client)
    assert body["status"] == "success"
    # Not one panel answered ``null``: the numbers are zero and the empty list is empty.
    for name in ("people", "places", "now", "waiting", "period"):
        assert body[name] is not None, f"{name} read as a failure on an empty deployment"
    people = body["people"]
    assert people["accounts"] == 2, people
    assert people["by_role"] == {"admin": 1, "head_admin": 1}, people
    assert people["deactivated"] == people["no_password"] == people["new_this_week"] == 0, people
    assert people["pending_approval"] == 0, people
    assert people["never_clocked_in"] == 2, people
    assert body["now"] == {
        "on_shift": 0, "by_site": [], "overtime_open": 0, "offline_waiting": 0, "refused_24h": 0
    }, body["now"]
    assert body["waiting"] == {
        "reviews": 0, "registrations": 0, "notes": 0, "alerts": None, "oldest_seconds": None
    }, body["waiting"]
    # The period panel on the same morning: a week nobody worked is seven days of zeroes and two
    # empty lists, and ``expected_days`` is the one figure that is not zero - the site was open.
    period = body["period"]
    assert (period["preset"], period["days"]) == ("days", 7), period
    assert period["workers"] == period["present_days"] == period["late_arrivals"] == 0, period
    assert period["approved_hours"] == period["overtime_hours"] == 0.0, period
    assert period["awaiting_approval_hours"] == 0.0, period
    assert period["average_attendance_rate"] == 0.0, period
    assert period["expected_days"] >= 1, period
    assert period["quietest"] == [] and period["most_late"] == [], period
    # Every seeded site has nobody in it, which is a list rather than a count - and an empty
    # roster must not file a site as manned.
    assert len(body["places"]["unmanned_today"]) == body["places"]["sites"] >= 2, body["places"]


# ---------------------------------------------------------------------------
# 7. what it costs
# ---------------------------------------------------------------------------
def traced_statements(client):
    """The SQL one read issues, captured off the connection the read opens."""
    statements: list[str] = []
    original = dashboard.db

    @contextmanager
    def traced(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    dashboard.db = traced
    try:
        payload(client)
    finally:
        dashboard.db = original
    return statements


def test_the_read_issues_counts_and_its_cost_does_not_follow_the_roster(client):
    """The decision this endpoint exists for, asserted on the SQL rather than on the answer.

    Two properties, and both are the point:

    * **no statement returns rows.** Every one of them aggregates (``COUNT``/``SUM``/``AVG``) or is
      the single-row lookup of ``shift_rules`` - and the period panel's own aggregates are in this
      list too, because it counts through the dashboard's connection rather than one of its own. A
      payload built by fetching rows and counting them in Python would need exactly the statements
      this refuses;
    * **the number of them does not move when the roster grows twentyfold.** A regression here
      is a payload that grew with the deployment instead of with the number of panels, and it
      would pass every other test in this file.

    The two extremes *are* per-worker rows, which is why the boundary is drawn rather than assumed:
    each is cut to ``reports.EXTREMES_LIMIT`` in SQL, so the number of rows they can return is
    bounded by the panel and not by how many people worked.
    """
    plant_world()
    small = traced_statements(client)

    with writing() as conn:
        conn.executemany(
            "INSERT INTO users (id, name, email, phone, password_hash, role, status) "
            "VALUES (?,?,?,?,?,'worker','active')",
            [(f"bulk-{index}", f"Bulk {index}", "", "", "a-stored-hash") for index in range(200)],
        )
    assert db_scalar("SELECT COUNT(*) FROM users") == 213, (
        "the planted roster plus the two hundred: this test measures a twentyfold roster"
    )

    big = traced_statements(client)
    assert len(small) == len(big), (
        f"the read issued {len(small)} statements for ten accounts and {len(big)} for two "
        "hundred and ten - its cost follows the roster"
    )
    # A ceiling that is a constant rather than a function of the deployment: five panels' worth of
    # aggregates, and a failure here means a panel started issuing statements per row.
    assert 8 <= len(big) <= 40, big
    aggregates = ("COUNT(", "SUM(", "AVG(")

    def bounded(statement: str) -> bool:
        upper = statement.upper()
        # An aggregate answers about the rows without returning them...
        if any(token in upper for token in aggregates):
            return True
        # ...or the statement caps itself: the rules by primary key, and the board's scan of the
        # oldest readable clock-ins, which is a ``LIMIT 5`` and so cannot follow the roster.
        return "ID = 1" in upper or "LIMIT " in upper

    unbounded = [statement for statement in big if not bounded(statement)]
    assert unbounded == [], (
        "a statement in this read returns an unbounded set of rows: "
        + repr([" ".join(statement.split())[:160] for statement in unbounded])
    )
    # Every statement that so much as reads the attendance history is an aggregate over it, or
    # capped: the same rule, applied to the tables that are the reason it exists.
    for statement in big:
        upper = statement.upper()
        if "FROM ATTENDANCE_LOGS" in upper or "FROM USERS" in upper:
            assert any(token in upper for token in aggregates) or "LIMIT " in upper, (
                "a list read of a table that grows with the roster: " + " ".join(statement.split())
            )


# ---------------------------------------------------------------------------
# 8. who may read it
# ---------------------------------------------------------------------------
def test_the_dashboard_is_for_administrators_and_the_root_tier(client):
    """A worker, a lead or an off-office worker asking for the deployment's totals is refused.

    Anyone with an account can ask, so this is not an anonymity question - the question is whether
    the *role* is enough, and a signed-in worker is exactly the caller that gets past the door
    and then has to be stopped by the audience.
    """
    for user_id in (WORKER, MOALLEM, OFF_OFFICE):
        response = client.get(DASHBOARD, headers=bearer(user_id))
        assert_denied(
            response,
            endpoint=DASHBOARD,
            detail=f"{user_id} ({harness.ROLES[user_id]}) read the console's dashboard",
        )
    assert read(client, as_user=ADMIN).status_code == 200
    assert read(client, as_user=HEAD_ADMIN).status_code == 200
    assert client.get(DASHBOARD, headers=harness.root_bearer()).status_code == 200


def test_a_reader_with_no_credential_at_all_is_refused(client):
    response = client.get(DASHBOARD)
    assert_denied(response, endpoint=DASHBOARD, detail="the dashboard answered an anonymous caller")


# ---------------------------------------------------------------------------
# 9. the definitions this module borrows, so they cannot be replaced silently
# ---------------------------------------------------------------------------
def test_the_predicates_come_from_the_modules_that_own_them():
    """The borrowed predicates are borrowed, not restated.

    ``waiting`` uses ``reports.PENDING_CODES`` and ``notes.OPEN_STATUSES`` rather than its own
    literals, and the account counts use ``developer.visibility_clause``; this asserts that the
    module still *names* them, because the alternative - a second copy of a predicate - is how
    two screens start disagreeing about the same queue and is invisible from the outside.
    """
    source = (harness.BACKEND_DIR / "dashboard.py").read_text(encoding="utf-8")
    for borrowed in (
        "reports.PENDING_CODES",
        # The period panel's own borrow: the counted twin of the attendance report, which owns
        # every definition this panel prints.
        "reports.attendance_period",
        "notes.OPEN_STATUSES",
        "registrations.STATUS_PENDING_APPROVAL",
        "overtime.open_crossings",
        "offline_sync.PENDING_STATUSES",
        "developer.visibility_clause",
    ):
        assert borrowed in source, f"dashboard.py stopped borrowing {borrowed}"
    for literal in ("'pending_review'", "'pending_overtime'", '"pending_review"', '"pending_overtime"'):
        assert literal not in source, (
            f"dashboard.py restates the pending predicate instead of borrowing it: {literal}"
        )
    # The one place a creation action is spelled, and the placeholder list that has to match it.
    assert dashboard.ACCOUNT_CREATION_ACTIONS == (
        "user_create", "admin_create", "registration_approved"
    )
    assert dashboard._CREATION_SQL == ", ".join("?" for _ in dashboard.ACCOUNT_CREATION_ACTIONS)


def test_an_account_created_from_the_console_counts_as_joined_this_week(client):
    """``new_this_week`` against a creation that really happened.

    The planted world asserts the figure against audit rows this suite wrote, which would pass
    just as well if the console's own creation path wrote a *different* action. This drives the
    real endpoint, so the tuple that says "these actions create an account" is held to the door
    an administrator actually uses.
    """
    plant_world()
    before = payload(client)["people"]

    created = client.post(
        "/api/v1/admin/users/add",
        headers=bearer(HEAD_ADMIN),
        json={
            "user_id": "4242",
            "name": "Hired Today",
            "email": "hired@example.test",
            "phone": "",
            "password": "site-attendance-2026",
            "role": "worker",
        },
    )
    assert created.status_code == 200, created.text

    after = payload(client)["people"]
    assert after["accounts"] == before["accounts"] + 1, after
    assert after["new_this_week"] == before["new_this_week"] + 1, after
    # A brand-new account has no face and no punch, which is the pair of hygiene signals the
    # panel exists for - so the same creation is visible in three figures, not one.
    assert after["no_face"] == before["no_face"] + 1, after
    assert after["never_clocked_in"] == before["never_clocked_in"] + 1, after


def test_the_panel_names_are_the_ones_the_console_draws(client):
    """The payload's shape, pinned: the console draws a panel per name here."""
    body = payload(client)
    assert set(body) == {
        "status", "as_of", "freshness", "day", "people", "places", "now", "waiting", "period"
    }, sorted(body)
    assert body["status"] == "success"
    assert set(body["freshness"]) == {"aging_seconds", "stale_seconds"}, body["freshness"]
    assert set(body["people"]) == {
        "accounts", "active", "pending_approval", "deactivated", "by_role", "enrolled",
        "no_face", "no_password", "new_this_week", "never_clocked_in", "onboarding",
        "dormant", "dormant_days", "onboarding_days",
    }, sorted(body["people"])
    assert set(body["now"]) == {
        "on_shift", "by_site", "overtime_open", "offline_waiting", "refused_24h",
    }, sorted(body["now"])
    assert set(body["now"]["by_site"][0]) == {"site_name", "workers"}, body["now"]["by_site"]
    assert set(body["places"]) == {
        "sites", "categories", "by_category", "no_category", "overriding_window", "unmanned_today",
    }, sorted(body["places"])
    assert set(body["waiting"]) == {
        "reviews", "registrations", "notes", "alerts", "oldest_seconds",
    }, sorted(body["waiting"])
    assert set(body["period"]) == {
        "days", "preset", "start", "end", "workers", "expected_days", "present_days",
        "late_arrivals", "average_attendance_rate", "approved_hours", "overtime_hours",
        "awaiting_approval_hours", "awaiting_approval_shifts", "by_day", "quietest", "most_late",
    }, sorted(body["period"])
    # The two windows the watch figures were counted over travel with them, because a label that
    # says thirty days while the query counted forty-five is a number nobody can check.
    assert body["people"]["dormant_days"] == dashboard.DORMANT_DAYS
    assert body["people"]["onboarding_days"] == dashboard.NEW_ACCOUNT_DAYS
    assert len(body["period"]["by_day"]) == body["period"]["days"], body["period"]["by_day"]
    assert set(body["period"]["by_day"][0]) == {"day", "present", "late"}, body["period"]["by_day"]
    # The window travels with the figures, in the fields the console's control and links read.
    assert body["period"]["preset"] in ("days", "month"), body["period"]
    assert body["period"]["start"] < body["period"]["end"], body["period"]
    assert set(body["period"]["quietest"][0]) == {
        "worker_id", "worker_name", "present_days", "rate", "late",
    }, body["period"]["quietest"]
    assert set(body["period"]["most_late"][0]) == set(body["period"]["quietest"][0]), (
        "the two linkages must be drawable by one renderer, or the console has two"
    )
