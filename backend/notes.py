"""Worker notes: the written channel between a worker and the administrator.

WHY THIS EXISTS
---------------
Every request a worker can only make by catching somebody on the phone is a request
that quietly never happens. "My password stopped working", "the cement for tower B
never arrived", "Wednesday is missing from my timesheet", "my face is not recognised
any more" - each of them has a person waiting on it, a decision behind it, and no
record that it was ever raised. This module is that record.

WHAT A NOTE IS
--------------
A note is a **thread**, not a form. A one-shot submit box is how a request is sent and
then forgotten; two parties who can both write into the same thread is how one gets
answered. So:

* a worker (or a moallem, or an administrator) opens a note with a category and a body;
* the administrator replies, or asks a question, and the worker answers;
* the note carries a status the two of them move forward
  (``open`` -> ``in_progress`` -> ``resolved`` by the admin, ``closed`` by the worker);
* a worker reply on a note the admin had already resolved puts it back to ``open`` and
  raises a notification, because "I marked it done" and "it is actually done" are
  different claims and only the worker knows which one is true.

WHY PASSWORDS ARE NOT IN HERE
-----------------------------
``password_reset`` is one of the categories, and the notes dashboard offers the admin a
one-tap "set a new password" for exactly those notes - which calls the *existing*
``/admin/users/edit_password`` endpoint and reveals the password once, on screen, in the
credentials flow. The generated password is deliberately **not** written into a reply:
``security.py`` stores passwords as one-way bcrypt hashes precisely so that the database
and every backup of it hold nothing readable, and a note body would be a plaintext
password in exactly those places, readable by anyone who can read the row.

INTERNAL REPLIES
----------------
A message flagged ``internal`` is an admin-side working note ("his account was created
by the old import, check the roster") and is filtered out **server-side** for every
worker-facing query. It is not hidden by the UI, which is the only kind of hidden that
is not hidden at all.

READ STATE
----------
Both unread counters are cleared when the side they belong to opens the thread - a
``GET`` that records a read, deliberately, because the alternative is a client that can
forget to call a second endpoint and a badge that never clears. It is a read receipt,
not a data change; nothing else about the note moves.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import notifications
import textguard
from config import settings
from database import db
from rate_limit import limiter
from security import CurrentUser, admin_only, any_authenticated

router = APIRouter(prefix="/worker/notes", tags=["notes"])
admin_router = APIRouter(prefix="/admin/notes", tags=["notes"])

_TS = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# categories / statuses
# ---------------------------------------------------------------------------
#: A closed set, because "something else" as free text is not a category. Each one is a
#: preset the worker can tap, which is the difference between a request that gets filed
#: correctly and one that says "help".
CATEGORY_PASSWORD_RESET = "password_reset"
CATEGORY_MISSING_ITEM = "missing_item"
CATEGORY_SHIFT_HOURS = "shift_hours"
CATEGORY_ENROLLMENT = "enrollment"
CATEGORY_WORKING_CONDITIONS = "working_conditions"
CATEGORY_OTHER = "other"

CATEGORIES: tuple[str, ...] = (
    CATEGORY_PASSWORD_RESET,
    CATEGORY_MISSING_ITEM,
    CATEGORY_SHIFT_HOURS,
    CATEGORY_ENROLLMENT,
    CATEGORY_WORKING_CONDITIONS,
    CATEGORY_OTHER,
)

#: Human text for the notification title. The console shows the worker's own words; this
#: is only what the administrator reads in the alert queue, before opening anything.
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_PASSWORD_RESET: "Password help",
    CATEGORY_MISSING_ITEM: "Missing item or material",
    CATEGORY_SHIFT_HOURS: "Shift / hours question",
    CATEGORY_ENROLLMENT: "Face registration",
    CATEGORY_WORKING_CONDITIONS: "Working conditions",
    CATEGORY_OTHER: "Other request",
}

STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"

STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_CLOSED)

#: What still needs somebody to do something. A resolved note is waiting on the worker
#: to agree, so it is not counted as work in hand, but it is not finished either.
OPEN_STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_IN_PROGRESS)

PRIORITIES: tuple[str, ...] = ("low", "normal", "high")
DEFAULT_PRIORITY = "normal"

#: An account whose password only a head admin may set - mirrors ``edit_password``.
PROTECTED_ROLES = ("admin", "head_admin")


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class NoteCreate(BaseModel):
    category: str = CATEGORY_OTHER
    subject: str
    body: str
    priority: str = DEFAULT_PRIORITY


class NoteReply(BaseModel):
    body: str
    #: ``internal`` is accepted from an administrator only; a worker's own replies are
    #: always worker-visible. See ``_client_visible``.
    internal: bool = False
    status: str | None = None


class NoteStatusUpdate(BaseModel):
    status: str
    reply: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime(_TS)


def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None,
    entity_id: Any,
    before: Any = None,
    after: Any = None,
    request: Request | None = None,
) -> None:
    """Append a notes event. Never raises - an audit failure must not lose the note."""
    try:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, "
            "before_json, after_json, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor.id if actor else None,
                actor.role if actor else None,
                action,
                "worker_notes",
                str(entity_id),
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                request.client.host if request is not None and request.client else None,
                _now(),
            ),
        )
    except sqlite3.Error:
        pass


def _clean_text(value: str, *, field: str, limit: int) -> str:
    """Normalise and vet a note, a reply or a subject before it is written.

    Every write path in this module funnels through here - the worker's note, the worker's
    reply, the administrator's reply, the message attached to a status change - because a
    note is the one field in this application whose entire purpose is free text written by
    one person and read by another. That is also why it is the *prose* profile rather than
    the identifier allowlist: markup, encoded markup and script URLs are refused, while
    apostrophes, ampersands and semicolons are kept, since "it's the second time & nobody
    came" is a sentence a worker writes and not an attack.

    The escaping that makes those characters safe belongs to the renderer and is present
    (``frontend/worker_modules.js`` and ``admin_modules.js`` pass every note field through
    ``escapeHtml``). What happens here is the other half of that arrangement: a note that
    reaches the database cannot *be* markup, so a reader that forgets to escape - the CSV
    export, an operator's shell, a view nobody has written yet - is not a way in.
    """
    try:
        return textguard.prose(value, field=field, max_length=limit)
    except ValueError as exc:
        raise textguard.http_error(exc) from None


def _validate_category(category: str) -> str:
    value = str(category or CATEGORY_OTHER).strip().lower()
    if value not in CATEGORIES:
        raise HTTPException(
            status_code=400, detail=f"Unknown category. Use one of: {', '.join(CATEGORIES)}."
        )
    return value


def _validate_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value not in STATUSES:
        raise HTTPException(
            status_code=400, detail=f"Unknown status. Use one of: {', '.join(STATUSES)}."
        )
    return value


def _validate_priority(priority: str) -> str:
    value = str(priority or DEFAULT_PRIORITY).strip().lower()
    if value not in PRIORITIES:
        raise HTTPException(
            status_code=400, detail=f"Unknown priority. Use one of: {', '.join(PRIORITIES)}."
        )
    return value


def _fetch_note(conn: sqlite3.Connection, note_id: int, *, worker_id: str | None = None) -> sqlite3.Row:
    """One note, optionally restricted to its owner.

    A worker asking for a colleague's note gets the same 404 as a note that does not
    exist: "not yours" and "not there" must be indistinguishable, or the endpoint
    becomes a way to enumerate which ids are real.
    """
    if worker_id is None:
        row = conn.execute("SELECT * FROM worker_notes WHERE id = ?", (note_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM worker_notes WHERE id = ? AND worker_id = ?", (note_id, worker_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    return row


def _messages(
    conn: sqlite3.Connection, note_id: int, *, include_internal: bool
) -> list[sqlite3.Row]:
    if include_internal:
        return list(
            conn.execute(
                "SELECT * FROM worker_note_messages WHERE note_id = ? ORDER BY id", (note_id,)
            ).fetchall()
        )
    # Filtered in the query rather than after it, so a worker-facing response cannot
    # carry an internal message by accident anywhere downstream of this function.
    return list(
        conn.execute(
            "SELECT * FROM worker_note_messages WHERE note_id = ? AND internal = 0 ORDER BY id",
            (note_id,),
        ).fetchall()
    )


def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "author_id": row["author_id"],
        "author_role": row["author_role"],
        "from_admin": row["author_role"] in ("admin", "head_admin"),
        "internal": bool(row["internal"]),
        "body": row["body"],
        "created_at": row["created_at"],
    }


def _last_message(conn: sqlite3.Connection, note_id: int, *, include_internal: bool) -> dict | None:
    rows = _messages(conn, note_id, include_internal=include_internal)
    return _message_dict(rows[-1]) if rows else None


def _note_dict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_internal: bool,
    worker_name: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(row["id"]),
        "worker_id": row["worker_id"],
        "category": row["category"],
        "category_label": CATEGORY_LABELS.get(row["category"], row["category"]),
        "subject": row["subject"],
        "body": row["body"],
        "status": row["status"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_reply_at": row["last_reply_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
        "closed_at": row["closed_at"],
        "closed_by": row["closed_by"],
        "admin_unread": int(row["admin_unread"] or 0),
        "worker_unread": int(row["worker_unread"] or 0),
        "open": row["status"] in OPEN_STATUSES,
        "last_message": _last_message(conn, int(row["id"]), include_internal=include_internal),
    }
    if worker_name is not None:
        payload["worker_name"] = worker_name
    return payload


def _notify_admins(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    worker: CurrentUser,
    kind: str,
    title_prefix: str,
) -> None:
    """Put a note in the administrator's alert queue.

    Notes have no ``dedupe_key``: two notes from the same worker about the same site are
    two obligations, and collapsing them would hide the second one.
    """
    label = CATEGORY_LABELS.get(row["category"], row["category"])
    notifications.notify(
        conn,
        kind=kind,
        title=f"{title_prefix}: {label}",
        body=f"{worker.name} ({worker.id}): {row['subject']}",
        severity=notifications.SEVERITY_WARNING if row["priority"] == "high" else notifications.SEVERITY_INFO,
        worker_id=worker.id,
        payload={"note_id": int(row["id"]), "category": row["category"], "status": row["status"]},
    )


def _open_note_count(conn: sqlite3.Connection, worker_id: str) -> int:
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    row = conn.execute(
        f"SELECT COUNT(*) FROM worker_notes WHERE worker_id = ? AND status IN ({placeholders})",
        (worker_id, *OPEN_STATUSES),
    ).fetchone()
    return int(row[0] or 0)


def _user_row(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT id, name, role FROM users WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.Error:  # pragma: no cover - only on an unmigrated database
        return None


# ---------------------------------------------------------------------------
# worker endpoints
# ---------------------------------------------------------------------------
@router.get("")
async def list_my_notes(
    status: str | None = None, limit: int = 50, current: CurrentUser = Depends(any_authenticated)
):
    """Your own notes, newest activity first, with the counts the badge needs."""
    limit = max(1, min(int(limit), 200))
    with db() as conn:
        if status:
            wanted = _validate_status(status)
            rows = conn.execute(
                "SELECT * FROM worker_notes WHERE worker_id = ? AND status = ? "
                "ORDER BY COALESCE(last_reply_at, created_at) DESC, id DESC LIMIT ?",
                (current.id, wanted, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM worker_notes WHERE worker_id = ? "
                "ORDER BY COALESCE(last_reply_at, created_at) DESC, id DESC LIMIT ?",
                (current.id, limit),
            ).fetchall()
        unread = conn.execute(
            "SELECT COALESCE(SUM(worker_unread), 0) FROM worker_notes WHERE worker_id = ?",
            (current.id,),
        ).fetchone()[0]
        open_count = _open_note_count(conn, current.id)
        notes = [_note_dict(conn, row, include_internal=False) for row in rows]
    return {
        "notes": notes,
        "open": open_count,
        "unread": int(unread or 0),
        "max_open": int(settings.notes_max_open_per_worker),
        "categories": list(CATEGORIES),
    }


@router.post("")
@limiter.limit(settings.notes_rate_limit)
async def create_note(
    request: Request, payload: NoteCreate, current: CurrentUser = Depends(any_authenticated)
):
    """Open a note. The author is the token, never a request field."""
    category = _validate_category(payload.category)
    priority = _validate_priority(payload.priority)
    subject = _clean_text(payload.subject, field="Subject", limit=settings.notes_max_subject_chars)
    body = _clean_text(payload.body, field="Message", limit=settings.notes_max_body_chars)
    now = _now()

    with db(write=True) as conn:
        # The cap is per worker and counts what is still waiting on somebody. A worker
        # with twenty unresolved requests has a situation to talk about, not a reason to
        # file a twenty-first; a busy site behind one tunnel IP must never hit this.
        if _open_note_count(conn, current.id) >= int(settings.notes_max_open_per_worker):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"You already have {settings.notes_max_open_per_worker} open notes. "
                    "Wait for a reply on those, or close one, before opening another."
                ),
            )
        cursor = conn.execute(
            "INSERT INTO worker_notes (worker_id, category, subject, body, status, priority, "
            "created_at, updated_at, admin_unread, worker_unread) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
            (current.id, category, subject, body, STATUS_OPEN, priority, now, now),
        )
        note_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO worker_note_messages (note_id, author_id, author_role, body, internal, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (note_id, current.id, current.role, body, now),
        )
        row = _fetch_note(conn, note_id)
        _notify_admins(
            conn, row, worker=current, kind=notifications.KIND_WORKER_NOTE, title_prefix="New worker note"
        )
        _audit(
            conn,
            action="worker_note_create",
            actor=current,
            entity_id=note_id,
            after={"category": category, "subject": subject, "priority": priority},
            request=request,
        )
        payload_out = _note_dict(conn, row, include_internal=False)

    return {"status": "success", "message": "Note sent to your administrator.", "note": payload_out}


@router.get("/{note_id}")
async def read_my_note(note_id: int, current: CurrentUser = Depends(any_authenticated)):
    """One note and its thread. Opening it is what clears the worker's unread badge."""
    with db(write=True) as conn:
        row = _fetch_note(conn, note_id, worker_id=current.id)
        conn.execute("UPDATE worker_notes SET worker_unread = 0 WHERE id = ?", (note_id,))
        note = _note_dict(conn, row, include_internal=False)
        note["worker_unread"] = 0
        note["messages"] = [
            _message_dict(message)
            for message in _messages(conn, note_id, include_internal=False)
        ]
    return note


@router.post("/{note_id}/replies")
@limiter.limit(settings.notes_rate_limit)
async def reply_my_note(
    request: Request,
    note_id: int,
    payload: NoteReply,
    current: CurrentUser = Depends(any_authenticated),
):
    """Answer in your own thread. A reply on a finished note reopens it.

    That is the point: an admin marking a note ``resolved`` is not the same as the worker
    agreeing, and the one person who can tell the difference should not have to open a
    new note to say so.
    """
    body = _clean_text(payload.body, field="Message", limit=settings.notes_max_body_chars)
    now = _now()
    with db(write=True) as conn:
        row = _fetch_note(conn, note_id, worker_id=current.id)
        before = {"status": row["status"]}
        reopened = row["status"] in (STATUS_RESOLVED, STATUS_CLOSED)
        new_status = STATUS_OPEN if reopened else row["status"]
        conn.execute(
            "INSERT INTO worker_note_messages (note_id, author_id, author_role, body, internal, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (note_id, current.id, current.role, body, now),
        )
        # ``internal`` is ignored outright for a worker: a message the author cannot see
        # in their own thread is not a message.
        conn.execute(
            "UPDATE worker_notes SET status = ?, updated_at = ?, last_reply_at = ?, "
            "admin_unread = admin_unread + 1, worker_unread = 0, "
            "resolved_at = CASE WHEN ? = 1 THEN NULL ELSE resolved_at END, "
            "resolved_by = CASE WHEN ? = 1 THEN NULL ELSE resolved_by END, "
            "closed_at = CASE WHEN ? = 1 THEN NULL ELSE closed_at END, "
            "closed_by = CASE WHEN ? = 1 THEN NULL ELSE closed_by END "
            "WHERE id = ?",
            (new_status, now, now, int(reopened), int(reopened), int(reopened), int(reopened), note_id),
        )
        refreshed = _fetch_note(conn, note_id)
        if reopened:
            # Reopening is a new obligation on an admin who believed it was done, which
            # is the one worker reply that must not sit unseen in a list.
            _notify_admins(
                conn,
                refreshed,
                worker=current,
                kind=notifications.KIND_WORKER_NOTE_REOPENED,
                title_prefix="Worker note reopened",
            )
        _audit(
            conn,
            action="worker_note_reply",
            actor=current,
            entity_id=note_id,
            before=before,
            after={"status": new_status, "reopened": reopened},
            request=request,
        )
        note = _note_dict(conn, refreshed, include_internal=False)
    return {"status": "success", "reopened": reopened, "note": note}


@router.post("/{note_id}/close")
async def close_my_note(note_id: int, current: CurrentUser = Depends(any_authenticated)):
    """The worker says it is dealt with. Only they can make that call."""
    now = _now()
    with db(write=True) as conn:
        row = _fetch_note(conn, note_id, worker_id=current.id)
        conn.execute(
            "UPDATE worker_notes SET status = ?, updated_at = ?, closed_at = ?, closed_by = ? WHERE id = ?",
            (STATUS_CLOSED, now, now, current.id, note_id),
        )
        _audit(
            conn,
            action="worker_note_close",
            actor=current,
            entity_id=note_id,
            before={"status": row["status"]},
            after={"status": STATUS_CLOSED},
        )
    return {"status": "success", "message": "Note closed."}


# ---------------------------------------------------------------------------
# admin endpoints
# ---------------------------------------------------------------------------
@admin_router.get("")
async def list_notes(
    status: str | None = None,
    category: str | None = None,
    worker_id: str | None = None,
    limit: int = 200,
    current: CurrentUser = Depends(admin_only),
):
    """Every note, with the per-status counts the dashboard filters on."""
    limit = max(1, min(int(limit), 500))
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("n.status = ?")
        params.append(_validate_status(status))
    if category:
        clauses.append("n.category = ?")
        params.append(_validate_category(category))
    if worker_id:
        clauses.append("n.worker_id = ?")
        params.append(str(worker_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(
            "SELECT n.*, u.name AS worker_name, u.role AS worker_role FROM worker_notes n "
            f"LEFT JOIN users u ON u.id = n.worker_id {where} "
            "ORDER BY COALESCE(n.last_reply_at, n.created_at) DESC, n.id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        counts = {name: 0 for name in STATUSES}
        for count_row in conn.execute(
            "SELECT status, COUNT(*) AS total FROM worker_notes GROUP BY status"
        ).fetchall():
            counts[str(count_row["status"])] = int(count_row["total"])
        unread = conn.execute(
            "SELECT COALESCE(SUM(admin_unread), 0) FROM worker_notes"
        ).fetchone()[0]
        notes = []
        for row in rows:
            note = _note_dict(conn, row, include_internal=True, worker_name=row["worker_name"])
            note["worker_role"] = row["worker_role"]
            notes.append(note)
    return {
        "notes": notes,
        "counts": counts,
        "unread": int(unread or 0),
        "open": counts[STATUS_OPEN] + counts[STATUS_IN_PROGRESS],
        "categories": list(CATEGORIES),
    }


@admin_router.get("/{note_id}")
async def read_note(note_id: int, current: CurrentUser = Depends(admin_only)):
    """One note with the whole thread, internal messages included."""
    with db(write=True) as conn:
        row = _fetch_note(conn, note_id)
        conn.execute("UPDATE worker_notes SET admin_unread = 0 WHERE id = ?", (note_id,))
        user = _user_row(conn, row["worker_id"])
        note = _note_dict(
            conn, row, include_internal=True, worker_name=user["name"] if user else None
        )
        note["worker_role"] = user["role"] if user else None
        note["admin_unread"] = 0
        # The console offers a password reset only where the signed-in admin may
        # actually perform it; ``edit_password`` refuses a standard admin touching an
        # administrator's account, and a button that always answers 403 is a trap.
        target_role = user["role"] if user else None
        note["can_reset_password"] = not (
            current.role == "admin" and target_role in PROTECTED_ROLES
        )
        note["messages"] = [
            _message_dict(message) for message in _messages(conn, note_id, include_internal=True)
        ]
    return note


@admin_router.post("/{note_id}/replies")
async def reply_note(
    note_id: int,
    payload: NoteReply,
    current: CurrentUser = Depends(admin_only),
):
    """Answer a note, optionally moving its status, optionally as an internal note."""
    body = _clean_text(payload.body, field="Message", limit=settings.notes_max_body_chars)
    new_status = _validate_status(payload.status) if payload.status else None
    now = _now()
    with db(write=True) as conn:
        row = _fetch_note(conn, note_id)
        internal = 1 if payload.internal else 0
        conn.execute(
            "INSERT INTO worker_note_messages (note_id, author_id, author_role, body, internal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, current.id, current.role, body, internal, now),
        )
        status = new_status or row["status"]
        conn.execute(
            "UPDATE worker_notes SET status = ?, updated_at = ?, last_reply_at = ?, "
            "worker_unread = worker_unread + ?, "
            "resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_at END, "
            "resolved_by = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_by END "
            "WHERE id = ?",
            (status, now, now, 0 if internal else 1, status, now, status, current.id, note_id),
        )
        _audit(
            conn,
            action="worker_note_reply",
            actor=current,
            entity_id=note_id,
            before={"status": row["status"]},
            after={"status": status, "internal": bool(internal)},
        )
        refreshed = _fetch_note(conn, note_id)
        note = _note_dict(conn, refreshed, include_internal=True)
    return {"status": "success", "note": note}


@admin_router.post("/{note_id}/status")
async def set_note_status(
    note_id: int,
    payload: NoteStatusUpdate,
    current: CurrentUser = Depends(admin_only),
):
    """Move a note forward, with an optional reply that says so."""
    status = _validate_status(payload.status)
    reply = (
        _clean_text(payload.reply, field="Message", limit=settings.notes_max_body_chars)
        if payload.reply
        else None
    )
    now = _now()
    with db(write=True) as conn:
        row = _fetch_note(conn, note_id)
        if reply:
            conn.execute(
                "INSERT INTO worker_note_messages (note_id, author_id, author_role, body, internal, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (note_id, current.id, current.role, reply, now),
            )
        conn.execute(
            "UPDATE worker_notes SET status = ?, updated_at = ?, "
            "last_reply_at = CASE WHEN ? = 1 THEN ? ELSE last_reply_at END, "
            "worker_unread = worker_unread + ?, "
            "resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_at END, "
            "resolved_by = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_by END "
            "WHERE id = ?",
            (
                status,
                now,
                int(reply is not None),
                now,
                1 if reply else 0,
                status,
                now,
                status,
                current.id,
                note_id,
            ),
        )
        _audit(
            conn,
            action="worker_note_status",
            actor=current,
            entity_id=note_id,
            before={"status": row["status"]},
            after={"status": status, "replied": bool(reply)},
        )
        refreshed = _fetch_note(conn, note_id)
        note = _note_dict(conn, refreshed, include_internal=True)
    return {"status": "success", "note": note}
