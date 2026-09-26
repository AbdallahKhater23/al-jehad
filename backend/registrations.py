"""Walk-up registration: one permanent public link, one photograph, one administrator's decision.

WHY THIS IS NOT THE REGISTRATION LINK THAT ALREADY SHIPS
--------------------------------------------------------
``enrollment`` issues a link *per person*: an administrator types the name, the role and the
account id, and the link reserves that id the moment it is created. That is the right shape for
one named hire and the wrong one for a walk-up, where nobody has applied yet and so there is no
id to reserve. Here there is one link for the whole site, anybody can submit to it, and nothing
about the applicant is decided until an administrator reads the request.

WHAT THE THREE PARTS ARE FOR
----------------------------
* **The intake switch** (``settings.registration_enabled``) is off by default. A static link
  gets forwarded, and this is the kill switch that closes it without a deploy - on the same
  reasoning as the calibration switch, a public endpoint that collects a face is not something a
  deployment should *discover* it is running.
* **The queue** is the review surface. Submission writes a request and nothing else: no account,
  no roster entry, no shift, no payroll. Approval is the only thing that creates an account, and
  it does so atomically (see ``approve_registration``).
* **The decision** is a human's. A photograph of a face is judged by an administrator looking at
  it, not by a model - which is why no model runs on the public route at all (see below).

WHY THE PUBLIC ROUTE RUNS NO MODELS
-----------------------------------
Two reasons, and both are about the deployment rather than about elegance. A public endpoint that
runs an ONNX inference is a way to keep the one vCPU that also serves the gate busy for free; and
this application's position on uploads is that liveness can only ever be *advisory* here, because
a file on disk cannot be proven live. So the public route streams the body to disk through the
shared upload policy, checks the text, and inserts a row. The face is decoded and embedded exactly
once, at review, inside the face-engine pool - where the reviewer's decision is what the model
work is *for*.

WHY THE ACCOUNT ID IS NOT PROMISED UNTIL THE DECISION
-----------------------------------------------------
The id is allocated at approval, inside the same write transaction that inserts the account, by
``security.lowest_free_id``. A number told to the applicant at submission would be a promise this
application cannot keep: ids are recycled, another request may take it in the meantime, and an
applicant who was told "you are 43" and then hired as 51 is a worse experience than one who was
told nothing. The answer they get is "we have your request", and the number arrives with the
account.

The allocator is the same one the ``kind=register`` invite flow reserves through, and it counts a
number a live invite is holding as taken (``security.reserved_account_ids``). A walk-up approval
and an invite can therefore never be handed the same id, whichever order they run in: an id a link
already promised is not "free" until that link is revoked, expires or is claimed.

ONE PHOTOGRAPH IS ONE REQUEST, ONE DECISION DESTROYS THE PHOTO
--------------------------------------------------------------
The same upload cannot become two pending requests - a script with one JPEG gets one row instead
of filling the review queue with copies of it (the partial unique index in migration 24). And a
rejected photo is wiped rather than filed: an intake funnel that keeps the faces it turned down
would contradict the retention story this system tells about every other face it stores. An
*approved* photo is destroyed too, once the reference template has been written - the face then
lives in the biometric store, under an immutable id, where ``retention`` already knows how to
find it. Keeping a second copy in an intake directory would be a face nothing sweeps.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

import biometrics
import enrollment
import face_engine
import notifications
import retention
import textguard
import uploads
from config import settings
from database import db, immediate
from rate_limit import limiter
from security import (
    CurrentUser,
    IdSpaceExhausted,
    admin_only,
    hash_password,
    lowest_free_id,
    refuse_developer_role,
    validate_password_strength,
    validate_user_id_for_role,
)

log = logging.getLogger("attendance.registrations")

#: Where a submission's photo waits for a reviewer. Read at import and overridable by the test
#: suite through ``harness.FILE_TREES``, like every other file tree the application writes.
PHOTOS_DIR = str(settings.registration_photos_dir)

#: The one permanent public link. Additive: nothing already served moved.
public_router = APIRouter(prefix="/register", tags=["registration"])
#: The review surface. The same audience as every other administrative read of a person.
admin_router = APIRouter(prefix="/admin/registrations", tags=["registration"])

STATUS_PENDING = "PENDING_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

#: What a walk-up may ask to be. ``enrollment``'s list rather than a copy of it: the reasoning
#: is the same (nobody self-registers as an administrator, because an account that can read the
#: audit trail is not something a forwarded link may mint) and two lists would drift apart.
REGISTRATION_ROLES = enrollment.REGISTER_ROLES

#: What counts as consent on the public form. Deliberately explicit: the field is a checkbox,
#: but the value arrives as a string and ``"false"`` is a string that means no.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The version of the consent wording each submission records. Bump it when the sentence the
#: applicant agreed to changes - the stored value is what makes an old consent readable as a
#: consent to *that* wording rather than to today's.
CONSENT_VERSION = "walk-up-registration-v1"

_TS = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# paths resolved at call time
# ---------------------------------------------------------------------------
def photos_dir() -> str:
    """The directory a submission's photo waits in, created on first use.

    Read at call time, like ``quick_links.photos_dir`` and ``punch_frames.frames_dir``, so the
    test suite's one redirect carries this tree along with the database.
    """
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    return PHOTOS_DIR


# ---------------------------------------------------------------------------
# audit + notification helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime(_TS)


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
    """Append an administrative event. Never raises - see ``enrollment._audit``."""
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
                _now(),
            ),
        )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# reading a request for a reviewer
# ---------------------------------------------------------------------------
def _as_review(row: sqlite3.Row) -> dict[str, Any]:
    """One request as the review surface reads it.

    The credential is not here at all - the column is not selected rather than selected and
    dropped, so there is no path on which a hash reaches a response body. The photo is not here
    either: it is served by its own route, so a list of forty requests does not carry forty
    faces, and so a reader without the row's id cannot fetch the bytes.
    """
    item = {
        "id": int(row["id"]),
        "status": row["status"],
        "full_name": row["full_name"],
        "phone": row["phone"],
        "email": row["email"],
        "requested_role": row["requested_role"],
        "work_details": row["work_details"],
        "assigned_id": row["assigned_id"],
        "submitted_ip": row["submitted_ip"],
        "consent_version": row["consent_version"],
        "created_at": row["created_at"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "decision_note": row["decision_note"],
        # Whether the photo is still on disk, so the console knows whether to ask for it. It is
        # not "was one submitted": a decided request's photo has been destroyed on purpose.
        "has_photo": bool(str(row["photo_path"] or "").strip()),
        "photo_bytes": int(row["photo_bytes"] or 0),
    }
    return item


def _load(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM registration_requests WHERE id = ?", (int(request_id),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Registration request not found.")
    return row


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------
def sweep_orphan_photos() -> int:
    """Remove photos no request points at, and say how many.

    A submission writes its photo *before* it inserts the row that names it - deliberately, so a
    row never points at a file that does not exist - which means a crash in between leaves a file
    with nothing behind it. Every other path removes its own photo, so what is left for this is
    that crash and a file somebody copied into the directory. One pass at startup is enough.

    It is deliberately narrow: a file is removed only when **no row names it**, whatever that
    row's status, and only when it is older than ``registration_photo_stale_hours``. A pending
    request's photo is never touched, and neither is a decided one's - that is what the stale
    window is for, and a photo that is still somebody's evidence must not vanish because a
    restart happened.
    """
    directory = PHOTOS_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    with db() as conn:
        rows = conn.execute("SELECT photo_path FROM registration_requests").fetchall()
    referenced = {
        os.path.basename(str(row["photo_path"]))
        for row in rows
        if str(row["photo_path"] or "").strip()
    }
    cutoff = datetime.now().timestamp() - float(settings.registration_photo_stale_hours) * 3600.0
    removed = 0
    for name in names:
        if name in referenced:
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) > cutoff:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("removed %s registration photo(s) with no request behind them", removed)
    return removed


def _destroy_photo(path: str) -> bool:
    """Wipe one submitted photo. ``True`` when it is gone (or never existed).

    ``retention.wipe_file`` rather than ``os.remove``: a face that has been *decided on* is
    residue, not a temporary file, and it is overwritten before it is unlinked for the same
    reason every biometric file in this application is. A wipe that fails is reported rather
    than swallowed - the caller keeps the row's reference to it so the sweep can try again, and
    an operator can see which one it was.
    """
    name = os.path.basename(str(path or ""))
    if not name:
        return True
    candidate = os.path.join(PHOTOS_DIR, name)
    if not os.path.exists(candidate):
        return True
    try:
        retention.wipe_file(candidate, directory=PHOTOS_DIR)
        return True
    except (OSError, ValueError) as exc:
        log.warning("could not wipe the registration photo %s (%s)", name, exc)
        return False


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class Decision(BaseModel):
    """What an administrator may add to a decision: a sentence, and nothing else.

    The id, the role and the name come from the applicant's own submission; the one thing a
    reviewer contributes is the *reason*, which is shown to the applicant on a rejection and kept
    on the row for an auditor. ``prose`` rather than ``identifier``: this is writing.
    """

    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(
            value, field="Note", max_length=textguard.MAX_NOTE, allow_empty=True
        )


# ---------------------------------------------------------------------------
# the public link
# ---------------------------------------------------------------------------
@public_router.get("")
@limiter.limit(settings.registration_rate_limit)
async def registration_intake(request: Request):  # noqa: ARG001 - the limiter needs it
    """What the public form has to satisfy, and whether intake is open at all.

    Public by necessity: the person filling the form in has no account, which is the entire point
    of the form. What it exposes is the *policy* - the upload ceiling, the accepted formats, the
    roles somebody may ask for, the shortest password - and no data of any kind: not a count of
    the queue, not a name, not an id.

    ``enabled: false`` still answers 200 rather than 404. A permanent link that has been
    switched off has to be able to *say* it is switched off, or an applicant sees a broken page
    and tries again tomorrow.
    """
    return {
        "enabled": bool(settings.registration_enabled),
        "roles": list(REGISTRATION_ROLES),
        "photo_policy": uploads.policy(),
        "min_password_length": settings.min_password_length,
        "consent_version": CONSENT_VERSION,
        "message": (
            "Submit your details and a photo. An administrator reviews every request before an "
            "account is created."
            if settings.registration_enabled
            else "Registration is closed at the moment. Ask your site administrator to open it."
        ),
    }


@public_router.post("")
@limiter.limit(settings.registration_rate_limit)
async def submit_registration(
    request: Request,
    full_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="worker"),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    work_details: str = Form(default=""),
    consent: str = Form(default=""),
    photo: UploadFile = File(...),
):
    """Accept one walk-up registration. Creates no account and promises no id.

    The order is the argument. Everything that can be refused is refused before a file is
    written: the switch, the text, the role, the consent and the password. Then the photo is
    streamed to disk through the shared policy - one chunk of memory whatever its size - and the
    row is inserted. Any failure after the file exists takes the file with it, so a refused
    submission cannot leave a face in the reviewers' directory for nobody to look at.

    The cap is enforced **inside the insert's transaction**, together with the insert: a count
    read before the write is advice, and N concurrent submissions would each read ``cap - 1``.
    """
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "registration_closed",
                "message": "Registration is closed at the moment. Ask your site administrator.",
            },
        )

    try:
        full_name = textguard.identifier(
            full_name, field="Full name", max_length=textguard.MAX_NAME
        )
        phone = textguard.contact(phone, field="Phone")
        email = textguard.contact(email, field="Email")
        work_details = textguard.prose(
            work_details, field="Work details", max_length=textguard.MAX_NOTE, allow_empty=True
        )
    except ValueError as exc:
        raise textguard.http_error(exc) from None

    role = str(role or "").strip().lower()
    if role not in REGISTRATION_ROLES:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "role_not_available",
                "message": (
                    "This link can register a worker or a lead worker (moallem) only. An "
                    "administrator account is created by an administrator."
                ),
            },
        )
    # Belt and braces: the role is already restricted to the two business roles above, and the
    # root tier must be refused *by name* on every path that could mint one rather than by an
    # accident of a list.
    refuse_developer_role(role)

    if str(consent or "").strip().lower() not in _TRUTHY:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "consent_required",
                "message": (
                    "Please confirm that you agree to your photograph being stored for "
                    "attendance verification."
                ),
            },
        )

    # The same policy an administrator's password obeys. This is the one password nobody can
    # reset for the applicant, so a weaker rule here would be a hole with a name on it.
    validate_password_strength(password)

    stored = await uploads.store_photo(photo, photos_dir(), field="photo", prefix="req-")
    try:
        password_hash = hash_password(password)
        with immediate() as conn:
            waiting = int(
                conn.execute(
                    "SELECT COUNT(*) FROM registration_requests WHERE status = ?",
                    (STATUS_PENDING,),
                ).fetchone()[0]
            )
            cap = int(settings.registration_pending_cap)
            if waiting >= cap:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error_code": "registration_queue_full",
                        "message": (
                            "There are already as many registration requests waiting for review "
                            "as this site accepts. Please try again later, or speak to your "
                            "administrator."
                        ),
                    },
                )
            stamp = _now()
            try:
                cursor = conn.execute(
                    "INSERT INTO registration_requests (status, full_name, phone, email, "
                    "requested_role, work_details, password_hash, photo_path, photo_sha256, "
                    "photo_bytes, photo_mime, submitted_ip, consent_at, consent_version, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        STATUS_PENDING,
                        full_name,
                        phone,
                        email,
                        role,
                        work_details,
                        password_hash,
                        stored.path,
                        stored.sha256,
                        int(stored.size),
                        stored.mime,
                        request.client.host if request.client else None,
                        stamp,
                        CONSENT_VERSION,
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError:
                # The partial unique index, and the only constraint it can be: the same upload
                # cannot be pending twice. Answering 409 says the truth (we already have this
                # photograph) instead of pretending a second row was created.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "registration_duplicate",
                        "message": (
                            "This photograph has already been submitted and is waiting for "
                            "review. You do not need to send it again."
                        ),
                    },
                ) from None
            request_id = int(cursor.lastrowid or 0)
            _audit(
                conn,
                action="registration_submitted",
                actor=None,
                entity="registration_requests",
                entity_id=str(request_id),
                after={
                    "requested_role": role,
                    "photo_sha256": stored.sha256,
                    "photo_bytes": int(stored.size),
                    "consent_version": CONSENT_VERSION,
                },
                request=request,
            )
            notifications.notify(
                conn,
                kind=notifications.KIND_REGISTRATION_SUBMITTED,
                severity=notifications.SEVERITY_INFO,
                title="Registration waiting for review",
                body=(
                    f"{full_name} asked to register as a {role}. Review the request in the "
                    "Registrations queue; no account exists until you approve it."
                ),
                payload={"request_id": request_id, "requested_role": role},
                dedupe_key=f"registration_submitted:{request_id}",
            )
    except BaseException:
        # Every refusal after the file was written, and every cancellation, takes the file with
        # it - including the two the database refused above.
        stored.discard()
        raise

    return {
        "status": "success",
        "request_id": request_id,
        "message": (
            "Your registration request has been received. An administrator will review it and "
            "you will be told how to sign in once it is approved."
        ),
    }


# ---------------------------------------------------------------------------
# the review surface
# ---------------------------------------------------------------------------
@admin_router.get("")
async def list_registrations(
    status: str = STATUS_PENDING,
    limit: int = 200,
    current: CurrentUser = Depends(admin_only),
):
    """The review queue: oldest first, because a queue nobody reads in order is a queue that
    silently starves whoever applied first.

    ``status=all`` is the audit view; the default is the work to do. The count of everything
    still pending comes back with the page, so a console can show a badge without asking twice -
    and it is counted separately from ``limit``, because a badge that said "3" because the page
    was truncated would be a badge that lies.
    """
    wanted = str(status or "").strip()
    limit = max(1, min(int(limit), 1000))
    with db() as conn:
        if wanted and wanted.lower() != "all":
            normalised = wanted.upper()
            if normalised not in STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"status must be one of {', '.join(STATUSES)} or 'all'.",
                )
            rows = conn.execute(
                "SELECT * FROM registration_requests WHERE status = ? ORDER BY id ASC LIMIT ?",
                (normalised, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM registration_requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM registration_requests WHERE status = ?", (STATUS_PENDING,)
            ).fetchone()[0]
        )
    return {
        "status": "success",
        "enabled": bool(settings.registration_enabled),
        "pending": pending,
        "requests": [_as_review(row) for row in rows],
        "count": len(rows),
    }


@admin_router.get("/{request_id}/photo")
async def registration_photo(
    request_id: int, current: CurrentUser = Depends(admin_only)
):  # noqa: ARG001 - the guard is the point
    """The submitted photograph, for the reviewer who has to judge it.

    A route of its own rather than a field on the list: forty requests in one response would
    otherwise carry forty faces, and the bytes are needed exactly once - by the person looking
    at the one request in front of them.

    ``no-store`` on the response, because a face is not a document a proxy should keep - and an
    approved request's photo is destroyed rather than served, which this answers honestly (404)
    rather than with a broken image.
    """
    with db() as conn:
        row = _load(conn, request_id)
    path = str(row["photo_path"] or "").strip()
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="This request has no photo on file.")
    response = FileResponse(path, media_type=str(row["photo_mime"] or "application/octet-stream"))
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_router.post("/{request_id}/approve")
async def approve_registration(
    request: Request,
    request_id: int,
    payload: Decision | None = None,
    current: CurrentUser = Depends(admin_only),
):
    """Hire the applicant: allocate an id, create the account, file the face, and say so.

    THE ORDER, AND WHY IT IS THIS ORDER
    -----------------------------------
    1. **The model work, outside the write lock.** The photo is decoded, the face is detected and
       the embedding is computed while the application holds *no* lock - an inference takes long
       enough that holding SQLite's single write lock across it would queue a punch at the gate
       behind a review. The work is submitted to the face-engine pool, the same one the punch
       uses, so a burst of reviews cannot run more inferences than a burst of punches.
    2. **One transaction**: the id is allocated for the applicant's role, the account is inserted
       with a freshly minted biometric id, and the request is moved to ``APPROVED`` with the id it
       was given. The compare-and-set (``WHERE status = ? AND status is still pending``) is what
       makes two administrators clicking approve at the same time produce one account rather than
       two: the second sees zero rows updated and is answered 409.
    3. **The template, after the commit.** ``biometrics.write_reference`` reads the account's
       biometric id from the database through its own connection, so it cannot run inside a
       transaction this process is holding - it would wait on the lock it is standing behind.
       Writing it *after* the commit is also the honest order: the account exists first, and a
       failure here is a warning an operator can act on rather than a half-created account.

    Nothing is created before the decision: no ``users`` row, no roster entry, no shift, no
    payroll row.
    """
    with db() as conn:
        row = _load(conn, request_id)
    if row["status"] != STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "already_reviewed",
                "message": (
                    f"This request was already {row['status'].lower().replace('_', ' ')} "
                    f"by {row['reviewed_by']} at {row['reviewed_at']}."
                ),
            },
        )

    role = str(row["requested_role"] or "").strip().lower()
    if role not in REGISTRATION_ROLES:
        # Only reachable if the row was edited by hand. Refuse rather than mint an account in a
        # role this surface was never allowed to hand out.
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "role_not_available",
                "message": "This request asks for a role that cannot be created here.",
            },
        )

    photo_path = str(row["photo_path"] or "").strip()
    if not photo_path or not os.path.exists(photo_path):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "registration_photo_missing",
                "message": (
                    "The photograph for this request is no longer on file, so there is nothing to "
                    "build a face reference from. Reject it and ask the applicant to submit again."
                ),
            },
        )

    # 1. The model work, under the same bounded pool the gate uses. ``face_frame`` takes the
    #    *path*, so the encoded photo is never held as bytes here at all: PIL opens the file it
    #    is pointed at, and what this holds afterwards is the decoded frame.
    try:
        image = uploads.face_frame(photo_path, field="registration photo")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "registration_photo_unreadable",
                "message": "The submitted photo could not be read. Reject it and ask for another.",
            },
        ) from None

    try:
        decision, embedding = await face_engine.ENGINE.run_async(
            enrollment.embed_reference, image, stage="registration_review"
        )
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None

    # 2. The account, the allocated id and the verdict, in one write.
    note = (payload.note if payload else None) or None
    stamp = _now()
    assigned = ""
    try:
        with immediate() as conn:
            try:
                assigned = lowest_free_id(conn, role)
            except IdSpaceExhausted as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "id_space_exhausted",
                        "message": (
                            f"{exc} Approve (or free) an account in that range first, or widen "
                            "the range in the configuration."
                        ),
                    },
                ) from None
            # The allocator is range-scoped by construction; this is the second half of the same
            # rule, and the one the console and the roster import share, so an id cannot land in
            # another role's block through this door either.
            validate_user_id_for_role(assigned, role)

            claimed = conn.execute(
                "UPDATE registration_requests SET status = ?, assigned_id = ?, reviewed_by = ?, "
                "reviewed_at = ?, decision_note = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    STATUS_APPROVED,
                    assigned,
                    current.id,
                    stamp,
                    note,
                    stamp,
                    int(request_id),
                    STATUS_PENDING,
                ),
            )
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "already_reviewed",
                        "message": "Another administrator reviewed this request first.",
                    },
                )
            conn.execute(
                "INSERT INTO users (id, name, email, phone, password_hash, role, status, "
                "enrolled_at, template_version, biometric_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 1, ?)",
                (
                    assigned,
                    str(row["full_name"]),
                    str(row["email"] or ""),
                    str(row["phone"] or ""),
                    str(row["password_hash"]),
                    role,
                    stamp,
                    # The face about to be filed can never be confused with whoever held this
                    # account id before: the number is recycled, and a previous holder's
                    # leftover file is moved aside here (see ``biometrics.new_account_id``).
                    biometrics.new_account_id(assigned),
                ),
            )
            _audit(
                conn,
                action="registration_approved",
                actor=current,
                entity="registration_requests",
                entity_id=str(request_id),
                after={
                    "worker_id": assigned,
                    "role": role,
                    "liveness": decision.as_payload(),
                    "note": note,
                },
                request=request,
            )
    except sqlite3.IntegrityError:
        # ``lowest_free_id`` and its ``INSERT`` are one transaction, so this can only be a race
        # with a writer that does not use this lock - the primary key is the last line of
        # defence, and answering 409 says what actually happened.
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "id_taken",
                "message": (
                    f"Account {assigned} was taken while this request was being approved. Try "
                    "again."
                ),
            },
        ) from None

    # 3. The template, after the commit. A failure here is not silent: the account exists and can
    #    be enrolled from the console, and the photograph is deliberately kept so that is possible.
    template_written = True
    template_error: str | None = None
    try:
        # Through the threadpool: this writes two files and re-encodes a thumbnail, and the
        # event loop is what serves the gate.
        await run_in_threadpool(biometrics.write_reference, assigned, image, embedding)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal: the account is already created
        template_written = False
        template_error = f"{type(exc).__name__}: {exc}"
        log.exception("registration %s was approved but its face reference could not be written", request_id)
        with db(write=True) as conn:
            notifications.notify(
                conn,
                kind=notifications.KIND_ENROLLMENT_COMPLETED,
                severity=notifications.SEVERITY_WARNING,
                title="A registered worker has no face reference",
                body=(
                    f"Request {request_id} was approved as account {assigned} ({role}), but "
                    f"storing the face reference failed ({template_error}). That account cannot "
                    "clock in until an administrator enrolls them from the console."
                ),
                worker_id=assigned,
                payload={"request_id": int(request_id), "error": template_error},
                dedupe_key=f"registration_template_failed:{request_id}",
            )

    # 4. The face now has a home under an immutable id, so the reviewed photograph is destroyed
    #    rather than left as a second copy nothing sweeps. Only on success, and only after the
    #    write above: while the reference could still fail, the photo is the way to retry.
    if template_written and _destroy_photo(photo_path):
        with db(write=True) as conn:
            conn.execute(
                "UPDATE registration_requests SET photo_path = '', updated_at = ? WHERE id = ?",
                (stamp, int(request_id)),
            )
            _audit(
                conn,
                action="registration_photo_removed",
                actor=current,
                entity="registration_requests",
                entity_id=str(request_id),
                after={"reason": "approved", "worker_id": assigned},
                request=request,
            )

    return {
        "status": "success",
        "request_id": int(request_id),
        "worker_id": assigned,
        "name": str(row["full_name"]),
        "role": role,
        "template_written": template_written,
        "message": (
            f"Account {assigned} created for {row['full_name']}."
            if template_written
            else (
                f"Account {assigned} was created, but the face reference could not be stored. "
                "Enroll this worker from the console before they can clock in."
            )
        ),
    }


@admin_router.post("/{request_id}/reject")
async def reject_registration(
    request: Request,
    request_id: int,
    payload: Decision | None = None,
    current: CurrentUser = Depends(admin_only),
):
    """Turn the applicant down. Creates nothing, and destroys the photograph.

    A refusal is a *product state*, not an error: the row survives with the reason on it, so the
    decision is auditable and the applicant can be told why. What does not survive is the face -
    an intake funnel that keeps the photographs it turned down is the retention violation this
    whole application is built to avoid. The password the applicant chose goes with it: a refusal
    is not a reason to keep somebody's credential.

    The row stays, which is what makes "may I apply again?" answerable: the partial unique index
    that stops one photograph becoming two *pending* requests stops applying here, so the same
    person may submit the same photograph again after a refusal (see migration 24).
    """
    with db() as conn:
        row = _load(conn, request_id)
    if row["status"] != STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "already_reviewed",
                "message": (
                    f"This request was already {row['status'].lower().replace('_', ' ')} "
                    f"by {row['reviewed_by']} at {row['reviewed_at']}."
                ),
            },
        )

    note = (payload.note if payload else None) or None
    stamp = _now()
    photo_path = str(row["photo_path"] or "").strip()
    with immediate() as conn:
        claimed = conn.execute(
            "UPDATE registration_requests SET status = ?, reviewed_by = ?, reviewed_at = ?, "
            "decision_note = ?, updated_at = ?, password_hash = '' WHERE id = ? AND status = ?",
            (STATUS_REJECTED, current.id, stamp, note, stamp, int(request_id), STATUS_PENDING),
        )
        if claimed.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "already_reviewed",
                    "message": "Another administrator reviewed this request first.",
                },
            )
        _audit(
            conn,
            action="registration_rejected",
            actor=current,
            entity="registration_requests",
            entity_id=str(request_id),
            after={"requested_role": row["requested_role"], "note": note},
            request=request,
        )

    destroyed = _destroy_photo(photo_path) if photo_path else True
    if destroyed:
        with db(write=True) as conn:
            conn.execute(
                "UPDATE registration_requests SET photo_path = '', updated_at = ? WHERE id = ?",
                (stamp, int(request_id)),
            )
            _audit(
                conn,
                action="registration_photo_removed",
                actor=current,
                entity="registration_requests",
                entity_id=str(request_id),
                after={"reason": "rejected"},
                request=request,
            )

    return {
        "status": "success",
        "request_id": int(request_id),
        "photo_destroyed": destroyed,
        "message": (
            "The request was refused and the photograph has been destroyed."
            if destroyed
            else (
                "The request was refused, but the photograph could not be removed and is still "
                "on disk. Remove it by hand."
            )
        ),
    }
