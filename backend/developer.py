"""The root tier's operations: runtime configuration, the private alert hub, diagnostics.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
One role sits above the administrators, and this module is its whole surface. The role itself
is defined in ``security`` (``DEVELOPER_ROLE``, the wildcard in ``require_role`` and the
``require_developer`` guard); what lives here is the *capability* that tier exists for, and the
concealment that keeps it off every screen an administrator can see.

Three design decisions, each of which is a security decision rather than a style one:

**The developer is a superset, and the superset is one line in ``security.require_role``.** Not
fifty widened declarations. A wildcard repeated per route goes stale the moment somebody adds a
route and forgets; a wildcard evaluated in the guard cannot. The one direction that is *not*
shared is this module's own routes: they are built from ``require_developer``, which no
administrator satisfies, so the separation holds both ways.

**Every capability here is reachable only through the API, and stored where only this module
reads it.** ``developer_alerts`` is its own table precisely so that no administrator-facing
query can leak the infrastructure alerts by forgetting a filter: a column can be forgotten, a
table nobody else names cannot. The administrator's own alert inbox
(``admin_notifications``) is untouched, and this module never reads it.

**Nothing on the hot path is instrumented with statement text.** SQLite has no
``pg_stat_statements``; what it has is ``EXPLAIN QUERY PLAN``, which can be asked on demand for
a *named* query from an allowlist. Slow statements are recorded as a verb, a duration and a
trace id - never the SQL - for the same reason ``telemetry`` only ever counts a statement's
verb: in a database layer the text can carry a worker id, and a diagnostics endpoint that
stores worker ids is a second copy of the data it exists to inspect.

The configuration store is a *distributed* store in the only sense this deployment has: one
row per key in the database every worker already shares, plus a version counter. An in-process
cache is kept beside it, and the cache is only trusted while the version it was read at is
still the version on disk - which is what makes "flip the flag, no restart, all workers agree"
true rather than aspirational. See ``runtime``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import punch_frames
from database import db, slow_queries as _slow_query_ring, connection_stats
from security import (
    DEVELOPER_ROLE,
    CurrentUser,
    hash_password,
    require_developer,
    validate_user_id_for_role,
)

logger = logging.getLogger("attendance.developer")

router = APIRouter()

# ---------------------------------------------------------------------------
# trace ids
# ---------------------------------------------------------------------------
#: The id of the request being handled, so a log line, an alert row and an audit row written
#: for the same failure can be joined by an operator who has only one of them. A ``ContextVar``
#: rather than a thread-local because the app is async: two requests share a thread and would
#: share a thread-local id.
_TRACE: ContextVar[str | None] = ContextVar("developer_trace_id", default=None)


def new_trace_id() -> str:
    """A short, sortable-enough id for one request. Not a secret, and never a credential."""
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str | None:
    return _TRACE.get()


def set_trace_id(trace_id: str | None) -> None:
    _TRACE.set(trace_id)


# ---------------------------------------------------------------------------
# runtime configuration (shared state, cached, version-checked)
# ---------------------------------------------------------------------------
FLAG = "flag"
LEVEL = "level"

#: Every key the runtime store may hold, with its type, its default and the one-line reason it
#: exists. A closed vocabulary on purpose: an operator who sets a key that nothing reads has
#: changed nothing and cannot tell, so a key that is not here is refused rather than stored.
RUNTIME_KEYS: dict[str, dict[str, Any]] = {
    "maintenance_mode": {
        "type": FLAG,
        "default": False,
        "why": "Refuse authenticated writes with 503 while the database is being worked on. "
               "Reads, sign-in and the readiness probe stay served, so a maintenance window "
               "does not look like an outage to an operator trying to check on it.",
    },
    "log_level": {
        "type": LEVEL,
        "default": "INFO",
        "why": "The root logger's level, applied to this process the moment it is set and "
               "picked up by every other worker on its next read. The reason this exists at "
               "all: an incident at 04:00 needs DEBUG now, not on the next deploy.",
    },
    "auth_anomaly_alerts": {
        "type": FLAG,
        "default": True,
        "why": "Whether repeated credential failures are raised into the alert hub. Off is a "
               "legitimate setting for a deployment behind a scanner that trips it hourly.",
    },
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: ``(version, values)`` - one cached read of the whole store, valid only while the version in
#: the database is still the version here.
_runtime_cache: tuple[int, dict[str, Any]] | None = None


class RuntimeValueError(ValueError):
    """A key or value the store will not hold."""


def _coerce(key: str, value: Any) -> Any:
    spec = RUNTIME_KEYS.get(key)
    if spec is None:
        raise RuntimeValueError(f"{key!r} is not a runtime key this build reads.")
    if spec["type"] == FLAG:
        if not isinstance(value, bool):
            raise RuntimeValueError(f"{key!r} is a flag; it takes true or false.")
        return value
    if spec["type"] == LEVEL:
        text = str(value).strip().upper()
        if text not in LOG_LEVELS:
            raise RuntimeValueError(f"{key!r} must be one of {', '.join(LOG_LEVELS)}.")
        return text
    raise RuntimeValueError(f"{key!r} has an unknown type.")  # pragma: no cover - registry is closed


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM developer_config_version WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def _read_values(conn: sqlite3.Connection) -> dict[str, Any]:
    values = {key: spec["default"] for key, spec in RUNTIME_KEYS.items()}
    try:
        rows = conn.execute("SELECT key, value_json FROM developer_config").fetchall()
    except sqlite3.Error:
        return values
    for row in rows:
        key = str(row["key"])
        if key not in RUNTIME_KEYS:  # a key from a newer build, or one since removed
            continue
        try:
            values[key] = _coerce(key, json.loads(row["value_json"]))
        except (ValueError, TypeError):
            continue  # a value that cannot be honoured falls back to the default, loudly below
    return values


def runtime_snapshot(*, refresh: bool = False) -> tuple[int, dict[str, Any]]:
    """The whole store and the version it was read at.

    One SELECT of the version row (and, when it has moved, one more for the values). The cache
    is the reason a per-request read is cheap; the version check is the reason it is *correct*
    across workers, which is the half a cache usually gets wrong.
    """
    global _runtime_cache
    if _runtime_cache is not None and not refresh:
        cached_version, cached_values = _runtime_cache
        with db() as conn:
            try:
                live = _read_version(conn)
            except sqlite3.Error:
                # The table is missing (an unmigrated database). Serve the defaults rather than
                # failing the request: the startup gate is what refuses to serve in that state.
                return cached_version, dict(cached_values)
        if live == cached_version:
            return cached_version, dict(cached_values)
    with db() as conn:
        try:
            version = _read_version(conn)
            values = _read_values(conn)
        except sqlite3.Error:
            version, values = 0, {key: spec["default"] for key, spec in RUNTIME_KEYS.items()}
    _runtime_cache = (version, values)
    return version, dict(values)


def runtime_value(key: str) -> Any:
    return runtime_snapshot()[1][key]


def set_runtime_value(
    key: str, value: Any, *, actor: CurrentUser, note: str | None = None
) -> dict[str, Any]:
    """Write one key and bump the shared version, so every worker sees it on its next read.

    The write and the bump are one transaction. A crash between them would leave either a value
    no worker picks up (silently ignored until the next unrelated write) or a version bump with
    no change behind it (a cache reload that reloads nothing) - both are worse than a refusal.
    """
    coerced = _coerce(key, value)
    if key == "log_level":
        apply_log_level(str(coerced))
    with db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO developer_config (key, value_json, value_type, updated_at, updated_by, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                value_type = excluded.value_type,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by,
                note = excluded.note
            """,
            (
                key,
                json.dumps(coerced),
                str(RUNTIME_KEYS[key]["type"]),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(actor.id),
                note,
            ),
        )
        conn.execute(
            "UPDATE developer_config_version SET version = version + 1, changed_at = ? WHERE id = 1",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
        )
    version, values = runtime_snapshot(refresh=True)
    return {"key": key, "value": values[key], "version": version}


def apply_log_level(level: str) -> None:
    """Apply a level to this process's root logger.

    Deliberately narrow: the level of one logger namespace, not a reconfiguration of logging.
    A "set the log level" feature that rebuilt handlers would silently drop whatever a
    deployment had attached to them.
    """
    text = str(level).upper()
    if text in LOG_LEVELS:
        logging.getLogger().setLevel(getattr(logging, text))
        logger.debug("runtime: root log level set to %s", text)


def invalidate_runtime_cache(actor: CurrentUser | None = None) -> None:
    """Drop this process's cache. The next read re-reads the version and the values."""
    global _runtime_cache
    _runtime_cache = None
    logger.info("runtime configuration cache dropped by %s", actor.id if actor else "system")


def maintenance_mode() -> bool:
    """Whether writes are currently refused.

    Read per request rather than per boot, which is the whole point of the store: a window
    opened at 03:00 must not need a deployment, and must apply to every worker.
    """
    try:
        return bool(runtime_value("maintenance_mode"))
    except Exception:  # pragma: no cover - a store that cannot be read must not stop writes
        return False


# ---------------------------------------------------------------------------
# the private alert hub
# ---------------------------------------------------------------------------
SEVERITIES = ("info", "warning", "critical")

#: The vocabulary the hub accepts. Closed for the same reason the runtime keys are: an alert
#: kind nothing raises is a filter that silently matches nothing, and a reader cannot tell
#: that from a quiet system.
ALERT_KINDS: dict[str, str] = {
    # The deployment's own events. These were written to ``admin_notifications`` until the
    # split below: a forced start, a schema repair, a retention sweep and a coverage report
    # are the *host's* log lines, addressed to whoever runs the deployment, and a site
    # administrator has no action to take on any of them. They live here now, and the
    # administrator's queue no longer carries them (``notifications.DEPLOYMENT_KINDS``).
    "startup_degraded": "The deployment started with advisory checks failing.",
    "schema_repair": "The schema was repaired rather than refused, at startup.",
    "retention_sweep": "An automated retention sweep erased data, or could not.",
    "coverage_report": "The standing detector-coverage comparison changed verdict.",
    "db_pool_saturation": "Connections or lock waits past what this deployment is sized for.",
    "db_lock_contention": "SQLite refused a write after busy_timeout: a punch may have failed.",
    "slow_query": "One statement took longer than the configured threshold.",
    "rate_limit_spike": "A client is being refused repeatedly: a misconfigured app, or a scan.",
    "unhandled_exception": "A request raised past every handler. Carries the trace id.",
    "auth_anomaly": "Repeated credential failures against the same account.",
    "startup_override": "The deployment was forced up past a failing self-test.",
    "capacity_refusal": "Face verification refused a request for room - a site arriving at once.",
    "developer_account_seeded": "The root account was created, or its credential was rotated.",
}


def _dedupe_stamp(window_seconds: int) -> str:
    """A key fragment that makes the unique index enforce "once per window"."""
    bucket = int(time.time() // max(1, window_seconds))
    return f"{window_seconds}:{bucket}"


def raise_alert(
    *,
    kind: str,
    summary: str,
    severity: str = "warning",
    source: str = "application",
    detail: Any = None,
    trace_id: str | None = None,
    dedupe_window_seconds: int | None = 3600,
) -> int | None:
    """Record an infrastructure alert for the root tier. Never raises.

    A diagnostics path that can fail a punch is worse than the fault it was watching, so every
    failure here is swallowed and logged. The dedupe key carries its own window, which is what
    the unique index enforces: one row per window, so a saturated pool writes one alert an hour
    rather than one per tick.
    """
    try:
        if kind not in ALERT_KINDS:
            logger.warning("developer alert with an unknown kind: %r", kind)
            return None
        if severity not in SEVERITIES:
            severity = "warning"
        key = f"{kind}:{_dedupe_stamp(dedupe_window_seconds)}" if dedupe_window_seconds else None
        with db(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO developer_alerts
                    (kind, severity, source, summary, detail_json, trace_id, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    severity,
                    source,
                    str(summary)[:500],
                    json.dumps(detail, default=str) if detail is not None else None,
                    trace_id or current_trace_id(),
                    key,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            if cursor.rowcount == 0:
                return None  # already raised in this window
            return int(cursor.lastrowid or 0)
    except Exception as exc:  # pragma: no cover - the hub must never break the caller
        logger.warning("could not raise a developer alert (%s): %s", kind, exc)
        return None


def list_alerts(
    *, limit: int = 100, unread_only: bool = False, kind: str | None = None
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    clauses, params = [], []
    if unread_only:
        clauses.append("read_at IS NULL")
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM developer_alerts {where} ORDER BY id DESC LIMIT ?", (*params, limit)
        ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_alert(alert_id: int, *, actor: CurrentUser) -> dict[str, Any]:
    with db(write=True) as conn:
        row = conn.execute("SELECT id, read_at FROM developer_alerts WHERE id = ?", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Alert not found.")
        if row["read_at"]:
            return {"id": alert_id, "already_read": True, "read_at": row["read_at"]}
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE developer_alerts SET read_at = ?, read_by = ? WHERE id = ?",
            (stamp, str(actor.id), alert_id),
        )
    return {"id": alert_id, "already_read": False, "read_at": stamp}


def unread_alert_count() -> int:
    try:
        return int(connection_stats()["unread_alerts"])
    except Exception:  # pragma: no cover
        return 0


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------
def pool_health() -> dict[str, Any]:
    """What "connection pool health" means for a SQLite deployment, honestly.

    There is no pool: there is one writer at a time and any number of readers, and the
    question an operator actually has is "is anything waiting on the write lock, and is the
    wait long enough to fail a punch". So that is what is reported - the journal mode, the
    busy timeout, how much waiting has been recorded and how much of it timed out - rather
    than a pool's counters, which would be an invented statistic here.
    """
    facts: dict[str, Any] = dict(connection_stats())
    with db() as conn:
        for pragma, name in (
            ("journal_mode", "journal_mode"),
            ("busy_timeout", "busy_timeout_ms"),
            ("page_count", "page_count"),
            ("page_size", "page_size"),
        ):
            try:
                facts[name] = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
            except sqlite3.Error as exc:
                facts[name] = f"unreadable: {exc}"
    facts["write_model"] = "single-writer (SQLite, WAL); readers never block the writer"
    facts["saturated"] = bool(
        facts.get("lock_timeouts") or (facts.get("max_lock_wait_seconds") or 0) >= 2.0
    )
    return facts


#: The queries an operator may ask SQLite to explain. Named, allowlisted, and read-only: this
#: is what replaces ``pg_stat_statements``, which SQLite does not have. Adding to it is a code
#: change and reviewed as one - the alternative (accepting SQL from a caller) is a diagnostics
#: endpoint that can read any table, which is not a diagnostics endpoint.
EXPLAINABLE: dict[str, str] = {
    "roster": "SELECT id, name, role, status FROM users ORDER BY CAST(id AS INTEGER) ASC",
    "audit_recent": "SELECT id, actor_id, action, entity, entity_id FROM audit_log ORDER BY id DESC LIMIT 200",
    "unread_notifications": "SELECT id, kind, severity FROM admin_notifications WHERE read_at IS NULL ORDER BY id DESC",
    "open_notes": "SELECT id, worker_id, status FROM worker_notes WHERE status = 'open'",
    "pending_reviews": "SELECT id, worker_id, status FROM attendance_logs WHERE status_code = 'pending_review'",
}


def slow_query_inspection(limit: int = 20) -> dict[str, Any]:
    """The recorded slow statements, and on demand the plan for a named query."""
    return {
        "threshold_ms": slow_query_threshold_ms(),
        "recent": _slow_query_ring(limit=max(1, min(int(limit), 100))),
        "explainable": sorted(EXPLAINABLE),
        "note": (
            "Statement text is deliberately not recorded (see the module docstring). What is "
            "recorded is the verb, how long it took and the request's trace id."
        ),
    }


def explain_query(name: str) -> list[str]:
    statement = EXPLAINABLE.get(name)
    if statement is None:
        raise HTTPException(
            status_code=404, detail=f"Not an explainable query. Try one of: {', '.join(sorted(EXPLAINABLE))}."
        )
    with db() as conn:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
    return [str(row["detail"]) for row in rows]


def slow_query_threshold_ms() -> int:
    return 250


#: The caches this deployment really has, each with how to drop it. Deliberately short and
#: deliberately honest: a "flush all caches" that reports four successes while three of them
#: did nothing is worse than one that reports two. Anything added here must have a reader, or
#: it is a button that lies.
CACHES: dict[str, str] = {
    "runtime_config": "This process's copy of the runtime store; the next read re-reads it.",
    "rate_limit_buckets": "The in-process rate-limiter's counters (slowapi's storage).",
}


def flush_cache(name: str, *, actor: CurrentUser | None = None) -> dict[str, Any]:
    if name not in CACHES:
        raise HTTPException(
            status_code=404, detail=f"Unknown cache. Try one of: {', '.join(sorted(CACHES))}."
        )
    if name == "runtime_config":
        invalidate_runtime_cache(actor)
        return {"cache": name, "flushed": True, "note": CACHES[name]}
    if name == "rate_limit_buckets":
        try:
            import rate_limit

            limiter = getattr(rate_limit, "limiter", None)
            storage = getattr(limiter, "_storage", None)
            reset = getattr(limiter, "reset", None)
            if callable(reset):
                reset()
            elif storage is not None and hasattr(storage, "clear"):
                storage.clear()
            else:  # pragma: no cover - depends on the slowapi version
                return {
                    "cache": name,
                    "flushed": False,
                    "note": "this build of the limiter exposes no way to drop its counters",
                }
            return {"cache": name, "flushed": True, "note": CACHES[name]}
        except Exception as exc:  # pragma: no cover
            return {"cache": name, "flushed": False, "note": f"could not flush: {exc}"}
    raise HTTPException(status_code=404, detail="Unknown cache.")  # pragma: no cover


def flush_all_caches(*, actor: CurrentUser | None = None) -> list[dict[str, Any]]:
    return [flush_cache(name, actor=actor) for name in sorted(CACHES)]


# ---------------------------------------------------------------------------
# the raw audit stream
# ---------------------------------------------------------------------------
#: The actions that are *security* events rather than administrative housekeeping. The
#: administrator's audit view shows everything; this view is the subset an incident is read
#: from, which is why it is named here rather than filtered by hand at the endpoint.
SECURITY_ACTIONS: tuple[str, ...] = (
    "login",
    "login_failed",
    "password_reset",
    "password_change_self",
    "user_create",
    "user_delete",
    "user_edit",
    "user_status",
    "role_change",
    "startup_override",
    "notification_acknowledge",
    "retention_sweep",
    "biometric_enroll",
    "review_approve",
    "review_reject",
)


def audit_stream(
    *,
    limit: int = 200,
    actions: Iterable[str] | None = None,
    actor_id: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """The raw appended history, plus the trace ids that link it to the alert hub.

    Raw on purpose: this is the surface an incident is reconstructed from, so it does not
    summarise, deduplicate or hide the developer's own actions from the developer. What it does
    *not* do is disclose a hash or a token: the audit rows carry no credential, and the
    projection below names every field it returns rather than shipping ``SELECT *``.
    """
    limit = max(1, min(int(limit), 1000))
    chosen = tuple(actions) if actions else SECURITY_ACTIONS
    placeholders = ",".join("?" for _ in chosen)
    clauses = [f"action IN ({placeholders})"]
    params: list[Any] = list(chosen)
    if actor_id:
        clauses.append("actor_id = ?")
        params.append(str(actor_id))
    if since:
        clauses.append("created_at >= ?")
        params.append(str(since))
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, created_at, actor_id, actor_role, action, entity, entity_id, ip, user_agent
            FROM audit_log
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        alerts = conn.execute(
            "SELECT id, kind, severity, trace_id, created_at, summary FROM developer_alerts "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {
        "events": [dict(row) for row in rows],
        "alerts": [dict(row) for row in alerts],
        "security_actions": list(SECURITY_ACTIONS),
    }


# ---------------------------------------------------------------------------
# concealment & anti-enumeration
# ---------------------------------------------------------------------------
def concealed_ids() -> set[str]:
    """The account ids that are hidden from every non-developer read.

    Read from the database rather than hardcoded, so an account created in a band this build
    has never heard of is still concealed by its role - and so that deleting the account
    un-conceals the id automatically.
    """
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id FROM users WHERE role = ?", (DEVELOPER_ROLE,)
            ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["id"]) for row in rows}


def hides(current: CurrentUser, subject_id: Any) -> bool:
    """Whether ``subject_id`` must be withheld from ``current``.

    One function, used by every read path, because the failure mode of this control is
    *forgetting* it in one of them - a roster, a detail view, an audit query, a count. The
    developer sees itself; nobody else sees it at all.
    """
    if current.is_developer:
        return False
    return str(subject_id) in concealed_ids()


def visible_rows(rows: list[dict[str, Any]], current: CurrentUser, *, key: str = "id") -> list[dict[str, Any]]:
    if current.is_developer:
        return rows
    hidden = concealed_ids()
    return [row for row in rows if str(row.get(key)) not in hidden]


def visibility_clause(current: CurrentUser, *, column: str = "user_id") -> tuple[str, tuple]:
    """``(sql, params)`` that hides the developer from a query, or ``("", ())`` for the developer.

    The SQL half of ``hides``, for the read paths that filter in the database - a list that is
    narrowed in Python leaks the *count* of what it narrowed, and a count is an enumeration.
    """
    if current.is_developer:
        return "", ()
    hidden = concealed_ids()
    if not hidden:
        return "", ()
    placeholders = ",".join("?" for _ in hidden)
    return f" AND {column} NOT IN ({placeholders})", tuple(sorted(hidden))


def anti_enumeration_response() -> dict[str, str]:
    """The one sentence every path returns about an account a caller may not know about.

    Uniform on purpose, and identical whether the account exists or not: a distinct answer
    ("that id is taken", "no such account") is an oracle that turns a hidden account into a
    discoverable one, which is the whole point of concealing it.
    """
    return {"detail": "Request accepted."}


# ---------------------------------------------------------------------------
# the seed
# ---------------------------------------------------------------------------
#: The id the deployment's root account is minted at unless the operator says otherwise.
#:
#: A 64-bit value far above every business band (``security.DEVELOPER_ID_FLOOR``), which is
#: storage-native here: ``users.id`` is ``TEXT PRIMARY KEY`` and every consumer parses it with
#: Python's arbitrary-precision ``int``, so 3.09e11 needs no schema change, no ``BIGINT``
#: migration, and cannot disturb a sequence - the AUTOINCREMENT sequences in this database
#: belong to ``attendance_logs`` and ``developer_alerts``, and ``users`` has never had one.
DEVELOPER_ID_DEFAULT = "309010401073"

#: The name the root account carries. Deliberately unremarkable: it appears in no list, so it
#: exists for a log line and for the operator who has to recognise the row in a backup.
DEVELOPER_NAME_DEFAULT = "Developer"


def seed_developer_account(
    *,
    password: str,
    user_id: str = DEVELOPER_ID_DEFAULT,
    name: str = DEVELOPER_NAME_DEFAULT,
    email: str | None = None,
    phone: str | None = None,
    rotate_password: bool = False,
    actor: str = "tools/seed_developer.py",
) -> dict[str, Any]:
    """Create the root account, or leave an existing one alone.

    **Idempotent in the direction that matters.** Running this twice is safe, and running it
    after an operator has rotated the credential does *not* put the seed's value back: a seed
    that rewrote a live password on every deploy would be a permanent backdoor on a schedule,
    and the password would live in a deploy pipeline's environment forever. ``rotate_password``
    is the explicit way to ask for a reset, and it bumps ``token_version`` so every outstanding
    token for the account dies with the old credential.

    **Credentials are a parameter, never a literal.** Nothing in this module - or in any request
    handler - contains a default password, an email address or a name that could be used to
    reach the account. ``tools/seed_developer.py`` reads its value from the environment or
    generates one; the runtime never sees either.

    The id is validated against ``security.ROLE_ID_RANGES``, so a seed pointed at a
    business-band id is refused rather than planting root in a worker's range. Raises
    ``ValueError`` for a refusal an operator should read, ``HTTPException(400)`` for a password
    that fails the deployment's own policy (``hash_password`` enforces it).
    """
    target = str(user_id).strip()
    validate_user_id_for_role(target, DEVELOPER_ROLE)
    if not password:
        raise ValueError(
            "a password is required: this seed has no default credential, on purpose"
        )
    who = str(actor)
    clean_email = (email or "").strip()
    clean_phone = (phone or "").strip()

    with db(write=True) as conn:
        row = conn.execute(
            "SELECT id, role, COALESCE(token_version, 0) AS token_version FROM users WHERE id = ?",
            (target,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (id, name, email, phone, password_hash, role, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                (target, name, clean_email, clean_phone, hash_password(password), DEVELOPER_ROLE),
            )
            created, rotated = True, True
        else:
            # An id in the root band that is *not* a developer account is a mistake in the
            # deployment (or an attempt to promote one), and neither should be resolved
            # silently by a seed. Refusing leaves the row exactly as it was.
            if str(row["role"]) != DEVELOPER_ROLE:
                raise ValueError(
                    f"user {target} exists with role {row['role']!r}; refusing to promote an "
                    "existing account to the root tier"
                )
            created = False
            rotated = bool(rotate_password)
            if rotated:
                conn.execute(
                    "UPDATE users SET password_hash = ?, token_version = ? WHERE id = ?",
                    (hash_password(password), int(row["token_version"]) + 1, target),
                )
            else:
                # The name is metadata an operator may legitimately fix; the credential is not.
                conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, target))

    # On the record, in the tier's own hub: an account that can read every audit row appearing
    # in a database is an event the root tier should be able to see without being told.
    raise_alert(
        kind="developer_account_seeded",
        summary=(
            f"root account {target} "
            + ("created" if created else ("credential rotated" if rotated else "re-seeded"))
        ),
        severity="warning",
        source="seed",
        detail={"id": target, "created": created, "rotated": rotated, "actor": who},
        dedupe_window_seconds=None,
    )
    logger.info("root account %s %s (by %s)", target, "created" if created else "re-seeded", who)
    # Never the hash, and never the password: the return value is printed by a CLI and may land
    # in a terminal scrollback or a CI log.
    return {
        "id": target,
        "name": name,
        "role": DEVELOPER_ROLE,
        "created": created,
        "rotated": rotated,
        "actor": who,
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
class RuntimeUpdate(BaseModel):
    value: Any = Field(..., description="The value to store; typed by the key.")
    note: str | None = Field(
        None, max_length=280, description="Why the change was made. Appended to the audit trail."
    )


@router.get("/developer/runtime")
async def read_runtime(current: CurrentUser = Depends(require_developer)):
    """Every runtime key, its current value, and the version all workers share."""
    version, values = runtime_snapshot(refresh=True)
    return {
        "version": version,
        "values": values,
        "defaults": {key: spec["default"] for key, spec in RUNTIME_KEYS.items()},
        "documentation": {key: spec["why"] for key, spec in RUNTIME_KEYS.items()},
    }


@router.patch("/developer/runtime/{key}")
async def update_runtime(
    key: str,
    req: RuntimeUpdate,
    request: Request,
    current: CurrentUser = Depends(require_developer),
):
    """Change one runtime value. Takes effect on every worker without a restart."""
    try:
        result = set_runtime_value(key, req.value, actor=current, note=req.note)
    except RuntimeValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    logger.info(
        "runtime %s set to %r by %s (trace %s)", key, result["value"], current.id, current_trace_id()
    )
    return result


@router.get("/developer/alerts")
async def read_alerts(
    limit: int = Query(100, ge=1, le=500),
    unread_only: bool = False,
    kind: str | None = None,
    current: CurrentUser = Depends(require_developer),
):
    """The private hub. Unreachable by every other role, by construction rather than by filter."""
    return {
        "alerts": list_alerts(limit=limit, unread_only=unread_only, kind=kind),
        "unread": unread_alert_count(),
        "kinds": ALERT_KINDS,
    }


@router.post("/developer/alerts/{alert_id}/read")
async def read_alert(
    alert_id: int, current: CurrentUser = Depends(require_developer)
):
    return acknowledge_alert(alert_id, actor=current)


@router.get("/developer/diagnostics/pool")
async def diagnostics_pool(current: CurrentUser = Depends(require_developer)):
    return pool_health()


@router.get("/developer/diagnostics/slow-queries")
async def diagnostics_slow_queries(
    limit: int = Query(20, ge=1, le=100), current: CurrentUser = Depends(require_developer)
):
    return slow_query_inspection(limit=limit)


@router.get("/developer/diagnostics/query-plan/{name}")
async def diagnostics_query_plan(
    name: str, current: CurrentUser = Depends(require_developer)
):
    return {"name": name, "plan": explain_query(name)}


@router.post("/developer/diagnostics/caches/{name}/flush")
async def diagnostics_flush(
    name: str, current: CurrentUser = Depends(require_developer)
):
    return flush_cache(name, actor=current)


@router.post("/developer/diagnostics/caches/flush")
async def diagnostics_flush_all(current: CurrentUser = Depends(require_developer)):
    return {"flushed": flush_all_caches(actor=current)}


@router.get("/developer/audit")
async def read_audit_stream(
    limit: int = Query(200, ge=1, le=1000),
    action: str | None = None,
    actor_id: str | None = None,
    since: str | None = None,
    current: CurrentUser = Depends(require_developer),
):
    """Real-time security events, failed-auth anomalies and the trace ids that join them."""
    return audit_stream(
        limit=limit,
        actions=(action,) if action else None,
        actor_id=actor_id,
        since=since or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
    )


# ---------------------------------------------------------------------------
# the refused punches: the band's own diagnostics, not a site's queue
# ---------------------------------------------------------------------------
#: The window the list defaults to, and its ceiling. A day of refusals is a triage queue; a
#: fortnight of them is a table nobody reads.
REFUSED_PUNCH_MAX_DAYS = 7


def _audit_refusal_clear(
    conn: sqlite3.Connection, *, actor: CurrentUser, refusal_id: int, worker_id: Any, ip: str | None
) -> None:
    """Append the one decision this surface makes. Never raises, like every audit write."""
    try:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, after_json, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor.id,
                actor.role,
                "refused_punch_clear",
                "refused_punches",
                str(refusal_id),
                json.dumps({"worker_id": worker_id}, default=str),
                ip,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    except sqlite3.Error:  # pragma: no cover - the trail must not cost the decision
        pass


@router.get("/developer/refused-punches")
async def list_refused_punches(
    current: CurrentUser = Depends(require_developer), days: int = 1
):
    """What the band refused, with the score and the frame beside it.

    The triage surface the deployment needed and never had: a worker who keeps being told "we
    could not confirm that is you" used to leave no record at all, so an operator whose band
    was derived from the wrong corpus could see neither the scores nor the faces. Newest first,
    ``days`` back (default today, capped at a week), each row carrying the distance, the reason
    and whether a frame is waiting.

    **This was an administrator route until it moved here.** The call it serves - "is the band
    refusing honest workers, and at what distance" - is a question about the *model*, not about
    a site's attendance: recognising a face in it is a judgement about a model's calibration,
    and the only person who can act on the answer is the one who can change the band. A site
    administrator reading a wall of refusals has no lever, and a face in a triage list is a
    worker's biometric in front of a reader who has no reason to hold it. Read-only evidence in
    either case - there is no approve path, because a refused punch was never attendance - and
    retention sweeps the rows and their frames on the frame window.
    """
    window = max(0, min(int(days), REFUSED_PUNCH_MAX_DAYS))
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
        item["frame_url"] = f"/api/v1/developer/refused-punches/{item['id']}/frame" if frame else None
        items.append(item)
    return items


@router.get("/developer/refused-punches/{refusal_id}/frame")
async def refused_punch_frame(
    refusal_id: int, current: CurrentUser = Depends(require_developer)
):
    """The downscaled frame one refused punch was measured from. Root tier only.

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


@router.post("/developer/refused-punches/{refusal_id}/clear")
async def clear_refused_punch(
    refusal_id: int, request: Request, current: CurrentUser = Depends(require_developer)
):
    """Drop one refusal from the list, so a triaged list is what still needs looking at.

    Deliberately a clear and not a *decision*: the evidence stays on disk until retention sweeps
    it, and the audit trail says who judged it needed nothing further. A refusal is not
    attendance and cannot become one.
    """
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT id, worker_id FROM refused_punches WHERE id = ?", (refusal_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="This refusal has no record.")
        conn.execute("DELETE FROM refused_punches WHERE id = ?", (refusal_id,))
        _audit_refusal_clear(
            conn,
            actor=current,
            refusal_id=refusal_id,
            worker_id=row["worker_id"],
            ip=request.client.host if request.client else None,
        )
    return {"status": "success", "message": "Refusal cleared."}
