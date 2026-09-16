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
vanishing because a phone clock was wrong.

A punch that verifies but lands outside every geofence is accepted as
``status='flagged'``, materialised with ``flag_reason``, and notified: on an offline
capture the GPS fix is the least trustworthy part, so the worker stays on record and an
administrator decides.

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
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import migrations
import notifications
import shift_hours
import shift_windows
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

#: ``attendance_logs.score`` is NOT NULL. An offline punch has no face-match score
#: because matching happened (if at all) on the client, so it is written as 0.0 with
#: the reason recorded - never as a passing score.
OFFLINE_SCORE = 0.0

LIVENESS_OFFLINE = "unverified_offline"

# ---- rejection codes -------------------------------------------------------
ERR_DEVICE_UNKNOWN = "device_unknown"
ERR_DEVICE_REVOKED = "device_revoked"
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
    note: str | None = None


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
    note: str | None = None


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
    regular = _number(rules, "regular_hours", 8.0)
    threshold = _number(rules, "overtime_notify_hours", 8.1)
    cutoff = _number(rules, "hard_cutoff_hours", 11.0)
    now_str = datetime.now().strftime(_TS)

    applied: list[dict] = []
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
            conn.execute(
                "INSERT INTO active_sessions (worker_id, site_name, clock_in_time, start_source, late_flag, liveness_class) "
                "VALUES (?, ?, ?, 'offline', ?, ?)",
                (worker_id, site, row["effective_time"], row["flag_reason"], LIVENESS_OFFLINE),
            )
            log_id = _insert_log(
                conn,
                worker_id=worker_id,
                site_name=site,
                action=ACTION_CLOCK_IN,
                timestamp=row["effective_time"],
                hours=0.0,
                status="Approved",
                status_code="approved",
                lat=row["lat"],
                lon=row["lon"],
                accuracy=row["accuracy"],
                flag_reason=row["flag_reason"],
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
            elapsed = round(max(0.0, (effective - clock_in).total_seconds() / 3600.0), 4)
            # A punch that arrives hours later still describes the same shift, so it is
            # paid by the same rule as a shift closed online - including the unpaid
            # break. Anything else would make the money depend on whether the phone had
            # a signal, which is not something the worker controls.
            hours, break_taken = shift_hours.paid_hours(elapsed, rules)
            flag_reason = row["flag_reason"]
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
                flag_reason = (
                    f"offline clock-out claims {elapsed:.2f}h on site, past the {cutoff:g}h review "
                    "mark; hours recorded as punched, verify before approving"
                )
            conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))

            overtime = None
            status_code = "approved"
            status_val = "Approved"
            if hours > threshold:
                overtime = round(hours - regular, 4)
                status_code = "pending_overtime"
                status_val = "Pending Overtime Approval"
                flag_reason = flag_reason or f"{hours:.2f}h exceeds the {threshold:g}h threshold"
                notifications.notify(
                    conn,
                    kind=notifications.KIND_REVIEW_PENDING,
                    severity=notifications.SEVERITY_WARNING,
                    title="Offline clock-out needs overtime approval",
                    body=(
                        f"Worker {worker_id} logged {hours:.2f}h from an offline punch at "
                        f"'{session['site_name']}'. Approve or adjust the extra hours before payroll."
                    ),
                    worker_id=worker_id,
                    site_name=session["site_name"],
                    payload={
                        "hours": hours,
                        "break_hours": break_taken,
                        "overtime_hours": overtime,
                        "source": "offline",
                    },
                    dedupe_key=f"offline_overtime:{worker_id}:{row['client_punch_id']}",
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
                overtime_hours=overtime,
                break_hours=break_taken or None,
                client_timestamp=row["client_timestamp"],
                device_id=row["device_id"],
                request_id=row["client_punch_id"],
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
    return {"applied": applied, "count": len(applied)}


# ---------------------------------------------------------------------------
# worker endpoints
# ---------------------------------------------------------------------------
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
