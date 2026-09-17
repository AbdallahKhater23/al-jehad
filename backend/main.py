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
from deepface import DeepFace
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
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
import database
import enrollment
import face_engine
import liveness
import migrations
import netguard
import notes
import notifications
import offline_sync
import overtime
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
LOCAL_REFS_DIR = str(PROJECT_ROOT / "local_references")
WORKER_PHOTOS_DIR = str(PROJECT_ROOT / "worker_photos")

#: Cosine distance bands. Below the first value the match is auto-approved, the
#: middle band is logged and routed to a human, and above the second the
#: attendance action is refused.
FACE_APPROVE_THRESHOLD = 0.40
FACE_REVIEW_THRESHOLD = 0.60

DEFAULT_SHIFT_RULES: dict[str, Any] = {
    "clock_in_window_start": "04:00",
    "clock_in_window_end": "06:30",
    "regular_hours": 8.0,
    "overtime_notify_hours": 8.1,
    "site_timezone": "Africa/Cairo",
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
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO attendance_logs
            (worker_id, site_name, action, timestamp, hours, score, status, status_code,
             lat, lon, accuracy, source, liveness_class, liveness_score, flag_reason,
             approved_hours, overtime_hours, break_hours)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            worker_id, site_name, action, timestamp, hours, score, status, status_code,
            lat, lon, accuracy, source, liveness_class, liveness_score, flag_reason,
            approved_hours, overtime_hours, break_hours,
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
            "Africa/Cairo or Asia/Riyadh."
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
    regular_hours: float | None = None
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


#: What a worker is told when the *photo* is the problem, keyed by the reason
#: ``compare_faces_sync`` reports. Those strings are written for an operator reading a
#: log - and one of them (``Internal processing error: <exception>``) used to be echoed
#: to the phone verbatim, which tells the worker nothing they can act on and hands out a
#: little of our internals. These say what to do instead, and the code beside each one is
#: what the client, the logs and the triage views key on, so the wording can change
#: without anything downstream changing with it.
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
    try:
        with open(reference_json_path, "r") as handle:
            reference_embedding = json.load(handle)

        # ``represent_direct``, not a submission: the endpoint submits *this whole
        # function* to the face engine, so the model call here is the body of an engine
        # job. A job that submitted to its own pool would wait for the worker running it
        # (see ``face_engine``), and the model and detector it names are the ones the
        # engine names, so a punch and an enrollment cannot drift onto different models.
        live_embedding_objs = face_engine.ENGINE.represent_direct(live_image_data)

        if len(live_embedding_objs) > 1:
            return {"verified": False, "distance": 99.9, "error": "Multiple faces detected."}

        live_embedding = live_embedding_objs[0]["embedding"]
        # The comparison itself, timed apart from the embedding that fed it: one is a numpy dot
        # product over 4096 floats and the other is half a second of TensorFlow, and reporting
        # them as one number would hide whichever one changed.
        started = time.perf_counter()
        distance = cosine(reference_embedding, live_embedding)
        telemetry.observe_cosine(time.perf_counter() - started)

        # The score on the way past, because it is the only early warning that the model, the
        # camera fleet or the enrollment photos changed: the thresholds are fixed at 0.40 and
        # 0.60, so a shift in *this* distribution is what moves the review queue. Observed for
        # real distances only - the 99.9 sentinel on the refusal paths below is not a score, and
        # putting it in the histogram would make every no-face frame look like a near miss.
        telemetry.observe_match_score(distance)

        return {
            "verified": bool(distance <= FACE_APPROVE_THRESHOLD),
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
    try:
        uid_int = int(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="User ID must be a numeric integer.")

    if role == "worker":
        if not (1 <= uid_int <= 499):
            raise HTTPException(status_code=400, detail="Worker ID must be in range 1-499 for role 'worker'.")
    elif role == "moallem":
        if not (500 <= uid_int <= 999):
            raise HTTPException(status_code=400, detail="Lead Worker (Moallem) ID must be in range 500-999.")
    elif role == "admin":
        if not (1000 <= uid_int <= 4999):
            raise HTTPException(status_code=400, detail="Admin ID must be in range 1000-4999.")
    elif role == "head_admin":
        if uid_int < 5000:
            raise HTTPException(status_code=400, detail="Head Admin ID must be 5000 or greater.")
    else:
        raise HTTPException(status_code=400, detail="Invalid user role specified.")


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
    match the enrolled template (``liveness.py`` then DeepFace).

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
        sites = conn.execute("SELECT * FROM construction_sites").fetchall()

    detected_site = None
    detected_site_row = None
    for site in sites:
        if get_distance_meters(site["lat"], site["lon"], lat, lon) <= site["radius"]:
            detected_site = site["site_name"]
            detected_site_row = site
            break

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
    image.thumbnail((640, 640))
    # ``rgb_array`` feeds the liveness model, which was trained on RGB crops;
    # ``img_array`` is the BGR view DeepFace expects. Computing both here keeps
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
        raise HTTPException(
            status_code=404,
            detail="Facial reference not registered. Please contact your administrator to enroll.",
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
    if similarity_score <= FACE_APPROVE_THRESHOLD:
        status_val = "success"
        status_msg = "Auto-Approved"
        log_status = STATUS_APPROVED
        status_code = "approved"
        telemetry.observe_verification("approved")
    elif similarity_score <= FACE_REVIEW_THRESHOLD:
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

    rules = get_shift_rules()
    regular_hours = float(rules["regular_hours"])
    overtime_hours = float(rules["overtime_notify_hours"])
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    hours_worked = 0.0
    break_taken = 0.0
    overtime = 0.0
    flag_reason = liveness_flag

    with db(write=True) as conn:
        if action == ACTION_CLOCK_IN:
            existing = conn.execute(
                "SELECT worker_id FROM active_sessions WHERE worker_id = ?", (current.id,)
            ).fetchone()
            if existing:
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
                raise HTTPException(
                    status_code=403, detail="Your account is flagged for manual review. Please contact HR."
                )

            session = conn.execute(
                "SELECT clock_in_time, site_name FROM active_sessions WHERE worker_id = ?", (current.id,)
            ).fetchone()
            if session is None:
                raise HTTPException(status_code=400, detail=_no_open_shift_message(conn, current.id))

            clock_in_time = _parse_ts(session["clock_in_time"])
            if clock_in_time is None:
                raise HTTPException(status_code=400, detail="The stored clock-in time is unreadable.")

            # Time on site first, then what is paid for it: the unpaid break belongs to
            # the shift (and is stored on it), not to the person closing it, so the
            # worker's own clock-out, an administrator's force-clock-out and the
            # auto-close all arrive at the same number through ``shift_hours``.
            elapsed_hours = round(max(0.0, (now - clock_in_time).total_seconds() / 3600.0), 4)
            hours_worked, break_taken = shift_hours.paid_hours(elapsed_hours, rules)
            hours_note = shift_hours.describe(elapsed_hours, hours_worked, break_taken)
            conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (current.id,))

            # Overtime is tracked, not silently approved: any shift past the
            # notification threshold is routed to a human before it can be paid.
            if hours_worked > overtime_hours:
                overtime = round(hours_worked - regular_hours, 4)
                log_status = STATUS_PENDING_OVERTIME
                status_code = "pending_overtime"
                status_val = "flagged"
                flag_reason = " | ".join(
                    part
                    for part in (
                        flag_reason,
                        f"{hours_worked:.2f}h exceeds the {overtime_hours:g}h regular threshold",
                    )
                    if part
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
                        f"{user_row['name']} (id {current.id}) worked {hours_worked:.2f}h at "
                        f"'{session['site_name']}', past the {overtime_hours:g}h threshold. "
                        "Approve or adjust the extra hours before payroll."
                    ),
                    worker_id=current.id,
                    site_name=session["site_name"],
                    dedupe_key=f"overtime:{current.id}:{now_str}",
                    payload={"hours": hours_worked, "overtime_hours": overtime},
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
            overtime_hours=overtime or None,
            break_hours=break_taken or None,
        )
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
        "overtime_hours": overtime,
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
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, email, phone, role, status, enrolled_at, biometric_id, "
            "COALESCE(token_version, 0) AS token_version, "
            "(password_hash IS NOT NULL AND password_hash <> '') AS password_set "
            "FROM users ORDER BY CAST(id AS INTEGER) ASC"
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
    with db() as conn:
        if action:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT ?", (action, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


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


@router.get("/admin/pending_reviews")
async def list_pending_reviews(current: CurrentUser = Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.worker_id, u.name, l.site_name, l.action, l.timestamp,
                   l.hours, l.score, l.status, l.status_code, l.flag_reason, l.overtime_hours
            FROM attendance_logs l
            JOIN users u ON l.worker_id = u.id
            WHERE l.status = ? OR l.status_code = 'pending_overtime'
            ORDER BY l.timestamp DESC
            """,
            (STATUS_PENDING_REVIEW,),
        ).fetchall()
    return [dict(row) for row in rows]


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

        conn.execute(
            "UPDATE attendance_logs SET status = ?, status_code = 'approved', approved_hours = ?, "
            "overtime_hours = ?, reviewed_by = ?, reviewed_at = ?, flag_reason = ? WHERE id = ?",
            (
                "Approved by Admin",
                approved,
                round(max(0.0, row["hours"] - float(get_shift_rules()["regular_hours"])), 4),
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
        elapsed_hours = round(max(0.0, (now - clock_in_time).total_seconds() / 3600.0), 4)
        # The same policy the worker's own clock-out applies, so the same shift pays the
        # same whichever way it is closed. Deducting the break only on one of those two
        # paths would mean the answer to "what is this day worth?" depended on whether
        # the worker remembered to tap the button, which is not a payroll rule.
        hours_worked, break_taken = shift_hours.paid_hours(elapsed_hours, get_shift_rules())
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
            overtime_hours=_overtime_hours(hours_worked),
            break_hours=break_taken or None,
        )
        _audit(
            conn,
            action="force_clock_out",
            actor=current,
            entity="active_sessions",
            entity_id=req.worker_id,
            after={
                "hours": hours_worked,
                "break_hours": break_taken,
                "elapsed_hours": elapsed_hours,
                "log_id": log_id,
            },
            request=request,
        )
    return {
        "status": "success",
        "message": (
            f"Worker {req.worker_id} successfully Force Clocked Out. "
            f"{shift_hours.describe(elapsed_hours, hours_worked, break_taken)}."
        ),
        "hours": hours_worked,
        "break_hours": break_taken,
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


def _overtime_hours(hours: float) -> float | None:
    try:
        regular = float(get_shift_rules()["regular_hours"])
    except (KeyError, TypeError, ValueError):
        regular = 8.0
    excess = round(hours - regular, 4)
    return excess if excess > 0 else None


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
    return [
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
    ]


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
    return {"unread": unread, "notifications": [dict(row) for row in rows]}


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
@router.post("/admin/enroll")
async def enroll_worker(
    request: Request,
    worker_id: str = Form(...),
    photo: UploadFile = File(...),
    current: CurrentUser = Depends(admin_only),
):
    try:
        int(worker_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Worker ID must be a numeric integer.")

    with db() as conn:
        user = conn.execute("SELECT name, role FROM users WHERE id = ?", (worker_id,)).fetchone()
    if user is None:
        raise HTTPException(status_code=404, detail="Worker ID not found in database. Create the user first.")

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


@router.get("/admin/shift_rules")
async def read_shift_rules(current: CurrentUser = Depends(admin_only)):
    return get_shift_rules()


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
    return {"status": "success", "rules": get_shift_rules()}


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


if settings.face_model_preload:  # pragma: no cover - heavy, skipped when disabled
    try:
        DeepFace.build_model("VGG-Face")
    except Exception as exc:  # pragma: no cover - the model can be fetched lazily
        print(f"[startup] DeepFace model preload skipped: {exc}")
