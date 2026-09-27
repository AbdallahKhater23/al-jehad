#!/usr/bin/env python
"""Audit every timestamp column before (and after) the UTC storage migration.

READ-ONLY. It opens the database with SQLite's ``mode=ro`` URI and never writes. Run it:

    python tools/timestamp_audit.py                 # the configured DATABASE_PATH
    python tools/timestamp_audit.py --db path.db    # an explicit file
    python tools/timestamp_audit.py --preview 5     # show N sample rows per column

For every table column that looks like a timestamp it reports the row count and the min/max
value, and what the same column becomes once it is read as UTC (the migration's ``-3 hours``
shift). That is the number an operator needs before a backfill: how many rows move, what the
range becomes, and whether any value is already in a shape the migration cannot convert.

Exit code is non-zero if a timestamp column contains a value the migration would not parse,
so this can gate a deploy.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import clock  # noqa: E402

#: A column is treated as a timestamp when its declared type is DATETIME, or when its name
#: ends in one of the conventions this schema uses for a stored moment. TEXT is included with
#: the name filter because several columns are declared TEXT and hold the same string shape.
_TS_NAME = re.compile(r"(timestamp$|_at$|^date$|_date$|_time$|_stamp$)", re.IGNORECASE)
_TS_TYPES = {"DATETIME", "TEXT", "TIMESTAMP"}

#: The migration's own shift, used to preview the result. Kept in step with ``clock``; a test
#: asserts the two agree, so this constant cannot silently drift from the Python conversion.
MIGRATION_SQL_SHIFT = f"datetime(col, '-{clock.KUWAIT_UTC_OFFSET_HOURS} hours')"


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _timestamp_columns(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = row[0]
        columns: list[str] = []
        for column in conn.execute(f'PRAGMA table_info("{table}")'):
            declared = str(column["type"] or "").split("(")[0].strip().upper()
            if declared in _TS_TYPES and _TS_NAME.search(str(column["name"])):
                columns.append(str(column["name"]))
        if columns:
            tables[table] = columns
    return tables


def _is_convertible(value: object) -> bool:
    """Whether the migration can convert this value: NULL/empty, or a stored timestamp."""
    if value is None or value == "":
        return True
    try:
        datetime.strptime(str(value), clock.TS_FORMAT)
        return True
    except (TypeError, ValueError):
        return False


def audit(path: Path, preview: int) -> int:
    if not path.exists():
        print(f"database not found: {path}", file=sys.stderr)
        return 2
    conn = _open_readonly(path)
    bad = 0
    print(f"database : {path}")
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error:
        version = None
    print(f"schema   : {version}  (migration that introduces UTC storage will be > 27)")
    print(f"Kuwait is UTC+{clock.KUWAIT_UTC_OFFSET_HOURS}; a Kuwait stamp becomes UTC via "
          f"{MIGRATION_SQL_SHIFT}\n")
    tables = _timestamp_columns(conn)
    for table, columns in sorted(tables.items()):
        for column in columns:
            try:
                row = conn.execute(
                    f'SELECT COUNT(*) AS n, MIN("{column}") AS lo, MAX("{column}") AS hi '
                    f'FROM "{table}"'
                ).fetchone()
            except sqlite3.Error as exc:  # pragma: no cover - schema drift
                print(f"{table}.{column}: unreadable ({exc})")
                continue
            n, lo, hi = row["n"], row["lo"], row["hi"]
            if n and (not _is_convertible(lo) or not _is_convertible(hi)):
                bad += 1
            preview_min = clock.kuwait_str_to_utc_str(str(lo)) if lo is not None else None
            preview_max = clock.kuwait_str_to_utc_str(str(hi)) if hi is not None else None
            print(f"{table}.{column}")
            print(f"    rows {n:>7}  kuwait[{lo} .. {hi}]  ->  utc[{preview_min} .. {preview_max}]")
            if preview and n:
                for sample in conn.execute(
                    f'SELECT "{column}" AS v FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL LIMIT ?',
                    (preview,),
                ):
                    print(f"    sample {sample['v']!r} -> {clock.kuwait_str_to_utc_str(str(sample['v']))!r}")
    conn.close()
    print()
    if bad:
        print(f"{bad} column(s) hold values the migration would not convert; review before applying.")
        return 1
    print("All timestamp columns hold convertible values.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="database file (read-only)")
    parser.add_argument("--preview", type=int, default=0, help="sample rows per column")
    args = parser.parse_args()
    if args.db is None:
        from config import settings

        args.db = Path(str(settings.database_path))
    return audit(args.db, args.preview)


if __name__ == "__main__":
    raise SystemExit(main())
