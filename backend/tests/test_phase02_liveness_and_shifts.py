"""Passive anti-spoofing and the shift/overtime rules.

Two properties are load-bearing here and both are easy to lose in a refactor:

* a presentation attack must be refused **before** the face embedding runs - the
  cheap check has to come first, or the expensive path stays available to an attacker;
* a shift must survive past 8 hours. Anything that closes a session at 8 h (or at
  8.1 h) destroys real work, and nothing at all closes a forgotten shift on a timer:
  it stays open and keeps counting until a human ends it - the worker clocks out, or
  an administrator force-clocks-out.
"""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import harness
from harness import (
    ADMIN,
    DB_PATH,
    MOALLEM,
    PASSWORDS,
    SEEDED_PENDING_LOG_ID,
    bearer,
    clock_in,
    db_scalar,
)

import liveness
import overtime
from config import settings

TS = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeSession:
    """A stand-in for an ``onnxruntime.InferenceSession``.

    The real MiniFASNet model is a ~1.7 MB binary that is not in this repository and
    ``onnxruntime`` is not installed in the venv, so the *policy* around the model is
    what gets tested: which verdict leads to which decision, and whether the expensive
    path is reached.
    """

    def __init__(self, probabilities):
        self._probabilities = np.asarray(probabilities, dtype=np.float32).reshape(1, -1)
        self.runs = 0

    def get_inputs(self):
        return [types.SimpleNamespace(name="input", shape=[1, 3, 80, 80], type="tensor(float)")]

    def get_outputs(self):
        return [types.SimpleNamespace(name="output")]

    def run(self, output_names, feeds):  # noqa: ARG002 - mirrors the ORT signature
        self.runs += 1
        return [self._probabilities]


# Raw model outputs in the canonical [live, print_attack, replay_attack] order. These are
# LOGITS: MiniFASNet's forward pass returns unnormalised scores and the softmax happens at
# inference, which is exactly what ``liveness._probabilities`` implements. Feeding already
# normalised probabilities here would quietly test the wrong numbers (softmax would
# flatten [0.95, 0.03, 0.02] to a 0.56 "genuine" and look like a detector bug).
GENUINE = [5.0, -2.0, -3.0]       # softmax ~ [0.993, 0.001, 0.0004]
PRINTED = [-2.0, 5.0, -3.0]       # a photo of a photo
REPLAYED = [-2.0, -3.0, 5.0]      # a screen being re-displayed
AMBIGUOUS = [0.5, 0.4, 0.3]       # genuine ~0.37: neither accepted nor a confident attack


def _install_session(monkeypatch, probabilities=None, *, available=True):
    session = FakeSession(probabilities if probabilities is not None else GENUINE)
    monkeypatch.setattr(liveness, "get_session", lambda: session if available else None)
    return session


def _spy_on_face_matching(monkeypatch, app_module):
    """Count calls into the expensive path, so 'early rejection' is measurable."""
    calls = []

    def spy(reference_json_path, live_image_data):  # noqa: ARG001
        calls.append(1)
        return {"verified": True, "distance": 0.12, "error": None}

    monkeypatch.setattr(app_module, "compare_faces_sync", spy)
    return calls


def _latest_clock_in(worker_id: str):
    return db_scalar(
        "SELECT liveness_class FROM attendance_logs WHERE worker_id = ? AND action = 'Clock In' "
        "ORDER BY id DESC LIMIT 1",
        (worker_id,),
    )


# ---------------------------------------------------------------------------
# liveness: the gate itself
# ---------------------------------------------------------------------------
def test_genuine_frame_passes_and_reaches_face_matching(monkeypatch, app_module, client, jpeg):
    session = _install_session(monkeypatch, GENUINE)
    calls = _spy_on_face_matching(monkeypatch, app_module)
    monkeypatch.setattr(settings, "liveness_mode", "enforce")

    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))

    assert response.status_code == 200, response.text[:300]
    assert session.runs == 1, "the liveness model must be consulted exactly once per punch"
    assert calls == [1], "a genuine frame must proceed to face matching"
    assert response.json()["liveness"]["verdict"] == "live"
    assert _latest_clock_in(MOALLEM) == "live"


@pytest.mark.parametrize(
    "probabilities,expected_class",
    [(PRINTED, "print_attack"), (REPLAYED, "replay_attack")],
    ids=["printed_photo", "screen_replay"],
)
def test_presentation_attack_is_refused_before_face_matching(
    monkeypatch, app_module, client, jpeg, probabilities, expected_class
):
    """The whole point of the integration: reject *before* paying for the embedding."""
    _install_session(monkeypatch, probabilities)
    calls = _spy_on_face_matching(monkeypatch, app_module)
    monkeypatch.setattr(settings, "liveness_mode", "enforce")

    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))

    assert response.status_code == 422, response.text[:300]
    detail = response.json()["detail"]
    assert detail["error_code"] == liveness.ERR_SPOOF
    assert detail["liveness"]["spoof_class"] == expected_class
    assert calls == [], f"face matching ran for a {expected_class}: early rejection is broken"
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0, (
        "a refused punch must not open a session"
    )
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'liveness_spoof' AND severity = 'critical'"
        )
        == 1
    ), "an administrator must be able to see the attempt"
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'liveness_rejected'") == 1


def test_ambiguous_frame_fails_closed_in_enforce(monkeypatch, app_module, client, jpeg):
    """Between the two thresholds the verdict is 'unknown', and biometrics fail closed."""
    _install_session(monkeypatch, AMBIGUOUS)
    calls = _spy_on_face_matching(monkeypatch, app_module)
    monkeypatch.setattr(settings, "liveness_mode", "enforce")

    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == liveness.ERR_LOW_CONFIDENCE
    assert calls == []


def test_advisory_mode_records_a_spoof_without_blocking_the_worker(
    monkeypatch, app_module, client, jpeg
):
    """Default mode: measure against real traffic before letting a threshold reject people."""
    _install_session(monkeypatch, PRINTED)
    calls = _spy_on_face_matching(monkeypatch, app_module)
    monkeypatch.setattr(settings, "liveness_mode", "advisory")

    response = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))

    assert response.status_code == 200, response.text[:300]
    assert calls == [1], "advisory mode must still run the normal path"
    assert _latest_clock_in(MOALLEM) == "print_attack", "the verdict must be on the record"
    log_id = db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT flag_reason FROM attendance_logs WHERE id = ?", (log_id,))
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = 'liveness_spoof'") == 1


def test_missing_model_fails_closed_in_enforce_and_open_in_advisory(
    monkeypatch, app_module, client, jpeg
):
    _install_session(monkeypatch, available=False)
    _spy_on_face_matching(monkeypatch, app_module)

    monkeypatch.setattr(settings, "liveness_mode", "enforce")
    refused = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert refused.status_code == 422
    assert refused.json()["detail"]["error_code"] == liveness.ERR_UNAVAILABLE, (
        "with no way to prove the frame is genuine, enforce must refuse rather than guess"
    )

    monkeypatch.setattr(settings, "liveness_mode", "advisory")
    allowed = clock_in(client, MOALLEM, image=jpeg, headers=bearer(MOALLEM))
    assert allowed.status_code == 200, "a missing optional model must not stop a site recording attendance"
    assert _latest_clock_in(MOALLEM) == "unavailable"


def test_readiness_reports_enforce_without_a_model_as_fatal(monkeypatch):
    _install_session(monkeypatch, available=False)
    monkeypatch.setattr(settings, "liveness_mode", "enforce")
    monkeypatch.setattr(settings, "liveness_allow_unavailable", False)
    report = liveness.readiness()
    assert report["ready"] is False and report["severity"] == "fatal"

    monkeypatch.setattr(settings, "liveness_allow_unavailable", True)
    assert liveness.readiness()["ready"] is True, "an explicit risk acceptance must be honoured"

    monkeypatch.setattr(settings, "liveness_mode", "advisory")
    assert liveness.readiness()["ready"] is True, "advisory must never block startup"


def test_preprocess_produces_the_shape_the_model_expects(app_module):
    batch = liveness.preprocess(np.zeros((480, 640, 3), dtype=np.uint8))
    assert batch.shape == (1, 3, settings.liveness_input_size, settings.liveness_input_size)
    assert batch.dtype == np.float32 and 0.0 <= float(batch.min()) and float(batch.max()) <= 1.0


# ---------------------------------------------------------------------------
# shift window
# ---------------------------------------------------------------------------
class _FrozenDatetime(datetime):
    """Freezes ``datetime.now`` at 05:00 in the site's timezone."""

    FIXED = datetime(2026, 9, 12, 5, 0, tzinfo=ZoneInfo("Africa/Cairo"))

    @classmethod
    def now(cls, tz=None):
        return cls.FIXED if tz is None else cls.FIXED.astimezone(tz)


def _rules(start: str, end: str) -> dict:
    return {
        "clock_in_window_start": start,
        "clock_in_window_end": end,
        "site_timezone": "Africa/Cairo",
    }


@pytest.mark.parametrize(
    "window,expected",
    [
        (("04:00", "06:30"), True),
        (("04:00", "05:00"), True),   # inclusive end: exactly 05:00 is on time
        (("05:00", "06:30"), True),   # inclusive start
        (("04:00", "04:59"), False),  # one minute before the window closes on 05:00
        (("05:01", "06:30"), False),  # one minute after it opens
        (("06:30", "07:00"), False),
    ],
)
def test_clock_in_window_boundaries(monkeypatch, app_module, window, expected):
    monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)
    assert app_module._within_clock_in_window(_rules(*window)) is expected


def test_default_shift_rules_are_the_documented_ones():
    """The rules an unconfigured install runs on, key for key.

    An equality assert rather than five separate ones: a *new* default is a policy
    change, and it should have to be spelled out here and in the docs rather than
    slipping in as an extra key nobody reviewed.
    """
    from main import DEFAULT_SHIFT_RULES

    assert DEFAULT_SHIFT_RULES == {
        "clock_in_window_start": "04:00",
        "clock_in_window_end": "06:30",
        "regular_hours": 8.0,
        "overtime_notify_hours": 8.1,
        "site_timezone": "Asia/Kuwait",
        # A full day is 8 h paid plus a 30-minute unpaid break, so it runs 8.5 h on
        # site; the break is charged only to a shift long enough to have contained one.
        "break_minutes": 30.0,
        "break_after_hours": 4.0,
        # Off. The shipped alert line (8.1) sits *above* the paid day (8), so a shipped close
        # would stand down (``shift_hours.day_end_rules``) and the switch would read as "on"
        # while nothing was ever closed - which is also what made a fresh volume fail the
        # ``overtime_close_deferred`` advisory on its first boot. Switching it on is one
        # setting, and it is the operator's to make.
        "auto_close_at_regular": 0,
    }
    assert "hard_cutoff_hours" not in DEFAULT_SHIFT_RULES, (
        "the 11 h cap was removed: the only automatic close is the paid-day boundary, "
        "and it is a policy an administrator sets rather than a fixed ceiling"
    )


def test_arrival_outside_the_window_is_logged_and_flagged(client, monkeypatch):
    """A late arrival is allowed and recorded - it is an exception to review, not a refusal."""
    # A window that cannot contain 'now' in the site timezone.
    rules = client.get("/api/v1/admin/shift_rules", headers=bearer(ADMIN)).json()
    cairo_now = datetime.now(ZoneInfo(str(rules.get("site_timezone") or "Africa/Cairo")))
    closed_start = (cairo_now - timedelta(hours=5)).strftime("%H:%M")
    closed_end = (cairo_now - timedelta(hours=4)).strftime("%H:%M")
    updated = client.post(
        "/api/v1/admin/shift_rules",
        headers=bearer(ADMIN),
        json={"clock_in_window_start": closed_start, "clock_in_window_end": closed_end},
    )
    assert updated.status_code == 200, updated.text[:200]

    response = clock_in(client, MOALLEM, headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:300]
    assert "outside the standard window" in response.json()["message"]
    assert db_scalar("SELECT late_flag FROM active_sessions WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT COUNT(*) FROM admin_notifications WHERE kind = 'late_arrival'") == 1


# ---------------------------------------------------------------------------
# overtime
# ---------------------------------------------------------------------------
def _plant_open_session(worker_id: str, hours_ago: float) -> str:
    """Open a shift that started ``hours_ago`` hours in the past.

    Written straight into the database because the API has no way to backdate a
    clock-in - which is itself the correct design, and exactly why aging a session
    for a test has to bypass the API.
    """
    clock_in_time = (datetime.now() - timedelta(hours=hours_ago)).strftime(TS)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, "Downtown Tower A", clock_in_time),
        )
        conn.commit()
    finally:
        conn.close()
    return clock_in_time


def test_the_alert_never_closes_a_shift():
    """``scan_overtime`` observes and reports; closing is ``scan_auto_close``'s job.

    Keeping them separate is what lets an operator turn the automatic close off
    (``auto_close_at_regular``) and still be told about a shift that ran long - which
    is the whole reason the alert exists.
    """
    _plant_open_session(MOALLEM, 9.5)

    summary = overtime.scan_overtime()

    assert summary["notified"] == 1
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 1, (
        "the session must still be open: only the close rule ends a day"
    )
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'", (MOALLEM,)
    ) == 0, "the alert writes no clock-out"


def test_overtime_alert_fires_once_at_the_crossing(monkeypatch):
    # 8.7 h on site is 8.2 h paid once the 30-minute unpaid break is taken, which is
    # past the 8.1 h alert threshold. The threshold is compared against *paid* hours,
    # because that is the number payroll acts on.
    _plant_open_session(MOALLEM, 8.7)

    first = overtime.scan_overtime()
    assert first["notified"] == 1
    # Counted for this worker, not globally: the live database this suite clones may
    # already contain alerts of its own, and a shared count would make this test about
    # somebody else's shift.
    # Counted for this worker, and expected at zero: the crossing is no longer an administrator
    # notification at all - it is an item in the Approvals queue for the open shift, which
    # ``test_overtime_crossing_queue`` pins. What this test is about is the *watcher* firing
    # once, which the stamp and the audit row below still say.
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'overtime_exceeded' AND worker_id = ?",
        (MOALLEM,),
    ) == 0
    assert db_scalar("SELECT overtime_notified_at FROM active_sessions WHERE worker_id = ?", (MOALLEM,)), (
        "the crossing must be stamped so the watcher does not re-alert every tick"
    )
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'overtime_detected' AND entity_id = ?", (MOALLEM,)
    ) == 1

    # The critical case: the timer runs again a minute later, in this process and in any
    # other worker process. The administrator must not be notified twice.
    second = overtime.scan_overtime()
    assert second["notified"] == 0 and second["already_notified"] == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'overtime_exceeded' AND worker_id = ?",
        (MOALLEM,),
    ) == 0, "a second pass raised an alert the first pass already raised"


def test_no_alert_before_the_threshold():
    # 8.5 h on site is exactly the paid day: 8.0 h worked plus the break, and 8.0 is
    # under the 8.1 h alert threshold. A full normal day must not page anybody.
    _plant_open_session(MOALLEM, 8.5)
    summary = overtime.scan_overtime()
    assert summary["notified"] == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'overtime_exceeded' AND worker_id = ?",
        (MOALLEM,),
    ) == 0


def test_clock_out_past_the_threshold_requires_admin_approval(client):
    """Post-shift routing: more than 8.1 *paid* hours cannot reach payroll on its own."""
    _plant_open_session(MOALLEM, 9.5)

    response = clock_in(
        client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM)
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["status"] == "flagged"
    # 9.5 h on site, minus the 30-minute unpaid break, is 9.0 h paid; of that, 1.0 h is
    # past the 8 h regular day and has to be approved.
    assert response.json()["hours"] == pytest.approx(9.0, abs=0.05)
    assert response.json()["break_hours"] == pytest.approx(0.5, abs=0.01)
    assert response.json()["overtime_hours"] == pytest.approx(1.0, abs=0.05)

    log_id = db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime"
    assert db_scalar("SELECT break_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(0.5, abs=0.01)

    pending = client.get("/api/v1/admin/pending_reviews", headers=bearer(ADMIN))
    assert pending.status_code == 200
    assert any(row["id"] == log_id for row in pending.json())

    # Unapproved hours are visible - as a row waiting for a decision - but not approved.
    before = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    waiting = before["rows"][0]
    assert waiting["awaiting_approval"] is True
    assert waiting["hours"] == pytest.approx(9.0, abs=0.05), "the shift holds what was worked"
    assert waiting["break_hours"] == pytest.approx(0.5, abs=0.01)
    # 9.0 h paid is the standard 8 h day plus an hour of overtime. Nobody is reviewing
    # whether the worker turned up - only whether the extra hour was authorised - so the
    # day is credited as soon as the shift is filed and only the hour past it waits. The
    # worker "works 8 h normally"; that part is not in question.
    assert waiting["approved_hours"] == pytest.approx(8.0, abs=0.05), waiting
    assert waiting["awaiting_approval_hours"] == pytest.approx(1.0, abs=0.05), waiting
    assert before["totals"]["approved_hours"] == pytest.approx(8.0, abs=0.05), (
        "the standard day is earned, not conditional on the overtime decision"
    )
    assert before["totals"]["awaiting_approval_hours"] == pytest.approx(1.0, abs=0.05)

    approved = client.post(
        "/api/v1/admin/approve_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "approved_hours": 8.25, "note": "authorised 15 min overtime"},
    )
    assert approved.status_code == 200, approved.text[:200]

    after = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    row = after["rows"][0]
    assert row["awaiting_approval"] is False, "the decision is what took it out of the queue"
    assert row["hours"] == pytest.approx(8.25, abs=0.01), (
        "the approved figure, not the clock: the admin is the authority on what it is worth"
    )
    assert row["recorded_hours"] == pytest.approx(9.0, abs=0.01), "the clock is kept beside it"
    assert after["totals"]["approved_hours"] == pytest.approx(8.25, abs=0.01)
    assert after["totals"]["hours"] == pytest.approx(8.25, abs=0.01)
    assert db_scalar("SELECT reviewed_by FROM attendance_logs WHERE id = ?", (log_id,)) == ADMIN
    assert db_scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'review_approve' AND entity_id = ?", (str(log_id),)) == 1


def test_a_forgotten_shift_ends_at_the_paid_limit_and_is_reported(app_module, client):
    """The day ends at 8 paid hours - and the administrator is told how it ended.

    ``enforce_11h_cutoff`` used to force-close a forgotten shift at exactly 11 h, writing
    a clock-out the worker never made; that capped a 13 h day at 11 and hid the
    difference. It is gone and must not come back by name. What replaces it is not a
    ceiling but the paid-day rule: the shift ends where the policy says a day ends
    (8 h paid, 8.5 h on site), it is marked ``auto_closed_8h``, it is payable, and the
    alert names the time that was still on the clock when the watcher got there.

    The close only ends the day in the arrangement where the crossing is observable before
    it does: the close is switched on *and* the alert line is put below the paid day (see
    ``harness.use_auto_close``), so the pair cooperates - the alert first, the close after.
    The shipped numbers have the close off, so nothing is auto-closed out of the box; that
    is ``test_day_end_precedence.py``'s subject.
    """
    harness.use_auto_close(client)
    _plant_open_session(MOALLEM, 12.0)

    assert not hasattr(app_module, "enforce_11h_cutoff"), (
        "the 11 h force-close was deleted on purpose - do not reintroduce it"
    )

    assert overtime.scan_overtime()["notified"] == 1, "the administrator is told as well"
    closed = overtime.scan_auto_close()
    assert closed["closed"] == 1

    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    log_id = db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "auto_closed_8h"
    assert db_scalar("SELECT hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(8.0, abs=0.01)
    assert db_scalar("SELECT break_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(0.5, abs=0.01)
    assert db_scalar("SELECT source FROM attendance_logs WHERE id = ?", (log_id,)) == "auto_close"
    assert db_scalar(
        "SELECT COUNT(*) FROM admin_notifications WHERE kind = 'shift_auto_closed' AND worker_id = ?",
        (MOALLEM,),
    ) == 1

    # The 8 h it recorded are work and nobody has to sign for them - the difference from the
    # old cutoff, which left the hours for a human to decide about.
    report = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    row = report["rows"][0]
    assert row["hours"] == pytest.approx(8.0, abs=0.01)
    assert row["awaiting_approval"] is False


# ---------------------------------------------------------------------------
# the break, and the administrator's route out of a forgotten shift
# ---------------------------------------------------------------------------
def test_a_force_clock_out_is_priced_by_the_same_policy(client):
    """A shift pays the same whoever closes it: the worker, the system, or an admin."""
    _plant_open_session(MOALLEM, 12.0)

    forced = client.post(
        "/api/v1/admin/force_clock_out", headers=bearer(ADMIN), json={"worker_id": MOALLEM}
    )
    assert forced.status_code == 200, forced.text[:200]
    assert db_scalar("SELECT COUNT(*) FROM active_sessions WHERE worker_id = ?", (MOALLEM,)) == 0
    hours = db_scalar(
        "SELECT hours FROM attendance_logs WHERE worker_id = ? AND status = ?",
        (MOALLEM, "Force Clocked Out by Admin"),
    )
    assert hours == pytest.approx(11.5, abs=0.05), (
        "12 h on site minus the 30-minute unpaid break - and specifically not the 11 h "
        "the old cutoff would have nailed it to"
    )
    assert db_scalar(
        "SELECT break_hours FROM attendance_logs WHERE worker_id = ? AND status = ?",
        (MOALLEM, "Force Clocked Out by Admin"),
    ) == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# refusing a shift: the other half of the approval gate
# ---------------------------------------------------------------------------
#  Until this endpoint existed the console's Reject button posted to
#  ``/admin/approve_review`` and threw its ``action`` argument away, so pressing Reject
#  *approved* the shift at its full recorded hours. A worker was paid overtime their
#  manager had just refused, and the only trace of the refusal was that the card vanished.
#  These tests are written against the money, not against the button: what the timesheet
#  pays afterwards is the thing that was wrong.
def _waiting_overtime_shift(client, hours_on_site: float = 9.5) -> int:
    """A clock-out past the threshold, sitting in the review queue. Returns its log id."""
    _plant_open_session(MOALLEM, hours_on_site)
    response = clock_in(client, MOALLEM, action="Clock Out", headers=bearer(MOALLEM))
    assert response.status_code == 200, response.text[:300]
    log_id = db_scalar("SELECT MAX(id) FROM attendance_logs WHERE worker_id = ?", (MOALLEM,))
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime"
    return int(log_id)


def _timesheet_row(client, log_id: int) -> dict:
    body = client.get(
        f"/api/v1/admin/reports/shifts?worker_id={MOALLEM}", headers=bearer(ADMIN)
    ).json()
    matching = [row for row in body["rows"] if row["log_id"] == log_id]
    assert matching, f"the shift must still be a row in the timesheet: {body['rows']}"
    return matching[0]


def test_rejecting_overtime_keeps_the_standard_day_and_credits_none(client):
    """The decision a manager makes most often, and the one that used to pay the overtime."""
    log_id = _waiting_overtime_shift(client)

    refused = client.post(
        "/api/v1/admin/reject_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "note": "no overtime was authorised for this shift"},
    )
    assert refused.status_code == 200, refused.text[:300]
    body = refused.json()
    assert body["overtime_hours"] == 0, "a refusal must not credit the hours it refused"
    assert body["approved_hours"] == pytest.approx(8.0, abs=0.01), (
        "the standard paid day is still owed - the refusal is of the extra hours, not the work"
    )

    # The row agrees with the response.
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "overtime_rejected"
    assert db_scalar("SELECT approved_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(8.0, abs=0.01)
    assert db_scalar("SELECT overtime_hours FROM attendance_logs WHERE id = ?", (log_id,)) == 0
    assert db_scalar("SELECT reviewed_by FROM attendance_logs WHERE id = ?", (log_id,)) == ADMIN

    # ... and so does the money. This is the assertion the old Reject button failed:
    # it would have left 9.0 h payable and 1.0 h of overtime standing.
    row = _timesheet_row(client, log_id)
    assert row["awaiting_approval"] is False, "the refusal is a decision, so it leaves the queue"
    assert row["hours"] == pytest.approx(8.0, abs=0.01), row
    assert row["recorded_hours"] == pytest.approx(9.0, abs=0.01), (
        "the clock is kept beside the decision: 8 h paid out of 9 h worked"
    )


def test_the_reason_for_a_refusal_is_recorded(client):
    """"Why was my overtime refused" has to be answerable next month."""
    log_id = _waiting_overtime_shift(client)
    reason = "the foreman confirmed the site closed at 16:00"

    assert client.post(
        "/api/v1/admin/reject_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "note": reason},
    ).status_code == 200

    assert db_scalar("SELECT flag_reason FROM attendance_logs WHERE id = ?", (log_id,)) == reason
    # The audit row is append-only, and carries the actor and the before-image the update
    # overwrote - so the arrival or liveness flag that was in ``flag_reason`` is still
    # readable, and so is who made the decision and when.
    assert db_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'review_reject' AND entity_id = ?",
        (str(log_id),),
    ) == 1
    audit = client.get("/api/v1/admin/audit_log", headers=bearer(ADMIN)).json()
    entries = [row for row in audit if row["action"] == "review_reject" and str(row["entity_id"]) == str(log_id)]
    assert entries, "the audit log is where the decision is kept"
    assert entries[0]["actor_id"] == ADMIN
    assert reason in str(entries[0]["after_json"]), entries[0]["after_json"]


def test_a_refusal_without_a_reason_is_refused(client):
    """A refusal that reduces somebody's pay and explains nothing is not a decision."""
    log_id = _waiting_overtime_shift(client)

    for payload in ({"log_id": log_id}, {"log_id": log_id, "note": ""}):
        response = client.post("/api/v1/admin/reject_review", headers=bearer(ADMIN), json=payload)
        assert response.status_code == 422, response.text[:200]

    # ... and nothing was decided by the attempts.
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime"
    assert db_scalar("SELECT approved_hours FROM attendance_logs WHERE id = ?", (log_id,)) is None


def test_rejecting_an_unconfirmed_punch_records_it_as_not_worked(client):
    """The other kind of review: a clock-in nobody could confirm was work at all.

    Rejecting that one is the opposite of rejecting overtime - nothing is payable, because
    the decision is that the work did not happen. Two meanings, two codes; the console
    shows one button for both, which is exactly why they must not share an outcome.
    """
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (SEEDED_PENDING_LOG_ID,)) == "pending_review"

    response = client.post(
        "/api/v1/admin/reject_review",
        headers=bearer(ADMIN),
        json={"log_id": SEEDED_PENDING_LOG_ID, "note": "outside every site, and the photo was unclear"},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["approved_hours"] == 0
    assert response.json()["status_code"] == "rejected"
    assert db_scalar("SELECT approved_hours FROM attendance_logs WHERE id = ?", (SEEDED_PENDING_LOG_ID,)) == 0

    # It leaves the queue rather than sitting there forever.
    pending = client.get("/api/v1/admin/pending_reviews", headers=bearer(ADMIN)).json()
    assert all(row["id"] != SEEDED_PENDING_LOG_ID for row in pending)


def test_a_settled_shift_cannot_be_refused_afterwards(client):
    """Idempotence: a second answer to a question already answered is not a decision."""
    log_id = _waiting_overtime_shift(client)
    first = client.post(
        "/api/v1/admin/reject_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "note": "refused"},
    )
    assert first.status_code == 200, first.text[:200]

    again = client.post(
        "/api/v1/admin/reject_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "note": "refused twice"},
    )
    assert again.status_code == 400, again.text[:200]
    assert db_scalar("SELECT flag_reason FROM attendance_logs WHERE id = ?", (log_id,)) == "refused", (
        "the second attempt must not rewrite the recorded reason"
    )


def test_approving_part_of_a_shift_credits_only_the_authorised_overtime(client):
    """The same arithmetic as a refusal, read the other way: what is recorded is what was

    authorised. A row carrying 8.25 approved hours beside the 1.0 h the clock recorded used
    to contradict itself, and the timesheet repeated the contradiction.
    """
    log_id = _waiting_overtime_shift(client)

    approved = client.post(
        "/api/v1/admin/approve_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "approved_hours": 8.25, "note": "authorised 15 min"},
    )
    assert approved.status_code == 200, approved.text[:200]
    assert db_scalar("SELECT approved_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(8.25, abs=0.01)
    assert db_scalar("SELECT overtime_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(0.25, abs=0.01), (
        "15 minutes authorised out of the 1 h worked - not the hour the clock recorded"
    )


def test_the_whole_overtime_hour_can_still_be_approved(client):
    """The refusal exists so that approving is a choice - and approving everything still works."""
    log_id = _waiting_overtime_shift(client)

    approved = client.post(
        "/api/v1/admin/approve_review",
        headers=bearer(ADMIN),
        json={"log_id": log_id, "approved_hours": 9.0, "note": "the extra hour was authorised"},
    )
    assert approved.status_code == 200, approved.text[:200]
    assert db_scalar("SELECT overtime_hours FROM attendance_logs WHERE id = ?", (log_id,)) == pytest.approx(1.0, abs=0.01)
    row = _timesheet_row(client, log_id)
    assert row["hours"] == pytest.approx(9.0, abs=0.01)


def test_an_anonymous_caller_cannot_refuse_a_shift(client):
    """The new route is an admin decision like the one beside it, and is guarded the same way."""
    log_id = _waiting_overtime_shift(client)
    assert client.post(
        "/api/v1/admin/reject_review", json={"log_id": log_id, "note": "not me"}
    ).status_code == 401
    assert client.post(
        "/api/v1/admin/reject_review",
        headers=bearer(MOALLEM),
        json={"log_id": log_id, "note": "not me"},
    ).status_code == 403
    assert db_scalar("SELECT status_code FROM attendance_logs WHERE id = ?", (log_id,)) == "pending_overtime"
