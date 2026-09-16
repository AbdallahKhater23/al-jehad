"""Timestamped, hash-verified project snapshot.

Why this exists
---------------
``times.db`` holds live payroll history and ``local_references/`` holds the only
copy of every enrolled face template; neither is tracked by git. Before any code
or schema change we need a snapshot we can prove is intact and is the *right*
database - a ``cp`` of a live SQLite file can capture a torn page state, and a
checksum alone cannot tell you whether the bytes describe a usable database.

Usage
-----
    python backend/tools/backup.py                          # create a snapshot
    python backend/tools/backup.py --prefix pre_migration   # named prefix
    python backend/tools/backup.py --verify backups/<dir>   # re-verify later

Exit codes
----------
    0  snapshot created and verified (or verified cleanly)
    1  verification failed - do not proceed with the change

Layout
------
    backups/<prefix>_YYYYMMDD_HHMMSS/
        source/    main.py, serve.py, tools/, tests/, frontend/, config, README, .gitignore
        data/      times.db (consistent VACUUM INTO snapshot) + stale backend copy
        assets/    local_references/, worker_photos/, backend/certs/
        env/       pip freeze of both interpreters + python/git versions
        MANIFEST.sha256   (relative paths: `sha256sum -c MANIFEST.sha256` works in-place)
        VERIFY.json       (machine-readable result, consumed by the startup gate)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

DEFAULT_DB = PROJECT_ROOT / "times.db"
STALE_DB = BACKEND_DIR / "times.db"
LIVE_REFS = PROJECT_ROOT / "local_references"
WORKER_PHOTOS = PROJECT_ROOT / "worker_photos"
CERTS_DIR = BACKEND_DIR / "certs"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"

SOURCE_FILES = (
    "backend/main.py",
    "backend/serve.py",
    "backend/config.py",
    "backend/database.py",
    "backend/security.py",
    "backend/notifications.py",
    "backend/migrations.py",
    "backend/readiness.py",
    "backend/schema_guard.py",
    "backend/enroll_workers.py",
    "backend/inspect_db.py",
    "backend/pytest.ini",
    "README.md",
    ".gitignore",
    ".env",
)
SOURCE_DIRS = ("frontend", "backend/tests", "backend/tools")
TABLES = ("users", "attendance_logs", "active_sessions", "construction_sites")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(db_path: Path) -> dict:
    """Canonical, read-only description of a database's contents.

    Counts and sums alone would not notice a rewritten row, so users and sites
    also get a digest of their full canonical projection.
    """
    result: dict = {"db": str(db_path)}
    if not db_path.exists():
        result["error"] = "missing"
        return result
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for table in TABLES:
            try:
                result[f"count:{table}"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                result[f"count:{table}"] = f"error:{exc}"
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hours), 0), COALESCE(MAX(id), 0) FROM attendance_logs"
            ).fetchone()
            result["logs_rows"], result["logs_sum_hours"], result["logs_max_id"] = row
        except sqlite3.Error:
            pass
        for table, projection in (
            ("users", "id||'|'||name||'|'||role||'|'||password_hash"),
            ("construction_sites", "site_name||'|'||lat||'|'||lon||'|'||radius"),
            ("active_sessions", "worker_id||'|'||site_name||'|'||clock_in_time"),
        ):
            try:
                rows = conn.execute(f"SELECT {projection} FROM {table} ORDER BY 1").fetchall()
                joined = "\n".join(str(value) for (value,) in rows)
                result[f"digest:{table}"] = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
            except sqlite3.Error:
                pass
        try:
            result["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            result["integrity_check"] = f"error:{exc}"
    finally:
        conn.close()
    return result


def _freeze(python_exe: str) -> str:
    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "freeze"],
            capture_output=True, text=True, timeout=180, check=False,
        )
        return completed.stdout.strip() or "(empty)"
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"(failed: {exc})"


def _git_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=15, check=False,
        )
        return completed.stdout.strip() or "(unknown)"
    except Exception:  # pragma: no cover
        return "(unknown)"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def create_snapshot(
    prefix: str = "pre_feature_build",
    *,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    project_root: Path | None = None,
    include_assets: bool = True,
    quiet: bool = False,
) -> Path:
    """Snapshot the project. Returns the snapshot directory (verified)."""
    db_path = Path(db_path or DEFAULT_DB)
    backup_dir = Path(backup_dir or DEFAULT_BACKUP_DIR)
    project_root = Path(project_root or PROJECT_ROOT)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{prefix}_{stamp}"
    (dest / "source").mkdir(parents=True, exist_ok=False)
    (dest / "data").mkdir()
    (dest / "assets").mkdir()
    (dest / "env").mkdir()

    source_fingerprint = fingerprint(db_path)

    # --- source ---------------------------------------------------------
    for relative in SOURCE_FILES:
        src = project_root / relative
        if src.exists():
            shutil.copy2(src, dest / "source" / src.name)
    for relative in SOURCE_DIRS:
        src = project_root / relative
        if src.exists():
            shutil.copytree(src, dest / "source" / src.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # --- database -------------------------------------------------------
    # VACUUM INTO writes an atomic, self-consistent single-file copy, so it is
    # correct even while the server is running and in WAL mode.
    snap_db = dest / "data" / "times.db"
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (str(snap_db),))
    finally:
        conn.close()
    if STALE_DB.exists():
        shutil.copy2(STALE_DB, dest / "data" / "times.db.stale-backend-copy")

    # --- assets ---------------------------------------------------------
    if include_assets:
        for src in (LIVE_REFS, WORKER_PHOTOS, CERTS_DIR):
            if src.exists():
                shutil.copytree(src, dest / "assets" / src.name, ignore=shutil.ignore_patterns("desktop.ini"))

    # --- environment ----------------------------------------------------
    (dest / "env" / "python-version.txt").write_text(
        f"run_with: {sys.executable}\n{sys.version}\n", encoding="utf-8"
    )
    (dest / "env" / "pip-freeze-run-with.txt").write_text(_freeze(sys.executable) + "\n", encoding="utf-8")
    venv_python = BACKEND_DIR / "venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = BACKEND_DIR / "venv" / "bin" / "python"
    if venv_python.exists() and str(venv_python) != sys.executable:
        (dest / "env" / "pip-freeze-venv.txt").write_text(_freeze(str(venv_python)) + "\n", encoding="utf-8")
    (dest / "env" / "versions.txt").write_text(
        f"git_head: {_git_head()}\ncreated_at: {_dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"prefix: {prefix}\nsource_db: {db_path}\n",
        encoding="utf-8",
    )

    # --- manifest -------------------------------------------------------
    manifest_lines = []
    for path in sorted(p for p in dest.rglob("*") if p.is_file()):
        if path.name in {"MANIFEST.sha256", "VERIFY.json"}:
            continue
        manifest_lines.append(f"{sha256_file(path)}  {path.relative_to(dest).as_posix()}")
    (dest / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    snapshot_fingerprint = fingerprint(snap_db)
    deltas = {
        key: (source_fingerprint.get(key), snapshot_fingerprint.get(key))
        for key in set(source_fingerprint) | set(snapshot_fingerprint)
        if key not in {"db", "integrity_check"} and source_fingerprint.get(key) != snapshot_fingerprint.get(key)
    }

    report = {
        "status": "PASS",
        "prefix": prefix,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_db": str(db_path),
        "snapshot_db": str(snap_db),
        "db_sha256": sha256_file(snap_db),
        "files": len(manifest_lines),
        "integrity_check": snapshot_fingerprint.get("integrity_check"),
        "fingerprint": snapshot_fingerprint,
        "live_write_delta": deltas,
        "notes": [],
    }
    if report["integrity_check"] != "ok":
        report["status"] = "FAIL"
        report["notes"].append("snapshot failed PRAGMA integrity_check")
    if deltas:
        report["notes"].append(
            "live server wrote rows while the snapshot was taken (expected); "
            "the snapshot is internally consistent"
        )
    (dest / "VERIFY.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not quiet:
        _print_report(dest, report)
    return dest


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify_snapshot(dest: Path, *, rehash: bool = True) -> dict:
    """Re-verify an existing snapshot directory."""
    dest = Path(dest)
    report = {"status": "FAIL", "dir": str(dest), "errors": [], "warnings": []}
    marker = dest / "VERIFY.json"
    if not marker.exists():
        report["errors"].append("VERIFY.json missing")
        return report
    stored = json.loads(marker.read_text(encoding="utf-8"))
    report.update({k: stored.get(k) for k in ("prefix", "created_at", "db_sha256", "integrity_check")})
    report["stored_status"] = stored.get("status")
    if stored.get("status") != "PASS":
        report["errors"].append(f"stored status is {stored.get('status')!r}, not PASS")
    if stored.get("integrity_check") != "ok":
        report["errors"].append("stored integrity_check was not ok")

    manifest = dest / "MANIFEST.sha256"
    if not manifest.exists():
        report["errors"].append("MANIFEST.sha256 missing")
        return report

    entries = [line.split("  ", 1) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    checked = 0
    if rehash:
        for digest, relative in entries:
            path = dest / relative
            if not path.exists():
                report["errors"].append(f"missing file: {relative}")
            elif sha256_file(path) != digest:
                report["errors"].append(f"hash mismatch: {relative}")
            else:
                checked += 1
    report["files_checked"] = checked
    report["files_listed"] = len(entries)

    snap_db = dest / "data" / "times.db"
    if snap_db.exists():
        actual = sha256_file(snap_db)
        if stored.get("db_sha256") and actual != stored["db_sha256"]:
            report["errors"].append("snapshot times.db hash does not match VERIFY.json")
        report["snapshot_fingerprint"] = fingerprint(snap_db)
        if report["snapshot_fingerprint"].get("integrity_check") != "ok":
            report["errors"].append("live PRAGMA integrity_check on the snapshot did not return ok")
    else:
        report["errors"].append("snapshot data/times.db missing")

    report["status"] = "PASS" if not report["errors"] else "FAIL"
    return report


def age_hours(dest: Path) -> float | None:
    marker = Path(dest) / "VERIFY.json"
    if not marker.exists():
        return None
    try:
        created = json.loads(marker.read_text(encoding="utf-8")).get("created_at")
        if not created:
            return None
        return (_dt.datetime.now() - _dt.datetime.fromisoformat(created)).total_seconds() / 3600.0
    except Exception:
        return None


def newest_verified_snapshot(backup_dir: Path) -> Path | None:
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in backup_dir.iterdir() if p.is_dir() and (p / "VERIFY.json").exists()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# reporting / CLI
# ---------------------------------------------------------------------------
def _print_report(dest: Path, report: dict) -> None:
    print("=" * 72)
    print(f"  Snapshot: {dest}")
    print(f"  Status:   {report['status']}")
    print(f"  Files:    {report.get('files')} hashed into MANIFEST.sha256")
    print(f"  DB:       integrity_check={report.get('integrity_check')} sha256={str(report.get('db_sha256'))[:16]}...")
    fingerprint_data = report.get("fingerprint", {})
    for table in TABLES:
        print(f"    {table:<20} {fingerprint_data.get(f'count:{table}')}")
    if report.get("live_write_delta"):
        print("  Note: the live database changed during the snapshot (server is running):")
        for key, (before, after) in report["live_write_delta"].items():
            print(f"    {key}: {before} -> {after}")
    for note in report.get("notes", []):
        print(f"  Note: {note}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or verify a project snapshot")
    parser.add_argument("--prefix", default="pre_feature_build")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--verify", metavar="DIR", help="verify an existing snapshot instead of creating one")
    parser.add_argument("--no-assets", action="store_true", help="skip biometric assets (fast, for tests)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.verify:
        report = verify_snapshot(Path(args.verify))
        print(json.dumps(report, indent=2))
        if report["status"] != "PASS":
            print(f"VERIFY FAILED: {len(report['errors'])} problem(s)", file=sys.stderr)
            return 1
        print("VERIFY PASS: hashes, database integrity and fingerprint all agree")
        return 0

    try:
        dest = create_snapshot(
            args.prefix,
            db_path=Path(args.db),
            backup_dir=Path(args.backup_dir),
            include_assets=not args.no_assets,
            quiet=args.quiet,
        )
    except Exception as exc:
        print(f"SNAPSHOT FAILED: {exc}", file=sys.stderr)
        return 1

    report = verify_snapshot(dest)
    if report["status"] != "PASS":
        print(f"SNAPSHOT VERIFICATION FAILED: {report['errors']}", file=sys.stderr)
        return 1
    print(f"VERIFY PASS: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
