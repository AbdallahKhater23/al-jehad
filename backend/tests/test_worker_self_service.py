"""A worker must be able to see their own shift, hours and timesheet.

THE BUG THIS GUARDS
-------------------
The security remediation moved the worker portal's own data behind admin-only
routes: ``/admin/active_sessions`` and ``/admin/logs`` require an admin role, and
the id-addressed ``/worker/stats/{worker_id}`` became admin-only as well
(an id-addressed read any peer could call is an object-reference problem). The
frontend kept calling all three behind ``.catch(() => ...)``, so a worker got:

* no visible shift, so the clock button always offered "Clock In" - and once a
  shift was actually open, every tap answered ``400 Already clocked in!``;
* hours-this-month pinned at 0;
* a History tab that failed on their own timesheet.

``/worker/me/stats`` and ``/worker/me/logs`` are the self-scoped half of the
contract: the subject is the token, never a request field, so there is no id to
tamper with and no role to escalate.

``/worker/me/report`` finished the sentence for the timesheet, and
``/worker/me/report/columns`` is its last inch: *which* columns this account's own CSV and
PDF carry. The preference is stored on the account rather than in the browser, because the
file is handed in from wherever the worker is standing - so the tests below pin the two
things that could quietly break that: that the choice is the account's own and nobody
else's, and that what comes back is what the server will really draw rather than what was
posted.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
import reports
from harness import ADMIN, MOALLEM, WORKER, bearer, clock_in

import harness

ADMIN_ONLY_PATHS = (
    "/api/v1/admin/active_sessions",
    "/api/v1/admin/logs",
    f"/api/v1/worker/stats/{WORKER}",
)


@pytest.mark.parametrize("path", ADMIN_ONLY_PATHS)
def test_a_worker_cannot_read_the_admin_views(client, path):
    """The reason the worker portal needed its own endpoints in the first place."""
    assert client.get(path, headers=bearer(WORKER)).status_code == 403
    assert client.get(path).status_code == 401


def test_the_self_scoped_views_need_a_token(client):
    for path in ("/api/v1/worker/me/stats", "/api/v1/worker/me/logs"):
        assert client.get(path).status_code == 401, path


def test_my_stats_reports_my_own_open_shift(client):
    """The clock button reads its state from here, so this is the field that matters."""
    before = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM))
    assert before.status_code == 200, before.text[:200]
    assert before.json()["active_session"] is None, "this worker has no open shift yet"

    started = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert started.status_code == 200, started.text[:300]

    after = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM)).json()
    session = after["active_session"]
    assert session is not None, "an open shift must be visible to the worker it belongs to"
    assert session["clock_in_time"], "the panel shows when the shift started"
    assert session["site_name"], "the panel shows where the shift started"
    assert "late_flag" in session

    # ... and not to anybody else: each worker reads their own row.
    other = client.get("/api/v1/worker/me/stats", headers=bearer(WORKER)).json()
    assert other["active_session"] != session


def test_my_stats_counts_only_my_own_hours(client):
    mine = client.get("/api/v1/worker/me/stats", headers=bearer(WORKER)).json()
    theirs = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM)).json()
    assert mine["worker_id"] == WORKER and theirs["worker_id"] == MOALLEM
    assert mine["total_hours"] != theirs["total_hours"] or mine["total_hours"] == 0


def test_my_logs_returns_only_my_own_rows(client):
    response = client.get("/api/v1/worker/me/logs?limit=20", headers=bearer(WORKER))
    assert response.status_code == 200, response.text[:200]
    rows = response.json()
    assert rows, "the seeded history belongs to this worker"
    assert {str(row["worker_id"]) for row in rows} == {WORKER}, (
        "a worker's timesheet must not carry a colleague's rows"
    )
    assert set(rows[0]) >= {"site", "action", "timestamp", "hours", "status"}
    assert len(rows) <= 20, "the limit must be honoured"

    # A worker with their own history gets their own rows, not this worker's.
    other = client.get("/api/v1/worker/me/logs", headers=bearer(MOALLEM)).json()
    assert {str(row["worker_id"]) for row in other} <= {MOALLEM}


def test_a_clock_in_appears_in_my_own_timesheet(client):
    started = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert started.status_code == 200, started.text[:300]
    rows = client.get("/api/v1/worker/me/logs", headers=bearer(MOALLEM)).json()
    assert any(row["action"] == "Clock In" for row in rows)


def test_a_shift_awaiting_review_is_announced_before_the_worker_tries_to_leave(client, jpeg):
    """A blocked clock-out with no warning is a dead end for the person on site.

    ``/attendance/verify`` refuses a clock-out while any of the worker's rows is still
    ``pending_review`` - deliberately, so an admin clears it - but the worker only found
    out after walking off site and taking a selfie, with no hint of what to do about it.
    The flag now travels with the worker's own stats, so the panel can say so up front.
    """
    # The harness seeds one pending_review row for WORKER, and none for MOALLEM.
    flagged = client.get("/api/v1/worker/me/stats", headers=bearer(WORKER)).json()
    clear = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM)).json()
    assert flagged["flagged_for_review"] is True
    assert clear["flagged_for_review"] is False

    # ... and the warning is about something real: the clock-out is refused.
    clock_in(client, WORKER, headers=bearer(WORKER), image=jpeg)
    refused = client.post(
        "/api/v1/attendance/verify",
        headers=bearer(WORKER),
        data={
            "worker_id": WORKER,
            "action": "Clock Out",
            "location_input": "30.05,31.23",
        },
        files={"selfie": ("selfie.jpg", jpeg, "image/jpeg")},
    )
    assert refused.status_code == 403, refused.text[:300]
    assert "manual review" in refused.text

    # A colleague with no pending row is not blocked by it.
    assert client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM)).json()[
        "flagged_for_review"
    ] is False


# ---------------------------------------------------------------------------
# my own timesheet, in a period, and where the hours were worked
# ---------------------------------------------------------------------------
def test_my_report_needs_a_token(client):
    assert client.get("/api/v1/worker/me/report").status_code == 401


def test_my_report_is_scoped_to_the_token(client):
    """The self-scoped twin of the shifts report carries nobody else's rows.

    ``/admin/reports/shifts`` is the whole company's timesheet. This is the same rows for
    the account that asked, and the subject is the token - so the one thing that must never
    happen is a colleague's shift appearing under it.
    """
    response = client.get(
        "/api/v1/worker/me/report?start=2026-03-01&end=2026-03-31", headers=bearer(WORKER)
    )
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["worker_id"] == WORKER
    assert body["rows"], "the seeded March history belongs to this account"
    assert {str(row["worker_id"]) for row in body["rows"]} == {WORKER}

    # The colleague's own report is their own, and it is empty for this period.
    other = client.get(
        "/api/v1/worker/me/report?start=2026-03-01&end=2026-03-31", headers=bearer(MOALLEM)
    ).json()
    assert other["worker_id"] == MOALLEM
    assert other["rows"] == [], "only one account has seeded history"


def test_my_report_splits_approved_from_awaiting(client):
    """The gate the whole overtime feature rests on: unapproved hours are not approved hours."""
    body = client.get(
        "/api/v1/worker/me/report?start=2026-03-01&end=2026-03-31", headers=bearer(WORKER)
    ).json()
    totals = body["totals"]
    assert totals["hours"] == pytest.approx(totals["approved_hours"] + totals["awaiting_approval_hours"])
    assert totals["awaiting_approval_hours"] >= 0
    # A shift still waiting on somebody is a row in the sheet, never a hidden one.
    assert totals["shifts"] == len(body["rows"])


def test_my_report_buckets_the_hours_by_site(client):
    """"How much did I work *where*" is the half a plain total cannot answer."""
    body = client.get(
        "/api/v1/worker/me/report?start=2026-03-01&end=2026-03-31", headers=bearer(WORKER)
    ).json()
    by_site = body["by_site"]
    assert by_site, "the seeded shifts were worked at a site"
    assert sum(site["hours"] for site in by_site) == pytest.approx(body["totals"]["hours"])
    assert sum(site["shifts"] for site in by_site) == body["totals"]["shifts"]
    assert body["totals"]["sites"] == len(by_site)
    for site in by_site:
        assert site["site_name"]
        assert site["hours"] == pytest.approx(
            site["approved_hours"] + site["awaiting_approval_hours"]
        )
    # Busiest first, so "where did my month go" is answered by the first line.
    assert by_site == sorted(by_site, key=lambda site: (-site["hours"], site["site_name"]))


def test_my_report_defaults_to_this_month(client):
    """An unasked-for period is the month in progress, not the whole history."""
    body = client.get("/api/v1/worker/me/report", headers=bearer(WORKER)).json()
    today = datetime.now().date()
    assert body["period"] == {"start": str(today.replace(day=1)), "end": str(today)}
    # The seeded history is March 2026, which is not this month by construction.
    assert body["rows"] == []


@pytest.mark.parametrize(
    "query",
    (
        "start=2026-03-31&end=2026-03-01",  # end before start
        "start=31-03-2026&end=2026-03-31",  # not a date this API accepts
        "start=2000-01-01&end=2026-03-31",  # wider than the three-year ceiling
    ),
)
def test_my_report_refuses_a_period_it_cannot_honour(client, query):
    """A report that silently answered a different period would be worse than an error."""
    response = client.get(f"/api/v1/worker/me/report?{query}", headers=bearer(WORKER))
    assert response.status_code == 400, response.text[:200]


# ---------------------------------------------------------------------------
# which columns my own files carry, remembered on my account
# ---------------------------------------------------------------------------
COLUMNS_PATH = "/api/v1/worker/me/report/columns"


def _stored(user_id: str = WORKER):
    """The preference on the account, read from the database rather than through the API."""
    return harness.db_scalar("SELECT report_columns FROM users WHERE id = ?", (user_id,))


def _store_by_hand(user_id: str, value) -> None:
    """Put a value on the column the way an operator, a restore or an old release would.

    The endpoint cannot produce junk, so the only honest way to test that junk is *read*
    safely is to write it where an endpoint is not looking - which is also why the reader
    normalises rather than trusting the column.
    """
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("UPDATE users SET report_columns = ? WHERE id = ?", (value, user_id))
        conn.commit()
    finally:
        conn.close()


def test_my_report_says_which_columns_my_files_will_carry(client):
    """The shape of the download travels *with* the rows it is built from.

    The two files are built on the client from this payload, so a column list fetched
    separately could arrive after the rows it describes - and a report that named its
    columns somewhere else would be one more request that can fail on its own.
    """
    body = client.get("/api/v1/worker/me/report", headers=bearer(WORKER)).json()
    assert body["columns"] == list(reports.DEFAULT_REPORT_COLUMNS)
    # The default is the file this screen has always written, not the whole vocabulary:
    # an account that has never chosen must not gain a column because one exists.
    assert body["columns"] == [
        column for column in reports.REPORT_COLUMNS if column != "recorded"
    ]
    assert "recorded" not in body["columns"]


def test_a_worker_narrows_the_columns_in_their_own_files(client):
    """The point of the whole thing: a worker keeps a file with the columns they read."""
    chosen = ["date", "hours", "recorded"]
    saved = client.post(COLUMNS_PATH, json={"columns": chosen}, headers=bearer(WORKER))
    assert saved.status_code == 200, saved.text[:300]
    assert saved.json()["columns"] == chosen
    assert _stored() == "date,hours,recorded", "stored as text, in the vocabulary's order"

    # And the next month's report carries it - on any device, because it is on the account.
    body = client.get("/api/v1/worker/me/report", headers=bearer(WORKER)).json()
    assert body["columns"] == chosen


def test_the_choice_is_written_in_the_vocabularys_order_however_it_was_ticked(client):
    """Two files of the same month line up column for column, whatever order was posted.

    The order is the server's, not the client's: a worker who ticks Status first gets a
    file whose last column is still Status, so it can be compared with the one they made
    last month from the same account on a different phone.
    """
    response = client.post(
        COLUMNS_PATH,
        json={"columns": ["status", "recorded", "DATE", " hours ", "date", "hours"]},
        headers=bearer(WORKER),
    )
    assert response.status_code == 200, response.text[:300]
    # Canonical order, each column once, and case and padding are not a different column.
    assert response.json()["columns"] == ["date", "hours", "recorded", "status"]


def test_a_column_this_build_cannot_fill_is_dropped_rather_than_refused(client):
    """A client one release ahead must still be able to save.

    Refusing the unknown id would make a newer app unable to save at all against this
    server; keeping it would put a column of empty cells in somebody's evidence. So it is
    dropped - and the reply says so, which is how a client learns what it will really get.
    """
    response = client.post(
        COLUMNS_PATH,
        json={"columns": ["date", "notes", "employee", "hours"]},
        headers=bearer(WORKER),
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["columns"] == ["date", "hours"]
    assert _stored() == "date,hours"


def test_a_choice_with_nothing_left_in_it_is_refused(client):
    """A file with no columns is not a report, so it is not a choice either."""
    assert client.post(COLUMNS_PATH, json={"columns": []}, headers=bearer(WORKER)).status_code == 400
    refused = client.post(
        COLUMNS_PATH, json={"columns": ["banana"]}, headers=bearer(WORKER)
    )
    assert refused.status_code == 400, refused.text[:200]
    assert refused.json()["detail"] == "A timesheet needs at least one column."

    # Refused, and nothing was written: the account keeps the file it had.
    assert _stored() is None
    body = client.get("/api/v1/worker/me/report", headers=bearer(WORKER)).json()
    assert body["columns"] == list(reports.DEFAULT_REPORT_COLUMNS)


def test_choosing_exactly_the_default_stores_nothing(client):
    """No narrowing and never choosing are the same file - today and next release.

    Storing the default as *nothing* is what lets the account that has narrowed nothing
    gain a column the release that adds one, while the account that deliberately dropped
    Status keeps a file without it. The other way round would freeze a worker's file at the
    shape it had on the day they first pressed Save.
    """
    response = client.post(
        COLUMNS_PATH, json={"columns": list(reports.DEFAULT_REPORT_COLUMNS)}, headers=bearer(WORKER)
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["columns"] == list(reports.DEFAULT_REPORT_COLUMNS)
    assert _stored() is None, "the default is stored as nothing, not as an explicit list"


def test_the_preference_is_the_accounts_and_nobody_elses(client):
    """Two people on one site office desktop must not share one file shape.

    The subject is the token, so there is no id in this request that could write onto
    somebody else's account - which is exactly the property an id-addressed preference
    would lose.
    """
    client.post(COLUMNS_PATH, json={"columns": ["date"]}, headers=bearer(WORKER))
    assert _stored(WORKER) == "date"
    assert _stored(MOALLEM) is None, "a colleague's report is untouched by my choice"
    assert client.get("/api/v1/worker/me/report", headers=bearer(MOALLEM)).json()[
        "columns"
    ] == list(reports.DEFAULT_REPORT_COLUMNS)

    # And their own choice is their own, in the other direction.
    client.post(COLUMNS_PATH, json={"columns": ["site", "status"]}, headers=bearer(MOALLEM))
    assert _stored(MOALLEM) == "site,status"
    assert _stored(WORKER) == "date"


def test_junk_on_the_column_reads_as_the_default(client):
    """The reader normalises, so nothing an operator or a restore leaves can reach a file.

    A malformed list is not a reason to hand somebody an empty download: it degrades to the
    file they have always had, which is the answer that cannot hide a shift.
    """
    for junk in ("nonsense", "date,,,", "DATE , hours", "date,hours,banana", "", None):
        _store_by_hand(WORKER, junk)
        body = client.get("/api/v1/worker/me/report", headers=bearer(WORKER)).json()
        expected = reports.report_columns(junk)
        assert body["columns"] == list(expected), (junk, body["columns"])
        assert body["columns"], f"{junk!r} must not leave an account with no columns"


def test_setting_the_columns_needs_a_token(client):
    assert client.post(COLUMNS_PATH, json={"columns": ["date"]}).status_code == 401


# ---------------------------------------------------------------------------
# an administrator who works a site as well
# ---------------------------------------------------------------------------
def test_an_admin_can_clock_in_like_anybody_else(client):
    """An administrator on the rota is a worker at the gate.

    ``/attendance/verify`` has always taken any authenticated role and has always refused
    a subject that is not the token's owner, so what was missing was never permission - it
    was that the console never offered the screen. This asserts the half the frontend cannot:
    that the punch itself is accepted, judged and recorded for an admin account.
    """
    started = clock_in(client, ADMIN, headers=bearer(ADMIN))
    assert started.status_code == 200, started.text[:300]

    session = client.get("/api/v1/worker/me/stats", headers=bearer(ADMIN)).json()
    assert session["active_session"] is not None, "the admin's own open shift is their own"
    assert session["active_session"]["site_name"]

    # And it lands in their own record, not only in the company's.
    logs = client.get("/api/v1/worker/me/logs", headers=bearer(ADMIN)).json()
    assert any(row["action"] == "Clock In" for row in logs)


def test_an_admin_reads_their_own_hours_not_everybody_elses(client):
    """The admin's own report stays theirs even though they may read the company's."""
    clock_in(client, ADMIN, headers=bearer(ADMIN))
    body = client.get("/api/v1/worker/me/report", headers=bearer(ADMIN)).json()
    assert body["worker_id"] == ADMIN
    assert {str(row["worker_id"]) for row in body["rows"]} <= {ADMIN}
    # WORKER's seeded March history is not reachable through this endpoint even for an
    # admin, because there is no id parameter to point at somebody else.
    other = client.get(
        "/api/v1/worker/me/report?start=2026-03-01&end=2026-03-31", headers=bearer(ADMIN)
    ).json()
    assert other["worker_id"] == ADMIN
    assert other["rows"] == []
