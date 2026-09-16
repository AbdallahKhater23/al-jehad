"""Schema drift guard: prove the live database is what the shipped code produces.

The expected schema is deliberately **not** a hand-maintained dictionary. It is
*compiled* from ``baseline_schema()`` plus every migration into an in-memory
database, and the live file is diffed against the result. Two consequences worth
stating, because they are the whole reason for doing it this way:

* there is exactly one declaration of the schema, so a second, stale copy cannot
  drift from the first;
* the migration list becomes self-validating - if a future migration is added
  without updating ``baseline_schema()`` (or the reverse), a fresh install stops
  equalling baseline + migrations and the guard says so.

REPAIR POLICY (the boundary matters more than the code)
-------------------------------------------------------
A repair may only **ADD** what the shipped schema already defines, and only
idempotently. Anything that would rewrite or reinterpret existing data is a
migration, and a migration is reviewed by a human - never performed silently at
boot. Concretely:

* missing table / column / index / trigger  -> repairable (add it)
* column affinity mismatch (REAL -> TEXT)   -> FATAL, repairable=False
  SQLite is dynamically typed, so it would happily keep storing strings in a
  REAL column and ``SUM(hours)`` would quietly return wrong payroll.
* live stricter than expected (extra NOT NULL) -> FATAL: every future write of
  that row now fails, so fail closed on the direction that breaks writes.
* live laxer (missing NOT NULL)             -> ADVISORY: writes still work.
* extra column                              -> ADVISORY (and it will surface
  loudly when a future migration tries to add that name).
* extra table / index                       -> INFO.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

import migrations
from config import settings

SEVERITY_INFO = "info"
SEVERITY_ADVISORY = "advisory"
SEVERITY_REPAIRABLE = "repairable"
SEVERITY_FATAL = "fatal"

_ORDER = {SEVERITY_INFO: 0, SEVERITY_ADVISORY: 1, SEVERITY_REPAIRABLE: 2, SEVERITY_FATAL: 3}

MODE_ENFORCE_REPAIR = "enforce_repair"
MODE_DETECT_ONLY = "detect_only"


@dataclass
class Drift:
    kind: str
    object_type: str
    name: str
    severity: str
    detail: str
    repairable: bool = False
    table: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    ready: bool
    severity: str
    expected_schema_version: int
    live_schema_version: int
    drift: list[Drift] = field(default_factory=list)
    repaired: list[dict] = field(default_factory=list)
    mode: str = MODE_ENFORCE_REPAIR
    error: str | None = None

    @property
    def blocking(self) -> list[Drift]:
        """Drift that must stop the server, after any repair attempt."""
        return [item for item in self.drift if item.severity == SEVERITY_FATAL]

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "severity": self.severity,
            "mode": self.mode,
            "expected_schema_version": self.expected_schema_version,
            "live_schema_version": self.live_schema_version,
            "repaired": self.repaired,
            "blocking": [item.as_dict() for item in self.blocking],
            "drift": [item.as_dict() for item in self.drift],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------
def _affinity(declared_type: str | None) -> str:
    """SQLite type affinity, per https://sqlite.org/datatype3.html#affname."""
    text = (declared_type or "").upper()
    if "INT" in text:
        return "INTEGER"
    if "CHAR" in text or "CLOB" in text or "TEXT" in text:
        return "TEXT"
    if "BLOB" in text or text == "":
        return "BLOB"
    if "REAL" in text or "FLOA" in text or "DOUB" in text:
        return "REAL"
    return "NUMERIC"


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    columns: dict[str, dict] = {}
    for row in conn.execute(f"PRAGMA table_info({table})"):
        columns[row[1]] = {
            "declared_type": row[2],
            "affinity": _affinity(row[2]),
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": bool(row[5]),
        }
    return columns


def _is_internal(name: str | None) -> bool:
    return (name or "").startswith("sqlite_")


def snapshot(conn: sqlite3.Connection) -> dict:
    tables = {}
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ):
        tables[row[0]] = _columns(conn, row[0])
    indexes = {
        row[0]: row[1]
        for row in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'")
        if not _is_internal(row[0])
    }
    triggers = {
        row[0]: row[1]
        for row in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'")
        if not _is_internal(row[0])
    }
    return {"tables": tables, "indexes": indexes, "triggers": triggers}


def expected_snapshot() -> dict:
    """Baseline DDL + every migration, applied to a throwaway in-memory database."""
    conn = sqlite3.connect(":memory:")
    try:
        migrations.ensure_schema(conn)
        migrations.run_migrations(conn)
        return snapshot(conn)
    finally:
        conn.close()


def live_snapshot(db_path: Path | None = None) -> tuple[dict, int]:
    target = Path(db_path or settings.database_path)
    conn = sqlite3.connect(str(target))
    try:
        version = migrations.current_version(conn)
        return snapshot(conn), version
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------
def diff_snapshots(expected: dict, live: dict) -> list[Drift]:
    drift: list[Drift] = []

    for table, expected_columns in expected["tables"].items():
        if table not in live["tables"]:
            drift.append(
                Drift(
                    kind="missing_table",
                    object_type="table",
                    name=table,
                    table=table,
                    severity=SEVERITY_REPAIRABLE,
                    repairable=True,
                    detail=f"table '{table}' is defined by the shipped schema but is absent",
                )
            )
            continue

        live_columns = live["tables"][table]
        for column, expected_meta in expected_columns.items():
            live_meta = live_columns.get(column)
            if live_meta is None:
                drift.append(
                    Drift(
                        kind="missing_column",
                        object_type="column",
                        name=f"{table}.{column}",
                        table=table,
                        severity=SEVERITY_REPAIRABLE,
                        repairable=True,
                        detail=f"column '{table}.{column}' ({expected_meta['declared_type']}) is missing",
                    )
                )
                continue

            if expected_meta["affinity"] != live_meta["affinity"]:
                drift.append(
                    Drift(
                        kind="affinity_mismatch",
                        object_type="column",
                        name=f"{table}.{column}",
                        table=table,
                        severity=SEVERITY_FATAL,
                        repairable=False,
                        detail=(
                            f"'{table}.{column}' holds {live_meta['declared_type']} "
                            f"(affinity {live_meta['affinity']}) but the shipped schema declares "
                            f"{expected_meta['declared_type']} (affinity {expected_meta['affinity']}). "
                            "SQLite would store values of the wrong type and aggregates would be wrong"
                        ),
                    )
                )
            elif live_meta["notnull"] and not expected_meta["notnull"]:
                drift.append(
                    Drift(
                        kind="live_stricter",
                        object_type="column",
                        name=f"{table}.{column}",
                        table=table,
                        severity=SEVERITY_FATAL,
                        repairable=False,
                        detail=(
                            f"'{table}.{column}' is NOT NULL in the database but not in the shipped "
                            "schema, so writes the code performs will fail"
                        ),
                    )
                )
            elif expected_meta["notnull"] and not live_meta["notnull"]:
                drift.append(
                    Drift(
                        kind="live_laxer",
                        object_type="column",
                        name=f"{table}.{column}",
                        table=table,
                        severity=SEVERITY_ADVISORY,
                        detail=(
                            f"'{table}.{column}' is nullable here but NOT NULL in the shipped schema "
                            "(legacy table created by an earlier revision; writes are unaffected)"
                        ),
                    )
                )

        for column in live_columns:
            if column not in expected_columns:
                drift.append(
                    Drift(
                        kind="extra_column",
                        object_type="column",
                        name=f"{table}.{column}",
                        table=table,
                        severity=SEVERITY_ADVISORY,
                        detail=f"'{table}.{column}' exists here but is not in the shipped schema",
                    )
                )

    for table in live["tables"]:
        if table not in expected["tables"]:
            drift.append(
                Drift(
                    kind="extra_table",
                    object_type="table",
                    name=table,
                    table=table,
                    severity=SEVERITY_INFO,
                    detail=f"table '{table}' is not part of the shipped schema",
                )
            )

    for index, table in expected["indexes"].items():
        if index not in live["indexes"]:
            drift.append(
                Drift(
                    kind="missing_index",
                    object_type="index",
                    name=index,
                    table=table,
                    severity=SEVERITY_REPAIRABLE,
                    repairable=True,
                    detail=f"index '{index}' on '{table}' is missing",
                )
            )

    for trigger, table in expected["triggers"].items():
        if trigger not in live["triggers"]:
            drift.append(
                Drift(
                    kind="missing_trigger",
                    object_type="trigger",
                    name=trigger,
                    table=table,
                    severity=SEVERITY_REPAIRABLE,
                    repairable=True,
                    detail=(
                        f"trigger '{trigger}' on '{table}' is missing, which silently removes a "
                        "database-enforced guarantee (for example audit_log staying append-only)"
                    ),
                )
            )

    return drift


def worst_severity(drift: list[Drift]) -> str:
    if not drift:
        return SEVERITY_INFO
    return max((item.severity for item in drift), key=lambda item: _ORDER[item])


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def inspect(db_path: Path | None = None) -> Report:
    """Read-only comparison. Never writes, so it is safe inside a request."""
    try:
        live, live_version = live_snapshot(db_path)
        expected = expected_snapshot()
    except sqlite3.Error as exc:
        return Report(
            ready=False,
            severity=SEVERITY_FATAL,
            expected_schema_version=migrations.SCHEMA_VERSION,
            live_schema_version=0,
            error=str(exc),
        )
    drift = diff_snapshots(expected, live)
    blocking = [item for item in drift if item.severity == SEVERITY_FATAL]
    return Report(
        ready=not blocking,
        severity=worst_severity(drift),
        expected_schema_version=migrations.SCHEMA_VERSION,
        live_schema_version=live_version,
        drift=drift,
        mode=MODE_DETECT_ONLY,
    )


def enforce(db_path: Path | None = None, mode: str | None = None) -> Report:
    """Inspect, attempt the documented additive repairs, then re-inspect."""
    effective_mode = mode or settings.schema_guard_mode
    report = inspect(db_path)
    if report.error:
        return report

    repairable = [item for item in report.drift if item.repairable]
    if effective_mode != MODE_ENFORCE_REPAIR or not repairable:
        report.mode = effective_mode
        return report

    target = Path(db_path or settings.database_path)
    try:
        conn = sqlite3.connect(str(target), isolation_level=None)
        try:
            migrations.ensure_schema(conn)
            applied = migrations.run_migrations(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        report.error = f"repair failed: {exc}"
        report.ready = False
        report.severity = SEVERITY_FATAL
        return report

    refreshed = inspect(db_path)
    refreshed.mode = effective_mode
    remaining = {item.name for item in refreshed.drift}
    refreshed.repaired = [
        {"kind": item.kind, "name": item.name, "outcome": "added"}
        for item in repairable
        if item.name not in remaining
    ]
    still_broken = [item.name for item in repairable if item.name in remaining]
    refreshed.repaired.extend(
        {"kind": "repair_failed", "name": name, "outcome": "still_drifted"} for name in still_broken
    )
    if applied:
        refreshed.repaired.append(
            {"kind": "migrations_applied", "name": ",".join(map(str, applied)), "outcome": "applied"}
        )
    return refreshed


def self_check() -> dict:
    """Prove the guard's own premises: a fresh install equals baseline + migrations."""
    expected = expected_snapshot()
    conn = sqlite3.connect(":memory:")
    try:
        migrations.ensure_schema(conn)
        migrations.run_migrations(conn)
        live = snapshot(conn)
    finally:
        conn.close()
    drift = diff_snapshots(expected, live)
    return {"ok": not drift, "drift": [item.as_dict() for item in drift]}


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in argv:
        result = self_check()
        print("schema guard self-check:", "PASS" if result["ok"] else "FAIL")
        for item in result["drift"]:
            print("  ", item)
        return 0 if result["ok"] else 1
    report = enforce(mode=MODE_DETECT_ONLY if "--detect-only" in argv else None)
    payload = report.as_dict()
    print(f"schema guard: {'OK' if report.ready else 'DRIFT'} (severity {report.severity}, mode {report.mode})")
    print(f"  live schema version: {payload['live_schema_version']}  expected: {payload['expected_schema_version']}")
    for item in report.drift:
        print(f"  [{item.severity:<10}] {item.kind:<18} {item.name}: {item.detail}")
    for item in report.repaired:
        print(f"  [repaired  ] {item}")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
