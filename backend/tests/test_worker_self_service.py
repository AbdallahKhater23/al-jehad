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
"""

from __future__ import annotations

import pytest
from harness import MOALLEM, WORKER, bearer, clock_in

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
