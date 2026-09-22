"""Rapid worker enrollment: self-service mobile capture and bulk batch import.

TWO ROUTES INTO THE SAME LIFECYCLE
----------------------------------
A reference template is the most consequential row in this system: every future punch
is judged against it, so a bad template produces permanent false rejections that no
amount of server tuning can fix. That is why the two paths are treated differently
rather than as two ways to do the same thing.

**Self-service** (``POST /admin/enrollment/invites`` then ``/enroll/<token>``)

* the token is a single-use, expiring, 32-byte random string; only its SHA-256 is
  stored, so a database leak cannot be replayed against the public endpoint;
* the worker captures the photo on the phone they will clock in with, in the place
  they will clock in from - the best available guarantee that the enrolled template
  resembles the live captures;
* liveness can run in ``enforce`` mode, because a live camera frame is exactly what
  passive anti-spoofing is designed to judge. A printed photo must not become a
  permanent template.

**Bulk** (``POST /admin/enrollment/bulk`` with a CSV roster + a ZIP of photos)

* for migrating an existing roster, where the alternative is thousands of manual
  captures;
* liveness can only be *advisory* here: a file on disk cannot be proven live, so
  pretending otherwise would be theatre. Every template it writes is tagged so an
  administrator can see which references were not camera-captured;
* jobs and items are rows in the database, not state in the background task, so
  progress is pollable, a restart does not lose the batch, and ``python -m enrollment
  --job N`` resumes the rows that never finished.

One bad row never aborts a batch: each item carries its own status and error code,
because a 480-row roster failing at row 12 because of one unreadable JPEG is a worse
outcome than importing 479 workers and reporting the bad one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import secrets
import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator

import biometrics
import face_engine
import liveness
import notifications
import textguard
import uploads
from config import PROJECT_ROOT, settings
from database import db
from rate_limit import limiter
from security import (
    BCRYPT_MAX_BYTES,
    CurrentUser,
    admin_only,
    hash_password,
    validate_password_strength,
    validate_user_id_for_role,
)

log = logging.getLogger("attendance.enrollment")

router = APIRouter(prefix="/admin/enrollment", tags=["enrollment"])
public_router = APIRouter(prefix="/enroll", tags=["enrollment"])

#: Roles accepted from a roster, matching ``security.ROLE_ID_RANGES``.
VALID_ROLES = ("worker", "moallem", "admin", "head_admin")

#: Invite kinds. ``enroll`` registers a face for an account that already exists;
#: ``register`` creates the account itself.
KIND_ENROLL = "enroll"
KIND_REGISTER = "register"

#: Roles a *link* may create. Deliberately not the full ``VALID_ROLES``: an invite is a
#: bearer token sent over WhatsApp, and a token that mints an administrator is a
#: privilege-escalation lever that leaks with the message. An admin account is created
#: by an admin, in the console, with the roster in front of them.
REGISTER_ROLES = ("worker", "moallem")

STAGING_ROOT = PROJECT_ROOT / "temp" / "enrollment_jobs"

ITEM_QUEUED = "queued"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"

_TS = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# paths resolved at call time
# ---------------------------------------------------------------------------
# The biometric directories, and every filename built from them, belong to
# ``biometrics``: one module knows where a face is stored and under which name, and this
# one asks it. They are read at call time (see ``biometrics.directories``) so the test
# suite can redirect them away from real worker photos.


def _staging_dir(job_id: int) -> Path:
    return STAGING_ROOT / str(job_id)


# ---------------------------------------------------------------------------
# audit + notification helpers
# ---------------------------------------------------------------------------
def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None,
    entity: str,
    entity_id: str,
    after: Any = None,
    request: Request | None = None,
) -> None:
    try:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, after_json, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor.id if actor else None,
                actor.role if actor else "public",
                action,
                entity,
                str(entity_id),
                json.dumps(after, default=str) if after is not None else None,
                request.client.host if request is not None and request.client else None,
                datetime.now().strftime(_TS),
            ),
        )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# biometric helpers
# ---------------------------------------------------------------------------
def _decode_image(file_bytes: bytes):
    """Decode already-validated photo bytes, through the shared upload policy.

    Every caller reaches this after ``uploads.read_photo`` / ``validate_photo_bytes``,
    so the size, the type and the pixel count have been checked by the time an image is
    actually decoded - one implementation for the whole application.
    """
    return uploads.decode_photo(file_bytes, field="photo")


def face_array(image):
    """The BGR array the face models expect for an already-decoded photo.

    A PIL image is RGB; the face detector and the embedding contract both work in BGR (see
    ``face_onnx``), the order ``cv2`` hands them over. The channels are reversed exactly once -
    here - rather than at each call site, because a template embedded from RGB and a
    live frame embedded from BGR would look like two different people to the distance
    check. ``main.compare_faces_sync`` builds the same array for the attendance path.
    """
    return np.asarray(image)[:, :, ::-1]


def _embed_image(image) -> list[float]:
    """FaceNet-128 embedding for an already-decoded photo. Raises ``ValueError`` on failure.

    The image is handed to the model **in memory**. It used to be saved as a JPEG first -
    ``local_references/.enroll_tmp_<random>.jpg``, or a ``tempfile.mkstemp`` in the system
    temp directory for a bulk batch - which wrote a worker's face to disk on every attempt,
    including the attempts that were about to be refused, into a directory that exists to
    hold faces. The array goes straight to the model, exactly as the attendance path has
    always passed it, so there is nothing to write and no failure path that leaves a file
    behind for a later cleanup to forget.

    ``represent_direct``, not a submission, because this runs *inside* a face-engine job
    (``embed_reference`` is what callers submit) or on the bulk-import thread. A job that
    submitted to its own pool would wait for the worker running it - see ``face_engine``.
    """
    objects = face_engine.ENGINE.represent_direct(face_array(image))
    if not objects:
        raise ValueError("no face detected")
    if len(objects) > 1:
        raise ValueError("multiple faces detected")
    return objects[0]["embedding"]


def _require_liveness(image, *, stage: str) -> liveness.GateDecision:
    """Run the enrollment liveness policy and refuse a presentation attack.

    ``ENROLLMENT_LIVENESS_MODE=inherit`` (the default) means enrollment is never laxer
    than attendance, and an operator who installs the model can raise it to ``enforce``
    without touching the attendance policy.

    The model call is direct (not submitted) because every caller of this is either a
    face-engine job (``embed_reference``) or the bulk-import thread: the pool bounds the
    work at the entry point, and a job must not submit to its own engine.
    """
    configured = (settings.enrollment_liveness_mode or "inherit").lower()
    mode = settings.liveness_mode if configured == "inherit" else configured
    decision = liveness.inspect(np.array(image), mode_override=mode)
    if not decision.allowed and decision.blocked:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": decision.error_code or liveness.ERR_SPOOF,
                "message": (
                    "Liveness check failed: a printed photo or a screen re-display cannot be "
                    "enrolled as a biometric reference. Capture the face directly in good light."
                ),
                "stage": stage,
                "liveness": decision.as_payload(),
            },
        )
    return decision


def embed_reference(image, *, stage: str) -> tuple[liveness.GateDecision, list[float]]:
    """Liveness plus face embedding for an already-validated photo. Writes nothing.

    ``image`` must have passed the shared upload policy (``uploads.read_photo`` or
    ``uploads.validate_photo_bytes``); the size and type are not re-checked here because
    every caller has already refused anything that was not a photo.

    Splitting the cheap, reversible half from ``write_reference`` is what makes a
    create-an-account-then-enroll-it path safe: an embedding can be computed, the row
    inserted, and only then the reference written, so a photo that cannot become a
    template never leaves a file behind and a rejected attempt can be retried.
    """
    decision = _require_liveness(image, stage=stage)
    try:
        embedding = _embed_image(image)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "no_face",
                "message": (
                    f"No single clear face was found in the photo ({exc}). Use a well-lit, "
                    "straight-on photo of the person alone."
                ),
            },
        ) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Enrollment failed: {exc}") from exc
    return decision, embedding


def write_reference(worker_id: str, image, embedding: list[float]) -> str:
    """Persist an embedding as ``worker_id``'s face reference, and return its path.

    The files are named by the account's immutable biometric id, never by the account id
    itself: ``biometrics`` holds that policy, and the reason for it.
    """
    return biometrics.write_reference(worker_id, image, embedding)


def register_reference(worker_id: str, image, *, stage: str) -> liveness.GateDecision:
    """Embed a photo and write it as an *existing* account's reference, in one step.

    For callers whose account is already in the database (the registration page writes
    its row first, the console's create-user form writes its row first): the alternative
    - writing the template before the account exists - can overwrite the reference of an
    account that already holds that id, which would hand one worker's clock-in to
    another worker's face.
    """
    decision, embedding = embed_reference(image, stage=stage)
    biometrics.write_reference(worker_id, image, embedding)
    return decision


# ---------------------------------------------------------------------------
# token helpers
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mask_name(name: str | None) -> str:
    if not name:
        return "worker"
    parts = [part for part in str(name).split() if part]
    if not parts:
        return "worker"
    if len(parts) == 1:
        return parts[0][:1] + "." * min(3, max(1, len(parts[0]) - 1))
    return f"{parts[0]} {parts[-1][0]}."


def _invite_status(row: sqlite3.Row, now: datetime) -> tuple[bool, str]:
    if row["revoked_at"]:
        return False, "revoked"
    expires = row["expires_at"]
    parsed = expires if isinstance(expires, datetime) else None
    if parsed is None:
        for fmt in (_TS, f"{_TS}.%f"):
            try:
                parsed = datetime.strptime(str(expires), fmt)
                break
            except (TypeError, ValueError):
                continue
    if parsed is not None and parsed < now:
        return False, "expired"
    if int(row["uses"]) >= int(row["max_uses"]):
        return False, "already_used"
    return True, "open"


def _invite_kind(row: sqlite3.Row) -> str:
    """``'enroll'`` or ``'register'``, read defensively.

    Every write sets the column explicitly; this only has to be right for a row read
    from a database where migration 8 has not run yet, which must be treated as the
    original kind rather than crashing the public page.
    """
    try:
        value = str(row["kind"] or KIND_ENROLL).strip().lower()
    except (IndexError, KeyError):
        return KIND_ENROLL
    return value if value in (KIND_ENROLL, KIND_REGISTER) else KIND_ENROLL


def _invite_missing() -> HTTPException:
    """The refusal for a token that matches no invite at all.

    It carries an ``error_code`` for the same reason every other refusal on this route
    does: ``enroll.html`` renders the sentence a worker reads from the code, in the
    worker's own language, and the English ``message`` is what an administrator reading
    a log or a ``curl`` sees. A bare string left the page with nothing to key on, so a
    worker reading Arabic, Hindi or Urdu was shown this English sentence.
    """
    return HTTPException(
        status_code=404,
        detail={"error_code": "invite_unknown", "message": "This enrollment link is not valid."},
    )


def _qr_data_uri(url: str) -> str | None:
    """QR PNG as a data URI, or ``None`` when the optional ``qrcode`` extra is absent.

    The URL is always returned, so the feature degrades to "send this link" rather
    than failing - a missing QR renderer must not block enrollment.
    """
    try:
        import base64

        import qrcode

        image = qrcode.make(url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return None


def _public_base_url(request: Request, override: str | None) -> str:
    base = (override or settings.enrollment_base_url or "").strip()
    if not base:
        base = str(request.base_url)
    if not base.endswith("/"):
        base += "/"
    return base


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class InviteCreate(BaseModel):
    worker_id: str
    ttl_hours: int | None = None
    max_uses: int | None = None
    #: Free text for the administrator's own reference ("the man from the third tower"):
    #: prose, so apostrophes survive and markup does not. It is shown in the invite list.
    note: str | None = None
    base_url: str | None = None
    #: ``enroll`` (default) registers a face for an account that exists; ``register``
    #: creates the account itself, in which case the name and role below are required
    #: and ``worker_id`` is the id being reserved for it.
    kind: str = KIND_ENROLL
    #: The name written onto the account a ``register`` link creates. It is the *only*
    #: name the visitor cannot choose - the public endpoint takes the id, the name and the
    #: role from this row - which makes this the field that decides what a worker is called
    #: on the roster, in the reports and in every notification about them. An identifier,
    #: therefore, not prose.
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(
            value, field="Note", max_length=textguard.MAX_LABEL, allow_empty=True
        )

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.identifier(
            value, field="Name", max_length=textguard.MAX_NAME, allow_empty=True
        )

    @field_validator("email")
    @classmethod
    def _plain_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.contact(value, field="Email")

    @field_validator("phone")
    @classmethod
    def _plain_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.contact(value, field="Phone")


# ---------------------------------------------------------------------------
# self-service invites (admin)
# ---------------------------------------------------------------------------
@router.post("/invites")
async def create_invite(request: Request, payload: InviteCreate, current: CurrentUser = Depends(admin_only)):
    """Issue a one-time link (and QR) to send to somebody.

    Two kinds, and the difference is what the link is allowed to do:

    * ``enroll`` - the account exists; the link registers its owner's face.
    * ``register`` - the account does **not** exist; the link creates it. This is the
      "send this to the new man" link, so it is the more dangerous of the two and is
      constrained accordingly: the role is chosen here by the admin (never by whoever
      opens the link), it must be a worker or a moallem, the id is reserved here so the
      visitor cannot pick its own, and it is single-use whatever ``max_uses`` says.

    The plaintext token is returned exactly once: only its hash is stored, so it cannot
    be recovered later - issue a new invite instead.
    """
    kind = str(payload.kind or KIND_ENROLL).strip().lower()
    if kind not in (KIND_ENROLL, KIND_REGISTER):
        raise HTTPException(status_code=400, detail=f"kind must be '{KIND_ENROLL}' or '{KIND_REGISTER}'.")
    worker_id = str(payload.worker_id).strip()

    pending_name = pending_role = None
    with db() as conn:
        user = conn.execute("SELECT id, name, role FROM users WHERE id = ?", (worker_id,)).fetchone()

    if kind == KIND_ENROLL:
        if user is None:
            raise HTTPException(status_code=404, detail="Worker ID not found. Create the user first.")
    else:
        if user is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Account {worker_id} already exists. A registration link creates a new account, "
                    "so reserve an id that is still free - or send an enrollment link for the "
                    "existing one."
                ),
            )
        pending_name = str(payload.name or "").strip()
        if not pending_name:
            raise HTTPException(status_code=400, detail="A registration link needs the person's name.")
        pending_role = str(payload.role or "worker").strip().lower()
        if pending_role not in REGISTER_ROLES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A registration link may only create a worker or a moallem, with the role chosen "
                    "here. An administrator account is created by an administrator in the console."
                ),
            )
        # A standard admin cannot create an administrator anywhere else either; this is
        # belt and braces, because the role is already restricted to worker/moallem.
        if pending_role in ("admin", "head_admin") and current.role != "head_admin":
            raise HTTPException(status_code=403, detail="Only a head administrator may create administrators.")
        # One implementation for every path that mints an account - the console, the
        # roster import and this link all ask ``security`` whether the id fits the
        # role's range, so an id reserved here cannot land in another role's block.
        validate_user_id_for_role(worker_id, pending_role)

    ttl = int(payload.ttl_hours or settings.enrollment_token_ttl_hours)
    default_uses = 1 if kind == KIND_REGISTER else settings.enrollment_token_max_uses
    uses = int(payload.max_uses or default_uses)
    if kind == KIND_REGISTER:
        # Single use, always. A multi-use registration link is a link that creates N
        # accounts for whoever has it, and the id it reserves can only be used once in
        # any case - so the second use would fail after the visitor had filled the form.
        uses = 1
    if ttl <= 0 or ttl > 24 * 30:
        raise HTTPException(status_code=400, detail="ttl_hours must be between 1 and 720.")
    if uses <= 0 or uses > 10:
        raise HTTPException(status_code=400, detail="max_uses must be between 1 and 10.")

    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = (now + timedelta(hours=ttl)).strftime(_TS)
    with db(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO enrollment_invites (token_hash, worker_id, created_by, created_at, expires_at, "
            "max_uses, note, kind, pending_name, pending_role, pending_email, pending_phone) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash_token(token),
                worker_id,
                current.id,
                now.strftime(_TS),
                expires_at,
                uses,
                payload.note,
                kind,
                pending_name,
                pending_role,
                (payload.email or "").strip() or None,
                (payload.phone or "").strip() or None,
            ),
        )
        invite_id = int(cursor.lastrowid or 0)
        _audit(
            conn,
            action="enrollment_invite_create",
            actor=current,
            entity="enrollment_invites",
            entity_id=str(invite_id),
            after={
                "kind": kind,
                "worker_id": worker_id,
                "expires_at": expires_at,
                "max_uses": uses,
                "note": payload.note,
                **({"name": pending_name, "role": pending_role} if kind == KIND_REGISTER else {}),
            },
            request=request,
        )

    url = f"{_public_base_url(request, payload.base_url)}enroll/{token}"
    return {
        "status": "success",
        "kind": kind,
        "invite_id": invite_id,
        "worker_id": worker_id,
        "worker_name": user["name"] if user is not None else pending_name,
        "role": user["role"] if user is not None else pending_role,
        "url": url,
        "token": token,
        "expires_at": expires_at,
        "max_uses": uses,
        "qr_png_data_uri": _qr_data_uri(url),
        "note": "Send this link once. Only its hash is stored, so it cannot be shown again.",
    }


@router.get("/invites")
async def list_invites(active: int = 0, limit: int = 200, current: CurrentUser = Depends(admin_only)):
    limit = max(1, min(int(limit), 1000))
    now = datetime.now()
    with db() as conn:
        rows = conn.execute(
            "SELECT i.id, i.worker_id, u.name AS worker_name, i.created_by, i.created_at, i.expires_at, "
            "i.max_uses, i.uses, i.revoked_at, i.completed_at, i.last_used_at, i.last_used_ip, i.note, "
            "i.kind, i.pending_name, i.pending_role, i.pending_email, i.pending_phone "
            "FROM enrollment_invites i LEFT JOIN users u ON i.worker_id = u.id "
            "ORDER BY i.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    output = []
    for row in rows:
        usable, state = _invite_status(row, now)
        if active and not usable:
            continue
        item = dict(row)
        item["state"] = state
        item["usable"] = usable
        item["kind"] = _invite_kind(row)
        # What the link is for, in one field, so the dashboard does not have to know that
        # a pending account keeps its name in a different column from an existing one.
        item["subject_name"] = (
            row["pending_name"] if item["kind"] == KIND_REGISTER else row["worker_name"]
        ) or row["worker_id"]
        output.append(item)
    return output


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(request: Request, invite_id: int, current: CurrentUser = Depends(admin_only)):
    now_str = datetime.now().strftime(_TS)
    with db(write=True) as conn:
        row = conn.execute("SELECT * FROM enrollment_invites WHERE id = ?", (invite_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Invite not found.")
        conn.execute("UPDATE enrollment_invites SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now_str, invite_id))
        _audit(
            conn,
            action="enrollment_invite_revoke",
            actor=current,
            entity="enrollment_invites",
            entity_id=str(invite_id),
            after={"worker_id": row["worker_id"]},
            request=request,
        )
    return {"status": "success", "message": f"Invite {invite_id} revoked."}


# ---------------------------------------------------------------------------
# self-service capture (public, token-scoped)
# ---------------------------------------------------------------------------
@public_router.get("/{token}")
@limiter.limit(settings.enrollment_rate_limit)
async def invite_info(request: Request, token: str):
    """Public peek at an invite: what kind of link this is, and who it is for.

    Returns a **masked** name and never the roster, including for a registration link
    whose name was typed by the administrator. The name is masked here for the same
    reason it is masked on an enrollment link: this endpoint is public, the token both
    travels through WhatsApp and sits in a URL history, and whoever is holding a
    forwarded link knows their own name already - a stranger holding it does not need
    to learn one.

    ``kind`` is the important field for the page that calls this: it decides whether
    the visitor is registering their face for an account that exists (``enroll``) or
    creating the account itself (``register``), and only the second asks for a password.
    """
    now = datetime.now()
    with db() as conn:
        row = conn.execute(
            "SELECT i.*, u.name AS worker_name FROM enrollment_invites i "
            "LEFT JOIN users u ON i.worker_id = u.id WHERE i.token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
    if row is None:
        raise _invite_missing() from None
    usable, state = _invite_status(row, now)
    kind = _invite_kind(row)
    reserved_name = row["pending_name"] if kind == KIND_REGISTER else row["worker_name"]
    return {
        "status": state,
        "usable": usable,
        "kind": kind,
        "worker_id": row["worker_id"],
        "worker_name": _mask_name(reserved_name),
        "role": (row["pending_role"] if kind == KIND_REGISTER else None),
        "expires_at": row["expires_at"],
        "remaining_uses": max(0, int(row["max_uses"]) - int(row["uses"])),
        "liveness_mode": settings.enrollment_liveness_mode,
        # The public capture page cannot read the server's settings, so the two numbers
        # it has to state before somebody taps a button are sent with the invite: the
        # upload ceiling it will be held to, and the shortest password it may choose.
        "photo_policy": uploads.policy(),
        "min_password_length": settings.min_password_length,
    }


@public_router.post("/{token}/register")
@limiter.limit(settings.enrollment_rate_limit)
async def submit_registration(
    request: Request,
    token: str,
    password: str = Form(...),
    photo: UploadFile = File(...),
    phone: str = Form(default=""),
    email: str = Form(default=""),
):
    """Create the account a registration link was issued for, with its reference photo.

    This is the one endpoint in the application that creates a user without a signed-in
    administrator behind the request, so every part of it is constrained by the invite
    rather than by the visitor: the id, the name and the role all come from the row the
    administrator wrote, and the only choices left to whoever opens the link are their
    own password and their own face.

    The order is deliberate. Everything that can fail and leave nothing behind happens
    first - the password policy, the photo policy, liveness, and the face embedding -
    and the account row, the reference and the consumed token are written only once all
    of them have passed. A failure therefore    costs the visitor a retry rather than costing them the link, and it cannot leave a
    half-created account that can sign in but cannot clock in.

    ``email`` and ``phone`` are the only free text such a visitor can set, and both are
    written onto a new ``users`` row. A name or a role cannot be smuggled in here: they come
    from the invite the administrator wrote, which is where they are vetted.
    """
    try:
        email = textguard.contact(email, field="Email")
        phone = textguard.contact(phone, field="Phone")
    except ValueError as exc:
        raise textguard.http_error(exc) from None

    now = datetime.now()
    with db() as conn:
        row = conn.execute("SELECT * FROM enrollment_invites WHERE token_hash = ?", (_hash_token(token),)).fetchone()
    if row is None:
        raise _invite_missing() from None
    usable, state = _invite_status(row, now)
    if not usable:
        raise HTTPException(
            status_code=410,
            detail={
                "error_code": f"invite_{state}",
                "message": f"This registration link is {state}. Ask your administrator for a new one.",
            },
        )
    if _invite_kind(row) != KIND_REGISTER:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "invite_kind_mismatch",
                "message": (
                    "This link registers a face for an account that already exists, so it cannot "
                    "create one. Open the enrollment page instead."
                ),
            },
        )

    # The same policy the console enforces when an administrator sets a password: the
    # person choosing it here is no more trusted than an administrator is, and a weaker
    # rule for the one password nobody can reset for them would be a hole with a name.
    validate_password_strength(password)

    worker_id = str(row["worker_id"])
    role = str(row["pending_role"] or "worker")
    name = str(row["pending_name"] or "").strip()
    if not name or role not in REGISTER_ROLES:
        # Only reachable if somebody edited the row by hand; refuse rather than mint an
        # account with no name or a role this table was never allowed to hand out.
        raise HTTPException(status_code=409, detail="This registration link is not complete. Ask for a new one.")
    validate_user_id_for_role(worker_id, role)

    # The free-id check happens before the photo work, not after: the id is the thing a
    # link reserves, and finding out it was taken after the visitor has posed for a
    # camera is a worse answer than finding out immediately. The insert re-checks it,
    # because this read and that write are not one transaction.
    with db() as conn:
        taken = conn.execute("SELECT id FROM users WHERE id = ?", (worker_id,)).fetchone()
    if taken is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "id_taken",
                "message": f"Account {worker_id} already exists. Ask your administrator for a new link.",
            },
        )

    file_bytes = await uploads.read_photo(photo, field="photo")
    image = uploads.decode_photo(file_bytes, field="photo")
    # Advisory by default, ``enforce`` when an operator says so - the same setting the
    # self-service capture answers to, because the camera situation is identical. The
    # embedding is computed here and written after the account exists: a photo that
    # cannot become a template must not leave a file behind, and the template must never
    # land on an id that is still somebody else's.
    try:
        decision, embedding = await face_engine.ENGINE.run_async(
            embed_reference, image, stage="registration"
        )
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None

    with db(write=True) as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, name, email, phone, password_hash, role, status, enrolled_at, "
                "template_version, biometric_id) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 1, ?)",
                (
                    worker_id,
                    name,
                    (email or row["pending_email"] or "").strip(),
                    (phone or row["pending_phone"] or "").strip(),
                    hash_password(password),
                    role,
                    now.strftime(_TS),
                    # The face this link is about to store is filed under an id minted
                    # here, so it can never be confused with whoever held this account id
                    # before (see ``biometrics.new_account_id``).
                    biometrics.new_account_id(worker_id),
                ),
            )
        except sqlite3.IntegrityError:
            # The id was free when the link was issued and is not free now. Answering 409
            # rather than 500 says the truth: the reservation lost a race.
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "id_taken",
                    "message": f"Account {worker_id} already exists. Ask your administrator for a link with a free id.",
                },
            )
        conn.execute(
            "UPDATE enrollment_invites SET uses = uses + 1, completed_at = ?, last_used_at = ?, last_used_ip = ? "
            "WHERE id = ?",
            (
                now.strftime(_TS),
                now.strftime(_TS),
                request.client.host if request.client else None,
                row["id"],
            ),
        )
        _audit(
            conn,
            action="user_create",
            actor=None,
            entity="users",
            entity_id=worker_id,
            after={
                "source": "registration_link",
                "invite_id": row["id"],
                "name": name,
                "role": role,
                "liveness": decision.as_payload(),
            },
            request=request,
        )
        notifications.notify(
            conn,
            kind=notifications.KIND_ENROLLMENT_COMPLETED,
            severity=notifications.SEVERITY_INFO,
            title="New account registered from a link",
            body=(
                f"{name} (id {worker_id}, {role}) created their own account with a registration "
                "link and registered a reference photo."
            ),
            worker_id=worker_id,
            payload={"invite_id": row["id"], "role": role, "liveness": decision.as_payload()},
            dedupe_key=f"register_done:{row['id']}",
        )

    write_reference(worker_id, image, embedding)

    return {
        "status": "success",
        "message": "Your account is ready. Sign in with your ID and the password you chose.",
        "worker_id": worker_id,
        "role": role,
        "liveness": decision.as_payload(),
    }


@public_router.post("/{token}")
@limiter.limit(settings.enrollment_rate_limit)
async def submit_enrollment(
    request: Request,
    token: str,
    photo: UploadFile = File(...),
    phone: str = Form(default=""),
    email: str = Form(default=""),
):
    """Capture a worker's reference photo from their own phone.

    Public by necessity - the worker has no account credentials yet - so the token is
    the entire authorization: single-use, expiring, hashed at rest, and rate limited.

    ``email`` and ``phone`` are the only free text a visitor can put on an existing
    account here, so they are vetted like every other stored string rather than being
    trusted because the endpoint is thin.
    """
    try:
        email = textguard.contact(email, field="Email")
        phone = textguard.contact(phone, field="Phone")
    except ValueError as exc:
        raise textguard.http_error(exc) from None
    now = datetime.now()
    with db() as conn:
        row = conn.execute("SELECT * FROM enrollment_invites WHERE token_hash = ?", (_hash_token(token),)).fetchone()
    if row is None:
        raise _invite_missing() from None
    usable, state = _invite_status(row, now)
    if not usable:
        raise HTTPException(
            status_code=410,
            detail={
                "error_code": f"invite_{state}",
                "message": f"This enrollment link is {state}. Ask your administrator for a new one.",
            },
        )

    if _invite_kind(row) != KIND_ENROLL:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "invite_kind_mismatch",
                "message": (
                    "This link creates a new account, so it has to be finished on the registration "
                    "page, where a password is chosen."
                ),
            },
        )

    # One policy for every upload in the application - size while reading, type from the
    # bytes, pixel ceiling before decoding. It replaced a bare ``photo.read()`` followed
    # by a length check, which spent the memory before it decided the file was too big.
    file_bytes = await uploads.read_photo(photo, field="photo")
    image = uploads.decode_photo(file_bytes, field="photo")

    worker_id = str(row["worker_id"])
    try:
        # Liveness and the embedding, as one unit of pool work: both are model calls, and
        # the engine is the one place that bounds how many run at once. Doing them on the
        # event loop, as this used to, meant one worker's photograph stalled every other
        # request in the process for the length of an inference.
        decision, embedding = await face_engine.ENGINE.run_async(
            embed_reference, image, stage="self_service"
        )
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        # A liveness refusal is a 422 written for the worker; it is not ours to rewrite.
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Enrollment failed: {exc}")

    biometrics.write_reference(worker_id, image, embedding)

    with db(write=True) as conn:
        conn.execute(
            "UPDATE enrollment_invites SET uses = uses + 1, completed_at = ?, last_used_at = ?, last_used_ip = ? "
            "WHERE id = ?",
            (
                now.strftime(_TS),
                now.strftime(_TS),
                request.client.host if request.client else None,
                row["id"],
            ),
        )
        conn.execute(
            "UPDATE users SET enrolled_at = ?, template_version = COALESCE(template_version, 0) + 1, "
            "phone = COALESCE(NULLIF(?, ''), phone), email = COALESCE(NULLIF(?, ''), email) WHERE id = ?",
            (now.strftime(_TS), phone.strip(), email.strip(), worker_id),
        )
        _audit(
            conn,
            action="enrollment_completed",
            actor=None,
            entity="users",
            entity_id=worker_id,
            after={"source": "self_service", "invite_id": row["id"], "liveness": decision.as_payload()},
            request=request,
        )
        notifications.notify(
            conn,
            kind=notifications.KIND_ENROLLMENT_COMPLETED,
            severity=notifications.SEVERITY_INFO,
            title="Worker completed self-service enrollment",
            body=f"Worker {worker_id} submitted a reference photo from their own device.",
            worker_id=worker_id,
            payload={"invite_id": row["id"], "liveness": decision.as_payload()},
            dedupe_key=f"enroll_done:{row['id']}",
        )

    return {
        "status": "success",
        "message": "Your reference photo has been registered. You can now clock in.",
        "worker_id": worker_id,
        "liveness": decision.as_payload(),
    }


# ---------------------------------------------------------------------------
# bulk onboarding
# ---------------------------------------------------------------------------
ROSTER_ALIASES = {
    "user_id": "user_id",
    "worker_id": "user_id",
    "id": "user_id",
    "name": "name",
    "full_name": "name",
    "role": "role",
    "email": "email",
    "phone": "phone",
    "photo": "photo",
    "photo_filename": "photo",
    "filename": "photo",
}


def _normalise_header(fieldnames: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in fieldnames or []:
        key = str(raw or "").strip().lower().replace(" ", "_")
        if key in ROSTER_ALIASES:
            mapping[str(raw)] = ROSTER_ALIASES[key]
    return mapping


def parse_roster(text: str) -> tuple[list[dict], list[str]]:
    """Parse the CSV into normalised rows plus a list of file-level problems."""
    problems: list[str] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    mapping = _normalise_header(reader.fieldnames)
    if "user_id" not in mapping.values():
        problems.append("roster is missing a user_id column (aliases: worker_id, id)")
    if "name" not in mapping.values():
        problems.append("roster is missing a name column (alias: full_name)")
    rows: list[dict] = []
    if problems:
        return rows, problems
    for index, raw in enumerate(reader, start=2):  # 2 = first data line, 1-based with header
        item = {target: "" for target in ROSTER_ALIASES.values()}
        for source, target in mapping.items():
            value = raw.get(source)
            if value is not None and not item.get(target):
                item[target] = str(value).strip()
        item["_line"] = index
        if not item["user_id"] and not item["name"]:
            continue  # blank line
        rows.append(item)
    return rows, problems


def _zip_index(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map every member to a lookup key: full name and lowercase basename."""
    index: dict[str, str] = {}
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        index[name] = name
        index.setdefault(name.split("/")[-1].lower(), name)
    return index


def _resolve_member(index: dict[str, str], wanted: str) -> str | None:
    if not wanted:
        return None
    if wanted in index:
        return index[wanted]
    return index.get(wanted.split("/")[-1].lower())


def process_job(job_id: int) -> dict:
    """Process (or resume) a bulk job. Runs after the response, in a worker thread.

    Deliberately resumable: every item's outcome is committed as it happens, so a crash
    or a redeploy mid-batch leaves a job that ``python -m enrollment --job N``
    continues rather than a batch an operator has to guess the state of.
    """
    staging = _staging_dir(job_id)
    started = datetime.now()
    summary = {"job_id": job_id, "processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}

    with db(write=True) as conn:
        job = conn.execute("SELECT * FROM enrollment_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return {"job_id": job_id, "error": "job not found"}
        if job["status"] in ("done", "done_with_errors"):
            return {"job_id": job_id, "error": f"job already {job['status']}"}
        # Recover rows left mid-flight by a crash or a redeploy, then claim the work
        # atomically. The claim is what makes two overlapping runners safe: the second
        # one finds nothing in 'queued' and does nothing, instead of writing every
        # template twice and double-counting the metrics.
        conn.execute(
            "UPDATE enrollment_job_items SET status = ? WHERE job_id = ? AND status = 'processing'",
            (ITEM_QUEUED, job_id),
        )
        conn.execute(
            "UPDATE enrollment_job_items SET status = 'processing' WHERE job_id = ? AND status = ?",
            (job_id, ITEM_QUEUED),
        )
        conn.execute(
            "UPDATE enrollment_jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
            (started.strftime(_TS), job_id),
        )

    photos: dict[str, str] = {}
    archive: zipfile.ZipFile | None = None
    archive_path = staging / "photos.zip"
    try:
        if archive_path.exists():
            archive = zipfile.ZipFile(archive_path)
            photos = _zip_index(archive)

        with db() as conn:
            items = conn.execute(
                "SELECT * FROM enrollment_job_items WHERE job_id = ? AND status = 'processing' ORDER BY id ASC",
                (job_id,),
            ).fetchall()

        for item in items:
            outcome = _process_item(job_id, item, archive=archive, photos=photos)
            summary["processed"] += 1
            summary[outcome] = summary.get(outcome, 0) + 1
            with db(write=True) as conn:
                conn.execute(
                    "UPDATE enrollment_jobs SET processed = processed + 1, "
                    "succeeded = succeeded + ?, failed = failed + ?, skipped = skipped + ? WHERE id = ?",
                    (
                        1 if outcome == "succeeded" else 0,
                        1 if outcome == "failed" else 0,
                        1 if outcome == "skipped" else 0,
                        job_id,
                    ),
                )
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("bulk enrollment job %s failed", job_id)
        with db(write=True) as conn:
            conn.execute(
                "UPDATE enrollment_jobs SET status = 'failed', finished_at = ?, detail = ? WHERE id = ?",
                (datetime.now().strftime(_TS), json.dumps({"error": f"{type(exc).__name__}: {exc}"}), job_id),
            )
        return {**summary, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if archive is not None:
            archive.close()

    finished = datetime.now()
    with db(write=True) as conn:
        row = conn.execute("SELECT * FROM enrollment_jobs WHERE id = ?", (job_id,)).fetchone()
        status = "done" if not (row and row["failed"]) else "done_with_errors"
        conn.execute(
            "UPDATE enrollment_jobs SET status = ?, finished_at = ?, detail = ? WHERE id = ?",
            (
                status,
                finished.strftime(_TS),
                json.dumps({"summary": summary, "duration_s": (finished - started).total_seconds()}),
                job_id,
            ),
        )
        notifications.notify(
            conn,
            kind=notifications.KIND_ENROLLMENT_COMPLETED,
            severity=notifications.SEVERITY_WARNING if summary.get("failed") else notifications.SEVERITY_INFO,
            title="Bulk enrollment finished",
            body=(
                f"Job {job_id}: {summary['succeeded']} enrolled, {summary['failed']} failed, "
                f"{summary['skipped']} skipped."
            ),
            payload=summary,
            dedupe_key=f"bulk_done:{job_id}",
        )

    import shutil

    shutil.rmtree(staging, ignore_errors=True)
    return {**summary, "status": status}


def _process_item(job_id: int, item: sqlite3.Row, *, archive, photos: dict[str, str]) -> str:
    """Enroll one roster row. Returns ``succeeded`` / ``failed`` / ``skipped``."""
    worker_id = str(item["worker_id"] or "").strip()
    with db() as conn:
        user = conn.execute("SELECT id, name, role FROM users WHERE id = ?", (worker_id,)).fetchone()
        dry_run = bool(conn.execute("SELECT dry_run FROM enrollment_jobs WHERE id = ?", (job_id,)).fetchone()["dry_run"])

    def finish(conn: sqlite3.Connection, status: str, code: str | None = None, detail: str | None = None) -> None:
        conn.execute(
            "UPDATE enrollment_job_items SET status = ?, error_code = ?, error_detail = ?, finished_at = ? WHERE id = ?",
            (status, code, detail, datetime.now().strftime(_TS), item["id"]),
        )

    if user is None:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "unknown_user", f"no row in users for id {worker_id!r}")
        return "failed"

    wanted = item["photo_name"] or f"{worker_id}.jpg"
    member = _resolve_member(photos, wanted)
    if archive is None or member is None:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "photo_missing", f"no member matching {wanted!r} in the ZIP")
        return "failed"

    # The declared size before the bytes are read. A ZIP member is compressed, so a
    # 20 KB entry can decompress to gigabytes; reading it to discover that is the
    # attack, and asking the archive first is free. The bytes read are still measured
    # again below, because the declaration is the uploader's number, not ours.
    declared = archive.getinfo(member).file_size
    limit = uploads.max_photo_bytes()
    if declared > limit:
        with db(write=True) as conn:
            finish(
                conn,
                ITEM_FAILED,
                uploads.ERR_TOO_LARGE,
                f"{member!r} declares {declared} bytes; the photo limit is {limit}",
            )
        return "failed"

    try:
        file_bytes = archive.read(member)
    except Exception as exc:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "photo_unreadable", f"{type(exc).__name__}: {exc}")
        return "failed"

    try:
        # Same size ceiling, same type sniffing and same pixel cap as every other upload
        # in the application - a ZIP is a delivery method, not a second policy.
        uploads.validate_photo_bytes(file_bytes, field=f"photo {member!r}")
    except HTTPException as exc:
        reason = exc.detail.get("error_code") if isinstance(exc.detail, dict) else str(exc.detail)
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, str(reason), f"{member!r} refused by the photo policy")
        return "failed"

    try:
        image = _decode_image(file_bytes)
    except Exception as exc:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "decode_failed", f"{type(exc).__name__}: {exc}")
        return "failed"

    # Both model calls go through the face engine, so a 400-row batch is serialized behind
    # the same bounded capacity as the punches instead of competing with them for the CPU
    # from its own thread. Each is one submission - the item's thread is not a pool worker,
    # so it may submit - and the two are kept separate so a refusal keeps its current
    # meaning: a declined liveness check *skips* the row, a photo with no usable face fails
    # it. Collapsing them into one call would report both as one or the other.
    try:
        decision = face_engine.ENGINE.run(_require_liveness, image, stage="bulk_import")
    except face_engine.FaceEngineBusy as exc:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "engine_busy", str(exc))
        return "failed"
    except HTTPException as exc:
        reason = exc.detail.get("error_code") if isinstance(exc.detail, dict) else str(exc.detail)
        with db(write=True) as conn:
            finish(conn, ITEM_SKIPPED, str(reason), "refused by the enrollment liveness policy")
        return "skipped"

    if dry_run:
        with db(write=True) as conn:
            finish(conn, ITEM_SKIPPED, "dry_run", "validated only (dry run)")
        return "skipped"

    try:
        embedding = face_engine.ENGINE.run(_embed_image, image)
    except face_engine.FaceEngineBusy as exc:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "engine_busy", str(exc))
        return "failed"
    except ValueError as exc:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "no_usable_face", str(exc))
        return "failed"
    except Exception as exc:
        with db(write=True) as conn:
            finish(conn, ITEM_FAILED, "embed_failed", f"{type(exc).__name__}: {exc}")
        return "failed"

    biometrics.write_reference(worker_id, image, embedding)
    liveness_class, liveness_score = decision.log_fields()

    with db(write=True) as conn:
        conn.execute(
            "UPDATE users SET enrolled_at = ?, template_version = COALESCE(template_version, 0) + 1 WHERE id = ?",
            (datetime.now().strftime(_TS), worker_id),
        )
        version = conn.execute("SELECT template_version FROM users WHERE id = ?", (worker_id,)).fetchone()
        conn.execute(
            "UPDATE enrollment_job_items SET status = ?, template_version = ?, finished_at = ?, "
            "error_detail = ? WHERE id = ?",
            (
                ITEM_DONE,
                int(version["template_version"]) if version else 1,
                datetime.now().strftime(_TS),
                json.dumps({"source": "bulk_import", "liveness_class": liveness_class, "photo": member}),
                item["id"],
            ),
        )
        _audit(
            conn,
            action="enrollment_bulk_item",
            actor=None,
            entity="users",
            entity_id=worker_id,
            after={
                "job_id": job_id,
                "source": "bulk_import",
                "liveness_class": liveness_class,
                "liveness_score": liveness_score,
                "photo_member": member,
            },
        )
    return "succeeded"


@router.post("/bulk")
async def bulk_enroll(
    request: Request,
    background: BackgroundTasks,
    roster: UploadFile = File(...),
    photos: UploadFile = File(...),
    dry_run: bool | None = Form(default=None),
    current: CurrentUser = Depends(admin_only),
):
    """Stage a CSV roster + ZIP of photos, then enroll in the background.

    Returns immediately with a ``job_id``. Nothing expensive (embedding) happens during
    the request, so a 500-row roster cannot hold an HTTP connection open or trip a
    proxy timeout.

    ``dry_run`` is accepted as a form field *or* a query parameter
    (``?dry_run=true``): an operator validating a roster before importing it should be
    able to do that from a URL, without crafting a multipart body.
    """
    if dry_run is None:
        dry_run = str(request.query_params.get("dry_run", "")).strip().lower() in {"1", "true", "yes", "on"}
    dry_run = bool(dry_run)
    roster_bytes = await roster.read()
    zip_bytes = await photos.read()
    if not roster_bytes:
        raise HTTPException(status_code=400, detail="Roster CSV is empty.")
    if not zip_bytes:
        raise HTTPException(status_code=400, detail="Photos ZIP is empty.")
    if len(zip_bytes) > int(settings.bulk_enroll_max_zip_mb) * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Photos ZIP exceeds {settings.bulk_enroll_max_zip_mb} MB. Split the batch.",
        )

    try:
        text = roster_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = roster_bytes.decode("latin-1")
        except Exception:
            raise HTTPException(status_code=400, detail="Roster CSV must be UTF-8 text.")

    rows, problems = parse_roster(text)
    if problems:
        raise HTTPException(status_code=400, detail={"error_code": "invalid_roster", "problems": problems})
    if not rows:
        raise HTTPException(status_code=400, detail="Roster contains no data rows.")
    if len(rows) > int(settings.bulk_enroll_max_rows):
        raise HTTPException(
            status_code=413, detail=f"Roster has {len(rows)} rows; the limit is {settings.bulk_enroll_max_rows}."
        )

    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        if row["user_id"] in seen and row["user_id"] not in duplicates:
            duplicates.append(row["user_id"])
        seen.add(row["user_id"])
    if duplicates:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "duplicate_roster_rows", "worker_ids": duplicates[:20]},
        )

    now = datetime.now().strftime(_TS)
    with db(write=True) as conn:
        cursor = conn.execute(
            "INSERT INTO enrollment_jobs (kind, status, created_by, created_at, source_name, dry_run, total) "
            "VALUES ('bulk_csv_zip', ?, ?, ?, ?, ?, ?)",
            ("queued", current.id, now, roster.filename or "roster.csv", 1 if dry_run else 0, len(rows)),
        )
        job_id = int(cursor.lastrowid or 0)
        for row in rows:
            conn.execute(
                "INSERT INTO enrollment_job_items (job_id, worker_id, name, role, email, phone, photo_name, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    row["user_id"],
                    row["name"],
                    row["role"],
                    row["email"],
                    row["phone"],
                    row["photo"] or f"{row['user_id']}.jpg",
                    ITEM_QUEUED,
                    now,
                ),
            )
        _audit(
            conn,
            action="enrollment_bulk_started",
            actor=current,
            entity="enrollment_jobs",
            entity_id=str(job_id),
            after={"rows": len(rows), "dry_run": bool(dry_run), "zip_bytes": len(zip_bytes)},
            request=request,
        )

    staging = _staging_dir(job_id)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "roster.csv").write_bytes(roster_bytes)
    (staging / "photos.zip").write_bytes(zip_bytes)

    # BackgroundTasks runs the sync function in a threadpool after the response is
    # sent. At real scale this belongs on a queue (RQ/Celery) so a 500-photo batch
    # cannot compete with live attendance for request threads; the job tables make
    # that swap a deployment change rather than a redesign.
    background.add_task(process_job, job_id)

    return {
        "status": "accepted",
        "job_id": job_id,
        "total": len(rows),
        "dry_run": bool(dry_run),
        "poll": f"/api/v1/admin/enrollment/jobs/{job_id}",
    }


@router.get("/jobs")
async def list_jobs(limit: int = 50, current: CurrentUser = Depends(admin_only)):
    limit = max(1, min(int(limit), 500))
    with db() as conn:
        rows = conn.execute(
            "SELECT id, kind, status, created_by, created_at, started_at, finished_at, source_name, dry_run, "
            "total, processed, succeeded, failed, skipped FROM enrollment_jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/jobs/{job_id}")
async def job_status(job_id: int, include_items: int = 0, current: CurrentUser = Depends(admin_only)):
    with db() as conn:
        job = conn.execute("SELECT * FROM enrollment_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        payload = {"job": dict(job)}
        if include_items:
            items = conn.execute(
                "SELECT id, worker_id, name, status, error_code, error_detail, template_version "
                "FROM enrollment_job_items WHERE job_id = ? ORDER BY id ASC LIMIT 2000",
                (job_id,),
            ).fetchall()
            payload["items"] = [dict(row) for row in items]
    return payload


def main(argv: list[str] | None = None) -> int:
    """Resume a staged bulk job from the command line (``python -m enrollment --job N``)."""
    import argparse

    parser = argparse.ArgumentParser(description="Resume a bulk enrollment job")
    parser.add_argument("--job", type=int, required=True)
    args = parser.parse_args(argv)
    import main as main_module

    main_module.init_db()
    result = process_job(args.job)
    print(json.dumps(result, indent=2))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
