"""Offline punch synchronisation and tamper-proof queuing.

THE PROBLEM
-----------
Remote sites lose signal. A worker with no bars must still clock in and out, and the
punch has to become real attendance later - without that gap becoming the obvious
way to commit payroll fraud: backdating an arrival, moving a clock-out, re-sending
yesterday's punch, or inventing a shift that never happened.

WHY THE DEVICE CLOCK CANNOT BE TRUSTED
--------------------------------------
"The client records the time and we trust it" is not a design, it is a hole: a
worker can move the phone clock back three hours and claim overtime, and the server
cannot tell that apart from a genuine late clock-out. No exploit is required, only
a settings screen.

THE DESIGN
----------
1. **Device key, with no secret at rest.** ``POST /attendance/devices/register``
   stores a random ``key_salt`` and returns
   ``device_key = HMAC-SHA256(SECRET_KEY, "device|v1|<worker>|<device>|<epoch>|<salt>")``.
   A database dump therefore yields nothing that can sign a punch, and revocation is
   ``key_epoch += 1`` - every previously issued key stops verifying at once.
2. **Server-signed time anchors.** While online, the client fetches
   ``POST /attendance/anchors``. The anchor is signed with the **server's** secret and
   recorded in ``device_anchors``. This is the crux: the device key is held by the
   client, so an anchor signed with the device key could be forged by the very client
   it is meant to constrain. A server-signed anchor binds the punch to a moment the
   device was provably online, and an unknown ``anchor_id`` is refused outright.
3. **Monotonic elapsed time.** The offset from the anchor comes from the device's
   monotonic clock (``performance.now()``), which the user cannot wind backwards.
4. **The server computes the authoritative time**: ``anchor.server_time +
   monotonic_offset_s``. The device's wall clock is carried along as
   ``client_timestamp`` (audit) and as a tamper *signal*
   (``client_offset_s = client_timestamp - anchor.server_time``).
5. **HMAC-SHA256 over a canonical field string** (``SIGNATURE_VERSION = 1``), compared
   with ``hmac.compare_digest``. Editing a timestamp, a coordinate, the action or the
   anchor breaks the signature.
6. **Replay prevention in three layers**: ``client_punch_id`` UNIQUE (re-uploading a
   batch is a no-op), ``(device_id, nonce)`` UNIQUE (a captured request cannot be
   re-inserted), and a duplicate window on ``(worker, action, effective time)``
   (double-tap).
7. **Window policy**: the anchored time must lie within
   ``OFFLINE_PUNCH_MAX_AGE_HOURS`` (72 h, the offline tolerance) in the past and not
   more than ``OFFLINE_PUNCH_CLOCK_SKEW_SECONDS`` (300 s) in the future, with
   ``0 <= monotonic_offset_s <= 72 h``.

RESIDUAL RISK, STATED HONESTLY
------------------------------
A device that is offline for a day can, with its own key, claim any offset inside the
tolerance window; that is inherent to offline capture and cannot be removed
cryptographically. It is bounded by ``OFFLINE_PUNCH_MAX_AGE_HOURS`` and made visible:
every offline row carries ``client_timestamp``, ``anchor_server_time`` and the derived
``effective_time`` side by side, so the triage view shows a disagreement immediately.
Operators who want a tighter bound lower that setting (24 h is a reasonable site default).

A rejection is **stored, never dropped** (``status='rejected'`` + ``rejection_code``), so
a punch that really happened stays visible at
``GET /api/v1/admin/punch_queue?status=rejected`` for a human to resolve, instead of
vanishing because a phone clock was wrong.A punch that verifies but lands outside every geofence is accepted as
``status='flagged'``, materialised with ``flag_reason``, and notified: on an offline
capture the GPS fix is the least trustworthy part, so the worker stays on record
and an administrator decides.

NOTHING ARRIVES APPROVED
------------------------
Materialising a queued punch is not the same as confirming it. A punch taken with no
signal was never checked - not the face, not the frame, not even the site - and the only
evidence for it is a photo on the worker's own phone. So the clock-out - the row that
carries the hours - is written as ``pending_review``, or ``pending_overtime`` when the
shift crossed the overtime line (that hold is a fact about the hours and wins over the
generic one), and ``reports.PAYABLE_CODES`` contains neither: the hours sit in the
awaiting-approval column until an administrator approves them. The reason is recorded on
the row, because a queue entry nobody can explain is a queue entry nobody acts on.

The arrival is recorded as ``unverified_offline`` rather than held for review, and the
difference is deliberate. It carries no hours, so there is no money for a reviewer to
decide; and a ``pending_review`` row stops this worker closing their shift at all, because
both the online clock-out and the quick links refuse while one exists. Holding the arrival
would mean somebody who came in during a dead spot could not clock out - the app punishing
them for the dead spot, and the day's hours never recorded. Unverified is what the row is;
the departure's hold is what an administrator acts on.

The selfie the phone kept is uploaded next door (``POST /attendance/sync/photo``), where
the server runs the **same** liveness evaluation and face comparison an online punch runs
and writes the verdict onto that review row. It is evidence, not a verdict: a score
cannot approve a punch nobody was there to watch, and it cannot refuse one either - both
of those are the administrator's, in the review queue.

CLIENT CONTRACT
---------------
``sign_punch()`` is the reference implementation; a client port must reproduce
``canonical_punch()`` byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

import biometrics
import face_detector
import face_engine
import liveness
import migrations
import notifications
import overtime
import punch_frames
import shift_hours
import shift_windows
import telemetry
import textguard
import uploads
from config import settings
from database import db
from rate_limit import limiter
from security import CurrentUser, admin_only, any_authenticated

log = logging.getLogger("attendance.offline")

router = APIRouter(prefix="/attendance", tags=["attendance"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

SIGNATURE_VERSION = 1

ACTION_CLOCK_IN = "Clock In"
ACTION_CLOCK_OUT = "Clock Out"

STATUS_ACCEPTED = "accepted"
STATUS_FLAGGED = "flagged"
STATUS_REJECTED = "rejected"

#: ``attendance_logs.site_name`` is NOT NULL and legitimate rows name a real site, so a
#: punch that geolocates nowhere needs an explicit sentinel rather than a fabricated
#: site name that payroll could mistake for the truth.
SITE_UNASSIGNED = "Unassigned (offline)"

#: ``attendance_logs.score`` is NOT NULL. An offline punch has no face-match score at
#: materialisation time, so it is written as 0.0 with the reason recorded - never as a
#: passing score. ``POST /attendance/sync/photo`` replaces it with the real distance once
#: the queued selfie arrives (see ``score_queued_selfie``).
OFFLINE_SCORE = 0.0

LIVENESS_OFFLINE = "unverified_offline"

#: The attendance status of a materialised punch that no human has signed off. The label
#: is the code, because that is the predicate ``GET /admin/pending_reviews`` matches on -
#: ``main.STATUS_PENDING_REVIEW`` is the same string, and it lives here as well because
#: this module may not import ``main`` at module scope (``main`` imports this one).
STATUS_CODE_PENDING_REVIEW = "pending_review"
STATUS_LABEL_PENDING_REVIEW = "pending_review"
#: The arrival's own status: recorded, never checked, and deliberately *not* in the review
#: queue. The clock-out half is the row an administrator has to decide - it carries the hours -
#: and it is also the row whose hold is meaningful, because the shift is already closed. Holding
#: the arrival too would put a money-less row in front of a reviewer and, far worse, stop the
#: worker closing their own shift: both the online clock-out and the quick links refuse to
#: proceed while any ``pending_review`` row exists. See ``migrations``
#: ``STATUS_CODE_UNVERIFIED_OFFLINE`` for the same reasoning written down where the vocabulary
#: lives.
STATUS_CODE_UNVERIFIED_OFFLINE = migrations.STATUS_CODE_UNVERIFIED_OFFLINE
STATUS_LABEL_UNVERIFIED_OFFLINE = migrations.STATUS_LABEL_UNVERIFIED_OFFLINE
#: The overtime hold's label and code, spelled the way ``migrations.STATUS_CODE_MAP``
#: translates them (the map is the authority, and it is the table the migration of an old
#: database reads).
STATUS_LABEL_PENDING_OVERTIME = "Pending Overtime Approval"
STATUS_CODE_PENDING_OVERTIME = "pending_overtime"

#: Why every materialised punch is in the queue, recorded as its ``flag_reason``. It is
#: the *provenance* reason and not a finding about the shift: a worker who syncs a genuine
#: day reads the same sentence as one who does not, and what separates them is the review
#: - which is the point. One sentence for both ends of the shift (see ``materialize_worker``).
OFFLINE_REVIEW_REASON = (
    "offline punch: captured with no signal, so nothing about the face, the frame or the "
    "site was checked when it was taken; the queued selfie is scored when it reaches the "
    "server, and the hours are held for review until an administrator approves them"
)

# ---- rejection codes -------------------------------------------------------
ERR_DEVICE_UNKNOWN = "device_unknown"
ERR_DEVICE_REVOKED = "device_revoked"
# ---- rejection codes for the queued selfie ---------------------------------
ERR_PUNCH_UNKNOWN = "punch_unknown"
ERR_PUNCH_NOT_ACCEPTED = "punch_not_accepted"
ERR_PUNCH_NO_SELFIE = "punch_has_no_selfie"
ERR_PUNCH_NOT_MATERIALIZED = "punch_not_materialized"
ERR_PHOTO_MISMATCH = "selfie_does_not_match_the_signed_punch"
ERR_BAD_SIGNATURE = "bad_signature"
ERR_MISSING_SIGNATURE = "missing_signature"
ERR_BAD_ANCHOR = "bad_anchor"
ERR_DUPLICATE = "duplicate_punch"
ERR_REPLAYED_NONCE = "replayed_nonce"
ERR_TOO_OLD = "punch_too_old"
ERR_IN_FUTURE = "punch_in_future"
ERR_CLOCK_TAMPERED = "clock_tampered"
ERR_INVALID_ACTION = "invalid_action"
ERR_ALREADY_IN = "already_clocked_in"
ERR_NO_SESSION = "no_open_session"
ERR_BAD_OFFSET = "invalid_monotonic_offset"
ERR_ADMIN_REJECTED = "admin_rejected"

_TS = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# crypto primitives
# ---------------------------------------------------------------------------
def _server_secret() -> bytes:
    return str(settings.secret_key).encode("utf-8")


def device_key_bytes(worker_id: str, device_id: str, key_epoch: int, key_salt: str) -> bytes:
    """Derive the per-device punch-signing key. Nothing secret is stored server-side."""
    message = f"device|v1|{worker_id}|{device_id}|{int(key_epoch)}|{key_salt}".encode("utf-8")
    return hmac.new(_server_secret(), message, hashlib.sha256).digest()


def device_key_token(worker_id: str, device_id: str, key_epoch: int, key_salt: str) -> str:
    """URL-safe form handed to the client exactly once, at registration."""
    return base64.urlsafe_b64encode(device_key_bytes(worker_id, device_id, key_epoch, key_salt)).decode("ascii")


def _b64_to_bytes(key: bytes | str) -> bytes:
    if isinstance(key, bytes):
        return key
    return base64.urlsafe_b64decode(key)


def _fmt_coord(value: float | None) -> str:
    return f"{float(value):.6f}" if value is not None else ""


def _fmt_accuracy(value: float | None) -> str:
    return f"{float(value):.1f}" if value is not None else ""


def canonical_punch(
    *,
    device_id: str,
    worker_id: str,
    action: str,
    client_punch_id: str,
    nonce: str,
    anchor_id: str,
    effective_timestamp: str,
    lat: float | None,
    lon: float | None,
    accuracy: float | None,
    photo_sha256: str | None,
) -> str:
    """The exact bytes that are signed. Changing this breaks every enrolled device.

    Newline separated, version tag first, positional to the end of the line. An empty
    field is the empty string - never ``null`` or a literal ``"None"`` - because two
    implementations must produce identical bytes or no signature will verify.
    """
    return "\n".join(
        [
            f"v{SIGNATURE_VERSION}",
            device_id,
            worker_id,
            action,
            client_punch_id,
            nonce,
            anchor_id,
            effective_timestamp,
            _fmt_coord(lat),
            _fmt_coord(lon),
            _fmt_accuracy(accuracy),
            photo_sha256 or "",
        ]
    )


def sign_punch(key: bytes | str, **fields: Any) -> str:
    """Reference implementation: hex HMAC-SHA256 over ``canonical_punch``."""
    return hmac.new(_b64_to_bytes(key), canonical_punch(**fields).encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_anchor(*, device_id: str, worker_id: str, server_time: str, anchor_id: str) -> str:
    return "\n".join([f"anchor|v{SIGNATURE_VERSION}", device_id, worker_id, server_time, anchor_id])


def sign_anchor(**fields: Any) -> str:
    """Sign an anchor with the **server** secret, never with the device key."""
    return hmac.new(_server_secret(), canonical_anchor(**fields).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_anchor_signature(*, device_id: str, worker_id: str, server_time: str, anchor_id: str, signature: str) -> bool:
    expected = sign_anchor(
        device_id=device_id, worker_id=worker_id, server_time=server_time, anchor_id=anchor_id
    )
    return hmac.compare_digest(expected, str(signature or "").lower())


def parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in (_TS, f"{_TS}.%f"):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


def effective_timestamp(anchor_server_time: str, monotonic_offset_s: float) -> datetime:
    """Authoritative punch time: a server anchor plus monotonic elapsed time."""
    base = parse_ts(anchor_server_time)
    if base is None:
        raise ValueError("anchor_server_time is not a readable timestamp")
    return base + timedelta(seconds=float(monotonic_offset_s))


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class DeviceRef(BaseModel):
    device_id: str | None = None
    #: Why the device is being registered, revoked or renamed, for the administrator's own
    #: records. Free text, prose profile.
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(
            value, field="Note", max_length=textguard.MAX_LABEL, allow_empty=True
        )


class OfflinePunch(BaseModel):
    client_punch_id: str
    action: str
    anchor_id: str
    anchor_server_time: str
    anchor_signature: str
    monotonic_offset_s: float
    nonce: str
    lat: float | None = None
    lon: float | None = None
    accuracy: float | None = None
    client_timestamp: str | None = None
    client_offset_s: int | None = None
    photo_sha256: str | None = None
    signature: str | None = None
    signature_version: int = SIGNATURE_VERSION


class SyncRequest(BaseModel):
    device_id: str
    punches: list[OfflinePunch] = Field(default_factory=list)


class ResolveRequest(BaseModel):
    decision: str = "approve"
    #: The administrator's reason for approving or refusing a queued punch. This note is
    #: written into ``attendance_logs.flag_reason`` (or the rejection row) and from there
    #: into the punch-queue view and the offline report - the two places an operator reads
    #: this text back.
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(value, field="Note", max_length=textguard.MAX_NOTE, allow_empty=True)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None,
    entity: str,
    entity_id: str,
    before: Any = None,
    after: Any = None,
    request: Request | None = None,
) -> None:
    try:
        ip = request.client.host if request is not None and request.client else None
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, before_json, after_json, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor.id if actor else None,
                actor.role if actor else "system",
                action,
                entity,
                str(entity_id),
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                ip,
                datetime.now().strftime(_TS),
            ),
        )
    except sqlite3.Error:
        pass


def _device_lookup(conn: sqlite3.Connection, worker_id: str, device_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM worker_devices WHERE worker_id = ? AND device_id = ?", (worker_id, device_id)
    ).fetchone()


def _rules(conn: sqlite3.Connection) -> dict:
    values = dict(migrations.DEFAULT_SHIFT_RULES)
    try:
        row = conn.execute("SELECT * FROM shift_rules WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        for key in values:
            try:
                if row[key] is not None:
                    values[key] = row[key]
            except (IndexError, KeyError):
                continue
    return values


def _number(values: dict, key: str, default: float) -> float:
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return default


def _reason(*parts: str | None) -> str | None:
    """Join recorded reasons with the separator the rest of the app uses.

    ``main.py`` composes ``flag_reason`` with ``" | ".join(...)`` at every one of its own
    write sites, and a reader of the column cannot tell which module wrote a row. Empty and
    ``None`` parts are dropped rather than joined, so a row with nothing to say about itself
    records ``NULL`` instead of a bare separator.
    """
    text = " | ".join(str(part).strip() for part in parts if part and str(part).strip())
    return text or None


def _within_clock_in_window(window: object, moment: datetime) -> bool:
    """Whether the *effective* time is inside the window, overnight ones included.

    Delegates to ``shift_windows``, which is the one implementation of this rule: a phone that
    was offline at 23:15 and synced in the morning has to be judged by exactly the same
    predicate as a punch taken online, or the same arrival is late on one path and on time on
    the other. ``window`` is a resolved ``shift_windows.Window``; a plain rules mapping still
    works and is treated as the site's own values.
    """
    return shift_windows.is_within_site_window(window, moment)


def _detect_site_row(conn: sqlite3.Connection, lat: float | None, lon: float | None):
    """The site whose geofence contains the point, as a row - or ``None``.

    A row rather than a name, because the caller needs the site's own shift window as well as
    its name, and a punch replayed from the queue hours later must be measured against the
    shift that was in force where the phone was, not against the global default.
    """
    if lat is None or lon is None:
        return None
    from main import get_distance_meters  # local import: main imports this module

    for site in conn.execute("SELECT * FROM construction_sites").fetchall():
        try:
            if get_distance_meters(site["lat"], site["lon"], float(lat), float(lon)) <= float(site["radius"]):
                return site
        except (TypeError, ValueError):
            continue
    return None


def _insert_log(conn: sqlite3.Connection, **kwargs: Any) -> int:
    cursor = conn.execute(
        """
        INSERT INTO attendance_logs
            (worker_id, site_name, action, timestamp, hours, score, status, status_code,
             lat, lon, accuracy, source, liveness_class, flag_reason, overtime_hours,
             client_timestamp, device_id, request_id, break_hours)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kwargs["worker_id"],
            kwargs["site_name"],
            kwargs["action"],
            kwargs["timestamp"],
            kwargs["hours"],
            kwargs.get("score", OFFLINE_SCORE),
            kwargs["status"],
            kwargs["status_code"],
            kwargs.get("lat"),
            kwargs.get("lon"),
            kwargs.get("accuracy"),
            "offline",
            kwargs.get("liveness_class", LIVENESS_OFFLINE),
            kwargs.get("flag_reason"),
            kwargs.get("overtime_hours"),
            kwargs.get("client_timestamp"),
            kwargs.get("device_id"),
            kwargs.get("request_id"),
            kwargs.get("break_hours"),
        ),
    )
    return int(cursor.lastrowid or 0)


def _effective_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"datetime({prefix}anchor_server_time, '+' || {prefix}monotonic_offset_s || ' seconds')"
    )


def _existing_nonce(conn: sqlite3.Connection, device_id: str, nonce: str) -> bool:
    return (
        conn.execute(
            "SELECT id FROM punch_queue WHERE device_id = ? AND nonce = ? LIMIT 1", (device_id, nonce)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def verify_punch(
    conn: sqlite3.Connection, *, worker_id: str, device: sqlite3.Row, punch: OfflinePunch, now: datetime
) -> tuple[dict, str | None]:
    """Verify one punch. Returns ``(row_parameters, rejection_code)``.

    Order is deliberate: cheap lookups and replay checks first, then cryptography, then
    time policy - so a known-duplicate cannot force HMAC work.
    """
    device_id = str(device["device_id"])
    max_age = timedelta(hours=float(settings.offline_punch_max_age_hours))
    skew = timedelta(seconds=float(settings.offline_punch_clock_skew_seconds))
    window = timedelta(seconds=float(settings.offline_duplicate_window_seconds))
    rules = _rules(conn)

    if punch.action not in (ACTION_CLOCK_IN, ACTION_CLOCK_OUT):
        return {}, ERR_INVALID_ACTION
    if not punch.client_punch_id or not punch.nonce or not punch.anchor_id:
        return {}, ERR_BAD_ANCHOR

    if conn.execute(
        "SELECT id FROM punch_queue WHERE client_punch_id = ?", (punch.client_punch_id,)
    ).fetchone():
        return {}, ERR_DUPLICATE
    if _existing_nonce(conn, device_id, punch.nonce):
        return {}, ERR_REPLAYED_NONCE

    anchor = conn.execute(
        "SELECT * FROM device_anchors WHERE anchor_id = ?", (punch.anchor_id,)
    ).fetchone()
    if anchor is None or str(anchor["device_id"]) != device_id or str(anchor["worker_id"]) != worker_id:
        return {}, ERR_BAD_ANCHOR

    anchor_time = parse_ts(anchor["server_time"])
    if anchor_time is None:
        return {}, ERR_BAD_ANCHOR
    if not verify_anchor_signature(
        device_id=device_id,
        worker_id=worker_id,
        server_time=anchor_time.strftime(_TS),
        anchor_id=str(anchor["anchor_id"]),
        signature=punch.anchor_signature,
    ):
        return {}, ERR_BAD_ANCHOR
    if str(punch.anchor_server_time) != anchor_time.strftime(_TS):
        # The client may not restate the anchor time; it must match the recorded row.
        return {}, ERR_BAD_ANCHOR

    try:
        offset = float(punch.monotonic_offset_s)
        effective = effective_timestamp(anchor_time.strftime(_TS), offset)
    except (TypeError, ValueError):
        return {}, ERR_BAD_ANCHOR

    if offset < 0 or timedelta(seconds=offset) > max_age:
        return {}, ERR_BAD_OFFSET
    if effective - now > skew:
        return {}, ERR_IN_FUTURE
    if now - effective > max_age:
        return {}, ERR_TOO_OLD

    if settings.offline_signature_required and not punch.signature:
        return {}, ERR_MISSING_SIGNATURE
    if punch.signature:
        key = device_key_bytes(worker_id, device_id, int(device["key_epoch"]), str(device["key_salt"]))
        expected = sign_punch(
            key,
            device_id=device_id,
            worker_id=worker_id,
            action=punch.action,
            client_punch_id=punch.client_punch_id,
            nonce=punch.nonce,
            anchor_id=str(anchor["anchor_id"]),
            effective_timestamp=effective.strftime(_TS),
            lat=punch.lat,
            lon=punch.lon,
            accuracy=punch.accuracy,
            photo_sha256=punch.photo_sha256,
        )
        if not hmac.compare_digest(expected, str(punch.signature).strip().lower()):
            return {}, ERR_BAD_SIGNATURE

    # A wall clock that disagrees with the anchored time by more than the allowance is
    # the signature of a deliberate clock change. The anchored time is still sound, but
    # the punch is refused rather than silently relocated - the worker can raise it with
    # an administrator, who will see the disagreement in the triage view.
    if (
        settings.offline_signature_required
        and punch.client_offset_s is not None
        and abs(int(punch.client_offset_s)) > int(settings.offline_punch_clock_skew_seconds)
    ):
        return {}, ERR_CLOCK_TAMPERED

    if conn.execute(
        f"SELECT id FROM punch_queue WHERE worker_id = ? AND action = ? AND status != 'rejected' "
        f"AND abs(strftime('%s', {_effective_sql()}) - strftime('%s', ?)) <= ?",
        (worker_id, punch.action, effective.strftime(_TS), int(window.total_seconds())),
    ).fetchone():
        return {}, ERR_DUPLICATE

    site_row = _detect_site_row(conn, punch.lat, punch.lon)
    site = site_row["site_name"] if site_row is not None else None
    # No site matched (a punch outside every geofence - already flagged as such) means there is
    # no site window to apply: the global rules are the only thing left, which is what
    # ``effective_window`` falls back to with a ``None`` row.
    site_window = shift_windows.effective_window(site_row, rules)
    flagged = site is None
    return (
        {
            "worker_id": worker_id,
            "device_id": device_id,
            "action": punch.action,
            "client_punch_id": punch.client_punch_id,
            "nonce": punch.nonce,
            "anchor_id": str(anchor["anchor_id"]),
            "anchor_server_time": anchor_time.strftime(_TS),
            "monotonic_offset_s": offset,
            "client_timestamp": punch.client_timestamp,
            "client_offset_s": punch.client_offset_s,
            "lat": punch.lat,
            "lon": punch.lon,
            "accuracy": punch.accuracy,
            "photo_sha256": punch.photo_sha256,
            "signature": punch.signature or "",
            "signature_version": int(punch.signature_version or SIGNATURE_VERSION),
            "site": site,
            "status": STATUS_FLAGGED if flagged else STATUS_ACCEPTED,
            "flag_reason": (
                f"offline punch outside every geofence (lat={punch.lat}, lon={punch.lon})" if flagged else None
            ),
            "late_flag": (
                None
                if punch.action != ACTION_CLOCK_IN
                else (
                    None
                    if _within_clock_in_window(site_window, effective)
                    else shift_windows.describe(site_window)
                )
            ),
            "effective_time": effective.strftime(_TS),
        },
        None,
    )


def _store_punch(
    conn: sqlite3.Connection, *, params: dict, rejected_code: str | None, now: datetime
) -> int:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO punch_queue
            (client_punch_id, device_id, worker_id, action, client_timestamp, client_offset_s,
             anchor_id, anchor_server_time, monotonic_offset_s, nonce, lat, lon, accuracy,
             photo_sha256, signature, signature_version, received_at, status, rejection_code,
             flag_reason, site_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            params.get("client_punch_id"),
            params.get("device_id"),
            params.get("worker_id"),
            params.get("action"),
            params.get("client_timestamp"),
            params.get("client_offset_s"),
            params.get("anchor_id"),
            params.get("anchor_server_time"),
            float(params.get("monotonic_offset_s") or 0.0),
            params.get("nonce"),
            params.get("lat"),
            params.get("lon"),
            params.get("accuracy"),
            params.get("photo_sha256"),
            params.get("signature") or "",
            int(params.get("signature_version") or SIGNATURE_VERSION),
            now.strftime(_TS),
            STATUS_REJECTED if rejected_code else params.get("status", STATUS_ACCEPTED),
            rejected_code,
            params.get("flag_reason"),
            params.get("site"),
        ),
    )
    return int(cursor.lastrowid or 0)


def _rejected_params(punch: OfflinePunch, *, worker_id: str, device_id: str) -> dict:
    """Minimal row for a punch refused before verification completed."""
    return {
        "client_punch_id": punch.client_punch_id,
        "device_id": device_id,
        "worker_id": worker_id,
        "action": punch.action,
        "client_timestamp": punch.client_timestamp,
        "client_offset_s": punch.client_offset_s,
        "anchor_id": punch.anchor_id,
        "anchor_server_time": punch.anchor_server_time,
        "monotonic_offset_s": float(punch.monotonic_offset_s or 0.0),
        "nonce": punch.nonce,
        "lat": punch.lat,
        "lon": punch.lon,
        "accuracy": punch.accuracy,
        "photo_sha256": punch.photo_sha256,
        "signature": punch.signature or "",
        "signature_version": int(punch.signature_version or SIGNATURE_VERSION),
    }


# ---------------------------------------------------------------------------
# materialization
# ---------------------------------------------------------------------------
_PENDING_SQL = """
    SELECT *, {effective} AS effective_time
    FROM punch_queue
    WHERE worker_id = ? AND status IN ('accepted', 'flagged') AND materialized_log_id IS NULL
    ORDER BY effective_time ASC, id ASC
"""


def materialize_worker(conn: sqlite3.Connection, worker_id: str) -> dict:
    """Turn verified queue rows into ``active_sessions`` / ``attendance_logs``.

    Applied in **effective-time order** because one batch routinely contains both a
    clock-in and a clock-out: applying them in arrival order would compute hours
    against the wrong session.
    """
    rules = _rules(conn)
    # The overtime line is not resolved here: ``shift_hours.overtime_assessment`` decides
    # what it counts, where it sits and how much of the shift it holds back, for this path
    # exactly as for the online ones.
    cutoff = _number(rules, "hard_cutoff_hours", 11.0)
    now_str = datetime.now().strftime(_TS)

    applied: list[dict] = []
    #: The worker's crossing notices, one per long shift in this batch. Returned rather than
    #: pushed here: a push is a third-party call and this runs inside the caller's write
    #: transaction - and the row is invisible to the dispatcher until that commits.
    crossings: list[dict] = []
    rows = conn.execute(_PENDING_SQL.format(effective=_effective_sql()), (worker_id,)).fetchall()
    for row in rows:
        effective = parse_ts(row["effective_time"])
        if effective is None:
            conn.execute(
                "UPDATE punch_queue SET status = 'rejected', rejection_code = ?, processed_at = ? WHERE id = ?",
                (ERR_BAD_ANCHOR, now_str, row["id"]),
            )
            continue

        session = conn.execute(
            "SELECT site_name, clock_in_time FROM active_sessions WHERE worker_id = ?", (worker_id,)
        ).fetchone()

        if row["action"] == ACTION_CLOCK_IN:
            if session is not None:
                conn.execute(
                    "UPDATE punch_queue SET status = 'rejected', rejection_code = ?, processed_at = ? WHERE id = ?",
                    (ERR_ALREADY_IN, now_str, row["id"]),
                )
                applied.append({"id": row["id"], "action": row["action"], "status": "rejected", "code": ERR_ALREADY_IN})
                continue
            site = row["site_name"] or SITE_UNASSIGNED
            reason = _reason(row["flag_reason"], OFFLINE_REVIEW_REASON)
            conn.execute(
                "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source, late_flag, liveness_class) "
                "VALUES (?, ?, ?, 'offline', ?, ?)",
                (worker_id, site, row["effective_time"], row["flag_reason"], LIVENESS_OFFLINE),
            )
            # The arrival is not approved either - but it is not put in the review queue, and
            # that is a deliberate difference. It carries no hours, so there is no money for a
            # reviewer to decide; and a ``pending_review`` row blocks this worker's next
            # clock-out, online or through a quick link. An arrival held that way would leave
            # somebody who came in during a dead spot unable to close their shift at all, which
            # is the exact opposite of what the hold is for. So it records the same provenance
            # and says what it is: unverified.
            log_id = _insert_log(
                conn,
                worker_id=worker_id,
                site_name=site,
                action=ACTION_CLOCK_IN,
                timestamp=row["effective_time"],
                hours=0.0,
                status=STATUS_LABEL_UNVERIFIED_OFFLINE,
                status_code=STATUS_CODE_UNVERIFIED_OFFLINE,
                lat=row["lat"],
                lon=row["lon"],
                accuracy=row["accuracy"],
                flag_reason=reason,
                client_timestamp=row["client_timestamp"],
                device_id=row["device_id"],
                request_id=row["client_punch_id"],
            )
        else:
            if session is None:
                conn.execute(
                    "UPDATE punch_queue SET status = 'rejected', rejection_code = ?, processed_at = ? WHERE id = ?",
                    (ERR_NO_SESSION, now_str, row["id"]),
                )
                applied.append({"id": row["id"], "action": row["action"], "status": "rejected", "code": ERR_NO_SESSION})
                continue

            clock_in = parse_ts(session["clock_in_time"]) or effective
            seconds_on_site = shift_hours.elapsed_seconds(clock_in, effective)
            elapsed = round(seconds_on_site / 3600.0, 4)
            # A punch that arrives hours later still describes the same shift, so it is
            # paid by the same rule as a shift closed online - the unpaid break and the
            # last-few-minutes rounding included. Anything else would make the money
            # depend on whether the phone had a signal, which is not something the worker
            # controls. There is no confirmation here: this punch was taken before anyone
            # could be asked, and the alternative is losing it entirely.
            record = shift_hours.recorded_shift(elapsed, rules)
            hours = record["paid_hours"]
            break_taken = record["break_hours"]
            # Provenance first, then whatever the shift itself turned out to be: a reader of
            # the queue learns what the row is before why it is a problem. The punch's own
            # reason (a geofence miss, say) is kept beside them rather than overwritten - it
            # used to be replaced by the long-shift sentence below, which is one finding
            # silently deleting another.
            flag_reason = _reason(OFFLINE_REVIEW_REASON, row["flag_reason"])
            # Deliberately NOT capped at ``hard_cutoff_hours``.
            #
            # This used to rewrite anything past 11 h as 11 h. The online path force-closed
            # a shift at 11 h and this path flattened it, so both told the same lie: a worker
            # who really worked 13 h was recorded as 11, and the missing 2 h stayed invisible
            # until they asked (see "Why there is no hard cutoff" in ``main.py``). A long
            # offline shift keeps its real hours and is flagged for a human instead - it is
            # routed to ``pending_overtime`` below, which is where an admin decides, and an
            # offline punch is exactly the case where that check earns its keep.
            if elapsed > cutoff:
                flag_reason = _reason(
                    flag_reason,
                    f"offline clock-out claims {elapsed:.2f}h on site, past the {cutoff:g}h review "
                    "mark; hours recorded as punched, verify before approving",
                )
            conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))

            #: Named in full rather than ``overtime``: the module of that name announces the
            #: crossing below, and a local shadowing it breaks the call that matters.
            overtime_hours = None
            # Nothing over the line, and no overtime to hold: still a review, because this
            # punch was materialised rather than confirmed.
            status_code = STATUS_CODE_PENDING_REVIEW
            status_val = STATUS_LABEL_PENDING_REVIEW
            # The two questions are separate, so they are answered separately: the *punch* is
            # still unverified and needs a human whatever the money says, while the *hours*
            # settle at the ceiling a mid-shift decision stated. A punch that reaches the
            # server hours late is the one case where nobody is watching the crossing queue
            # any more, so asking the same question a second time is how an answer gets lost.
            # No decision returns the assessment unchanged.
            assessment = overtime.apply_authorisation(
                conn,
                worker_id,
                session["clock_in_time"],
                shift_hours.overtime_assessment(seconds_on_site, rules),
                rules,
            )
            if assessment["needs_approval"]:
                overtime_hours = assessment["overtime_hours"]
                status_code = STATUS_CODE_PENDING_OVERTIME
                status_val = STATUS_LABEL_PENDING_OVERTIME
                flag_reason = _reason(flag_reason, assessment["flag_sentence"])
                notifications.notify(
                    conn,
                    kind=notifications.KIND_REVIEW_PENDING,
                    severity=notifications.SEVERITY_WARNING,
                    title="Offline clock-out needs overtime approval",
                    body=(
                        f"Worker {worker_id} logged {assessment['paid_hours']:.2f}h paid from an "
                        f"offline punch at '{session['site_name']}', past the "
                        f"{assessment['threshold_hours']:g}h overtime line with "
                        f"{overtime_hours:.2f}h past the paid day. Approve or adjust the extra hours "
                        "before payroll."
                    ),
                    worker_id=worker_id,
                    site_name=session["site_name"],
                    payload={
                        "hours": hours,
                        "break_hours": break_taken,
                        "overtime_hours": overtime_hours,
                        "basis": assessment["basis"],
                        "paid_hours": assessment["paid_hours"],
                        "threshold_hours": assessment["threshold_hours"],
                        "source": "offline",
                    },
                    dedupe_key=f"offline_overtime:{worker_id}:{row['client_punch_id']}",
                )
                # ... and the worker is told, exactly as if they had been online when the
                # shift ended: the announcement is a function of the shift, not of whether
                # the phone had a signal at the time. ``moment`` is the punch's own effective
                # time, so the crossing the worker reads is the one they lived.
                crossings.append(
                    overtime.announce_crossing(
                        conn,
                        worker_id=worker_id,
                        site_name=session["site_name"],
                        clock_in_time=session["clock_in_time"],
                        values=rules,
                        moment=effective,
                    )
                )
            log_id = _insert_log(
                conn,
                worker_id=worker_id,
                site_name=session["site_name"],
                action=ACTION_CLOCK_OUT,
                timestamp=row["effective_time"],
                hours=hours,
                status=status_val,
                status_code=status_code,
                lat=row["lat"],
                lon=row["lon"],
                accuracy=row["accuracy"],
                flag_reason=flag_reason,
                overtime_hours=overtime_hours,
                break_hours=break_taken or None,
                client_timestamp=row["client_timestamp"],
                device_id=row["device_id"],
                request_id=row["client_punch_id"],
            )
            # Settled, so the decision behind it is spent by the row that settled it: the same
            # closing, hours later, must not leave a live answer for the next shift. A clock-in
            # has nothing to spend - only an ending settles a decision.
            overtime.consume_authorisation(conn, worker_id, session["clock_in_time"], log_id)
            if status_code == STATUS_CODE_PENDING_REVIEW:
                # One notification per offline shift, and it is sent for exactly the shift
                # whose hold has no other call to action: the overtime branch above already
                # told an administrator what to do about this one. Two notices for one shift
                # is how a review queue stops being read. The clock-in half sends none - the
                # queue lists both ends of the shift, and one row is the shift's.
                notifications.notify(
                    conn,
                    kind=notifications.KIND_REVIEW_PENDING,
                    severity=notifications.SEVERITY_WARNING,
                    title="Offline clock-out needs review",
                    body=(
                        f"Worker {worker_id} recorded {hours:.2f}h paid at "
                        f"'{session['site_name']}' from a punch taken with no signal. Nothing "
                        "about that punch was checked when it was taken - the selfie it kept is "
                        "scored here when it arrives - so the hours are held until an "
                        "administrator approves or adjusts them."
                    ),
                    worker_id=worker_id,
                    site_name=session["site_name"],
                    log_id=log_id,
                    payload={
                        "hours": hours,
                        "break_hours": break_taken,
                        "source": "offline",
                        "client_punch_id": row["client_punch_id"],
                    },
                    dedupe_key=f"offline_review:{worker_id}:{row['client_punch_id']}",
                )

        conn.execute(
            "UPDATE punch_queue SET materialized_log_id = ?, processed_at = ?, location_trusted = ? WHERE id = ?",
            (log_id, now_str, 1 if row["site_name"] else 0, row["id"]),
        )
        applied.append(
            {
                "id": row["id"],
                "action": row["action"],
                "status": "materialized",
                "log_id": log_id,
                "effective_time": row["effective_time"],
                "site": row["site_name"] or SITE_UNASSIGNED,
                "flagged": bool(row["flag_reason"]),
            }
        )
    return {"applied": applied, "count": len(applied), "crossings": crossings}


# ---------------------------------------------------------------------------
# the queued selfie, scored when it reaches the server
# ---------------------------------------------------------------------------
#: ``face_detector``'s verdicts, as the sentence a reviewer reads on the review row. The
#: three names are the band's own vocabulary - this maps them to words, it does not decide
#: anything about them.
_MATCH_PHRASES = {
    face_detector.MATCH_APPROVED: "inside the approval band",
    face_detector.MATCH_REVIEW: "inside the review band",
    face_detector.MATCH_REFUSED: "outside the approval band",
}


def _frame_arrays(blob: bytes) -> tuple[np.ndarray, np.ndarray]:
    """``(rgb, bgr)`` for one selfie: the same ceiling and channel order as a live punch.

    ``main.py`` thumbnails to 640 px before either model sees the frame, and hands the
    liveness model RGB while DeepFace expects BGR. A scoring path that skipped either would
    print a number beside the online one that was measured on different pixels.
    """
    image = uploads.decode_photo(blob, field="selfie")
    image.thumbnail((640, 640))
    rgb = np.array(image)
    return rgb, rgb[:, :, ::-1]


def _selfie_sentence(decision, distance: float | None, verdict: str | None, error: str | None) -> str:
    """One clause per check, in the order they run, for the row's ``flag_reason``."""
    result = decision.result
    probability = result.genuine_prob if result.is_live else result.confidence
    detail = f" - {result.detail}" if result.detail and not result.is_live else ""
    liveness_part = f"liveness '{result.verdict}'"
    if probability is not None:
        liveness_part += f" ({probability:.2f})"
    liveness_part += detail

    if distance is not None:
        match_part = f"face match {distance:.2f}"
        if verdict is not None:
            match_part += f" ({_MATCH_PHRASES[verdict]})"
    elif error:
        match_part = f"no face match made ({error.rstrip('.')})"
    else:
        match_part = "no face match was attempted"
    return f"sync selfie: {liveness_part}, {match_part}"


def _selfie_attention(scored: dict) -> dict | None:
    """What the evidence did *not* confirm, or ``None`` when both checks came back clean.

    Returns the notification kind and severity, the telemetry label, and the phrase the
    sentence reads. The labels are the ones the online punch already observes, so one
    dashboard counts a spoof whatever path it arrived by.
    """
    result = scored["liveness"].result
    if result.verdict in {liveness.VERDICT_SPOOF, liveness.VERDICT_LOW_CONFIDENCE}:
        return {
            "kind": notifications.KIND_LIVENESS_SPOOF,
            "severity": notifications.SEVERITY_CRITICAL,
            "telemetry": "liveness_spoof",
            "phrase": f"the liveness check called this frame '{result.verdict}'",
        }
    if result.verdict == liveness.VERDICT_LIVE and scored["match_verdict"] == face_detector.MATCH_APPROVED:
        return None
    if scored["match_verdict"] is None:
        # No distance came back: the comparison could not run at all (no face in the frame, a
        # stale template, no template). "Could not be made" and "was made and refused" are
        # different findings, which is why they are different labels here too.
        return {
            "kind": notifications.KIND_LIVENESS_DEGRADED,
            "severity": notifications.SEVERITY_WARNING,
            "telemetry": "frame_refused",
            "phrase": f"the face could not be compared ({scored['match_error'] or 'unknown reason'})",
        }
    if scored["match_verdict"] == face_detector.MATCH_REFUSED:
        return {
            "kind": notifications.KIND_LIVENESS_DEGRADED,
            "severity": notifications.SEVERITY_WARNING,
            "telemetry": "rejected",
            "phrase": (
                f"the face matched at distance {scored['distance']:.2f}, "
                f"{_MATCH_PHRASES[face_detector.MATCH_REFUSED]}"
            ),
        }
    # The match landed inside an acceptable band and the frame still was not confirmed, so
    # liveness is the finding - it is the only condition left. (The first branch took the two
    # verdicts that mean an attack; this is everything else that is not ``live``.)
    return {
        "kind": notifications.KIND_LIVENESS_DEGRADED,
        "severity": notifications.SEVERITY_WARNING,
        "telemetry": "flagged_review",
        "phrase": f"the liveness check reported '{result.verdict}'",
    }


async def score_queued_selfie(worker_id: str, blob: bytes) -> dict:
    """Run the online punch's two checks over a queued selfie, and report what they said.

    The comparison itself is ``main.compare_faces_sync`` - imported late, because ``main``
    imports this module - so there is one implementation of the match, its multi-face guard
    and its template refusals. What is *not* shared is the policy, and it cannot be: an
    online punch may refuse the worker, who is standing there and can take another photo,
    while an offline one has already happened. So a finding here is recorded for the reviewer
    and never turned into a rejection - this function returns evidence and writes nothing.

    Raises ``face_engine.FaceEngineBusy`` when the engine has no capacity. That is the server
    being unable to answer rather than an answer, so the caller passes it on and the phone
    keeps its copy: the same distinction the online punch makes, and the reason no verdict is
    recorded in that case.
    """
    from main import compare_faces_sync

    rgb, bgr = _frame_arrays(blob)
    decision = await face_engine.ENGINE.run_async(liveness.inspect, rgb)
    liveness_class, liveness_score = decision.log_fields()

    distance: float | None = None
    match_verdict: str | None = None
    match_error: str | None = None
    if not decision.allowed:
        # A frame the liveness check refused is not embedded: a presentation attack must not
        # be able to make the server spend half a second of VGG-Face, which is what the
        # online path avoids too.
        match_error = "the liveness check refused this frame, so it was not compared"
    else:
        with db() as conn:
            user = conn.execute(
                "SELECT id, biometric_id FROM users WHERE id = ?", (worker_id,)
            ).fetchone()
        reference = biometrics.resolve_reference(worker_id, biometrics.id_from(user)) if user else ""
        if user is None or not os.path.exists(reference):
            match_error = "no facial reference is enrolled for this account"
        else:
            face_data = await face_engine.ENGINE.run_async(compare_faces_sync, reference, bgr)
            match_error = face_data.get("error") or None
            if not match_error:
                distance = float(face_data["distance"])
                try:
                    # Re-derived from the live pipeline rather than read off ``face_data``, for
                    # the reason the online punch re-derives it: a caller may stub the
                    # comparison itself, and a verdict path that only works when a particular
                    # payload shape came back is a verdict path that stops being tested.
                    match_verdict = face_detector.band_for(biometrics.current_pipeline()).classify(distance)
                except face_detector.UnknownPipelineError:
                    match_verdict = None

    return {
        "liveness": decision,
        "liveness_class": liveness_class,
        "liveness_score": liveness_score,
        "distance": distance,
        "match_verdict": match_verdict,
        "match_error": match_error,
        "sentence": _selfie_sentence(decision, distance, match_verdict, match_error),
    }


# ---------------------------------------------------------------------------
# worker endpoints
# ---------------------------------------------------------------------------
@router.post("/sync/photo")
@limiter.limit(settings.attendance_rate_limit)
async def upload_sync_photo(
    request: Request,
    client_punch_id: str = Form(...),
    photo: UploadFile = File(...),
    current: CurrentUser = Depends(any_authenticated),
):
    """Score the selfie a queued punch kept on the phone, onto that punch's review row.

    WHY THIS IS ITS OWN REQUEST, NOT A FIELD IN THE BATCH
    -----------------------------------------------------
    The photo could have ridden inside ``POST /attendance/sync`` as base64. It deliberately
    does not: a JSON body is parsed *before* any handler runs, so a batch of fifty selfies
    would be held in memory in full before one byte of it could be measured - the exact
    failure ``uploads.read_photo`` exists to prevent ("a limit checked after the body has
    been read is not a limit"). Here the frame goes through that one upload policy, one
    request per photo, and the punch batch stays the small signed document it was.

    WHAT BINDS THE PHOTO TO THE PUNCH
    ---------------------------------
    ``photo_sha256`` sits inside the HMAC the device signed, and this row was written from
    that verified payload. So the bytes uploaded here must hash to it or they are not the
    frame this punch was signed against - checked before anything is decoded.

    WHAT IT DOES NOT DO
    -------------------
    It does not approve, refuse, re-price or re-materialise anything. The punch is already in
    the review queue; this writes the score, the liveness verdict and the sentence the
    reviewer reads onto that row. Scoring can therefore change what is *known* about a shift,
    never what it is worth.
    """
    blob = await uploads.read_photo(photo, field="selfie")

    with db() as conn:
        punch = conn.execute(
            "SELECT id, worker_id, action, site_name, status, materialized_log_id, photo_sha256, "
            "photo_scored_at FROM punch_queue WHERE client_punch_id = ? AND worker_id = ?",
            (client_punch_id, current.id),
        ).fetchone()
    if punch is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": ERR_PUNCH_UNKNOWN,
                "message": "No queued punch with that id belongs to you.",
            },
        )
    if str(punch["status"]) not in (STATUS_ACCEPTED, STATUS_FLAGGED):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": ERR_PUNCH_NOT_ACCEPTED,
                "message": f"Punch {client_punch_id} was '{punch['status']}' and has no review row to score.",
            },
        )
    if not punch["photo_sha256"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": ERR_PUNCH_NO_SELFIE,
                "message": "This punch was signed without a selfie, so there is nothing to score.",
            },
        )
    if punch["materialized_log_id"] is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": ERR_PUNCH_NOT_MATERIALIZED,
                "message": "This punch has not been materialised yet; sync it first.",
            },
        )
    if hashlib.sha256(blob).hexdigest() != str(punch["photo_sha256"]).strip().lower():
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": ERR_PHOTO_MISMATCH,
                "message": (
                    "This photo is not the one this punch was signed against, so it cannot be "
                    "used as evidence for it."
                ),
            },
        )

    if punch["photo_scored_at"]:
        # Idempotent on purpose. A phone whose connection drops after a scoring that succeeded
        # re-uploads the same photo, and it has to be able to stop: re-running the models would
        # rewrite the same evidence and re-notify an administrator who has already been told.
        with db() as conn:
            row = conn.execute(
                "SELECT score, liveness_class, liveness_score, flag_reason FROM attendance_logs "
                "WHERE id = ?",
                (punch["materialized_log_id"],),
            ).fetchone()
        return {
            "status": "already_scored",
            "client_punch_id": client_punch_id,
            "score": row["score"] if row is not None else None,
            "liveness_class": row["liveness_class"] if row is not None else None,
            "liveness_score": row["liveness_score"] if row is not None else None,
            "materialized_log_id": punch["materialized_log_id"],
        }

    try:
        scored = await score_queued_selfie(current.id, blob)
    except face_engine.FaceEngineBusy as exc:
        # Nothing has been written and nothing is recorded as scored, so the retry is a first
        # attempt rather than a second one.
        raise face_engine.busy_http_exception(exc) from None

    attention = _selfie_attention(scored)
    now_str = datetime.now().strftime(_TS)
    # The frame the score was measured from, kept as the log row's evidence - the same copy
    # the online punch keeps (see ``punch_frames``), stored before the write transaction so a
    # decode or disk problem here cannot abort a scoring that already ran. Best-effort like
    # the online path: evidence lost to a full disk is not hours lost.
    frame_name: str | None = None
    try:
        frame_name = punch_frames.store_frame(uploads.decode_photo(blob, field="selfie"))
    except (OSError, ValueError) as exc:  # decode failure cannot be far: the models just read these bytes
        log.warning("could not store the offline punch frame for worker %s", current.id, exc_info=True)
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT flag_reason FROM attendance_logs WHERE id = ?", (punch["materialized_log_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE attendance_logs SET score = ?, liveness_class = ?, liveness_score = ?, "
            "flag_reason = ?, punch_frame = ? WHERE id = ?",
            (
                # ``score`` is NOT NULL and 0.0 is this module's "no score" sentinel, for the
                # same reason materialisation writes it: a row may never carry a number that
                # looks like a match it never had.
                scored["distance"] if scored["distance"] is not None else OFFLINE_SCORE,
                scored["liveness_class"],
                scored["liveness_score"],
                _reason(row["flag_reason"] if row is not None else None, scored["sentence"]),
                frame_name,
                punch["materialized_log_id"],
            ),
        )
        conn.execute(
            "UPDATE punch_queue SET photo_scored_at = ? WHERE id = ?", (now_str, punch["id"])
        )
        if attention is not None:
            notifications.notify(
                conn,
                kind=attention["kind"],
                severity=attention["severity"],
                title="Offline selfie did not confirm the punch",
                body=(
                    f"The selfie queued with a {str(punch['action']).lower()} by worker "
                    f"{current.id} at '{punch['site_name'] or SITE_UNASSIGNED}' was scored on "
                    f"arrival: {attention['phrase']}. The punch was already held for review - "
                    "its hours stay unpayable until an administrator decides."
                ),
                worker_id=current.id,
                site_name=punch["site_name"],
                log_id=punch["materialized_log_id"],
                payload={
                    "liveness": scored["liveness"].as_payload(),
                    "score": scored["distance"],
                    "match": scored["match_verdict"],
                    "client_punch_id": client_punch_id,
                },
                dedupe_key=f"offline_selfie:{client_punch_id}",
            )
        _audit(
            conn,
            action="offline_selfie_scored",
            actor=current,
            entity="punch_queue",
            entity_id=punch["id"],
            before={"photo_scored_at": None},
            after={
                "score": scored["distance"],
                "liveness_class": scored["liveness_class"],
                "match": scored["match_verdict"],
                "attention": attention["telemetry"] if attention else None,
            },
            request=request,
        )

    # The same vocabulary the online punch observes, so a spoof is counted once however it
    # arrived; ``approved`` here means the evidence confirmed the frame, never that the punch
    # was approved - that is still a human's, in the review queue.
    telemetry.observe_verification(attention["telemetry"] if attention else "approved")

    return {
        "status": "success",
        "client_punch_id": client_punch_id,
        "score": scored["distance"],
        "liveness_class": scored["liveness_class"],
        "liveness_score": scored["liveness_score"],
        "liveness": scored["liveness"].as_payload(),
        "match": scored["match_verdict"],
        "match_error": scored["match_error"],
        "sentence": scored["sentence"],
        "materialized_log_id": punch["materialized_log_id"],
    }


@router.post("/devices/register")
@limiter.limit(settings.attendance_rate_limit)
async def register_device(
    request: Request, payload: DeviceRef, current: CurrentUser = Depends(any_authenticated)
):
    """Register this phone/browser as a signing device and return its key **once**."""
    import secrets

    device_id = (payload.device_id or f"dev-{uuid.uuid4().hex[:24]}").strip()
    if not device_id or len(device_id) > 128:
        raise HTTPException(status_code=400, detail="device_id must be 1-128 characters.")

    now = datetime.now().strftime(_TS)
    salt = secrets.token_urlsafe(24)
    with db(write=True) as conn:
        existing = _device_lookup(conn, current.id, device_id)
        if existing is not None and existing["revoked_at"] is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "device_already_registered",
                    "message": "This device is already registered. Revoke it first to issue a new key.",
                    "device_id": device_id,
                },
            )
        if existing is not None:
            # Re-registering a revoked device: bump the epoch so any key still held by
            # the old device stops verifying from this moment.
            epoch = int(existing["key_epoch"]) + 1
            conn.execute(
                "UPDATE worker_devices SET key_salt = ?, key_epoch = ?, revoked_at = NULL, created_at = ?, "
                "last_seen_at = NULL, note = ?, registered_ip = ?, last_anchor_at = NULL WHERE id = ?",
                (salt, epoch, now, payload.note, request.client.host if request.client else None, existing["id"]),
            )
        else:
            epoch = 1
            conn.execute(
                "INSERT INTO worker_devices (device_id, worker_id, key_salt, key_epoch, created_at, note, registered_ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    current.id,
                    salt,
                    epoch,
                    now,
                    payload.note,
                    request.client.host if request.client else None,
                ),
            )
        _audit(
            conn,
            action="offline_device_register",
            actor=current,
            entity="worker_devices",
            entity_id=device_id,
            after={"key_epoch": epoch, "note": payload.note},
            request=request,
        )
        key = device_key_token(current.id, device_id, epoch, salt)

    return {
        "status": "success",
        "device_id": device_id,
        "worker_id": current.id,
        "key_epoch": epoch,
        "device_key": key,
        "signature_version": SIGNATURE_VERSION,
        "server_time": now,
        "note": "Store device_key in secure storage. It is never returned again.",
    }


@router.post("/anchors")
@limiter.limit(settings.attendance_rate_limit)
async def issue_anchor(request: Request, payload: DeviceRef, current: CurrentUser = Depends(any_authenticated)):
    """Issue a server-signed time anchor - the only way an offline punch can claim a time."""
    with db(write=True) as conn:
        devices = conn.execute(
            "SELECT * FROM worker_devices WHERE worker_id = ? AND revoked_at IS NULL ORDER BY id ASC",
            (current.id,),
        ).fetchall()
        if not devices:
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": ERR_DEVICE_UNKNOWN,
                    "message": "No active signing device. Register one first.",
                },
            )
        device = devices[0]
        if payload.device_id:
            match = next((row for row in devices if row["device_id"] == payload.device_id), None)
            if match is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error_code": ERR_DEVICE_UNKNOWN, "message": "Unknown or revoked device."},
                )
            device = match

        now = datetime.now()
        server_time = now.strftime(_TS)
        anchor_id = uuid.uuid4().hex
        signature = sign_anchor(
            device_id=str(device["device_id"]),
            worker_id=current.id,
            server_time=server_time,
            anchor_id=anchor_id,
        )
        conn.execute(
            "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at, issued_ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                anchor_id,
                device["device_id"],
                current.id,
                server_time,
                server_time,
                request.client.host if request.client else None,
            ),
        )
        conn.execute(
            "UPDATE worker_devices SET last_anchor_at = ?, last_seen_at = ? WHERE id = ?",
            (server_time, server_time, device["id"]),
        )

    return {
        "status": "success",
        "anchor_id": anchor_id,
        "device_id": device["device_id"],
        "worker_id": current.id,
        "server_time": server_time,
        "anchor_signature": signature,
        "signature_version": SIGNATURE_VERSION,
        "max_offline_hours": settings.offline_punch_max_age_hours,
    }


def _new_anchor(conn: sqlite3.Connection, device: sqlite3.Row, worker_id: str, when: datetime) -> dict:
    server_time = when.strftime(_TS)
    anchor_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO device_anchors (anchor_id, device_id, worker_id, server_time, issued_at) VALUES (?, ?, ?, ?, ?)",
        (anchor_id, device["device_id"], worker_id, server_time, server_time),
    )
    return {
        "anchor_id": anchor_id,
        "device_id": device["device_id"],
        "worker_id": worker_id,
        "server_time": server_time,
        "anchor_signature": sign_anchor(
            device_id=str(device["device_id"]), worker_id=worker_id, server_time=server_time, anchor_id=anchor_id
        ),
        "signature_version": SIGNATURE_VERSION,
    }


@router.post("/sync")
@limiter.limit(settings.attendance_rate_limit)
async def sync_punches(request: Request, payload: SyncRequest, current: CurrentUser = Depends(any_authenticated)):
    """Accept a batch of offline punches, verify each, then materialise in time order.

    Every punch gets its own verdict: a worker who reconnects after a day offline must
    not have to work out which of twelve punches the server disliked.
    """
    if not payload.punches:
        raise HTTPException(status_code=400, detail="No punches supplied.")
    if len(payload.punches) > int(settings.offline_batch_max):
        raise HTTPException(
            status_code=413, detail=f"A batch may contain at most {settings.offline_batch_max} punches."
        )

    now = datetime.now()
    results: list[dict] = []

    with db(write=True) as conn:
        device = _device_lookup(conn, current.id, payload.device_id)
        if device is None:
            raise HTTPException(
                status_code=404,
                detail={"error_code": ERR_DEVICE_UNKNOWN, "message": "Unknown device for this worker."},
            )
        if device["revoked_at"] is not None:
            raise HTTPException(
                status_code=403,
                detail={"error_code": ERR_DEVICE_REVOKED, "message": "This device has been revoked."},
            )

        for punch in payload.punches:
            params, rejection = verify_punch(conn, worker_id=current.id, device=device, punch=punch, now=now)
            if rejection in (ERR_DUPLICATE, ERR_REPLAYED_NONCE):
                results.append({"client_punch_id": punch.client_punch_id, "status": "duplicate", "code": rejection})
                continue
            if rejection:
                _store_punch(
                    conn,
                    params=_rejected_params(punch, worker_id=current.id, device_id=str(device["device_id"])),
                    rejected_code=rejection,
                    now=now,
                )
                results.append({"client_punch_id": punch.client_punch_id, "status": "rejected", "code": rejection})
                continue

            _store_punch(conn, params=params, rejected_code=None, now=now)
            conn.execute(
                "UPDATE device_anchors SET consumed_by = ? WHERE anchor_id = ? AND consumed_by IS NULL",
                (params["client_punch_id"], params["anchor_id"]),
            )
            results.append(
                {
                    "client_punch_id": params["client_punch_id"],
                    "status": params["status"],
                    "effective_time": params["effective_time"],
                    "site": params["site"],
                    "flagged": bool(params["flag_reason"]),
                }
            )

        applied = materialize_worker(conn, current.id)
        conn.execute("UPDATE worker_devices SET last_seen_at = ? WHERE id = ?", (now.strftime(_TS), device["id"]))

        problems = [row for row in results if row.get("status") in (STATUS_FLAGGED, "rejected", "duplicate")]
        if problems:
            notifications.notify(
                conn,
                kind=notifications.KIND_OFFLINE_SYNC,
                severity=notifications.SEVERITY_WARNING,
                title="Offline punches need attention",
                body=(
                    f"Worker {current.id} synchronised {len(payload.punches)} offline punch(es); "
                    f"{len(problems)} were flagged, rejected or duplicates."
                ),
                worker_id=current.id,
                payload={"results": results},
                dedupe_key=f"offline_sync:{current.id}:{now.strftime(_TS)}",
            )
        _audit(
            conn,
            action="offline_sync",
            actor=current,
            entity="punch_queue",
            entity_id=payload.device_id,
            after={"received": len(payload.punches), "applied": applied["count"], "results": results},
            request=request,
        )
        # A fresh anchor in the response: reconnecting is exactly when the client
        # should refresh its authority, and one round trip beats two.
        next_anchor = _new_anchor(conn, device, current.id, now)

    # Committed above: a materialized long shift owes the same notice the online paths give.
    overtime.deliver_worker_notices(applied["crossings"])

    return {
        "status": "success",
        "server_time": now.strftime(_TS),
        "received": len(payload.punches),
        "applied": applied["count"],
        "results": results,
        "materialized": applied["applied"],
        "next_anchor": next_anchor,
    }


@router.get("/devices")
async def list_my_devices(current: CurrentUser = Depends(any_authenticated)):
    with db() as conn:
        rows = conn.execute(
            "SELECT device_id, key_epoch, created_at, last_seen_at, revoked_at FROM worker_devices "
            "WHERE worker_id = ? ORDER BY id DESC",
            (current.id,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post("/devices/{device_id}/revoke")
async def revoke_device(request: Request, device_id: str, current: CurrentUser = Depends(any_authenticated)):
    """Revoke a signing device: bump ``key_epoch`` so its key stops verifying."""
    with db(write=True) as conn:
        device = _device_lookup(conn, current.id, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        conn.execute(
            "UPDATE worker_devices SET revoked_at = ?, key_epoch = key_epoch + 1 WHERE id = ?",
            (datetime.now().strftime(_TS), device["id"]),
        )
        _audit(
            conn,
            action="offline_device_revoke",
            actor=current,
            entity="worker_devices",
            entity_id=device_id,
            before={"key_epoch": device["key_epoch"]},
            after={"key_epoch": int(device["key_epoch"]) + 1},
            request=request,
        )
    return {"status": "success", "message": f"Device {device_id} revoked. Its key can no longer sign punches."}


# ---------------------------------------------------------------------------
# admin endpoints
# ---------------------------------------------------------------------------
@admin_router.get("/punch_queue")
async def list_punch_queue(
    status: str | None = None,
    worker_id: str | None = None,
    limit: int = 200,
    current: CurrentUser = Depends(admin_only),
):
    """Offline punch triage. Rejected punches are kept and listed, never dropped."""
    limit = max(1, min(int(limit), 1000))
    query = (
        "SELECT p.id, p.client_punch_id, p.device_id, p.worker_id, u.name AS worker_name, p.action, "
        "p.client_timestamp, p.anchor_server_time, p.monotonic_offset_s, p.client_offset_s, "
        f"{_effective_sql('p')} AS effective_time, p.lat, p.lon, p.site_name, p.status, "
        "p.rejection_code, p.flag_reason, p.received_at, p.materialized_log_id "
        "FROM punch_queue p LEFT JOIN users u ON p.worker_id = u.id WHERE 1 = 1"
    )
    params: list[Any] = []
    if status:
        query += " AND p.status = ?"
        params.append(status)
    if worker_id:
        query += " AND p.worker_id = ?"
        params.append(worker_id)
    query += " ORDER BY p.id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


@admin_router.post("/punch_queue/{punch_id}/resolve")
async def resolve_punch(
    request: Request, punch_id: int, payload: ResolveRequest, current: CurrentUser = Depends(admin_only)
):
    """Approve (materialise) or reject a flagged/rejected offline punch."""
    decision = (payload.decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'.")

    now_str = datetime.now().strftime(_TS)
    with db(write=True) as conn:
        row = conn.execute("SELECT * FROM punch_queue WHERE id = ?", (punch_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Punch not found.")

        if decision == "reject":
            conn.execute(
                "UPDATE punch_queue SET status = 'rejected', rejection_code = ?, flag_reason = ?, processed_at = ? "
                "WHERE id = ?",
                (ERR_ADMIN_REJECTED, payload.note, now_str, punch_id),
            )
            _audit(
                conn,
                action="offline_punch_reject",
                actor=current,
                entity="punch_queue",
                entity_id=punch_id,
                before={"status": row["status"], "code": row["rejection_code"]},
                after={"note": payload.note},
                request=request,
            )
            return {"status": "success", "decision": "reject", "message": f"Punch {punch_id} rejected."}

        if row["status"] not in (STATUS_FLAGGED, STATUS_REJECTED):
            raise HTTPException(
                status_code=400, detail=f"Punch {punch_id} is '{row['status']}' and cannot be approved."
            )
        conn.execute(
            "UPDATE punch_queue SET status = 'accepted', rejection_code = NULL, flag_reason = ? WHERE id = ?",
            (payload.note or row["flag_reason"], punch_id),
        )
        applied = materialize_worker(conn, row["worker_id"])
        _audit(
            conn,
            action="offline_punch_resolve",
            actor=current,
            entity="punch_queue",
            entity_id=punch_id,
            before={"status": row["status"], "code": row["rejection_code"]},
            after={"decision": "approve", "note": payload.note, "applied": applied["applied"]},
            request=request,
        )
    # Approving a punch can materialize a long shift, which is a crossing like any other.
    overtime.deliver_worker_notices(applied["crossings"])
    return {"status": "success", "decision": "approve", "applied": applied["applied"]}


@admin_router.get("/devices")
async def list_devices(current: CurrentUser = Depends(admin_only)):
    with db() as conn:
        rows = conn.execute(
            "SELECT d.id, d.device_id, d.worker_id, u.name AS worker_name, d.key_epoch, d.created_at, "
            "d.last_seen_at, d.last_anchor_at, d.revoked_at FROM worker_devices d "
            "LEFT JOIN users u ON d.worker_id = u.id ORDER BY d.id DESC LIMIT 500"
        ).fetchall()
    return [dict(row) for row in rows]
