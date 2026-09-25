"""Latency and CPU profile of the API's hot paths, on an isolated clone of the live DB.

Why this exists
---------------
Optimising without measuring is guessing, and this app has enough moving parts to make
guessing wrong in both directions: the clock-in path is dominated by model inference that
no amount of Python tuning helps, while the admin console's reads are plain SQLite work
that scales with rows and is easy to miss because a dev database is nearly empty.

So this tool does two things the test suite deliberately does not:

* it puts a realistic number of rows into the *throwaway* database before measuring, so an
  O(n) query or a per-row lookup shows up as a slope rather than as noise;
* it reports where the time actually goes (``cProfile``), not just how long the request
  took - a fast endpoint with a slow unused branch still has a slow branch.

Isolation is the test harness's, not this file's: ``tests/harness.py`` clones the live
database into a temp directory, stubs DeepFace, and refuses outbound HTTP. This tool only
ever writes to that clone, and it stops before measuring anything if the app is pointed
anywhere else.

Usage
-----
    python backend/tools/profile_endpoints.py                       # 5 runs of every case
    python backend/tools/profile_endpoints.py --iterations 10
    python backend/tools/profile_endpoints.py --only reports        # cases whose label matches
    python backend/tools/profile_endpoints.py --workers 80 --days 90
    python backend/tools/profile_endpoints.py --no-synthetic        # measure the clone as-is
    python backend/tools/profile_endpoints.py --dump profile.prof   # for pstats / snakeviz

Exit codes
----------
    0  measured (a failing endpoint is reported, not hidden)
    1  the app is not isolated, or the harness could not start
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import os
import pstats
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from pstats import SortKey

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent
TESTS_DIR = BACKEND_DIR / "tests"

for _path in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import harness  # noqa: E402  - importing it *is* the isolation layer (see its docstring)

#: The working directory is part of that isolation and is entered explicitly, because
#: ``harness`` no longer chdir()s at import - doing so breaks pytest-xdist's collection (see
#: safety rule 1 in its docstring). Before ``import main``, which is below.
harness.enter_temp_root()
from fastapi.testclient import TestClient  # noqa: E402

#: Two months of a working year, wide enough to include whatever the clone holds.
PERIOD = "start=2026-07-01&end=2026-09-30"

#: The reads the console and the worker app actually make, plus the clock-in punch.
#: ``label, method, path, kwargs``; a case is one request unless it says otherwise.
CASES: tuple[tuple[str, str, str, dict], ...] = (
    ("admin reports/shifts (a quarter)", "GET", f"/api/v1/admin/reports/shifts?{PERIOD}", {}),
    ("admin reports/attendance (a quarter)", "GET", f"/api/v1/admin/reports/attendance?{PERIOD}", {}),
    ("admin reports/pending", "GET", "/api/v1/admin/reports/pending", {}),
    ("admin reports/export csv (a quarter)", "GET", f"/api/v1/admin/reports/export?kind=shifts&format=csv&{PERIOD}", {}),
    ("admin active_sessions (polled board)", "GET", "/api/v1/admin/active_sessions", {}),
    ("admin users (roster)", "GET", "/api/v1/admin/users", {}),
    # The alert queue is not measured here: it left ``/admin/notifications`` for the root
    # tier's ``/developer/notifications``, and this tool signs in as an administrator (the
    # harness clones a database with no root account). Measuring it would mean minting one
    # into the clone to time a ``LIMIT 100`` read, which is not what this list is for.
    ("admin pending_reviews", "GET", "/api/v1/admin/pending_reviews", {}),
    ("admin logs (200 rows)", "GET", "/api/v1/admin/logs?limit=200", {}),
    ("admin audit_log (200 rows)", "GET", "/api/v1/admin/audit_log?limit=200", {}),
    ("admin notes (open)", "GET", "/api/v1/admin/notes?status=open&limit=100", {}),
    ("admin sites", "GET", "/api/v1/admin/sites", {}),
    ("admin shift_rules", "GET", "/api/v1/admin/shift_rules", {}),
    ("worker me/logs (50 rows)", "GET", "/api/v1/worker/me/logs?limit=50", {}),
    ("worker me/stats", "GET", "/api/v1/worker/me/stats", {}),
    ("public status", "GET", "/api/v1/status", {}),
    ("admin status/detail (readiness self-test)", "GET", "/api/v1/status/detail", {}),
    ("auth login (bcrypt verify)", "POST", "/api/v1/auth/login", {"json": {
        "user_id": harness.WORKER,
        "email_or_phone": harness.EMAILS[harness.WORKER],
        "password": harness.PASSWORDS[harness.WORKER],
    }}),
)


# --------------------------------------------------------------------------- #
# Dataset: realistic row counts in the throwaway clone
# --------------------------------------------------------------------------- #
def build_dataset(app_module, workers: int, days: int, *, verbose: bool) -> None:
    """Add ``workers`` accounts with a shift on each of the last ``days`` days.

    Deliberately written straight into the temp database rather than through the API: the
    point is to reach a realistic row count in seconds, and hashing a password per row
    would dominate the runtime (bcrypt at the app's cost factor is ~0.25 s each). One real
    hash is generated and reused, so every row is still a valid bcrypt string.
    """
    import datetime as dt

    if workers <= 0 or days <= 0:
        return

    shared_hash = app_module.pwd_context.hash("bench-only-password")
    today = dt.date.today()
    site = "Downtown Tower A"

    users = [
        (str(index + 2), f"Bench Worker {index + 1}", f"bench{index + 1}@example.test", "", shared_hash, "worker")
        for index in range(workers)
    ]

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        next_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM attendance_logs").fetchone()[0] or 0) + 1
        conn.executemany(
            "INSERT OR REPLACE INTO users (id, name, email, phone, password_hash, role) VALUES (?,?,?,?,?,?)",
            users,
        )

        # Project onto the columns this schema actually has: ``lat``/``lon`` arrived in a
        # later migration, and a benchmark that breaks on an older or newer clone is a
        # benchmark nobody runs twice.
        present = {row[1] for row in conn.execute("SELECT * FROM pragma_table_info('attendance_logs')")}
        columns = [name for name in ("id", "worker_id", "site_name", "action", "timestamp", "hours", "score", "status", "lat", "lon") if name in present]

        def log_row(**values) -> tuple:
            return tuple(values[name] for name in columns)

        logs = []
        for index, user in enumerate(users):
            worker_id = user[0]
            for day in range(days):
                stamp = today - dt.timedelta(days=day)
                # Weekends off, like the seeded working_days default.
                if stamp.weekday() >= 5:
                    continue
                clock_in = dt.datetime.combine(stamp, dt.time(8, 0)).strftime("%Y-%m-%d %H:%M:%S")
                clock_out = dt.datetime.combine(stamp, dt.time(17, 0)).strftime("%Y-%m-%d %H:%M:%S")
                # One worker in five is left awaiting a decision, so the review queues
                # have rows to walk rather than an empty result set to time.
                pending = index % 5 == 0
                status = "pending_review" if pending else "success"
                logs.append(log_row(id=next_id, worker_id=worker_id, site_name=site, action="Clock In",
                                    timestamp=clock_in, hours=0.0, score=0.12, status=status, lat=30.05, lon=31.23))
                next_id += 1
                logs.append(log_row(id=next_id, worker_id=worker_id, site_name=site, action="Clock Out",
                                    timestamp=clock_out, hours=0.0 if pending else 8.0, score=0.12,
                                    status=status, lat=30.05, lon=31.23))
                next_id += 1
        conn.executemany(
            f"INSERT OR REPLACE INTO attendance_logs ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            logs,
        )

        # Roughly one worker in three is still on site: this is what the Live Ops board
        # polls, so a wrong shape here would hide the cost of the whole tab.
        conn.execute("DELETE FROM active_sessions")
        open_now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany(
            "INSERT INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            [(user[0], site, open_now) for user in users if int(user[0]) % 3 == 0],
        )
        conn.commit()
    finally:
        conn.close()

    if verbose:
        rows = harness.db_scalar("SELECT COUNT(*) FROM attendance_logs")
        print(f"  clone dataset : {len(users)} bench workers, {rows} attendance rows, "
              f"{harness.db_scalar('SELECT COUNT(*) FROM active_sessions')} on site")


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
def cases_for(session, only: str | None):
    """The runnable cases, as ``(label, callable)`` pairs, with auth per role."""
    def request(method: str, path: str, headers: dict, **kwargs):
        return getattr(session, method.lower())(path, headers=headers, **kwargs)

    selected = []
    for label, method, path, kwargs in CASES:
        if only and only.lower() not in label.lower():
            continue
        # ``worker`` cases read their own data, so they carry the worker's token; every
        # other case is an administrator's.
        headers = harness.bearer(harness.WORKER) if label.startswith("worker") else harness.bearer(harness.HEAD_ADMIN)
        if label.startswith("public"):
            headers = {}
        selected.append((label, method, path, kwargs, headers))

    # ``MOALLEM`` rather than ``WORKER``: the fixture leaves worker 1 with a
    # ``pending_review`` punch, and the app refuses a second punch from a flagged account
    # (403, by design). Measuring that refusal would tell us nothing about the punch path.
    punch = "worker clock in + clock out (2 requests)"
    if not only or only.lower() in punch:
        selected.append((punch, "PUNCH", "", {"user_id": harness.MOALLEM}, {}))
    return selected


def run_case(session, method: str, path: str, kwargs: dict, headers: dict):
    """One case: a single request, or the punch pair that must leave no session behind."""
    if method == "PUNCH":
        user_id = kwargs["user_id"]
        headers = harness.bearer(user_id)
        first = harness.clock_in(session, user_id, headers=headers)
        second = harness.clock_in(session, user_id, action="Clock Out", headers=headers)
        return second if second.status_code != 200 else first
    return session.request(method, path, headers=headers or None, **kwargs)


def _interesting_frame(filename: str, name: str) -> bool:
    """App code, plus the C calls the app itself makes (sqlite3, bcrypt).

    An in-process client spends most of a request inside httpx/anyio/asyncio, so an
    unfiltered ranking is a list of the transport's frames and says nothing about the
    server. Filtering here is what turns the profile into an answer.
    """
    if "sqlite3" in filename or "sqlite3" in name or "bcrypt" in filename:
        return True
    # The app is a flat set of modules in backend/, so "directly in that directory" is the
    # whole test. ``BACKEND_DIR in path.parents`` would also match every site-packages frame
    # (the virtualenv lives inside backend/), which is how a ranking fills up with httpx.
    return Path(filename).parent == BACKEND_DIR


def hot_functions(stats: pstats.Stats, top: int) -> list[str]:
    """The functions a single case spent its time in, by cumulative time.

    Attribution has to be per case: one global profile of a sweep tells you which
    function ran most often *across* the sweep, which is how a slow export hides behind
    twenty fast reads.
    """
    rows = []
    for (filename, lineno, name), (_calls, _ncalls, inner, cumulative, _callers) in stats.stats.items():
        if not _interesting_frame(filename, name):
            continue
        rows.append((cumulative, inner, f"{name} ({Path(filename).name}:{lineno})"))
    rows.sort(reverse=True)
    return [f"{cumulative * 1000:8.1f} ms total, {inner * 1000:8.1f} ms own  {where}" for cumulative, inner, where in rows[:top]]


def measure(app_module, session, selected, iterations: int, *, per_case: bool, top: int) -> list[dict]:
    results = []
    for label, method, path, kwargs, headers in selected:
        samples: list[float] = []
        response = None
        # Timing first, profiling after: ``cProfile`` roughly doubles the cost of every
        # call it hooks, so a profiled run is a bad latency sample. The profile gets one
        # extra, untimed run of the same case instead.
        for _ in range(iterations):
            harness.reset_rate_limits(app_module)
            started = time.perf_counter()
            response = run_case(session, method, path, kwargs, headers)
            samples.append((time.perf_counter() - started) * 1000.0)
        profiler = None
        if per_case:
            profiler = cProfile.Profile()
            profiler.enable()
            harness.reset_rate_limits(app_module)
            run_case(session, method, path, kwargs, headers)
            profiler.disable()
        results.append(
            {
                "label": label,
                "status": response.status_code,
                "bytes": len(response.content),
                "median_ms": statistics.median(samples),
                "p95_ms": sorted(samples)[max(0, int(len(samples) * 0.95) - 1)],
                "runs": iterations,
                "hot": hot_functions(pstats.Stats(profiler), top) if profiler is not None else [],
            }
        )
    return results


def print_latency(results: list[dict], *, slowest: int) -> None:
    print("\nlatency per case (median over the runs; p95 in brackets)")
    print(f"  {'case':<44} {'status':>6} {'bytes':>9}   median")
    print("  " + "-" * 78)
    ranked = sorted(results, key=lambda item: -item["median_ms"])
    for row in ranked:
        flag = "" if row["status"] < 400 else "  <- not a 2xx"
        print(
            f"  {row['label']:<44} {row['status']:>6} {row['bytes']:>9}   "
            f"{row['median_ms']:>8.2f} ms ({row['p95_ms']:.2f}){flag}"
        )
    for row in ranked[:slowest]:
        if not row["hot"]:
            continue
        print(f"\ninside: {row['label']}  ({row['median_ms']:.1f} ms median)")
        for line in row["hot"]:
            print(f"    {line}")


def print_profile(profiler: cProfile.Profile, top: int) -> None:
    stats = pstats.Stats(profiler)
    # ``SortKey.TIME`` is pstats' name for tottime (the other is ``CUMULATIVE``).
    for sort_key, title in ((SortKey.TIME, "time in the function itself"), (SortKey.CUMULATIVE, "including callees")):
        print(f"\ntop {top} by {title}")
        stats.sort_stats(sort_key).print_stats(top)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Profile the API's hot paths on a DB clone")
    parser.add_argument("--iterations", type=int, default=5, help="runs per case (default 5)")
    parser.add_argument("--top", type=int, default=10, help="functions to print per ranking")
    parser.add_argument("--per-case", type=int, default=3, help="profile the N slowest cases one by one (0 to skip)")
    parser.add_argument("--only", default=None, help="substring filter on the case label")
    parser.add_argument("--workers", type=int, default=40, help="bench workers to seed (default 40)")
    parser.add_argument("--days", type=int, default=45, help="working days of shifts to seed (default 45)")
    parser.add_argument("--no-synthetic", action="store_true", help="measure the clone exactly as it is")
    parser.add_argument("--dump", default=None, help="write raw cProfile stats here")
    args = parser.parse_args(argv)

    print("isolation")
    print(f"  live database : {BACKEND_DIR.parent / 'times.db'}")
    print(f"  clone in use  : {harness.DB_PATH}")

    import main  # imported after the harness has redirected DATABASE_PATH

    # The same guard the suite's conftest uses: refuse to measure anything that is not the
    # temp clone, because the failure mode is silently profiling (and writing to) payroll.
    harness.assert_database_isolation()
    # Every file tree the app writes into - see ``harness.FILE_TREES``.
    harness.redirect_file_directories()
    harness.install_outbound_guard()
    harness.reset_database(main)

    if not args.no_synthetic:
        build_dataset(main, args.workers, args.days, verbose=True)

    with TestClient(main.app) as session:
        selected = cases_for(session, args.only)
        if not selected:
            print(f"\nno case matches {args.only!r}")
            return 0

        # The sweep-wide profile and the per-case ones cannot both be active (cProfile
        # refuses to nest), and the per-case attribution is the more useful of the two.
        profiler = None
        if args.per_case <= 0:
            profiler = cProfile.Profile()
            profiler.enable()
        results = measure(
            main,
            session,
            selected,
            max(1, args.iterations),
            per_case=args.per_case > 0,
            top=args.top,
        )
        if profiler is not None:
            profiler.disable()

    print_latency(results, slowest=max(0, args.per_case))
    if profiler is not None:
        print_profile(profiler, args.top)

    if args.dump:
        profiler.dump_stats(args.dump)
        print(f"\nraw stats written to {args.dump} (pstats {args.dump})")

    # The fingerprint of the measurement itself: same dataset, same day, comparable runs.
    print(
        f"\ndataset fingerprint: workers={args.workers} days={args.days} "
        f"iterations={args.iterations} python={sys.version.split()[0]} "
        f"hash={hashlib.sha256(str(results).encode()).hexdigest()[:12]}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:  # harness isolation failures land here
        print(f"\nrefusing to profile: {error}")
        raise SystemExit(1)
