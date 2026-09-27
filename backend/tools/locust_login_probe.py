"""Throwaway probe: how many logins per second does the deployment actually serve?

The stress ladder showed the *punch* path flat to 50 simultaneous workers while every
concurrent login waited ~5 s (at 20) to ~11 s (at 50). That shape is a queue, not a load:
bcrypt is CPU-bound and the deployment appears to have about one usable core for it. This
probe confirms it without needing any roster - it fires the same admin login at increasing
concurrency and reports each wave's wall clock, which is the throughput ceiling.

Not a real user behaviour test; it deliberately removes the think time so the queue is the
only thing measured. Run it against a deployment you own.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import urllib.request

HOST = os.environ.get("PROBE_HOST", "https://al-jehad-production.up.railway.app")
USER_ID = os.environ.get("PROBE_USER_ID", "5000")
LOGIN = os.environ.get("PROBE_LOGIN", "admin@siteops.com")
PASSWORD = os.environ.get("PROBE_PASSWORD", "admin12345")

PAYLOAD = json.dumps(
    {"user_id": USER_ID, "email_or_phone": LOGIN, "password": PASSWORD}
).encode()


def one_login() -> tuple[float, int]:
    request = urllib.request.Request(
        HOST + "/api/v1/auth/login",
        data=PAYLOAD,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
            code = response.status
    except urllib.error.HTTPError as error:
        error.read()
        code = error.code
    except Exception as error:  # noqa: BLE001 - the probe reports whatever happens
        print(f"    transport error: {error!r}")
        code = 0
    return (time.perf_counter() - start) * 1000.0, code


def wave(level: int) -> None:
    latencies: list[float] = []
    codes: dict[int, int] = {}
    lock = threading.Lock()

    def worker() -> None:
        ms, code = one_login()
        with lock:
            latencies.append(ms)
            codes[code] = codes.get(code, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(level)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started

    latencies.sort()
    print(
        f"  {level:>3} concurrent: wall {wall * 1000:>7.0f} ms  "
        f"throughput {level / wall:>5.2f} logins/s  "
        f"min {latencies[0]:>6.0f}  median {statistics.median(latencies):>6.0f}  "
        f"max {latencies[-1]:>7.0f} ms  codes {codes}"
    )


def main() -> int:
    print(f"login throughput probe -> {HOST}")
    print(f"  account {USER_ID} ({LOGIN}), {len(sys.argv) - 1} level(s) requested")
    levels = [int(arg) for arg in sys.argv[1:]] or [1, 8, 16, 32, 64]
    for level in levels:
        wave(level)
        time.sleep(2)  # let the queue drain between waves
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
