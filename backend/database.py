"""SQLite access helpers.

Every connection goes through here so that three things are guaranteed:
``busy_timeout`` is set (concurrent readers/writers wait instead of failing with
"database is locked"), the journal mode is WAL (readers never block the single
writer), and the connection is *instrumented* - statements, lock waits and
contention are counted as they happen, because "how long are we holding the write
lock, and is anything timing out on it?" is the question that decides whether a
punch at 04:00 succeeds (see ``telemetry``). ``journal_mode`` is persisted in the
database file, but it is re-applied at startup because a freshly restored file
may not have it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import telemetry
from config import settings

#: Resolved once at import: absolute, never dependent on the current directory.
DB_PATH: Path = settings.database_path

BUSY_TIMEOUT_MS = 5000

# ---------------------------------------------------------------------------
# the diagnostics view (``developer`` is the only reader)
# ---------------------------------------------------------------------------
#: A bounded ring of the statements that took too long, and a handful of counters.
#:
#: Deliberately *not* a second Prometheus metric: ``telemetry`` owns what is scraped, and a
#: diagnostics counter that also appeared in /metrics would be one number with two owners.
#: Deliberately no statement text either - the verb, the duration and the trace id only, for
#: the reason ``InstrumentedConnection`` gives above: in a database layer, statement text can
#: carry a worker id.
SLOW_STATEMENT_MS = 250.0
SLOW_STATEMENT_RING = 200

_slow_statements: deque = deque(maxlen=SLOW_STATEMENT_RING)
_stats: dict[str, float] = {
    "connections_opened": 0.0,
    "read_only_connections": 0.0,
    "statements": 0.0,
    "lock_waits": 0.0,
    "lock_timeouts": 0.0,
    "max_lock_wait_seconds": 0.0,
    "slow_statements": 0.0,
}

#: Set by whichever module owns trace ids (``developer`` does), so a slow statement can be
#: joined to the request that caused it without this module importing the module that reads it.
_trace_provider: Callable[[], str | None] | None = None


def set_trace_provider(provider: Callable[[], str | None] | None) -> None:
    global _trace_provider
    _trace_provider = provider


def _trace_id() -> str | None:
    if _trace_provider is None:
        return None
    try:
        return _trace_provider()
    except Exception:  # pragma: no cover - a provider that throws must not fail a query
        return None


def record_statement(operation: str, seconds: float) -> None:
    """Count a statement, and keep the slow ones. Called on the query path, so it is tiny."""
    _stats["statements"] += 1
    if seconds * 1000.0 < SLOW_STATEMENT_MS:
        return
    _stats["slow_statements"] += 1
    _slow_statements.append(
        {
            "operation": operation,
            "seconds": round(seconds, 4),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "trace_id": _trace_id(),
        }
    )


def record_lock_wait(seconds: float) -> None:
    _stats["lock_waits"] += 1
    if seconds > _stats["max_lock_wait_seconds"]:
        _stats["max_lock_wait_seconds"] = round(seconds, 4)


def record_lock_timeout() -> None:
    _stats["lock_timeouts"] += 1


def slow_queries(limit: int = 20) -> list[dict]:
    """The slowest statements seen, newest first. Never the SQL - see the module docstring."""
    entries = list(_slow_statements)
    return list(reversed(entries))[: max(1, int(limit))]


def connection_stats() -> dict:
    """What the diagnostics surface reports, plus the hub's own unread count."""
    facts: dict = {key: (int(value) if key != "max_lock_wait_seconds" else value) for key, value in _stats.items()}
    facts["busy_timeout_ms"] = BUSY_TIMEOUT_MS
    facts["slow_statement_threshold_ms"] = SLOW_STATEMENT_MS
    facts["unread_alerts"] = _unread_alert_count()
    return facts


def _unread_alert_count() -> int:
    """Read straight from the table: this module cannot import the module that writes it."""
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM developer_alerts WHERE read_at IS NULL"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------
def _is_write_lock_acquisition(statement: str) -> bool:
    """Whether this statement is where SQLite takes the write lock.

    ``BEGIN IMMEDIATE`` takes it (and waits up to ``busy_timeout`` for it, which is exactly
    the wait an operator wants to see), an ``INSERT``/``UPDATE``/``DELETE`` in a deferred
    transaction takes it on the first write instead - invisible to us, which is why the
    transaction histogram exists as the honest companion to this one.
    """
    return telemetry.sql_operation(statement) == "transaction" and "immediate" in statement.lower()


def _closes_transaction(statement: str) -> bool:
    operation = telemetry.sql_operation(statement)
    if operation != "transaction":
        return False
    lowered = statement.lstrip().lower()
    return lowered.startswith("commit") or lowered.startswith("rollback")


class InstrumentedConnection(sqlite3.Connection):
    """A connection that reports what it did, without changing what it does.

    Subclassed rather than wrapped because ``sqlite3`` takes a ``factory`` and every caller in
    this application (endpoints, migrations, the retention sweeper, the readiness probes) then
    gets the same treatment from one place - a wrapper would have to be threaded through
    ``db()``, ``connect()`` and every direct user, and the first one missed would be the one
    holding the lock.

    What is recorded, and what deliberately is not:

    * the **verb** of every statement (``select``, ``insert``, ``pragma``...). Never the text:
      statement text is unbounded, and in a database layer it can carry a worker id;
    * the **lock wait** for a ``BEGIN IMMEDIATE``, which is the time a punch spends queued
      behind another writer;
    * the **duration of an explicit transaction**, from that BEGIN to its COMMIT or ROLLBACK;
    * **lock contention**, when SQLite refuses with "database is locked"/"busy" after
      ``busy_timeout``. Counted separately from other ``OperationalError``s so an alert on it
      means contention rather than a code bug wearing the same exception type.
    """

    def execute(self, sql, parameters=()):  # noqa: A003 - sqlite3's own signature
        operation = telemetry.sql_operation(sql)
        started = time.perf_counter()
        try:
            result = super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if telemetry.is_lock_error(exc):
                telemetry.count_lock_error(operation=operation)
                record_lock_timeout()
            raise
        finally:
            elapsed = time.perf_counter() - started
            telemetry.count_statement(operation)
            record_statement(operation, elapsed)
            if _is_write_lock_acquisition(sql):
                telemetry.observe_lock_wait(operation=operation, seconds=elapsed)
                record_lock_wait(elapsed)
                # The wait is over *now*; the clock for the transaction itself starts here, or
                # the histogram would attribute a 5-second lock wait to the lock holder's own
                # transaction length and make a slow transaction look like a slow query.
                self._telemetry_txn_started = time.perf_counter()
            elif _closes_transaction(sql):
                begun = getattr(self, "_telemetry_txn_started", None)
                if begun is not None:
                    telemetry.observe_transaction(mode="explicit", seconds=time.perf_counter() - begun)
                    self._telemetry_txn_started = None
        return result

    def executemany(self, sql, seq_of_parameters):
        operation = telemetry.sql_operation(sql)
        try:
            return super().executemany(sql, seq_of_parameters)
        except sqlite3.OperationalError as exc:
            if telemetry.is_lock_error(exc):
                telemetry.count_lock_error(operation=operation)
            raise
        finally:
            telemetry.count_statement(operation)

    def executescript(self, sql_script):
        try:
            return super().executescript(sql_script)
        except sqlite3.OperationalError as exc:
            if telemetry.is_lock_error(exc):
                telemetry.count_lock_error(operation="script")
            raise
        finally:
            telemetry.count_statement("script")


def resolve_path(target: Path) -> Path:
    """The concrete file a configured path names, past any directory junction or symlink.

    The test harness points ``DATABASE_PATH`` at a *stable* path whose directory is a
    junction (a symlink on POSIX) that it repoints at a fresh database before every test,
    so that no reset ever has to touch a file another test still holds. SQLite, however,
    identifies a database's locks - and in WAL mode its shared-memory index in particular -
    by the path it was opened with: on Windows, two connections opened through the same
    junction *across* a repoint contend over the old file's lock even though the junction now
    names a different file, and the second one fails with "database is locked". Resolving here
    means every connection is opened by the concrete generation path, which is exactly what
    the rotation promises: a straggler keeps its own file and the next test gets its own.

    It is a no-op for a normal deployment, where the configured path is already its own file.
    """
    return Path(os.path.realpath(str(target)))


def connect(
    *,
    db_path: Path | None = None,
    timeout: float = 10.0,
    isolation_level: str | None = "",
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open a connection. ``isolation_level=None`` means autocommit (used by
    migrations, which manage ``BEGIN IMMEDIATE`` themselves)."""
    target = resolve_path(Path(db_path or DB_PATH))
    if read_only:
        conn = sqlite3.connect(
            f"file:{target}?mode=ro",
            uri=True,
            timeout=timeout,
            isolation_level=isolation_level,
            factory=InstrumentedConnection,
        )
    else:
        conn = sqlite3.connect(
            str(target), timeout=timeout, isolation_level=isolation_level, factory=InstrumentedConnection
        )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    telemetry.count_connection(read_only=read_only)
    _stats["read_only_connections" if read_only else "connections_opened"] += 1
    return conn


@contextmanager
def db(*, write: bool = False, db_path: Path | None = None, isolation_level: str | None = ""):
    """Transactional connection context. Commits only when ``write=True``.

    A write here is an *implicit* transaction - the driver opens one around the first DML
    statement and ``commit()`` closes it - so its duration is measured around this block
    rather than by watching for BEGIN/COMMIT statements (which never appear on the wire).
    That makes this the one place every punch's write time is visible, including the moment
    it spent waiting for the lock, and it is reported next to the explicit transactions
    (migrations, the retention sweeper) under the same metric with a ``mode`` label.
    """
    conn = connect(db_path=db_path, isolation_level=isolation_level)
    started = time.perf_counter()
    try:
        yield conn
        if write:
            conn.commit()
            telemetry.observe_transaction(mode="implicit", seconds=time.perf_counter() - started)
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def configure(*, db_path: Path | None = None, repair: bool = True) -> dict:
    """Apply connection-level pragmas. Returns what was observed.

    Never raises: a database that is locked by another process (for example the
    old server still running) must not abort startup, it must be reported.
    """
    target = Path(db_path or DB_PATH)
    result = {"db_path": str(target), "journal_mode": None, "repaired": False, "error": None}
    try:
        conn = connect(db_path=target, isolation_level=None)
        try:
            current = conn.execute("PRAGMA journal_mode").fetchone()[0]
            result["journal_mode"] = current
            if repair and str(current).lower() != "wal":
                try:
                    new_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    result["journal_mode"] = new_mode
                    result["repaired"] = str(new_mode).lower() == "wal"
                except sqlite3.Error as exc:
                    result["error"] = f"could not enable WAL: {exc}"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# small read helpers
# ---------------------------------------------------------------------------
def scalar(sql: str, params: tuple = (), *, default=None):
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return list(conn.execute(sql, params).fetchall())


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def writable(*, db_path: Path | None = None) -> tuple[bool, str | None]:
    """Probe write access by taking and releasing a RESERVED lock (no changes)."""
    target = Path(db_path or DB_PATH)
    try:
        conn = connect(db_path=target, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True, None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, str(exc)
