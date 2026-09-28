"""A punch taken on a site whose geofence has been deleted.

WHY THIS SUITE EXISTS
---------------------
A punch has always meant "you are inside a geofence", and ``site_at`` answers that question by
looking at the sites that exist *now*. Deleting a site therefore erases the only thing that can
say a worker is where they are supposed to be, and the two states it collapses are not the same
state at all:

* the worker is somewhere they should not be - a refusal, as it always was; and
* the worker is standing exactly where their shift was opened, and an administrator has removed
  the site row out from under them.

The second one used to be indistinguishable from the first, which is how a shift came to be
unclosable from the field: every tap answered "you are outside any designated construction site
geofence", the shift ran on, and a transit trip that reached its destination was booked as
"clocked out without confirming arrival at any site geofence" - a false statement in the worker's
own record. The report that produced this suite is exactly that: a transit worker deleted the
site, checked in, added the site back, and never got an arrival or a site name.

The evidence used is the delete itself. ``/admin/sites/delete`` records the row it removed - lat,
lon, radius - in the append-only audit log, so the boundary a fence had outlives the fence. That
is what makes this a question about a *place* rather than a hole in the rule: the exception can
only be claimed from inside the boundary the site actually had, and only to close or confirm a
shift that was already open.

What is asserted here:

1. **A transit trip that reaches a deleted destination is confirmed**, keeps its departure
   moment, and says in its flags that the fence it matched has been deleted.
2. **A shift whose own site row was deleted can be closed from that site** - approved, named
   after the site the shift was opened at, and flagged.
3. **The exception is a place, not a bypass.** A worker away from the deleted fence is still
   refused, a deleted fence never opens a new shift, and a site removed outside the API (no
   recorded delete) leaves nothing to match - the ordinary refusal, exactly as before.
4. **The worker's own panel shows the real site**, which is the symptom the report described.
5. **The quick link makes the same exception**, because it is the other way a worker punches and
   a rule that holds on one path and not the other is how the same report comes back.
"""

from __future__ import annotations

import sqlite3

import harness
import main
from harness import (
    ADMIN,
    DOWNTOWN,
    HEAD_ADMIN,
    INSIDE_DOWNTOWN,
    MOALLEM,
    OUTSIDE_ALL_SITES,
    WORKER,
    bearer,
    clock_in,
    db_rows,
    db_scalar,
    jpeg_bytes,
)

#: The site's seeded geofence: lat, lon, radius. Re-adding it has to use the same numbers, or
#: the test would be measuring the fixture rather than the rule.
DOWNTOWN_GEOMETRY = (30.05, 31.23, 65.0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _grant_transit(client, user_id: str = MOALLEM) -> None:
    """Hand the off-geofence privilege to one account, through the real admin endpoint."""
    name = db_scalar("SELECT name FROM users WHERE id = ?", (user_id,))
    response = client.post(
        "/api/v1/admin/users/edit",
        headers=bearer(ADMIN),
        json={"user_id": user_id, "name": name, "email": "", "phone": "", "transit_enabled": True},
    )
    assert response.status_code == 200, response.text[:300]


def _delete_downtown(client):
    """Remove the seeded site through the API, which is what records the fence it had."""
    return client.post(
        "/api/v1/admin/sites/delete", headers=bearer(ADMIN), data={"site_name": DOWNTOWN}
    )


def _readd_downtown(client):
    lat, lon, radius = DOWNTOWN_GEOMETRY
    return client.post(
        "/api/v1/admin/sites/add",
        headers=bearer(ADMIN),
        json={"site_name": DOWNTOWN, "location_input": f"{lat},{lon}", "radius": radius},
    )


def _backdate_the_departure(user_id: str = MOALLEM, hours: float = 3.0) -> None:
    """Move the shift's start into the past so a closed shift has travel time to credit."""
    from datetime import datetime, timedelta

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


#: ``db_rows`` hands back plain tuples, so these two keep the column order in one place rather
#: than spelling ``row[2]`` at each assertion. ``_session`` is ``None`` when no shift is open.
SESSION_COLUMNS = "site_name, is_transit, clock_in_time"
LOG_COLUMNS = "action, status_code, site_name, source, flag_reason, hours"


def _session(user_id: str = MOALLEM):
    rows = db_rows(
        f"SELECT {SESSION_COLUMNS} FROM active_sessions WHERE worker_id = ?", (user_id,)
    )
    return rows[0] if rows else None


def _logs(user_id: str = MOALLEM):
    return db_rows(
        f"SELECT {LOG_COLUMNS} FROM attendance_logs WHERE worker_id = ? ORDER BY id", (user_id,)
    )


def _issue_link(client, worker_id: str = WORKER) -> str:
    """A quick link for one worker, issued the way the console issues it."""
    response = client.post(
        "/api/v1/admin/quick_links", headers=bearer(HEAD_ADMIN), json={"worker_id": worker_id}
    )
    assert response.status_code == 200, response.text[:300]
    return response.json()["token"]


def _link_punch(client, token, *, coordinates: str = INSIDE_DOWNTOWN):
    lat, lon = coordinates.split(",")
    return client.post(
        f"/api/v1/q/{token}",
        data={"lat": lat, "lon": lon, "confirm_early_checkout": "1"},
        files={"selfie": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )


def _clear_standing_review() -> None:
    """Drop the ``pending_review`` row the seed leaves behind.

    A flagged account is refused a clock-out on *both* punch paths, which is a rule of its own
    (and has its own test elsewhere). Left in place it would be the thing every assertion below
    measured instead of the deleted fence.
    """
    conn = sqlite3.connect(str(harness.current_db_path()))
    try:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. a transit trip that reaches a deleted destination
# ---------------------------------------------------------------------------
def test_a_transit_arrival_at_a_deleted_destination_confirms_the_trip(client):
    """The milestone the report never got: arrival at the site, named, with the fence recorded."""
    _grant_transit(client)
    departed = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES)
    assert departed.json()["status"] == "in_transit"
    departed_at = db_scalar(
        "SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (MOALLEM,)
    )

    assert _delete_downtown(client).status_code == 200

    arrival = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert arrival.status_code == 200, arrival.text[:300]
    assert arrival.json()["status"] == "arrived"
    assert arrival.json()["site"] == DOWNTOWN, "the trip must name the site it arrived at"

    session = _session()
    assert session is not None, "an arrival authorises the shift; it does not end it"
    assert session[0] == DOWNTOWN
    assert session[1] == 0, "arrival clears the in-transit flag"
    assert session[2] == departed_at, "the departure moment must not move"

    logs = _logs()
    assert len(logs) == 1, logs
    assert logs[0][0] == "Transit Confirmed"
    assert logs[0][1] == "approved"
    assert logs[0][2] == DOWNTOWN
    assert logs[0][3] == "site_arrival"
    assert logs[0][4] == main.DELETED_SITE_REASON, (
        "a punch that matched no configured fence has to say so on its own row"
    )


def test_the_workers_own_panel_shows_the_site_after_a_deleted_fence_arrival(client):
    """The reported symptom, read where the worker read it: the panel said "In Transit"."""
    _grant_transit(client)
    clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES)
    _delete_downtown(client)
    clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)

    stats = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM)).json()
    assert stats["active_session"]["site_name"] == DOWNTOWN
    assert stats["active_session"]["site_name"] != main.TRANSIT_SITE_NAME


def test_the_deleted_destination_is_credited_when_the_trip_closes_the_same_way(client):
    """Arrive and finish in one tap at a deleted destination: the travel is still paid for."""
    _grant_transit(client)
    clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=OUTSIDE_ALL_SITES)
    _backdate_the_departure(hours=3.0)
    _delete_downtown(client)

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
    assert body["status"] != "pending_review", "reaching the site is not an abandonment"
    assert body["site"] == DOWNTOWN
    assert body["hours"] >= 2.9, f"the three hours of travel were not credited: {body}"
    assert _session() is None, "the shift closed"


# ---------------------------------------------------------------------------
# 2. a plain shift whose own site row was deleted
# ---------------------------------------------------------------------------
def test_the_worker_can_close_a_shift_whose_own_site_was_deleted(client):
    """The trap: without this the shift could never be closed from the field at all."""
    opened = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert opened.status_code == 200, opened.text[:300]
    assert _delete_downtown(client).status_code == 200

    closed = clock_in(
        client,
        MOALLEM,
        action="Clock Out",
        headers=bearer(MOALLEM),
        coordinates=INSIDE_DOWNTOWN,
        confirmed=True,
    )
    assert closed.status_code == 200, closed.text[:300]
    assert closed.json()["site"] == DOWNTOWN
    assert _session() is None, "the shift has to be closable, or it runs on forever"

    logs = _logs()
    assert len(logs) == 2, logs
    assert logs[1][1] == "approved"
    assert logs[1][2] == DOWNTOWN, (
        "the close names the site the shift was opened at - it happened there"
    )
    assert main.DELETED_SITE_REASON in (logs[1][4] or "")


# ---------------------------------------------------------------------------
# 3. the exception is a place, not a bypass
# ---------------------------------------------------------------------------
def test_a_worker_away_from_the_deleted_fence_is_still_refused(client):
    """A deleted site is not a licence to clock out from anywhere."""
    clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    _delete_downtown(client)

    refused = clock_in(
        client,
        MOALLEM,
        action="Clock Out",
        headers=bearer(MOALLEM),
        coordinates=OUTSIDE_ALL_SITES,
        confirmed=True,
    )
    assert refused.status_code == 403, refused.text[:300]
    assert "outside any designated construction site geofence" in refused.json()["detail"]
    assert _session() is not None, "the refusal closed the shift anyway"
    assert len(_logs()) == 1, "a refused punch writes no row"


def test_a_deleted_fence_cannot_open_a_new_shift(client):
    """Standing where a site used to be is not standing at a site the company still runs."""
    _delete_downtown(client)

    refused = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert refused.status_code == 403, refused.text[:300]
    assert _session() is None
    assert _logs() == []


def test_a_site_removed_without_a_recorded_delete_is_still_a_refusal(client):
    """The evidence is the delete event, so a row taken out by hand leaves nothing to match."""
    clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    conn = sqlite3.connect(str(harness.current_db_path()))
    try:
        conn.execute("DELETE FROM construction_sites WHERE site_name = ?", (DOWNTOWN,))
        conn.commit()
    finally:
        conn.close()

    refused = clock_in(
        client,
        MOALLEM,
        action="Clock Out",
        headers=bearer(MOALLEM),
        coordinates=INSIDE_DOWNTOWN,
        confirmed=True,
    )
    assert refused.status_code == 403, refused.text[:300]
    assert _session() is not None


# ---------------------------------------------------------------------------
# 4. the site coming back
# ---------------------------------------------------------------------------
def test_the_readded_site_is_the_live_fence_again(client):
    """Re-adding it restores the ordinary rule - and the fence that is live is the one in force."""
    clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    _delete_downtown(client)

    closed = clock_in(
        client,
        MOALLEM,
        action="Clock Out",
        headers=bearer(MOALLEM),
        coordinates=INSIDE_DOWNTOWN,
        confirmed=True,
    )
    assert closed.status_code == 200, closed.text[:300]
    assert _readd_downtown(client).status_code == 200

    # The next shift is judged by the live fence, so the flag belongs to the punch that needed
    # the exception and to nothing after it.
    reopened = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert reopened.status_code == 200, reopened.text[:300]
    assert reopened.json()["site"] == DOWNTOWN
    assert reopened.json()["status"] != "in_transit"
    logs = _logs()
    assert logs[-1][2] == DOWNTOWN
    assert main.DELETED_SITE_REASON not in (logs[-1][4] or "")


def test_the_reported_sequence_ends_with_the_shift_at_the_readded_site(client):
    """The reported journey, verbatim: delete the site, check in, add it back.

    A transit-enabled worker clocking in inside a fence that has been deleted gets a travel
    shift - there is no site row left to name, so the shift is opened at the placeholder. The
    arrival that follows once the site is back is the ordinary live-fence arrival, and the
    panel ends up naming the site that was added back. What must never happen is what the
    report described: the shift left saying "In Transit" with nothing able to change it.
    """
    _grant_transit(client)
    assert _delete_downtown(client).status_code == 200

    departure = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert departure.status_code == 200, departure.text[:300]
    assert departure.json()["status"] == "in_transit"
    assert _session()[0] == main.TRANSIT_SITE_NAME

    assert _readd_downtown(client).status_code == 200

    arrival = clock_in(client, MOALLEM, headers=bearer(MOALLEM), coordinates=INSIDE_DOWNTOWN)
    assert arrival.status_code == 200, arrival.text[:300]
    assert arrival.json()["status"] == "arrived"
    assert arrival.json()["site"] == DOWNTOWN

    session = _session()
    assert session[0] == DOWNTOWN and session[1] == 0
    stats = client.get("/api/v1/worker/me/stats", headers=bearer(MOALLEM)).json()
    assert stats["active_session"]["site_name"] == DOWNTOWN, (
        "the panel has to name the site that was added back, not the placeholder"
    )


# ---------------------------------------------------------------------------
# 5. the other way a worker punches
# ---------------------------------------------------------------------------
def test_a_quick_link_can_close_a_shift_whose_site_was_deleted(client):
    """The shared tablet is on the same site, so it is trapped by the same deletion."""
    _clear_standing_review()
    # The seed leaves worker 1 clocked in at the site, which is the shift this closes.
    assert _session(WORKER) is not None
    token = _issue_link(client)
    assert _delete_downtown(client).status_code == 200

    closed = _link_punch(client, token)
    assert closed.status_code == 200, closed.text[:300]
    assert _session(WORKER) is None, "the link could not close the shift it was taken at"

    rows = db_rows(
        f"SELECT {LOG_COLUMNS} FROM attendance_logs WHERE worker_id = ? ORDER BY id DESC LIMIT 1",
        (WORKER,),
    )
    assert rows[0][2] == DOWNTOWN
    assert main.DELETED_SITE_REASON in (rows[0][4] or "")


def test_a_quick_link_cannot_open_a_shift_on_a_deleted_fence(client):
    """No shift is open, so there is nothing to close - and a deleted site is not a workplace."""
    _clear_standing_review()
    assert _session(MOALLEM) is None
    token = _issue_link(client, MOALLEM)
    assert _delete_downtown(client).status_code == 200

    refused = _link_punch(client, token)
    assert refused.status_code == 403, refused.text[:300]
    assert refused.json()["detail"]["error_code"] == "outside_geofence"
    assert _session(MOALLEM) is None
