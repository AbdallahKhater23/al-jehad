"""Notification queues: one for the deployment's owner, one for the worker it happened to.

This replaces the removed Twilio/WhatsApp integration: alerts are rows, they are
visible in the triage queue (the root tier's ``/developer/notifications``) or in the
worker's own inbox, they cannot be lost to a third-party outage, and they cost nothing
to send.

``dedupe_key`` is the important column: a UNIQUE constraint plus ``INSERT OR
IGNORE`` gives exactly-once semantics, which is what lets the overtime watcher
run on a timer in several worker processes without spamming the administrator.

WHY TWO TABLES
--------------
``admin_notifications`` is *about* workers but it is **for** the root tier - the
triage queue, carrying audit-facing payloads, with a read state that means "an
operator has dealt with this" and an acknowledgement state that means "and here is
why that was the right call". ``worker_notifications`` is the other side of the
same events, addressed to the person they happened to, on its own retention clock
(see ``retention._sweep_worker_notifications``). They are written in the same
transaction by the caller, which is what keeps them from disagreeing: every worker
notification for an event is written where the admin alert for that event is.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

KIND_OVERTIME_EXCEEDED = "overtime_exceeded"
KIND_LATE_ARRIVAL = "late_arrival"
KIND_REVIEW_PENDING = "review_pending"
KIND_LIVENESS_SPOOF = "liveness_spoof"
KIND_LIVENESS_DEGRADED = "liveness_degraded"
KIND_ENROLLMENT_COMPLETED = "enrollment_completed"
KIND_OFFLINE_SYNC = "offline_sync"
KIND_WORKER_NOTE = "worker_note"
KIND_WORKER_NOTE_REOPENED = "worker_note_reopened"
KIND_AUTO_CLOSED = "auto_closed_11h"
#: The 8 h policy close: the system ended an open shift because its paid hours reached
#: the regular limit. Distinct from ``KIND_AUTO_CLOSED`` above, which is the 11 h cutoff
#: that no longer exists - an alert from that one is history, not a current event.
KIND_SHIFT_AUTO_CLOSED = "shift_auto_closed"
#: A quick clock link was used. Sent on a link's *first* use only - the officer who issued
#: it needs to know the link reached a working phone, and a tap after that is an ordinary
#: punch whose record is the link's own list of uses.
KIND_QUICK_LINK = "quick_link"
#: The worker's own alert: their shift has passed the overtime line. Kind for
#: ``worker_notifications`` - there is no admin equivalent, because the administrator gets
#: ``KIND_OVERTIME_EXCEEDED`` for the same event.
KIND_WORKER_OVERTIME_CROSSED = "overtime_crossed"
#: The worker's own notice that the system ended their shift at the paid-day limit, the
#: twin of ``KIND_SHIFT_AUTO_CLOSED`` above. Deliberately *not*
#: ``KIND_WORKER_OVERTIME_CROSSED``: a close records exactly the paid day, so there are no
#: overtime hours for a crossing notice to be about - the two events carry different
#: figures and would contradict each other if one kind said both. Without this one the only
#: way the worker learned the day was over was the next clock-out refusing them.
KIND_WORKER_SHIFT_AUTO_CLOSED = "shift_auto_closed"
#: The worker's half of an administrator *answering* their crossing while the shift is still
#: running: the ceiling that was authorised for it, who authorised it, and what is left
#: unauthorised. Without this the worker only ever heard that a line had been crossed, and the
#: one thing they cannot find out on their own - how far the rest of their night is paid - was
#: the only thing the decision actually said.
KIND_WORKER_OVERTIME_AUTHORISED = "overtime_authorised"
#: ... and the refusal, deliberately a *different* kind rather than the same one carrying
#: different words. "Your extra time is authorised up to 10.5 h" and "no extra time is
#: authorised for this shift" are opposite statements, and a kind that said both could be
#: neither counted nor translated as either - the same reason the auto-close's notice is not the
#: crossing's.
KIND_WORKER_OVERTIME_DECLINED = "overtime_declined"
KIND_STARTUP_DEGRADED = "startup_degraded"
KIND_STARTUP_OVERRIDE = "startup_override"
KIND_SCHEMA_REPAIR = "schema_repair"
#: An automated retention sweep erased data, or could not erase something it was supposed to.
#: The sweep's own deletion of *read* notifications is one of ``retention``'s targets - there
#: is deliberately no pruning helper here, so that every deletion in this application has
#: exactly one implementation, in the module that writes the compliance event describing it.
KIND_RETENTION_SWEEP = "retention_sweep"
#: The channel itself has gone quiet: worker notices have passed the push age window without
#: being delivered. Not a fault of any one punch - it is the *absence* of the rows that would
#: otherwise be the evidence, which is why it is written from a reading of the backlog rather
#: than from an event. Severity warning, not critical: attendance keeps working, every notice
#: is still in the worker's own inbox, and what an operator has lost is the phone ringing.
KIND_WORKER_PUSH_UNDELIVERED = "push_undelivered"
#: The standing detector-coverage report: the run where SCRFD started recovering more of this
#: deployment's own punch frames than YuNet did, or the run where it stopped. A *change* in the
#: detector comparison is the event - a report that says the same thing every day is a report
#: nobody opens - and it is the cue to re-read the embedder migration runbook rather than a
#: punch-level fault.
KIND_COVERAGE_REPORT = "coverage_report"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


def notify(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    body: str,
    severity: str = SEVERITY_INFO,
    worker_id: str | None = None,
    site_name: str | None = None,
    session_id: int | None = None,
    log_id: int | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> bool:
    """Insert a notification. Returns True when a new row was created.

    Never raises: a notification failure must not fail the attendance request
    that triggered it.
    """
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO admin_notifications
                (kind, severity, worker_id, site_name, session_id, log_id, title, body, payload, dedupe_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                severity,
                worker_id,
                site_name,
                session_id,
                log_id,
                title,
                body,
                json.dumps(payload) if payload is not None else None,
                dedupe_key,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        return cursor.rowcount > 0
    except sqlite3.Error:
        return False


def notify_worker(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    kind: str,
    title: str,
    body: str,
    severity: str = SEVERITY_INFO,
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> bool:
    """Write one row into the worker's own inbox. Returns True when it was new.

    Never raises, for the same reason ``notify`` does not: this runs inside the attendance or
    watcher transaction that produced the event, and a notification that cannot be written
    must not fail the punch, the clock-out or the scan that was doing its job.

    Delivery is deliberately *not* attempted here. A push is a network call to a third
    party, and the caller is holding a write transaction and (for a punch) a worker standing
    at a gate. ``push.dispatch_async`` is called after the transaction commits.
    """
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO worker_notifications
                (worker_id, kind, title, body, payload, dedupe_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(worker_id),
                kind,
                title,
                body,
                json.dumps(payload) if payload is not None else None,
                dedupe_key,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        return cursor.rowcount > 0
    except sqlite3.Error:
        return False


def unread_count(conn: sqlite3.Connection) -> int:
    """Unread alerts, over every row in the queue.

    One reader - the root tier - so there is nothing to exclude: an alert that records a
    decision the deployment's owner has to take is unread work like any other, and a count
    narrowed to some subset would say four are waiting while the screen below showed three.
    """
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM admin_notifications WHERE read_at IS NULL").fetchone()[0]
        )
    except sqlite3.Error:
        return 0


def acknowledge(
    conn: sqlite3.Connection,
    notification_id: int,
    *,
    by: str,
    note: str,
    now: datetime | None = None,
) -> bool:
    """Accept an alert, with the reason. Returns whether **this** call is the one that did it.

    One conditional statement rather than read-then-write, and the condition is the whole
    design: ``WHERE acknowledged_at IS NULL`` means two administrators acknowledging the same
    alert at the same moment cannot overwrite each other's reason with the later one. The
    second call sets nothing and returns False, and the endpoint answers it as the conflict it
    is - the audit trail keeps the first acceptance, which is the one that was actually made.

    Acknowledging also *reads* the row (``COALESCE``, so an earlier reader is not rewritten).
    An alert somebody has accepted has by definition been seen, and leaving it in the unread
    count would badge a decision that has already been made.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        "UPDATE admin_notifications SET acknowledged_at = ?, acknowledged_by = ?, "
        "acknowledgement_note = ?, read_at = COALESCE(read_at, ?), read_by = COALESCE(read_by, ?) "
        "WHERE id = ? AND acknowledged_at IS NULL",
        (stamp, str(by), note, stamp, str(by), int(notification_id)),
    )
    return cursor.rowcount > 0


def unacknowledged_count(conn: sqlite3.Connection) -> int:
    """How many alerts on this deployment are still waiting for a human to answer.

    The queue is the root tier's, so this is the whole queue: every alert that records a
    decision is a decision the deployment's owner has to take, and none of them is somebody
    else's outstanding work.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM admin_notifications WHERE acknowledged_at IS NULL"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0


def newest_of_kind(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    """The most recent alert of one kind, acknowledgement and all.

    What readiness reads: an alert kind whose newest row has not been accepted is an open
    question about this deployment, and the newest row is the one that describes the state it
    is in now. An older, acknowledged row must not answer for it.
    """
    try:
        return conn.execute(
            "SELECT * FROM admin_notifications WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (str(kind),),
        ).fetchone()
    except sqlite3.Error:
        return None


def worker_unread_count(conn: sqlite3.Connection, worker_id: str) -> int:
    """Unread rows in one worker's inbox - the number the app badges.

    Scoped by ``worker_id`` here rather than at the call site: this is the one query that
    decides what a worker is told is waiting for them, and a caller that forgot the clause
    would count somebody else's. The route is tested for the same property.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM worker_notifications WHERE worker_id = ? AND read_at IS NULL",
                (str(worker_id),),
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0


