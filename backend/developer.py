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
import os
import re
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import punch_frames
from config import settings
from database import connect, db, resolve_path, slow_queries as _slow_query_ring, connection_stats
from security import (
    DEVELOPER_ID_FLOOR,
    DEVELOPER_ROLE,
    READONLY_SCOPE,
    CurrentUser,
    create_access_token,
    hash_password,
    require_developer,
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
CHOICE = "choice"

#: The liveness modes the runtime override may name, plus the empty string for "no override".
#: ``liveness.MODES`` is the same three values; they are spelled out here rather than imported
#: because ``liveness`` reads this store back (lazily, in ``liveness.mode()``) and a module-level
#: import in this direction would close that loop. A test holds the two tuples to each other, so
#: a fourth mode added to one of them cannot go unnoticed in the other.
LIVENESS_OVERRIDE_KEY = "liveness_mode_override"
LIVENESS_MODES: tuple[str, ...] = ("off", "advisory", "enforce")
LIVENESS_OVERRIDE_CHOICES: tuple[str, ...] = ("",) + LIVENESS_MODES

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
    LIVENESS_OVERRIDE_KEY: {
        "type": CHOICE,
        "choices": LIVENESS_OVERRIDE_CHOICES,
        "default": "",
        "why": "The liveness policy in force, overriding LIVENESS_MODE without a restart: '' "
               "follows the setting, and off/advisory/enforce are liveness's own vocabulary. "
               "The rescue this exists for is a site at 07:00 whose model is refusing honest "
               "workers - the operator drops to advisory in one call and measures, rather than "
               "waiting for a deploy. Read by liveness.mode(), and therefore by the punch path, "
               "by enrolment's 'inherit' mode, by the readiness verdict and by status alike: "
               "an override only some of them honoured would be a button that lies. Disabling "
               "enforcement is a security-relevant act, so it is on the audit trail with the "
               "reason that was given for it.",
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
    if spec["type"] == CHOICE:
        text = str(value).strip().lower()
        if text not in spec["choices"]:
            raise RuntimeValueError(
                f"{key!r} must be one of: "
                + ", ".join(repr(choice) for choice in spec["choices"])
                + " ('' means 'follow the deployment's setting')."
            )
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


# ---------------------------------------------------------------------------
# the audit trail this tier appends to
# ---------------------------------------------------------------------------
#: The action every change to the runtime store is recorded under, whichever door it came
#: through - the generic ``PATCH /developer/runtime/{key}`` or a purpose-built lever such as
#: ``POST /developer/ml/liveness-mode``. One name, so "what has this deployment's policy done
#: lately" is one query rather than one query per feature that grew its own writer.
RUNTIME_CHANGE_ACTION = "runtime_change"


def _audit_developer(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser,
    entity: str,
    entity_id: str | int | None,
    before: Any = None,
    after: Any = None,
    ip: str | None = None,
    created_at: str | None = None,
) -> None:
    """Append one event to the deployment's audit trail, inside the caller's transaction.

    The same columns and the same never-raise contract as ``main._audit``, written here rather
    than imported because ``main`` imports *this* module: that direction is what keeps the root
    tier's door out of the application's, and reaching back for its helper would close the loop.
    It takes no ``Request`` either - ip and user agent are the only two things it wanted, and
    the ip is what the trail is read for; the user agent of a curl from an operator's laptop is
    noise, and this surface is not a browser console's.
    """
    try:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, "
            "before_json, after_json, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor.id,
                actor.role,
                action,
                entity,
                str(entity_id) if entity_id is not None else None,
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                ip,
                created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    except sqlite3.Error:  # pragma: no cover - the trail must not cost the decision
        logger.warning("could not append the audit row for %s", action, exc_info=True)


def _audit_runtime_change(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    key: str,
    before: Any,
    after: Any,
    note: str | None = None,
    ip: str | None = None,
    created_at: str | None = None,
) -> None:
    """The shape a runtime change takes in the trail: one action, before and after by key."""
    _audit_developer(
        conn,
        action=RUNTIME_CHANGE_ACTION,
        actor=actor,
        entity="developer_config",
        entity_id=key,
        before={"value": before},
        after={"value": after, "note": note},
        ip=ip,
        created_at=created_at,
    )


def set_runtime_value(
    key: str,
    value: Any,
    *,
    actor: CurrentUser,
    note: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Write one key and bump the shared version, so every worker sees it on its next read.

    Three writes and they are one transaction: the value, the audit row, and the version bump.
    A crash between any two of them would leave either a value no worker picks up (silently
    ignored until the next unrelated write), a version bump with no change behind it (a cache
    reload that reloads nothing), or - the one that matters for a tier whose whole surface is
    reachable by one account - a change to the deployment's policy that its own audit trail
    does not know about. All three or none.

    The previous value is recorded in the audit row rather than only beside it, so an incident
    reconstructed from the trail alone can answer "what did it used to be" without a second
    query that the next change would have overwritten.
    """
    coerced = _coerce(key, value)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db(write=True) as conn:
        previous = _read_values(conn).get(key)
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
                stamp,
                str(actor.id),
                note,
            ),
        )
        _audit_runtime_change(
            conn,
            actor=actor,
            key=key,
            before=previous,
            after=coerced,
            note=note,
            ip=ip,
            created_at=stamp,
        )
        conn.execute(
            "UPDATE developer_config_version SET version = version + 1, changed_at = ? WHERE id = 1",
            (stamp,),
        )
    # In-process effects, applied after the commit rather than inside it: neither can be rolled
    # back, and both are idempotent, so doing them once the change is durable is the only
    # ordering that cannot leave this process disagreeing with every other worker.
    if key == "log_level":
        apply_log_level(str(coerced))
    if key == LIVENESS_OVERRIDE_KEY:
        _republish_build_info()
    version, values = runtime_snapshot(refresh=True)
    return {"key": key, "value": values[key], "previous": previous, "version": version}


def liveness_override() -> str | None:
    """The runtime liveness mode, or ``None`` when the deployment's own setting decides.

    Read by ``liveness.mode()`` on every punch - through the same version-checked cache
    ``maintenance_mode`` reads through, at the same cost - and swallowed the same way: a store
    that cannot be read must not turn a punch into a 500, so every failure here is the
    configured mode rather than an exception.
    """
    try:
        value = str(runtime_value(LIVENESS_OVERRIDE_KEY) or "").strip().lower()
    except Exception:  # pragma: no cover - a store that cannot be read falls back to the setting
        return None
    return value if value in LIVENESS_MODES else None


def _republish_build_info() -> None:
    """Re-publish the build-info gauge, whose ``liveness_mode`` label is the mode in force.

    The gauge is published once at boot (``telemetry.publish_build_info``), which would leave it
    advertising the configured mode for the whole life of the process - precisely the window an
    override is set in. Failure is swallowed: a metric must never cost a change.
    """
    try:
        import telemetry

        telemetry.publish_build_info(refresh=True)
    except Exception:  # pragma: no cover
        logger.debug("build info not republished after a liveness override", exc_info=True)


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
    # The deployment's own events. These were written to ``admin_notifications`` as well as
    # here: a forced start, a schema repair, a retention sweep and a coverage report are the
    # *host's* log lines, addressed to whoever runs the deployment, and a site administrator
    # has no action to take on any of them. They live here as alerts, and the durable rows of
    # the same events sit in ``admin_notifications`` - whose queue is this tier's too now
    # (``/developer/notifications``), so there is no audience left to withhold one from.
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
    "impersonation_token_minted": "A read-only session was minted for another account.",
    "backup_unverified": "A manual database snapshot did not pass its own check.",
    "snapshot_unverified": "A project snapshot was written and failed its own verification.",
    "snapshot_failed": "A project snapshot could not be written at all.",
    "biometric_reindex": "A face template was rewritten from a stored selfie, by hand.",
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
    "sessions_revoked",
    "startup_override",
    "notification_acknowledge",
    "retention_sweep",
    "biometric_enroll",
    "review_approve",
    "review_reject",
    # The root tier's own levers over the deployment: a policy change, a journal flush, and a
    # restart of the process that owns the models. Infrastructure rather than attendance - and
    # exactly what an incident is reconstructed from when the question is "was anything done to
    # the machine while this was going wrong".
    RUNTIME_CHANGE_ACTION,
    "db_wal_checkpoint",
    "engine_worker_restart",
    # And the recovery tools: a snapshot taken before a change, a face template rewritten by
    # hand, and a read-only session minted for somebody else's account. All three are acts of
    # the root tier on the *records* the application exists to keep.
    "dev_backup_created",
    "dev_snapshot_created",
    "dev_snapshot_failed",
    "dev_biometric_reindex",
    "dev_impersonation_token_minted",
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


def _require_root_band_id(user_id: str) -> None:
    """Refuse an account id below the root band. Raises ``HTTPException(400)``.

    The root account sits far above every business account on purpose: an id inside a worker's
    or an administrator's range would make the deployment's own root read as an ordinary
    account in a log line, and would put it in the part of the id space an operator (or an
    approval) is filling. This is what keeps ``DEVELOPER_ID_FLOOR`` a rule the seed enforces
    rather than a convention it happens to follow - the per-role bands that used to be checked
    alongside it are gone.
    """
    try:
        value = int(str(user_id).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="User ID must be a numeric integer.") from None
    if value < DEVELOPER_ID_FLOOR:
        raise HTTPException(
            status_code=400,
            detail=f"Developer ID must be {DEVELOPER_ID_FLOOR} or greater.",
        )


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

    The id is checked against the root band's floor (``security.DEVELOPER_ID_FLOOR``), so a seed
    pointed at an ordinary account's id is refused rather than planting root among the business
    accounts. Raises ``ValueError`` for a refusal an operator should read, ``HTTPException(400)``
    for an id below the root band or a password that fails the deployment's own policy
    (``hash_password`` enforces it).
    """
    target = str(user_id).strip()
    _require_root_band_id(target)
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
        result = set_runtime_value(
            key,
            req.value,
            actor=current,
            note=req.note,
            ip=request.client.host if request.client else None,
        )
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


# ---------------------------------------------------------------------------
# live sessions: who can act right now, and the one lever that ends it
# ---------------------------------------------------------------------------
#: Roles whose sessions are listed. The developer sees the deployment's *operators*; other
#: developer sessions are shown too - a second root session is exactly the thing to know about.
SESSION_ROLES: tuple[str, ...] = ("worker", "moallem", "admin", "head_admin", DEVELOPER_ROLE)


def _live_session_row(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per account that could hold a live token, newest sign-in first.

    ``token_version`` is the *version* of the credential, not a session count: every token an
    account holds carries the version it was minted at, and the account re-reads the column on
    every request. "Live" therefore means ``token_version`` has not been bumped past the
    token's ``ver`` - which the server knows per token, not per account. The honest projection
    is: who could be signed in, when their credential was last minted, and what revoking would
    do. Placeholders name every field returned rather than shipping ``SELECT *``.
    """
    placeholders = ",".join("?" for _ in SESSION_ROLES)
    rows = conn.execute(
        f"""
        SELECT u.id, u.name, u.role, u.status,
               COALESCE(u.token_version, 0) AS token_version,
               (SELECT MAX(a.created_at) FROM audit_log a
                 WHERE a.actor_id = u.id AND a.action = 'login') AS last_login_at,
               (SELECT MAX(a.ip) FROM audit_log a
                 WHERE a.actor_id = u.id AND a.action = 'login') AS last_login_ip,
               (SELECT COUNT(*) FROM worker_devices d
                 WHERE d.worker_id = u.id AND d.revoked_at IS NULL) AS active_devices
        FROM users u
        WHERE u.role IN ({placeholders})
        ORDER BY last_login_at DESC, u.id
        """,
        SESSION_ROLES,
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/developer/sessions")
async def list_live_sessions(current: CurrentUser = Depends(require_developer)):
    """Every account that could hold a live token, with the lever beside it.

    This is a *revocation* surface, not a session log: the bearer tokens are stateless JWTs the
    server does not store, so "which tokens exist" is unknowable and the useful question is the
    one this answers - who could act right now, and what happens when the lever is pulled. The
    audit ``login`` rows are the best evidence of a live session, so the projection reads them
    rather than pretending to list tokens.
    """
    with db() as conn:
        return _live_session_row(conn)


@router.post("/developer/sessions/{user_id}/revoke")
async def revoke_user_sessions(
    user_id: str, request: Request, current: CurrentUser = Depends(require_developer)
):
    """End every session an account holds, by bumping ``token_version``.

    The same lever a password change pulls (``main._revoke_user_access`` says why devices and
    invites go with it): every outstanding token carries the old version and stops verifying at
    the next request, the offline signing keys on any phone are revoked so a queued punch cannot
    outlive the session, and an open enrollment link cannot mint a fresh credential afterwards.
    The account itself is untouched - its password, its role, its history; this is a sign-out,
    not a deactivation. A developer session may be revoked like any other, which is the point:
    a lost laptop does not get an exception for wearing the root tier.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db(write=True) as conn:
        row = conn.execute(
            "SELECT id, name, role, status, COALESCE(token_version, 0) AS token_version FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such account.")
        if row["status"] != "active":
            # A deactivated account has no sessions to revoke: its sign-out already happened,
            # and saying so is more useful than a second bump that revokes nothing.
            raise HTTPException(status_code=409, detail="This account is deactivated and holds no sessions.")

        before_version = int(row["token_version"])
        conn.execute(
            "UPDATE users SET token_version = COALESCE(token_version, 0) + 1 WHERE id = ?",
            (user_id,),
        )
        devices = conn.execute(
            "UPDATE worker_devices SET revoked_at = ?, key_epoch = key_epoch + 1 "
            "WHERE worker_id = ? AND revoked_at IS NULL",
            (now, user_id),
        ).rowcount
        invites = conn.execute(
            "UPDATE enrollment_invites SET revoked_at = ? WHERE worker_id = ? AND revoked_at IS NULL",
            (now, user_id),
        ).rowcount
        try:
            conn.execute(
                "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, before_json, after_json, ip, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    current.id,
                    current.role,
                    "sessions_revoked",
                    "users",
                    str(user_id),
                    json.dumps({"token_version": before_version}, default=str),
                    json.dumps({"token_version": before_version + 1, "devices_revoked": int(devices or 0), "invites_revoked": int(invites or 0)}, default=str),
                    request.client.host if request.client else None,
                    now,
                ),
            )
        except sqlite3.Error:  # pragma: no cover - the trail must not cost the revocation
            pass
    return {
        "status": "success",
        "user_id": user_id,
        "token_version": before_version + 1,
        "devices_revoked": int(devices or 0),
        "invites_revoked": int(invites or 0),
    }


# ---------------------------------------------------------------------------
# the ML tier: what the models are doing, and the lever over the one policy
# ---------------------------------------------------------------------------
def _band_view(band: Any) -> dict[str, Any]:
    """One decision band as an operator reads it: the lines, the measurement, and the rule.

    ``derived()`` is included beside the stored lines because that is the whole claim this
    application makes about them - a line is the output of a rule applied to a measurement, and
    a test holds the shipped numbers to it. A panel that showed only the lines would make a
    hand-edited threshold indistinguishable from a derived one.
    """
    approve, review = band.derived()
    return {
        "model": band.model,
        "approve": band.approve,
        "review": band.review,
        "derived_approve": approve,
        "derived_review": review,
        "one_line": review <= approve,
        "genuine_ceiling": band.genuine_ceiling,
        "impostor_floor": band.impostor_floor,
        "evidence": band.evidence,
        "basis": band.basis(),
    }


def _detector_view() -> dict[str, Any]:
    """The detector, the crop it produces, and what a verification would run with *now*.

    WHAT ASKING COSTS, STATED RATHER THAN HIDDEN
    -------------------------------------------
    The effective pipeline is not knowable from the settings: this build is *for* YuNet, and a
    deployment without the model file detects with the previous one, which is a different crop
    and therefore a different embedding space. ``face_detector.available`` is what tells those
    apart, and it opens the session to do it - so the first call on a process that has not yet
    taken a punch is the call that loads the detector, once, for the same memory the first
    punch would have paid for anyway. A panel that reported "the model file is present" instead
    would be the readiness check that lied, and the pipeline line it feeds (``face_match_band``)
    is a check the startup gate refuses to serve without.
    """
    import face_detector

    available, reason = face_detector.available()
    path = face_detector.model_path()
    return {
        "available": available,
        "reason": reason or None,
        "detector": face_detector.active_detector(),
        "pipeline": face_detector.active_pipeline(),
        "intended": {
            "detector": face_detector.DETECTOR_NAME,
            "pipeline": face_detector.PIPELINE,
        },
        "fallback": {
            "detector": face_detector.FALLBACK_DETECTOR,
            "pipeline": face_detector.FALLBACK_PIPELINE,
        },
        "model_path": str(path),
        "model_present": path.exists(),
        "model_fingerprint": face_detector.fingerprint(),
        "crop": {
            "aligned_size": face_detector.ALIGNED_SIZE,
            "min_subject_px": face_detector.MIN_SUBJECT_PX,
            "native_input_size": face_detector.DETECTOR_INPUT_SIZE,
            "working_input_size": face_detector.capped_input_size(),
            "tiles": face_detector.capped_tiles(),
            "warm_frame": list(face_detector.warm_frame_size()),
            "frame_ceiling": face_detector.frame_ceiling(),
            "score_threshold": face_detector.SCORE_THRESHOLD,
            "max_subject_frame_fraction": face_detector.MAX_SUBJECT_FRAME_FRACTION,
            "close_frame_retry_px": face_detector.CLOSE_FRAME_RETRY_PX,
        },
    }


def ml_diagnostics() -> dict[str, Any]:
    """The engine panel: the queue, the crop, liveness, and every band this build can score.

    Four answers, each from the module that owns the fact rather than from a copy of it here:
    ``face_engine.stats`` for depth, throughput and refusals; ``face_detector`` for the detector
    and the crop; ``liveness.status`` for the mode and its thresholds; and the band table
    itself, with the *active* band named separately because that is the one a punch is decided
    by.

    A build with no band for the live pipeline is a *state*, not a failure of this endpoint:
    enrolment refuses and the startup gate names the missing measurement. So the active band is
    reported as an error string beside the table rather than raised - the panel an operator is
    reading during that incident is the last thing that should 500.
    """
    import face_detector
    import face_engine
    import liveness

    panel: dict[str, Any] = {
        "engine": face_engine.stats(),
        "detector": _detector_view(),
        "liveness": liveness.status(),
        "liveness_override": liveness_override(),
        # The vocabulary the console's mode select is drawn from, rather than a fourth copy of
        # three words spelled out in the frontend: this endpoint is the *other* door onto the
        # same policy, so the two doors naming the same modes is the only arrangement in which
        # a button drawn here cannot ask for something ``set_liveness_mode`` refuses.
        "liveness_modes": list(liveness.MODES),
        "bands": {name: _band_view(band) for name, band in face_detector.BANDS.items()},
    }
    try:
        panel["active_band"] = _band_view(face_detector.active_band())
        panel["active_band_error"] = None
    except Exception as exc:  # noqa: BLE001 - a missing measurement is a reportable state
        panel["active_band"] = None
        panel["active_band_error"] = f"{type(exc).__name__}: {exc}"
    return panel


def _gallery_facts(source: str) -> tuple[float | None, dict[str, Any]]:
    """``(the shadow line, what the cache says about itself)`` from a cached gallery.

    Read as a *cache*, not loaded as a gallery: ``shadow_rollout.load_gallery`` validates the
    cached width and graph digest against the live shadow encoder, which means loading that
    graph - a 200 MiB answer to a question about a JSON file. What is reported here is what the
    cache claims, and the endpoint's own text names the tool that refuses a mismatched one; a
    line this endpoint computed from a gallery belonging to another graph would be a gate
    verdict about vectors nobody is comparing.
    """
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    band = payload.get("band") or {}
    templates = payload.get("templates") or {}
    facts = {
        "path": str(source),
        "templates": len(templates),
        "width": payload.get("width"),
        "model_id": str(payload.get("model_id") or "")[:16] or None,
        "contract": payload.get("contract"),
        "band": {
            "model": band.get("model"),
            "approve": band.get("approve"),
            "review": band.get("review"),
            "evidence": band.get("evidence"),
        },
    }
    approve = band.get("approve")
    return (float(approve) if approve is not None else None), facts


def shadow_summary(
    *,
    gallery: str | None = None,
    enforced_threshold: float | None = None,
    shadow_threshold: float | None = None,
    min_coverage: float = 0.98,
    min_paired_samples: int = 2000,
    max_error_rate: float = 0.005,
) -> dict[str, Any]:
    """The shadow migration's paired log, and the cutover gate's verdict on it.

    WHAT CAN BE ANSWERED HERE, AND WHAT CANNOT
    ------------------------------------------
    The paired log is a SQLite file the shadow run writes (``shadow.scores``), so its health and
    its threshold-free comparison - events, errors, paired samples, verdict agreement, the two
    encoders' mean milliseconds - are four SELECTs and are always answerable. The *gate* is a
    different question: ``shadow.flip_ready`` needs the two lines the comparison was made at
    (the enforced one is the live band's approve, the shadow's lives in the gallery the backfill
    wrote) and the gallery's coverage of the workforce. Both come from the cache, so both are
    read from it when the caller names it, and the gate is reported as ``null`` with the reason
    it could not be run when the cache is not named - which is an honest answer, and the one an
    operator needs to tell "not ready" apart from "not evaluated".

    The gate is the *same* function the offline tool calls, over the same log: a second
    implementation of "is this migration safe" that could disagree with the CLI is exactly the
    kind of drift this endpoint exists to avoid, and ``tools/shadow_migration.py report`` still
    owns the answer that can load both graphs.
    """
    import face_detector
    import shadow
    import shadow_rollout

    log_path = settings.shadow_log_path
    configured = {
        "log": str(log_path) if log_path else None,
        "log_present": bool(log_path) and Path(str(log_path)).exists(),
        "shadow_model": str(settings.facenet_shadow_model_path),
        "shadow_model_present": Path(str(settings.facenet_shadow_model_path)).exists(),
        "contract": settings.facenet_shadow_contract,
    }
    report: dict[str, Any] = {
        "configured": configured,
        "started": False,
        "gallery": None,
        "summary": None,
        "paired": None,
        "coverage": None,
        "flip_ready": None,
        "gate": {
            "min_coverage": float(min_coverage),
            "min_paired_samples": int(min_paired_samples),
            "max_error_rate": float(max_error_rate),
        },
        "reason": None,
    }
    if not configured["log_present"]:
        report["reason"] = (
            "no paired log to read: SHADOW_LOG_PATH is unset, or the file does not exist yet, so "
            "no shadow pass has been scored on this deployment"
        )
        return report
    report["started"] = True
    try:
        report["summary"] = shadow.summary_of(str(log_path))
    except shadow.ShadowError as exc:
        report["reason"] = str(exc)
        return report

    enforced = enforced_threshold
    if enforced is None:
        try:
            enforced = float(face_detector.active_band().approve)
        except Exception as exc:  # noqa: BLE001 - reported, not raised: the table is below
            report["reason"] = f"the enforced line could not be read from the live band: {exc}"
            return report
    shadow_line = shadow_threshold
    if gallery:
        try:
            cached_line, facts = _gallery_facts(gallery)
            report["gallery"] = facts
            with db() as conn:
                workers = shadow_rollout.total_workers(conn)
            report["coverage"] = round(min(1.0, facts["templates"] / float(max(1, workers))), 4)
            report["gallery"]["workers"] = workers
        except (OSError, ValueError, TypeError) as exc:
            report["reason"] = f"the gallery cache at {gallery} could not be read: {exc}"
            return report
        if shadow_line is None:
            shadow_line = cached_line
    if shadow_line is None:
        report["reason"] = (
            "the shadow's own line is not known here: pass shadow_threshold=<the line the shadow "
            "was calibrated at>, or gallery=<the cache 'backfill' wrote>, whose band carries it. "
            "The enforced line is read from the live band"
        )
        return report
    report["paired"] = shadow.paired_confusion(
        str(log_path),
        enforced_threshold=float(enforced),
        shadow_threshold=float(shadow_line),
    )
    if report["coverage"] is None:
        report["reason"] = (
            "the paired log has been read, but the cutover gate also needs the shadow gallery's "
            "coverage of the workforce: pass gallery=<the cache 'backfill' wrote>"
        )
        return report
    ready, reason = shadow.flip_ready(
        coverage_fraction=float(report["coverage"]),
        paired=report["paired"],
        min_coverage=float(min_coverage),
        min_paired_samples=int(min_paired_samples),
        max_error_rate=float(max_error_rate),
        summary=report["summary"],
    )
    report["flip_ready"] = ready
    report["reason"] = reason
    return report


def set_liveness_mode(
    mode: str, *, reason: str | None, actor: CurrentUser, ip: str | None = None
) -> dict[str, Any]:
    """Point the runtime liveness mode at one of ``off`` / ``advisory`` / ``enforce``.

    WHY THIS IS A LEVER OF ITS OWN RATHER THAN A PATCH OF THE RUNTIME STORE
    ---------------------------------------------------------------------
    The store's writer accepts any registered key and any value of that key's type, and what
    this lever adds is the part an incident is *read* from: a required reason, recorded with the
    change in one transaction, and a vocabulary refusal in the vocabulary's own words. The
    generic ``PATCH /developer/runtime/liveness_mode_override`` still works - it is the same
    key, and it now appends the same audit row - but the mode is the one value here that changes
    whether a genuine worker can clock in, so it gets a door that says so.

    'off' is accepted deliberately: during an incident (a model that refuses everybody, a
    detector that will not load) the way to keep a site working is to stop asking the question,
    and an operator who cannot turn it off will turn the whole server off instead. What the
    endpoint refuses to do is make that quiet - the change is logged at WARNING, it is on the
    trail with its reason, and it appears in the published ``liveness_mode`` label.
    """
    wanted = str(mode or "").strip().lower()
    if wanted not in LIVENESS_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode must be one of: {', '.join(LIVENESS_MODES)}."
        )
    text = str(reason or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="a reason is required: it is what the audit row for this change is read for.",
        )
    previous = liveness_override()
    result = set_runtime_value(LIVENESS_OVERRIDE_KEY, wanted, actor=actor, note=text, ip=ip)
    logger.warning(
        "liveness mode override %r -> %r by %s (%s)", previous, wanted, actor.id, text
    )
    return {
        "mode": wanted,
        "previous": previous,
        "reason": text,
        "version": result["version"],
        "note": (
            "applies to every worker on its next read; liveness.mode() is the one place it is "
            "resolved, so the punch path, enrolment, readiness and status all see it"
        ),
    }


class LivenessModeUpdate(BaseModel):
    mode: str = Field(..., description="off, advisory or enforce")
    reason: str = Field(
        ..., min_length=1, max_length=280, description="Why the mode is being moved."
    )


@router.get("/developer/ml/diagnostics")
async def ml_engine_diagnostics(current: CurrentUser = Depends(require_developer)):
    """The models, the queue and the decision bands. Root tier only."""
    return ml_diagnostics()


@router.get("/developer/ml/shadow-summary")
async def ml_shadow_summary(
    gallery: str | None = Query(None, description="The cache 'backfill' wrote, for the gate."),
    enforced_threshold: float | None = None,
    shadow_threshold: float | None = None,
    min_coverage: float = 0.98,
    min_paired_samples: int = 2000,
    max_error_rate: float = 0.005,
    current: CurrentUser = Depends(require_developer),
):
    """The paired log's health, and the cutover gate when it can be evaluated."""
    return shadow_summary(
        gallery=gallery,
        enforced_threshold=enforced_threshold,
        shadow_threshold=shadow_threshold,
        min_coverage=min_coverage,
        min_paired_samples=min_paired_samples,
        max_error_rate=max_error_rate,
    )


@router.post("/developer/ml/liveness-mode")
async def set_liveness_mode_route(
    body: LivenessModeUpdate,
    request: Request,
    current: CurrentUser = Depends(require_developer),
):
    """Move the liveness policy without a restart. Audited with the reason given."""
    return set_liveness_mode(
        body.mode,
        reason=body.reason,
        actor=current,
        ip=request.client.host if request.client else None,
    )


# ---------------------------------------------------------------------------
# the database tier: contention, the journal, and the schema it holds
# ---------------------------------------------------------------------------
WAL_CHECKPOINT_MODES: tuple[str, ...] = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")


def _wal_frames(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """``(busy, log_frames, checkpointed_frames)`` from a PASSIVE checkpoint.

    PASSIVE is the one mode that never blocks a writer and never waits for a reader, so it is
    safe on the request path - and it is also the only way SQLite will tell an operator how long
    the journal currently is: there is no ``PRAGMA wal_size``. Using it to *report* means the
    report is a checkpoint as well, which is the honest trade rather than a hidden one.
    """
    try:
        row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    except sqlite3.Error:
        return (0, -1, -1)
    busy, log_frames, checkpointed = (int(value or 0) for value in row)
    return (busy, log_frames, checkpointed)


def db_stats() -> dict[str, Any]:
    """Contention, statement health and the journal - as far as one process can see them.

    **Per process, and the panel says so.** ``database.connection_stats`` counts statements,
    lock waits and timeouts in the process that recorded them; a deployment with several workers
    has several of these, and a number read from one of them is that worker's. What *is* shared
    is the database itself, which is why the journal facts and the pragmas beside the counters
    are worth more than the counters alone: they are the same answer from every worker.

    The pragmas are read first and the counters last on purpose - opening the connection this
    call needs is itself counted, so reading the counters afterwards would report a connection
    this endpoint had just made and call it load.
    """
    facts: dict[str, Any] = {
        "counters": dict(connection_stats()),
        "scope": "statement counters are per process; the pragmas below are the shared database's",
        # The modes ``db_wal_checkpoint`` accepts, so the console's select offers exactly the
        # four the lever takes. A mode spelled in the frontend instead would be a fifth mode the
        # day somebody edits one of the two lists, and the server answers that with a 400.
        "checkpoint_modes": list(WAL_CHECKPOINT_MODES),
    }
    connection = connect(isolation_level=None)
    try:
        pragmas: dict[str, Any] = {}
        for name, statement in (
            ("journal_mode", "PRAGMA journal_mode"),
            ("wal_autocheckpoint", "PRAGMA wal_autocheckpoint"),
            ("page_size", "PRAGMA page_size"),
            ("page_count", "PRAGMA page_count"),
            ("freelist_count", "PRAGMA freelist_count"),
            ("cache_size", "PRAGMA cache_size"),
            ("synchronous", "PRAGMA synchronous"),
            ("foreign_keys", "PRAGMA foreign_keys"),
        ):
            try:
                pragmas[name] = connection.execute(statement).fetchone()[0]
            except sqlite3.Error as exc:
                pragmas[name] = f"unreadable: {exc}"
        busy, log_frames, checkpointed = _wal_frames(connection)
        facts["pragmas"] = pragmas
        facts["wal"] = {
            "busy": busy,
            "pages_in_log": log_frames,
            "frames_checkpointed": checkpointed,
            "bytes_in_log": (
                log_frames * int(pragmas.get("page_size") or 0) if log_frames > 0 else 0
            ),
            "report_is_a_passive_checkpoint": True,
        }
    finally:
        connection.close()
    return facts


def db_wal_checkpoint(
    mode: str | None, *, actor: CurrentUser, ip: str | None = None
) -> dict[str, Any]:
    """Checkpoint the write-ahead log by hand, in the mode the caller names.

    TRUNCATE by default, because the question this exists for is "why is the journal 200 MB",
    and TRUNCATE is the only mode that hands the disk space back. It also *blocks writers* for
    as long as it takes to copy the log's live frames into the database - which is exactly why
    it is a lever an operator pulls deliberately, with the outcome on the audit trail, rather
    than something a monitoring job does on a schedule.

    Run on its own autocommit connection (``isolation_level=None``), because SQLite refuses a
    checkpoint inside a transaction, and ``database.connect`` is what the rest of the codebase
    reaches for when it needs one that is not managed by ``db()``.
    """
    wanted = str(mode or "TRUNCATE").strip().upper()
    if wanted not in WAL_CHECKPOINT_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode must be one of: {', '.join(WAL_CHECKPOINT_MODES)}."
        )
    connection = connect(isolation_level=None)
    try:
        before_busy, before_log, before_checkpointed = _wal_frames(connection)
        try:
            row = connection.execute(f"PRAGMA wal_checkpoint({wanted})").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=409, detail=f"the checkpoint failed: {exc}") from None
        busy, log_frames, checkpointed = (int(value or 0) for value in row)
    finally:
        connection.close()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db(write=True) as conn:
        _audit_developer(
            conn,
            action="db_wal_checkpoint",
            actor=actor,
            entity="database",
            entity_id=str(settings.database_path),
            before={"pages_in_log": before_log, "frames_checkpointed": before_checkpointed},
            after={
                "mode": wanted,
                "pages_in_log": log_frames,
                "frames_checkpointed": checkpointed,
            },
            ip=ip,
            created_at=stamp,
        )
    logger.warning(
        "wal checkpoint(%s) by %s: %s frame(s) in the log, %s checkpointed, busy=%s",
        wanted,
        actor.id,
        log_frames,
        checkpointed,
        busy,
    )
    return {
        "status": "success",
        "mode": wanted,
        "busy": busy,
        "log_frames": log_frames,
        "checkpointed_frames": checkpointed,
        "before_frames": before_log,
        "busy_before": before_busy,
        "at": stamp,
    }


def db_integrity() -> dict[str, Any]:
    """The schema against its baseline, the migrations against the build, and a full check.

    DETECT ONLY, AND THAT IS THE POINT
    ----------------------------------
    ``schema_guard.enforce`` exists and it *repairs* - it runs ``ensure_schema`` and the pending
    migrations against an already-serving database. A diagnostics GET must not be able to do
    that: the repair is startup's decision (it knows whether the deployment was forced up, and it
    records what it did), and a read route that silently migrated a database would make
    "who changed the schema" unanswerable. So this calls ``inspect``, which only compares.

    ``PRAGMA integrity_check`` is a full read of every page of the database - seconds on a large
    one, and it is reported the same way either way. Its companion ``PRAGMA foreign_keys`` is
    off by default in SQLite and this application never turns it on, so the check is about
    b-tree and index consistency rather than about every orphaned row; that is stated in the
    payload rather than left for the reader to assume.
    """
    import migrations
    import schema_guard

    report = schema_guard.inspect()
    with db() as conn:
        try:
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        except sqlite3.Error as exc:
            integrity = [f"unreadable: {exc}"]
        applied = sorted(migrations.applied_versions(conn))
        pending = migrations.pending_migrations(conn)
    return {
        "schema": report.as_dict(),
        "schema_version": {
            "expected": migrations.SCHEMA_VERSION,
            "current": applied[-1] if applied else 0,
            "applied": applied,
            "pending": [{"version": version, "name": name} for version, name in pending],
        },
        "integrity": {
            "ok": integrity == ["ok"],
            "output": integrity,
            "note": (
                "a full page-level check; PRAGMA foreign_keys is off in this deployment, so rows "
                "orphaned by a missing parent are not what this reports"
            ),
        },
    }


@router.get("/developer/db/stats")
async def developer_db_stats(current: CurrentUser = Depends(require_developer)):
    """Contention, statements and the journal. Root tier only."""
    return db_stats()


@router.post("/developer/db/wal-checkpoint")
async def developer_db_wal_checkpoint(
    request: Request,
    mode: str = Query("TRUNCATE", description="PASSIVE, FULL, RESTART or TRUNCATE"),
    current: CurrentUser = Depends(require_developer),
):
    """Checkpoint the write-ahead log by hand. Audited with both frame counts."""
    return db_wal_checkpoint(
        mode, actor=current, ip=request.client.host if request.client else None
    )


@router.get("/developer/db/integrity")
async def developer_db_integrity(current: CurrentUser = Depends(require_developer)):
    """Schema drift, applied migrations and a full integrity check. Root tier only."""
    return db_integrity()


# ---------------------------------------------------------------------------
# the engine process: the child that owns the models
# ---------------------------------------------------------------------------
def _rss_bytes(pid: int | None) -> int | None:
    """A process's resident set in bytes, read from the kernel; ``None`` where unavailable.

    Read from the parent side rather than asked for over the IPC boundary, for two reasons: the
    answer must not require the child to be answering at all (a child wedged inside an
    inference is exactly when an operator wants to know how large it is), and asking would run
    through the same pipe the punch path is queued behind. ``None`` on Windows and on any
    platform without ``/proc``: a made-up number would be worse than no number, which is the
    same judgement ``face_worker._self_rss`` makes on the child's side.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{int(pid)}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def engine_process_stats() -> dict[str, Any]:
    """The model child: whether one exists, what it holds, and whether it still answers.

    **A diagnostics call must not spawn one.** Building the child is the ~200 MiB the flag
    exists to move off the API process, and a monitoring route that spawned it on every scrape
    would put it back on the request path, unasked. So ``face_process.current`` is used - the
    accessor that does not build a client - and a deployment that has never taken a punch is
    reported as ``started: false`` rather than as a failure.

    A ping is only sent when the child is *alive*: ``FaceProcess.call`` starts a child that is
    not running, so pinging a dead one would quietly restart it and hide the death this panel
    is here to show. The restart count is the transport's ``starts``, which is the honest
    version of "how many times has this been replaced since the worker booted".
    """
    import face_engine
    import face_process

    if not face_process.enabled():
        return {
            "enabled": False,
            "message": "In-process inference active",
            "engine": face_engine.stats(),
        }
    transport = face_process.current()
    if transport is None:
        return {
            "enabled": True,
            "started": False,
            "message": "no model process has been needed since this worker started",
            "handler": str(face_process.WORKER),
        }
    view: dict[str, Any] = {"enabled": True, "started": True, **transport.stats()}
    pid = view.get("worker_pid") or view.get("pid")
    view["rss_pid"] = pid
    view["rss_bytes"] = _rss_bytes(pid)
    if view.get("alive"):
        started = time.perf_counter()
        try:
            pong = transport.ping()
        except Exception as exc:  # noqa: BLE001 - the failure *is* the reading
            view["ping_ms"] = None
            view["ping_error"] = f"{type(exc).__name__}: {exc}"
        else:
            view["ping_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            view["interpreter_pid"] = pong.get("pid")
            view["interpreter_python"] = pong.get("python")
            view["rss_bytes"] = _rss_bytes(pong.get("pid")) or view["rss_bytes"]
            view["rss_pid"] = pong.get("pid") or pid
    else:
        # Not pinged: a call would start a fresh child, and a panel must not be the thing that
        # decides a crash is over. The next real punch does; this reports what it would find.
        view["ping_ms"] = None
        view["ping_note"] = "the child is not running; a ping would start a replacement"
    return view


def restart_engine_worker(*, actor: CurrentUser, ip: str | None = None) -> dict[str, Any]:
    """Replace the model process with a fresh interpreter, by hand.

    The lever for the one failure the transport cannot see: a child that is alive, answering,
    and holding a working set it will never give back (an unbound embedding of a hostile image,
    a detector arena sized by the largest frame ever shown). Nothing else can free that - the
    only way is a new interpreter - which is why this is a manual action with an audit row
    rather than a policy the server applies to itself.

    A child that was not running is started rather than refused: the operator's intent ("have a
    healthy model process") is the same either way, and ``old_pid`` is ``null`` when there was
    nothing to stop. The new child is pinged before the answer goes out, which is also how the
    transport learns which interpreter really runs the models on a Windows virtualenv.
    """
    import face_process

    if not face_process.enabled():
        raise HTTPException(
            status_code=409,
            detail=(
                "this deployment runs the face models in-process (FACE_ENGINE_PROCESS is off), so "
                "there is no model process to restart"
            ),
        )
    transport = face_process.client()
    before = transport.stats()
    transport.restart()
    after = transport.stats()
    interpreter: int | None = None
    ping_error: str | None = None
    try:
        interpreter = transport.ping().get("pid")
    except Exception as exc:  # noqa: BLE001 - reported in the answer, audited as the outcome
        ping_error = f"{type(exc).__name__}: {exc}"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db(write=True) as conn:
        _audit_developer(
            conn,
            action="engine_worker_restart",
            actor=actor,
            entity="face_process",
            entity_id=str(before.get("handler") or "face_worker"),
            before={
                "pid": before.get("pid"),
                "worker_pid": before.get("worker_pid"),
                "starts": before.get("starts"),
            },
            after={"pid": after.get("pid"), "worker_pid": interpreter, "ping_error": ping_error},
            ip=ip,
            created_at=stamp,
        )
    logger.warning(
        "face model process restarted by %s: pid %s -> %s",
        actor.id,
        before.get("pid"),
        after.get("pid"),
    )
    return {
        "status": "success",
        "old_pid": before.get("pid"),
        "new_pid": after.get("pid"),
        "old_worker_pid": before.get("worker_pid"),
        "new_worker_pid": interpreter,
        "ping_error": ping_error,
        "restarted_at": stamp,
    }


@router.get("/developer/engine/process-stats")
async def developer_engine_process_stats(current: CurrentUser = Depends(require_developer)):
    """The model child's memory, liveness and ping latency. Root tier only."""
    return engine_process_stats()


@router.post("/developer/engine/restart-worker")
async def developer_engine_restart(
    request: Request, current: CurrentUser = Depends(require_developer)
):
    """Replace the model process. Refused when there is no separate process to replace."""
    return restart_engine_worker(
        actor=current, ip=request.client.host if request.client else None
    )


# ---------------------------------------------------------------------------
# offline forensics: devices, anchors, and a clock that was wound back
# ---------------------------------------------------------------------------
#: The rejection codes that mean *tampering or a broken clock* rather than "this photo is not
#: you". Named here as the vocabulary this surface filters on, and returned in the answer so a
#: reader can tell which rejections are in the list from which are merely rejected.
TAMPER_CODES: tuple[str, ...] = (
    "clock_tampered",
    "invalid_monotonic_offset",
    "bad_signature",
    "replayed_nonce",
)

#: How far back the anchor ledger reaches by default. Three days is the window the offline
#: protocol itself reasons about (``offline_sync`` bounds a monotonic offset at 72 h), so the
#: ledger and the rule it exists to check are the same length.
ANCHOR_WINDOW_HOURS = 72


def _mask_salt(value: Any) -> dict[str, Any] | None:
    """A device's key salt, as much of it as a ledger needs: the first six characters.

    The salt is key material, not a label, and the question this ledger answers about a device is
    *which one* - the first six characters are enough to tell two rows apart in a support
    conversation and not enough to derive anything. The length travels with it so an operator can
    see the difference between a salt that is short and a mask that is hiding one.
    """
    if value is None or value == "":
        return None
    text = str(value)
    return {"prefix": text[:6], "length": len(text)}


def offline_devices(*, hours: int = ANCHOR_WINDOW_HOURS) -> dict[str, Any]:
    """Every signing device, and the anchors issued in the window, consumed or not.

    The device row is the *trust* half of the offline protocol and the anchor is the *time*
    half. What an operator needs from the pair is the shape of the ledger rather than the rows:
    devices that were revoked but are still anchoring, anchors issued and never used (a device
    that asked for time and never came back - a phone that died, or one being held), and the
    ``last_anchor_at`` that says which devices are still alive. ``key_salt`` is masked; the
    epoch, the revocation and the timestamps are not, because they are what the ledger is for.
    """
    window = max(1, min(int(hours), 24 * 30))
    cutoff = (datetime.now() - timedelta(hours=window)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        devices = conn.execute(
            """
            SELECT d.id, d.device_id, d.worker_id, u.name AS worker_name, u.role AS worker_role,
                   u.status AS worker_status, d.key_epoch, d.key_salt, d.created_at,
                   d.last_seen_at, d.revoked_at, d.last_anchor_at
            FROM worker_devices d
            LEFT JOIN users u ON d.worker_id = u.id
            ORDER BY d.id DESC LIMIT 500
            """
        ).fetchall()
        anchors = conn.execute(
            """
            SELECT COUNT(*) AS issued,
                   SUM(consumed_by IS NULL) AS unconsumed,
                   SUM(consumed_by IS NOT NULL) AS consumed
            FROM device_anchors WHERE issued_at >= ?
            """,
            (cutoff,),
        ).fetchone()
    items = []
    for row in devices:
        item = dict(row)
        item["key_salt"] = _mask_salt(item.pop("key_salt", None))
        items.append(item)
    return {
        "window_hours": window,
        "since": cutoff,
        "devices": items,
        "anchors": {
            "issued": int(anchors["issued"] or 0),
            "unconsumed": int(anchors["unconsumed"] or 0),
            "consumed": int(anchors["consumed"] or 0),
        },
        "note": (
            "key_salt is masked to its first six characters: the ledger's question is which "
            "device, not what its key material is"
        ),
    }


def offline_tamper_alerts(
    *, limit: int = 50, worker_id: str | None = None, days: int = 7
) -> dict[str, Any]:
    """Clock skew and signature refusals from the queue, with the discrepancy worked out.

    WHY THIS IS NOT THE ADMINISTRATOR'S PUNCH QUEUE
    ----------------------------------------------
    ``/admin/punch_queue`` is a *site's* triage: which flagged punch should become attendance.
    This is the other question, and it is not about a site at all - a device whose monotonic
    offset disagrees with the anchor it was issued, a signature that does not verify, a nonce
    replayed twice. Those are answers about the protocol, and the only person who can act on one
    is the one who can revoke the device or change the clock rule.

    The discrepancy is *derived here* in Python rather than in SQL, because the arithmetic that
    matters is the one ``offline_sync`` already owns: ``effective_timestamp`` is the server's
    own rule for turning an anchor plus a monotonic offset into the time a punch really
    happened, and a second expression of it in a SELECT would be a second answer to "what time
    was this" - which is the value a resolution decision is made on. The wall-clock claim
    beside it (``skew_seconds``) is that effective time subtracted from what the device said,
    which is the number to look at first: a hundred seconds is a phone with a wrong timezone, a
    hundred thousand is a clock that was moved.
    """
    import offline_sync

    window = max(1, min(int(days), 90))
    cutoff = (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" for _ in TAMPER_CODES)
    query = f"""
        SELECT p.id, p.client_punch_id, p.device_id, p.worker_id, u.name AS worker_name,
               p.action, p.client_timestamp, p.anchor_server_time, p.monotonic_offset_s,
               p.client_offset_s, p.status, p.rejection_code, p.flag_reason,
               p.signature_version, p.received_at, p.materialized_log_id
        FROM punch_queue p
        LEFT JOIN users u ON p.worker_id = u.id
        WHERE (p.status = 'rejected' OR p.rejection_code IN ({placeholders}))
          AND p.received_at >= ?
    """
    params: list[Any] = [*TAMPER_CODES, cutoff]
    if worker_id:
        query += " AND p.worker_id = ?"
        params.append(str(worker_id))
    query += " ORDER BY p.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["tamper"] = str(item.get("rejection_code") or "") in TAMPER_CODES
        item["effective_time"] = None
        item["skew_seconds"] = None
        anchored = item.get("anchor_server_time") is not None
        if anchored and item.get("monotonic_offset_s") is not None:
            try:
                derived = offline_sync.effective_timestamp(
                    item["anchor_server_time"], float(item["monotonic_offset_s"])
                )
            except (TypeError, ValueError):
                derived = None
            if derived is not None:
                item["effective_time"] = derived.strftime("%Y-%m-%d %H:%M:%S")
                claimed = offline_sync.parse_ts(item.get("client_timestamp"))
                if claimed is not None:
                    item["skew_seconds"] = round((claimed - derived).total_seconds(), 1)
        items.append(item)
    return {
        "window_days": window,
        "tamper_codes": list(TAMPER_CODES),
        "counts": {
            "in_window": len(items),
            "tamper": sum(1 for item in items if item["tamper"]),
            "with_an_anchor": sum(1 for item in items if item["effective_time"]),
        },
        "alerts": items,
        "note": (
            "a rejected offline punch was never materialised as attendance; these rows are "
            "evidence about a device, not a queue to approve"
        ),
    }


@router.get("/developer/offline/devices")
async def developer_offline_devices(
    hours: int = Query(ANCHOR_WINDOW_HOURS, ge=1, le=24 * 30),
    current: CurrentUser = Depends(require_developer),
):
    """The signing-device ledger and its anchors. Root tier only."""
    return offline_devices(hours=hours)


@router.get("/developer/offline/tamper-alerts")
async def developer_offline_tamper_alerts(
    limit: int = Query(50, ge=1, le=500),
    days: int = Query(7, ge=1, le=90),
    worker_id: str | None = None,
    current: CurrentUser = Depends(require_developer),
):
    """Clock skew, bad signatures and replay attempts. Root tier only."""
    return offline_tamper_alerts(limit=limit, days=days, worker_id=worker_id)


# ---------------------------------------------------------------------------
# the manual database snapshot
# ---------------------------------------------------------------------------
BACKUP_PREFIX = "backup_manual_dev"


def _snapshot_path(directory: Path, stamp: str) -> Path:
    """``<prefix>_<stamp>.db``, with a numeric suffix if that second is already taken.

    Refusing would be worse than a suffix: an operator clicking the button twice has asked for
    two snapshots, and a snapshot written over a name that already exists is not a snapshot at
    all - ``sqlite3.connect`` opens the existing file and the backup *into* it would replace a
    copy somebody may already be relying on.
    """
    candidate = directory / f"{BACKUP_PREFIX}_{stamp}.db"
    counter = 1
    while candidate.exists():  # pragma: no cover - needs two calls inside one second
        counter += 1
        candidate = directory / f"{BACKUP_PREFIX}_{stamp}_{counter}.db"
    return candidate


def _do_snapshot(source: Path, destination: Path) -> None:
    """Copy a live database with SQLite's own online backup API.

    WHY NOT A FILE COPY
    -------------------
    The database runs in WAL mode, so the newest commits are in a ``-wal`` file beside it: a
    ``shutil.copy2`` of the ``.db`` alone can capture a state that never existed, and it takes
    no lock while doing it, so a writer can move pages under the copy. ``Connection.backup``
    reads through SQLite instead - it takes the locks it needs, copies page by page, and
    restarts the copy when a writer changes a page it has already read - which is what makes
    the result a consistent point-in-time snapshot rather than a hopeful one.

    Runs on a worker thread (``run_in_threadpool`` at the endpoint): this is seconds to minutes
    of blocking file I/O on a database that grows, and this process has one event loop with a
    gate's punches behind it.
    """
    source_conn = sqlite3.connect(str(source), timeout=30.0)
    destination_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()


def _verify_snapshot(path: Path) -> str:
    """``PRAGMA quick_check`` on a fresh copy: ``ok``, or what is wrong with it. Never raises.

    The snapshot is verified before it is reported, because a backup nobody has opened is a
    belief rather than a fact - and this is the copy an operator will reach for *after* the
    change that went wrong. ``quick_check`` rather than ``integrity_check``: it skips the
    index-versus-table comparison, which is the half a snapshot cannot have broken, and stays
    cheap on a database large enough for the backup to have taken a while.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
        try:
            rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"unreadable: {exc}"
    return "ok" if rows == ["ok"] else "; ".join(rows[:5])


def create_database_backup(*, actor: CurrentUser, ip: str | None = None) -> dict[str, Any]:
    """Take a timestamped, consistent copy of the live database, and verify it.

    WHAT THIS IS NOT
    ----------------
    ``tools/backup.py`` makes the *project* snapshot an operator wants before a deploy - the
    database, the source tree, the biometric assets, the interpreter's own freeze, all hashed
    into a manifest a later run can verify. That is the right tool for a planned change. This
    is the one-call version of the part an operator needs in a hurry, from a shell that is
    already open, in the middle of an incident: the database, as it is, right now.

    The destination is ``settings.backup_dir`` (created if it is not there), and the result is
    reported *and* audited with its size - a snapshot is only useful if somebody can find it
    later, which is what the audit row is for.
    """
    source = resolve_path(Path(settings.database_path))
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = _snapshot_path(directory, stamp)

    started = time.perf_counter()
    _do_snapshot(source, destination)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    size_bytes = destination.stat().st_size
    verification = _verify_snapshot(destination)

    with db(write=True) as conn:
        _audit_developer(
            conn,
            action="dev_backup_created",
            actor=actor,
            entity="database",
            entity_id=str(destination),
            before={"database": str(source)},
            after={
                "file": str(destination),
                "size_bytes": size_bytes,
                "verified": verification,
                "took_ms": elapsed_ms,
            },
            ip=ip,
        )
    if verification != "ok":
        # Kept rather than deleted, and raised rather than merely reported: the bytes may still
        # be recoverable by somebody who knows SQLite, and a snapshot that failed its own check
        # is the one event on this surface that an operator must not have to go looking for.
        raise_alert(
            kind="backup_unverified",
            summary=f"manual snapshot {destination.name} failed quick_check",
            severity="critical",
            source="developer",
            detail={"file": str(destination), "verification": verification},
            dedupe_window_seconds=None,
        )
        logger.error("manual database snapshot is not a usable database: %s", verification)
    return {
        "status": "success",
        "file": str(destination),
        "size_bytes": size_bytes,
        "verified": verification,
        "database": str(source),
        "took_ms": elapsed_ms,
    }


@router.post("/developer/db/backup")
async def developer_db_backup(
    request: Request, current: CurrentUser = Depends(require_developer)
):
    """Snapshot the live database with SQLite's online backup API. Root tier only."""
    return await run_in_threadpool(
        create_database_backup,
        actor=current,
        ip=request.client.host if request.client else None,
    )


# ---------------------------------------------------------------------------
# the project snapshot: everything, hashed into a manifest a later run can check
# ---------------------------------------------------------------------------
#: What a snapshot taken from this console is called. Deliberately not ``BACKUP_PREFIX`` above,
#: which names the single-file database copies: an operator looking at the directory has to be
#: able to tell a verified whole-project snapshot from a quick copy of one file, and the name is
#: the first place that distinction can be made.
SNAPSHOT_PREFIX = "manual_dev"

#: How a timestamp is written on this surface. One format for every row: the tool's own
#: ``VERIFY.json`` carries ISO, the file timestamps do not, and a list where two rows read as two
#: different formats is a list an operator has to decode before comparing.
SNAPSHOT_STAMP = "%Y-%m-%d %H:%M:%S"

#: A name this console will write into, or read out of, the backup directory.
#:
#: The prefix reaches ``tools/backup.py``, which joins it onto the destination with a path
#: separator - so a prefix of ``../../etc`` would have written a snapshot outside the backup
#: directory altogether, and a ``name`` on the verify route is a path segment out of a URL. Only
#: names are accepted: no separators, no leading dot, nothing a shell or a path could read as
#: anything other than a name.
_SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")

#: One snapshot at a time.
#:
#: A snapshot copies the source tree, the database and (by default) every face template, then
#: hashes all of it: on the one vCPU this deployment is sized for, that is the largest thing this
#: process can be asked to do. Two at once would fight over the same disk and the same single
#: writer, and the second would tell an operator "done" about a copy that was still being
#: written. A second click gets an answer instead of a second snapshot.
_SNAPSHOT_LOCK = threading.Lock()


class SnapshotRequest(BaseModel):
    """What one click of the console's snapshot button carries."""

    prefix: str | None = Field(
        default=None, description="The name the snapshot's directory starts with."
    )
    include_assets: bool = Field(
        default=True,
        description=(
            "Whether to copy the face templates, the worker photographs and the "
            "certificates. On: a snapshot that cannot restore an enrolled worker's face is not "
            "a snapshot of this deployment."
        ),
    )
    note: str | None = Field(
        default=None, max_length=200, description="Why, for the audit trail."
    )


def _mtime(path: Path) -> float:
    """When this was last written, or ``0.0`` when it cannot be read."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _directory_size(path: Path) -> int:
    """Bytes on disk under ``path``, walked without following anything out of the tree.

    A snapshot's size is a directory's, not a file's, and this is the number an operator is
    really asking for when they wonder whether the disk will hold the next one. Symlinks are not
    followed - a link out of the backup directory is not bytes this deployment is holding.
    """
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:  # pragma: no cover - a file removed mid-walk
                    continue
    return total


def _snapshot_folder(directory: Path, name: str) -> dict[str, Any]:
    """One directory in the backup directory, described by what it actually says.

    Read from ``VERIFY.json`` - the file ``tools/backup.py`` writes as its own verdict - rather
    than from a conclusion this module reached. A directory with no ``VERIFY.json`` is reported
    as *unverified* rather than hidden: an interrupted snapshot, or one somebody copied in by
    hand, is exactly what an operator needs to see before trusting the row above it.
    """
    folder = directory / name
    marker = folder / "VERIFY.json"
    written = _mtime(marker) or _mtime(folder)
    entry: dict[str, Any] = {
        "name": name,
        "kind": "unverified",
        "path": str(folder),
        "size_bytes": _directory_size(folder),
        "created_at": datetime.fromtimestamp(written).strftime(SNAPSHOT_STAMP),
        "age_hours": round((time.time() - written) / 3600.0, 2),
        "status": None,
        "prefix": None,
        "files": None,
        "integrity_check": None,
        "db_sha256": None,
        "verifiable": False,
        "notes": [],
    }
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        entry["notes"].append("no readable VERIFY.json")
        return entry
    created = _parse_stamp(str(payload.get("created_at") or ""))
    entry.update(
        kind="snapshot",
        status=str(payload.get("status") or "UNKNOWN"),
        prefix=str(payload.get("prefix") or ""),
        files=int(payload.get("files") or 0),
        integrity_check=payload.get("integrity_check"),
        db_sha256=str(payload.get("db_sha256") or ""),
        verifiable=True,
        notes=[str(note) for note in payload.get("notes") or []],
    )
    if created is not None:
        # The tool's own timestamp, which is when the snapshot was *taken* rather than when its
        # manifest happened to be written - the two agree for a snapshot this console wrote, and
        # disagree by however long a hand-moved directory has been sitting there.
        entry["created_at"] = created.strftime(SNAPSHOT_STAMP)
        entry["age_hours"] = round((datetime.now() - created).total_seconds() / 3600.0, 2)
    return entry


def _parse_stamp(stamp: str) -> datetime | None:
    """A ``VERIFY.json`` timestamp back into a ``datetime``, or ``None`` when it is not one."""
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _database_copy(directory: Path, path: Path) -> dict[str, Any]:
    """A single-file copy of the database, the kind the one-call hook above writes.

    No manifest and no verdict: the copy was checked when it was written (``quick_check``), and
    re-reading every file in the backup directory on every paint is not a thing a list view is
    allowed to do. This is why it is listed under its own kind rather than as a snapshot - the
    two answer different questions, and only one of them has a manifest behind it.
    """
    written = _mtime(path)
    try:
        size = path.stat().st_size
    except OSError:  # pragma: no cover - removed between the walk and the read
        size = 0
    return {
        "name": path.name,
        "kind": "database",
        "path": str(path),
        "size_bytes": size,
        "created_at": datetime.fromtimestamp(written).strftime(SNAPSHOT_STAMP),
        "age_hours": round((time.time() - written) / 3600.0, 2),
        "status": None,
        "prefix": None,
        "files": 1,
        "integrity_check": None,
        "db_sha256": None,
        "verifiable": False,
        "notes": ["a single-file copy of the database, with no manifest to check it against"],
    }


def list_backups(*, limit: int = 50) -> dict[str, Any]:
    """What is in the backup directory, newest first.

    Both kinds, because both are in there and an operator deciding what to restore from has to be
    able to see the difference: the **snapshots** this console writes (source, database, assets,
    a manifest and a verdict) and the **database copies** the one-call hook writes. Files that
    are neither are left out - a backup directory with stray files in it is not a list of
    backups - and a directory with no verdict is reported as *unverified* rather than skipped.

    ``limit`` is applied after the sort, so the newest are the ones that survive it: truncating
    "the oldest twenty" and "the twenty newest" are different answers, and only one of them is
    useful.
    """
    directory = Path(settings.backup_dir)
    found: list[dict[str, Any]] = []
    if directory.is_dir():
        for child in directory.iterdir():
            try:
                if child.is_dir():
                    found.append(_snapshot_folder(directory, child.name))
                elif child.is_file() and child.suffix.lower() == ".db":
                    found.append(_database_copy(directory, child))
            except OSError:  # pragma: no cover - a directory removed mid-walk
                continue
    found.sort(key=lambda item: float(item["age_hours"] if item["age_hours"] is not None else 1e12))
    items = found[: max(1, int(limit))]
    return {
        "status": "success",
        "directory": str(directory),
        "exists": directory.is_dir(),
        "prefix": SNAPSHOT_PREFIX,
        "items": items,
        "count": len(items),
        "total": len(found),
        "snapshots": sum(1 for item in found if item["kind"] == "snapshot"),
        "databases": sum(1 for item in found if item["kind"] == "database"),
        "unverified": sum(1 for item in found if item["kind"] == "unverified"),
        "total_bytes": sum(int(item["size_bytes"]) for item in found),
        "newest": found[0] if found else None,
    }


def _write_snapshot(
    name: str, directory: Path, *, include_assets: bool
) -> tuple[Path, dict[str, Any], str]:
    """Write one snapshot with the project's own tool, and return it with its verdict.

    ``tools/backup.py`` is *the* snapshot tool - the database through ``VACUUM INTO`` (atomic and
    correct while the server is running), the source tree, the assets, the interpreter's freeze,
    and a ``MANIFEST.sha256`` a later run can check. This does not reimplement any of it: it
    calls it, then runs the tool's own verifier over the result, because "the snapshot was
    written" and "the snapshot is intact" are the two halves the tool exists to keep apart.

    Imported here rather than at module import, on purpose: ``tools/`` is the operator's
    directory and the application must not refuse to *start* because a deployment pruned it - a
    console that cannot take a snapshot should answer that it cannot, at the moment somebody
    asks, rather than fail to boot.
    """
    from tools import backup as project_backup

    database = resolve_path(Path(settings.database_path))
    destination: Path | None = None
    written_prefix = name
    for attempt in range(1, 11):
        # The tool names the directory ``<prefix>_<stamp>``, and the stamp is whole seconds: two
        # clicks inside one second would otherwise be one snapshot and one ``FileExistsError`` -
        # which is exactly what an operator clicking twice has *not* asked for.
        candidate = name if attempt == 1 else f"{name}_{attempt}"
        written_prefix = candidate
        try:
            destination = project_backup.create_snapshot(
                candidate,
                db_path=database,
                backup_dir=directory,
                include_assets=include_assets,
                quiet=True,
            )
            break
        except FileExistsError:
            continue
    if destination is None:  # pragma: no cover - ten collisions inside one second
        raise HTTPException(
            status_code=409,
            detail="Ten snapshots already exist for this second. Wait a moment and try again.",
        )
    report = project_backup.verify_snapshot(destination)
    return destination, report, written_prefix


def create_project_snapshot(
    *,
    actor: CurrentUser,
    ip: str | None = None,
    prefix: str | None = None,
    include_assets: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """Take a verified whole-project snapshot, and say what was written.

    THE TWO ANSWERS THIS GIVES
    --------------------------
    **Written** and **intact** are separate facts, and both are reported. A snapshot that was
    written and then failed its own verification is not deleted: the bytes may still be
    recoverable by somebody who knows SQLite, and a half-lie about it is worse than a file
    somebody can look at. It is kept, it is listed as ``FAIL``, and it raises a critical alert -
    this tier is the one an operator has to not have to go looking for.

    Blocking work on a worker thread, and one at a time: see ``_SNAPSHOT_LOCK``.
    """
    name = str(prefix or SNAPSHOT_PREFIX).strip() or SNAPSHOT_PREFIX
    if not _SNAPSHOT_NAME.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "bad_snapshot_name",
                "message": (
                    "A snapshot's name may contain letters, digits, dots, dashes and "
                    "underscores only."
                ),
            },
        )
    if not _SNAPSHOT_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "snapshot_in_progress",
                "message": (
                    "A snapshot is already being written. It is the heaviest thing this "
                    "deployment does; wait for it to finish."
                ),
            },
        )
    # Created only once the request is going to happen: a refused click must not leave an empty
    # directory in a listing an operator reads to decide what exists.
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        try:
            destination, report, written_prefix = _write_snapshot(
                name, directory, include_assets=include_assets
            )
        except Exception as exc:  # noqa: BLE001 - whatever the tool refused to do
            with db(write=True) as conn:
                _audit_developer(
                    conn,
                    action="dev_snapshot_failed",
                    actor=actor,
                    entity="backup",
                    entity_id=name,
                    before={"database": str(settings.database_path)},
                    after={"error": str(exc), "include_assets": bool(include_assets)},
                    ip=ip,
                )
            raise_alert(
                kind="snapshot_failed",
                summary=f"snapshot {name} could not be written: {exc}",
                severity="critical",
                source="developer",
                detail={"directory": str(directory), "error": str(exc)},
                dedupe_window_seconds=None,
            )
            logger.error("project snapshot could not be written: %s", exc)
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "snapshot_failed",
                    "message": (
                        f"The snapshot could not be written: {exc}. Anything it had already "
                        f"copied is still in {directory} and is listed as unverified."
                    ),
                },
            ) from exc
    finally:
        _SNAPSHOT_LOCK.release()

    took_ms = round((time.perf_counter() - started) * 1000.0, 1)
    verified = str(report.get("status") or "FAIL")
    errors = [str(item) for item in report.get("errors") or []]
    result = {
        "status": "success",
        "directory": str(destination),
        "name": destination.name,
        # What was actually written, not what was asked for: a second snapshot inside the same
        # second is written under ``<name>_2``, and the list reads the directory's own prefix.
        "prefix": written_prefix,
        "database": str(resolve_path(Path(settings.database_path))),
        "include_assets": bool(include_assets),
        "files": int(report.get("files_listed") or 0),
        "files_checked": int(report.get("files_checked") or 0),
        "size_bytes": _directory_size(destination),
        "verified": verified,
        "integrity_check": report.get("integrity_check"),
        "errors": errors,
        "took_ms": took_ms,
    }
    with db(write=True) as conn:
        _audit_developer(
            conn,
            action="dev_snapshot_created" if verified == "PASS" else "dev_snapshot_failed",
            actor=actor,
            entity="backup",
            entity_id=str(destination),
            before={"database": result["database"]},
            after={
                "name": destination.name,
                "files": result["files"],
                "size_bytes": result["size_bytes"],
                "verified": verified,
                "include_assets": bool(include_assets),
                "took_ms": took_ms,
                "errors": errors[:5],
                "note": note or None,
            },
            ip=ip,
        )
    if verified != "PASS":
        raise_alert(
            kind="snapshot_unverified",
            summary=f"snapshot {destination.name} failed verification: {'; '.join(errors[:2])}",
            severity="critical",
            source="developer",
            detail={"directory": str(destination), "errors": errors[:5]},
            dedupe_window_seconds=None,
        )
        logger.error("project snapshot failed verification: %s", errors[:5])
    return result


def _snapshot_child(directory: Path, name: str) -> Path:
    """The snapshot directory ``name``, or a refusal - never a path out of the backup directory."""
    if not _SNAPSHOT_NAME.fullmatch(str(name or "")):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "bad_snapshot_name",
                "message": "A snapshot's name may contain letters, digits, dots, dashes and underscores only.",
            },
        )
    candidate = directory / name
    if candidate.resolve().parent != directory.resolve() or not candidate.is_dir():
        raise HTTPException(status_code=404, detail="No such snapshot in the backup directory.")
    return candidate


def verify_stored_snapshot(*, name: str) -> dict[str, Any]:
    """Re-check one stored snapshot against its own manifest.

    Re-*hashes* every file the manifest lists and reads the snapshot's database, rather than
    repeating the verdict ``VERIFY.json`` already carries: the stored verdict is what was true
    when the snapshot was written, and the question a month later is whether the bytes are still
    those bytes. A file that moved, a disk that rotted, a hand edit - this is the only place any
    of them is caught before the restore that needs them.

    Not audited: it changes nothing. It is the *reading* of the records rather than an act on
    them, and every file in the backup directory was written by this tier already.
    """
    directory = Path(settings.backup_dir)
    target = _snapshot_child(directory, name)
    from tools import backup as project_backup

    started = time.perf_counter()
    report = project_backup.verify_snapshot(target)
    report["name"] = target.name
    report["size_bytes"] = _directory_size(target)
    report["took_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return report


@router.get("/developer/db/backups")
async def developer_db_backups(
    limit: int = Query(50, ge=1, le=500), current: CurrentUser = Depends(require_developer)
) -> dict[str, Any]:
    """What is in the backup directory: snapshots, database copies, newest first."""
    return await run_in_threadpool(list_backups, limit=limit)


@router.post("/developer/db/snapshot")
async def developer_db_snapshot(
    request: Request,
    payload: SnapshotRequest | None = None,
    current: CurrentUser = Depends(require_developer),
) -> dict[str, Any]:
    """Take a verified project snapshot with ``tools/backup.py``. Root tier only."""
    body = payload or SnapshotRequest()
    return await run_in_threadpool(
        create_project_snapshot,
        actor=current,
        ip=request.client.host if request.client else None,
        prefix=body.prefix,
        include_assets=body.include_assets,
        note=body.note,
    )


@router.post("/developer/db/backups/{name}/verify")
async def developer_db_backup_verify(
    name: str, current: CurrentUser = Depends(require_developer)
) -> dict[str, Any]:
    """Re-hash a stored snapshot against its manifest. Root tier only."""
    return await run_in_threadpool(verify_stored_snapshot, name=name)


# ---------------------------------------------------------------------------
# the geofence probe: why a worker at the site was told they were not
# ---------------------------------------------------------------------------
class GeoProbe(BaseModel):
    lat: float = Field(..., description="The candidate latitude, as a client reported it.")
    lon: float = Field(..., description="The candidate longitude, as a client reported it.")
    site_name: str = Field(
        ..., min_length=1, description="Which site's perimeter to compare against."
    )


def geo_test_point(*, lat: float, lon: float, site_name: str) -> dict[str, Any]:
    """Distance from one point to one site's perimeter, with the two mistakes costed.

    The refusal this exists to explain is ``outside_geofence``: a worker standing on the site is
    told they are not at it. There are three ways that happens, and the answer says which one:

    * the coordinates really are outside the radius - ``margin_meters`` is negative, and by how
      much is the number that decides whether the site's radius or the phone's GPS is wrong;
    * the two were sent **the other way round** - ``swapped_distance_meters`` is the distance
      that mistake would produce, and a worker whose reported pair is thousands of kilometres
      away while the swapped one is metres away has answered the question already;
    * the pair is not on Earth at all - ``coordinates_plausible`` is the application's own
      coordinate check (``main.validate_plausible_coordinates``), reported rather than raised
      because a probe is exactly where an implausible pair should stay inspectable.

    A probe, with no side effects: nothing is written, no shift is consulted, and no attendance
    row is created. What it also cannot tell you is whether a *punch* would be accepted here -
    the gate also weighs the reported accuracy and the location-trust decision, which is a
    different question with the same distance in it.
    """
    import main

    with db() as conn:
        site = conn.execute(
            "SELECT site_name, lat, lon, radius FROM construction_sites WHERE site_name = ?",
            (str(site_name),),
        ).fetchone()
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")

    site_lat, site_lon = float(site["lat"]), float(site["lon"])
    radius = float(site["radius"])
    distance = main.get_distance_meters(site_lat, site_lon, lat, lon)
    swapped = main.get_distance_meters(site_lat, site_lon, lon, lat)
    try:
        main.validate_plausible_coordinates(lat, lon)
    except HTTPException as exc:
        plausible, complaint = False, str(exc.detail)
    else:
        plausible, complaint = True, None
    return {
        "site_name": str(site["site_name"]),
        "target_coordinates": {"lat": lat, "lon": lon},
        "site_center": {"lat": site_lat, "lon": site_lon},
        "site_radius_meters": radius,
        "calculated_distance_meters": round(distance, 2),
        "inside_geofence": distance <= radius,
        "margin_meters": round(radius - distance, 2),
        # The two diagnoses the distance alone cannot separate.
        "swapped_distance_meters": round(swapped, 2),
        "inside_geofence_if_swapped": swapped <= radius,
        "coordinates_plausible": plausible,
        "coordinates_complaint": complaint,
        "note": (
            "geometry only: a punch at this point is also weighed against the reported accuracy "
            "and the device's location trust, and this probe creates no attendance row"
        ),
    }


@router.post("/developer/geo/test-point")
async def developer_geo_test_point(
    probe: GeoProbe, current: CurrentUser = Depends(require_developer)
):
    """Distance from one coordinate to one site, with the swapped and implausible cases costed."""
    return geo_test_point(lat=probe.lat, lon=probe.lon, site_name=probe.site_name)


# ---------------------------------------------------------------------------
# the biometric re-index
# ---------------------------------------------------------------------------
#: The staleness reasons a re-embedding from the stored selfie can *prove* it fixed, and the
#: sentences for the ones it cannot.
#:
#: The dividing line is whether a distance between the old vector and the new one means
#: anything. ``no_provenance`` and ``legacy_template_name`` are records whose *embedding* may
#: be perfectly current - a file written before provenance was recorded, or filed under the
#: account id the old scheme used - so the re-derivation is measured against them, under the
#: live band, and only written when that measurement says "the same face". A different
#: *pipeline* or *model* is the opposite case: the crop or the vector space moved, so a
#: distance between the two is not a same-person distance under any line this build has
#: derived - it is two numbers that happen to have the same units. ``biometrics`` says the same
#: thing in ``stale_reason``, and the fix it names is one photograph, which costs far less than
#: a cohort of templates that match nobody.
#: A test holds these names to ``biometrics.STALE_*``, so a reason renamed there cannot leave
#: this table answering a question nobody asks any more.
REINDEX_VERIFIABLE: frozenset[str] = frozenset(
    {"no_provenance", "legacy_template_name"}
)
REINDEX_NEEDS_PHOTO: dict[str, str] = {
    "unreadable": (
        "the stored template cannot be read, or its width is not the one this pipeline "
        "produces, so there is nothing on disk to check a re-embedding against"
    ),
    "other_pipeline": (
        "the stored template was made from a different crop; a distance between that vector and "
        "one made from this crop is not a same-person distance under any line this build "
        "derived, so the face has to be taken again"
    ),
    "other_model": (
        "the stored template was made by a different recognition model, in another vector "
        "space; no line in force is about it, so the face has to be taken again"
    ),
}

#: How many accounts one run may touch. A re-index is one decode and one model call per
#: worker, which on this deployment's single vCPU is seconds each - so this is a batching
#: control, not a throttle, and the default is deliberately a batch rather than the workforce.
REINDEX_MAX_LIMIT = 2000


def _reindex_worklist(*, limit: int) -> list[dict[str, Any]]:
    """The enrolled, active accounts a re-index would look at, oldest id first.

    ``enrolled_at IS NOT NULL`` is the population the spec of a re-index is about: an account
    with no template has nothing to re-write and is not stale, it is unenrolled, and the roster
    already says so.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, biometric_id, COALESCE(template_version, 0) AS template_version "
            "FROM users WHERE enrolled_at IS NOT NULL AND biometric_id IS NOT NULL "
            "AND biometric_id <> '' "
            "AND COALESCE(NULLIF(status, ''), 'active') = 'active' "
            "ORDER BY CAST(id AS INTEGER) ASC LIMIT ?",
            (max(1, min(int(limit), REINDEX_MAX_LIMIT)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _prepare_reindex(
    user_id: str, biometric_id: str | None, *, include_current: bool = False
) -> dict[str, Any]:
    """Everything a re-index needs from the disk for one account, off the event loop.

    The order matters, twice. The template is read first (a small JSON), because an account whose
    template is *not* stale has nothing to re-index and must not cost a JPEG decode - which is
    the difference between a dry run over the workforce being a few seconds and being minutes.
    And a reason that a photograph cannot settle is answered *before* the disk is asked whether
    there is one: for those accounts "there is no selfie" names a fact the operator cannot act on
    and hides the one they can, which is that the face has to be taken again.

    ``include_current`` is the ``only_stale=false`` mode: a template that already scores is
    still re-derived and checked against itself, which is the operation to run after changing
    the *frame ceiling* - the templates are usable, and they were made from frames of a
    different size than the ones now arriving.

    The frame is built by ``uploads.face_frame`` on the stored selfie's **path**, so it goes
    through the same decode, the same ingestion boundary and the same ceiling a punch and an
    enrolment go through. That is not tidiness: a template produced by a different resample of
    the same face moves every distance it is later compared with, and the stored selfie is the
    only copy of the face left.
    """
    import biometrics
    import uploads

    template_path = biometrics.resolve_reference(user_id, biometric_id)
    reason, reference = biometrics.template_problem(template_path)
    if reason == biometrics.STALE_LEGACY_NAME:
        # Read anyway, deliberately. The name rule refuses a legacy-named file *without reading
        # it* because a name cannot tell you which of two files it is - and a re-index is the
        # one caller that can answer that with evidence instead of a name: the vector it is
        # about to write is compared with this one, and only a match is written anywhere.
        try:
            reference = biometrics.read_reference(template_path)
        except (OSError, ValueError, TypeError):
            reference = None
    if reason is None and not include_current:
        return {"status": "current", "reason": None, "template_path": template_path}
    if reason is not None and reason not in REINDEX_VERIFIABLE:
        # The record alone decides this one, and the verdict does not depend on what is on disk:
        # a photograph already stored for this account would not help either (see
        # ``REINDEX_NEEDS_PHOTO``), so looking for it could only produce a worse answer.
        return {
            "status": "needs_photo",
            "reason": reason,
            "template_path": template_path,
            "detail": REINDEX_NEEDS_PHOTO.get(
                reason, "this template is not one a re-index repairs"
            ),
        }

    photo = biometrics.resolve_photo(user_id, biometric_id)
    if not photo:
        return {
            "status": "missing_photo",
            "reason": reason,
            "template_path": template_path,
            "detail": "no reference selfie is stored for this account",
        }
    try:
        image = uploads.face_frame(photo, field="reindex selfie")
    except Exception as exc:  # noqa: BLE001 - an undecodable selfie is an answer, not a crash
        return {
            "status": "error",
            "reason": reason,
            "template_path": template_path,
            "detail": f"the stored selfie could not be decoded: {exc}",
        }
    return {
        "status": "ok",
        "reason": reason,
        "reference": reference,
        "image": image,
        "photo": photo,
        "template_path": template_path,
        "frame_edge": max(image.size),
    }


def _reindex_verdict(reason: str, reference: Any, embedding: list[float]) -> dict[str, Any]:
    """Whether a fresh embedding may replace a stored one, and the evidence either way.

    The gate is the deployment's own: ``main.cosine`` is the function a punch is decided with,
    and ``face_detector.active_band().classify`` is the rule that decides it - so "may this
    template be rewritten" is answered by the same arithmetic as "is this the same person",
    at the *approve* line and not the review one. A re-embedding that lands outside it is not
    an upgrade to be applied hopefully: it means the stored selfie does not reproduce the
    template, and a template that disagrees with the face it came from is worse than a stale
    one, because it looks current.
    """
    import face_detector
    import main

    verdict: dict[str, Any] = {"reason": reason, "action": "needs_photo", "why": None}
    if reason is not None and reason not in REINDEX_VERIFIABLE:
        verdict["why"] = REINDEX_NEEDS_PHOTO.get(
            reason, "this template is not one a re-index repairs"
        )
        return verdict
    stored = list(reference.embedding) if reference is not None else None
    if stored is None or len(stored) != len(embedding):
        verdict["why"] = (
            "the stored template could not be read, or its width is not the one this pipeline "
            "produces, so the re-derived face cannot be checked against it"
        )
        return verdict
    try:
        band = face_detector.active_band()
    except Exception as exc:  # noqa: BLE001 - no derived lines means nothing may be written
        verdict["action"] = "unverifiable"
        verdict["why"] = f"the live band could not be read, so nothing can be checked: {exc}"
        return verdict
    distance = main.cosine(stored, embedding)
    classification = band.classify(distance)
    verdict.update(
        {
            "distance": round(float(distance), 4),
            "approve_line": band.approve,
            "classification": classification,
        }
    )
    if classification == face_detector.MATCH_APPROVED:
        verdict["action"] = "write"
        return verdict
    verdict["why"] = (
        f"re-deriving the face from the stored selfie did not reproduce the stored vector "
        f"(distance {float(distance):.3f} against an approve line of {band.approve:.2f}), so the "
        "two are not the same measurement and the face has to be taken again"
    )
    if reason is None:
        verdict["why"] = (
            f"this template already scores, and re-deriving it from the stored selfie moved it "
            f"past the approve line (distance {float(distance):.3f} against {band.approve:.2f}) - "
            "the stored selfie is a smaller copy of the frame the template was made from, so "
            "refreshing it would replace a working template with a worse one"
        )
    return verdict


def _apply_reindex(user_id: str, image: Any, embedding: list[float]) -> str:
    """Write one template, then bump the account's template version. Returns the path.

    ``biometrics.write_reference`` is the one writer every enrolment path goes through, and a
    re-index goes through it too rather than around it: the template file, the reference selfie
    beside it, the provenance in the same JSON and the retirement of any legacy-named file are
    one operation, and a second writer for "the same record, but from a stored photo" is how
    the record and the photo come to disagree.

    One consequence is deliberate and worth stating: the stored selfie is re-encoded (it is
    thumbnailed and written again at ``biometrics.PHOTO_QUALITY``), so a re-index costs one
    generation of JPEG loss on it. The alternative - writing the template and leaving the photo
    untouched - would need a second writer, and a template whose provenance and photo were
    written by different code paths is exactly the disagreement the record exists to prevent.

    ``users.enrolled_at`` is *not* touched: it answers "when was this face captured", and a
    stamp from today on a photograph from last year would be a lie in the column every
    freshness report is built on. ``template_version`` is what moves, because that is exactly
    what happened - the record was rebuilt from what was already there.
    """
    import biometrics

    path = biometrics.write_reference(user_id, image, embedding)
    with db(write=True) as conn:
        conn.execute(
            "UPDATE users SET template_version = COALESCE(template_version, 0) + 1 "
            "WHERE id = ?",
            (str(user_id),),
        )
    return path


async def reindex_biometric_templates(
    *,
    dry_run: bool,
    limit: int,
    only_stale: bool,
    actor: CurrentUser,
    ip: str | None = None,
) -> dict[str, Any]:
    """Re-derive stored face templates from the reference selfies already on disk.

    WHAT THIS CAN AND CANNOT REPAIR, AND WHY THE DEFAULT IS A DRY RUN
    ----------------------------------------------------------------
    A template is *stale* for five reasons (``biometrics.stale_reason``), and only two of them
    are about how a face is **filed**: a template written before provenance was recorded, and
    one filed under the account id the old naming scheme used. For those, the embedding may be
    perfectly current, the stored selfie is the same face, and re-deriving the vector and
    checking it against the stored one - under the deployment's own approve line - turns "it is
    still refused today" into "it matches again", with no photograph taken.

    The other three are about the **crop or the model**, which is a different statement: no
    distance between a vector from one crop and a vector from another means "same person" under
    any line this build has derived, so no check is possible and a write would be a guess. Those
    accounts come back as ``needs_photo`` with the reason - whether or not a selfie happens to be
    on disk, because it could not settle the question either way - which is what ``biometrics``
    has always said the fix is: one photograph, cheaper than a cohort of templates that match
    nobody.

    ``dry_run`` defaults to true because the write is irreversible in the way that matters: the
    old vector is gone once it is replaced, and the approve-line check above is a *rule*, not an
    observation of whether the newly written templates actually score. The dry run reports the
    same verdict for every account, including the distance, so an operator looks at the
    distribution before anything moves.

    Nothing here holds the write lock across a model call: the engine pool admits one embedding
    at a time, the decode and the writes happen on worker threads, and a batch can be stopped
    between accounts without leaving a half-written record - ``write_reference`` replaces both
    files atomically, and the version bump follows it.
    """
    import biometrics
    import enrollment as enrollment_module
    import face_engine

    worklist = _reindex_worklist(limit=limit)
    counts = {
        "total_scanned": len(worklist),
        "reindexed": 0,
        "would_reindex": 0,
        "already_current": 0,
        "missing_photo": 0,
        "failed_detection": 0,
        "needs_photo": 0,
        "busy": 0,
        "errors": 0,
    }
    errors: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    distances: list[float] = []

    include_current = not bool(only_stale)
    for entry in worklist:
        user_id = str(entry["id"])
        prepared = await run_in_threadpool(
            _prepare_reindex,
            user_id,
            entry.get("biometric_id"),
            include_current=include_current,
        )
        state = prepared["status"]
        if state == "current":
            counts["already_current"] += 1
            continue
        if state == "missing_photo":
            counts["missing_photo"] += 1
            refusals.append(
                {
                    "id": user_id,
                    "name": entry.get("name"),
                    "reason": prepared["reason"],
                    "why": prepared["detail"],
                }
            )
            continue
        if state == "needs_photo":
            # Refused from the record itself, with no distance to report: nothing was measured,
            # because nothing about the stored embedding could be measured here.
            counts["needs_photo"] += 1
            refusals.append(
                {
                    "id": user_id,
                    "name": entry.get("name"),
                    "reason": prepared["reason"],
                    "why": prepared["detail"],
                    "distance": None,
                    "approve_line": None,
                }
            )
            continue
        if state == "error":
            counts["errors"] += 1
            errors.append({"id": user_id, "detail": prepared["detail"]})
            continue

        try:
            # The embedding inside the engine pool, so a batch cannot starve the gate: the pool
            # is the one place that bounds how many model calls run at once, and a re-index that
            # ran its own inference alongside it would do exactly what the pool exists to stop.
            # ``_embed_image`` is the enrolment paths' own single-subject rule (one face, no
            # second box, nothing too small to crop) rather than a second copy of it.
            embedding = await face_engine.ENGINE.run_async(
                enrollment_module._embed_image, prepared["image"]
            )
        except face_engine.FaceEngineBusy as exc:
            counts["busy"] += 1
            errors.append({"id": user_id, "detail": f"the engine refused the job: {exc}"})
            continue
        except ValueError as exc:
            counts["failed_detection"] += 1
            errors.append({"id": user_id, "detail": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - one account must not end the batch
            counts["errors"] += 1
            errors.append({"id": user_id, "detail": f"{type(exc).__name__}: {exc}"})
            continue

        verdict = _reindex_verdict(prepared["reason"], prepared["reference"], list(embedding))
        if verdict.get("distance") is not None:
            distances.append(float(verdict["distance"]))
        if verdict["action"] == "write" and not dry_run:
            try:
                await run_in_threadpool(_apply_reindex, user_id, prepared["image"], list(embedding))
            except Exception as exc:  # noqa: BLE001 - reported per account
                counts["errors"] += 1
                errors.append({"id": user_id, "detail": f"could not write the template: {exc}"})
                continue
            counts["reindexed"] += 1
        elif verdict["action"] == "write":
            counts["would_reindex"] += 1
        else:
            counts["needs_photo"] += 1
            refusals.append(
                {
                    "id": user_id,
                    "name": entry.get("name"),
                    "reason": prepared["reason"],
                    "why": verdict["why"],
                    "distance": verdict.get("distance"),
                    "approve_line": verdict.get("approve_line"),
                }
            )

    summary: dict[str, Any] = {
        "status": "success",
        "dry_run": bool(dry_run),
        "only_stale": bool(only_stale),
        **counts,
        # The buckets above are disjoint and the list below is not: it collects everything that
        # went wrong, including the refusals that already have a bucket of their own (a busy
        # engine, a crop with no face in it). Both are useful - the buckets are what a caller
        # sums to check nothing was lost, the list is what an operator reads - so the count is
        # repeated under its own name rather than the list replacing it.
        "error_count": counts["errors"],
        "distances": {
            "measured": len(distances),
            "min": round(min(distances), 4) if distances else None,
            "max": round(max(distances), 4) if distances else None,
        },
        "errors": errors,
        "refusals": refusals,
        "pipeline": biometrics.current_pipeline(),
        "stored_selfie_max_edge": biometrics.PHOTO_MAX_EDGE,
        "frame_ceiling": settings.face_frame_max_px,
    }
    with db(write=True) as conn:
        _audit_developer(
            conn,
            action="dev_biometric_reindex",
            actor=actor,
            entity="biometrics",
            entity_id=summary["pipeline"],
            before={"dry_run": bool(dry_run), "only_stale": bool(only_stale), "limit": int(limit)},
            after={key: value for key, value in counts.items()},
            ip=ip,
        )
    if counts["reindexed"]:
        raise_alert(
            kind="biometric_reindex",
            summary=f"{counts['reindexed']} face template(s) rebuilt from stored selfies",
            severity="warning",
            source="developer",
            detail={"reindexed": counts["reindexed"], "by": str(actor.id)},
            dedupe_window_seconds=None,
        )
    logger.warning(
        "biometric re-index by %s: dry_run=%s scanned=%s written=%s needs_photo=%s",
        actor.id,
        dry_run,
        counts["total_scanned"],
        counts["reindexed"],
        counts["needs_photo"],
    )
    return summary


@router.post("/developer/biometrics/reindex")
async def developer_biometrics_reindex(
    request: Request,
    dry_run: bool = Query(True, description="Report what would change without writing anything."),
    limit: int = Query(500, ge=1, le=REINDEX_MAX_LIMIT),
    only_stale: bool = Query(True, description="Skip templates that already score."),
    current: CurrentUser = Depends(require_developer),
):
    """Rebuild stale face templates from the stored reference selfies. Root tier only."""
    return await reindex_biometric_templates(
        dry_run=dry_run,
        limit=limit,
        only_stale=only_stale,
        actor=current,
        ip=request.client.host if request.client else None,
    )


# ---------------------------------------------------------------------------
# the read-only impersonation session
# ---------------------------------------------------------------------------
#: Fifteen minutes, hard-coded rather than configurable. The exposure window is the whole risk
#: of this tool, and a deployment that could widen it would widen it - for a debugging session
#: that is a re-mint away from never ending at all.
IMPERSONATION_TTL_SECONDS = 900

#: The roles a session may not be minted for. ``developer`` because it is the tier that mints
#: these and a session for it would be a second root credential the audit trail cannot tell
#: apart from a sign-in; ``head_admin`` because that tier is the one a developer is meant to be
#: *separate from* rather than to become when it is convenient.
IMPERSONATION_FORBIDDEN_ROLES: tuple[str, ...] = (DEVELOPER_ROLE, "head_admin")


def mint_impersonation_token(
    worker_id: str, *, actor: CurrentUser, ip: str | None = None
) -> dict[str, Any]:
    """Mint a short-lived, read-only session for another account, and say so loudly.

    READ-ONLY IS ENFORCED, NOT ASKED FOR
    ------------------------------------
    The token carries ``security.READONLY_SCOPE``, and ``get_current_user`` refuses any method
    outside ``security.SAFE_METHODS`` for a token that carries it - at the one dependency every
    authenticated route goes through. That is the difference between a debugging session and a
    payroll incident: a token minted with the target's role and nothing else would let the
    holder clock a punch in *as that worker*, and the attendance row would be attributed to the
    worker, not to whoever was holding the root credential. A warning in a response body is a
    promise; this is a door.

    What the session *is* for: seeing exactly what a client sees - a mobile rendering bug, an
    empty shift history, a notification inbox that disagrees with the roster. Reads are still
    reads, and they are recorded against the account being looked at, which is stated in the
    answer rather than left to be discovered.

    A deactivated account is refused rather than served: ``get_current_user`` does not check
    ``users.status`` on every request (a sign-in does), so a token minted for a switched-off
    account would be a working session for an account an administrator has deliberately
    stopped - and "debugging" is not a reason to hand one out.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT id, role, COALESCE(status, 'active') AS status, "
            "COALESCE(token_version, 0) AS token_version FROM users WHERE id = ?",
            (str(worker_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such account.")
    target_role = str(row["role"])
    if target_role in IMPERSONATION_FORBIDDEN_ROLES:
        raise HTTPException(
            status_code=403, detail="Cannot impersonate root or head_admin tiers"
        )
    if str(row["status"] or "active") != "active":
        raise HTTPException(
            status_code=403,
            detail=(
                "This account is not active. A session minted for it would be a live session "
                "for an account an administrator has switched off; activate it first if it has "
                "to be looked at as itself."
            ),
        )

    token, expires_at = create_access_token(
        str(row["id"]),
        target_role,
        int(row["token_version"]),
        ttl_hours=IMPERSONATION_TTL_SECONDS / 3600.0,
        scope=READONLY_SCOPE,
    )
    with db(write=True) as conn:
        _audit_developer(
            conn,
            action="dev_impersonation_token_minted",
            actor=actor,
            entity="users",
            entity_id=str(row["id"]),
            before={"role": target_role, "token_version": int(row["token_version"])},
            after={
                "scope": READONLY_SCOPE,
                "ttl_seconds": IMPERSONATION_TTL_SECONDS,
                "expires_at_utc": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ip=ip,
        )
    raise_alert(
        kind="impersonation_token_minted",
        summary=f"a read-only session was minted for {row['id']} ({target_role})",
        severity="warning",
        source="developer",
        detail={"target": str(row["id"]), "role": target_role, "by": str(actor.id)},
        dedupe_window_seconds=None,
    )
    logger.warning(
        "read-only impersonation token for %s (%s) minted by %s, expiring %s",
        row["id"],
        target_role,
        actor.id,
        expires_at.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return {
        "status": "success",
        "impersonating_worker_id": str(row["id"]),
        "role": target_role,
        "access_token": token,
        "token_type": "bearer",
        "expires_in_seconds": IMPERSONATION_TTL_SECONDS,
        "expires_at_utc": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        "read_only": True,
        "warning": (
            "Read-only and temporary: only GET/HEAD/OPTIONS are accepted, it expires in 15 "
            "minutes, and every mint is audited. Anything it reads is recorded against the "
            "account being looked at, so do not use it to do that account's work."
        ),
    }


@router.post("/developer/auth/impersonate/{worker_id}")
async def developer_impersonate(
    request: Request, worker_id: str, current: CurrentUser = Depends(require_developer)
):
    """Mint a 15-minute read-only session for one account. Refused for root and head admin."""
    return mint_impersonation_token(
        worker_id, actor=current, ip=request.client.host if request.client else None
    )
