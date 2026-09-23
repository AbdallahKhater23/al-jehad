"""Site attendance API - FastAPI application.

WHAT CHANGED IN THIS REVISION, AND WHY
--------------------------------------
The shipped version of this module authenticated nothing. Identity travelled in
the request *payload* (``admin_id``, ``creator_id``, ``worker_id``), so any caller
could name the identity it wanted, and the 16 admin endpoints answered anonymous
requests. This revision replaces that with:

* a JWT access token issued by ``/api/v1/auth/login`` and validated on every
  protected route by ``require_role``;
* identity read from the database on *every* request, so a deleted user, a
  demoted admin and a rotated password all take effect immediately - the
  per-process ``USER_CACHE`` dictionary is gone, and with it the bug where a
  rotation stayed invisible on every other ASGI worker;
* no client-supplied identity fields anywhere: the Pydantic models no longer
  declare them. They are *ignored* rather than rejected, so the existing
  front-end, which still sends ``admin_id``, keeps working;
* the Twilio/WhatsApp alert path replaced by rows in ``admin_notifications``;
* interactive API docs disabled by default, the duplicate unprefixed router
  removed, and the ``sites/delete`` contract bug fixed;
* a startup self-test that refuses to serve traffic when a critical check fails.

``init_db()`` remains a public function because operations and tests call it, but
it is now idempotent: the old routine rewrote an existing user's password with a
hardcoded demo value on every single start.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import warnings
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Mapping

import numpy as np
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from scipy.spatial.distance import cosine
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

# ---------------------------------------------------------------------------
# import path bootstrap
#
# ``database``, ``security``, ``enrollment`` and friends are *top-level* modules, so
# they only import when ``backend/`` is on sys.path. That holds for `serve.py` and for
# pytest, but not for every way an operator starts the app (`uvicorn main:app` from the
# repository root, an IDE run configuration, a WSGI/ASGI runner). Rather than fail with
# "ModuleNotFoundError: No module named 'enrollment'", the package directory is added
# based on this file's own location, which cannot be wrong.
# ---------------------------------------------------------------------------
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

#: Failures a worker is never shown in the raw (an unusable frame, an exception inside
#: the face check) are logged here instead, so the operator keeps the reason the worker
#: does not get.
log = logging.getLogger("attendance.api")

import biometrics
import branding
import corpus
import database
import developer
import enrollment
import face_detector
import face_engine
import liveness
import migrations
import netguard
import notes
import notifications
import offline_sync
import overtime
import punch_frames
import push
import quick_links
import readiness
import reports
import retention
import schema_guard
import security
import telemetry
import textguard
import shift_hours
import shift_windows
import uploads
from config import BACKEND_DIR, PROJECT_ROOT, settings
from database import db
from rate_limit import limiter
from security import (
    CurrentUser,
    admin_only,
    any_authenticated,
    create_access_token,
    hash_password,
    pwd_context,
    require_role,
    validate_password_strength,
    verify_password,
)


# ---------------------------------------------------------------------------
# Responses that are already JSON
# ---------------------------------------------------------------------------
def _json(payload) -> JSONResponse:
    """Hand back a payload that is already JSON, without FastAPI walking it first.

    FastAPI runs every non-``Response`` return value through ``jsonable_encoder``, which
    walks the structure one value at a time - an ``isinstance``/``dataclass`` test per leaf.
    On these row payloads that walk changes no byte: every value is a dict, a list, a
    string, a number or ``None``, straight out of SQLite. It is also, measured on this
    machine, one of the largest costs of the list endpoints - ~6.6 ms for the 129 KB
    ``/admin/pending_reviews`` body and ~2.5 ms for ``/admin/logs`` - for work that produces
    an identical response.

    Safe exactly while those payloads stay JSON-native, which is what
    ``tests/test_json_payloads_skip_the_encoder.py`` asserts (``jsonable_encoder(payload) ==
    payload``, and ``json.dumps`` succeeding). A ``datetime`` or a ``Decimal`` reaching a row
    must fail there and be fixed at the source rather than becoming a 500 here. Same
    reasoning, and the same shape, as ``reports._encoded``.
    """
    return JSONResponse(content=payload)

# ---------------------------------------------------------------------------
# Application, limiter, static paths
# ---------------------------------------------------------------------------
# The Limiter is shared (``rate_limit.py``) rather than built here: slowapi's 429 handler
# only renders for the instance on ``app.state.limiter``, so a second instance in
# another module would silently produce routes whose rate limit raises an unhandled
# error instead of a friendly 429.

# ``docs_url=None`` and friends are deliberate: the generated documentation
# enumerates the whole admin surface for an anonymous visitor. An operator can
# re-enable it with ENABLE_API_DOCS=1 on a trusted network.
_DOCS = settings.enable_api_docs
app = FastAPI(
    title="Site Attendance API",
    version=settings.app_version,
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
router = APIRouter()

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# The network layer: CORS by origin class, the admin address gate, and the security
# headers. One middleware, because all three read the same request facts (method, path,
# Origin, peer address) and parsing X-Forwarded-For three times is how it gets parsed
# wrong once. Replaces ``CORSMiddleware``, whose single origin list served both the worker
# app and the administrator console - see ``netguard`` for what that cost.
#
# Order matters: middleware added later wraps earlier ones. This one is added here, before
# the routes, so it sits inside the frontend revalidation middleware (a refusal still gets
# its Cache-Control) and outside the router (a refusal never reaches a handler).
netguard.install(app)

FRONTEND_DIR = str(PROJECT_ROOT / "frontend")
#: The two biometric trees, named from the settings rather than from a literal path. The point
#: is the *child process*: a script or a second worker inherits ``LOCAL_REFS_DIR`` /
#: ``WORKER_PHOTOS_DIR`` from the environment and gets the same answer the server has, where a
#: path baked into this file would have sent it to the checkout - writing faces nobody looks at.
#: A test repoints these attributes (see ``harness.FILE_TREES``) and every reader asks for them
#: at call time, which is what lets one repoint carry the rotation to all of them.
LOCAL_REFS_DIR = str(settings.local_refs_dir)
WORKER_PHOTOS_DIR = str(settings.worker_photos_dir)

# The cosine distance bands are **not** here. They belong to the pipeline that produced the
# embedding - a distance means nothing without the crop it was measured on - so they live in
# ``face_detector.MatchBand``, one band per ``(pipeline, model)``, each derived from measured
# genuine/impostor boundaries and none of them inherited from the previous crop. Read one with
# ``face_detector.band_for(pipeline)`` and decide with ``band.classify(distance)``; the two
# numbers used to sit here as 0.40/0.60 with no record of what they were measured against.

DEFAULT_SHIFT_RULES: dict[str, Any] = {
    "clock_in_window_start": "04:00",
    "clock_in_window_end": "06:30",
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    # A zone the runtime can actually resolve. This used to say "KUWAIT", which is not an
    # IANA key: ``ZoneInfo`` raises for it, so ``shift_windows.resolve_timezone`` fell back to
    # ``DEFAULT_TIMEZONE`` (Asia/Kuwait, below in ``shift_windows``) on every punch while the
    # console displayed the word KUWAIT as if it were the site's clock. The two agreed by
    # accident and only because the fallback happened to be the zone we meant - a silent
    # substitution is indistinguishable from a real setting until someone edits the other one.
    "site_timezone": "Asia/Kuwait",
    # The unpaid break and the end of the paid day. A full day is 8 h paid plus a
    # 30-minute unpaid break (8.5 h on site); ``shift_hours.py`` is the only module that
    # turns those numbers into money, and every path that closes a shift asks it.
    "break_minutes": 30.0,
    "break_after_hours": 4.0,
    "auto_close_at_regular": 1,
}

ACTION_CLOCK_IN = "Clock In"
ACTION_CLOCK_OUT = "Clock Out"

STATUS_APPROVED = "Approved"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_PENDING_OVERTIME = "Pending Overtime Approval"
#: Written by ``overtime.scan_auto_close`` when a shift reaches its paid limit. Its
#: status *code* is its own (``auto_closed_8h``) and counts as payable at the hours the
#: system recorded, unlike the 11 h-era ``auto_closed`` rows, which still need a decision.
STATUS_AUTO_CLOSED = migrations.STATUS_AUTO_CLOSED_LABEL
STATUS_CODE_AUTO_CLOSED = migrations.STATUS_CODE_AUTO_CLOSED_8H
#: Written by ``reject_review`` when the hours past the regular day are refused. The
#: standard day stays payable and the overtime is recorded as zero - see
#: ``migrations.STATUS_OVERTIME_REJECTED_LABEL`` for why it is not simply ``approved``.
STATUS_OVERTIME_REJECTED = migrations.STATUS_OVERTIME_REJECTED_LABEL
STATUS_TYPE_REJECTED = migrations.STATUS_REJECTED_LABEL
STATUS_FORCED_IN = "Force Clocked In by Admin"
STATUS_FORCED_OUT = "Force Clocked Out by Admin"


# ---------------------------------------------------------------------------
# database bootstrap
# ---------------------------------------------------------------------------
def init_db() -> dict:
    """Create the baseline tables, apply migrations, and bootstrap an admin.

    Idempotent by construction: it never rewrites the password of an existing
    user, and it creates a head admin only when the database has none. Safe to
    call from a request handler, a test fixture or a migration script.
    """
    pragma = database.configure(repair=True)
    conn = database.connect(isolation_level=None)
    try:
        summary = migrations.initialize(conn)
    finally:
        conn.close()

    # Not a migration, deliberately. Migration 11 mints an id for every account; this
    # gives the files already on disk the name that id asks for. It is kept out of the
    # migration because migrations must stay pure, additive statements about the schema
    # that tools can replay against an in-memory copy (the drift guard does exactly
    # that), while this is idempotent filesystem work that a running backup can interrupt
    # and the next boot can finish. A failure here is logged and survived: the readers
    # fall back to the legacy names, so a rename that cannot happen must never stop the
    # server from starting.
    adoption: dict = {}
    try:
        adoption = {
            "biometric_files": biometrics.adopt_legacy_files(),
            "punch_photos": quick_links.adopt_legacy_photo_names(),
        }
    except Exception as exc:  # noqa: BLE001 - startup must not depend on a rename
        log.warning("biometric file adoption failed: %s", exc)
        adoption = {"error": f"{type(exc).__name__}: {exc}"}
    return {**summary, "journal_mode": pragma.get("journal_mode"), "adoption": adoption}


def get_shift_rules() -> dict:
    """Current shift rules, falling back to the documented defaults."""
    rules = dict(DEFAULT_SHIFT_RULES)
    try:
        with db() as conn:
            row = conn.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        for key in DEFAULT_SHIFT_RULES:
            try:
                value = row[key]
            except (IndexError, KeyError):
                continue
            # A blank column is "nothing configured" and keeps the shipped value. These
            # columns are NOT NULL with a default, so an administrator emptying the window
            # boxes in the console stores ``''`` rather than NULL - and a ``''`` read back as a
            # real value would resolve to 00:00-23:59 at every punch, i.e. a company window
            # that makes nobody late. ``shift_windows._pick`` reads blank the same way.
            if value is not None and str(value).strip() != "":
                rules[key] = value
    return rules


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    before: Any = None,
    after: Any = None,
    request: Request | None = None,
) -> None:
    """Append an administrative event. Never raises.

    ``audit_log`` is append-only *in the database* (triggers reject UPDATE and
    DELETE), so a correction has to be appended as a new event rather than
    silently rewriting history.
    """
    ip = user_agent = None
    if request is not None:
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
    try:
        conn.execute(
            """
            INSERT INTO audit_log
                (actor_id, actor_role, action, entity, entity_id, before_json, after_json, ip, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor.id if actor else None,
                actor.role if actor else None,
                action,
                entity,
                str(entity_id) if entity_id is not None else None,
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                ip,
                user_agent,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    except sqlite3.Error:
        pass


def _record_refused_punch(
    *,
    worker_id: str,
    site_name: str | None,
    action: str,
    error_code: str,
    score: float | None,
    punch_frame: str | None,
    source: str,
    biometric_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Keep a refused punch's evidence for triage, instead of throwing it away.

    The refusal path has the frame and the score in hand and, before this table, discarded
    both: an operator whose workers were refused all day could see neither. This is the
    write-half of that surface (the read-half is ``/admin/refused_punches``).

    ``conn=None`` (the punch path's call) opens its own short write transaction, because the
    caller is raising a 422 and has no open transaction to lend. With a connection the row
    joins the caller's transaction, which is how the offline-sync refusals write.

    Never raises to its caller for its own work: the punch's 422 is the worker's answer and
    must not depend on whether the evidence was kept. (Disk failures mid-``store_frame`` are
    the caller's problem - the frame arrives here already stored or ``None``.)
    """
    try:
        pipeline = biometrics.current_pipeline()
    except Exception:  # noqa: BLE001 - a pipeline name is decoration on a refusal record
        pipeline = None
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write(target: sqlite3.Connection) -> int:
        cursor = target.execute(
            """
            INSERT INTO refused_punches
                (worker_id, biometric_id, site_name, action, error_code, score,
                 pipeline, source, punch_frame, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker_id,
                biometric_id,
                site_name,
                action,
                error_code,
                score,
                pipeline,
                source,
                punch_frame,
                stamp,
            ),
        )
        return int(cursor.lastrowid)

    if conn is not None:
        return _write(conn)
    with db(write=True) as own:
        return _write(own)


def _insert_log(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    site_name: str,
    action: str,
    timestamp: str,
    hours: float,
    score: float,
    status: str,
    status_code: str,
    lat: float | None = None,
    lon: float | None = None,
    accuracy: float | None = None,
    source: str = "online",
    liveness_class: str | None = None,
    liveness_score: float | None = None,
    flag_reason: str | None = None,
    approved_hours: float | None = None,
    overtime_hours: float | None = None,
    break_hours: float | None = None,
    punch_frame: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO attendance_logs
            (worker_id, site_name, action, timestamp, hours, score, status, status_code,
             lat, lon, accuracy, source, liveness_class, liveness_score, flag_reason,
             approved_hours, overtime_hours, break_hours, punch_frame)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            worker_id, site_name, action, timestamp, hours, score, status, status_code,
            lat, lon, accuracy, source, liveness_class, liveness_score, flag_reason,
            approved_hours, overtime_hours, break_hours, punch_frame,
        ),
    )
    return int(cursor.lastrowid or 0)


# ---------------------------------------------------------------------------
# Why there is no hard cutoff
# ---------------------------------------------------------------------------
# Earlier revisions force-closed (``enforce_11h_cutoff``) any session past an
# 11 h cutoff, writing a clock-out the worker never made. That invented hours: a
# shift nobody closed was capped rather than reported, so somebody who genuinely
# worked 13 h was paid for 11, and the missing 2 h were invisible until someone
# asked. The close was removed deliberately: a forgotten shift stays open and
# keeps counting, the overtime watcher still alerts the administrator at
# ``overtime_notify_hours``, and the closing decision belongs to a human (the
# worker clocks out, or an admin force-clocks-out).
# ---------------------------------------------------------------------------
# request models
#
# Note what is NOT here: ``admin_id``, ``creator_id``. Identity comes from the
# bearer token. Pydantic ignores unexpected keys, so a legacy client that still
# sends them is not rejected - the claim is simply discarded.
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    user_id: str
    email_or_phone: str
    password: str


class UserAddRequest(BaseModel):
    """A new account, created by an administrator.

    ``name`` is the field that matters here. It is stored, echoed into the audit entry, and
    printed in every notification that mentions this person - so it is an *identifier* under
    the ``textguard`` allowlist rather than free text: letters (English or Arabic), digits,
    spaces and ``. , - _ ( ) /``. See that module for why an apostrophe is refused and why
    the Arabic ranges are listed block by block instead of relying on ``\\w``.

    ``password`` is deliberately untouched by any of this. It is hashed and never rendered,
    and a validator that trimmed or restricted characters in it would quietly weaken every
    credential in the system; ``security.validate_password_strength`` is the check for it, and
    it answers a different question.
    """

    user_id: str
    name: str
    email: str = ""
    phone: str = ""
    password: str
    role: str

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        return textguard.identifier(value, field="Name", max_length=textguard.MAX_NAME)

    @field_validator("email")
    @classmethod
    def _plain_email(cls, value: str) -> str:
        return textguard.contact(value, field="Email")

    @field_validator("phone")
    @classmethod
    def _plain_phone(cls, value: str) -> str:
        return textguard.contact(value, field="Phone")


def _plain_hhmm(value: str | None, *, field: str) -> str | None:
    """A strict 24-hour ``HH:MM``, or ``None`` for "not configured".

    An empty string is read as "not configured" rather than as an error, because that is what
    an HTML form sends for a time box nobody filled in - and rejecting it would make the
    console's own forms stop saving. Every other shape is refused here, where an administrator
    sees it, instead of being stored and then silently ignored at the gate: a start of
    ``25:00`` stored in ``shift_rules`` used to degrade to ``00:00``-``23:59`` at every punch,
    which is a window that makes nobody late (``shift_windows`` still falls back if one ever
    gets in by another route).

    Shared by the site form and the company-wide rules on purpose: those two are the same
    question asked at two scopes, and a rule that lived in only one of them is how a site ends
    up validated and the company default not.
    """
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if not shift_windows.is_valid_hhmm(candidate):
        raise ValueError(
            f"{field} must be a 24-hour time in HH:MM form (for example 04:00, 22:00, "
            f"00:00); '{value}' is not. Minutes are 00-59 and hours are 00-23."
        )
    return candidate


def _plain_timezone(value: str | None) -> str | None:
    """Refuse a timezone ``zoneinfo`` cannot resolve.

    A typo like ``Africa/Cario`` or a POSIX-style ``EET-2`` would otherwise be stored and then
    quietly fall back to the default at every punch - which moves a site's window by an hour or
    two and presents as "the wrong people are late", not as a typo. Applies to the site's zone
    and to the company default for the same reason.
    """
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if not shift_windows.is_known_timezone(candidate):
        raise ValueError(
            f"'{value}' is not a timezone this server knows. Use an IANA name such as "
            "Asia/Kuwait or Asia/Riyadh."
        )
    return candidate


class SiteModel(BaseModel):
    """A construction site, including the shift its people are measured against.

    The three window fields are optional and default to ``None``, which means **inherit from
    the global ``shift_rules``** - per field, so a night site can set only its hours and keep
    the company timezone. That is also why an untouched field in an edit payload must not be
    written as NULL: see ``edit_site``, which distinguishes "absent" from "explicitly cleared".
    """

    site_name: str
    #: A Google Maps URL or a ``lat,lon`` pair, parsed and discarded - never stored, never
    #: rendered. That is why it is not put through the identifier allowlist above: a Maps URL
    #: is full of ``: / ? = &``, and refusing those would refuse the way administrators
    #: actually paste a site's coordinates.
    location_input: str
    radius: float
    clock_in_window_start: str | None = None
    clock_in_window_end: str | None = None
    site_timezone: str | None = None

    @field_validator("site_name")
    @classmethod
    def _plain_site_name(cls, value: str) -> str:
        """A site name is a key, a label in every report, and a notification field.

        It is the primary key of ``construction_sites`` and it is written into
        ``attendance_logs.site_name``, which means a name carrying markup is a stored payload
        in the *attendance* table as well as in the site list - the one row an operator is
        most likely to open in a spreadsheet.
        """
        return textguard.identifier(
            value, field="Site name", max_length=textguard.MAX_SITE_NAME
        )

    @field_validator("clock_in_window_start", "clock_in_window_end")
    @classmethod
    def _validate_window_time(cls, value: str | None) -> str | None:
        """The site's own hours, checked by the shared rule (see ``_plain_hhmm``)."""
        return _plain_hhmm(value, field="Site clock-in window")

    @field_validator("site_timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        """The site's own zone, checked by the shared rule (see ``_plain_timezone``)."""
        return _plain_timezone(value)


class ForceClockRequest(BaseModel):
    worker_id: str
    #: Empty means "the site the administrator picked", so it is allowed to be absent; when
    #: present it must be a name that could have come from the sites table.
    site_name: str = ""
    #: The hours the administrator is *authorising* for this shift, for the close of a shift
    #: whose clock data cannot be trusted to speak for itself - the forgotten clock-out above all:
    #: a worker who left the site at 18:00 and is force-closed at 23:00 did not work ten hours,
    #: and the only person who can name the figure is the one closing the shift.
    #: Absent (the default) keeps the arithmetic exactly as it was - the clock decides. When
    #: stated it must be within ``0..24`` h and at least 0.25 h steps are the console's choice,
    #: not the API's: the server accepts any figure it can mean.
    hours: float | None = None

    @field_validator("site_name")
    @classmethod
    def _plain_site_name(cls, value: str) -> str:
        return textguard.identifier(
            value,
            field="Site name",
            max_length=textguard.MAX_SITE_NAME,
            allow_empty=True,
        )


class ReviewApprovalRequest(BaseModel):
    log_id: int
    approved_hours: float | None = None
    #: Why the administrator approved or refused an irregular shift. Free text, so it goes
    #: through the prose profile: apostrophes and ampersands survive, markup does not. This
    #: text ends up in the audit log and in the notification the worker may read.
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(value, field="Note", max_length=textguard.MAX_NOTE, allow_empty=True)


class ReviewRejectionRequest(BaseModel):
    """A refusal, and the reason for it.

    ``note`` is **required** here, unlike an approval's: this is the one decision in the
    application that takes hours away from somebody, and a refusal with no reason written
    down is unanswerable for as long as the row exists. The reason survives in three
    places - the log row's ``flag_reason``, the append-only ``audit_log`` before/after
    pair, and the response - so "why was my overtime refused" has an answer next month.
    """

    log_id: int
    note: str

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str) -> str:
        return textguard.prose(value, field="Rejection reason", max_length=textguard.MAX_NOTE)


class ShiftHoursEditRequest(BaseModel):
    """A correction to a shift that has already been worked.

    ``hours`` is the figure the administrator is signing: it becomes both the recorded
    hours and the approved hours, because a correction the record still disagreed with
    would be no correction at all. ``note`` is optional - most edits speak for themselves
    - but whatever is typed rides the audit trail beside the before/after figures.
    """

    hours: float
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(value, field="Note", max_length=textguard.MAX_NOTE, allow_empty=True)


class PasswordEditRequest(BaseModel):
    worker_id: str
    new_password: str


class UserEditRequest(BaseModel):
    """What an administrator may change about an existing account: name, contact, rate.

    Two things are deliberately absent, and both for the same reason - this is an
    *update* to a person, not a new identity for one:

    * the **id**. It is the key every attendance row, punch, device key and audit entry is
      written against, so rewriting it would orphan a person's history rather than correct
      their record;
    * the **role**. It is a function of that id: ``_validate_id_and_role`` puts workers at
      1-499, lead workers at 500-999, admins at 1000-4999 and head admins at 5000+, which
      is what lets an operator read a role off a number. No existing id admits another
      role's range, so a "role change" could only ever be refused or be a lie. A
      promotion is a new account with an id in the right block, and the old account keeps
      the hours - which are the part that must not move.
    """

    user_id: str
    name: str
    email: str = ""
    phone: str = ""
    #: ``None`` leaves the rate alone. ``0`` clears it: an hourly rate of zero is not a
    #: rate, it is one nobody has agreed yet, and storing "unknown" beats storing an
    #: agreed 0.00. Nothing multiplies it any more - the shifts screen is a timesheet -
    #: but a wrong record is still a wrong record.
    hourly_rate: float | None = None

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        """Shape only, deliberately: an *empty* name is refused by the endpoint below with
        the 400 and the sentence the console already shows, and the roster's tests pin that
        contract. This validator answers the other question - whether the text is a name at
        all - and answering it here means a payload carrying markup never reaches the
        database even if a future edit forgets to look.
        """
        return textguard.identifier(
            value, field="Name", max_length=textguard.MAX_NAME, allow_empty=True
        )

    @field_validator("email")
    @classmethod
    def _plain_email(cls, value: str) -> str:
        return textguard.contact(value, field="Email")

    @field_validator("phone")
    @classmethod
    def _plain_phone(cls, value: str) -> str:
        return textguard.contact(value, field="Phone")


class UserDeleteRequest(BaseModel):
    user_id: str


class UserStatusRequest(BaseModel):
    user_id: str
    active: bool = True


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ShiftRulesUpdate(BaseModel):
    """The company-wide rules - what a site inherits when it has no window of its own.

    The window fields carry the same validators as a site's, because this is the same window
    at a wider scope: without them an administrator could type ``25:00`` into the company
    default and every punch would then be judged against ``00:00``-``23:59``, silently, with
    ``late_flag`` never set again. The timezone is checked for the same reason - a typo there
    moves every inheriting site's window by an hour or two.
    """

    clock_in_window_start: str | None = None
    clock_in_window_end: str | None = None
    #: The paid length of a day.
    regular_hours: float | None = None
    #: The overtime line, in **paid** hours - the same hours ``regular_hours`` counts, i.e.
    #: time on site less the unpaid break, never time on site. Resolved once, in seconds, by
    #: ``shift_hours.overtime_rule`` / ``overtime_assessment``, which every clock-out path
    #: and the watcher read instead of reading this column themselves.
    overtime_notify_hours: float | None = None
    site_timezone: str | None = None
    #: Unpaid break, in minutes, deducted from a shift that ran long enough to have
    #: contained one (``break_after_hours``). 0 turns the deduction off entirely.
    break_minutes: float | None = None
    break_after_hours: float | None = None
    #: 1 closes a shift when the paid hours reach ``regular_hours``, 0 leaves it open.
    auto_close_at_regular: int | None = None

    @field_validator("clock_in_window_start", "clock_in_window_end")
    @classmethod
    def _validate_window_time(cls, value: str | None) -> str | None:
        """The company window's hours, checked by the shared rule."""
        return _plain_hhmm(value, field="Company clock-in window")

    @field_validator("site_timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        """The company timezone, checked by the shared rule."""
        return _plain_timezone(value)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
#: The per-site clock-in window columns, in the order every write and read uses them.
#: Named once because three call sites (insert, update, the audit payload) must agree.
_SITE_WINDOW_FIELDS = ("clock_in_window_start", "clock_in_window_end", "site_timezone")

#: The same three fields on the company-wide row, which is what a site inherits. The names are
#: identical because they are the same window at a different scope - ``shift_windows`` reads a
#: site's column and falls back to this one - and it is only the *storage* of an emptied field
#: that differs: a site's columns are nullable (NULL = inherit) while ``shift_rules`` is NOT
#: NULL, so there an empty box is stored as ``''``.
_COMPANY_WINDOW_KEYS = _SITE_WINDOW_FIELDS


def _site_window_columns(req: SiteModel) -> dict[str, Any]:
    """The three window columns as they should be stored. ``None`` means "inherit"."""
    return {key: getattr(req, key) for key in _SITE_WINDOW_FIELDS}


def _validate_site_radius(radius: float) -> float:
    """A radius a worker can actually be inside of.

    A distance measured from a phone is never exactly zero, so a site stored with a
    radius of 0 - or a negative one - is a site nobody can ever clock into: the geofence
    refuses every punch and the worker is told they are "outside any designated
    construction site", which is both true and useless. The console took the 0 and said
    nothing, so the mistake only ever surfaced at the gate.
    """
    if not radius or radius <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Radius must be greater than 0 metres - a site with a radius of 0 "
                "can never be clocked into."
            ),
        )
    return radius


def get_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_location_input(loc_str: str) -> tuple[float, float]:
    """Parse raw ``lat,lon`` strings or Google Maps URLs into floats."""
    if not loc_str:
        raise HTTPException(status_code=400, detail="Location input is empty.")

    loc_str = loc_str.strip()

    gmaps_match = re.search(r"@([-+]?\d*\.\d+),([-+]?\d*\.\d+)", loc_str)
    if gmaps_match:
        return float(gmaps_match.group(1)), float(gmaps_match.group(2))

    if "!3d" in loc_str and "!4d" in loc_str:
        try:
            lat = float(loc_str.split("!3d")[1].split("!4d")[0])
            lon = float(loc_str.split("!4d")[1].split("!")[0])
            return lat, lon
        except Exception:
            pass

    parts = loc_str.split(",")
    if len(parts) == 2:
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            pass

    raise HTTPException(
        status_code=400, detail="Invalid location format. Provide valid GPS coordinates or a Google Maps URL."
    )


def validate_plausible_coordinates(lat: float, lon: float) -> None:
    """Reject readings that cannot be a real fix.

    ``(0, 0)`` is the canonical output of a mock-location provider and is never a
    legitimate position for this deployment, so it is refused as *invalid input*
    rather than merely as "outside the geofence" - the distinction matters,
    because the second one looks like a worker standing in the wrong place.
    """
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise HTTPException(status_code=400, detail="Location coordinates are not finite numbers.")
    if abs(lat) > 90 or abs(lon) > 180:
        raise HTTPException(status_code=400, detail="Location coordinates are out of range.")
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        raise HTTPException(
            status_code=400,
            detail="Location (0,0) is the default reading of a mock-location provider and is not a valid fix.",
        )


def site_at(conn: sqlite3.Connection, lat: float, lon: float) -> sqlite3.Row | None:
    """The site whose geofence contains this fix, or ``None`` - the whole rule, once.

    First match wins, in the order the table returns the sites, and that is deliberate: an
    overlapping pair of geofences has to resolve to *one* site or the punch, the window it is
    judged by and the site named on the record could each pick a different one. Two callers
    ask this question - the punch handler and the worker's own "what window applies where I am
    standing" look-up - and a second copy of this loop is how they would come to disagree.
    """
    for site in conn.execute("SELECT * FROM construction_sites").fetchall():
        if get_distance_meters(site["lat"], site["lon"], lat, lon) <= site["radius"]:
            return site
    return None


#: What a worker is told when the *photo* is the problem, keyed by the reason
#: ``compare_faces_sync`` reports. Those strings are written for an operator reading a
#: log - and one of them (``Internal processing error: <exception>``) used to be echoed
#: to the phone verbatim, which tells the worker nothing they can act on and hands out a
#: little of our internals. These say what to do instead, and the code beside each one is
#: what the client, the logs and the triage views key on, so the wording can change
#: without anything downstream changing with it.
#: The error ``compare_faces_sync`` reports when the *stored template* is the problem.
#: Its own string rather than a generic failure because the two have different owners: a
#: stale template needs an administrator to take a photograph, and a server fault needs
#: somebody to read a log. Keys the mapping below, so it is a constant and not a literal.
FACE_REFERENCE_STALE = "Reference face template predates the current face pipeline."

FACE_FRAME_REFUSALS: dict[str, tuple[str, str]] = {
    "No face detected.": (
        "face_not_found",
        "No face was found in the photo. Fill the frame with your face, in even light, "
        "and take it again.",
    ),
    "Multiple faces detected.": (
        "multiple_faces",
        "More than one face is in the photo. You have to be the only person in the frame.",
    ),
    "Reference embedding not found.": (
        "reference_missing",
        "Your face is not registered yet, so this punch cannot be matched. Ask your "
        "administrator to enroll you.",
    ),
    FACE_REFERENCE_STALE: (
        "reference_stale",
        "Your face records were taken before a change to the face check, so this photo "
        "cannot be compared with them. Ask your administrator to enroll you again - it "
        "takes one photo.",
    ),
}

#: The refusal a worker gets when the failure is ours rather than theirs. Never the
#: exception text: "Internal processing error: ..." on a phone is a dead end.
FACE_CHECK_FAILED: tuple[str, str] = (
    "face_check_failed",
    "The photo could not be checked just now. Take it again, and tell your administrator "
    "if it keeps happening.",
)


def _frame_refusal(error: str) -> tuple[str, str]:
    """``(error_code, worker-facing message)`` for a face check that could not be used."""
    return FACE_FRAME_REFUSALS.get(error, FACE_CHECK_FAILED)


def compare_faces_sync(reference_json_path: str, live_image_data) -> dict:
    # The template is read *before* the model runs, on purpose: an unusable template is a
    # fact about the file, and spending a VGG-Face embedding to discover it would put a
    # 200 ms model call in front of a refusal that has nothing to do with the photo. It
    # also keeps the failure where an operator can find it - see ``biometrics``.
    try:
        reference = biometrics.read_reference(reference_json_path)
    except FileNotFoundError:
        return {"verified": False, "distance": 99.9, "error": "Reference embedding not found."}
    except Exception:
        # Unreadable is answered as stale rather than as a server fault: the fix is the
        # same photograph, and two names for one fix is how a worker gets sent round a
        # loop. The detailed reason is in ``biometrics.stale_references`` for an admin.
        return {"verified": False, "distance": 99.9, "error": FACE_REFERENCE_STALE}

    reason = biometrics.stale_reason(reference)
    if reason is not None:
        log.warning(
            "refusing to score a stale face template (%s): %s",
            reason,
            os.path.basename(reference_json_path),
        )
        return {"verified": False, "distance": 99.9, "error": FACE_REFERENCE_STALE}

    try:
        # The decision lines, from the pipeline that produced this embedding rather than from
        # a constant. The staleness check above has already required that pipeline to be the
        # live one, so this cannot read another crop's band - but it is read from the
        # template on purpose: if a build ever ships a crop without deriving its lines, the
        # refusal names the pipeline instead of scoring against numbers nobody measured.
        band = face_detector.band_for(reference.pipeline or "")
    except face_detector.UnknownPipelineError as exc:
        log.error("face verification refused: %s", exc)
        return {
            "verified": False,
            "distance": 99.9,
            "error": "Face match thresholds are not derived for this pipeline.",
        }

    try:
        # ``represent_direct``, not a submission: the endpoint submits *this whole
        # function* to the face engine, so the model call here is the body of an engine
        # job. A job that submitted to its own pool would wait for the worker running it
        # (see ``face_engine``), and the model and detector it names are the ones the
        # engine names, so a punch and an enrollment cannot drift onto different models.
        live_embedding_objs = face_engine.ENGINE.represent_direct(live_image_data)

        if len(live_embedding_objs) > 1:
            return {"verified": False, "distance": 99.9, "error": "Multiple faces detected."}

        live_embedding = live_embedding_objs[0]["embedding"]
        if len(reference.embedding) != len(live_embedding):
            # A template of a different size cannot be compared at all: ``cosine`` would
            # raise on the shape mismatch and answer with "Internal processing error" - a
            # 500 that tells a worker nothing and blames us for what is a template change.
            log.warning(
                "face template %s has %s dimensions, the live embedding has %s",
                os.path.basename(reference_json_path),
                len(reference.embedding),
                len(live_embedding),
            )
            return {"verified": False, "distance": 99.9, "error": FACE_REFERENCE_STALE}

        # The comparison itself, timed apart from the embedding that fed it: one is a numpy dot
        # product over 4096 floats and the other is half a second of TensorFlow, and reporting
        # them as one number would hide whichever one changed.
        started = time.perf_counter()
        distance = cosine(reference.embedding, live_embedding)
        telemetry.observe_cosine(time.perf_counter() - started)

        # The score on the way past, because it is the only early warning that the model, the
        # camera fleet or the enrollment photos changed: the bands are derived from this
        # deployment's own material and then frozen, so a shift in *this* distribution is what
        # moves the review queue. Observed for real distances only - the 99.9 sentinel on the
        # refusal paths below is not a score, and putting it in the histogram would make every
        # no-face frame look like a near miss.
        telemetry.observe_match_score(distance)

        return {
            "verified": band.classify(distance) == face_detector.MATCH_APPROVED,
            "distance": round(distance, 4),
            "error": None,
        }
    except ValueError:
        return {"verified": False, "distance": 99.9, "error": "No face detected."}
    except FileNotFoundError:
        return {"verified": False, "distance": 99.9, "error": "Reference embedding not found."}
    except Exception as exc:
        return {"verified": False, "distance": 99.9, "error": f"Internal processing error: {exc}"}


def send_whatsapp_alert(worker_id: str, worker_name: str, site_name: str, score: float, *, conn=None) -> bool:  # noqa: ARG001
    """Removed. External messaging is gone; alerts are internal notifications.

    The symbol survives only so that operations code and the frozen acceptance
    test keep importing. It performs **no** network I/O and always returns False;
    there are no credentials for it to fall back to. Use
    ``notifications.notify()`` instead, which writes a row the admin dashboard
    shows.
    """
    warnings.warn(
        "send_whatsapp_alert() has been removed: alerts are stored in admin_notifications "
        "and surfaced on the admin dashboard.",
        DeprecationWarning,
        stacklevel=2,
    )
    return False


def _validate_id_and_role(user_id: str, role: str) -> None:
    # One rule, one implementation. ``security`` owns the id bands and the list of roles no
    # API may create; the range chain below used to be a second copy of those bands, and two
    # copies is how the console and a registration link come to disagree about what an id may
    # be. The refusal is explicit and by name, not an accident of a range that happens not to
    # include the root band - an administrator who could create one could promote themselves
    # into the tier that reads the audit trail.
    security.refuse_developer_role(role)
    security.validate_user_id_for_role(user_id, role)
    try:
        uid_int = int(user_id)
    except ValueError:  # pragma: no cover - ``validate_user_id_for_role`` already refused it
        raise HTTPException(status_code=400, detail="User ID must be a numeric integer.") from None
    del uid_int, role  # kept for the signature's sake; the rules live in ``security``


# ---------------------------------------------------------------------------
# managing an existing account
#
# Editing, deactivating and deleting an account share one set of rules, and the rules
# are here rather than in each endpoint because "may this administrator act on this
# account" must not be answered three different ways. They are the rules
# ``/admin/users/edit_password`` already enforced, generalized from the password to the
# whole account - a standard admin resetting an administrator's password and a standard
# admin *deleting* one are the same privilege escalation wearing a different verb.
# ---------------------------------------------------------------------------
def _guard_standard_admin(actor: CurrentUser, target: sqlite3.Row, action: str) -> None:
    """A standard admin may not act on an administrator's account at all."""
    if actor.role == "admin" and target["role"] in ("admin", "head_admin"):
        raise HTTPException(
            status_code=403,
            detail=f"Standard Admins cannot {action} administrator accounts.",
        )


def _refuse_self_account(actor: CurrentUser, user_id: str, action: str) -> None:
    """Nobody edits, deactivates or deletes the account they are signed in with.

    It is not a moral rule, it is a lockout: the admin would either demote themselves out
    of the console or remove the account mid-request.
    """
    if str(user_id) == str(actor.id):
        raise HTTPException(
            status_code=400,
            detail=f"You cannot {action} the account you are signed in with.",
        )


def _user_snapshot(row: sqlite3.Row) -> dict:
    """The account fields worth recording, and never the password hash.

    ``audit_log`` is append-only, so whatever is written there is written for good; a
    bcrypt hash in it would be a credential with unlimited lifetime. The snapshot names
    the fields an administrator can actually change and stops there.
    """
    return {
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "role": row["role"],
        "status": row["status"],
        "hourly_rate": row["hourly_rate"],
    }


def _revoke_user_access(conn: sqlite3.Connection, user_id: str, *, now: str) -> dict:
    """Take away everything that lets an account act, without touching its history.

    Sessions are revoked by the caller bumping ``token_version`` (every outstanding token
    carries the old version and stops verifying); what is left are the two credentials
    that do not live in ``users``: the offline signing keys a phone holds, and an
    enrollment link that could still hand out a fresh face template.
    """
    devices = conn.execute(
        "UPDATE worker_devices SET revoked_at = ?, key_epoch = key_epoch + 1 "
        "WHERE worker_id = ? AND revoked_at IS NULL",
        (now, user_id),
    ).rowcount
    invites = conn.execute(
        "UPDATE enrollment_invites SET revoked_at = ? WHERE worker_id = ? AND revoked_at IS NULL",
        (now, user_id),
    ).rowcount
    return {"devices_revoked": int(devices or 0), "invites_revoked": int(invites or 0)}


def _delete_biometric_files(user_id: str) -> tuple[list[str], list[str]]:
    """Remove a person's face template and selfie. Returns ``(removed, failed)``.

    This is the one part of an account that is not in the database. Deleting the row and
    leaving the template on disk would leave the biometric half of a deleted worker
    behind, and the failure is reported rather than swallowed: the caller says which
    files it could not remove, because a silent failure here is invisible forever.

    Both names are removed - the account's immutable biometric id, and the account id it
    used to be filed under - because the file that a *later* worker could inherit is
    precisely the old one (see ``biometrics``). ``biometrics.remove_files`` owns which
    files those are.
    """
    return biometrics.remove_files(user_id)


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------
@router.post("/auth/login")
@limiter.limit(settings.login_rate_limit)
async def login(request: Request, req: LoginRequest):
    """Exchange an id + email/phone + password for a signed access token.

    Failures are deliberately identical for "no such user" and "wrong password",
    so the endpoint cannot be used to enumerate accounts.

    A deactivated account is refused *after* that check rather than folded into it: the
    refusal names a real state (an administrator deactivated this account) and is only
    ever seen by somebody who already knows the password, whereas answering it earlier
    would turn "is this account deactivated?" into a question anyone can ask.
    """
    with db() as conn:
        user_row = conn.execute(
            "SELECT id, name, email, phone, role, status, password_hash, "
            "COALESCE(token_version, 0) AS token_version "
            "FROM users WHERE id = ? AND (email = ? OR phone = ?)",
            (req.user_id, req.email_or_phone, req.email_or_phone),
        ).fetchone()

    if not user_row or not verify_password(req.password, user_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials or user ID")

    if str(user_row["status"] or "active").strip().lower() != "active":
        raise HTTPException(
            status_code=403,
            detail="This account has been deactivated. Ask an administrator to reactivate it.",
        )

    token, expires = create_access_token(
        str(user_row["id"]), user_row["role"], int(user_row["token_version"])
    )
    return {
        "status": "success",
        "user": {
            "id": user_row["id"],
            "name": user_row["name"],
            "email": user_row["email"],
            "phone": user_row["phone"],
            "role": user_row["role"],
        },
        "token": token,
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "expires_in": int(settings.jwt_ttl_hours * 3600),
    }


@router.post("/auth/change_password")
async def change_own_password(
    payload: PasswordChangeRequest, current: CurrentUser = Depends(any_authenticated)
):
    """Rotate your own password. Bumps ``token_version``, revoking every session.

    The old credential is required: without it a stolen token would be enough to
    seize the account permanently.
    """
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (current.id,)
        ).fetchone()
        if row is None or not verify_password(payload.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
        new_hash = hash_password(payload.new_password)
        conn.execute(
            "UPDATE users SET password_hash = ?, token_version = COALESCE(token_version, 0) + 1 WHERE id = ?",
            (new_hash, current.id),
        )
        _audit(conn, action="password_change_self", actor=current, entity="users", entity_id=current.id)
    return {"status": "success", "message": "Password updated. Please sign in again."}


@router.get("/auth/me")
async def whoami(current: CurrentUser = Depends(any_authenticated)):
    return {"id": current.id, "name": current.name, "role": current.role}


# ---------------------------------------------------------------------------
# worker endpoints
# ---------------------------------------------------------------------------
@router.get("/worker/stats/{worker_id}")
async def get_worker_stats(worker_id: str, current: CurrentUser = Depends(admin_only)):
    """Monthly totals for a named worker.

    Admin-only on purpose: an id-addressed read that any peer may call is an
    object-reference problem waiting to happen (and, in the shipped version, it
    was open to the whole network). A worker reads their own totals through
    ``/worker/me/stats``, which carries no id to tamper with.
    """
    with db() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN COALESCE(status_code, '') NOT IN ('pending_review', 'pending_overtime')
                                  THEN hours ELSE 0 END), 0.0) AS payable_hours,
                COALESCE(SUM(CASE WHEN COALESCE(status_code, '') IN ('pending_review', 'pending_overtime')
                                  THEN hours ELSE 0 END), 0.0) AS pending_hours,
                COALESCE(SUM(COALESCE(overtime_hours, 0)), 0.0) AS overtime_hours
            FROM attendance_logs
            WHERE worker_id = ?
              AND action = ?
              AND strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now', 'localtime')
            """,
            (worker_id, ACTION_CLOCK_OUT),
        ).fetchone()

    # The worker's own open shift travels with their own totals, so the clock panel
    # never has to read ``/admin/active_sessions`` - which a worker cannot read at
    # all. That mismatch made the panel offer "Clock In" to somebody who was already
    # clocked in, so every tap after the first answered "Already clocked in!".
    with db() as conn:
        session = conn.execute(
            "SELECT site_name, clock_in_time, late_flag FROM active_sessions WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()

    # The worker's clock panel ticks a live elapsed timer and warns when the open
    # shift crosses the overtime threshold. That number lives in ``shift_rules``,
    # which only an admin may read, so it travels with the worker's own stats:
    # a hardcoded 8.1 in the panel would quietly lie the day an admin retunes it.
    rules = get_shift_rules()
    total = float(row["payable_hours"] or 0.0) + float(row["pending_hours"] or 0.0)
    # The clock panel ticks up from the clock-in time and has to know when the *paid*
    # day ends: with a 30-minute unpaid break, that is 8.5 h on site, not 8. The two
    # numbers travel together so the panel never has to guess the policy.
    break_for_panel = {
        "break_minutes": float(rules["break_minutes"]),
        "break_after_hours": float(rules["break_after_hours"]),
        "paid_day_hours": shift_hours.regular_hours(rules),
        "on_site_day_hours": round(
            shift_hours.regular_hours(rules)
            + (shift_hours.break_hours(rules) if shift_hours.auto_close_enabled(rules) else 0.0),
            4,
        ),
        "auto_close_at_regular": 1 if shift_hours.auto_close_enabled(rules) else 0,
    }
    # An unresolved review blocks clock-out (see ``/attendance/verify``), so the panel is
    # told before the worker walks to a site and takes a selfie that can only be refused.
    # The rule is deliberate - an admin clears the flag, or forces the shift closed - but
    # a refusal with no warning is a dead end for the person holding the phone.
    with db() as conn:
        flagged = conn.execute(
            "SELECT 1 FROM attendance_logs WHERE worker_id = ? AND status = ? LIMIT 1",
            (worker_id, STATUS_PENDING_REVIEW),
        ).fetchone()
    return {
        "worker_id": worker_id,
        "total_hours": round(total, 2),
        "regular_hours": round(float(row["payable_hours"] or 0.0), 2),
        "pending_hours": round(float(row["pending_hours"] or 0.0), 2),
        "overtime_hours": round(float(row["overtime_hours"] or 0.0), 2),
        "overtime_notify_hours": float(rules["overtime_notify_hours"]),
        **break_for_panel,
        "flagged_for_review": flagged is not None,
        "active_session": (
            {
                "site_name": session["site_name"],
                "clock_in_time": session["clock_in_time"],
                "late_flag": session["late_flag"],
            }
            if session is not None
            else None
        ),
    }


@router.get("/worker/me/stats")
async def get_my_stats(current: CurrentUser = Depends(any_authenticated)):
    """Your own monthly totals. The subject is the token, never a request field."""
    return await get_worker_stats(current.id, current)


@router.get("/worker/me/site-window")
async def get_my_site_window(
    location_input: str = Query(..., max_length=300),
    current: CurrentUser = Depends(any_authenticated),
):
    """The clock-in window where this phone is standing, and whether now is inside it.

    The punch card said nothing about the window until *after* the shutter: the verdict was
    written into an administrator's notification ("arrival outside the clock-in window"), which
    is a sentence about the worker that the worker never read. This answers the same question
    one tap earlier - the same geofence test, the same per-site window, the same predicate - so
    somebody standing at a gate knows whether they are early, on time or late while there is
    still time to do something about it (wait, walk in, or tell a supervisor).

    Read-only, self-scoped and deliberately unable to refuse anything: whatever this returns,
    the punch is still judged by ``/attendance/verify``. A worker whose phone cannot reach this
    endpoint loses a line of text, not their hours.
    """
    lat, lon = parse_location_input(location_input)
    validate_plausible_coordinates(lat, lon)

    rules = get_shift_rules()
    with db() as conn:
        # Read in the same request as the window resolution below, so the site named here and
        # the hours reported cannot come from two different versions of the row.
        site_row = site_at(conn, lat, lon)

    window = shift_windows.effective_window(site_row, rules)
    arrival = window.arrival(datetime.now())
    return {
        # ``on_site`` is the honest answer to "is this even a place a punch can be taken from":
        # with no geofence match the punch is refused, and a window shown for a site the worker
        # is not standing on would explain a decision that is not the one about to be made.
        "on_site": site_row is not None,
        "site_name": window.site_name,
        "window": window.as_dict(),
        # The subject is the token, exactly as in the two self-scoped reads above; nothing here
        # is about another worker, so there is no id to tamper with.
        "worker_id": current.id,
        **arrival.as_dict(),
    }


@router.get("/worker/me/logs")
async def get_my_logs(limit: int = 50, current: CurrentUser = Depends(any_authenticated)):
    """Your own recent attendance rows.

    The worker-facing twin of ``/admin/logs``: the History tab used to call the
    admin endpoint, which answers 403 for the worker reading their own timesheet.
    """
    limit = max(1, min(int(limit), 500))
    with db() as conn:
        rows = conn.execute(
            "SELECT id, worker_id, site_name, action, timestamp, hours, score, status, status_code, "
            "lat, lon, approved_hours, overtime_hours, flag_reason "
            "FROM attendance_logs WHERE worker_id = ? ORDER BY timestamp DESC LIMIT ?",
            (current.id, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "worker_id": row["worker_id"],
            "site": row["site_name"],
            "action": row["action"],
            "timestamp": row["timestamp"],
            "hours": row["hours"],
            "score": row["score"],
            "status": row["status"],
            "status_code": row["status_code"],
            "lat": row["lat"],
            "lon": row["lon"],
            "approved_hours": row["approved_hours"],
            "overtime_hours": row["overtime_hours"],
            "flag_reason": row["flag_reason"],
        }
        for row in rows
    ]


@router.get("/worker/me/report")
async def get_my_report(
    start: str | None = None,
    end: str | None = None,
    current: CurrentUser = Depends(any_authenticated),
):
    """Your own timesheet for a period - how long, and where.

    The self-scoped twin of ``/admin/reports/shifts``. An administrator who works at a
    site as well as running it has the same question about their own hours that every
    worker has, and until now the only way to answer it was to read the company-wide
    report and pick their own rows out of it. The subject is the token (``current.id``),
    never a request field, so there is no id to tamper with and no way to read somebody
    else's timesheet through it.

    The rows are the timesheet's own, from the same function the console reads, so the
    figures here cannot disagree with the ones an administrator sees on the Shifts tab -
    including the rule that matters most: hours nobody has signed off are reported in
    ``awaiting_approval_hours`` and never added to ``approved_hours``.

    ``by_site`` is what makes this a report about *where*: the same hours bucketed per
    site, which is the breakdown somebody asks for when they were sent to more than one
    place in the period.

    ``columns`` is the shape of the two files this account downloads (see
    ``/worker/me/report/columns``). It travels here rather than in a second request because
    the files are built from *this* payload on the client - a column list fetched separately
    could arrive after the rows it describes.
    """
    first, second, start_date, end_date = reports._range_bounds(start, end)
    result = reports.shift_timesheet_rows(start=first, end=second, worker_id=current.id)
    rows = result["rows"]

    # Bucketed here rather than in SQL: the rows are already in hand and the arithmetic
    # has to match theirs exactly, which it does by summing the same field.
    by_site: dict[str, dict] = {}
    for row in rows:
        bucket = by_site.setdefault(
            row["site_name"],
            {"site_name": row["site_name"], "shifts": 0, "hours": 0.0, "approved_hours": 0.0},
        )
        bucket["shifts"] += 1
        bucket["hours"] += float(row["hours"] or 0.0)
        bucket["approved_hours"] += float(row["approved_hours"] or 0.0)

    sites = []
    for bucket in by_site.values():
        hours = round(bucket["hours"], 4)
        approved = round(bucket["approved_hours"], 4)
        sites.append(
            {
                "site_name": bucket["site_name"],
                "shifts": bucket["shifts"],
                "hours": hours,
                "approved_hours": approved,
                # ``hours == approved_hours + awaiting_approval_hours`` holds by
                # construction here, exactly as it does in the totals below.
                "awaiting_approval_hours": round(hours - approved, 4),
            }
        )
    # Busiest site first, then by name so two sites with equal hours keep a stable order.
    sites.sort(key=lambda item: (-item["hours"], item["site_name"]))

    # Overtime is its own figure on the log row rather than part of ``hours``, so it is
    # summed over the same rows the timesheet counts and is never added into a total: it
    # is the hours that needed a decision, not hours worked twice.
    with db() as conn:
        overtime = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(overtime_hours, 0)), 0.0) AS overtime_hours "
            "FROM attendance_logs "
            "WHERE worker_id = ? AND action = 'Clock Out' AND timestamp >= ? AND timestamp < ?",
            (current.id, first, second),
        ).fetchone()
        # The account's own column choice, read beside the same connection as the figures:
        # an account that was deleted mid-session has no row to prefer anything, and the
        # default file is the right answer for a session that outlived its account.
        preference = conn.execute(
            "SELECT report_columns FROM users WHERE id = ?", (current.id,)
        ).fetchone()
    totals = result["totals"]

    return _json(
        {
            "worker_id": current.id,
            "worker_name": current.name,
            "period": {"start": str(start_date), "end": str(end_date)},
            "rows": rows,
            "by_site": sites,
            "columns": list(
                reports.report_columns(preference["report_columns"] if preference else None)
            ),
            "totals": {
                "shifts": totals["shifts"],
                "sites": len(sites),
                "hours": totals["hours"],
                "approved_hours": totals["approved_hours"],
                "awaiting_approval_hours": totals["awaiting_approval_hours"],
                "awaiting_approval": totals["awaiting_approval"],
                "break_hours": totals["break_hours"],
                "late_arrivals": totals["late_arrivals"],
                "overtime_hours": round(float(overtime["overtime_hours"] or 0.0), 4),
            },
        }
    )


class ReportColumnsRequest(BaseModel):
    """The columns this account's own timesheet files should carry.

    A plain list of ids rather than a flag per column: the vocabulary lives in one place
    (``reports.REPORT_COLUMNS``), the file's order is that vocabulary's order whatever this
    list says, and a list cannot express a column left in an ambiguous state. An empty list
    is not a choice - a file with no columns is not a report - and is refused below.
    """

    columns: list[str] = []


@router.post("/worker/me/report/columns")
async def set_my_report_columns(
    req: ReportColumnsRequest, current: CurrentUser = Depends(any_authenticated)
):
    """Remember which columns **your own** timesheet files carry.

    A preference, and deliberately the account's rather than the browser's: a timesheet is
    handed in from wherever the worker happens to be, so a choice kept in one device's
    storage would give the same account two different files - and would be lost with the
    phone. The subject is the token, like every other ``/worker/me`` route, so there is no
    id in this request that could write a preference onto somebody else's account.

    No audit entry, and that is a decision rather than an oversight: the log answers "who
    changed this person's *record*", and this changes only the shape of a document its own
    account holder reads. It touches nobody's hours, grants nothing, and hides nothing -
    every column it can name is a field of a row the same worker already reads in full on
    the History screen.

    The reply carries the list the server actually stored rather than the one it was sent:
    ids this build does not know are dropped, so a client asking for a column this server
    cannot fill is told by the answer what it will really get.
    """
    chosen = reports.report_columns_choice(req.columns)
    if not chosen:
        raise HTTPException(status_code=400, detail="A timesheet needs at least one column.")
    stored = reports.stored_report_columns(chosen)
    with db(write=True) as conn:
        conn.execute("UPDATE users SET report_columns = ? WHERE id = ?", (stored, current.id))
    return {
        "status": "success",
        "columns": list(reports.report_columns(stored)),
        "message": "Report columns saved.",
    }


#: The roles that may put their *own* face on the clock, and why it is not every role.
#:
#: A reference template is a credential, not a preference: every punch by the account is
#: judged against it, so whoever holds a session could mint the face that session punches
#: with - take over a worker's account and the account's clock-ins become yours, and the
#: hours land on that worker's timesheet. Enrolling somebody is therefore an administrator's
#: act (``/admin/enroll``), and this list is only about the one account a session may
#: re-credential without anybody else: its own.
#:
#: It names ``admin`` alone because that is the role that runs the console *and* works a
#: shift of its own - the same list the console keeps for who may step from the console onto
#: the handset (``UI.handsetRoles``) - so the button and this endpoint agree, and neither is
#: a superset of the other. A ``head_admin`` owns the deployment rather than a rota (no
#: punch card, so no template to need), and a worker's reference is issued by the company
#: through an enrollment link.
SELF_ENROLL_ROLES = ("admin",)


@router.post("/worker/me/enroll")
@limiter.limit(settings.enrollment_rate_limit)
async def enroll_my_face(
    request: Request,
    photo: UploadFile = File(...),
    current: CurrentUser = Depends(require_role(*SELF_ENROLL_ROLES)),
):
    """Register **your own** reference photo, so you can clock in.

    The head-administrator-shaped hole this fills: an administrator who works a site reaches
    the clock (``POST /attendance/verify`` takes any authenticated role) but a punch needs a
    stored template, and every existing way to get one put somebody else in the middle -
    ``/admin/enroll``, an enrollment link sent to a phone, the console's create-user form,
    which only runs while the account is being made. An administrator with no template could
    therefore see the punch card and never use it, and the only fix was to ask the head
    administrator to enroll them.

    The subject is the token. There is no worker id in the request to tamper with, so "enroll
    my face" cannot be aimed at somebody else - and unlike ``/admin/enroll``, which any
    administrator may point at any account, this one can only ever write the caller's.

    The photo is a **live capture** from the console's camera, which is why the enrollment
    liveness policy runs here (``enrollment.embed_reference``, the same call the self-service
    link makes): a frame off a camera is exactly what passive anti-spoofing is built to
    judge, and a printed photo must not become a permanent template. The path that accepts a
    file from disk - the console's create-user form, the roster import - deliberately does
    not pretend a file can be proven live.
    """
    file_bytes = await uploads.read_photo(photo, field="enrollment photo")
    try:
        image = uploads.decode_photo(file_bytes, field="enrollment photo")
        # 800 px is what the console's other enrollment path embeds and stores; the detector
        # gains nothing from more, and the reference selfie beside the template is a
        # thumbnail.
        image.thumbnail((800, 800))
    except Exception:
        raise HTTPException(status_code=400, detail="Image processing failed.")

    worker_id = str(current.id)
    with db() as conn:
        row = conn.execute(
            "SELECT enrolled_at, biometric_id FROM users WHERE id = ?", (worker_id,)
        ).fetchone()
    # Read before the write, so the audit event can say whether this *replaced* a template or
    # created the first one. "Somebody enrolled a face for this account" and "somebody
    # replaced the face this account was using" are different events to whoever reads the log
    # afterwards, and the second is the one worth looking at.
    replaced = biometrics.is_enrolled(worker_id, biometrics.id_from(row))

    try:
        # Liveness and the embedding as one unit of pool work, as the self-service link does:
        # both are model calls and the engine is the one place that bounds how many run at
        # once, so a console enrolling itself cannot stall the punches behind it.
        decision, embedding = await face_engine.ENGINE.run_async(
            enrollment.embed_reference, image, stage="console_self_enroll"
        )
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    except HTTPException:
        # A liveness refusal is a 422 written for the person in front of the camera; it is
        # not ours to rewrite into something vaguer.
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Enrollment failed: {exc}") from exc

    biometrics.write_reference(worker_id, image, embedding)

    with db(write=True) as conn:
        conn.execute(
            "UPDATE users SET enrolled_at = ?, template_version = COALESCE(template_version, 0) + 1 "
            "WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), worker_id),
        )
        _audit(
            conn,
            action="biometric_self_enroll",
            actor=current,
            entity="users",
            entity_id=worker_id,
            # The before-image stays even though the write is the account's own: this is the
            # event that decides which face the account's clock-ins will match from now on.
            before={
                "enrolled_at": row["enrolled_at"] if row is not None else None,
                "template_replaced": replaced,
            },
            after={"source": "console", "liveness": decision.as_payload()},
            request=request,
        )
        notifications.notify(
            conn,
            kind=notifications.KIND_ENROLLMENT_COMPLETED,
            severity=notifications.SEVERITY_INFO,
            title="An administrator registered their own face",
            body=(
                f"{current.name} (id {worker_id}) captured their reference photo from the "
                f"console, {('replacing the template on file' if replaced else 'for the first time')}. "
                "Their clock-ins will be matched against it from now on."
            ),
            worker_id=worker_id,
            payload={"source": "console_self_enroll", "liveness": decision.as_payload()},
            # One per enrollment, not one per attempt: the timestamp is the event, so a
            # liveness refusal followed by a good capture is one enrollment, not two
            # notifications about the same person.
            dedupe_key=f"self_enroll:{worker_id}:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )

    return {
        "status": "success",
        "worker_id": worker_id,
        "template_replaced": replaced,
        "message": "Your photo is registered. You can clock in now.",
    }


# ---------------------------------------------------------------------------
# the worker's own notification channel
# ---------------------------------------------------------------------------
class PushSubscriptionRequest(BaseModel):
    """What a browser hands over when a worker allows notifications on this device.

    ``endpoint`` is a URL from the browser's push service and the two keys are the
    encryption material for that endpoint - they are the capability to send to it, which is
    why the endpoint is not trusted: it must be an ``https`` URL on a host a push service
    actually hands out (``push.validate_endpoint``), because the server is the party that
    will later POST to it. A worker-supplied URL the server fetches is the definition of an
    SSRF, so the control is an allowlist of push services rather than a shape check.
    """

    endpoint: str = Field(..., max_length=push.MAX_ENDPOINT_CHARS)
    p256dh: str = Field(..., max_length=push.MAX_KEY_CHARS)
    auth: str = Field(..., max_length=push.MAX_KEY_CHARS)


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., max_length=push.MAX_ENDPOINT_CHARS)


def _worker_notification(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "created_at": row["created_at"],
        "read": row["read_at"] is not None,
        "delivered": row["delivered_at"] is not None,
    }


@router.get("/worker/me/notifications")
async def get_my_notifications(
    limit: int = 50,
    unread_only: bool = False,
    current: CurrentUser = Depends(any_authenticated),
):
    """Your own inbox: what this application has told you, newest first.

    The worker-facing twin of ``/admin/notifications``, and the *record* half of the push
    channel: a phone that was off, a permission that was never granted or a push that
    failed all leave the event here. ``unread`` is what the app badges and what makes a
    foreground poll able to behave like a notification without a service worker.

    The subject is the token (``current.id``), never a request field - so there is no id to
    tamper with and no way to read somebody else's inbox through it.
    """
    limit = max(1, min(int(limit), 200))
    where = "worker_id = ?" + (" AND read_at IS NULL" if unread_only else "")
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM worker_notifications WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (current.id, limit),
        ).fetchall()
        unread = notifications.worker_unread_count(conn, current.id)
    return {
        "worker_id": current.id,
        "unread": unread,
        "notifications": [_worker_notification(row) for row in rows],
        "push": push.browser_config(),
    }


@router.post("/worker/me/notifications/read")
async def mark_my_notifications_read(
    notification_id: int | None = None, current: CurrentUser = Depends(any_authenticated)
):
    """Mark one notification read, or the whole inbox when no id is given.

    A body-less POST rather than PATCH, because this app's whole API is POST-for-writes and
    a worker's phone is the least forgiving client to introduce a new verb to. The
    ``worker_id`` clause is not decoration: it is what makes "mark read" unable to touch
    somebody else's row, and the route is tested for exactly that.
    """
    with db(write=True) as conn:
        if notification_id is None:
            changed = conn.execute(
                "UPDATE worker_notifications SET read_at = ? WHERE worker_id = ? AND read_at IS NULL",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), current.id),
            ).rowcount
            return {"status": "success", "marked": int(changed or 0)}
        changed = conn.execute(
            "UPDATE worker_notifications SET read_at = ? "
            "WHERE id = ? AND worker_id = ? AND read_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(notification_id), current.id),
        ).rowcount
        if not changed:
            # Either it is already read or it is not this worker's. Both answers are the same
            # one, deliberately: telling a caller that an id exists but belongs to somebody
            # else is an enumeration oracle.
            raise HTTPException(status_code=404, detail="No unread notification with that id.")
    return {"status": "success", "marked": 1}


@router.get("/worker/me/push")
async def get_my_push_config(current: CurrentUser = Depends(any_authenticated)):
    """Whether this deployment can push, and what the browser needs to subscribe.

    The app asks for the browser's notification permission *because of this answer*: a
    deployment with no VAPID keys must not prompt a worker for a permission nothing can use.
    """
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM worker_push_subscriptions WHERE worker_id = ? AND revoked_at IS NULL",
            (current.id,),
        ).fetchone()[0]
    return {**push.browser_config(), "subscriptions": int(count)}


class CorpusConsentRequest(BaseModel):
    """The worker's own answer to "may we keep your punch frames for calibration?".

    ``granted`` is the decision; ``note`` is optional free-text (vetted as prose, like every
    note here): it is the *which ask did they answer* that a bare yes loses, and the one thing
    a later audit of the consent wants. A withdrawal carries it the same way - "changed my
    mind" is a sentence worth keeping, not just a boolean flip.
    """

    granted: bool
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(value, field="Consent note", max_length=textguard.MAX_NOTE, allow_empty=False)


@router.get("/worker/me/corpus/consent")
async def get_my_corpus_consent(current: CurrentUser = Depends(any_authenticated)):
    """The worker's own capture consent, as the app should show it: current state plus history.

    Visible to the worker *and only about the worker* - the history is their decisions, not the
    corpus. A worker who cannot see what they agreed to cannot be said to have agreed
    knowingly, so this read is half of what makes the grant meaningful.
    """
    return {
        "granted": corpus.worker_consent(current.id),
        "history": corpus.consent_history(current.id),
        "capture_enabled": bool(settings.calibration_capture_enabled),
    }


@router.post("/worker/me/corpus/consent")
async def set_my_corpus_consent(
    request: Request,
    payload: CorpusConsentRequest,
    current: CurrentUser = Depends(any_authenticated),
):
    """Grant or withdraw the worker's own opt-in to calibration capture, on the record.

    One endpoint for both directions, because a *withdrawal* is the decision that needs the
    least friction: a worker who wants out must not have to find a different screen, or wait
    for an administrator. The decision is appended to ``corpus_capture_consents`` (append-only
    at the database level) **and** to ``audit_log`` with the actor and request provenance, so
    "who said yes, when, from where" is answerable from two independent records.

    The endpoint answers identically whether capture is enabled or not: the consent is the
    worker's decision about *their* face, and the deployment switch is the operator's decision
    about the site. One must not gate the other - a worker opt-in recorded while capture is
    off stays on the record for the day the operator turns it on, and a worker is never asked
    to consent twice because an operator toggled a variable.
    """
    granted = payload.granted
    with db(write=True) as conn:
        consent_id = corpus.record_consent(
            conn,
            worker_id=current.id,
            granted=granted,
            actor_id=current.id,
            note=payload.note,
            ip=request.client.host if request.client else None,
        )
        _audit(
            conn,
            action="corpus_capture_consent",
            actor=current,
            entity="corpus_capture_consents",
            entity_id=consent_id,
            after={"granted": granted, "note": payload.note},
            request=request,
        )
    return {"status": "success", "granted": granted, "consent_id": consent_id}


@router.post("/worker/me/push/subscribe")
async def subscribe_my_push(
    request: Request,
    payload: PushSubscriptionRequest,
    current: CurrentUser = Depends(any_authenticated),
):
    """Register this browser so a notification can reach it with the app closed.

    Refused (409, with the reason) when the deployment cannot push at all: storing a
    subscription nothing will ever send to would make the app promise alerts it cannot keep.
    """
    usable, reason = push.transport_available()
    if not usable:
        raise HTTPException(status_code=409, detail=f"Push notifications are unavailable: {reason}")
    user_agent = request.headers.get("user-agent")
    try:
        with db(write=True) as conn:
            created = push.subscribe(
                conn,
                worker_id=current.id,
                endpoint=payload.endpoint,
                p256dh=payload.p256dh,
                auth=payload.auth,
                user_agent=user_agent,
            )
    except ValueError as exc:
        # The endpoint is a URL this server will later POST to, so a malformed one is refused
        # where it is typed rather than stored and discovered by a failing push.
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "success", "created": bool(created)}


@router.post("/worker/me/push/unsubscribe")
async def unsubscribe_my_push(
    payload: PushUnsubscribeRequest, current: CurrentUser = Depends(any_authenticated)
):
    """Turn notifications off for this device. Idempotent: a second call is not an error."""
    with db(write=True) as conn:
        retired = push.unsubscribe(conn, worker_id=current.id, endpoint=payload.endpoint)
    return {"status": "success", "retired": bool(retired)}


# ---------------------------------------------------------------------------
# attendance
# ---------------------------------------------------------------------------
@router.post("/attendance/verify")
@limiter.limit(settings.attendance_rate_limit)
async def verify_worker(
    request: Request,
    worker_id: str = Form(...),
    action: str = Form(...),
    location_input: str = Form(...),
    selfie: UploadFile = File(...),
    #: Sent back by the phone after it has shown the early clock-out warning and the
    #: worker chose to go ahead. Absent on the first attempt, which is what makes the
    #: warning happen: the server, not the phone, decides whether a shift is short.
    confirm_early_checkout: str | None = Form(default=None),
    current: CurrentUser = Depends(any_authenticated),
):
    """Clock in or out: session + geofence + face, in that order.

    **The session is the credential.** This endpoint used to also demand the worker's
    password in the form - a second time, seconds after they typed it to sign in - and
    that repeat was removed on purpose. It protected nothing a password can protect: it
    is typed on a phone, in the sun, standing next to colleagues, and a mistyped one
    answered 401, which signed the worker out mid-punch and lost them the tap. What
    actually guards the record is what follows it - a token that dies when the password
    is rotated or the account is deactivated, the geofence, and a live face that has to
    match the enrolled template (``liveness.py``, then the check in ``face_engine``).

    The subject is ``current.id`` - the ``worker_id`` form field is accepted only
    so the existing client keeps working, and is rejected if it names anybody
    other than the token's owner.
    """
    if str(worker_id) != current.id:
        raise HTTPException(
            status_code=403, detail="You may only record attendance for your own account."
        )
    if action not in (ACTION_CLOCK_IN, ACTION_CLOCK_OUT):
        raise HTTPException(status_code=400, detail="Action must be 'Clock In' or 'Clock Out'.")
    # The worker has already seen the warning and chosen to clock out anyway.
    confirmed_early = _form_flag(confirm_early_checkout)

    # The account is read for the name that the alert, the audit row and the response
    # carry - never for a credential. ``any_authenticated`` has already checked the one
    # that matters: the token's signature and its ``token_version`` against this row, so a
    # rotated password or a deactivated account is refused before anything is recorded.
    # Reading it from the database rather than a per-process cache is what makes that
    # refusal effective on every ASGI worker at once.
    with db() as conn:
        user_row = conn.execute(
            "SELECT id, name, biometric_id FROM users WHERE id = ?", (current.id,)
        ).fetchone()
    if user_row is None:
        # The token names an account that is gone: the session is dead, not the form.
        raise HTTPException(status_code=401, detail="Invalid token")

    lat, lon = parse_location_input(location_input)
    validate_plausible_coordinates(lat, lon)

    with db() as conn:
        # The window columns come back with the row: the punch is about to be measured against
        # *this* site's shift, and re-reading the row later would be a second query that could
        # disagree with the geofence that just matched (an administrator editing the site
        # between the two). One read, one answer to "which site is this, and what are its hours".
        detected_site_row = site_at(conn, lat, lon)

    detected_site = detected_site_row["site_name"] if detected_site_row is not None else None

    if not detected_site:
        raise HTTPException(
            status_code=403,
            detail="Location Rejected. You are outside any designated construction site geofence.",
        )

    # One upload policy for the whole app (size while reading, image type from the
    # bytes, pixel ceiling): see ``uploads.py``. This endpoint used to read the body
    # with no limit at all and hand it to PIL, so an oversized POST was a way to make
    # the server allocate its own size in memory.
    file_bytes = await uploads.read_photo(selfie, field="selfie")
    image = uploads.decode_photo(file_bytes, field="selfie")
    # The pixel ceiling for the frame the detector sees. 640 used to be the number; the
    # working-distance band (0.7-1.5 m, see the framing coach) puts a face at 53-113 px in
    # a 640-wide frame, and the alignment template wants ~112 - so beyond ~0.7 m the crop
    # was being upscaled before it was embedded, and every extra metre cost match score.
    # 1280 keeps the crop at native scale through ~1.5 m. Latency scales with frame area,
    # not linearly: YuNet on a 1280x960 frame costs ~55 ms against ~15 ms at 640, and the
    # embedding itself is size-independent (always a 112x112 crop).
    image.thumbnail((settings.punch_selfie_max_px, settings.punch_selfie_max_px))
    # ``rgb_array`` feeds the liveness model, which was trained on RGB crops;
    # ``img_array`` is the BGR view the detector and the embedding contract expect - see
    # ``face_onnx``, which does not convert channels anywhere. Computing both here keeps
    # the two consumers from silently swapping channel order.
    rgb_array = np.array(image)
    img_array = rgb_array[:, :, ::-1]

    # --- passive liveness, BEFORE any face embedding --------------------------
    # MiniFASNet at 80x80 costs a fraction of a VGG-Face embedding with an MTCNN
    # detector, so a presentation attack is refused without paying for the expensive
    # path - and without giving an attacker a CPU exhaustion lever.
    try:
        liveness_decision = await face_engine.ENGINE.run_async(liveness.inspect, rgb_array)
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    liveness_class, liveness_score = liveness_decision.log_fields()
    liveness_flag = None
    if not liveness_decision.allowed:
        with db(write=True) as conn:
            notifications.notify(
                conn,
                kind=notifications.KIND_LIVENESS_SPOOF,
                severity=notifications.SEVERITY_CRITICAL,
                title="Presentation attack refused",
                body=(
                    f"A {action.lower()} for {user_row['name']} (id {current.id}) was refused by the "
                    f"liveness check ({liveness_decision.result.verdict}: {liveness_decision.result.detail})."
                ),
                worker_id=current.id,
                site_name=None,
                payload={"liveness": liveness_decision.as_payload(), "action": action},
                dedupe_key=f"spoof:{current.id}:{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
            _audit(
                conn,
                action="liveness_rejected",
                actor=current,
                entity="attendance_logs",
                entity_id=current.id,
                after={"action": action, "liveness": liveness_decision.as_payload()},
                request=request,
            )
        telemetry.observe_verification("liveness_spoof")
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": liveness_decision.error_code or liveness.ERR_SPOOF,
                "message": (
                    "Liveness check failed. Hold the camera so your face fills the frame - "
                    "a photo of a photo, or a screen, cannot be used to clock in."
                ),
                "liveness": liveness_decision.as_payload(),
            },
        )
    if liveness_decision.result.verdict != liveness.VERDICT_LIVE:
        # Advisory mode: recorded, flagged and notified, but not blocked. This is how a
        # threshold gets calibrated against real site traffic before it may reject anybody.
        liveness_flag = f"liveness {liveness_decision.result.verdict}"
        with db(write=True) as conn:
            notifications.notify(
                conn,
                kind=(
                    notifications.KIND_LIVENESS_SPOOF
                    if liveness_decision.result.verdict in {liveness.VERDICT_SPOOF, liveness.VERDICT_LOW_CONFIDENCE}
                    else notifications.KIND_LIVENESS_DEGRADED
                ),
                severity=notifications.SEVERITY_WARNING,
                title="Liveness check did not confirm a genuine face",
                body=(
                    f"{user_row['name']} (id {current.id}) clocked with liveness "
                    f"'{liveness_decision.result.verdict}' ({liveness_decision.result.detail}). "
                    "Recorded, not blocked, because LIVENESS_MODE is advisory."
                ),
                worker_id=current.id,
                payload={"liveness": liveness_decision.as_payload()},
                dedupe_key=f"liveness_advisory:{current.id}:{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )

    reference_filepath = biometrics.resolve_reference(current.id, biometrics.id_from(user_row))
    if not os.path.exists(reference_filepath):
        # One wall, two audiences. A worker cannot register a template for themselves - that
        # is the company's act, through an enrollment link or an administrator - so they are
        # told to ask. An administrator can, from the console (``/worker/me/enroll``), and
        # telling them to contact their administrator when they are one is a dead end with a
        # phone number in it.
        raise HTTPException(
            status_code=404,
            detail=(
                "Facial reference not registered. Register your own photo from the console, "
                "then clock in again."
                if current.role in SELF_ENROLL_ROLES
                else "Facial reference not registered. Please contact your administrator to enroll."
            ),
        )

    try:
        # One pool slot for the comparison. This used to run on anyio's shared thread pool,
        # where forty simultaneous punches were forty simultaneous TensorFlow calls with
        # nothing bounding them; the engine owns that capacity now, and a full queue is
        # answered with 503 + Retry-After instead of an invisible pile-up.
        face_data = await face_engine.ENGINE.run_async(
            compare_faces_sync, reference_filepath, img_array
        )
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    if face_data.get("error"):
        # A photo the check cannot use (no face, two faces, no reference) is the worker's
        # to fix and answers 400 with the sentence that says how. Anything else is ours,
        # so it answers 500 - asking the worker to retry a server fault, in words that
        # blame their photo, is how a bug survives a week of "it keeps saying no".
        reason = str(face_data["error"])
        error_code, message = _frame_refusal(reason)
        # A photo the check could not use is its own outcome, never lumped in with a mismatch:
        # "no face in the frame" and "that is not this worker" have different causes and
        # different fixes, and a review queue built from the sum of them cannot be acted on.
        telemetry.observe_verification("frame_refused")
        if error_code == FACE_CHECK_FAILED[0]:
            log.warning("face check failed for worker %s: %s", current.id, reason)
            raise HTTPException(status_code=500, detail={"error_code": error_code, "message": message})
        raise HTTPException(status_code=400, detail={"error_code": error_code, "message": message})

    similarity_score = face_data["distance"]
    # The band, resolved here rather than read off ``face_data`` above: a caller is free to
    # stub ``compare_faces_sync`` (the suite does), and a verdict path that only works when a
    # particular payload shape came back is a verdict path that silently stops being tested.
    # ``biometrics.current_pipeline()`` is the same name the staleness check just compared the
    # template against, so the lines read here are the ones that crop was measured for.
    band = face_detector.band_for(biometrics.current_pipeline())
    verdict = band.classify(similarity_score)
    # The frame the score was measured from, kept as the punch's evidence: a pending review
    # is a decision about money, and "matched at 0.52" without the picture is a claim without
    # anything to check it against (see ``punch_frames``). Stored *after* the verdict so a
    # refused punch - 422, the worker still standing there - writes no file at all, and
    # discarded on the domain refusals below, which leave no log row to claim it.
    punch_frame = None
    try:
        punch_frame = punch_frames.store_frame(image)
    except OSError:
        # A punch that cannot keep its evidence is still a punch: refusing it would lose an
        # honest worker's hours over a disk. The row carries its numbers without a picture,
        # exactly like the rows written before frames existed.
        log.warning("could not store the punch frame for worker %s", current.id, exc_info=True)
    # The same frame again, when an operator has asked for calibration captures: the corpus keeps
    # the *worker* (that is the label) and nothing about their hours - see ``corpus`` for why it is a
    # separate store with a separate switch rather than a column on this punch. Off by default,
    # never raises, and it re-runs the detector on its own downscaled copy so the crop it stores
    # reproduces exactly for whoever measures it later.
    corpus.maybe_capture_punch(image, worker_id=str(current.id), verdict=verdict)
    if verdict == face_detector.MATCH_APPROVED:
        status_val = "success"
        status_msg = "Auto-Approved"
        log_status = STATUS_APPROVED
        status_code = "approved"
        telemetry.observe_verification("approved")
    elif verdict == face_detector.MATCH_REVIEW:
        status_val = "flagged"
        status_msg = "Attendance logged - Pending HR Review"
        log_status = STATUS_PENDING_REVIEW
        status_code = "pending_review"
        telemetry.observe_verification("flagged_review")
    else:
        # 422, never 401. A mismatch is a fact about this frame, not about the session -
        # and 401 in this app means one thing to the client: the token is unusable, so
        # sign the worker out and repaint the login screen (see ``BODY_CREDENTIAL_ENDPOINTS``
        # in frontendjavascript.js). Answering 401 here did exactly that, so a hat, a
        # shadow or the wrong face ended the shift's session and the worker was told
        # "Your session expired. Sign in again." - at the gate, on a phone, needing the
        # password this app exists to not ask for - while the real reason (the score) was
        # thrown away. The liveness refusal above already answers 422 with this same
        # {error_code, message} shape, which the client renders as a readable message.
        telemetry.observe_verification("rejected")
        # A refusal used to discard the frame and leave nothing at all: the score went into
        # the 422 body and vanished, and an operator whose workers were all refused could see
        # neither the scores nor the faces. The record below is the triage surface - the
        # frame, the score and the reason kept for the day, readable in the console and
        # swept by retention like any other evidence store. Best-effort on both halves: a
        # refusal must not fail *because* recording it failed, and the 422 below is the
        # worker's answer either way.
        try:
            _record_refused_punch(
                conn=None,
                worker_id=str(current.id),
                site_name=detected_site,
                action=action,
                error_code="face_mismatch",
                score=float(similarity_score),
                punch_frame=punch_frame,
                source="online",
            )
        except Exception:  # noqa: BLE001 - the refusal itself must survive a recording failure
            log.warning("could not record the refused punch for worker %s", current.id, exc_info=True)
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "face_mismatch",
                # The number is kept for the record - the log line, the admin triage
                # view, a support call - and never shown to the worker: "Score: 0.7589"
                # is not something a person on a site can act on. The sentence is.
                "score": similarity_score,
                "message": (
                    "We could not confirm that this is you. Fill the frame with your face, "
                    "in even light, and take it again - if it keeps failing, ask your "
                    "administrator to check the photo on your account."
                ),
            },
        )

    # The rules travel as they are: which hours the overtime line counts, where it sits and
    # how much of the shift it holds back are all decided by ``overtime_assessment`` below,
    # not by reading columns here.
    rules = get_shift_rules()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    hours_worked = 0.0
    break_taken = 0.0
    #: The hours past the paid day that this clock-out is holding back. Named in full, not
    #: ``overtime``: the module of that name is what announces the crossing below, and a
    #: local variable shadowing it is a bug waiting for the next reader.
    overtime_hours = 0.0
    #: The worker's crossing notice, when this clock-out made one (``overtime.announce_crossing``).
    #: Delivered after the transaction below, because a push cannot see an uncommitted row.
    crossing = None
    flag_reason = liveness_flag

    with db(write=True) as conn:
        if action == ACTION_CLOCK_IN:
            existing = conn.execute(
                "SELECT worker_id FROM active_sessions WHERE worker_id = ?", (current.id,)
            ).fetchone()
            if existing:
                # The frame goes with the refusal: this rollback writes no row, so the file
                # would otherwise sit unclaimed until retention's residue pass found it.
                punch_frames.discard_frame(punch_frame)
                raise HTTPException(status_code=400, detail="Already clocked in!")

            # The window is the *detected site's*, not the global one: a night site and a day
            # site cannot share a shift, and the global rule is only the fallback for a site
            # that has not configured its own (``shift_windows``).
            site_window = shift_windows.effective_window(detected_site_row, rules)
            late_flag = None if site_window.contains_moment(now) else shift_windows.describe(site_window)
            conn.execute(
                "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source, late_flag, liveness_class) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (current.id, detected_site, now_str, "online", late_flag, liveness_class),
            )
            status_msg += f" (Clocked In at {detected_site})"
            if late_flag:
                flag_reason = " | ".join(part for part in (flag_reason, late_flag) if part)
                status_msg += " [arrival outside the standard window]"
                notifications.notify(
                    conn,
                    kind=notifications.KIND_LATE_ARRIVAL,
                    severity=notifications.SEVERITY_WARNING,
                    title="Arrival outside the clock-in window",
                    # The window named here is the one that was actually applied, read from the
                    # resolved site window rather than formatted from the global rule - which is
                    # how this message used to hardcode "04:00-06:30" at a site that had a
                    # different shift entirely, sending an administrator to change a setting
                    # that was not the one in force.
                    body=(
                        f"{user_row['name']} (id {current.id}) clocked in at {now_str}, "
                        f"outside {site_window.site_name or detected_site}'s "
                        f"{site_window.label()} clock-in window."
                    ),
                    worker_id=current.id,
                    site_name=detected_site,
                    dedupe_key=f"late:{current.id}:{now_str[:10]}",
                )
        else:
            flagged = conn.execute(
                "SELECT id FROM attendance_logs WHERE worker_id = ? AND status = ? LIMIT 1",
                (current.id, STATUS_PENDING_REVIEW),
            ).fetchone()
            if flagged:
                # Raising rolls the transaction back, so the log row this punch was about to
                # write never lands - which means the frame would be left on disk with nothing
                # claiming it. Discarded here rather than left for retention's residue pass,
                # for the same reason the face-mismatch refusal discards: these are the two
                # refusals that happen *after* the frame was stored, and both leave no row.
                punch_frames.discard_frame(punch_frame)
                raise HTTPException(
                    status_code=403, detail="Your account is flagged for manual review. Please contact HR."
                )

            session = conn.execute(
                "SELECT clock_in_time, site_name FROM active_sessions WHERE worker_id = ?", (current.id,)
            ).fetchone()
            if session is None:
                punch_frames.discard_frame(punch_frame)
                raise HTTPException(status_code=400, detail=_no_open_shift_message(conn, current.id))

            clock_in_time = _parse_ts(session["clock_in_time"])
            if clock_in_time is None:
                raise HTTPException(status_code=400, detail="The stored clock-in time is unreadable.")

            # Time on site first, then what is paid for it: the unpaid break belongs to
            # the shift (and is stored on it), not to the person closing it, so the
            # worker's own clock-out, an administrator's force-clock-out and the
            # auto-close all arrive at the same number through ``shift_hours``.
            #
            # Measured in whole seconds between the two stored timestamps, and that is the
            # figure the overtime decision is made on too - see ``overtime_assessment``.
            seconds_on_site = shift_hours.elapsed_seconds(clock_in_time, now)
            record = shift_hours.recorded_shift(seconds_on_site / 3600.0, rules)
            if record["needs_confirmation"] and not confirmed_early:
                # Refused *before* anything is written and before the session is deleted,
                # so a cancel leaves the shift exactly as it was and the confirmed request
                # that follows records the very same hours. Raising from inside the
                # transaction is the rollback the "flagged for review" refusal has always
                # used, and the phone renders the sentence itself from the numbers below -
                # the English here is the API's own, for anything that is not the app.
                # No row is written here either, so the frame goes with the refusal.
                punch_frames.discard_frame(punch_frame)
                raise HTTPException(status_code=409, detail=_early_checkout_refusal(record))
            hours_worked = record["paid_hours"]
            break_taken = record["break_hours"]
            hours_note = record["description"]
            conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (current.id,))

            # Overtime is tracked, not silently approved: the one resolver decides whether
            # this shift has reached the line, and how much of it is held back.
            #
            # ``apply_authorisation`` is where a crossing answered mid-shift is cashed in: the
            # ceiling somebody stated for this shift decides what is held, so the clock-out does
            # not ask again for hours that were already decided about. With no answer it returns
            # the assessment unchanged - the ceiling defaults to the regular paid day, which is
            # the arithmetic this line always did.
            assessment = overtime.apply_authorisation(
                conn,
                current.id,
                clock_in_time,
                shift_hours.overtime_assessment(seconds_on_site, rules),
                rules,
            )
            if assessment["needs_approval"]:
                overtime_hours = assessment["overtime_hours"]
                log_status = STATUS_PENDING_OVERTIME
                status_code = "pending_overtime"
                status_val = "flagged"
                flag_reason = " | ".join(
                    part for part in (flag_reason, assessment["flag_sentence"]) if part
                )
                status_msg = (
                    f"Clocked Out of {session['site_name']}. {hours_note} - "
                    "overtime requires admin approval before payroll"
                )
                notifications.notify(
                    conn,
                    kind=notifications.KIND_REVIEW_PENDING,
                    severity=notifications.SEVERITY_WARNING,
                    title="Overtime requires approval",
                    body=(
                        f"{user_row['name']} (id {current.id}) worked "
                        f"{assessment['paid_hours']:.2f}h paid at '{session['site_name']}', "
                        f"past the {assessment['threshold_hours']:g}h overtime line with "
                        f"{overtime_hours:.2f}h past the paid day. Approve or adjust the extra "
                        "hours before payroll."
                    ),
                    worker_id=current.id,
                    site_name=session["site_name"],
                    dedupe_key=f"overtime:{current.id}:{now_str}",
                    payload={
                        "hours": hours_worked,
                        "overtime_hours": overtime_hours,
                        "basis": assessment["basis"],
                        "paid_hours": assessment["paid_hours"],
                        "threshold_hours": assessment["threshold_hours"],
                    },
                )
                # ... and the worker is told, in their own inbox, in the same transaction.
                # The watcher cannot do this for them: it only sees shifts that are still
                # open, and this one has just ended. ``announce_crossing`` is the one writer
                # of that sentence, shared with the watcher and the other three paths.
                crossing = overtime.announce_crossing(
                    conn,
                    worker_id=current.id,
                    site_name=session["site_name"],
                    clock_in_time=session["clock_in_time"],
                    values=rules,
                    moment=now,
                )
            else:
                status_msg += f" (Clocked Out of {session['site_name']}. {hours_note})"

        log_id = _insert_log(
            conn,
            worker_id=current.id,
            site_name=detected_site,
            action=action,
            timestamp=now_str,
            hours=hours_worked,
            score=similarity_score,
            status=log_status,
            status_code=status_code,
            lat=lat,
            lon=lon,
            source="online",
            liveness_class=liveness_class,
            liveness_score=liveness_score,
            flag_reason=flag_reason,
            overtime_hours=overtime_hours or None,
            break_hours=break_taken or None,
            punch_frame=punch_frame,
        )
        if action == ACTION_CLOCK_OUT:
            # The shift has settled, so the decision it was settled by is cashed in against
            # this very row: an answer cannot authorise a second shift, and the record says
            # which clock-out spent it. Guarded on the action because an *arrival* settles no
            # shift and has no clock-in of its own to pair a decision with - and one row is
            # written for both, so asking unconditionally is an unbound name at the top of
            # every working day.
            overtime.consume_authorisation(conn, current.id, clock_in_time, log_id)
        if status_code == "pending_review":
            notifications.notify(
                conn,
                kind=notifications.KIND_REVIEW_PENDING,
                severity=notifications.SEVERITY_WARNING,
                title="Biometric match needs review",
                body=(
                    f"{user_row['name']} (id {current.id}) matched at distance {similarity_score}; "
                    f"the {action.lower()} was logged and is awaiting review."
                ),
                worker_id=current.id,
                site_name=detected_site,
                log_id=log_id,
                payload={"score": similarity_score},
                dedupe_key=f"review:{log_id}",
            )
        # The business counter, recorded where the row is actually written: a punch that was
        # verified and then refused for a domain reason (already clocked in, no open shift,
        # account flagged) never reaches this line, which is exactly the distinction an
        # operator wants between "verification is failing" and "the shift state is wrong".
        telemetry.observe_punch(
            action="clock_in" if action == ACTION_CLOCK_IN else "clock_out",
            status=str(log_status),
        )
        _audit(
            conn,
            action=f"attendance_{'clock_in' if action == ACTION_CLOCK_IN else 'clock_out'}",
            actor=current,
            entity="attendance_logs",
            entity_id=log_id,
            after={
                "site": detected_site,
                "score": similarity_score,
                "hours": hours_worked,
                "break_hours": break_taken,
                "status": log_status,
            },
            request=request,
        )

    # Committed, so the notice is visible to the dispatcher - and on its own thread, so a
    # worker standing at a gate is not waiting on somebody else's push service.
    overtime.deliver_worker_notices(crossing)

    return {
        "status": status_val,
        "message": status_msg,
        "score": similarity_score,
        # ``hours`` is what is payable, which is what this field has always meant. The
        # break travels beside it rather than inside it, so the phone can show both.
        "hours": hours_worked,
        "break_hours": break_taken,
        "paid_hours": hours_worked,
        "site": detected_site,
        "overtime_hours": overtime_hours,
        "liveness": liveness_decision.as_payload(),
    }


def _within_clock_in_window(rules: Mapping[str, Any], moment: datetime | None = None) -> bool:
    """Whether *now* - or ``moment`` - is inside the window described by ``rules``.

    A thin wrapper over ``shift_windows``, kept because it is the name the punch paths and
    their tests already use, and because it has to resolve "now" *here*: the suite freezes
    ``main.datetime`` to pin the boundaries, and a helper that resolved the clock inside
    ``shift_windows`` would ignore that monkeypatch and silently test a different hour.

    ``rules`` may be a site's resolved window, a site row or the global rules - anything with
    the three window keys. Prefer passing a resolved window: this one has no site row, so it
    can only fall back to the global rules for whatever ``rules`` does not carry.
    """
    if isinstance(rules, shift_windows.Window):
        window = rules
    else:
        window = shift_windows.effective_window(rules, rules)
    return window.contains_moment(moment if moment is not None else datetime.now())


# ---------------------------------------------------------------------------
# admin: users
# ---------------------------------------------------------------------------
@router.get("/admin/users")
async def list_users(current: CurrentUser = Depends(admin_only)):
    """Every account, with the state an administrator needs to hand out access.

    This is the credentials roster, so it answers the questions an admin has when a
    worker calls: is the account active, has a face been enrolled, has a password ever
    been set (and when), and how many sessions did the last reset invalidate.

    ``password_hash`` is deliberately absent and must stay absent. A bcrypt hash is a
    credential, not information: shipping it to the browser would let anyone who can
    read a response - a shared screen, a saved HAR file, a proxy log - run an offline
    crack against every password in the company. The plaintext is not returned either,
    because it does not exist anywhere: the hash is one-way by design.
    """
    # The root account is concealed from every administrator, so the roster is narrowed *in
    # the query* rather than after it: a list filtered in Python still hands back the count of
    # what it removed, and a count is an enumeration. A developer session sees itself, which is
    # how the account is edited at all.
    hide_sql, hide_params = developer.visibility_clause(current, column="id")
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, email, phone, role, status, enrolled_at, biometric_id, "
            "COALESCE(token_version, 0) AS token_version, "
            "(password_hash IS NOT NULL AND password_hash <> '') AS password_set "
            "FROM users WHERE 1 = 1" + hide_sql + " ORDER BY CAST(id AS INTEGER) ASC",
            hide_params,
        ).fetchall()
        # "When was the password last set?" is already recorded - append-only, in the
        # audit log - so it is read from there rather than duplicated into the users
        # table, where it would be one more column to keep in step with the writes.
        changed = {
            row["entity_id"]: row["last_change"]
            for row in conn.execute(
                "SELECT entity_id, MAX(created_at) AS last_change FROM audit_log "
                "WHERE action IN ('password_reset', 'password_change_self') "
                "AND entity_id IS NOT NULL GROUP BY entity_id"
            ).fetchall()
        }
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "role": row["role"],
            "status": row["status"],
            # Whether the worker can clock in is decided by the stored reference file
            # (``/attendance/verify`` answers 404 without it), so that is what is
            # checked. ``enrolled_at`` is the metadata beside it, not the gate: a
            # template removed by hand must read as "not enrolled", because it is.
            # The lookup goes through ``biometrics`` so this answer and the one
            # ``/attendance/verify`` acts on cannot drift apart - the file is named by
            # the account's immutable id, not by the account id in this row.
            "face_enrolled": biometrics.is_enrolled(row["id"], biometrics.id_from(row)),
            "enrolled_at": row["enrolled_at"],
            "password_set": bool(row["password_set"]),
            "password_changed_at": changed.get(row["id"]),
            "sessions_revoked": row["token_version"],
        }
        for row in rows
    ]


@router.post("/admin/users/add")
async def add_user(
    request: Request, req: UserAddRequest, current: CurrentUser = Depends(admin_only)
):
    if current.role == "admin" and req.role in ("admin", "head_admin"):
        raise HTTPException(
            status_code=403, detail="Standard Admins cannot create admin or head admin accounts."
        )
    _validate_id_and_role(req.user_id, req.role)
    hashed = hash_password(req.password)

    with db(write=True) as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, name, email, phone, password_hash, role, status, biometric_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    req.user_id,
                    req.name,
                    req.email,
                    req.phone,
                    hashed,
                    req.role,
                    # An id here rather than waiting for the first enrollment: it is minted
                    # once per account, and this is the only moment where the account id
                    # could still have belonged to somebody else. ``new_account_id`` also
                    # clears any legacy-named file that id left behind, so this account can
                    # never resolve to that face (see ``biometrics``).
                    biometrics.new_account_id(req.user_id),
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="User ID already exists.")
        _audit(
            conn,
            action="user_create",
            actor=current,
            entity="users",
            entity_id=req.user_id,
            after={"name": req.name, "role": req.role},
            request=request,
        )
    return {"status": "success", "message": f"User {req.user_id} with role '{req.role}' successfully created."}


@router.post("/admin/users/create")
async def create_user(
    request: Request,
    user_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    email: str = Form(default=""),
    phone: str = Form(default=""),
    photo: UploadFile | None = File(default=None),
    current: CurrentUser = Depends(admin_only),
):
    """Create an account from the Credentials tab, with its face reference in one step.

    This is the console's counterpart to a registration link: the same account, created
    by an administrator instead of by the worker on their own phone. It is a separate
    endpoint from ``/admin/users/add`` rather than a replacement for it, because the two
    answer different questions - that one is JSON and creates a bare row, this one takes
    a photo and therefore a multipart body.

    The order is the one the public registration page uses, and for the same reason:
    the password policy, the upload policy, liveness and the face embedding all run
    *before* anything is written, so a photo that cannot become a template leaves no
    half-created account behind - an account that can sign in but never clock in is a
    support call, not a saved step. The row is written first and the template second,
    so a template can never land on an id that is still somebody else's.

    ``photo`` is optional: an account with no face is a legitimate thing to create (the
    worker will be enrolled later, by a link or from the enrollment tab), it just cannot
    clock in until then.
    """
    role = str(role or "").strip().lower()
    user_id = str(user_id).strip()
    if current.role == "admin" and role in ("admin", "head_admin"):
        raise HTTPException(
            status_code=403, detail="Standard Admins cannot create admin or head admin accounts."
        )
    _validate_id_and_role(user_id, role)
    # The same rules the JSON model applies, because this is the same door with a different
    # content type. A multipart endpoint that skipped the text checks would be the way past
    # them for anybody who read the code - and it is the endpoint the Credentials tab uses,
    # so it is the one a real administrator's browser actually posts to.
    try:
        name = textguard.identifier(name, field="Name", max_length=textguard.MAX_NAME)
        email = textguard.contact(email, field="Email")
        phone = textguard.contact(phone, field="Phone")
    except ValueError as exc:
        raise textguard.http_error(exc) from None
    # The same rule the worker would be held to on a registration link. A console that
    # accepted "1234" here would make the link's policy pointless.
    validate_password_strength(password)

    image = embedding = None
    decision = None
    if photo is not None:
        file_bytes = await uploads.read_photo(photo, field="photo")
        image = uploads.decode_photo(file_bytes, field="photo")
        try:
            # Model work goes through the engine even here: this call used to run on the
            # event loop, so one administrator uploading a photo froze every other request
            # in the process - including a worker at the gate - for the whole inference.
            decision, embedding = await face_engine.ENGINE.run_async(
                enrollment.embed_reference, image, stage="console_create"
            )
        except face_engine.FaceEngineBusy as exc:
            raise face_engine.busy_http_exception(exc) from None

    now = datetime.now()
    with db(write=True) as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, name, email, phone, password_hash, role, status, enrolled_at, "
                "template_version, biometric_id) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    user_id,
                    name,
                    email or "",
                    phone or "",
                    hash_password(password),
                    role,
                    now.strftime("%Y-%m-%d %H:%M:%S") if image is not None else None,
                    # ``0`` rather than NULL for an account with no face yet:
                    # ``template_version`` is NOT NULL, and a version is a counter, not a
                    # presence flag - whether the template exists is the file on disk.
                    1 if image is not None else 0,
                    biometrics.new_account_id(user_id),
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="User ID already exists.")
        _audit(
            conn,
            action="user_create",
            actor=current,
            entity="users",
            entity_id=user_id,
            after={
                "source": "credentials_console",
                "name": name,
                "role": role,
                "photo": image is not None,
                **({"liveness": decision.as_payload()} if decision is not None else {}),
            },
            request=request,
        )
        if image is not None:
            _audit(
                conn,
                action="biometric_enroll",
                actor=current,
                entity="users",
                entity_id=user_id,
                after={"source": "credentials_console", "template_replaced": False},
                request=request,
            )

    if image is not None:
        enrollment.write_reference(user_id, image, embedding)

    return {
        "status": "success",
        "user_id": user_id,
        "role": role,
        "face_enrolled": image is not None,
        "liveness": decision.as_payload() if decision is not None else None,
        "message": (
            f"User {user_id} created with a face reference."
            if image is not None
            else f"User {user_id} created. No photo supplied, so they cannot clock in yet."
        ),
    }


@router.post("/admin/admins/add")
async def add_admin(
    request: Request, req: UserAddRequest, current: CurrentUser = Depends(require_role("head_admin"))
):
    _validate_id_and_role(req.user_id, "admin")
    hashed = hash_password(req.password)
    with db(write=True) as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, name, email, phone, password_hash, role, status, biometric_id) "
                "VALUES (?, ?, ?, ?, ?, 'admin', 'active', ?)",
                (
                    req.user_id,
                    req.name,
                    req.email,
                    req.phone,
                    hashed,
                    biometrics.new_account_id(req.user_id),
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="User ID already exists.")
        _audit(
            conn,
            action="admin_create",
            actor=current,
            entity="users",
            entity_id=req.user_id,
            after={"name": req.name, "role": "admin"},
            request=request,
        )
    return {"status": "success", "message": f"Admin {req.user_id} successfully created."}


@router.post("/admin/users/edit_password")
async def edit_password(
    request: Request, req: PasswordEditRequest, current: CurrentUser = Depends(admin_only)
):
    """Reset another user's password and revoke their sessions.

    Bumping ``token_version`` is what makes this an actual revocation: without it
    a token stolen before the reset keeps working.
    """
    with db(write=True) as conn:
        target = conn.execute("SELECT role FROM users WHERE id = ?", (req.worker_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User ID not found.")
        if current.role == "admin" and target["role"] in ("admin", "head_admin"):
            raise HTTPException(status_code=403, detail="Standard Admins cannot change admin or head admin passwords.")

        hashed = hash_password(req.new_password)
        conn.execute(
            "UPDATE users SET password_hash = ?, token_version = COALESCE(token_version, 0) + 1 WHERE id = ?",
            (hashed, req.worker_id),
        )
        _audit(
            conn,
            action="password_reset",
            actor=current,
            entity="users",
            entity_id=req.worker_id,
            after={"target_role": target["role"]},
            request=request,
        )
    return {"status": "success", "message": f"Password for user {req.worker_id} successfully updated."}


@router.get("/admin/users/{user_id}")
async def read_user(user_id: str, current: CurrentUser = Depends(admin_only)):
    """One account, with the fields the roster payload deliberately leaves out.

    ``/admin/users`` is a fixed contract - an administrator scans it and every row of it
    is pinned by a test - so the editable detail that is *not* on it (the hourly rate, the
    contact fields) is read here instead of being bolted onto every row of the list.
    """
    # A concealed account reads as a missing one: the same 404, in the same words, as an id
    # that was never issued. A 403 here would confirm the account exists, which is the whole
    # thing the concealment is for.
    if developer.hides(current, user_id):
        raise HTTPException(status_code=404, detail="User ID not found.")
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="User ID not found.")
    return {
        "id": str(row["id"]),
        **_user_snapshot(row),
        "face_enrolled": biometrics.is_enrolled(row["id"], biometrics.id_from(row)),
        "enrolled_at": row["enrolled_at"],
    }


@router.post("/admin/users/edit")
async def edit_user(
    request: Request, req: UserEditRequest, current: CurrentUser = Depends(admin_only)
):
    """Change an account's own details: name, contact and pay rate.

    What an administrator can change is on ``UserEditRequest``, and what they cannot is
    there too - it is a shorter list, and the reasons are the interesting part.

    Everything that changes is recorded in the append-only log, ``before`` beside
    ``after``: "who renamed this account, and when" is a question the roster cannot answer
    and the log can.

    The hourly rate is money: it is what the shifts report multiplies approved hours by,
    so it is validated as a number in a sane range and a rate of 0 clears it rather than
    paying somebody nothing per hour.
    """
    user_id = str(req.user_id).strip()
    name = str(req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name must not be empty.")
    email = str(req.email or "").strip()
    phone = str(req.phone or "").strip()

    with db(write=True) as conn:
        target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User ID not found.")
        _guard_standard_admin(current, target, "edit")

        before = _user_snapshot(target)
        changes: dict[str, Any] = {"name": name, "email": email, "phone": phone}

        if req.hourly_rate is not None:
            rate = float(req.hourly_rate)
            if not 0.0 <= rate <= 1000.0:
                raise HTTPException(
                    status_code=400, detail="hourly_rate must be between 0 and 1000."
                )
            changes["hourly_rate"] = rate or None

        assignments = ", ".join(f"{key} = ?" for key in changes)
        conn.execute(
            f"UPDATE users SET {assignments} WHERE id = ?", (*changes.values(), user_id)
        )
        after = {**before, **changes}
        _audit(
            conn,
            action="user_edit",
            actor=current,
            entity="users",
            entity_id=user_id,
            before=before,
            after=after,
            request=request,
        )
    return {
        "status": "success",
        "user_id": user_id,
        "user": after,
        "message": f"User {user_id} updated.",
    }


@router.post("/admin/users/status")
async def set_user_status(
    request: Request, req: UserStatusRequest, current: CurrentUser = Depends(admin_only)
):
    """Deactivate an account, or bring it back.

    Deactivating is what "remove this person's access" means once they have history. The
    account stops being able to sign in (``/auth/login`` refuses a deactivated row), every
    outstanding token stops verifying (``token_version`` is bumped), the offline signing
    keys on their phone are revoked, any live enrollment link is revoked, and the face
    template is deleted from disk - a departed worker's face should not stay enrolled for
    a clock-in they will never make.

    Reactivating restores the sign-in and nothing else: the template is gone, so the
    roster shows the account as not enrolled until somebody takes their photo again. That
    is the honest trade (the alternative is keeping biometric data for people who no
    longer work here), and the console says so before it deactivates.

    Deactivating the account you are signed in with is refused, and that is the whole of
    the lockout rule - see ``delete_user`` for why no separate "last head admin" case is
    needed.
    """
    user_id = str(req.user_id).strip()
    wanted = bool(req.active)
    verb = "reactivate" if wanted else "deactivate"
    if not wanted:
        _refuse_self_account(current, user_id, "deactivate")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with db(write=True) as conn:
        target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User ID not found.")
        _guard_standard_admin(current, target, verb)

        already = str(target["status"] or "active").strip().lower()
        if not wanted and already != "active":
            # Idempotent, and it must not bump the session version a second time: a
            # repeated click would otherwise revoke nothing and log nothing new while
            # telling the administrator it did.
            return {
                "status": "success",
                "user_id": user_id,
                "active": False,
                "message": f"User {user_id} is already deactivated.",
            }
        if wanted and already == "active":
            return {
                "status": "success",
                "user_id": user_id,
                "active": True,
                "message": f"User {user_id} is already active.",
            }

        revoked: dict[str, int] = {}
        if wanted:
            conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (user_id,))
        else:
            revoked = _revoke_user_access(conn, user_id, now=now)
            conn.execute(
                "UPDATE users SET status = 'inactive', "
                "token_version = COALESCE(token_version, 0) + 1 WHERE id = ?",
                (user_id,),
            )
        _audit(
            conn,
            action=f"user_{verb}",
            actor=current,
            entity="users",
            entity_id=user_id,
            before=_user_snapshot(target),
            after={"status": "active" if wanted else "inactive", **revoked},
            request=request,
        )

    removed: list[str] = []
    failed: list[str] = []
    if not wanted:
        removed, failed = _delete_biometric_files(user_id)
    return {
        "status": "success",
        "user_id": user_id,
        "active": wanted,
        "face_removed": removed,
        "face_removal_failed": failed,
        "devices_revoked": revoked.get("devices_revoked", 0),
        "invites_revoked": revoked.get("invites_revoked", 0),
        "message": (
            f"User {user_id} reactivated. They cannot clock in until a face is enrolled again."
            if wanted
            else f"User {user_id} deactivated: no access, history kept."
        ),
    }


@router.post("/admin/users/delete")
async def delete_user(
    request: Request, req: UserDeleteRequest, current: CurrentUser = Depends(admin_only)
):
    """Delete an account with nothing attached to it, and refuse when it has history.

    THE RULE: an account with attendance records is not deletable, it is deactivatable.

    A worker's shifts are the record of work that was done and of approved hours that are
    owed. The reports ``LEFT JOIN`` the account, so deleting it would leave those rows
    standing with an id nobody can read the name of - the person would vanish from a
    report that is still being paid from. The hours win, so this refuses, names the count
    and points at ``/admin/users/status``, which removes access and touches no history.

    What is left is the case this endpoint exists for: an account created by mistake, an
    id typed for the wrong man, a duplicate. Those have no shifts, so the row and
    everything that only makes sense beside it - device keys, queued punches, live
    invitation links - go together, audited with what went with it, and the face template
    and selfie are removed from disk afterwards.

    The console cannot be left without a head admin, and that is not a special case here:
    the only account that could be the last one is the account you are signed in as, which
    this endpoint refuses (``_refuse_self_account``), and the id ranges stop a head admin
    from being renamed out of the role (5000 is not a valid admin id).
    """
    user_id = str(req.user_id).strip()
    _refuse_self_account(current, user_id, "delete")

    with db(write=True) as conn:
        target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User ID not found.")
        _guard_standard_admin(current, target, "delete")

        open_shift = conn.execute(
            "SELECT site_name, clock_in_time FROM active_sessions WHERE worker_id = ?",
            (user_id,),
        ).fetchone()
        if open_shift is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{target['name']} has been clocked in at {open_shift['site_name']} "
                    f"since {open_shift['clock_in_time']}; close that shift first."
                ),
            )

        shifts = int(
            conn.execute(
                "SELECT COUNT(*) AS shifts FROM attendance_logs WHERE worker_id = ?",
                (user_id,),
            ).fetchone()["shifts"]
        )
        if shifts:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{target['name']} has {shifts} attendance record(s). Deleting the "
                    "account would take those shifts out of the reports they are paid "
                    "from, so it is refused. Deactivate the account instead: it removes "
                    "their access and keeps their hours."
                ),
            )

        before = _user_snapshot(target)
        # With the account gone a revoked key has nothing left to protect, and the
        # "one phone per worker" limit is counted from ``worker_devices``, so the rows
        # themselves go rather than being left behind marked dead.
        devices = conn.execute(
            "DELETE FROM worker_devices WHERE worker_id = ?", (user_id,)
        ).rowcount
        invites = conn.execute(
            "DELETE FROM enrollment_invites WHERE worker_id = ?", (user_id,)
        ).rowcount
        queued = conn.execute(
            "DELETE FROM punch_queue WHERE worker_id = ?", (user_id,)
        ).rowcount
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        _audit(
            conn,
            action="user_delete",
            actor=current,
            entity="users",
            entity_id=user_id,
            before=before,
            after={
                "deleted": True,
                "attendance_records": 0,
                "open_shift": False,
                "devices_removed": int(devices or 0),
                "invites_removed": int(invites or 0),
                "punches_removed": int(queued or 0),
            },
            request=request,
        )

    # After the commit, because the files are not part of the transaction and the account
    # is already gone: a failure to remove one is reported to the caller as well as
    # logged in the response, never swallowed.
    removed, failed = _delete_biometric_files(user_id)
    return {
        "status": "success",
        "user_id": user_id,
        "deleted": True,
        "face_removed": removed,
        "face_removal_failed": failed,
        "message": (
            f"User {user_id} deleted."
            if not failed
            else f"User {user_id} deleted, but {', '.join(failed)} could not be removed."
        ),
    }


@router.get("/admin/audit_log")
async def list_audit_log(
    limit: int = 200, action: str | None = None, current: CurrentUser = Depends(admin_only)
):
    """Administrative history: manual edits, overrides, reviews, actor and IP."""
    limit = max(1, min(int(limit), 1000))
    # Concealment is part of the query, not a filter after it: an audit row that names the root
    # account as its actor *or* as its subject is exactly the row an administrator must not
    # read, and a post-filter would still return the length of the shortened list.
    #
    # ``COALESCE`` because ``NOT IN`` is NULL-tainted: a bare ``entity_id NOT IN (...)`` is
    # never true for a NULL subject, so it would silently drop every row that has no subject -
    # which is most of the table.
    by_actor, actor_params = developer.visibility_clause(current, column="actor_id")
    by_subject, subject_params = developer.visibility_clause(
        current, column="COALESCE(entity_id, '')"
    )
    hide_sql = by_actor + by_subject
    hide_params = actor_params + subject_params
    with db() as conn:
        if action:
            rows = conn.execute(
                f"SELECT * FROM audit_log WHERE action = ?{hide_sql} ORDER BY id DESC LIMIT ?",
                (action, *hide_params, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM audit_log WHERE 1 = 1{hide_sql} ORDER BY id DESC LIMIT ?",
                (*hide_params, limit),
            ).fetchall()
    return _json([dict(row) for row in rows])


# ---------------------------------------------------------------------------
# admin: sites
# ---------------------------------------------------------------------------
@router.get("/admin/sites")
async def list_sites(current: CurrentUser = Depends(admin_only)):
    """Every site, with the clock-in window that is actually in force there.

    Both shapes are returned on purpose. The three raw columns say what the site has been
    *configured* with (``null`` = inherit), which is what an edit form has to load. ``window``
    is the resolved result - the hours, the zone, and which of the two each came from - which
    is what an administrator needs to answer "why was this arrival flagged?" without having to
    know how the fallback works. Reporting only the configured values would leave a site with
    no overrides looking unconfigured at 04:00.
    """
    global_rules = get_shift_rules()
    with db() as conn:
        rows = conn.execute("SELECT * FROM construction_sites").fetchall()
    sites = []
    for row in rows:
        sites.append(
            {
                "site_name": row["site_name"],
                "lat": row["lat"],
                "lon": row["lon"],
                "radius": row["radius"],
                "clock_in_window_start": row["clock_in_window_start"],
                "clock_in_window_end": row["clock_in_window_end"],
                "site_timezone": row["site_timezone"],
                "window": shift_windows.effective_window(row, global_rules).as_dict(),
            }
        )
    return sites


@router.post("/admin/sites/add")
async def add_site(request: Request, req: SiteModel, current: CurrentUser = Depends(admin_only)):
    lat, lon = parse_location_input(req.location_input)
    validate_plausible_coordinates(lat, lon)
    _validate_site_radius(req.radius)
    window = _site_window_columns(req)
    with db(write=True) as conn:
        try:
            conn.execute(
                "INSERT INTO construction_sites "
                "(site_name, lat, lon, radius, clock_in_window_start, clock_in_window_end, site_timezone) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (req.site_name, lat, lon, req.radius, *window.values()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Site name already exists.")
        _audit(
            conn,
            action="site_create",
            actor=current,
            entity="construction_sites",
            entity_id=req.site_name,
            after={"lat": lat, "lon": lon, "radius": req.radius, **window},
            request=request,
        )
    return {"status": "success", "message": f"Site '{req.site_name}' successfully added at ({lat}, {lon})."}


@router.post("/admin/sites/edit")
async def edit_site(request: Request, req: SiteModel, current: CurrentUser = Depends(admin_only)):
    lat, lon = parse_location_input(req.location_input)
    validate_plausible_coordinates(lat, lon)
    _validate_site_radius(req.radius)
    # Only the window fields the caller *sent* are written, and that is not a nicety: the
    # console's site form predates this feature and posts four fields, so treating an absent
    # key as NULL would erase a site's shift every time somebody moved its pin on the map.
    # Clearing an override is therefore explicit - send the key with ``null`` - while omitting
    # it leaves whatever is there alone (``model_fields_set``).
    sent = [key for key in _SITE_WINDOW_FIELDS if key in req.model_fields_set]
    window = {key: getattr(req, key) for key in sent}
    with db(write=True) as conn:
        existing = conn.execute(
            "SELECT * FROM construction_sites WHERE site_name = ?", (req.site_name,)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Site not found.")
        assignments = ["lat = ?", "lon = ?", "radius = ?", *[f"{key} = ?" for key in sent]]
        conn.execute(
            f"UPDATE construction_sites SET {', '.join(assignments)} WHERE site_name = ?",
            (lat, lon, req.radius, *window.values(), req.site_name),
        )
        _audit(
            conn,
            action="site_edit",
            actor=current,
            entity="construction_sites",
            entity_id=req.site_name,
            before=dict(existing),
            after={"lat": lat, "lon": lon, "radius": req.radius, **window},
            request=request,
        )
    return {"status": "success", "message": f"Site '{req.site_name}' updated successfully."}


@router.post("/admin/sites/delete")
async def delete_site(
    request: Request,
    site_name: str = Form(...),
    current: CurrentUser = Depends(admin_only),
):
    """Delete a site.

    ``site_name`` is declared as ``Form(...)`` on purpose: the shipped version
    declared it as a query parameter while the front-end posts multipart form
    data, so the Delete button answered 422 for everybody.

    The name is validated like any other: it is an identifier for a row, and a delete that
    accepts markup is a delete that writes markup into the audit entry describing it.
    """
    try:
        site_name = textguard.identifier(
            site_name, field="Site name", max_length=textguard.MAX_SITE_NAME
        )
    except ValueError as exc:
        raise textguard.http_error(exc) from None
    with db(write=True) as conn:
        existing = conn.execute(
            "SELECT lat, lon, radius FROM construction_sites WHERE site_name = ?", (site_name,)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Site not found.")
        conn.execute("DELETE FROM construction_sites WHERE site_name = ?", (site_name,))
        _audit(
            conn,
            action="site_delete",
            actor=current,
            entity="construction_sites",
            entity_id=site_name,
            before=dict(existing),
            request=request,
        )
    return {"status": "success", "message": f"Site '{site_name}' deleted successfully."}


# ---------------------------------------------------------------------------
# admin: data retention
# ---------------------------------------------------------------------------
@router.get("/admin/retention")
async def retention_status(current: CurrentUser = Depends(admin_only)):
    """The retention policy in force, the last sweep, and what is still on disk that should not be.

    Three questions, in the order an auditor asks them: *what is the policy*, *is anything
    actually running it*, and *is the data gone*. The third is answered from the filesystem
    rather than from a query (``retention.residue``), because the whole point is to find the
    faces the database can no longer name - which is precisely what a query cannot see.

    ``last_run`` is the row the sweeper writes, so it survives a restart; that is what
    distinguishes "scheduled and working" from "scheduled and quietly dead".
    """
    return {
        "policy": retention.policy().as_dict(),
        "dry_run": bool(settings.retention_dry_run),
        "scheduler": {
            "enabled": bool(settings.retention_enabled),
            "running": retention.watcher_running(),
            "interval_seconds": int(settings.retention_interval_seconds),
            "initial_delay_seconds": int(settings.retention_initial_delay_seconds),
        },
        "last_run": retention.last_run(),
        "residue": retention.residue(),
        "attendance_logs": (
            "never deleted automatically: the hours a person worked are pay records. The "
            "sweep reports their age and deletes nothing."
        ),
    }


@router.post("/admin/retention/dry-run")
async def retention_dry_run(current: CurrentUser = Depends(admin_only)):
    """Report exactly what the next sweep would erase, and write nothing at all.

    Nothing, including an audit row: the dry run opens the database **read-only**, so an
    accidental write fails with ``attempt to write a readonly database`` instead of quietly
    doing half a sweep. That is a stronger promise than passing a flag around, and it is the
    one an operator needs before the first ``--apply``.

    Run in a worker thread because it walks directories and hashes what it finds - blocking
    work, and the event loop has punches to serve.
    """
    report = await run_in_threadpool(retention.sweep, dry_run=True, actor=f"admin:{current.id}")
    return report


# ---------------------------------------------------------------------------
# admin: live sessions, reviews, logs
# ---------------------------------------------------------------------------
@router.get("/admin/active_sessions")
async def list_active_sessions(current: CurrentUser = Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT a.worker_id, u.name, a.site_name, a.clock_in_time, u.role, a.late_flag
            FROM active_sessions a
            JOIN users u ON a.worker_id = u.id
            ORDER BY a.clock_in_time DESC
            """
        ).fetchall()
    return [
        {
            "worker_id": row["worker_id"],
            "name": row["name"],
            "site_name": row["site_name"],
            "clock_in_time": row["clock_in_time"],
            "role": row["role"],
            "late_flag": row["late_flag"],
        }
        for row in rows
    ]


@router.get("/admin/workers_live/{site_name}")
async def get_workers_live(site_name: str, current: CurrentUser = Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT a.worker_id, u.name, a.clock_in_time
            FROM active_sessions a
            JOIN users u ON a.worker_id = u.id
            WHERE a.site_name = ?
            ORDER BY a.clock_in_time DESC
            """,
            (site_name,),
        ).fetchall()
    return [
        {"worker_id": row["worker_id"], "name": row["name"], "clock_in_time": row["clock_in_time"]}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# admin: the live crossing, as an approval rather than an alert
#
# A shift that is *still open* and past the overtime line is a question, not a notice: it is
# answered by a person, the answer changes what the shift is paid for, and until somebody
# answers it the shift keeps running. That is the shape of the Approvals queue, not of
# ``admin_notifications`` - which is why the crossing was moved here, and why reading it is no
# longer a way to make it go away. The item disappears when the shift ends and not before.
# ---------------------------------------------------------------------------
class CrossingDecisionRequest(BaseModel):
    """The answer to an open-shift crossing.

    ``authorised_hours`` is the ceiling being authorised: the paid hours this shift may run to.
    Omitted means "the hours already worked" - approving what there is evidence for - and stated
    means the rest of the shift is covered deliberately, with a figure on it. Hours past the
    ceiling come back as a second question rather than being paid quietly.
    """

    authorised_hours: float | None = Field(
        None,
        ge=0,
        description="Paid hours this shift may run to; defaults to the hours worked so far.",
    )
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # The same prose profile every note in this application goes through: apostrophes and
        # ampersands survive, markup does not. This text is read by an auditor and may reach the
        # worker, and neither reader escapes it.
        return textguard.prose(value, field="Note", max_length=textguard.MAX_NOTE, allow_empty=True)


@router.get("/admin/overtime/crossings/count")
async def count_overtime_crossings(current: CurrentUser = Depends(admin_only)):
    """How many crossings are waiting on a person, without the queue's payload.

    Exists for the nav badge: the console paints the Approvals tab's count on screens where
    the tab is *not* open, so the full ``open_crossings`` shape - a decision payload, break
    arithmetic and an overtime sentence per shift - would be fetched and dropped on every
    poll. One small read, ``needs_answer`` being the same guard the queue's cards use to
    decide whether they are a question: a decided-but-still-covered shift is information,
    not something an operator must act on, and it must not light a badge.
    """
    return _json({"count": sum(1 for item in overtime.open_crossings() if item.get("needs_answer"))})


@router.get("/admin/overtime/crossings")
async def list_overtime_crossings(current: CurrentUser = Depends(admin_only)):
    """Open shifts past the overtime line: the decisions the Approvals queue is holding.

    Read from the shifts themselves (``overtime.open_crossings``) rather than from stored
    notification rows, so an item cannot outlive the shift it describes, and a crossing nobody
    answers stays on the list instead of looking like something somebody already dealt with.
    """
    return _json(overtime.open_crossings())


async def _answer_crossing(
    request: Request, worker_id: str, req: CrossingDecisionRequest, current: CurrentUser, *, accept: bool
):
    """Both answers in one place, because both write the same decision row.

    The audit event is written only when the answer is new: a second tap on a crossing somebody
    already answered returns the standing decision without pretending a second decision was made
    (``overtime.decide_crossing``, same rule as the forced-start acknowledgement). An answer that
    supersedes one the shift has outgrown *is* new, and its event says which decision it replaced -
    the row carries the chain, but "why is this 12 h now" is a question about the trail.
    """
    try:
        with db(write=True) as conn:
            result = overtime.decide_crossing(
                conn,
                worker_id=worker_id,
                accept=accept,
                authorised_hours=req.authorised_hours,
                note=req.note,
                actor_id=current.id,
            )
            if not result["already_decided"]:
                _audit(
                    conn,
                    action="overtime_authorise" if accept else "overtime_decline",
                    actor=current,
                    entity="overtime_authorisations",
                    entity_id=result["worker_id"],
                    before={
                        "open_shift_clock_in": result["clock_in_time"],
                        # ``None`` on a first decision. Present, rather than omitted, because an
                        # audit reader has to be able to tell "extended a ceiling somebody had
                        # authorised" from "authorised for the first time" in this row alone.
                        "superseded": result.get("superseded"),
                    },
                    after={
                        "decision": result["decision"],
                        "authorised_hours": result["authorised_hours"],
                        "recorded_hours_at_decision": result["recorded_hours_at_decision"],
                        "note": req.note,
                    },
                    request=request,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # Committed first, because ``push`` selects notices that are not yet delivered and cannot see
    # the row this request just wrote. A repeat of an answer already on the record wrote no notice,
    # and the helper reads that flag itself - so this is the same call on both paths.
    overtime.deliver_worker_notices(result)
    return _json(result)


@router.post("/admin/overtime/crossings/{worker_id}/accept")
async def accept_overtime_crossing(
    request: Request,
    worker_id: str,
    req: CrossingDecisionRequest,
    current: CurrentUser = Depends(admin_only),
):
    """Authorise this shift up to a ceiling; its clock-out settles there rather than at the day."""
    return await _answer_crossing(request, worker_id, req, current, accept=True)


@router.post("/admin/overtime/crossings/{worker_id}/decline")
async def decline_overtime_crossing(
    request: Request,
    worker_id: str,
    req: CrossingDecisionRequest,
    current: CurrentUser = Depends(admin_only),
):
    """Refuse the extra time: nothing past the regular paid day is authorised for this shift."""
    return await _answer_crossing(request, worker_id, req, current, accept=False)


@router.get("/admin/pending_reviews")
async def list_pending_reviews(current: CurrentUser = Depends(admin_only)):
    """The review queue: every shift a human has to decide about, with the evidence beside it.

    The evidence is the point of this route. Each row now carries the three things a decision
    actually rests on - the match score (``score``, a face *distance*: lower is a better match,
    and the band lines that grade it are on ``GET /admin/shift_rules``), the reason the shift
    was flagged (``flag_reason``, the liveness verdict, the window miss or the overtime
    sentence the shift earned), and whether a frame is waiting (``frame_url``, served by
    :func:`admin_pending_review_frame`). The frame itself is deliberately *not* inlined here:
    a queue of ten reviews would carry ten pictures whether or not anybody opened them, on the
    connection this console is usually opened over.

    An offline punch's score can still be the 0.0 "not scored yet" sentinel: the queued selfie
    is scored when it reaches the server (``POST /attendance/sync/photo``), and until then the
    row says so rather than showing a number that looks like a match.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.worker_id, u.name, u.role, l.site_name, l.action, l.timestamp,
                   l.hours, l.score, l.status, l.status_code, l.flag_reason, l.overtime_hours,
                   l.source, l.liveness_class, l.liveness_score, l.punch_frame
            FROM attendance_logs l
            JOIN users u ON l.worker_id = u.id
            WHERE l.status = ? OR l.status_code = 'pending_overtime'
            ORDER BY l.timestamp DESC
            """,
            (STATUS_PENDING_REVIEW,),
        ).fetchall()
    return _json([_pending_review_row(row) for row in rows])


def _pending_review_row(row) -> dict:
    """One review row for the wire, with the URL for its frame and not the file name.

    ``punch_frame`` is server-side plumbing the client never sees - the same treatment the
    quick-link use list gives ``photo_path`` - because a filename is not an identifier this
    app exposes, and the browser has no business depending on one.
    """
    item = dict(row)
    frame = item.pop("punch_frame", None)
    item["frame_url"] = f"/api/v1/admin/pending_review_frame/{item['id']}" if frame else None
    # The verdict, not a distance alone: the number means different things under different
    # pipelines, and the browser recomputing it would mean shipping the bands to the client -
    # thresholds stated once in ``face_detector``, restated in JavaScript. ``1.0`` is the
    # "no comparison was made" sentinel (admin override, quick link); ``0.0`` is the offline
    # "not scored yet" sentinel. A comparison *made and refused* still classifies - that is
    # exactly the verdict the reviewer needs to see.
    item["match_verdict"] = None
    score = item.get("score")
    if score is not None and float(score) not in (0.0, 1.0):
        try:
            item["match_verdict"] = face_detector.band_for(biometrics.current_pipeline()).classify(float(score))
        except face_detector.UnknownPipelineError:
            item["match_verdict"] = None
    return item


@router.get("/admin/refused_punches")
async def list_refused_punches(
    current: CurrentUser = Depends(admin_only),
    days: int = 1,
):
    """The refused punches: what the band refused, with the score and the frame beside it.

    The triage surface the deployment needed and never had: a worker who keeps being told
    "we could not confirm that is you" used to leave no record at all, so an operator whose
    band was derived from the wrong corpus could see neither the scores nor the faces. This
    lists them - newest first, ``days`` back (default today, capped at 7) - each row carrying
    the distance, the reason the punch was refused, and whether a frame is waiting
    (``frame_url``, served by :func:`admin_refused_punch_frame`).

    The frame is a link rather than inline data, for the same reason the review queue does
    it this way: a day of refusals would otherwise carry every picture whether or not
    anybody looked. Rows are read-only evidence - there is no approve path, because a
    refused punch was never attendance - and retention sweeps them on the frame window.
    """
    window = max(0, min(int(days), 7))
    with db() as conn:
        if window == 0:
            rows = conn.execute(
                """
                SELECT r.id, r.worker_id, u.name, r.site_name, r.action, r.error_code,
                       r.score, r.pipeline, r.source, r.punch_frame, r.created_at
                FROM refused_punches r
                LEFT JOIN users u ON r.worker_id = u.id
                ORDER BY r.id DESC
                """
            ).fetchall()
        else:
            cutoff = (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d 00:00:00")
            rows = conn.execute(
                """
                SELECT r.id, r.worker_id, u.name, r.site_name, r.action, r.error_code,
                       r.score, r.pipeline, r.source, r.punch_frame, r.created_at
                FROM refused_punches r
                LEFT JOIN users u ON r.worker_id = u.id
                WHERE r.created_at >= ?
                ORDER BY r.id DESC
                """,
                (cutoff,),
            ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        frame = item.pop("punch_frame", None)
        item["frame_url"] = f"/api/v1/admin/refused_punch_frame/{item['id']}" if frame else None
        items.append(item)
    return _json(items)


@router.get("/admin/refused_punch_frame/{refusal_id}")
async def admin_refused_punch_frame(refusal_id: int, current: CurrentUser = Depends(admin_only)):
    """The downscaled frame one refused punch was measured from. Administrator-only.

    The same contract as the review frame: ``resolve_stored`` proves the path is inside the
    frame directory before anything is opened, and a row whose frame was never stored or has
    been swept answers 404 rather than pretending to have a picture.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT punch_frame FROM refused_punches WHERE id = ?", (refusal_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This refusal has no record.")
    path = punch_frames.resolve_stored(row["punch_frame"])
    if path is None:
        raise HTTPException(status_code=404, detail="No frame was stored for this refusal.")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/admin/refused_punches/{refusal_id}/clear")
async def clear_refused_punch(refusal_id: int, current: CurrentUser = Depends(admin_only)):
    """Mark one refusal as seen, so a triaged list stays a list of what still needs looking at.

    Deliberately a *clear* and not a delete: the evidence stays on disk until retention
    sweeps it, and the audit trail says who decided it needed nothing further. A refusal is
    not attendance and cannot become one - there is no approve path here, only a way for the
    list to shrink once somebody has looked.
    """
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT id, worker_id FROM refused_punches WHERE id = ?", (refusal_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="This refusal has no record.")
        conn.execute("DELETE FROM refused_punches WHERE id = ?", (refusal_id,))
        _audit(
            conn,
            action="refused_punch_clear",
            actor=current,
            entity="refused_punches",
            entity_id=refusal_id,
            after={"worker_id": row["worker_id"]},
        )
    return {"status": "success", "message": "Refusal cleared."}


@router.get("/admin/pending_review_frame/{log_id}")
async def admin_pending_review_frame(log_id: int, current: CurrentUser = Depends(admin_only)):
    """The downscaled frame one pending review was measured from. Administrator-only.

    The same shape as ``/admin/quick_link_photo``: the stored name is resolved against the
    frame directory and checked to be *inside* it before it is served, so a row whose path was
    edited cannot turn this route into a file-read primitive. A row with no frame - written
    before frames existed, or whose frame retention wiped, or whose disk was full at punch
    time - answers 404 rather than pretending to have a picture; the review is decided on its
    numbers in that case, exactly as it always was.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT punch_frame FROM attendance_logs WHERE id = ?", (log_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This punch has no record.")
    path = punch_frames.resolve_stored(row["punch_frame"])
    if path is None:
        raise HTTPException(status_code=404, detail="No frame was stored for this punch.")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/admin/approve_review")
async def approve_review(
    request: Request, req: ReviewApprovalRequest, current: CurrentUser = Depends(admin_only)
):
    """Clear a review flag, optionally adjusting the payable hours."""
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT worker_id, hours, status, status_code FROM attendance_logs WHERE id = ?",
            (req.log_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attendance log record not found.")
        if row["status"] != STATUS_PENDING_REVIEW and row["status_code"] != "pending_overtime":
            raise HTTPException(status_code=400, detail="This log is not marked as pending review.")

        approved = req.approved_hours if req.approved_hours is not None else row["hours"]
        if approved < 0:
            raise HTTPException(status_code=400, detail="Approved hours cannot be negative.")
        if approved > row["hours"]:
            raise HTTPException(
                status_code=400,
                detail=f"Approved hours ({approved}) cannot exceed the recorded hours ({row['hours']}).",
            )

        # Overtime is what the administrator *authorised* past the regular day, not what the
        # clock happened to record. Approving 8.25 h of a 9.5 h shift settles the shift at 15
        # minutes of overtime; a row carrying 8.25 approved hours beside 1.5 h of overtime
        # contradicts itself, and the timesheet would repeat the contradiction.
        regular_hours = float(get_shift_rules()["regular_hours"])
        conn.execute(
            "UPDATE attendance_logs SET status = ?, status_code = 'approved', approved_hours = ?, "
            "overtime_hours = ?, reviewed_by = ?, reviewed_at = ?, flag_reason = ? WHERE id = ?",
            (
                "Approved by Admin",
                approved,
                round(max(0.0, approved - regular_hours), 4),
                current.id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                req.note,
                req.log_id,
            ),
        )
        conn.execute(
            "UPDATE admin_notifications SET read_at = ?, read_by = ? WHERE log_id = ? AND read_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), current.id, req.log_id),
        )
        _audit(
            conn,
            action="review_approve",
            actor=current,
            entity="attendance_logs",
            entity_id=req.log_id,
            before={"hours": row["hours"], "status": row["status"]},
            after={"approved_hours": approved, "note": req.note},
            request=request,
        )
    return {"status": "success", "message": "Attendance record approved by Admin."}


@router.post("/admin/reject_review")
async def reject_review(
    request: Request, req: ReviewRejectionRequest, current: CurrentUser = Depends(admin_only)
):
    """Refuse a shift that was routed to review, and record why.

    What "reject" means depends on what is being reviewed, and the two are opposites, so
    they are two codes and not one:

    * **A shift past the overtime threshold** (``pending_overtime``) still happened. The
      worker keeps the standard paid day, the hours past it are refused, and ``overtime_hours``
      is recorded as zero - the refusal is of the *extra* hours, not of the work. The row
      becomes ``overtime_rejected``, which is payable at the standard day, so the day is paid
      and the timesheet cannot credit a minute the administrator refused.
    * **An unconfirmed punch** (``pending_review`` - the location was outside every site's
      radius, or the face did not match) is a shift nobody could confirm was work. Rejecting
      it records it as not worked: nothing is payable, and ``rejected`` counts for zero hours
      wherever hours are summed.

    Either way the row leaves the review queue, the notification it came from is marked
    read, and the reason is written to the row and to the append-only audit trail.

    This endpoint exists because the console's Reject button used to POST to
    ``/admin/approve_review``: it took an ``action`` argument and never read it, so pressing
    Reject *approved* the shift at its full recorded hours - a worker was paid overtime their
    manager had just refused.
    """
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT worker_id, hours, status, status_code, overtime_hours, approved_hours, flag_reason "
            "FROM attendance_logs WHERE id = ?",
            (req.log_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attendance log record not found.")
        code = str(row["status_code"] or "")
        if row["status"] != STATUS_PENDING_REVIEW and code != "pending_overtime":
            raise HTTPException(status_code=400, detail="This log is not marked as pending review.")

        recorded = float(row["hours"] or 0.0)
        if code == "pending_overtime":
            # The standard day is owed, so it is approved at the policy figure rather than
            # at the clock - capped by the recorded hours for a shift that never reached a
            # full day, which cannot happen through the threshold but is free to guard.
            regular_hours = float(get_shift_rules()["regular_hours"])
            approved = round(min(regular_hours, recorded), 4)
            overtime = 0.0
            status_label = STATUS_OVERTIME_REJECTED
            new_code = migrations.STATUS_CODE_OVERTIME_REJECTED
            message = (
                f"Overtime refused. The shift is recorded at {approved:g} h and no overtime "
                f"is credited."
            )
        else:
            approved = 0.0
            overtime = 0.0
            status_label = STATUS_TYPE_REJECTED
            new_code = migrations.STATUS_CODE_REJECTED
            message = "Attendance record rejected: the shift is recorded as not worked."

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE attendance_logs SET status = ?, status_code = ?, approved_hours = ?, "
            "overtime_hours = ?, reviewed_by = ?, reviewed_at = ?, flag_reason = ? WHERE id = ?",
            (status_label, new_code, approved, overtime, current.id, now_str, req.note, req.log_id),
        )
        conn.execute(
            "UPDATE admin_notifications SET read_at = ?, read_by = ? WHERE log_id = ? AND read_at IS NULL",
            (now_str, current.id, req.log_id),
        )
        _audit(
            conn,
            action="review_reject",
            actor=current,
            entity="attendance_logs",
            entity_id=req.log_id,
            # The before-image carries the arrival/liveness flag this write replaces in
            # ``flag_reason``, so the reason for the refusal is added to the record rather
            # than swapped for the reason the shift was flagged in the first place.
            before={
                "hours": recorded,
                "status": row["status"],
                "status_code": code,
                "approved_hours": row["approved_hours"],
                "overtime_hours": row["overtime_hours"],
                "flag_reason": row["flag_reason"],
            },
            after={
                "status_code": new_code,
                "approved_hours": approved,
                "overtime_hours": overtime,
                "reason": req.note,
            },
            request=request,
        )
    return {
        "status": "success",
        "message": message,
        "status_code": new_code,
        "approved_hours": approved,
        "overtime_hours": overtime,
    }


@router.post("/admin/shifts/{log_id}/hours")
async def edit_worked_shift_hours(
    request: Request, log_id: int, req: ShiftHoursEditRequest, current: CurrentUser = Depends(admin_only)
):
    """Correct the hours of a shift that has already been worked and filed.

    The clock records what it saw, but the clock is not always right about the *day*: the
    punch pair straddled a mid-shift errand, the offline replay arrived twice, or the
    figure an approval settled carries a typo. Until now the only paths that touched a
    filed row's hours were the review decisions - so a settled shift carrying a wrong
    number had no correction at all, and "edit the timesheet" meant editing the database
    by hand. This endpoint is the ordinary, audited way to do that.

    What it deliberately does NOT do:

    * **reopen a decision.** A row awaiting review is answered by the review, not edited
      around it - editing a pending row would let an edit stand in for the decision the
      queue exists to record, so pending rows are refused here.
    * **touch an open shift.** A shift still running is closed by the clock-out (or a
      force-out); there is nothing filed yet to correct.

    The row keeps its status code - the answer the review gave is history - and only the
    figures move: ``hours`` and ``approved_hours`` take the corrected figure together (a
    record that disagreed with its own approval would repeat in every report), and
    ``overtime_hours`` is re-derived from the same resolver the approval gate uses, so a
    corrected 9.5 h day credits 1.5 h of overtime under a regular day of 8 h - computed,
    never typed.

    The edit is appended to the audit trail as ``shift_hours_edit`` with the before and
    after figures, because a timesheet that changed without a trail is not a timesheet -
    it is a rumour.
    """
    hours = round(float(req.hours), 4)
    if hours < 0 or hours > overtime.MAX_AUTHORISED_HOURS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"hours must be between 0 and {overtime.MAX_AUTHORISED_HOURS:g} for one shift"
            ),
        )

    with db(write=True) as conn:
        row = conn.execute(
            "SELECT worker_id, hours, status, status_code, approved_hours, overtime_hours, action "
            "FROM attendance_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attendance log record not found.")
        if row["action"] != ACTION_CLOCK_OUT:
            raise HTTPException(status_code=400, detail="Only a clock-out row carries a shift's hours.")
        code = str(row["status_code"] or "")
        if code in ("pending_review", "pending_overtime", "unverified_offline", "auto_closed"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "This shift is still waiting on a decision (a review answer, or the "
                    "auto-close) - settle it there rather than editing around it."
                ),
            )

        regular_hours = float(get_shift_rules()["regular_hours"])
        overtime_hours = round(max(0.0, hours - regular_hours), 4)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE attendance_logs SET hours = ?, approved_hours = ?, overtime_hours = ?, "
            "reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (hours, hours, overtime_hours, current.id, now_str, log_id),
        )
        _audit(
            conn,
            action="shift_hours_edit",
            actor=current,
            entity="attendance_logs",
            entity_id=log_id,
            before={
                "hours": row["hours"],
                "approved_hours": row["approved_hours"],
                "overtime_hours": row["overtime_hours"],
                "status_code": code,
            },
            after={
                "hours": hours,
                "approved_hours": hours,
                "overtime_hours": overtime_hours,
                "note": req.note,
            },
            request=request,
        )
    return {
        "status": "success",
        "message": (
            f"Shift corrected to {hours:g}h "
            f"(overtime re-derived at {overtime_hours:g}h)."
        ),
        "hours": hours,
        "overtime_hours": overtime_hours,
    }


@router.post("/admin/force_clock_in")
async def force_clock_in(
    request: Request, req: ForceClockRequest, current: CurrentUser = Depends(admin_only)
):
    with db(write=True) as conn:
        worker = conn.execute("SELECT name, role FROM users WHERE id = ?", (req.worker_id,)).fetchone()
        if worker is None:
            raise HTTPException(status_code=404, detail="Worker ID not found.")

        if conn.execute("SELECT worker_id FROM active_sessions WHERE worker_id = ?", (req.worker_id,)).fetchone():
            raise HTTPException(status_code=400, detail="Worker is already clocked in.")

        site = conn.execute(
            "SELECT site_name FROM construction_sites WHERE site_name = ?", (req.site_name,)
        ).fetchone()
        if site is None:
            raise HTTPException(status_code=404, detail="Construction site not found.")

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source) "
            "VALUES (?, ?, ?, 'admin_override')",
            (req.worker_id, req.site_name, now_str),
        )
        log_id = _insert_log(
            conn,
            worker_id=req.worker_id,
            site_name=req.site_name,
            action=ACTION_CLOCK_IN,
            timestamp=now_str,
            hours=0.0,
            score=1.0,
            status=STATUS_FORCED_IN,
            status_code="approved",
            source="admin_override",
        )
        _audit(
            conn,
            action="force_clock_in",
            actor=current,
            entity="active_sessions",
            entity_id=req.worker_id,
            after={"site": req.site_name, "log_id": log_id},
            request=request,
        )
    return {
        "status": "success",
        "message": f"Worker {req.worker_id} successfully Force Clocked In to '{req.site_name}'.",
    }


@router.post("/admin/force_clock_out")
async def force_clock_out(
    request: Request, req: ForceClockRequest, current: CurrentUser = Depends(admin_only)
):
    now = datetime.now()
    with db(write=True) as conn:
        session = conn.execute(
            "SELECT site_name, clock_in_time FROM active_sessions WHERE worker_id = ?", (req.worker_id,)
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="Worker is not currently clocked in.")

        clock_in_time = _parse_ts(session["clock_in_time"]) or now
        seconds_on_site = shift_hours.elapsed_seconds(clock_in_time, now)
        # The same policy the worker's own clock-out applies, so the same shift pays the
        # same whichever way it is closed. Deducting the break only on one of those two
        # paths would mean the answer to "what is this day worth?" depended on whether
        # the worker remembered to tap the button, which is not a payroll rule.
        rules = get_shift_rules()
        record = shift_hours.recorded_shift(seconds_on_site / 3600.0, rules)
        hours_worked = record["paid_hours"]
        break_taken = record["break_hours"]
        # An administrator-named figure overrides the clock. The forgotten clock-out is the
        # case: the session has run all night, and the paid hours the clock would record are
        # nobody's estimate of the shift. The figure given is what the row records - subject
        # to the same bounds any hours field answers to - and the audit event carries both
        # numbers, so "the clock said 10.9 h and the administrator authorised 9.5 h" is a
        # sentence the trail can produce months later.
        authorised_override: float | None = None
        if req.hours is not None:
            authorised_override = round(float(req.hours), 4)
            if authorised_override < 0 or authorised_override > overtime.MAX_AUTHORISED_HOURS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"hours must be between 0 and {overtime.MAX_AUTHORISED_HOURS:g} "
                        "for one shift"
                    ),
                )
            hours_worked = authorised_override
        # ... and the overtime figure comes from the same resolver as the approval gate.
        # This path records the row as *approved* rather than routing it for review, which
        # is the point of an override: the administrator closing the shift is the person
        # the review would be handed to. What it must not do is settle the shift as if
        # nobody had answered: ``apply_authorisation`` reads the standing decision for this
        # shift and reports what is *still* held past it, so closing this way cannot
        # contradict an answer given in the Approvals queue - which would be an approval
        # silently reversing itself hours later. With no answer it returns the assessment
        # unchanged, and the arithmetic is exactly what this line always did.
        assessment = overtime.apply_authorisation(
            conn,
            req.worker_id,
            session["clock_in_time"],
            shift_hours.overtime_assessment(seconds_on_site, rules),
            rules,
        )
        if authorised_override is not None:
            # The named figure is the decision: hours past it are not silently paid but held
            # exactly as an answered crossing would hold them, so the override and the
            # Approvals queue produce the same row for the same answer.
            assessment["overtime_hours"] = round(
                max(0.0, authorised_override - float(assessment["regular_hours"])), 4
            )
            assessment["authorised_hours"] = authorised_override
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (req.worker_id,))
        log_id = _insert_log(
            conn,
            worker_id=req.worker_id,
            site_name=session["site_name"],
            action=ACTION_CLOCK_OUT,
            timestamp=now_str,
            hours=hours_worked,
            score=1.0,
            status=STATUS_FORCED_OUT,
            status_code="approved",
            source="admin_override",
            approved_hours=hours_worked,
            overtime_hours=assessment["overtime_hours"] or None,
            break_hours=break_taken or None,
        )
        # ... and the answer that settled it is spent by this row, so it cannot be read as
        # authorisation for the worker's next shift.
        overtime.consume_authorisation(conn, req.worker_id, session["clock_in_time"], log_id)
        # The worker is told their shift crossed the line, even though this override records
        # the hours as authorised: the notice states the event and the rule (time past the
        # paid day needs approval before it is paid), not the outcome, which is what makes one
        # sentence honest on all four paths. Their card and their history both show the day.
        crossing = overtime.announce_crossing(
            conn,
            worker_id=req.worker_id,
            site_name=session["site_name"],
            clock_in_time=session["clock_in_time"],
            values=rules,
            moment=now,
        )
        _audit(
            conn,
            action="force_clock_out",
            actor=current,
            entity="active_sessions",
            entity_id=req.worker_id,                after={
                    "hours": hours_worked,
                    "clock_recorded_hours": record["paid_hours"],
                    "break_hours": break_taken,
                    "elapsed_hours": assessment["elapsed_hours"],
                    "log_id": log_id,
                },
            request=request,
        )
    overtime.deliver_worker_notices(crossing)
    return {
        "status": "success",
        "message": (
            f"Worker {req.worker_id} successfully Force Clocked Out. "
            + (
                f"Recorded at the authorised {hours_worked:.2f}h "
                f"(the clock would have recorded {record['paid_hours']:.2f}h)."
                if authorised_override is not None
                else record["description"]
            )
        ),
        "hours": hours_worked,
        "break_hours": break_taken,
    }


def _form_flag(value: str | None) -> bool:
    """A checkbox-style form field: anything that reads as a yes is one.

    Mirrors ``shift_hours.auto_close_enabled`` on purpose - an unreadable value must not
    quietly turn a flag *on*, and the phone sends a plain ``1``.
    """
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _early_checkout_refusal(record: Mapping[str, Any]) -> dict[str, Any]:
    """The early clock-out question, with the numbers the phone needs to spell it out.

    ``message`` is English because the API has one language; the app does not use it -
    it builds the same sentence from these figures in the reader's own language, so the
    warning a worker sees on a site in Cairo is not written in English.
    """
    return {
        "error_code": "confirm_early_checkout",
        "message": (
            f"You have worked {record['paid_hours']:.2f}h of the "
            f"{record['regular_hours']:.2f}h paid day. Clocking out now records "
            f"{record['paid_hours']:.2f}h, not the full day. Repeat the punch with "
            "confirm_early_checkout=1 to record it, or stay clocked in."
        ),
        "paid_hours": record["paid_hours"],
        "regular_hours": record["regular_hours"],
        "elapsed_hours": record["elapsed_hours"],
        "break_hours": record["break_hours"],
        "short_hours": record["short_hours"],
    }


def _no_open_shift_message(conn: sqlite3.Connection, worker_id: str) -> str:
    """Why there is nothing to clock out of - and, usually, exactly when it ended.

    The commonest reason a clock-out finds no open shift is no longer a mistake: the
    shift was closed by the system when the paid hours reached the limit. Answering
    "Cannot clock out without clocking in first" to somebody whose shift just ended
    would read as data loss, so the closing time and the hours are said out loud.
    """
    try:
        row = conn.execute(
            "SELECT timestamp, hours, break_hours FROM attendance_logs "
            "WHERE worker_id = ? AND action = ? AND status_code = ? "
            "ORDER BY id DESC LIMIT 1",
            (worker_id, ACTION_CLOCK_OUT, STATUS_CODE_AUTO_CLOSED),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return "Cannot clock out without clocking in first."
    paid = float(row["hours"] or 0.0)
    taken = float(row["break_hours"] or 0.0)
    break_note = f" (including a {taken * 60:.0f}-minute unpaid break)" if taken > 0 else ""
    return (
        f"Your shift was closed automatically at {row['timestamp']}: {paid:.2f}h paid{break_note}. "
        "Nothing more is recorded for it - ask your administrator if you kept working."
    )


@router.get("/admin/logs")
async def get_logs(limit: int = 500, worker_id: str | None = None, current: CurrentUser = Depends(admin_only)):
    limit = max(1, min(int(limit), 5000))
    with db() as conn:
        if worker_id:
            rows = conn.execute(
                "SELECT l.id, l.worker_id, u.name, l.site_name, l.action, l.timestamp, l.hours, l.score, l.status, l.status_code, "
                "l.lat, l.lon, l.approved_hours, l.overtime_hours, l.flag_reason "
                "FROM attendance_logs l JOIN users u ON l.worker_id = u.id "
                "WHERE l.worker_id = ? ORDER BY l.timestamp DESC LIMIT ?",
                (worker_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT l.id, l.worker_id, u.name, l.site_name, l.action, l.timestamp, l.hours, l.score, l.status, l.status_code, "
                "l.lat, l.lon, l.approved_hours, l.overtime_hours, l.flag_reason "
                "FROM attendance_logs l JOIN users u ON l.worker_id = u.id "
                "ORDER BY l.timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return _json([
        {
            "id": row["id"],
            "worker_id": row["worker_id"],
            "name": row["name"],
            "site": row["site_name"],
            "action": row["action"],
            "timestamp": row["timestamp"],
            "hours": row["hours"],
            "score": row["score"],
            "status": row["status"],
            "status_code": row["status_code"],
            "lat": row["lat"],
            "lon": row["lon"],
            "approved_hours": row["approved_hours"],
            "overtime_hours": row["overtime_hours"],
            "flag_reason": row["flag_reason"],
        }
        for row in rows
    ])


# ---------------------------------------------------------------------------
# admin: notifications (replaces the external messaging integration)
# ---------------------------------------------------------------------------
@router.get("/admin/notifications")
async def list_notifications(
    unread_only: int = 0, limit: int = 100, current: CurrentUser = Depends(admin_only)
):
    limit = max(1, min(int(limit), 500))
    with db() as conn:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM admin_notifications WHERE read_at IS NULL ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM admin_notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        unread = notifications.unread_count(conn)
        unacknowledged = notifications.unacknowledged_count(conn)
    return _json(
        {
            "unread": unread,
            "unacknowledged": unacknowledged,
            "notifications": [dict(row) for row in rows],
        }
    )


class NotificationAcknowledgement(BaseModel):
    """The reason an operator accepted an alert.

    ``note`` is **required**, and that is the whole difference between this endpoint and
    ``/read``. An alert that records a decision - a forced start past a failing self-test, a
    channel that has stopped delivering - is not answered by having been looked at: it is
    answered by somebody saying, in their own words, why it is acceptable. A bare click leaves
    the next person to read the same row with the same question, and leaves an investigation
    with no account of who decided what.

    Free text, so it goes through the prose profile like every note in this application:
    apostrophes and ampersands survive, markup and script URLs do not.
    """

    note: str

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str) -> str:
        return textguard.prose(value, field="Acknowledgement reason", max_length=textguard.MAX_NOTE)


@router.post("/admin/notifications/{notification_id}/acknowledge")
async def acknowledge_notification(
    notification_id: int,
    payload: NotificationAcknowledgement,
    request: Request,
    current: CurrentUser = Depends(admin_only),
):
    """Accept an alert with a reason, and put both on the record.

    Three writes, in one transaction, each doing a different job: the row gains the
    acknowledgement (``notifications.acknowledge``), the append-only ``audit_log`` gains the
    event with the actor, the note and the request's own provenance, and the alert is marked
    read - an alert somebody has accepted has by definition been seen, and leaving it in the
    unread count would badge a decision that has already been made.

    Acknowledging twice is refused rather than overwritten. The first acceptance is the one
    that was actually made, and rewriting it would leave the audit trail describing a decision
    nobody took; the second caller is told who accepted it and when, which is the answer they
    needed anyway.
    """
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT * FROM admin_notifications WHERE id = ?", (int(notification_id),)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Notification not found.")
        if row["acknowledged_at"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Notification {notification_id} was already acknowledged by "
                    f"{row['acknowledged_by']} on {row['acknowledged_at']}. The first "
                    "acknowledgement is the one on the record; a second would rewrite whose "
                    "decision it was."
                ),
            )
        wrote = notifications.acknowledge(
            conn, int(notification_id), by=current.id, note=payload.note
        )
        if not wrote:
            # Reachable only if the row was acknowledged between the read above and this
            # statement - which the write lock makes impossible here, so this is the second
            # layer of the same rule rather than a second rule.
            raise HTTPException(
                status_code=409,
                detail=f"Notification {notification_id} was acknowledged concurrently.",
            )
        _audit(
            conn,
            action="notification_acknowledge",
            actor=current,
            entity="admin_notifications",
            entity_id=notification_id,
            before={"acknowledged": False},
            after={
                "note": payload.note,
                "kind": row["kind"],
                "severity": row["severity"],
                "title": row["title"],
                "alerted_at": row["created_at"],
            },
            request=request,
        )
        acknowledged = conn.execute(
            "SELECT * FROM admin_notifications WHERE id = ?", (int(notification_id),)
        ).fetchone()
    return _json(
        {
            "status": "success",
            "message": f"Notification {notification_id} acknowledged.",
            "notification": dict(acknowledged),
        }
    )


@router.post("/admin/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: int, current: CurrentUser = Depends(admin_only)):
    with db(write=True) as conn:
        exists = conn.execute(
            "SELECT id FROM admin_notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Notification not found.")
        conn.execute(
            "UPDATE admin_notifications SET read_at = ?, read_by = ? WHERE id = ? AND read_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), current.id, notification_id),
        )
    return {"status": "success", "message": f"Notification {notification_id} marked as read."}


# ---------------------------------------------------------------------------
# admin: enrollment and shift rules
# ---------------------------------------------------------------------------
@router.get("/admin/enroll/needs_reenrollment")
async def list_stale_templates(current: CurrentUser = Depends(admin_only)):
    """Who has to be photographed again, and why.

    The replacement of the face detector changed the crop an embedding is computed from,
    so a template made before it no longer describes the picture a punch produces. Scoring
    one anyway is the worst outcome available - a meaningless number that can read as a
    *mismatch*, which accuses a worker of being somebody else - so ``compare_faces_sync``
    refuses instead and points here.

    Deliberately not a field on ``/admin/users``: that payload is a fixed contract every
    row of the roster carries, and this is a short list an administrator acts on once
    after an upgrade. The repair is the enrollment flow that already exists -
    ``/admin/enroll`` with a fresh photo, or an enrollment link for a worker who is not at
    the desk - so this endpoint reports rather than repairs.

    ``expected_dimensions`` is left to the caller's own model by way of the live
    embedding's shape check at punch time; here a template is stale when it records no
    pipeline (every file written before the change) or records a different one.
    """
    return _json(biometrics.stale_references())


@router.post("/admin/enroll")
async def enroll_worker(
    request: Request,
    worker_id: str = Form(...),
    photo: UploadFile = File(...),
    current: CurrentUser = Depends(admin_only),
):
    """Write a reference photo as an *existing* account's template.

    This is the console's replacement for a face that was never taken or has gone stale
    (``/admin/enroll/needs_reenrollment`` names the second case): the administrator picks a
    file from the machine in front of them, and everything a punch is checked against --
    the template and the selfie beside it -- is rewritten under the account's immutable
    biometric id. A file from disk, so no liveness claim is made for it: the paths that
    judge a live capture are ``/worker/me/enroll`` and the person's own registration link.

    Any administrator may do this for a **worker**, and that is deliberate: it is the same
    authority as changing their name or their rate, and it is the answer on the day a
    worker's photo will not match. Over an **administrator's** face it is a standard admin's
    escalation - a template is what a punch is verified against, so writing one is choosing
    who that account will clock in as - and ``_guard_standard_admin`` refuses it, exactly as
    it does for the account's name, its password and its status.
    """
    try:
        int(worker_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Worker ID must be a numeric integer.")

    with db() as conn:
        user = conn.execute("SELECT name, role FROM users WHERE id = ?", (worker_id,)).fetchone()
    if user is None:
        raise HTTPException(status_code=404, detail="Worker ID not found in database. Create the user first.")
    _guard_standard_admin(current, user, "enroll a face for")

    file_bytes = await uploads.read_photo(photo, field="enrollment photo")
    try:
        image = uploads.decode_photo(file_bytes, field="enrollment photo")
        # 800 px is what this endpoint has always embedded and stored; the detector gains
        # nothing from more, and the reference selfie beside the row is a thumbnail.
        image.thumbnail((800, 800))
    except Exception:
        raise HTTPException(status_code=400, detail="Image processing failed.")

    try:
        embedding_objs = await face_engine.ENGINE.represent_async(enrollment.face_array(image))
        if len(embedding_objs) > 1:
            raise HTTPException(status_code=400, detail="Multiple faces detected in enrollment photo.")

        # Both files, atomically, under the account's immutable biometric id. This used to
        # write the selfie straight into ``worker_photos/<account id>.jpg``, embed from
        # ``backend/temp_enroll_<account id>.jpg``, and write the template non-atomically -
        # a full-size JPEG of a worker's face named by their account id, left in the source
        # tree whenever the process died mid-request. The image goes to the model from
        # memory, so there is no temporary file left to remove on either path.
        await run_in_threadpool(
            biometrics.write_reference, worker_id, image, embedding_objs[0]["embedding"]
        )

        with db(write=True) as conn:
            conn.execute(
                "UPDATE users SET enrolled_at = ?, template_version = COALESCE(template_version, 0) + 1 "
                "WHERE id = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), worker_id),
            )
            _audit(
                conn,
                action="biometric_enroll",
                actor=current,
                entity="users",
                entity_id=worker_id,
                after={"template_replaced": True},
                request=request,
            )
        return {"status": "success", "message": f"Facial data for worker {worker_id} successfully enrolled."}
    except face_engine.FaceEngineBusy as exc:
        raise face_engine.busy_http_exception(exc) from None
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="No face detected. Please ensure good lighting and clear view.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Enrollment failed: {exc}")


# ---------------------------------------------------------------------------
# company identity - whose app this is, on the login screen and on every sheet
# ---------------------------------------------------------------------------
#
# The company's name, its lockup lines and its mark used to be a constant in
# ``frontendjavascript.js`` and a file beside it, which meant every screen that says whose
# payroll this is - and the sheet an administrator hands to payroll - needed a code change
# and a redeploy to carry a different name. They are a settings row now (see ``branding``),
# editable on the console's Admin tab like the shift rules next door.
#
# The read is deliberately public. The screen that most needs the company's name is the
# login panel, which by definition has no session, and the alternative - a hardcoded wordmark
# on the one screen whose whole job is to answer "is this the app my administrator told me
# about?" - is the thing being removed here. What this exposes is the name the company put
# on its own front door and the mark it prints on its own documents: no account details, no
# configuration, nothing an unauthenticated visitor could not read off a payslip.


@router.get("/branding")
async def read_branding():
    """The company's own name and mark, as configured. Public: the login screen needs it.

    A field is ``null`` when nobody has configured it - the caller keeps the lockup it
    shipped with - and an empty string when the company decided that line is not printed.
    Those are different answers and they stay different all the way to the paper, which is
    why this returns both rather than flattening them into one "empty".

    ``logo_url`` is a path, not an absolute URL: this page is served from a LAN address, a
    chosen port or an HTTPS tunnel depending on the deployment, and only the client knows
    which origin it is talking to. The ``?v=`` in it is the stored version of the bytes, so
    a replaced mark is a different URL and no cache can serve the old one to a fresh page.
    """
    with db() as conn:
        return _json(branding.payload_for_api(conn))


@router.get("/branding/logo")
async def read_branding_logo():
    """The company's mark, as bytes. Public, like the name beside it, and for the same reason.

    Cached hard (a year, immutable) because the URL carries the version of the bytes it
    names: a replacement is a *different* URL, so there is nothing here to revalidate and a
    phone stops fetching a logo it already has. The type is one of two this server chose
    when it re-encoded the upload, and ``nosniff`` says so - the browser is not invited to
    decide for itself what these bytes are.
    """
    with db() as conn:
        stored = branding.logo_bytes(conn)
    if stored is None:
        raise HTTPException(status_code=404, detail="No logo is configured.")
    data, mime, _version = stored
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(data)),
        },
    )


class BrandingUpdate(BaseModel):
    """The company's own lines. Every one of them is optional *and* nullable.

    Absent means "leave this alone"; ``null`` means "back to the shipped lockup"; a string -
    including the empty one - means the company decided what that line says, so an empty box
    is a line that is not printed rather than a silent reset.
    """

    name: str | None = None
    legal: str | None = None
    est: str | None = None
    tagline: str | None = None


@router.post("/admin/branding")
async def update_branding(
    request: Request, payload: BrandingUpdate, current: CurrentUser = Depends(admin_only)
):
    """Save the company's name and lockup lines.

    ``admin_only``, the same authority the shift rules beside it take: this is the company's
    own identity, not one account's, and it is the \"who runs this deployment\" role rather
    than something a page can reach without a session (see the read endpoints above for why
    reading is public and writing is not).
    """
    supplied = {key: getattr(payload, key) for key in payload.model_fields_set}
    if not supplied:
        raise HTTPException(status_code=400, detail="Nothing to save.")
    with db(write=True) as conn:
        result = branding.write(conn, supplied, actor_id=current.id)
        _audit(
            conn,
            action="branding_update",
            actor=current,
            entity="company_settings",
            entity_id=1,
            before=result["before"],
            after=result["after"],
            request=request,
        )
        state = branding.payload_for_api(conn)
    return {"status": "success", "branding": state}


@router.post("/admin/branding/logo")
async def upload_branding_logo(
    request: Request,
    logo: UploadFile = File(...),
    current: CurrentUser = Depends(admin_only),
):
    """Set the company's mark from an uploaded image.

    The bytes are validated by ``uploads`` (size while reading, type by signature, pixel
    ceiling before decoding) and then *re-encoded* into the stored form - PNG when the image
    has transparency to keep, JPEG otherwise. The file the browser can fetch afterwards is
    therefore one this server wrote, never the one it was sent: an upload cannot become a
    document this origin serves, whatever it claimed to be.

    The audit records the mark's *shape* - type, dimensions, size - and never its bytes: the
    question the log answers is "who put this logo on our payslips, and when", and a few
    hundred kilobytes of base64 in every audit row would make the log unreadable.
    """
    data = await uploads.read_photo(logo, field="logo")
    with db(write=True) as conn:
        before = branding.read(conn)["logo"]
        state = branding.set_logo(conn, data, actor_id=current.id)
        _audit(
            conn,
            action="branding_logo_set",
            actor=current,
            entity="company_settings",
            entity_id=1,
            before=before,
            after=state["logo"],
            request=request,
        )
        payload = branding.payload_for_api(conn)
    return {"status": "success", "branding": payload}


@router.delete("/admin/branding/logo")
async def remove_branding_logo(request: Request, current: CurrentUser = Depends(admin_only)):
    """Remove the company's mark, so the one this application ships with is used again.

    A named act rather than "upload an empty file": "put our old mark back" is a thing an
    administrator wants to do, and it is the undo for the upload above. The row's version
    is bumped either way, so a screen still holding the removed mark's URL is holding a URL
    that now answers 404 rather than a cached image of a logo the company just took down.
    """
    with db(write=True) as conn:
        had = branding.clear_logo(conn, actor_id=current.id)
        if had:
            _audit(
                conn,
                action="branding_logo_removed",
                actor=current,
                entity="company_settings",
                entity_id=1,
                request=request,
            )
        payload = branding.payload_for_api(conn)
    return {"status": "success", "removed": had, "branding": payload}


@router.get("/admin/corpus_consents")
async def list_corpus_consents(
    limit: int = 200, current: CurrentUser = Depends(admin_only)
):
    """Who has opted in to calibration capture, and the decision history behind it.

    This is the operator's half of the consent picture: the corpus coverage an operator can
    plan a derivation around is exactly the set this returns, and the deployment switch (an
    environment variable) means nothing without it - capture runs only for the intersection of
    the two. Per-worker histories are included because "granted, then withdrawn" is the state
    whose *captures* an erasure request will name, and that question is answered here rather
    than by re-deriving it from a folder of sidecars.
    """
    limit = max(1, min(int(limit), 500))
    with db() as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, worker_id, granted, note, created_at, created_by, revoked_at "
                "FROM corpus_capture_consents ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        granted = corpus.consented_workers(conn)
    return {
        "capture_enabled": bool(settings.calibration_capture_enabled),
        "consented_workers": sorted(granted),
        "decisions": [dict(row) for row in rows],
    }


@router.get("/admin/shift_rules")
async def read_shift_rules(current: CurrentUser = Depends(admin_only)):
    """The company's shift rules, and what this application makes of them.

    ``day_end`` is not a setting; it is the consequence of the settings beside it - the two
    watchers' thresholds, whether the auto-close is on, whether the crossing alert can
    therefore ever fire, and which rule ends a day. It travels with the rules so the console
    can say "with these numbers, nothing will ever be flagged as overtime" on the screen
    where the numbers are typed, instead of only in a startup log nobody reads. The
    precedence itself is stated once, in ``shift_hours.day_end_rules``.
    """
    rules = get_shift_rules()
    return {**rules, "day_end": shift_hours.day_end_rules(rules)}


@router.post("/admin/shift_rules")
async def update_shift_rules(
    request: Request, payload: ShiftRulesUpdate, current: CurrentUser = Depends(admin_only)
):
    # ``model_fields_set``, not "every key the model has": a field the caller did not send must
    # be left alone, and a field the caller *did* send as empty means "back to the shipped
    # value". This table stores that as NULL (``get_shift_rules`` reads a NULL column as
    # "nothing configured, use ``DEFAULT_SHIFT_RULES``"), and the previous version filtered
    # None values out - so an administrator could move the company window to a night shift and
    # then had no way to take it off again: an emptied box was indistinguishable from an
    # untouched one. ``edit_site`` draws the same distinction for a site's own columns.
    supplied = {key: getattr(payload, key) for key in payload.model_fields_set}
    if not supplied:
        raise HTTPException(status_code=400, detail="No shift rule changes supplied.")
    allowed = {
        "clock_in_window_start",
        "clock_in_window_end",
        "regular_hours",
        "overtime_notify_hours",
        "site_timezone",
        "break_minutes",
        "break_after_hours",
        "auto_close_at_regular",
    }
    # An emptied box on a window field means "back to the shipped value", and these three columns
    # are ``NOT NULL`` - so it is stored as ``''``, which ``get_shift_rules`` and
    # ``shift_windows._pick`` both read as "nothing configured". The previous version dropped
    # every ``None``, which made an emptied box indistinguishable from an untouched field: an
    # administrator could move the company window to a night shift and never take it off again.
    # A null anywhere else means "not supplied" and is left out, rather than written into a
    # column that cannot hold it.
    changes: dict[str, Any] = {}
    for key, value in supplied.items():
        if key not in allowed:
            continue
        if value is None:
            if key in _COMPANY_WINDOW_KEYS:
                changes[key] = ""
            # ``continue`` matters: without it the line below wrote the ``None`` back over the
            # ``''`` - straight into a ``NOT NULL`` column, so clearing the company clock-in
            # window answered 500 and saved nothing. It also kept a ``None`` for every other
            # field in play, which is what "not supplied" has to mean.
            continue
        changes[key] = value
    # Refuse nonsense rather than storing it: a negative break or a paid day of zero
    # would silently pay every worker for nothing, and 0/1 is not a preference.
    if changes.get("break_minutes") is not None and not (0 <= float(changes["break_minutes"]) <= 240):
        raise HTTPException(status_code=400, detail="break_minutes must be between 0 and 240.")
    if changes.get("break_after_hours") is not None and not (
        0 <= float(changes["break_after_hours"]) <= 24
    ):
        raise HTTPException(status_code=400, detail="break_after_hours must be between 0 and 24.")
    if changes.get("regular_hours") is not None and not (0 < float(changes["regular_hours"]) <= 24):
        raise HTTPException(status_code=400, detail="regular_hours must be between 0 and 24.")
    # The overtime line is a *paid-hours* figure, and it is compared against one, so it gets
    # the same range check the paid day does. It went unvalidated: a zero (or a negative, or
    # 900) was stored, and a line at 0 holds every shift of every length for approval while a
    # line at 900 silently means "nobody is ever on overtime". Neither is a preference.
    if changes.get("overtime_notify_hours") is not None and not (
        0 < float(changes["overtime_notify_hours"]) <= 24
    ):
        raise HTTPException(
            status_code=400, detail="overtime_notify_hours must be between 0 and 24."
        )
    if changes.get("auto_close_at_regular") is not None:
        changes["auto_close_at_regular"] = 1 if int(changes["auto_close_at_regular"]) else 0

    with db(write=True) as conn:
        before = dict(
            conn.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone() or {}
        )
        assignments = ", ".join(f"{key} = ?" for key in changes)
        conn.execute(
            f"UPDATE shift_rules SET {assignments}, updated_at = ?, updated_by = ? WHERE id = 1",
            (
                *changes.values(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                current.id,
            ),
        )
        _audit(
            conn,
            action="shift_rules_update",
            actor=current,
            entity="shift_rules",
            entity_id=1,
            before=before,
            after=changes,
            request=request,
        )
    # The same ``day_end`` block the read returns, so saving a change shows its verdict on
    # the spot: an operator who sets the alert back above the regular day sees immediately
    # that nothing will be flagged.
    rules = get_shift_rules()
    return {"status": "success", "rules": {**rules, "day_end": shift_hours.day_end_rules(rules)}}


# ---------------------------------------------------------------------------
# health, readiness
# ---------------------------------------------------------------------------
@router.get("/status")
async def api_status():
    """Cheap liveness ping.

    The body is deliberately unchanged from the original build: `/api/v1/status` is the
    readiness probe every deployment and the test suite already depend on, and widening
    its response would be a breaking change to a published contract. The richer detail
    lives at `/api/v1/status/detail`.
    """
    return {"status": "active"}


@router.get("/status/detail")
async def api_status_detail(current: CurrentUser = Depends(admin_only)):
    """Operational detail: anti-spoofing posture, watcher state, offline policy.

    Admin-only because it describes the deployment's defences, including whether the
    liveness model is currently loaded.
    """
    return {
        "status": "active",
        "version": settings.app_version,
        "liveness": liveness.status(),
        "overtime_watcher": overtime.watcher_running(),
        "offline_sync": {
            "signature_required": settings.offline_signature_required,
            "max_offline_hours": settings.offline_punch_max_age_hours,
            "batch_max": settings.offline_batch_max,
        },
    }


@app.get("/metrics", include_in_schema=False)
@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """The Prometheus scrape endpoint. Never anonymous, and never in the OpenAPI schema.

    Served at **both** ``/metrics`` and ``/api/v1/metrics``, from this one handler. The bare
    path is the convention every scrape config and dashboard template already assumes, and
    the scoped one is where an API client expects to find it; publishing two names for one
    payload costs nothing and no longer risks a scrape that silently 404s because an operator
    aimed at ``/metrics`` while the route lived under the prefix. Both are added here, so the
    protection, the 501 when the library is missing and the ``no-store`` header cannot drift
    between them.

    Two ways in, and which one applies is a deployment decision rather than a fallback:

    * ``METRICS_TOKEN`` set - a scrape job sends ``Authorization: Bearer <token>`` (or
      ``?token=`` for a scraper that cannot set headers). Compared with
      ``secrets.compare_digest`` so the comparison does not leak the token's length or
      prefix through timing. This is the mode a real Prometheus should use.
    * no token - an **admin JWT** is required. That keeps a working default for an operator
      who is testing (`curl -H "Authorization: Bearer $TOKEN" .../metrics`) without leaving
      the endpoint readable by anyone who can reach the port, which is the failure this
      codebase keeps finding in its own history.

    ``METRICS_ENABLED=0`` answers 404 rather than 403: a disabled surface should not confirm
    that it exists. The body is the standard exposition format; the response is marked
    ``no-store`` because a cached scrape is a graph that lies.
    """
    if not telemetry.AVAILABLE:
        # An optional extra that is absent is reported as such, the same way the XLSX export
        # reports a missing openpyxl rather than answering 500 to a monitoring probe.
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "metrics_unavailable",
                "message": (
                    "Prometheus metrics are not installed on this server. "
                    "Install them with: pip install -r requirements-optional.txt"
                    f" ({telemetry.IMPORT_ERROR})"
                ),
            },
        )
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    if not _metrics_authorised(request):
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "metrics_unauthorised",
                "message": (
                    "Metrics require the scrape token (Authorization: Bearer <METRICS_TOKEN>) "
                    "or an administrator session."
                ),
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Response(
        content=telemetry.render(),
        media_type=telemetry.METRICS_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


def _bearer_token(request: Request) -> str:
    """The caller's bearer token, from the header or the ``token`` query parameter."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return str(request.query_params.get("token") or "").strip()


def _metrics_authorised(request: Request) -> bool:
    """Whether this caller may read the registry. See ``metrics`` for the two modes."""
    presented = _bearer_token(request)
    expected = str(settings.metrics_token or "")
    if expected:
        # Constant-time: a length-or-prefix leak on a shared secret is a real oracle, and
        # this is the one credential in the app that a scrape job sends on every poll.
        return bool(presented) and secrets.compare_digest(presented, expected)
    try:
        claims = security.decode_access_token(presented) if presented else None
    except Exception:  # noqa: BLE001 - any token problem is simply "not authorised"
        return False
    if not claims:
        return False
    user_id = str(claims.get("sub") or "")
    row = security._load_user(user_id) if user_id else None
    if row is None:
        return False
    return str(row["role"]) in {"admin", "head_admin"}


@router.post("/admin/overtime/scan")
async def scan_overtime_now(current: CurrentUser = Depends(admin_only)):
    """Run the overtime watcher on demand.

    The timer normally does this every ``OVERTIME_WATCHER_INTERVAL_SECONDS``; this
    endpoint exists so an administrator is not blind between ticks, and so the
    behaviour is testable without waiting for a thread.
    """
    return overtime.scan_overtime()


# Instrumented here rather than at the top of the module because the middleware must be
# added to the app object before it starts serving, and this is the last point where the
# whole route table (readiness included) exists. It records every request, including the
# ones that raise: an unhandled exception is a 500 that never became a response object.
telemetry.instrument_app(app)

router.include_router(readiness.router)

app.include_router(router, prefix="/api/v1")
# Phase 02 routers. Additive: every pre-existing path keeps its shape, so the shipped
# frontend needs no change to keep working.
app.include_router(offline_sync.router, prefix="/api/v1")
app.include_router(offline_sync.admin_router, prefix="/api/v1")
app.include_router(enrollment.router, prefix="/api/v1")
app.include_router(enrollment.public_router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")
# Worker notes: the written channel between a worker and the administrator. Additive,
# like the phase-02 routers above - two new prefixes, nothing moved.
app.include_router(notes.router, prefix="/api/v1")
app.include_router(notes.admin_router, prefix="/api/v1")
# Quick clock links: the admin surface (issue, list, revoke, review the punches) and the
# public one-tap punch the link itself authenticates. Additive, like the routers above.
app.include_router(quick_links.admin_router, prefix="/api/v1")
app.include_router(quick_links.public_router, prefix="/api/v1")
# The root tier's own surface: runtime configuration, the private alert hub, diagnostics and
# the raw audit stream. Additive, like every router above - nothing already served moved, and
# every route on it is built from ``require_developer``, so an administrator cannot reach one
# by any name they can present.
app.include_router(developer.router, prefix="/api/v1")
# Every statement the slow-query view reports carries the trace id of the request that ran it,
# and the id is owned by ``developer``. Bound here rather than inside ``database`` because the
# layer below must not import the tier above it - the dependency points one way.
database.set_trace_provider(developer.current_trace_id)


@app.get("/enroll/{token}", include_in_schema=False)
async def enrollment_page(token: str):  # noqa: ARG001 - the token is read by the page itself
    """Serve the standalone mobile capture page for a self-service enrollment link.

    Separate from the SPA on purpose: a worker enrolling on their own phone has no
    account yet, so this page must not load the admin/worker dashboard bundle, and it
    has to work from a bare link with no prior visit to the app.
    """
    page = os.path.join(FRONTEND_DIR, "enroll.html")
    if not os.path.exists(page):  # pragma: no cover - packaging accident
        return {"error": f"enroll.html not found in {FRONTEND_DIR}"}
    return FileResponse(page)


@app.get("/q/{token}", include_in_schema=False)
async def quick_link_page(token: str):  # noqa: ARG001 - the token is read by the page itself
    """Serve the one-tap clock page for a quick link.

    Its own page rather than a route in the dashboard, for the same reason ``/enroll`` has
    one: whoever opens this link has no session, no password and possibly no app installed,
    so loading the console's bundle would show them a login screen instead of the button
    they were sent.
    """
    page = os.path.join(FRONTEND_DIR, "quick.html")
    if not os.path.exists(page):  # pragma: no cover - packaging accident
        return {"error": f"quick.html not found in {FRONTEND_DIR}"}
    return FileResponse(page)
    


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Bring the database up to date, verify the build, then start the overtime timer."""
    summary = init_db()
    application.state.startup_summary = summary
    application.state.gate_report = readiness.run_startup_gate(application)
    # Started only after the gate has passed: the watcher must not run against a
    # schema the gate has just declared unusable.
    application.state.overtime_watcher = overtime.start_watcher()
    # Retention is the same shape of timer as the overtime watcher and starts beside it, after
    # the same gate. It is deliberately *not* an asyncio task: the work is blocking file
    # overwrites plus SQLite writes, so as an awaitable it would hold the event loop for the
    # duration of the largest directory - stalling every punch at the gate while retention
    # tidies up. ``python -m retention --apply`` is the entry point for a deployment that
    # would rather schedule it from cron.
    application.state.retention_sweeper = retention.start_watcher()
    try:
        yield
    finally:
        overtime.stop_watcher()
        retention.stop_watcher()
        # Stops the inference workers, so a graceful shutdown does not wait on threads that
        # uvicorn knows nothing about. They are daemons and would die with the process
        # anyway; saying so here is what keeps the queue from being drained behind a
        # server that has already decided it is finished.
        face_engine.ENGINE.shutdown()


# The registry lives in ``readiness`` and needs an app to introspect, so the
# lifespan is attached after the routes exist.
app.router.lifespan_context = lifespan


# 3. Legacy mount, kept so existing /static/... bookmarks keep resolving.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def read_index():
    file_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(file_path):
        return {"error": f"index.html not found in path: {os.path.abspath(file_path)}"}
    return FileResponse(file_path)


# 5. Also serve the frontend from the site root. index.html loads its assets with
# RELATIVE paths so the app keeps working when the folder is hosted on its own.
# Registered last on purpose: a mount at "/" would otherwise shadow the API.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


# 6. Make the browser revalidate the frontend on every load.
#
# StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers apply
# *heuristic* freshness and are free to keep running a months-old
# frontendjavascript.js without ever asking. That is how a fixed frontend keeps
# reproducing an already-fixed bug on a worker's phone: the server and the
# browser disagree about which code is running, and nothing says so. Revalidation
# on a site LAN costs a 304, which is a cheap price for the guarantee that the
# frontend reaching the device is the frontend that was shipped.
@app.middleware("http")
async def revalidate_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    # ``/metrics`` is excluded on purpose: a scrape must never be served from a cache, and
    # "no-cache, must-revalidate" is an instruction a proxy is free to optimise around.
    if not request.url.path.startswith("/api/") and request.url.path != "/metrics":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# 7. Tag every request with a trace id, and let the failures an operator needs to hear about
#    reach the root tier's alert hub.
#
# The id is set before the route runs so that a diagnostics statement, an audit row and an
# alert written by the same request can be joined by somebody holding only one of them. A
# ContextVar, so two concurrent requests never share one, and cleared afterwards so a pooled
# task cannot inherit the last request's id.
#
# The alert is raised on the *response* status as well as on an exception that got past every
# handler: a 500 that a handler turned into a response and one that propagated are the same
# event to an operator. A 429 is the rate-limit signal for the same reason - it is observable
# at the edge, so nothing has to reach into the limiter to notice a client being refused.
@app.middleware("http")
async def developer_trace_and_alerts(request: Request, call_next):
    developer.set_trace_id(developer.new_trace_id())
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            developer.raise_alert(
                kind="unhandled_exception",
                summary=f"{request.method} {request.url.path} raised {type(exc).__name__}",
                severity="critical",
                source="http",
                detail={"method": request.method, "path": request.url.path},
            )
            raise
        if request.url.path.startswith("/api/"):
            if response.status_code >= 500:
                developer.raise_alert(
                    kind="unhandled_exception",
                    summary=f"{request.method} {request.url.path} answered {response.status_code}",
                    severity="error",
                    source="http",
                    detail={
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                    },
                )
            elif response.status_code == 429:
                # Deduplicated over five minutes: a scan trips this hundreds of times, and an
                # alert hub that fills with one client's noise is one nobody reads.
                developer.raise_alert(
                    kind="rate_limit_spike",
                    summary=f"{request.url.path} refused a client for rate",
                    severity="warning",
                    source="http",
                    detail={"method": request.method, "path": request.url.path},
                    dedupe_window_seconds=300,
                )
        # The id travels back to the caller so a worker's screenshot and an operator's log line
        # can be joined without either of them reading a database.
        response.headers["X-Trace-Id"] = developer.current_trace_id() or ""
        return response
    finally:
        developer.set_trace_id(None)


if settings.face_model_preload:  # pragma: no cover - heavy, skipped when disabled
    try:

        import face_onnx

        face_onnx.load_now()
    except Exception as exc:  # pragma: no cover - the model can also be fetched lazily
        print(f"[startup] face model preload skipped: {exc}")
