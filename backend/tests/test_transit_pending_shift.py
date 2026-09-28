"""The off-site transit worker: pending on the road, confirmed on arrival, closed by an admin.

WHY THIS SUITE EXISTS
---------------------
A shift that opens away from every site is the one shift whose *end* cannot be witnessed. The
feature that allows it defers exactly one thing - location at the moment the shift opens - and
these three states are what the worker and their administrator see while it is in that limbo:

1. **A check-in off-site is pending, and it counts.** It is an open shift: the worker is on the
   clock, the console lists them, the panel shows them on the road - and nothing is payable
   yet, because no site has authorised the day. It is deliberately *not* a ledger row: a
   zero-hour "Clock In" would put the time somebody is driving into the payroll ledger before
   any human agreed to pay for it.

2. **Arriving confirms the shift and tells the worker.** The placeholder site becomes the site
   they arrived at, the departure moment does not move (so the travel is paid), and the worker
   gets a notice in their own inbox - the durable half of the arrival, since the response
   message only exists on the screen that made it.

3. **An off-site check-out is refused, and the worker can ask an administrator to close it.**
   This is the rule the suite is really about. Nothing about an unconfirmed shift has been
   authorised by a *place*, so the system will not decide when it ended: the Clock Out is
   refused, the shift stays open and keeps counting, the worker's request raises one critical
   alert for the administrator, and the closing is the administrator's act
   (``/admin/force_clock_out``, at the hours they judge). The observer that would otherwise end
   a shift nobody remembers - the auto-close - is not allowed to pay an unconfirmed trip
   either; it says so in its summary and leaves the decision to a human.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import harness
import main
import overtime
from harness import (
    ADMIN,
    DOWNTOWN,
    INSIDE_DOWNTOWN,
    MOALLEM,
    OUTSIDE_ALL_SITES,
    bearer,
    clock_in,
    db_rows,
    db_scalar,
)

TRANSIT_SITE = main.TRANSIT_SITE_NAME


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _grant_transit(client, user_id: str = MOALLEM) -> None:
    """The privilege is per person and an administrator's to grant (see ``test_transit_to_site``)."""
    name = db_scalar("SELECT name FROM users WHERE id = ?", (user_id,))
    response = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(ADMIN),
        json={"user_id": user_id, "name": name, "email": "", "phone": "", "transit_enabled": True},
    )
    assert response.status_code == 200, response.text[:300]


def _depart(client, user_id: str = MOALLEM):
    """Clock in from a fix inside no geofence, and hand back the response."""
    response = clock_in(client, user_id, headers=bearer(user_id), coordinates=OUTSIDE_ALL_SITES)
    assert response.status_code == 200, response.text[:300]
    return response


def _backdate_the_departure(user_id: str = MOALLEM, hours: float = 9.0) -> None:
    """Move the shift's start into the past, so a watcher pass has something to act on."""
    conn = sqlite3.connect(str(harness.current_db_path()))
    try:
        stamp = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE active_sessions SET clock_in_time = ?, transit_start_time = ? WHERE worker_id = ?",
            (stamp, stamp, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _session(user_id: str = MOALLEM):
    rows = db_rows(
        "SELECT site_name, is_transit, clock_in_time FROM active_sessions WHERE worker_id = ?",
        (user_id,),
    )
    return rows[0] if rows else None


def _stats(client, user_id: str = MOALLEM) -> dict:
    return client.get("/api/v1/worker/me/stats", headers=bearer(user_id)).json()


def _ask_to_be_closed(client, user_id: str = MOALLEM):
    return client.post("/api/v1/worker/me/request_checkout", headers=bearer(user_id))


# ---------------------------------------------------------------------------
# 1. pending, and counting
# ---------------------------------------------------------------------------
def test_an_off_site_check_in_opens_a_pending_shift_that_counts(client):
    """On the clock, on the board, not payable: the state the feature is named for."""
    _grant_transit(client)
    body = _depart(client).json()
    assert body["status"] == "in_transit"
    assert body["site"] == TRANSIT_SITE
    assert body["paid_hours"] == 0.0, "a departure pays nothing until a site confirms it"
    assert "Arrival at site required" in body["message"]

    session = _session()
    assert session is not None, "the off-site check-in has to count as an open shift"
    assert session[0] == TRANSIT_SITE
    assert session[1] == 1, "and it says on it that no geofence has confirmed it"
    assert (
        db_scalar("SELECT start_source FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
        == "mobile_transit"
    )

    # The worker's own panel: on shift, on the road, and told so rather than left to infer it.
    stats = _stats(client)
    assert stats["active_session"]["site_name"] == TRANSIT_SITE
    assert stats["active_session"]["in_transit"] is True

    # ... and the console sees the same shift, so somebody other than the worker can act on it.
    live = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN)).json()
    mine = [row for row in live if row["worker_id"] == MOALLEM]
    assert mine and mine[0]["site_name"] == TRANSIT_SITE

    # Nothing payable yet, and no ledger row pretending otherwise.
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)) == 0
    assert stats["total_hours"] == 0


# ---------------------------------------------------------------------------
# 2. arrival
# ---------------------------------------------------------------------------
def test_arriving_confirms_the_shift_and_tells_the_worker(client):
    """The site changes from the placeholder to the real one, and the worker is told, durably."""
    _grant_transit(client)
    _depart(client)
    departed_at = db_scalar(
        "SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    )

    arrival = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert arrival.status_code == 200, arrival.text[:300]
    body = arrival.json()
    assert body["status"] == "arrived"
    assert body["site"] == DOWNTOWN, "the arrival names the site it was authorised by"
    assert DOWNTOWN in body["message"], "the worker is told they have arrived"

    session = _session()
    assert session[0] == DOWNTOWN, "the placeholder is replaced by the site"
    assert session[1] == 0, "and the shift is no longer unconfirmed"
    assert session[2] == departed_at, "the travel time starts at the departure, not the arrival"

    notices = db_rows(
        "SELECT kind, title, body FROM worker_notifications WHERE worker_id = ? ORDER BY id",
        (MOALLEM,),
    )
    assert notices, "the arrival has to leave the worker a notice they can read back"
    kind, title, body = notices[-1]
    assert kind == main.notifications.KIND_WORKER_TRANSIT_ARRIVED
    assert DOWNTOWN in title and DOWNTOWN in body
    assert "credited" in body, "the notice says what the arrival did to their hours"

    stats = _stats(client)
    assert stats["active_session"]["site_name"] == DOWNTOWN
    assert stats["active_session"]["in_transit"] is False


# ---------------------------------------------------------------------------
# 3. the off-site check-out: refused, asked for, and closed by an administrator
# ---------------------------------------------------------------------------
def test_an_off_site_check_out_is_refused_and_leaves_the_shift_open(client):
    """The rule: no place authorised this shift, so nothing about it is the worker's to end."""
    _grant_transit(client)
    _depart(client)
    started = db_scalar(
        "SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    )

    refused = clock_in(
        client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES
    )
    assert refused.status_code == 409, refused.text[:300]
    detail = refused.json()["detail"]
    assert detail["error_code"] == "off_site_checkout_needs_admin"
    assert detail["can_request"] is True, "the refusal has to point at the way out"
    assert detail["site_name"] == TRANSIT_SITE
    assert "still open" in detail["message"]

    session = _session()
    assert session is not None, "a refused punch must not close the shift"
    assert session[1] == 1 and session[2] == started, "the shift is exactly as it was"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)) == 0, (
        "a refusal writes no ledger row"
    )


def test_the_worker_can_ask_an_administrator_to_close_the_shift(client):
    """One critical alert per shift, not per tap - and the audit trail says who asked."""
    _grant_transit(client)
    _depart(client)

    asked = _ask_to_be_closed(client)
    assert asked.status_code == 200, asked.text[:300]
    assert asked.json()["already_asked"] is False
    assert asked.json()["site_name"] == TRANSIT_SITE

    alerts = db_rows(
        "SELECT kind, severity, title, worker_id, payload FROM admin_notifications "
        "WHERE kind = ?",
        (main.notifications.KIND_CHECKOUT_REQUEST,),
    )
    assert len(alerts) == 1, alerts
    _kind, severity, title, worker_id, payload = alerts[0]
    assert severity == "critical", "an unclosable shift is not an informational alert"
    assert worker_id == MOALLEM
    assert "asking to be clocked out" in title
    assert "In Transit" in payload, "the alert carries the shift, not just the person"

    # The same tap again is the same request: the shift stays behind one alert, so a worker
    # who taps twice (or whose phone retries) cannot bury it.
    again = _ask_to_be_closed(client)
    assert again.status_code == 200
    assert again.json()["already_asked"] is True
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM admin_notifications WHERE kind = ?",
            (main.notifications.KIND_CHECKOUT_REQUEST,),
        )
        == 1
    )
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'attendance_checkout_requested' "
            "AND entity_id = ?",
            (MOALLEM,),
        )
        == 1
    )


def test_an_administrator_closes_the_shift_the_worker_asked_about(client):
    """The end of an off-site shift is an administrator's act, at the hours they judge."""
    _grant_transit(client)
    _depart(client)
    _backdate_the_departure(hours=3.0)
    assert _ask_to_be_closed(client).status_code == 200

    closed = client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(ADMIN),
        json={"worker_id": MOALLEM, "hours": 2.5},
    )
    assert closed.status_code == 200, closed.text[:300]
    assert _session() is None, "the administrator's closing is what ends the shift"

    rows = db_rows(
        "SELECT site_name, hours, status_code, source, approved_hours FROM attendance_logs "
        "WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
        (MOALLEM,),
    )
    assert rows, "the closing records the shift"
    site, hours, status_code, source, approved = rows[0]
    assert site == TRANSIT_SITE
    assert status_code == "approved"
    assert source == "admin_override"
    assert approved == 2.5 and hours == 2.5, (
        "the administrator's figure is what the row records, not the all-night clock"
    )


def test_a_worker_with_no_open_shift_cannot_raise_a_request(client):
    """The endpoint asks about a shift; with none open there is nothing to ask about."""
    _grant_transit(client)
    refused = _ask_to_be_closed(client)
    assert refused.status_code == 400, refused.text[:300]
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM admin_notifications WHERE kind = ?",
            (main.notifications.KIND_CHECKOUT_REQUEST,),
        )
        == 0
    )


def test_the_watcher_will_not_pay_an_unconfirmed_trip(client):
    """The backstop must not become the automatic approval the feature withholds.

    The auto-close writes an *approved* row at the paid limit, so a shift that never reached a
    site would be paid by the one path that acts with nobody asking it to. It stands back
    instead, and says in its summary that it did.
    """
    harness.use_auto_close(client)
    _grant_transit(client)
    _depart(client)
    _backdate_the_departure(hours=9.0)

    summary = overtime.scan_auto_close()
    assert summary["transit_held"] == 1, summary
    assert summary["awaiting_arrival"][0]["worker_id"] == MOALLEM
    assert summary["closed"] == 0, "nothing may be closed, and therefore nothing paid"

    assert _session() is not None, "the shift is left for a human to end"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,)) == 0

    # A shift that reaches its site is not held back: the arrival already ended the question, and
    # the ordinary close is free to end the day at the paid limit as it does for anybody else.
    arrived = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert arrived.status_code == 200, arrived.text[:300]
    after_arrival = overtime.scan_auto_close()
    assert after_arrival["transit_held"] == 0, after_arrival
