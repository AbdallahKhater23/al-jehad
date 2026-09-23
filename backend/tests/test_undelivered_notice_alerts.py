"""A channel that has gone quiet, said out loud.

Every other operator alert this application writes is the consequence of an event: a punch, a
scan, a refused login, a shift closed at the paid-day limit. A push channel that stops working
produces no such row. What it produces is the **absence** of ``delivered_at`` stamps - a worker
notice that has passed ``PUSH_MAX_AGE_MINUTES`` and will therefore never be pushed, because
past that window nothing is retried. So the fact has to be *measured*, and this file pins the
measurement: the reading itself, the alert it raises, the surfaces an operator can watch
without opening a database, and the two clocks it runs on.

The distinction the module is built around, and the reason it is more than a count, is who can
fix what. A stranded notice belonging to a worker with **no live subscription** is a phone that
never asked to be told (nothing was sent, nothing will be); one belonging to a worker **with a
live subscription** is a push service that refused or failed the send. An operator sent to the
wrong one of those loses an afternoon.

Silence is a first-class outcome here, and the most important test is the one where the alert
must **not** fire: with no VAPID key pair, or with ``PUSH_ENABLED=0``, an undelivered backlog is
the expected state. Alerting on it would be a permanent alarm about a decision somebody made
deliberately, and it would drown the reading that matters - a deployment that *is* trying to
deliver and cannot.

    pytest backend/tests/test_undelivered_notice_alerts.py -q
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta

import pytest
from config import settings
from database import db
from harness import MOALLEM, WORKER, current_db_path

import harness
import notifications
import overtime
import push
import readiness

TS = "%Y-%m-%d %H:%M:%S"

#: A fixed instant, so "past the window" is a property of the data and not of the moment the
#: suite happens to run. The reading takes ``now``; the surfaces that poll (readiness, the
#: watcher) use the real clock and are seeded relative to it instead.
ANCHOR = datetime(2026, 9, 21, 12, 0, 0)

FCM = "https://fcm.googleapis.com/fcm/send/abc123:APA91b"


def _stamp(moment: datetime) -> str:
    return moment.strftime(TS)


def _seed_notice(
    worker_id: str = WORKER,
    *,
    age_minutes: float = 90,
    kind: str = "shift_auto_closed",
    delivered: bool = False,
    attempts: int = 0,
    now: datetime = ANCHOR,
    title: str = "Your shift was closed",
    body: str = "8.00h payable.",
) -> None:
    """Write one worker notice directly.

    Deliberately not through ``notifications.notify_worker``: that helper stamps ``created_at``
    with the wall clock, and the whole subject of this file is a notice whose age is old
    enough to matter.
    """
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_notifications "
            "(worker_id, kind, title, body, payload, created_at, delivered_at, delivery_attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(worker_id),
                kind,
                title,
                body,
                None,
                _stamp(now - timedelta(minutes=age_minutes)),
                _stamp(now) if delivered else None,
                int(attempts),
            ),
        )


def _seed_device(worker_id: str = WORKER, *, revoked_at: str | None = None) -> None:
    """A subscription row: a live device unless ``revoked_at`` is set (a retired one)."""
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_push_subscriptions "
            "(worker_id, endpoint, p256dh, auth, created_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            # One endpoint per worker: a device is a browser, and the column is unique.
            (
                str(worker_id),
                f"{FCM}-{worker_id}",
                "BAbcdefghijklmnop",
                "auth-key-value",
                _stamp(ANCHOR),
                revoked_at,
            ),
        )


def _reading(now: datetime = ANCHOR) -> dict:
    with db() as conn:
        return push.stranded_notices(conn, now=now)


def _alerts() -> list[sqlite3.Row]:
    conn = sqlite3.connect(current_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM admin_notifications WHERE kind = ? ORDER BY id",
            (notifications.KIND_WORKER_PUSH_UNDELIVERED,),
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def push_ready(monkeypatch):
    """A deployment that *is* trying to deliver - the state the alert exists for.

    Every half of the state is *stated* rather than assumed, which is the whole reason this
    fixture exists: whether the developer's checkout has a VAPID key pair in ``.env`` and whether
    ``pywebpush`` happens to be installed are properties of the machine, and a test that reads
    them from the ambient environment is a test that reports the machine rather than the code
    (two of these did exactly that the moment the runbook was followed on this laptop). So the
    key pair is set here, and the late import is stubbed rather than installed - the stub refuses
    to send, because no test in this file may reach a push service (the outbound guard would
    refuse it anyway).
    """

    def _never_push(subscription_info, data, vapid_private_key, vapid_claims):  # pragma: no cover
        raise AssertionError("no test in this file may actually push")

    monkeypatch.setattr(settings, "push_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vapid_public_key", "BPublicKeyForTests", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "private-key-for-tests", raising=False)
    monkeypatch.setattr(push, "_load_webpush", lambda: (_never_push, RuntimeError), raising=True)
    usable, reason = push.transport_available()
    assert usable, f"the transport fixture did not take effect: {reason}"
    return push


# ---------------------------------------------------------------------------
# The reading: what counts as stranded
# ---------------------------------------------------------------------------
def test_a_notice_past_the_window_is_stranded_and_the_clock_gives_up_at_the_window(app_module):
    """The window is the definition, not a threshold near it.

    ``PUSH_MAX_AGE_MINUTES`` is where ``dispatch`` stops waking a phone, so a notice past it
    that is still undelivered will *never* be delivered. Counting anything newer would report a
    backlog that is simply the next send being a moment away.
    """
    _seed_notice(age_minutes=90)
    _seed_notice(age_minutes=40)
    _seed_notice(age_minutes=5, title="Still fresh, still deliverable")

    reading = _reading()
    window = int(settings.push_max_age_minutes)
    assert reading["window_minutes"] == window
    assert reading["notices"] == 2, "the notice inside the window was counted as stranded"
    assert reading["workers"] == 1
    assert reading["oldest"] == _stamp(ANCHOR - timedelta(minutes=90))
    assert reading["age_seconds"] == 90 * 60
    assert reading["age"] == "1h 30m"
    assert reading["kinds"] == {"shift_auto_closed": 2}
    assert reading["checked_at"] == _stamp(ANCHOR)

    # A notice exactly at the cutoff is not stranded: at that stamp the send is still in
    # principle possible, and it is ``dispatch`` that decides the boundary.
    _seed_notice(age_minutes=window)
    assert _reading()["notices"] == 2


def test_the_reading_names_the_half_a_different_person_has_to_fix(app_module):
    """Two stranded notices, two different repairs, one sentence that keeps them apart."""
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=75)                    # registered device: the send failed
    _seed_device(MOALLEM, revoked_at=_stamp(ANCHOR))        # a retired device is no device
    _seed_notice(MOALLEM, age_minutes=200)

    reading = _reading()
    assert reading["notices"] == 2
    assert reading["workers"] == 2
    assert reading["with_device"] == 1
    assert reading["no_device"] == 1

    sentence = push._stranded_report(reading)
    assert "2 worker notification(s) for 2 worker(s)" in sentence
    assert "no live device" in sentence
    assert "push service refused or failed the send" in sentence
    assert f"{reading['window_minutes']}-minute push window" in sentence
    assert "still in the worker's own inbox" in sentence
    assert "worker_notice_backlog" in sentence


def test_a_notice_that_exhausted_its_attempts_is_still_reported(app_module):
    """``delivery_attempts`` at the limit means never retried, not never worth mentioning."""
    _seed_notice(age_minutes=600, attempts=int(settings.push_attempt_limit))
    _seed_notice(age_minutes=120, attempts=0)

    reading = _reading()
    assert reading["notices"] == 2
    assert reading["attempted"] == 1
    sentence = push._stranded_report(reading)
    assert "1 were attempted" in sentence
    assert f"after {int(settings.push_attempt_limit)} attempts a notice is not retried" in sentence


def test_nothing_stranded_reads_as_nothing(app_module):
    reading = _reading()
    assert reading["notices"] == 0
    assert reading["oldest"] == ""
    assert reading["age"] == ""
    assert reading["kinds"] == {}
    assert push._stranded_report(reading).startswith("0 worker notification(s)")


def test_a_notice_the_worker_has_read_is_no_longer_stranded(app_module):
    """``read_at`` is the worker's own answer; an answered question is not an undelivered one.

    This is the drain path a deployment switching push on for the first time needs: every
    notice written before push existed is undelivered forever, and without this exclusion the
    backlog stayed red until retention aged the rows out - 180 days of a warning nothing could
    clear. The inbox is the record either way; what this reading is *for* is a channel that is
    trying and cannot deliver.
    """
    _seed_notice(age_minutes=300)
    # The same notice, but the worker opened the app and read it - undelivered, yet known.
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_notifications "
            "(worker_id, kind, title, body, payload, created_at, delivered_at, delivery_attempts, read_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, NULL, 0, ?)",
            (
                str(WORKER),
                "shift_auto_closed",
                "Read in the inbox",
                "8.00h payable.",
                _stamp(ANCHOR - timedelta(minutes=300)),
                _stamp(ANCHOR - timedelta(minutes=10)),
            ),
        )

    reading = _reading()
    assert reading["notices"] == 1, "a read notice still counted as stranded"
    assert reading["oldest"] == _stamp(ANCHOR - timedelta(minutes=300))
    assert reading["workers"] == 1


def test_the_reading_never_touches_rows_it_should_not(app_module):
    """Delivered notices, and notices newer than the window, are not this alert's business."""
    _seed_notice(age_minutes=300)
    _seed_notice(age_minutes=300, delivered=True, title="Delivered long ago")
    _seed_notice(age_minutes=1)
    assert _reading()["notices"] == 1


def test_the_backlog_is_read_through_the_undelivered_index(app_module):
    """This runs on a timer against a growing table; a scan of it is a bug waiting to be one.

    Migration 14 created ``idx_worker_notifications_undelivered`` for exactly this filter
    (``delivered_at`` then ``created_at``), and the reading must keep using it.
    """
    _seed_notice(age_minutes=90)
    with db() as conn:
        plan = "\n".join(
            str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM worker_notifications "
                "WHERE delivered_at IS NULL AND created_at < ?",
                (_stamp(ANCHOR - timedelta(minutes=15)),),
            ).fetchall()
        )
    assert "idx_worker_notifications_undelivered" in plan, plan
    assert "SCAN worker_notifications" not in plan, plan


# ---------------------------------------------------------------------------
# The alert
# ---------------------------------------------------------------------------
def test_the_operator_is_told_once_with_the_counts_and_no_secrets(push_ready, app_module):
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=90)
    _seed_notice(WORKER, age_minutes=45, kind="shift_auto_closed")
    _seed_notice(MOALLEM, age_minutes=70, kind="overtime_crossed", attempts=1)

    result = push.alert_stranded_notices(now=ANCHOR)
    assert result["checked"] is True
    assert result["alerted"] is True
    assert result["notices"] == 3

    rows = _alerts()
    assert len(rows) == 1
    alert = rows[0]
    assert alert["severity"] == notifications.SEVERITY_WARNING
    assert alert["read_at"] is None, "an alert nobody has to acknowledge is one nobody sees"
    assert alert["title"] == "Worker notifications went undelivered"
    assert alert["worker_id"] is None, "the channel is the subject, not one worker"

    payload = json.loads(alert["payload"])
    assert payload["notices"] == 3 and payload["workers"] == 2
    assert payload["with_device"] == 2 and payload["no_device"] == 1
    assert payload["window_minutes"] == int(settings.push_max_age_minutes)
    assert payload["age_seconds"] == 90 * 60
    assert payload["kinds"] == {"shift_auto_closed": 2, "overtime_crossed": 1}
    for field in ("3 worker notification(s)", "1 have no live device", "2 belong to a worker"):
        assert field in alert["body"], alert["body"]

    # An endpoint is a capability to send to somebody's phone, and an alert row is a row that
    # gets logged, mailed and pasted into issues. The subscription's key material must not be
    # in it - not the URL, not the browser's keys.
    blob = f"{alert['body']}{alert['payload']}"
    assert FCM not in blob and "fcm.googleapis.com" not in blob
    assert "BAbcdefghijklmnop" not in blob and "auth-key-value" not in blob


def test_the_same_hour_is_told_once_and_the_next_hour_again(push_ready, app_module):
    """A backlog that persists is worth re-saying; a backlog said sixty times an hour is not.

    The watcher tick and the dispatch path can both take the reading in the same minute, and
    neither is "the" alert - so the row is deduplicated on the hour rather than guarded by the
    caller.

    Seeded with a registered device, because this is the dedupe of an alert that *should* be
    written: a notice nothing was asked to deliver is not alerted at all (see
    ``test_a_backlog_waiting_for_a_device_is_reported_but_never_alerted``).
    """
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=90)

    assert push.alert_stranded_notices(now=ANCHOR)["alerted"] is True
    second = push.alert_stranded_notices(now=ANCHOR + timedelta(minutes=10))
    assert second["alerted"] is False
    assert second["reason"] == "already told this hour"
    assert second["notices"] == 1, "a suppressed alert still reports the backlog it measured"
    push.alert_stranded_notices(now=ANCHOR + timedelta(minutes=59))
    assert len(_alerts()) == 1

    third = push.alert_stranded_notices(now=ANCHOR + timedelta(hours=1))
    assert third["alerted"] is True
    rows = _alerts()
    assert len(rows) == 2
    assert rows[0]["dedupe_key"] != rows[1]["dedupe_key"]


def test_a_quiet_channel_is_not_reported_when_nothing_has_been_left_behind(push_ready, app_module):
    _seed_notice(age_minutes=2)
    _seed_notice(age_minutes=300, delivered=True)

    result = push.alert_stranded_notices(now=ANCHOR)
    assert result["alerted"] is False
    assert result["reason"] == "nothing was left behind"
    assert _alerts() == []


def test_a_deployment_that_does_not_intend_to_push_is_never_told_about_its_own_inbox(
    monkeypatch, app_module
):
    """The honest default of this repository, and it must stay quiet.

    With no key pair the backlog is expected: the inbox is the record and no phone was ever
    meant to ring. With ``PUSH_ENABLED=0`` the same is true by explicit decision. In both cases
    an alert would be a standing alarm about a choice, not a reading of a fault - and it would
    hide the one case that matters, where the keys are set and nothing arrives.

    "No key pair" is set here rather than hoped for: this checkout may well have one in ``.env``
    (following the runbook puts it there), and the point of the test is the *state*, not the
    laptop's environment file.
    """
    _seed_notice(age_minutes=600)

    monkeypatch.setattr(settings, "vapid_public_key", None, raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", None, raising=False)
    unconfigured = push.alert_stranded_notices(now=ANCHOR)
    assert unconfigured["checked"] is False
    assert unconfigured["alerted"] is False
    assert "no VAPID key pair is configured" in unconfigured["reason"]
    assert _alerts() == []

    monkeypatch.setattr(settings, "vapid_public_key", "BPublicKeyForTests", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "private-key-for-tests", raising=False)
    monkeypatch.setattr(settings, "push_enabled", False, raising=False)
    switched_off = push.alert_stranded_notices(now=ANCHOR)
    assert switched_off["checked"] is False
    assert "PUSH_ENABLED=0" in switched_off["reason"]
    assert _alerts() == []


def test_the_dispatch_path_takes_the_reading_too(monkeypatch, app_module):
    """A channel that goes quiet between watcher passes is reported by the next punch.

    Dispatch is the traffic that keeps happening while nobody is looking at ops, so it is one
    of the two clocks - and it takes the reading on the connection it already holds, which is
    why this runs through the real ``dispatch`` rather than the watcher entry point.
    """
    def _never_push(subscription_info, data, vapid_private_key, vapid_claims):  # pragma: no cover
        raise AssertionError("no test in this file may actually push")

    monkeypatch.setattr(settings, "push_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vapid_public_key", "BPublicKeyForTests", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "private-key-for-tests", raising=False)
    monkeypatch.setattr(push, "_load_webpush", lambda: (_never_push, RuntimeError), raising=True)

    # A device is registered, so the notice is the channel's to deliver and its non-arrival is
    # the finding this path reports.
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=120)

    with db(write=True) as conn:
        summary = push.dispatch(conn, now=ANCHOR, send=_never_push)

    assert summary["considered"] == 0, "a notice past the window is never retried"
    assert summary["delivered"] == 0
    rows = _alerts()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["notices"] == 1


def test_a_dispatch_on_a_deployment_that_cannot_push_stays_silent(monkeypatch, app_module):
    """The same rule as the alert, asserted on the path a punch takes.

    The unusable transport is stated (no key pair), not inherited: a checkout with a pair in
    ``.env`` and ``pywebpush`` installed is a deployment that *can* push, which is a different
    test's subject.
    """
    monkeypatch.setattr(settings, "vapid_public_key", None, raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", None, raising=False)
    _seed_notice(age_minutes=600)
    with db(write=True) as conn:
        summary = push.dispatch(conn, now=ANCHOR)
    assert summary["skipped"], "the transport should have been reported as unusable"
    assert _alerts() == []


# ---------------------------------------------------------------------------
# The surfaces: visible without opening a database
# ---------------------------------------------------------------------------
def test_readiness_reports_a_channel_that_was_asked_and_did_not_deliver(push_ready, app_module):
    """The state the check exists for: a device is registered, and nothing arrives."""
    now = datetime.now()
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=45, now=now)
    _seed_notice(MOALLEM, age_minutes=30, now=now)

    check = readiness._check_worker_notice_backlog({})
    assert check.name == "worker_notice_backlog"
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is False, "a notice the channel was asked to deliver and did not must fail"
    assert "2 worker notification(s)" in check.detail
    assert check.value["notices"] == 2 and check.value["workers"] == 2
    assert check.value["measured"] is True
    assert check.value["channel_failures"] == 1, "only the registered worker's notice is a failure"
    assert check.value["waiting_for_device"] == 1
    assert check.value["no_device"] == 1


def test_a_backlog_waiting_for_a_device_is_reported_but_is_not_a_failure(push_ready, app_module):
    """Undelivered without a failure, and the difference is the whole point.

    A notice written for a worker who has never allowed notifications was never sent anywhere:
    nothing was refused, nothing failed, and no operator action changes it. Counting that as a
    failing channel reports a fault that cannot be reproduced or fixed - and on this deployment
    it did exactly that, because every notice written before notifications were enabled is
    undelivered for that reason and stays so until retention ages it out (180 days). The state
    is still *reported*, in full, so "green" here does not mean "nobody should look".

    It is also not alerted: an alert row an hour claiming a broken channel would be the same
    false claim, written where an operator cannot argue with it.
    """
    now = datetime.now()
    _seed_notice(age_minutes=45, now=now)
    _seed_notice(MOALLEM, age_minutes=30, now=now)

    check = readiness._check_worker_notice_backlog({})
    assert check.ok is True, "a backlog no channel was asked to deliver was reported as a failure"
    assert check.value["notices"] == 2, "the reading still reports what is waiting"
    assert check.value["channel_failures"] == 0
    assert check.value["waiting_for_device"] == 2
    assert "2 notice(s) are waiting" in check.detail, check.detail
    assert "nothing was sent, so nothing was refused" in check.detail, check.detail

    # Read at the same moment the notices were seeded: ``ANCHOR`` is a fixed past instant, and
    # against it these rows are still inside the window and there is nothing to say at all.
    result = push.alert_stranded_notices(now=now)
    assert result["notices"] == 2, "the alert path must see the same backlog readiness reports"
    assert result["alerted"] is False
    assert result["reason"] == "no channel failure: the backlog is waiting for a device"
    assert _alerts() == []


def test_the_public_probe_carries_it_so_a_quiet_channel_is_visible_from_outside(
    push_ready, client, app_module
):
    """The point of the whole feature: an uptime monitor sees a dead channel.

    The readiness probe is the surface that is actually watched, and it is the only one an
    automated check can read. A channel that was asked and did not deliver has to appear there
    as a degraded check - and the anonymous projection must stay a verdict, never the plumbing.
    """
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=45, now=datetime.now())

    response = client.get("/api/v1/readiness")
    body = response.json()
    assert "worker_notice_backlog" in body["degraded_checks"]
    assert body["degraded"] is True
    # Advisory, like every other push verdict: the application *is* serving. What is degraded
    # is the channel, and a probe that answered 503 for it would take a working deployment out
    # of service over a phone that is not ringing. It is reported, and it is reported where an
    # operator's monitor already looks.
    assert body["ok"] is True and response.status_code == 200
    assert body["checks"]["worker_notice_backlog"] == {"ok": False, "tier": "advisory"}, (
        "the public projection must be the verdict, never the detail"
    )
    assert "push window without being delivered" not in response.text, (
        "the check's detail leaked to an anonymous caller"
    )


def test_readiness_measures_nothing_rather_than_guessing_when_push_is_off(push_ready, app_module, monkeypatch):
    """Configured to push, nothing stranded: a pass. Not configured: not measured, and a pass.

    The second half is what keeps this from being a duplicate of ``worker_push_delivery``: a
    deployment that has said out loud that no phone should ring must not be reported as having
    a delivery problem.
    """
    healthy = readiness._check_worker_notice_backlog({})
    assert healthy.ok is True
    assert "no worker notice has been left behind" in healthy.detail

    monkeypatch.setattr(settings, "vapid_private_key", "", raising=False)
    _seed_notice(age_minutes=600, now=datetime.now())
    unmeasured = readiness._check_worker_notice_backlog({})
    assert unmeasured.ok is True
    assert unmeasured.value["measured"] is False
    assert "not measured" in unmeasured.detail


def test_the_check_is_registered_where_an_operator_will_see_it(push_ready, app_module):
    names = [fn({}).name for fn in readiness.CHECKS if getattr(fn, "__name__", "") == "_check_worker_notice_backlog"]
    assert names == ["worker_notice_backlog"], "the check is not in the registry"


# ---------------------------------------------------------------------------
# The clock that keeps running when nobody is punching
# ---------------------------------------------------------------------------
def test_the_watcher_tick_takes_the_reading(monkeypatch, app_module):
    """A quiet channel is exactly the case where nobody is doing anything.

    The punches that would otherwise take the reading stop arriving at the same moment the
    notices do, so the timer has to take it as well - otherwise the alert only fires for
    deployments busy enough not to need it.
    """
    def _never_push(subscription_info, data, vapid_private_key, vapid_claims):  # pragma: no cover
        raise AssertionError("no test in this file may actually push")

    monkeypatch.setattr(settings, "push_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vapid_public_key", "BPublicKeyForTests", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "private-key-for-tests", raising=False)
    monkeypatch.setattr(push, "_load_webpush", lambda: (_never_push, RuntimeError), raising=True)
    # The scans are not the subject; the tick is. Without this the pass is a real overtime
    # sweep, which is a much larger thing to set up and to keep stable.
    monkeypatch.setattr(overtime, "scan_auto_close", lambda: {"closed": 0, "deferred": False})
    monkeypatch.setattr(overtime, "scan_overtime", lambda: {"notified": 0})

    # Registered device: the tick exists to catch a channel that was asked and went quiet, and
    # only that reaches the alert (a notice waiting for a device is reported in readiness).
    _seed_device(WORKER)
    _seed_notice(WORKER, age_minutes=600, now=datetime.now())

    try:
        assert overtime.start_watcher(interval=5, enabled=True) is True
        deadline = time.time() + 20
        while not _alerts() and time.time() < deadline:
            time.sleep(0.1)
    finally:
        overtime.stop_watcher(timeout=10)

    rows = _alerts()
    assert rows, "the watcher tick never reported the backlog"
    assert json.loads(rows[0]["payload"])["notices"] == 1
    assert not overtime.watcher_running()


def test_the_alert_kind_is_declared_beside_the_others(app_module):
    """A kind is an interface: the console, the runbook and the operators all filter on it."""
    assert notifications.KIND_WORKER_PUSH_UNDELIVERED == "push_undelivered"
    assert notifications.KIND_WORKER_PUSH_UNDELIVERED != notifications.KIND_WORKER_SHIFT_AUTO_CLOSED
    assert harness.OUTBOUND, "the outbound guard should be installed for the whole session"
