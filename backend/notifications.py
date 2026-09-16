"""Internal admin notification queue.

This replaces the removed Twilio/WhatsApp integration: alerts are rows, they are
visible on the admin dashboard, they cannot be lost to a third-party outage, and
they cost nothing to send.

``dedupe_key`` is the important column: a UNIQUE constraint plus ``INSERT OR
IGNORE`` gives exactly-once semantics, which is what lets the overtime watcher
run on a timer in several worker processes without spamming the administrator.
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
KIND_STARTUP_DEGRADED = "startup_degraded"
KIND_STARTUP_OVERRIDE = "startup_override"
KIND_SCHEMA_REPAIR = "schema_repair"
#: An automated retention sweep erased data, or could not erase something it was supposed to.
#: The sweep's own deletion of *read* notifications is one of ``retention``'s targets - there
#: is deliberately no pruning helper here, so that every deletion in this application has
#: exactly one implementation, in the module that writes the compliance event describing it.
KIND_RETENTION_SWEEP = "retention_sweep"

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


def unread_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM admin_notifications WHERE read_at IS NULL").fetchone()[0])
    except sqlite3.Error:
        return 0


