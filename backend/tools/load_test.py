"""Load test the punch path: 50 workers spaced 0.2 s apart, then 30 at once.

Two scenarios, run by one tool but always reported separately - they answer
different questions:

* ``ramp``  - N workers (default 50), one punch each, arrivals ``--gap`` seconds
  apart (default 0.2): a site trickling in through the gate. What matters here is
  whether per-punch latency stays flat while requests keep arriving.
* ``burst`` - M workers (default 30), one punch each, all submitted at the same
  instant: the 04:00 minibus arriving together. What matters here is the queue
  (``face_engine``: capacity 2, queue 64) absorbing the pile-up without
  refusing punches, and what the last worker in the queue waited.

Default mode is ISOLATED: the harness's throwaway database clone, the stubbed
face models, and a real uvicorn server on an ephemeral loopback port. It
measures the HTTP + engine-queue + SQLite path under real concurrency without
touching live data and without paying for TensorFlow. Pass ``--url`` to point
the same two scenarios at a live deployment instead.

    python backend/tools/load_test.py
    python backend/tools/load_test.py --ramp-workers 50 --gap 0.2 --burst-workers 30
    python backend/tools/load_test.py --url https://example.ngrok-free.dev \\
        --user-id 1 --email-or-phone worker@example.test --password ... [--selfie photo.jpg]

The rate limiter
----------------
``/api/v1/attendance/verify`` is limited per client IP (``15/minute`` by
default). A load test is one IP, so the isolated run raises the limit to
``--rate-limit`` (default: effectively off) before the app is imported - and
says so in its output. 429s are always counted and reported, never silently
folded into the failures. A live deployment keeps its own limit; a ramp of 50
in ten seconds *will* meet it, and that is the deployment's honest answer.

Live mode
---------
One account's credentials drive both scenarios (the endpoint requires the
token's owner and the ``worker_id`` field to agree, so per-worker tokens need
per-worker accounts). The ramp therefore alternates Clock In / Clock Out so
consecutive punches do not trip the open-session rule; the burst is 30
concurrent punches from the same account, and the business rules will refuse
most of them *by design* - those refusals are reported under their own status
codes, next to the latency numbers, which are still the point.

Exit codes: 0 measured (a refused punch is a result, not an error);
1 could not run at all (isolation failure, server never came up, live login
refused).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent
TESTS_DIR = BACKEND_DIR / "tests"
PROJECT_ROOT = BACKEND_DIR.parent


def _load_httpx():
    """The HTTP client, whichever name this venv installed it under.

    This venv carries ``httpx2`` (what starlette 1.6's TestClient prefers) and not the
    original ``httpx``; a deployment that has the original should not have to install
    anything. The two share one API for everything used here.
    """
    try:
        import httpx

        return httpx
    except ModuleNotFoundError:
        import httpx2 as httpx  # type: ignore[no-redef]

        return httpx


httpx = _load_httpx()

for _path in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

#: Inside the harness's seeded ``Downtown Tower A`` geofence - the same
#: coordinate every punch test uses, so a window/geofence refusal here would
#: mean the fixture changed, not the load.
INSIDE_DOWNTOWN = "30.05,31.23"

#: First id of the virtual workforce inserted into the clone. High enough to
#: never collide with a seeded or real account id.
LOAD_USER_BASE = 7000

PUNCH_PATH = "/api/v1/attendance/verify"


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
async def punch(client, base_url: str, worker_id: str, headers: dict, action: str, jpeg: bytes) -> dict:
    """One punch, timed. A transport failure is status 0, never an exception."""
    started = time.perf_counter()
    try:
        response = await client.post(
            base_url + PUNCH_PATH,
            data={"worker_id": worker_id, "action": action, "location_input": INSIDE_DOWNTOWN},
            files={"selfie": ("selfie.jpg", jpeg, "image/jpeg")},
            headers=headers,
        )
        return {
            "status": response.status_code,
            "latency": time.perf_counter() - started,
            "detail": "" if response.status_code < 400 else str(response.text)[:140],
        }
    except Exception as exc:  # noqa: BLE001 - a dead connection is a result too
        return {
            "status": 0,
            "latency": time.perf_counter() - started,
            "detail": f"{type(exc).__name__}: {exc}"[:140],
        }


def _client(total: int):
    """One client per scenario: enough connections for everyone to be in flight at once."""
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=max(total, 32)),
        timeout=httpx.Timeout(60.0),
    )


async def run_ramp(roster, gap: float, jpeg: bytes, base_url: str, actions) -> list[dict]:
    """Workers arrive ``gap`` seconds apart; each fires exactly one punch."""
    async with _client(len(roster)) as client:

        async def one(index: int, worker_id: str, headers: dict) -> dict:
            await asyncio.sleep(index * gap)
            return await punch(client, base_url, worker_id, headers, actions(index), jpeg)

        return list(await asyncio.gather(*(one(i, uid, h) for i, (uid, h) in enumerate(roster))))


async def run_burst(roster, jpeg: bytes, base_url: str, actions) -> list[dict]:
    """Every worker submits at the same instant - that is the entire point."""
    async with _client(len(roster)) as client:
        return list(
            await asyncio.gather(
                *(punch(client, base_url, uid, h, actions(i), jpeg) for i, (uid, h) in enumerate(roster))
            )
        )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(len(sorted_values) * pct / 100.0)) - 1))
    return sorted_values[index]


def report(name: str, results: list[dict], wall: float) -> None:
    """Status histogram first (a fast 500 is a failure, not a good number), then latency."""
    statuses = Counter(result["status"] for result in results)
    latencies = sorted(result["latency"] for result in results)

    print(f"\n  {name}")
    print(f"  {'-' * 66}")
    print(f"    punches      : {len(results)} in {wall:.2f} s ({len(results) / wall if wall else 0.0:.1f}/s)")
    histogram = "  ".join(
        f"{code if code else 'conn-error'} x {count}" for code, count in sorted(statuses.items())
    )
    print(f"    statuses     : {histogram}")
    if latencies:
        print(
            f"    latency (s)  : min {latencies[0]:.3f}   median {statistics.median(latencies):.3f}   "
            f"p95 {percentile(latencies, 95):.3f}   max {latencies[-1]:.3f}"
        )
    refusals = [(position, result) for position, result in enumerate(results, start=1) if result["status"] >= 400]
    for position, result in refusals[:5]:
        print(f"    ! worker #{position}: {result['status']} {result['detail']}")
    if len(refusals) > 5:
        print(f"    ! ... and {len(refusals) - 5} more refused")


# --------------------------------------------------------------------------- #
# Isolated mode: the harness clone + a real uvicorn server
# --------------------------------------------------------------------------- #
def prepare_load_roster(app_module, count: int) -> list[tuple[str, dict]]:
    """``count`` worker accounts with enrolled templates, in the throwaway clone only.

    Written straight into the clone database (the same choice
    ``tools/profile_endpoints.py`` makes): going through the admin API would
    hash a password per account and dominate the setup time. One hash is
    computed and reused, so every row is still a valid account.
    """
    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        columns_present = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        shared_hash = app_module.pwd_context.hash("load-test-only-password")
        columns = ["id", "name", "email", "phone", "password_hash", "role"]
        if "status" in columns_present:
            columns.append("status")
        rows = []
        for index in range(count):
            user_id = str(LOAD_USER_BASE + index + 1)
            values = [user_id, f"Load Worker {index + 1}", f"load{index + 1}@example.test", "", shared_hash, "worker"]
            if "status" in columns_present:
                values.append("active")
            rows.append(values)
        conn.executemany(
            f"INSERT OR REPLACE INTO users ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    # A punch without a template answers 404 ("Facial reference not registered")
    # before it ever reaches the model queue - which would make the load test
    # measure the refusal path. Enroll every virtual worker the way the harness
    # enrolls the seeded ones.
    roster = []
    for index in range(count):
        user_id = str(LOAD_USER_BASE + index + 1)
        harness.seed_reference(user_id)
        roster.append((user_id, harness.bearer(user_id)))
    return roster


def clear_load_activity() -> None:
    """Wipe the punch state the scenarios leave behind - in the throwaway clone only.

    The ramp opens sessions for its 50 workers; without this the burst would measure
    30 "Already clocked in!" refusals instead of 30 punches racing the gate. Called
    before each scenario so the two scenarios are independent measurements.
    """
    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM active_sessions")
        conn.execute("DELETE FROM attendance_logs WHERE CAST(worker_id AS INTEGER) >= ?", (LOAD_USER_BASE,))
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def serve_isolated(app):
    """Serve the app over real HTTP on an ephemeral loopback port.

    A real uvicorn server rather than ``TestClient``: thirty concurrent punches
    have to run through an actual ASGI stack with an actual connection backlog,
    which is the thing being measured. The lifespan runs once here (nothing
    else in this process has started the app), so the gate and the watchers
    behave exactly as a deployment's would.
    """
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="load-test-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if server.started and server.servers:
            break
        if not thread.is_alive():
            raise RuntimeError("the load-test server thread exited before it was ready")
        time.sleep(0.05)
    else:
        raise RuntimeError("the load-test server did not come up within 60s")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def run_isolated(args) -> int:
    """Both scenarios against the harness clone, with the face models stubbed."""
    # The limiter is read once, when ``config.settings`` is built - which importing
    # ``harness`` already triggers (its import chain reaches ``config``). So this has to
    # happen before that import, not merely before ``import main``.
    os.environ["ATTENDANCE_RATE_LIMIT"] = args.rate_limit

    import harness

    # The temp working directory, which ``harness`` no longer enters at import because doing
    # so breaks pytest-xdist's collection (see safety rule 1 in its docstring).
    harness.enter_temp_root()

    import main  # noqa: E402 - after the env it reads

    harness.assert_database_isolation()
    # Every file tree the app writes into, so a load test's punches do not file frames in the
    # checkout. See ``harness.FILE_TREES``.
    harness.redirect_file_directories()
    harness.install_outbound_guard()
    harness.reset_database(main)

    total = max(args.ramp_workers, args.burst_workers)
    print("isolated load test")
    print(f"  database      : {harness.DB_PATH} (throwaway clone)")
    print(f"  face models   : stubbed (this measures HTTP + queue + SQLite, not inference)")
    print(f"  rate limit    : {args.rate_limit} (raised from the shipped 15/minute; one IP)")
    print(f"  roster        : {total} virtual workers, enrolled")

    roster = prepare_load_roster(main, total)

    import face_engine  # noqa: E402 - stats are printed after each scenario

    def action_for_ramp(index: int) -> str:
        return "Clock In"

    def action_for_burst(index: int) -> str:
        return "Clock In"

    with serve_isolated(main.app) as base_url:
        jpeg = harness.jpeg_bytes()
        scenarios = []
        if args.ramp_workers > 0:
            scenarios.append(
                (
                    f"ramp: {args.ramp_workers} workers, {args.gap}s apart",
                    run_ramp(roster[: args.ramp_workers], args.gap, jpeg, base_url, action_for_ramp),
                )
            )
        if args.burst_workers > 0:
            scenarios.append(
                (
                    f"burst: {args.burst_workers} workers at once",
                    run_burst(roster[: args.burst_workers], jpeg, base_url, action_for_burst),
                )
            )

        for index, (name, scenario) in enumerate(scenarios):
            clear_load_activity()
            harness.reset_rate_limits(main)
            started = time.perf_counter()
            results = asyncio.run(scenario)
            report(name, results, time.perf_counter() - started)
            engine = face_engine.stats()
            print(
                f"    face engine  : completed {engine['completed']}, refused {engine['refused']}, "
                f"slowest {engine['slowest_ms']} ms (counters are cumulative for the run)"
            )
            if index < len(scenarios) - 1:
                print()  # a blank line between the two scenarios' reports

    return 0


# --------------------------------------------------------------------------- #
# Live mode: a real deployment over --url
# --------------------------------------------------------------------------- #
def default_selfie_path() -> Path | None:
    """The same convention ``backend/test_load.py`` used: a photo from worker_photos/."""
    candidates = sorted((PROJECT_ROOT / "worker_photos").glob("*.jpg"))
    return candidates[0] if candidates else None


def run_live(args) -> int:
    """Both scenarios against a deployment. One account; see the docstring caveat."""
    base_url = args.url.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        try:
            probe = client.get(base_url + "/api/v1/status")
        except Exception as exc:  # noqa: BLE001
            print(f"cannot reach {base_url}: {type(exc).__name__}: {exc}")
            return 1
        if probe.status_code >= 400:
            print(f"{base_url}/api/v1/status answered {probe.status_code}; check the URL")
            return 1

        login = client.post(
            base_url + "/api/v1/auth/login",
            json={
                "user_id": args.user_id,
                "email_or_phone": args.email_or_phone,
                "password": args.password,
            },
        )
        if login.status_code != 200:
            print(f"login refused ({login.status_code}): {login.text[:200]}")
            return 1
        token = login.json().get("access_token") or login.json().get("token")

    selfie_path = Path(args.selfie) if args.selfie else default_selfie_path()
    if selfie_path is None or not selfie_path.exists():
        print("no selfie to send: pass --selfie, or put a .jpg in worker_photos/")
        return 1
    jpeg = selfie_path.read_bytes()
    print(f"live load test against {base_url} (account {args.user_id}, selfie {selfie_path.name})")
    print("  the deployment keeps its own 15/minute attendance limit - 429s are the server answering, and are reported")

    headers = {"Authorization": f"Bearer {token}"}

    def action_for_ramp(index: int) -> str:
        # One account: alternate so a committed Clock In does not make the next
        # punch an open-session refusal. The burst cannot alternate (it is one
        # instant) and is reported with that caveat.
        return "Clock In" if index % 2 == 0 else "Clock Out"

    def action_for_burst(index: int) -> str:
        return "Clock In"

    scenarios = []
    if args.ramp_workers > 0:
        scenarios.append(
            (
                f"ramp: {args.ramp_workers} punches, {args.gap}s apart (alternating in/out)",
                run_ramp([(args.user_id, headers)] * args.ramp_workers, args.gap, jpeg, base_url, action_for_ramp),
            )
        )
    if args.burst_workers > 0:
        scenarios.append(
            (
                "burst: " + str(args.burst_workers) + " punches at once, one account "
                "(business rules will refuse most - the latencies are the measurement)",
                run_burst([(args.user_id, headers)] * args.burst_workers, jpeg, base_url, action_for_burst),
            )
        )
    for name, scenario in scenarios:
        started = time.perf_counter()
        results = asyncio.run(scenario)
        report(name, results, time.perf_counter() - started)
        print()

    return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load test the punch path: a ramp, then a burst")
    parser.add_argument("--ramp-workers", type=int, default=50, help="workers arriving one by one (default 50)")
    parser.add_argument("--gap", type=float, default=0.2, help="seconds between ramp arrivals (default 0.2)")
    parser.add_argument("--burst-workers", type=int, default=30, help="workers punching at the same instant (default 30)")
    parser.add_argument("--url", default=None, help="a live deployment to test instead of the isolated clone")
    parser.add_argument("--user-id", default="1", help="live mode: account id to punch as")
    parser.add_argument("--email-or-phone", default=None, help="live mode: the account's login email/phone")
    parser.add_argument("--password", default=None, help="live mode: the account's password")
    parser.add_argument("--selfie", default=None, help="live mode: the jpg to send as the selfie")
    parser.add_argument("--rate-limit", default="1000000/minute",
                        help="isolated mode: attendance limit to import the app with (default: effectively off)")
    args = parser.parse_args(argv)

    if args.url:
        if not (args.email_or_phone and args.password):
            print("live mode needs --email-or-phone and --password (one login, then the token drives every punch)")
            return 1
        return run_live(args)
    return run_isolated(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:  # harness isolation failures land here
        print(f"\nrefusing to load test: {error}")
        raise SystemExit(1)
