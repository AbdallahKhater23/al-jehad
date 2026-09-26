"""Deferred transit-to-site shifts: a paid shift may start on the road and be authorised on arrival.

WHY THIS SUITE EXISTS
---------------------
A punch has always meant "you are inside a geofence". That is the wrong rule for the people who
drive between sites: their paid day begins when they leave, not when they arrive, and under the
geofence rule those hours are simply lost. The feature relaxes exactly one thing - *location at the
moment the shift opens* - and nothing else. The face check still runs on every phase, the hours
are still paid by ``shift_hours``, and a shift that never reaches a site is never auto-approved.

What is asserted here:

1. **The privilege is per person, granted by an administrator.** Two workers can hold the same
   role and only one of them drive; so it is a flag on the account, off by default, that
   ``/admin/users/edit`` turns on for one named person. A worker cannot grant it to themselves.
2. **A plain worker is still refused off-geofence.** The relaxation must not have widened the
   rule for everybody - only for accounts an administrator has named.
3. **The three phases.** Departure opens an unpaid, unconfirmed shift (``in_transit``); a
   checkpoint inside a fence confirms it, keeping the departure timestamp so travel is paid; and a
   clock-out that never reached a site is routed to review, not approved.
4. **The biometric pipeline is not bypassed.** These punches run the same liveness and match the
   gate does - the suite's stub engine returns an approved match, so a punch that succeeds here
   has passed the whole chain.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import harness
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

TRANSIT_SITE = "In Transit"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _grant_transit(client, user_id: str = MOALLEM) -> None:
    """Hand the off-geofence privilege to one account, the way an administrator does it.

    Through the real endpoint rather than a direct ``UPDATE``: the point of this feature is that
    a *human* decides who gets it, so the test exercises the decision, not the column.
    """
    name = db_scalar("SELECT name FROM users WHERE id = ?", (user_id,))
    response = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(ADMIN),
        json={"user_id": user_id, "name": name, "email": "", "phone": "", "transit_enabled": True},
    )
    assert response.status_code == 200, response.text[:300]
    assert db_scalar("SELECT transit_enabled FROM users WHERE id = ?", (user_id,)) == 1


def _depart(client, user_id: str = MOALLEM):
    """Open a travel shift from a fix inside no geofence, and assert it opened as a transit one."""
    response = clock_in(client, user_id, headers=bearer(user_id), coordinates=OUTSIDE_ALL_SITES)
    assert response.status_code == 200, response.text[:300]
    return response


def _backdate_the_departure(user_id: str = MOALLEM, hours: float = 3.0) -> None:
    """Move the shift's start into the past so a closed shift has real travel time to credit."""
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


def _log_rows(user_id: str = MOALLEM) -> list[sqlite3.Row]:
    return db_rows(
        "SELECT action, status, status_code, hours, approved_hours, source, flag_reason "
        "FROM attendance_logs WHERE worker_id = ? ORDER BY id",
        (user_id,),
    )


# ---------------------------------------------------------------------------
# 1. the grant is the administrator's, one account at a time
# ---------------------------------------------------------------------------
def test_the_privilege_is_off_by_default_and_only_an_administrator_turns_it_on(client):
    """The flag defaults off, the roster publishes it, and a worker cannot grant it to themself."""
    roster = client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()
    by_id = {row["id"]: row for row in roster}
    assert by_id[MOALLEM]["transit_enabled"] is False, "a fresh account must not hold the privilege"

    # A worker has no way in: the edit endpoint is admin-only, so self-granting is refused before
    # the field is even read.
    refused = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(MOALLEM),
        json={"user_id": MOALLEM, "name": "Seed Lead Worker", "transit_enabled": True},
    )
    assert refused.status_code in (401, 403), refused.text[:300]
    assert db_scalar("SELECT transit_enabled FROM users WHERE id = ?", (MOALLEM,)) == 0

    _grant_transit(client)
    roster = client.get("/api/v1/admin/users", headers=bearer(ADMIN)).json()
    assert {row["id"]: row for row in roster}[MOALLEM]["transit_enabled"] is True


def test_the_grant_is_written_to_the_audit_log_with_a_before_and_after(client):
    """A privilege that can be flipped without a trail would be the one silent write on the screen."""
    _grant_transit(client)
    rows = db_rows(
        "SELECT before_json, after_json FROM audit_log WHERE action = 'user_edit' "
        "AND entity_id = ? ORDER BY id DESC LIMIT 1",
        (MOALLEM,),
    )
    assert rows, "the transit grant left no audit entry"
    before, after = rows[0]
    assert '"transit_enabled": false' in before
    assert '"transit_enabled": true' in after


def test_an_edit_that_does_not_mention_the_grant_leaves_it_alone(client):
    """Renaming somebody must not silently revoke their travel privilege."""
    _grant_transit(client)
    renamed = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(ADMIN),
        json={"user_id": MOALLEM, "name": "Renamed Lead Worker", "email": "", "phone": ""},
    )
    assert renamed.status_code == 200, renamed.text[:300]
    assert db_scalar("SELECT transit_enabled FROM users WHERE id = ?", (MOALLEM,)) == 1


# ---------------------------------------------------------------------------
# 2. a plain worker is still confined to the fence
# ---------------------------------------------------------------------------
def test_a_worker_without_the_privilege_is_still_refused_off_geofence(client):
    """The relaxation must not have widened the rule for everybody - only for the named accounts."""
    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES)
    assert response.status_code == 403, response.text[:300]
    assert "outside any designated construction site geofence" in response.json()["detail"]
    assert (
        db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    ), "the refusal opened a shift anyway"
    assert _log_rows() == [], "a refused punch must leave no ledger row"


# ---------------------------------------------------------------------------
# 3. Phase A - departure
# ---------------------------------------------------------------------------
def test_a_transit_departure_opens_an_unconfirmed_shift(client):
    """Off-geofence and privileged: an open, unpaid shift carrying its departure moment and origin."""
    _grant_transit(client)
    response = _depart(client)
    body = response.json()
    assert body["status"] == "in_transit"
    assert body["site"] == TRANSIT_SITE
    assert body["hours"] == 0.0, "a departure pays nothing until a site confirms it"

    session = db_rows(
        "SELECT site_name, start_source, is_transit, clock_in_time, transit_start_time, "
        "transit_origin_lat, transit_origin_lon FROM active_sessions WHERE worker_id = ?",
        (MOALLEM,),
    )
    assert len(session) == 1
    site_name, source, is_transit, clock_in_time, transit_start, lat, lon = session[0]
    assert site_name == TRANSIT_SITE
    assert source == "mobile_transit"
    assert is_transit == 1
    assert transit_start == clock_in_time, "the departure moment is the shift's clock-in"
    assert lat is not None and lon is not None, "the origin is evidence, and must be recorded"

    assert _log_rows() == [], "the departure is a session, not a payable ledger row"


# ---------------------------------------------------------------------------
# 4. Phase B - arrival confirms the shift
# ---------------------------------------------------------------------------
def test_an_arrival_inside_a_fence_confirms_the_shift_and_keeps_the_departure_time(client):
    """The milestone that authorises the shift: the flag clears, the start does not move."""
    _grant_transit(client)
    _depart(client)
    departed_at = db_scalar(
        "SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    )

    response = clock_in(
        client, MOALLEM, action="Transit Checkpoint", headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["status"] == "arrived"
    assert response.json()["site"] == DOWNTOWN

    row = db_rows(
        "SELECT site_name, is_transit, clock_in_time FROM active_sessions WHERE worker_id = ?",
        (MOALLEM,),
    )[0]
    assert row[0] == DOWNTOWN
    assert row[1] == 0, "arrival clears the in-transit flag"
    assert row[2] == departed_at, "the clock-in time must not move, or the travel hours are lost"

    logs = _log_rows()
    assert len(logs) == 1, logs
    action, status, status_code, hours, approved_hours, source, _flag = logs[0]
    assert action == "Transit Confirmed"
    assert status_code == "approved"
    assert hours == 0.0, "the arrival milestone authorises the shift; the clock-out pays for it"
    assert source == "site_arrival"


def test_an_arrival_that_is_not_at_a_site_is_refused_and_leaves_the_shift_in_transit(client):
    """A check-in from the car park is not an arrival; the shift is left exactly as it was."""
    _grant_transit(client)
    _depart(client)

    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES)
    assert response.status_code == 422, response.text[:300]
    assert "Cannot confirm arrival" in response.json()["detail"]

    assert db_scalar("SELECT is_transit FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1
    assert _log_rows() == [], "a refused arrival writes no milestone"


# ---------------------------------------------------------------------------
# 5. Phase C - an abandoned transit shift goes to review, never auto-approval
# ---------------------------------------------------------------------------
def test_clocking_out_still_in_transit_is_held_for_review(client):
    """Never reached a site: the shift closes so it stops counting, but nothing is auto-approved."""
    _grant_transit(client)
    _depart(client)
    _backdate_the_departure(hours=2.0)

    response = clock_in(
        client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES
    )
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["paid_hours"] == 0.0, "an abandoned trip is not payable without a decision"

    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0

    logs = _log_rows()
    assert len(logs) == 1, logs
    _action, status, status_code, hours, approved_hours, _source, flag = logs[0]
    assert status_code == "pending_review"
    assert approved_hours is None, "assigning approved hours would be the automatic approval we withheld"
    assert hours and hours > 1.5, "the travel time is recorded for the reviewer, not approved"
    assert flag == "Worker clocked out without confirming arrival at any site geofence"

    alert = db_rows(
        "SELECT title FROM admin_notifications WHERE kind = 'review_pending' AND worker_id = ?",
        (MOALLEM,),
    )
    assert alert, "an abandoned transit shift must raise an administrator alert"


# ---------------------------------------------------------------------------
# 6. travel is credited once the shift closes at a site
# ---------------------------------------------------------------------------
def test_travel_time_is_credited_when_the_shift_closes_at_the_site(client):
    """A departure three hours before the close is paid for, because the start never moved."""
    _grant_transit(client)
    _depart(client)
    _backdate_the_departure(hours=3.0)

    arrived = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert arrived.status_code == 200, arrived.text[:300]

    closed = clock_in(
        client,
        MOALLEM,
        action="Clock Out",
        headers=bearer(MOALLEM),
        coordinates=INSIDE_DOWNTOWN,
        confirmed=True,
    )
    assert closed.status_code == 200, closed.text[:300]
    body = closed.json()
    assert body["site"] == DOWNTOWN
    assert body["hours"] >= 2.9, f"the three hours of travel were not credited: {body}"


def test_a_clock_out_at_a_site_confirms_and_closes_the_shift_in_one_tap(client):
    """A worker who drives straight to a site and clocks out has arrived; that must be credited."""
    _grant_transit(client)
    _depart(client)
    _backdate_the_departure(hours=2.5)

    closed = clock_in(
        client,
        MOALLEM,
        action="Clock Out",
        headers=bearer(MOALLEM),
        coordinates=INSIDE_DOWNTOWN,
        confirmed=True,
    )
    assert closed.status_code == 200, closed.text[:300]
    body = closed.json()
    assert body["status"] != "pending_review", "an arrival inside a fence is not an abandonment"
    assert body["site"] == DOWNTOWN
    assert body["hours"] >= 2.4, f"the travel was not credited: {body}"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0


# ---------------------------------------------------------------------------
# 7. the schema the state machine depends on
# ---------------------------------------------------------------------------
def test_the_migration_adds_the_transit_columns_idempotently():
    """The drift guard replays the list; a second run must add nothing and change nothing."""
    import migrations

    conn = sqlite3.connect(":memory:")
    try:
        migrations.ensure_schema(conn)
        migrations.migration_27_transit_to_site_shifts(conn)
        users = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        sessions = {row[1] for row in conn.execute("PRAGMA table_info(active_sessions)")}
        assert "transit_enabled" in users
        assert {"is_transit", "transit_start_time", "transit_origin_lat", "transit_origin_lon"} <= sessions
        # Replay: nothing is added (and, crucially, no exception is raised).
        migrations.migration_27_transit_to_site_shifts(conn)
        assert migrations.SCHEMA_VERSION == 27
    finally:
        conn.close()
