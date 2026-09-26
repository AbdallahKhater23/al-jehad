"""Quick clock links: one tap, one worker, no password.

THE FEATURE
-----------
An administrator names a worker and issues a link. The worker opens it on their phone and
taps once: they are clocked in. The next tap clocks them out. No id, no password, no login
screen - the link *is* the credential, and it belongs to one person, so a punch made with
it lands on that person and on nobody else. The console has a screen that shows every link,
the worker it belongs to, and every punch it has produced, with the photo that was taken.

WHAT THAT COSTS, AND HOW IT IS BOUNDED
--------------------------------------
A credential that clocks somebody in without proving who is holding the phone is by
construction weaker than a password plus a face match. The design's job is to make the
weakening explicit and bounded rather than pretend it away:

* the link **expires** (``QUICK_LINK_TTL_HOURS``, 30 days by default), can be **revoked**,
  and can be capped at N uses; the token is stored as a SHA-256 hash, so a database leak
  hands nobody a working clock-in - the same rule ``enrollment_invites`` follows;
* it is **tied to one worker**, and the punch endpoint accepts no ``worker_id`` at all, so
  a link can never clock in a person the administrator did not name;
* the phone must still be **inside a construction site geofence**, so a link forwarded to
  somebody sitting at home produces no punch;
* a **selfie is still required** on every tap. It is *detected*, not *matched*: the face is
  counted to prove a face was there, the photo is stored, and a human looks at it
  afterwards. Comparing it against the worker's reference would be a lie dressed as a
  control - a link that somebody else is holding is not proof of who is holding it - so the
  honest output of the biometric stage here is "a face was present", and the photo is what
  an administrator reviews;
* **every use is a row**: which link, which worker, in or out, the site, the coordinates,
  the IP, the user agent, and the photo, all listed on the link's own screen.

WHY A QUICK-LINK PUNCH IS NOT ROUTED TO THE APPROVAL QUEUE
----------------------------------------------------------
The attendance row it writes is ``approved``, carrying ``source='quick_link'`` and a
``flag_reason`` naming the link. Sending it to ``pending_review`` would sound stricter and
be worse: the password clock-out path refuses to close a shift while *any* row of the
worker's is awaiting review, so one quick-link clock-in would leave that worker unable to
clock out at all until an administrator cleared it, and the shift would instead end in an
automatic close at the paid limit - hours the worker did work, recorded by a system that
had been told the punch was untrustworthy and could not tell anyone. The review surface for
these punches is the link itself: the photo sits on screen next to the punch it belongs to,
and an administrator who dislikes what they see has the same remedies as for any other
shift (force clock-out, adjust the approved hours, deactivate the account, revoke the link).

NOTIFICATIONS
-------------
The first use of a link notifies the administrator, so the person who issued it learns that
it reached a working phone. After that a tap is an ordinary punch and alerts nobody: an
alert that fires on every tap is an alert nobody reads, and it would drown the ones that
matter. Liveness that does not confirm a genuine face, and overtime on a clock-out, alert
exactly as they do on the password path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

import face_detector
import face_engine
import liveness
import notifications
import overtime
import shift_hours
import shift_windows
import textguard
import uploads
from config import settings
from database import db
from rate_limit import limiter
from security import CurrentUser, admin_only

log = logging.getLogger("attendance.quick_links")

#: Administrator surface (``/api/v1/admin/quick_links``) and the public one
#: (``/api/v1/q/<token>``). Separate routers because they are separate audiences: one is
#: token-authenticated, the other is authenticated by the link in the path alone.
admin_router = APIRouter(prefix="/admin", tags=["admin"])
public_router = APIRouter(prefix="/q", tags=["quick links"])

_TS = "%Y-%m-%d %H:%M:%S"

#: Where the punch selfies are kept, from the settings so that a child process inherits the
#: answer in ``QUICK_LINK_PHOTOS_DIR`` rather than writing into the checkout - the same
#: convention ``main.WORKER_PHOTOS_DIR`` uses. Repointable from a test either way, by assigning
#: this attribute.
PHOTOS_DIR = str(settings.quick_link_photos_dir)


def photos_dir() -> str:
    """The photo directory, created on first use."""
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    return PHOTOS_DIR


def punch_photo_name() -> str:
    """The name of one punch selfie: random, and nothing to do with the punch's row.

    These were named ``{link row id}_{timestamp}_{random}`` - which named a worker's
    face from a number anybody can count upwards. The photo is looked up by *use id*
    over the API, never by filename, so the name only ever has to be unique.
    """
    return f"{secrets.token_hex(16)}.jpg"


def _is_legacy_photo_name(name: str) -> bool:
    """Whether ``name`` is the old ``{row id}_{YYYYmmdd_HHMMSS}_{8 hex}.jpg`` shape."""
    stem = name[:-4] if name.endswith(".jpg") else ""
    parts = stem.split("_")
    if len(parts) != 4:
        return False
    row_id, day, clock, random_bits = parts
    return (
        row_id.isdigit()
        and len(day) == 8
        and day.isdigit()
        and len(clock) == 6
        and clock.isdigit()
        and len(random_bits) == 8
        and all(character in "0123456789abcdef" for character in random_bits)
    )


def adopt_legacy_photo_names() -> dict[str, Any]:
    """Rename the punch selfies that are still filed under their link's row id.

    The file moves to a random name and the row is pointed at it in the same pass, so
    the console's photo URL keeps working: the browser asks for
    ``/admin/quick_link_photo/<use id>`` and the path is server-side plumbing it never
    sees (see ``quick_link_photo``).

    Idempotent - a name that is already random is skipped - and kept out of migration 11
    on purpose, next to ``biometrics.adopt_legacy_files``: migrations are replayed
    against in-memory databases by the drift guard, where a rename has nothing to work
    with. Called from ``main.init_db``.
    """
    summary: dict[str, Any] = {"renamed": [], "missing": [], "failed": []}
    directory = os.path.abspath(photos_dir())
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, photo_path FROM quick_link_uses "
                "WHERE photo_path IS NOT NULL AND photo_path <> ''"
            ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - database not there yet
        return {**summary, "error": f"{type(exc).__name__}: {exc}"}

    for row in rows:
        stored = os.path.basename(str(row["photo_path"]))
        if not _is_legacy_photo_name(stored):
            continue
        source = os.path.join(directory, stored)
        if not os.path.exists(source):
            # The row points at a photo that is already gone. Its URL answers 404 either
            # way; clearing the row is an operator's decision, not a rename's.
            summary["missing"].append(stored)
            continue
        target_name = punch_photo_name()
        try:
            os.replace(source, os.path.join(directory, target_name))
        except OSError as exc:
            summary["failed"].append({"file": stored, "error": str(exc)})
            continue
        with db(write=True) as conn:
            conn.execute(
                "UPDATE quick_link_uses SET photo_path = ? WHERE id = ?",
                (target_name, row["id"]),
            )
        summary["renamed"].append(target_name)
    return summary


def _discard_photo(name: str) -> None:
    """Delete a selfie whose punch did not happen.

    A refusal that leaves the photo behind would be an unattributed face on disk: the punch
    it belongs to does not exist, so nothing on any screen could ever explain whose it was.
    """
    path = os.path.join(photos_dir(), os.path.basename(name))
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:  # pragma: no cover - best effort
        log.warning("quick link selfie %s could not be removed after a refused punch", name)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_link_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in (_TS, f"{_TS}.%f"):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def _link_state(row: sqlite3.Row, now: datetime) -> tuple[bool, str]:
    """``(usable, state)`` for a link row.

    ``max_uses`` 0 means "as many taps as the shift needs until it expires", so the cap is
    only consulted when there is one.
    """
    if row["revoked_at"]:
        return False, "revoked"
    expires = _parse_link_ts(row["expires_at"])
    if expires is not None and expires < now:
        return False, "expired"
    cap = int(row["max_uses"] or 0)
    if cap and int(row["uses"] or 0) >= cap:
        return False, "used_up"
    return True, "active"


def _refuse_link(state: str) -> HTTPException:
    messages = {
        "revoked": "This clock link was revoked by an administrator.",
        "expired": "This clock link has expired. Ask your administrator for a new one.",
        "used_up": "This clock link has reached its allowed number of uses.",
        "unknown": "This clock link is not valid.",
    }
    return HTTPException(
        status_code=410,
        detail={"error_code": f"link_{state}", "message": messages.get(state, messages["unknown"])},
    )


def _load_link(token: str) -> tuple[sqlite3.Row, bool, str]:
    """The link row for a token, plus whether it may still be used and why not."""
    with db() as conn:
        row = conn.execute("SELECT * FROM quick_links WHERE token_hash = ?", (_hash_token(token),)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "link_unknown", "message": "This clock link is not valid."},
        )
    usable, state = _link_state(row, datetime.now())
    return row, usable, state


def detect_faces_sync(image_array) -> list[dict]:
    """Detect faces in a frame **without** embedding or comparing them.

    This is the whole of the biometric check on a quick-link punch: how many faces are in
    the photo. Detection is the cheap half of the model API - no VGG-Face embedding - which
    is why it is used here rather than a full verification.

    The model loads inside ``face_engine``, so importing this module still does not pull
    TensorFlow into a process that only wants the admin routes, and the test harness's
    stub is what runs, exactly as it is for ``main.compare_faces_sync``. The call is direct
    because this function *is* the job: the endpoint submits it to the pool (see
    ``face_engine`` for why a job must not submit to its own engine).
    """
    try:
        faces = face_engine.ENGINE.detect_direct(image_array)
    except ValueError:
        return []
    return list(faces or [])


def _confidence(face: dict) -> float:
    """One detection's confidence, above a low floor, so detector noise is not a face.

    ``extract_faces`` reports ``confidence`` while ``represent`` reports
    ``face_confidence``; both are read so this keeps working if the call ever moves to the
    other half of the API. An entry that carries neither is taken at face value rather than
    dropped: unable to *lower* the answer, this must not quietly discard a face the detector
    did report.
    """
    try:
        return float(face.get("confidence", face.get("face_confidence", 1.0)))
    except (AttributeError, TypeError, ValueError):
        return 1.0


def _subject_report(faces: list[dict]) -> face_detector.SubjectReport:
    """The people in a quick-link selfie, counted by the punch path's own rule.

    The confidence floor is this path's; the count is ``face_detector.subject_detections``,
    which is what the password punch uses. Both endpoints used to count *detections*, so a
    detector that returns two boxes over one face - or a face-shaped speck on a nearby poster -
    refused the worker with "more than one face is in the photo": a sentence about a person who
    was not in the frame, and one the worker at the gate cannot act on. Two detections that are
    each subject-sized are still two people, and this still refuses them.
    """
    confident = [face for face in faces if _confidence(face) >= 0.5]
    return face_detector.subject_detections(confident)


def _link_url(request: Request, token: str, override: str | None = None) -> str:
    """Where this server is reachable from, with one implementation for both link kinds.

    ``enrollment._public_base_url`` already encodes that policy (an explicit override, then
    ``ENROLLMENT_BASE_URL``, then the request host, which is what makes a link work behind
    the proxy that forwarded it). A second copy here would be a second answer to "what is
    this server's address", and the two would drift.
    """
    import enrollment

    return f"{enrollment._public_base_url(request, override)}q/{token}"


def _worker_row(conn: sqlite3.Connection, worker_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, role, status FROM users WHERE id = ?", (worker_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Worker ID not found.")
    return row


def _require_active_worker(row: sqlite3.Row) -> None:
    """A deactivated account's link must stop working with it.

    Deactivating an account revokes its tokens, its device keys and its enrollment links;
    a clock link that survived would be the one credential still punching for somebody the
    administrator has switched off.
    """
    if str(row["status"] or "active").strip().lower() != "active":
        raise HTTPException(
            status_code=410,
            detail={
                "error_code": "link_account_inactive",
                "message": "The account this link belongs to is deactivated. Ask an administrator.",
            },
        )


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class QuickLinkCreate(BaseModel):
    worker_id: str
    ttl_hours: int | None = None
    max_uses: int | None = None
    #: For the administrator's own records ("the man on the third tower"). The link's note
    #: is copied onto the punch the link produces and appears in the link's use list, so it
    #: is prose rather than an identifier - and, being prose, it carries no markup.
    note: str | None = None
    base_url: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(
            value, field="Note", max_length=textguard.MAX_LABEL, allow_empty=True
        )


# ---------------------------------------------------------------------------
# admin: issue, list, revoke, and read back what a link did
# ---------------------------------------------------------------------------
@admin_router.post("/quick_links")
async def create_quick_link(
    request: Request, payload: QuickLinkCreate, current: CurrentUser = Depends(admin_only)
):
    """Issue a clock link for one worker.

    The token is returned exactly once, because only its hash is stored: an administrator
    who loses it revokes the link and issues another. That is the same trade
    ``/admin/enrollment/invites`` makes, and for the same reason - a link that can be read
    back out of the database is a link an attacker can read out of the database.
    """
    worker_id = str(payload.worker_id).strip()
    with db() as conn:
        worker = _worker_row(conn, worker_id)
    _require_active_worker(worker)
    if str(worker["role"]) in {"admin", "head_admin"}:
        # A link is a punch without a credential, and an administrative account is not a
        # punching account: its hours are not what the report pays out. Refusing here keeps
        # the rule in one place instead of relying on nobody pressing the button.
        raise HTTPException(
            status_code=400,
            detail=(
                f"User {worker_id} is an administrator. Clock links are issued to workers and "
                "lead workers."
            ),
        )

    # ``is not None`` rather than truthiness: a caller asking for ``ttl_hours=0`` is asking
    # for something invalid, and silently substituting the default would grant a month-long
    # credential in answer to a request for one that expires immediately.
    ttl = int(payload.ttl_hours) if payload.ttl_hours is not None else int(settings.quick_link_ttl_hours)
    if ttl <= 0 or ttl > 24 * 365:
        raise HTTPException(status_code=400, detail="ttl_hours must be between 1 and 8760.")

    max_uses = int(payload.max_uses if payload.max_uses is not None else settings.quick_link_max_uses)
    if max_uses < 0 or max_uses > 100:
        raise HTTPException(status_code=400, detail="max_uses must be between 0 and 100 (0 = unlimited).")

    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = (now + timedelta(hours=ttl)).strftime(_TS)
    with db(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO quick_links (token_hash, worker_id, created_by, created_at, expires_at, "
            "max_uses, uses, note) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (_hash_token(token), worker_id, current.id, now.strftime(_TS), expires_at, max_uses, payload.note),
        )
        link_id = int(cursor.lastrowid or 0)
        import main

        main._audit(
            conn,
            action="quick_link_create",
            actor=current,
            entity="quick_links",
            entity_id=str(link_id),
            after={
                "worker_id": worker_id,
                "expires_at": expires_at,
                "max_uses": max_uses,
                "note": payload.note,
            },
            request=request,
        )

    url = _link_url(request, token, payload.base_url)
    # The QR is a convenience, not the credential: an admin who wants the link on a sheet at
    # the gate gets one, and an installation without the optional ``qrcode`` extra gets
    # ``None`` and the URL. Same trade ``/admin/enrollment/invites`` makes.
    import enrollment

    return {
        "status": "success",
        "link_id": link_id,
        "worker_id": worker_id,
        "worker_name": worker["name"],
        "url": url,
        "token": token,
        "qr_png_data_uri": enrollment._qr_data_uri(url),
        "expires_at": expires_at,
        "max_uses": max_uses,
        "note": (
            "Send this link to the worker once. Only its hash is stored, so it cannot be "
            "shown again - revoke it and issue another if it is lost."
        ),
    }


@admin_router.get("/quick_links")
async def list_quick_links(
    active: int = 0, worker_id: str | None = None, limit: int = 200, current: CurrentUser = Depends(admin_only)
):
    """Every link, with the worker it belongs to and whether that worker is on shift now.

    ``active=1`` filters to the links that would still work, which is the view an
    administrator wants when deciding whether somebody still has a working credential.
    """
    limit = max(1, min(int(limit), 1000))
    now = datetime.now()
    sql = (
        "SELECT q.id, q.worker_id, u.name AS worker_name, u.role AS worker_role, "
        "u.status AS worker_status, q.created_by, q.created_at, q.expires_at, q.max_uses, "
        "q.uses, q.revoked_at, q.last_used_at, q.last_used_ip, q.note "
        "FROM quick_links q LEFT JOIN users u ON q.worker_id = u.id"
    )
    params: list[Any] = []
    if worker_id:
        sql += " WHERE q.worker_id = ?"
        params.append(str(worker_id))
    sql += " ORDER BY q.id DESC LIMIT ?"
    params.append(limit)

    with db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        sessions = {
            str(row["worker_id"]): row
            for row in conn.execute(
                "SELECT worker_id, site_name, clock_in_time FROM active_sessions"
            ).fetchall()
        }

    output = []
    for row in rows:
        usable, state = _link_state(row, now)
        if active and not usable:
            continue
        item = dict(row)
        item["state"] = state
        item["usable"] = usable
        item["worker_active"] = str(row["worker_status"] or "active").strip().lower() == "active"
        item["remaining_uses"] = None if not int(row["max_uses"] or 0) else max(
            0, int(row["max_uses"]) - int(row["uses"] or 0)
        )
        session = sessions.get(str(row["worker_id"]))
        item["clocked_in"] = session is not None
        item["clock_in_time"] = session["clock_in_time"] if session is not None else None
        item["open_shift_site"] = session["site_name"] if session is not None else None
        output.append(item)
    return output


@admin_router.post("/quick_links/{link_id}/revoke")
async def revoke_quick_link(request: Request, link_id: int, current: CurrentUser = Depends(admin_only)):
    """Stop a link from working. Idempotent: the first revocation is the one recorded."""
    now_str = datetime.now().strftime(_TS)
    with db(write=True) as conn:
        row = conn.execute("SELECT * FROM quick_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Clock link not found.")
        if row["revoked_at"]:
            return {
                "status": "success",
                "link_id": link_id,
                "revoked_at": row["revoked_at"],
                "message": "This clock link was already revoked.",
            }
        conn.execute("UPDATE quick_links SET revoked_at = ? WHERE id = ?", (now_str, link_id))
        import main

        main._audit(
            conn,
            action="quick_link_revoke",
            actor=current,
            entity="quick_links",
            entity_id=str(link_id),
            before={"worker_id": row["worker_id"], "revoked_at": None},
            after={"worker_id": row["worker_id"], "revoked_at": now_str},
            request=request,
        )
    return {"status": "success", "link_id": link_id, "revoked_at": now_str, "message": "Clock link revoked."}


@admin_router.get("/quick_links/{link_id}/uses")
async def list_quick_link_uses(link_id: int, current: CurrentUser = Depends(admin_only)):
    """Every punch a link produced: who, when, where from, and the photo that was taken.

    This is the screen the feature exists for. A link clocks somebody in without proving who
    is holding the phone, so the answer to "was that really them?" has to be a photograph a
    human can look at, next to the punch it belongs to.
    """
    with db() as conn:
        link = conn.execute(
            "SELECT q.id, q.worker_id, u.name AS worker_name FROM quick_links q "
            "LEFT JOIN users u ON q.worker_id = u.id WHERE q.id = ?",
            (link_id,),
        ).fetchone()
        if link is None:
            raise HTTPException(status_code=404, detail="Clock link not found.")
        rows = conn.execute(
            "SELECT s.id, s.worker_id, s.action, s.site_name, s.lat, s.lon, s.accuracy, s.log_id, "
            "s.photo_path, s.ip, s.user_agent, s.face_count, s.created_at, "
            "l.hours, l.break_hours, l.status AS log_status, l.flag_reason, l.timestamp "
            "FROM quick_link_uses s LEFT JOIN attendance_logs l ON l.id = s.log_id "
            "WHERE s.link_id = ? ORDER BY s.id DESC LIMIT 500",
            (link_id,),
        ).fetchall()

    uses = []
    for row in rows:
        item = dict(row)
        # The path itself is server-side plumbing; the browser only ever gets the id it can
        # fetch the photo by, so a row can never point the console at an arbitrary file.
        item.pop("photo_path", None)
        item["photo_url"] = f"/api/v1/admin/quick_link_photo/{row['id']}" if row["photo_path"] else None
        uses.append(item)
    return {
        "link_id": link["id"],
        "worker_id": link["worker_id"],
        "worker_name": link["worker_name"],
        "uses": uses,
    }


@admin_router.get("/quick_link_photo/{use_id}")
async def quick_link_photo(use_id: int, current: CurrentUser = Depends(admin_only)):
    """The selfie for one punch. Administrator-only, and only ever a file this app wrote.

    The stored path is resolved and then checked to be *inside* the photo directory before
    it is served: the path comes from our own database, but a path read from a database is
    still a path, and a future bug that lets one be edited must not turn this route into a
    file-read primitive.
    """
    with db() as conn:
        row = conn.execute("SELECT photo_path FROM quick_link_uses WHERE id = ?", (use_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This punch has no record.")
    stored = row["photo_path"]
    if not stored:
        raise HTTPException(status_code=404, detail="No photo was stored for this punch.")

    directory = os.path.abspath(photos_dir())
    candidate = os.path.abspath(os.path.join(directory, os.path.basename(str(stored))))
    if os.path.dirname(candidate) != directory or not os.path.exists(candidate):
        raise HTTPException(status_code=404, detail="The stored photo is no longer on disk.")
    return FileResponse(candidate, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# public: what the link is, and the one tap
# ---------------------------------------------------------------------------
@public_router.get("/{token}")
@limiter.limit(settings.quick_link_rate_limit)
async def quick_link_info(request: Request, token: str):
    """What the page needs to render one button: whose link, and which way the tap goes.

    Rate limited like the punch, because this endpoint is also an oracle: it answers
    "does this token exist?" for anybody who guesses. The rate limit plus the 32-byte token
    makes guessing impractical, and the answer for an unknown token says nothing about
    whether a *number* was close.
    """
    row, usable, state = _load_link(token)
    now = datetime.now()
    with db() as conn:
        worker = conn.execute(
            "SELECT id, name, role, status FROM users WHERE id = ?", (row["worker_id"],)
        ).fetchone()
        if worker is None:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "link_unknown", "message": "This clock link is not valid."},
            )
        _require_active_worker(worker)
        session = conn.execute(
            "SELECT site_name, clock_in_time FROM active_sessions WHERE worker_id = ?",
            (row["worker_id"],),
        ).fetchone()
        sites = [r["site_name"] for r in conn.execute("SELECT site_name FROM construction_sites").fetchall()]

    cap = int(row["max_uses"] or 0)
    payload = {
        "status": "ok" if usable else state,
        "usable": usable,
        "state": state,
        "message": "" if usable else _refuse_link(state).detail["message"],
        "link_id": row["id"],
        "worker_id": worker["id"],
        "worker_name": worker["name"],
        "clocked_in": session is not None,
        "clock_in_time": session["clock_in_time"] if session is not None else None,
        "open_shift_site": session["site_name"] if session is not None else None,
        # What the next tap will do, so the button can say it *before* it is pressed rather
        # than reporting it afterwards.
        "next_action": "Clock Out" if session is not None else "Clock In",
        "expires_at": row["expires_at"],
        "max_uses": cap,
        "uses": int(row["uses"] or 0),
        "remaining_uses": None if not cap else max(0, cap - int(row["uses"] or 0)),
        "note": row["note"],
        "sites": sites,
        # The same numbers the punch endpoint enforces, so the page can refuse a 40 MB video
        # before uploading it instead of being told afterwards.
        "photo_policy": uploads.policy(),
        "server_time": now.strftime(_TS),
    }
    if not usable:
        raise HTTPException(status_code=410, detail=payload)
    return payload


@public_router.post("/{token}")
@limiter.limit(settings.quick_link_rate_limit)
async def submit_quick_punch(
    request: Request,
    token: str,
    selfie: UploadFile = File(...),
    lat: float = Form(...),
    lon: float = Form(...),
    accuracy: float | None = Form(default=None),
    #: Set on the second tap, after the page has shown the early clock-out warning and the
    #: worker chose to go ahead. The link page never decides this for itself.
    confirm_early_checkout: str | None = Form(default=None),
):
    """Clock the link's worker in - or out - from one tap and one photo.

    There is deliberately no ``action`` parameter: whether this is a clock-in or a
    clock-out is *state*, not a choice, and letting the caller name it would let a phone
    claim a second clock-in while one is already open (or a clock-out with none). The server
    reads the worker's open shift inside the same transaction it writes, so the decision
    cannot be raced by a double tap.

    The order of the checks is deliberate and matches ``/attendance/verify``: the link, the
    account, the location, the image policy, liveness, and finally the face. Everything that
    can fail and leave nothing behind happens before the first write.
    """
    import main

    confirmed_early = main._form_flag(confirm_early_checkout)
    row, usable, state = _load_link(token)
    if not usable:
        raise _refuse_link(state)

    # --- location first: a punch from outside every site is not a punch at all ----------
    main.validate_plausible_coordinates(lat, lon)
    with db() as conn:
        worker = conn.execute(
            "SELECT id, name, role, status FROM users WHERE id = ?", (row["worker_id"],)
        ).fetchone()
        if worker is None:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "link_unknown", "message": "This clock link is not valid."},
            )
        _require_active_worker(worker)
        # The whole row, with the site's category joined on: this punch is measured against the
        # matched site's own shift window, and that window is the category's when the site has
        # not set one (see ``shift_windows.SITE_ROW_SQL``).
        sites = conn.execute(shift_windows.SITE_ROW_SQL).fetchall()

    detected_site = None
    detected_site_row = None
    for site in sites:
        if main.get_distance_meters(site["lat"], site["lon"], lat, lon) <= site["radius"]:
            detected_site = site["site_name"]
            detected_site_row = site
            break
    if not detected_site:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "outside_geofence",
                "message": (
                    "Location rejected. This phone is outside every designated construction site, "
                    "so the link cannot record a punch from here."
                ),
            },
        )

    # --- the photo policy, then the photo ----------------------------------------------
    file_bytes = await uploads.read_photo(selfie, field="selfie")
    # The same chain as the live punch endpoints (``uploads.face_frame``): a quick link is a
    # punch, and a worker at 1.2 m deserves the same pixels - and, more importantly, the
    # same ones their stored template was built from whichever way the punch reaches the
    # server.
    image = uploads.face_frame(file_bytes, field="selfie")
    # ``rgb_array`` feeds the liveness model (trained on RGB crops), ``img_array`` is the BGR
    # view the detector expects - the same split ``/attendance/verify`` makes, and for the
    # same reason: two consumers must not silently swap channel order.
    rgb_array = np.array(image)
    img_array = rgb_array[:, :, ::-1]

    try:
        liveness_decision = await face_engine.ENGINE.run_async(liveness.inspect, rgb_array)
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    liveness_class, liveness_score = liveness_decision.log_fields()
    if not liveness_decision.allowed:
        with db(write=True) as conn:
            notifications.notify(
                conn,
                kind=notifications.KIND_LIVENESS_SPOOF,
                severity=notifications.SEVERITY_CRITICAL,
                title="Presentation attack refused",
                body=(
                    f"A quick-link clock for {worker['name']} (id {worker['id']}, link "
                    f"{row['id']}) was refused by the liveness check "
                    f"({liveness_decision.result.verdict}: {liveness_decision.result.detail})."
                ),
                worker_id=str(worker["id"]),
                payload={"liveness": liveness_decision.as_payload(), "link_id": row["id"]},
                dedupe_key=f"quicklink_spoof:{row['id']}:{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
            main._audit(
                conn,
                action="quick_link_liveness_rejected",
                entity="quick_links",
                entity_id=str(row["id"]),
                after={"worker_id": worker["id"], "liveness": liveness_decision.as_payload()},
                request=request,
            )
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": liveness_decision.error_code or liveness.ERR_SPOOF,
                "message": (
                    "Liveness check failed. Hold the camera so your face fills the frame - a photo "
                    "of a photo, or a screen, cannot be used to clock in."
                ),
                "liveness": liveness_decision.as_payload(),
            },
        )

    try:
        # One pool slot for the whole detection, so a burst of taps cannot spawn forty
        # simultaneous TensorFlow calls - and it is awaited, not run on the event loop.
        faces = await face_engine.ENGINE.run_async(detect_faces_sync, img_array)
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    subjects = _subject_report(faces)
    if subjects.merged or subjects.specks:
        log.info("face detection on a clock-link selfie: %s", subjects.summary())
    face_count = subjects.count
    if face_count == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "no_face",
                "message": "No face was found in the photo. Take a selfie in good light and try again.",
            },
        )
    if face_count > 1:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "multiple_faces",
                "message": "More than one face is in the photo. Only the worker may be in frame.",
            },
        )

    liveness_flag = None
    if liveness_decision.result.verdict != liveness.VERDICT_LIVE:
        # Advisory mode: recorded and flagged, not blocked. Same calibration rule as the
        # password path - a threshold earns the right to reject people by being measured first.
        liveness_flag = f"liveness {liveness_decision.result.verdict}"
        with db(write=True) as conn:
            notifications.notify(
                conn,
                kind=notifications.KIND_LIVENESS_DEGRADED,
                severity=notifications.SEVERITY_WARNING,
                title="Quick-link punch did not confirm a genuine face",
                body=(
                    f"{worker['name']} (id {worker['id']}) used clock link {row['id']} with liveness "
                    f"'{liveness_decision.result.verdict}' ({liveness_decision.result.detail})."
                ),
                worker_id=str(worker["id"]),
                payload={"liveness": liveness_decision.as_payload(), "link_id": row["id"]},
                dedupe_key=(
                    f"quicklink_liveness:{row['id']}:{datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
            )

    # --- the punch ----------------------------------------------------------------------
    # The rules travel as they are: the overtime line and what it counts are resolved once,
    # by ``shift_hours.overtime_assessment`` below, exactly as the password path does it.
    rules = main.get_shift_rules()
    now = datetime.now()
    now_str = now.strftime(_TS)
    photo_name = punch_photo_name()
    try:
        image.save(os.path.join(photos_dir(), photo_name), format="JPEG")
    except OSError as exc:  # pragma: no cover - disk failure
        raise HTTPException(status_code=500, detail=f"The punch photo could not be stored: {exc}")

    # The review card's evidence, from the same decoded frame (see ``punch_frames``). The
    # quick-link photo already exists, but it is retention-wiped on its own window and
    # served from the *use* row; the log row is what a pending review is decided from, so the
    # frame rides on it. Best-effort like the online path: a full disk takes the picture, not
    # the punch.
    try:
        punch_frame = main.punch_frames.store_frame(image)
    except OSError:
        punch_frame = None

    with db(write=True) as conn:
        # Re-read the link inside the transaction. Two taps arriving together (a double tap,
        # or a phone retrying a slow request) must not both count as a use, and must not both
        # punch: the row is the lock.
        fresh = conn.execute("SELECT * FROM quick_links WHERE id = ?", (row["id"],)).fetchone()
        usable_now, state_now = (_link_state(fresh, now) if fresh is not None else (False, "revoked"))
        if not usable_now or fresh is None:
            _discard_photo(photo_name)
            raise _refuse_link(state_now if fresh is not None else "revoked")

        session = conn.execute(
            "SELECT clock_in_time, site_name FROM active_sessions WHERE worker_id = ?",
            (worker["id"],),
        ).fetchone()
        action = main.ACTION_CLOCK_OUT if session is not None else main.ACTION_CLOCK_IN
        hours_worked = 0.0
        break_taken = 0.0
        #: Named in full rather than ``overtime``: the module of that name announces the
        #: crossing below, and a local shadowing it would silently break the call.
        overtime_hours = 0.0
        #: The worker's crossing notice, when this tap made one: delivered after this
        #: transaction commits, because a push cannot see an uncommitted row.
        crossing = None
        flag_reason = f"quick link #{row['id']} ({action.lower()}) - selfie recorded, not matched"
        if liveness_flag:
            flag_reason = " | ".join(part for part in (liveness_flag, flag_reason) if part)

        if action == main.ACTION_CLOCK_OUT:
            # The same rule the password path applies: a worker with a row awaiting review does
            # not get to run up more hours on an unverified credential. The shift stays open,
            # which the auto-close covers, and an administrator is one tap away.
            flagged = conn.execute(
                "SELECT id FROM attendance_logs WHERE worker_id = ? AND status = ? LIMIT 1",
                (worker["id"], main.STATUS_PENDING_REVIEW),
            ).fetchone()
            if flagged:
                _discard_photo(photo_name)
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error_code": "account_flagged",
                        "message": (
                            "This account has attendance awaiting review, so the shift cannot be "
                            "closed from a link. Ask an administrator to clear the review."
                        ),
                    },
                )

            clock_in_time = main._parse_ts(session["clock_in_time"])
            if clock_in_time is None:
                _discard_photo(photo_name)
                raise HTTPException(status_code=400, detail="The stored clock-in time is unreadable.")
            seconds_on_site = shift_hours.elapsed_seconds(clock_in_time, now)
            record = shift_hours.recorded_shift(seconds_on_site / 3600.0, rules)
            if record["needs_confirmation"] and not confirmed_early:
                # The same question, the same numbers and the same rollback as
                # ``/attendance/verify``. The photo was written above, so it goes with the
                # refusal: a shift nobody recorded must not leave a selfie behind.
                _discard_photo(photo_name)
                raise HTTPException(status_code=409, detail=main._early_checkout_refusal(record))
            hours_worked = record["paid_hours"]
            break_taken = record["break_hours"]
            hours_note = record["description"]
            conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker["id"],))

            log_status = main.STATUS_APPROVED
            status_code = "approved"
            status_val = "success"
            status_msg = f"Clocked Out of {session['site_name']}. {hours_note}"
            # Overtime is tracked, not silently paid - the same resolvers, and so the same
            # answer, as the password path. The second one is the mid-shift decision: a link
            # clock-out must settle at the ceiling somebody stated for this shift, or an
            # operator who answered a crossing would watch this tap hold the same hours for
            # approval a second time. No answer returns the assessment unchanged.
            assessment = overtime.apply_authorisation(
                conn,
                worker["id"],
                session["clock_in_time"],
                shift_hours.overtime_assessment(seconds_on_site, rules),
                rules,
            )
            if assessment["needs_approval"]:
                overtime_hours = assessment["overtime_hours"]
                log_status = main.STATUS_PENDING_OVERTIME
                status_code = "pending_overtime"
                status_val = "flagged"
                flag_reason = " | ".join(
                    part for part in (flag_reason, assessment["flag_sentence"]) if part
                )
                status_msg = (
                    f"Clocked Out of {session['site_name']}. {hours_note} - overtime requires admin "
                    "approval before payroll"
                )
                notifications.notify(
                    conn,
                    kind=notifications.KIND_REVIEW_PENDING,
                    severity=notifications.SEVERITY_WARNING,
                    title="Overtime requires approval",
                    body=(
                        f"{worker['name']} (id {worker['id']}) worked "
                        f"{assessment['paid_hours']:.2f}h paid at '{session['site_name']}' and "
                        f"clocked out with a quick link, past the "
                        f"{assessment['threshold_hours']:g}h overtime line with "
                        f"{overtime_hours:.2f}h past the paid day. Approve or adjust the extra hours."
                    ),
                    worker_id=str(worker["id"]),
                    site_name=session["site_name"],
                    dedupe_key=f"overtime:{worker['id']}:{now_str}",
                    payload={"hours": hours_worked, "overtime_hours": overtime_hours, "link_id": row["id"]},
                )
                # ... and the tap owes the worker the same sentence the app does. The watcher
                # cannot supply it: it walks shifts that are still open, and this one is over.
                crossing = overtime.announce_crossing(
                    conn,
                    worker_id=str(worker["id"]),
                    site_name=session["site_name"],
                    clock_in_time=session["clock_in_time"],
                    values=rules,
                    moment=now,
                )
        else:
            # A quick link can be used at any site, so the window is the one where the phone
            # actually is - not the global rule, and not the site the worker usually works at.
            site_window = shift_windows.effective_window(detected_site_row, rules)
            late_flag = (
                None
                if site_window.contains_moment(now)
                else shift_windows.describe(site_window)
            )
            conn.execute(
                "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source, "
                "late_flag, liveness_class) VALUES (?, ?, ?, ?, ?, ?)",
                (worker["id"], detected_site, now_str, "quick_link", late_flag, liveness_class),
            )
            if late_flag:
                flag_reason = " | ".join(part for part in (flag_reason, late_flag) if part)
                notifications.notify(
                    conn,
                    kind=notifications.KIND_LATE_ARRIVAL,
                    severity=notifications.SEVERITY_WARNING,
                    title="Arrival outside the clock-in window",
                    body=(
                        f"{worker['name']} (id {worker['id']}) clocked in at {now_str} with a quick "
                        f"link, outside {site_window.site_name or detected_site}'s "
                        f"{site_window.label()} clock-in window."
                    ),
                    worker_id=str(worker["id"]),
                    site_name=detected_site,
                    dedupe_key=f"late:{worker['id']}:{now_str[:10]}",
                )
            log_status = main.STATUS_APPROVED
            status_code = "approved"
            status_val = "success"
            status_msg = f"Clocked In at {detected_site}"

        log_id = main._insert_log(
            conn,
            worker_id=str(worker["id"]),
            site_name=detected_site,
            action=action,
            timestamp=now_str,
            hours=hours_worked,
            # 1.0 is the same "no comparison was made" value the admin-override rows carry.
            # A distance here would be a fabricated number: the photo was counted, not matched.
            score=1.0,
            status=log_status,
            status_code=status_code,
            lat=lat,
            lon=lon,
            accuracy=accuracy,
            source="quick_link",
            liveness_class=liveness_class,
            liveness_score=liveness_score,
            flag_reason=flag_reason,
            overtime_hours=overtime_hours or None,
            break_hours=break_taken or None,
            punch_frame=punch_frame,
        )
        if action == main.ACTION_CLOCK_OUT:
            # The answer the shift was settled at is spent by this row - a link punch is one
            # more way of ending a shift, not a way of leaving the decision live for the next
            # one. Guarded on the action because one row is written for both punches, and there
            # is no session - no clock-in to pair a decision with - when the tap is an arrival.
            overtime.consume_authorisation(conn, worker["id"], session["clock_in_time"], log_id)

        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        cursor = conn.execute(
            "INSERT INTO quick_link_uses (link_id, worker_id, action, site_name, lat, lon, accuracy, "
            "log_id, photo_path, ip, user_agent, face_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                worker["id"],
                action,
                detected_site,
                lat,
                lon,
                accuracy,
                log_id,
                photo_name,
                ip,
                user_agent,
                face_count,
                now_str,
            ),
        )
        use_id = int(cursor.lastrowid or 0)
        first_use = int(fresh["uses"] or 0) == 0  # noqa: F821 - re-read inside this transaction
        conn.execute(
            "UPDATE quick_links SET uses = uses + 1, last_used_at = ?, last_used_ip = ? WHERE id = ?",
            (now_str, ip, row["id"]),
        )

        if first_use:
            # Once per link, not once per tap: the officer who issued it needs to know the link
            # reached a working phone, and then it is an ordinary credential.
            notifications.notify(
                conn,
                kind=notifications.KIND_QUICK_LINK,
                severity=notifications.SEVERITY_INFO,
                title="A clock link was used for the first time",
                body=(
                    f"{worker['name']} (id {worker['id']}) clocked "
                    f"{'in' if action == main.ACTION_CLOCK_IN else 'out'} at {detected_site} with "
                    f"clock link {row['id']}"
                    + (f" from {ip}" if ip else "")
                    + ". The photo is on the link's screen for review."
                ),
                worker_id=str(worker["id"]),
                site_name=detected_site,
                log_id=log_id,
                payload={"link_id": row["id"], "use_id": use_id, "action": action},
                dedupe_key=f"quicklink_firstuse:{row['id']}",
            )

        main._audit(
            conn,
            action=f"quick_link_{'clock_in' if action == main.ACTION_CLOCK_IN else 'clock_out'}",
            entity="quick_link_uses",
            entity_id=use_id,
            after={
                "link_id": row["id"],
                "worker_id": worker["id"],
                "action": action,
                "site": detected_site,
                "log_id": log_id,
                "hours": hours_worked,
                "break_hours": break_taken,
                "face_count": face_count,
                "status": log_status,
            },
            request=request,
        )

    # Committed, so the notice is visible to the dispatcher - and on its own thread, so the
    # tap that produced it did not wait on anybody's push service.
    overtime.deliver_worker_notices(crossing)

    return {
        "status": status_val,
        "action": action,
        "message": status_msg,
        "worker_id": worker["id"],
        "worker_name": worker["name"],
        "site": detected_site,
        "hours": hours_worked,
        "break_hours": break_taken,
        "overtime_hours": overtime_hours,
        "log_id": log_id,
        "use_id": use_id,
        "face_count": face_count,
        "photo_url": f"/api/v1/admin/quick_link_photo/{use_id}",
        "liveness": liveness_decision.as_payload(),
        "server_time": now_str,
    }
