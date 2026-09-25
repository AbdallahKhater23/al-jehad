"""An administrator's own record is the head admin's to act on, and nobody else's.

WHY THIS EXISTS
---------------
``_guard_standard_admin`` has always said one sentence - a standard admin may not act on an
administrator's account - and for a long time it was asked in one place: the endpoints that
write an account's *own columns* (its name, its password, its status) and, later, its face.
Every other surface let a standard admin reach over a peer without anything noticing:

* ``/admin/force_clock_in`` and ``/admin/force_clock_out``, which write the account's own
  attendance - a forced clock-in is a shift on site with no face match and no geofence, and a
  forced clock-out is that shift's payable hours, so a standard admin could have put a peer on
  site and priced their day;
* ``/admin/approve_review`` and ``/admin/reject_review``, where an administrator's own long
  shift waits like anybody else's (``test_admin_shift_visibility``) - approving it is
  authorising overtime for a peer, and rejecting it is taking a peer's hours away;
* the live crossing answers (``/admin/overtime/crossings/{worker_id}/accept|decline``), which
  are the same decision asked while the shift is still running.

A rule with holes is not a rule, and the holes are the interesting half: an administrator who
could not be *deleted* by a peer could still be put on site, paid and have their overtime
settled by one. So this suite pins the whole boundary - and pins the two things the boundary is
not: a **worker** is still a standard admin's to manage in all three places, and a
**head admin** can still decide an administrator's own shift, which is the only way that shift
is ever decided at all on a site with a single administrator.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from harness import ADMIN, DB_PATH, HEAD_ADMIN, MOALLEM, bearer, clock_in, db_scalar

TS = "%Y-%m-%d %H:%M:%S"
SITE = "Downtown Tower A"
#: The long shift every overtime case is built from: past a 8 h paid day, and past the
#: threshold the crossing alert fires on, so the same planted session serves both queues.
LONG_SHIFT_HOURS = 9.5


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _plant_open_shift(worker_id: str, hours_on_site: float) -> str:
    """Open a shift that started ``hours_on_site`` ago, straight in the database.

    The API has no way to backdate a clock-in - correctly - so a test that needs a shift
    already past the paid day plants one. Closed explicitly: a connection left open can hold
    a lock the per-test database reset trips over.
    """
    clock_in_time = (datetime.now() - timedelta(hours=hours_on_site)).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, SITE, clock_in_time),
        )
        conn.commit()
    finally:
        conn.close()
    return clock_in_time


def _a_long_shift_for(client, user_id: str, *, hours_on_site: float = LONG_SHIFT_HOURS) -> int:
    """One completed shift past the paid day, and the clock-out row's id.

    ``confirmed`` answers the early-clock-out question: the gate is not what this suite is
    about. The punch is the person's own - ``/attendance/verify`` takes any authenticated
    role - which is how an administrator who works a site earns the row these tests then try
    to decide.
    """
    _plant_open_shift(user_id, hours_on_site)
    closed = clock_in(client, user_id, action="Clock Out", headers=bearer(user_id), confirmed=True)
    assert closed.status_code == 200, closed.text[:300]
    return int(db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (user_id,)))


def _force_in(client, actor: str, worker_id: str):
    return client.post(
        "/api/v1/admin/force_clock_in",
        headers=bearer(actor),
        json={"worker_id": worker_id, "site_name": SITE},
    )


def _force_out(client, actor: str, worker_id: str):
    return client.post(
        "/api/v1/admin/force_clock_out",
        headers=bearer(actor),
        json={"worker_id": worker_id},
    )


def _decide(client, actor: str, log_id: int, *, approve: bool = True, note: str = "checked the site sheet"):
    if approve:
        return client.post(
            "/api/v1/admin/approve_review", headers=bearer(actor), json={"log_id": log_id, "note": note}
        )
    return client.post(
        "/api/v1/admin/reject_review", headers=bearer(actor), json={"log_id": log_id, "note": note}
    )


def _answer_crossing(client, actor: str, worker_id: str, *, accept: bool = True):
    return client.post(
        f"/api/v1/admin/overtime/crossings/{worker_id}/{'accept' if accept else 'decline'}",
        headers=bearer(actor),
        json={"note": "the pour had to finish"},
    )


def _refused(response) -> None:
    assert response.status_code == 403, (
        f"a standard admin reached over an administrator: {response.status_code} {response.text[:300]}"
    )
    assert "Standard Admins cannot" in response.json()["detail"], response.json()


# ---------------------------------------------------------------------------
# 1. the forced clock
# ---------------------------------------------------------------------------
def test_a_standard_admin_cannot_force_an_administrator_in(client):
    """A forced clock-in is a shift on site with no face match and no geofence."""
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (ADMIN,)) == 0, (
        "the fixture leaves the administrator off shift, which is the case this is about"
    )
    _refused(_force_in(client, ADMIN, ADMIN))
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (ADMIN,)) == 0, (
        "nothing may be written by the refused call"
    )


def test_a_standard_admin_cannot_force_an_administrator_out(client):
    """And the close prices the shift, which is the payroll half of the same reach."""
    _plant_open_shift(ADMIN, 20.0)
    _refused(_force_out(client, ADMIN, ADMIN))
    assert db_scalar("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (ADMIN,)) is not None, (
        "the shift is still open: the refusal happened before anything was written"
    )
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ?", (ADMIN,)) == 0


def test_the_head_admin_can_force_an_administrator_in_and_out(client):
    """The other half of the rule: without it, nobody at all could put an admin on site."""
    opened = _force_in(client, HEAD_ADMIN, ADMIN)
    assert opened.status_code == 200, opened.text[:300]
    assert db_scalar("SELECT start_source FROM active_sessions WHERE worker_id = ?", (ADMIN,)) == (
        "admin_override"
    )

    closed = _force_out(client, HEAD_ADMIN, ADMIN)
    assert closed.status_code == 200, closed.text[:300]
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (ADMIN,)) == 0
    assert db_scalar("SELECT status FROM attendance_logs WHERE worker_id = ?", (ADMIN,)) is not None


# ---------------------------------------------------------------------------
# 2. the review queue
# ---------------------------------------------------------------------------
def test_a_standard_admin_cannot_decide_an_administrators_overtime(client):
    """Both answers, because being told "not yours" is exactly when the second is reached for."""
    log_id = _a_long_shift_for(client, ADMIN)
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime", (
        "the row has to be waiting, or this would be testing the 400 instead of the guard"
    )

    _refused(_decide(client, ADMIN, log_id, approve=True))
    _refused(_decide(client, ADMIN, log_id, approve=False))
    # ... and the row is untouched: still waiting, still nothing approved or refused on it.
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime"
    assert db_scalar("SELECT reviewed_by FROM attendance_logs WHERE id = ?", (log_id,)) is None


def test_the_head_admin_settles_an_administrators_overtime(client):
    log_id = _a_long_shift_for(client, ADMIN)

    decided = _decide(client, HEAD_ADMIN, log_id, approve=True)
    assert decided.status_code == 200, decided.text[:300]
    assert db_scalar("SELECT reviewed_by FROM attendance_logs WHERE id = ?", (log_id,)) == HEAD_ADMIN
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "approved"


# ---------------------------------------------------------------------------
# 3. the live crossing - the same question, asked earlier
# ---------------------------------------------------------------------------
def test_a_standard_admin_cannot_answer_an_administrators_crossing(client):
    _plant_open_shift(ADMIN, LONG_SHIFT_HOURS)

    _refused(_answer_crossing(client, ADMIN, ADMIN, accept=True))
    _refused(_answer_crossing(client, ADMIN, ADMIN, accept=False))
    assert db_scalar(
        "SELECT COUNT(*) FROM overtime_authorisations WHERE worker_id = ?", (ADMIN,)
    ) == 0, "no decision row may exist after two refused answers"


def test_the_head_admin_answers_an_administrators_crossing(client):
    _plant_open_shift(ADMIN, LONG_SHIFT_HOURS)

    answered = _answer_crossing(client, HEAD_ADMIN, ADMIN, accept=True)
    assert answered.status_code == 200, answered.text[:300]
    assert db_scalar(
        "SELECT decision FROM overtime_authorisations WHERE worker_id = ?", (ADMIN,)
    ) == "authorised"


# ---------------------------------------------------------------------------
# what the boundary is *not*
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("verb", ["in", "out"])
def test_a_worker_is_still_a_standard_admins_to_manage(client, verb):
    """The rule is about *whose* record it is, not about the verb being dangerous.

    A standard admin who cannot stand behind a worker's broken phone is a standard admin who
    has to wake somebody up, and the escalation this closes is a peer's record - not a
    worker's. ``MOALLEM`` rather than ``WORKER`` because the fixture leaves the seeded worker
    clocked in, and a second clock-in is refused on its own terms.
    """
    if verb == "in":
        response = _force_in(client, ADMIN, MOALLEM)
    else:
        _plant_open_shift(MOALLEM, 3.0)
        response = _force_out(client, ADMIN, MOALLEM)
    assert response.status_code == 200, response.text[:300]


def test_a_workers_overtime_is_still_decided_by_any_administrator(client):
    log_id = _a_long_shift_for(client, MOALLEM)

    decided = _decide(client, ADMIN, log_id, approve=True)
    assert decided.status_code == 200, decided.text[:300]
    assert db_scalar("SELECT reviewed_by FROM attendance_logs WHERE id = ?", (log_id,)) == ADMIN
