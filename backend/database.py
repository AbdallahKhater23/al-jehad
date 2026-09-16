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

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import telemetry
from config import settings

#: Resolved once at import: absolute, never dependent on the current directory.
DB_PATH: Path = settings.database_path

BUSY_TIMEOUT_MS = 5000


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
            raise
        finally:
            elapsed = time.perf_counter() - started
            telemetry.count_statement(operation)
            if _is_write_lock_acquisition(sql):
                telemetry.observe_lock_wait(operation=operation, seconds=elapsed)
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


def connect(
    *,
    db_path: Path | None = None,
    timeout: float = 10.0,
    isolation_level: str | None = "",
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open a connection. ``isolation_level=None`` means autocommit (used by
    migrations, which manage ``BEGIN IMMEDIATE`` themselves)."""
    target = Path(db_path or DB_PATH)
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
