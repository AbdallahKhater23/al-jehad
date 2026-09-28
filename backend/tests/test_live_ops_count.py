"""The Live Ops board's figures, counted instead of downloaded.

WHY THIS SUITE EXISTS
---------------------
``GET /admin/live_ops/count`` is the board's own read: how many people are on site, how many
places that is spread across, how many arrived late, and which shift has been open longest.
Before it existed, every one of those numbers was produced by fetching payloads and counting
what was in them - including ``/admin/users``, which answers ``password_set`` per row and joins
the audit log for every account's last password change, so a board whose job is to say "12 on
site" paid a price that grew with the *roster*.

The one claim that has to hold is the one the board cannot check for itself: **these figures
are the row list, counted** - the same join, the same rows - because a board whose numeral
disagrees with the table under it is worse than a board with no numeral. So most of this suite
is the counted read compared against ``/admin/active_sessions`` on the same fixture, and one
test compares ``dashboard.now`` against it, because that panel borrows the read rather than
repeating its query.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from harness import ADMIN, DB_PATH, MOALLEM, OFF_OFFICE, WORKER, bearer, db_scalar

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"
OTHER_SITE = "Harbour Depot"


# ---------------------------------------------------------------------------
# planting
# ---------------------------------------------------------------------------
def _plant(worker_id: str, *, hours_ago: float | None = None, stamp: str | None = None,
           site_name: str = SITE, late_flag: str | None = None) -> str:
    """Open a shift straight in the database, and answer its clock-in.

    The API has no way to backdate a clock-in - correctly - and this suite needs shifts of
    different ages to have a "longest" at all. ``stamp`` is for the one case that is about the
    stamp itself (a value nothing in the application can read). Closed explicitly: a connection
    left open can hold a lock the per-test database reset trips over.
    """
    if stamp is None:
        stamp = (datetime.now() - timedelta(seconds=int(round((hours_ago or 0) * 3600)))).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, late_flag) "
            "VALUES (?,?,?,?)",
            (worker_id, site_name, stamp, late_flag),
        )
        conn.commit()
    finally:
        conn.close()
    return stamp


def _clear_board() -> None:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM active_sessions")
        conn.commit()
    finally:
        conn.close()


def _count(client) -> dict:
    response = client.get("/api/v1/admin/live_ops/count", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


def _rows(client) -> list[dict]:
    response = client.get("/api/v1/admin/active_sessions", headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:300]
    return response.json()


def _sorted(ids) -> list[str]:
    return sorted(str(value) for value in ids)


# ---------------------------------------------------------------------------
# the figures are the rows
# ---------------------------------------------------------------------------
def test_the_figures_are_the_row_list_counted(client):
    """The claim the board cannot check for itself: numeral and table are the same rows."""
    _clear_board()
    _plant(WORKER, hours_ago=10)
    _plant(MOALLEM, hours_ago=2, site_name=OTHER_SITE, late_flag="outside the 05:00-06:00 window")
    _plant(OFF_OFFICE, hours_ago=5, site_name=OTHER_SITE)

    rows = _rows(client)
    counted = _count(client)

    assert counted["on_site"] == len(rows) == 3
    assert _sorted(counted["worker_ids"]) == _sorted(row["worker_id"] for row in rows)
    assert counted["late"] == 1, counted
    # The sites, in the order the dashboard draws them: most people first, then by name.
    assert counted["sites"] == [
        {"site_name": OTHER_SITE, "workers": 2},
        {"site_name": SITE, "workers": 1},
    ], counted["sites"]
    assert counted["as_of"], "the figures are a snapshot and say when they were taken"


def test_late_is_the_sentence_the_server_writes(client):
    """``late_flag`` is prose or nothing: a ``true``/``'1'`` test would count nobody, ever."""
    _clear_board()
    _plant(WORKER, hours_ago=3, late_flag="outside Downtown Tower A's 05:00-06:00 window")
    _plant(MOALLEM, hours_ago=3)
    # The other spelling the rule names rather than assumes: a column that holds prose can
    # hold a zero too, and a zero is not an arrival that missed its window.
    _plant(OFF_OFFICE, hours_ago=3, late_flag="0")

    assert _count(client)["late"] == 1, "only the sentence is a late arrival"
    # ...and the rows carry the same flag, which is what the row's own badge reads.
    flags = {row["worker_id"]: row["late_flag"] for row in _rows(client)}
    assert flags[WORKER] and not flags[MOALLEM] and flags[OFF_OFFICE] == "0"


def test_the_longest_shift_is_the_oldest_clock_in_the_board_can_measure(client):
    _clear_board()
    _plant(WORKER, hours_ago=1)
    _plant(MOALLEM, hours_ago=9, site_name=OTHER_SITE)
    _plant(OFF_OFFICE, hours_ago=4)

    counted = _count(client)
    longest = counted["longest"]
    assert longest is not None
    assert longest["worker_id"] == MOALLEM, "nine hours ago is the longest shift on the board"
    assert longest["site_name"] == OTHER_SITE
    # The name the *rows* carry for that worker, because the hint under the figure and the row
    # it points at are read side by side: a count that named somebody else would be a bug the
    # board could not show.
    theirs = [row for row in _rows(client) if row["worker_id"] == MOALLEM][0]
    assert longest["name"] == theirs["name"]
    # The count is the server's own arithmetic, and it is *sent*: the console starts its live
    # counter from it rather than parsing a zone-less stamp in whatever zone it is standing in.
    assert 9 * 3600 - 5 <= longest["seconds_on_site"] <= 9 * 3600 + 5, longest


def test_a_clock_in_nothing_can_read_is_stepped_over_not_named(client):
    """A shift whose duration cannot be computed is not the board's longest shift."""
    _clear_board()
    _plant(WORKER, stamp="not a time")
    _plant(MOALLEM, hours_ago=2)

    longest = _count(client)["longest"]
    assert longest is not None
    assert longest["worker_id"] == MOALLEM, longest
    assert longest["seconds_on_site"] >= 2 * 3600 - 5


def test_nobody_on_shift_is_zero_and_no_longest_shift(client):
    """Nothing is zero, but a *longest shift* that does not exist is not 0h 0m either."""
    _clear_board()
    counted = _count(client)
    assert counted["on_site"] == 0
    assert counted["late"] == 0
    assert counted["sites"] == []
    assert counted["worker_ids"] == []
    assert counted["longest"] is None


def test_a_session_whose_account_is_gone_is_counted_by_neither(client):
    """The join is the rows' own, so a stale session cannot move the two apart."""
    _clear_board()
    _plant(WORKER, hours_ago=3)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            ("90001", SITE, datetime.now().strftime(TS)),
        )
        conn.commit()
    finally:
        conn.close()
    assert db_scalar("SELECT COUNT(*) FROM users WHERE id = ?", ("90001",)) == 0

    counted = _count(client)
    assert counted["on_site"] == len(_rows(client)) == 1, "the orphan is dropped by the join"
    assert counted["worker_ids"] == [WORKER]


# ---------------------------------------------------------------------------
# who may read it
# ---------------------------------------------------------------------------
def test_the_boards_own_rules_are_the_boards_own_audience(client):
    """``admin_only``, like every other read of the roster and the gate."""
    assert client.get("/api/v1/admin/live_ops/count", headers=bearer(WORKER)).status_code == 403
    assert client.get("/api/v1/admin/live_ops/count").status_code in (401, 403)
    assert client.get("/api/v1/admin/live_ops/count", headers=bearer(MOALLEM)).status_code == 403


# ---------------------------------------------------------------------------
# one read, two screens
# ---------------------------------------------------------------------------
def test_the_dashboard_borrows_the_boards_figures_rather_than_repeating_them(client):
    """``dashboard.now`` is the same read, so the two screens cannot disagree."""
    _clear_board()
    _plant(WORKER, hours_ago=3)
    _plant(MOALLEM, hours_ago=1, site_name=OTHER_SITE)

    counted = _count(client)
    panel = client.get("/api/v1/admin/dashboard", headers=bearer(ADMIN)).json()["now"]

    assert panel["on_shift"] == counted["on_site"] == 2
    assert panel["by_site"] == counted["sites"], (
        "the dashboard and the board are two answers to one question: who is on the gate"
    )
