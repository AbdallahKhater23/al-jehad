"""Create load-test worker accounts on a deployment, enrol a face for each, and write the
Locust punch roster.

``locustfile.py`` can only drive workers that **exist and are enrolled** - the punch answers
404 with no template and refuses "Already clocked in!" for a second greenlet on one account,
so ``N`` simulated workers means ``N`` real accounts. This tool is how those accounts come to
exist, using only endpoints the console itself uses:

* ``POST /api/v1/auth/login``            - sign in as an administrator
* ``GET  /api/v1/admin/users``           - find the ids in use
* ``POST /api/v1/admin/users/create``    - create the account **and** write its face template
  in one multipart request (``user_id, name, role, password, email, phone, transit_enabled,
  photo``); this is the console's Credentials-tab call, and it runs the password policy, the
  upload policy and the face embedding before it writes anything, so a photo that cannot be a
  template leaves no half-created account behind.

It deliberately does **not** use ``/admin/enroll`` afterwards: ``create`` already enrols, and
a second enrollment would bump ``template_version`` and rewrite the very file the punch is
about to be measured against for no reason.

The worker id band is ``1-499``. ``security``'s per-role ranges are gone, and the server's
walk-up allocator counts upward from the highest id in use rather than scanning for the lowest
free one, so a provisioning run and an approval can aim at the same number - in a load test that
is one refused ``create``, not a wrong account. Account ids are recycled in this system, so the
free ids here are the lowest unused numbers in that band rather than numbered lazily upward.

Usage
-----
    LOCUST_ADMIN_ID=5000 LOCUST_ADMIN_LOGIN=admin@siteops.com \\
    LOCUST_ADMIN_PASSWORD=... \\
    python backend/tools/locust_provision.py \\
        --host https://al-jehad-production.up.railway.app \\
        --count 20 --photo-dir worker_photos --out /tmp/roster.json

``--dry-run`` logs in, reads the roster and prints exactly which ids and photos it *would*
use, creating nothing. Run it first.

These are **real accounts on a live deployment**. That is the point of the tool - a punch has
to belong to an enrolled person - but it means the accounts it creates are visible to the
administrators of that deployment. The tool prints the ids it created so the follow-up is a
copy-paste.

Repeating a run
---------------
They cannot simply be deleted afterwards. ``/admin/users/delete`` refuses any account that has
attendance records - those shifts are what the reports are paid from, and the endpoint's rule
is *"an account with attendance records is not deletable, it is deactivatable"*. A punch run
leaves such records by definition, so a second run **reuses the first run's accounts** rather
than creating another batch:

    # re-enable the accounts in an existing roster, then run the punch test again
    LOCUST_ADMIN_PASSWORD=... python backend/tools/locust_provision.py \\
        --host https://al-jehad-production.up.railway.app \\
        --roster /tmp/roster.json --reactivate

    # ... run locust ...

    # and turn their access back off when you are done
    LOCUST_ADMIN_PASSWORD=... python backend/tools/locust_provision.py \\
        --host https://al-jehad-production.up.railway.app \\
        --roster /tmp/roster.json --deactivate

``--reactivate`` / ``--deactivate`` read a roster (``--roster``, defaulting to ``--out``), sign
in as the administrator and call ``POST /api/v1/admin/users/status`` for each account. Neither
creates an account, and neither needs ``--photo-dir``.

``--reactivate`` does one more thing, because the status write alone would not be enough to
repeat a run: **deactivating an account deletes its face template from disk**, deliberately - the
API's own words are "a departed worker's face should not stay enrolled for a clock-in they will
never make" - and reactivating "restores the sign-in and nothing else". An account flipped back
on can therefore sign in and *cannot punch*. So ``--reactivate`` re-enrols every account whose
template is gone, from the roster's own ``selfie`` (``POST /api/v1/admin/enroll``), and then
re-reads ``/admin/users`` to report each account's status and whether it has a face. This is why
the roster round-trips: it carries exactly the two things needed to put an account back - the
password and the image it was enrolled with.

``--deactivate`` is the honest way to leave a deployment as it was found. Note it is a trade
rather than a reversible toggle: the templates go with it, so a later ``--reactivate`` re-enrols
them from the roster. An account is only usable if its password has also not been rotated since -
the roster holds the password it was created with, so a password reset makes it stale.

One-step run
------------
``--run`` does the whole cycle - reactivate (re-enrolling any face the deactivation removed),
drive the punch load test, then take the access away again - and reports the numbers:

    LOCUST_ADMIN_PASSWORD=... python backend/tools/locust_provision.py \\
        --host https://al-jehad-production.up.railway.app \\
        --roster /tmp/roster.json --run

It sizes the run itself: one worker per account (``--users``, default the whole roster), a
``--gap`` of 0.2 s between consecutive workers' first punch, ``--shift`` seconds on site,
``--lead-in`` seconds allowed for the logins, and a Locust ``-t`` computed from those three so
nobody has to guess. The site to punch from is read from ``/admin/sites`` unless ``--site-fix``
says otherwise.

The result table is parsed from Locust's own CSV rather than scraped off its console output: how
many checked in and out, and min/median/avg/p95/max in ms for each punch endpoint.

The restoration is in a ``finally``, so a load test that crashes - or one you stop with Ctrl+C -
still leaves the accounts switched off, because the test was the only reason for them to have
access. Deactivating also force-closes any shift a worker was still on, so nothing is left on the
Live Ops board with an owner who can no longer sign in.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

WORKER_ID_RANGE = (1, 499)
CREATE_PATH = "/api/v1/admin/users/create"
LOGIN_PATH = "/api/v1/auth/login"
ROSTER_PATH = "/api/v1/admin/users"
STATUS_PATH = "/api/v1/admin/users/status"
ENROLL_PATH = "/api/v1/admin/enroll"
ACTIVE_PATH = "/api/v1/admin/active_sessions"
FORCE_OUT_PATH = "/api/v1/admin/force_clock_out"
SITES_PATH = "/api/v1/admin/sites"

#: The Locust file this tool drives, and the punch endpoint's name in its CSV output.
LOCUSTFILE = Path(__file__).with_name("locustfile.py")
PUNCH_NAME_PREFIX = "/api/v1/attendance/verify"
AUTH_NAME_PREFIX = "/api/v1/auth/login"
#: Must match ``locustfile.FRESH_LOGIN_NAME``: the request name a Phase-3 cold login is recorded
#: under, which is how the auth threshold is kept off the one-time lead-in mints.
PHASE3_FRESH_LOGIN_NAME = "/api/v1/auth/login [phase3 fresh]"

#: Phase-3 pass/fail thresholds, and why each number is where it is.
#:
#: * ``auth_p95_ms`` / ``auth_p99_ms`` - a cold login is the one thing here that serialises on
#:   the single core (measured ceiling ~4.75 logins/s, ~13.4 s at 64 concurrent). 5 s p95 is the
#:   point past which a phone on a shift-start network shows a spinner a person gives up on;
#:   15 s p99 admits the genuine tail without admitting a pathological queue.
#: * ``punch_p95_ms`` - the face path held ~930 ms at 50 concurrent in Phase 2; 3 s is the same
#:   gate budget the ladder used, kept identical so a Phase-3 number is comparable to a Phase-2 one.
#: * ``verify_503`` - the face engine's overflow answer (503 + Retry-After). Zero is the only
#:   acceptable count: any 503 is a worker being told to come back later.
#: * ``fail_ratio`` - everything that is not a treated 429. 1% tolerates the odd transient; it
#:   is not a budget for a failing path.
#: * ``fresh_share`` - a **sanity** floor, not a performance bound: at least this share of the
#:   configured cold cohort must actually log in, or the blend silently did not run and the auth
#:   threshold measured nothing at all.
PHASE3_THRESHOLDS = {
    "auth_p95_ms": 5000.0,
    "auth_p99_ms": 15000.0,
    "punch_p95_ms": 3000.0,
    "verify_503": 0,
    "fail_ratio": 0.01,
    "fresh_share": 0.8,
}

#: Strong enough for ``security.validate_password_strength`` (min 8, not a weak word).
DEFAULT_PASSWORD = "Loadtest2026!"

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")


def login(session: requests.Session, host: str, user_id: str, login_id: str, password: str) -> str:
    response = session.post(
        host + LOGIN_PATH,
        json={"user_id": user_id, "email_or_phone": login_id, "password": password},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"admin login refused ({response.status_code}): {response.text[:200]}")
    token = response.json().get("access_token") or response.json().get("token")
    if not token:
        raise SystemExit("admin login answered 200 with no token")
    return token


def used_ids(session: requests.Session, host: str, headers: dict) -> set[int]:
    response = session.get(host + ROSTER_PATH, headers=headers, timeout=60)
    if response.status_code != 200:
        raise SystemExit(f"cannot read the roster ({response.status_code}): {response.text[:200]}")
    ids = set()
    for row in response.json() or []:
        try:
            ids.add(int(row["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def free_worker_ids(taken: set[int], count: int) -> list[int]:
    """The lowest unused ids in the worker band, in order - the same choice the app makes
    for a recycled id, for the same reason (the band is only 499 wide)."""
    low, high = WORKER_ID_RANGE
    free = [value for value in range(low, high + 1) if value not in taken]
    if len(free) < count:
        raise SystemExit(
            f"only {len(free)} free worker ids in {low}-{high}; asked for {count}"
        )
    return free[:count]


def photos_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix.lower() in PHOTO_SUFFIXES and path.stat().st_size > 1024
    )


def create_account(
    session: requests.Session,
    host: str,
    headers: dict,
    *,
    user_id: int,
    name: str,
    password: str,
    email: str,
    photo: Path,
) -> tuple[bool, str]:
    """Create one account with its face template. Returns ``(ok, message)``."""
    with photo.open("rb") as handle:
        response = session.post(
            host + CREATE_PATH,
            headers=headers,
            data={
                "user_id": str(user_id),
                "name": name,
                "role": "worker",
                "password": password,
                "email": email,
                "phone": "",
                # An empty box is the default refusal; the transit grant is not part of this.
                "transit_enabled": "",
            },
            files={"photo": (photo.name, handle, "image/jpeg")},
            timeout=180,
        )
    if response.status_code != 200:
        return False, f"{response.status_code} {response.text[:160]}"
    body = response.json()
    enrolled = bool(body.get("face_enrolled"))
    liveness = body.get("liveness") or {}
    verdict = liveness.get("result", {}).get("verdict") if isinstance(liveness, dict) else None
    return True, f"enrolled={enrolled}" + (f" liveness={verdict}" if verdict else "")


def load_roster_json(path: Path) -> list[dict]:
    """Read a roster written by this tool, dropping entries without an id."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read roster {path}: {exc}")
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("user_id")]


def read_users(session: requests.Session, host: str, headers: dict) -> dict[str, dict]:
    """The deployment's roster keyed by id - used to report the state after a change."""
    response = session.get(host + ROSTER_PATH, headers=headers, timeout=120)
    if response.status_code != 200:
        return {}
    return {str(row["id"]): row for row in response.json() or []}


def enroll_face(
    session: requests.Session,
    host: str,
    headers: dict,
    user_id: str,
    photo: Path,
) -> tuple[bool, str]:
    """Write an account's face template from a file (``POST /api/v1/admin/enroll``).

    Used to put back a template that a deactivation removed. The endpoint takes
    ``worker_id`` plus a ``photo`` upload and rewrites everything a punch is checked against
    under the account's immutable biometric id.
    """
    with photo.open("rb") as handle:
        response = session.post(
            host + ENROLL_PATH,
            data={"worker_id": user_id},
            files={"photo": (photo.name, handle, "image/jpeg")},
            headers=headers,
            timeout=180,
        )
    if response.status_code != 200:
        return False, f"{response.status_code} {response.text[:140]}"
    return True, ""


def close_open_shifts(
    session: requests.Session,
    host: str,
    headers: dict,
    user_ids: set[str],
) -> list[str]:
    """Force-close every shift still open for these accounts, and report which.

    A shift belongs to a person who is about to lose the ability to sign in, so once the
    account is off it can never be closed by its owner: it would sit on the Live Ops board
    for good. ``/admin/force_clock_out`` is the console's own way to end it - the same call an
    administrator makes for a worker who forgot - so the cleanup ends with the board honest.
    """
    response = session.get(host + ACTIVE_PATH, headers=headers, timeout=120)
    if response.status_code != 200:
        print(f"  could not read active_sessions ({response.status_code})")
        return []
    open_ids = [
        str(row["worker_id"])
        for row in response.json() or []
        if str(row.get("worker_id")) in user_ids
    ]
    for user_id in open_ids:
        ended = session.post(
            host + FORCE_OUT_PATH,
            json={"worker_id": user_id},
            headers=headers,
            timeout=120,
        )
        print(f"  closed shift {user_id}: {ended.status_code}")
    return open_ids


def pick_site_fix(session: requests.Session, host: str, headers: dict) -> str | None:
    """The first site's ``"lat,lon"`` - an honest in-geofence fix to punch from.

    Read from the deployment rather than guessed: a coordinate outside every fence is refused
    with 403 *before* the model runs, so a wrong one turns the whole test into a measurement
    of the refusal path.
    """
    response = session.get(host + SITES_PATH, headers=headers, timeout=60)
    if response.status_code != 200:
        return None
    for site in response.json() or []:
        if site.get("lat") is not None and site.get("lon") is not None:
            return f"{site['lat']},{site['lon']}"
    return None


def read_punch_stats(csv_prefix: Path) -> dict:
    """The punch rows out of Locust's ``*_stats.csv``, keyed by request name.

    Parsed rather than scraped from stdout so the numbers in the one-step report are the ones
    Locust actually recorded, and so a run whose console output the operator did not read still
    ends with the counts on screen.
    """
    path = Path(str(csv_prefix) + "_stats.csv")
    if not path.is_file():
        return {}
    rows: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Name", "")
            if not name.startswith(PUNCH_NAME_PREFIX):
                continue
            rows[name] = {
                "requests": int(float(row.get("Request Count") or 0)),
                "failures": int(float(row.get("Failure Count") or 0)),
                "min": int(float(row.get("Min Response Time") or 0)),
                "median": int(float(row.get("Median Response Time") or 0)),
                "avg": int(float(row.get("Average Response Time") or 0)),
                "p95": int(float(row.get("95%") or 0)),
                "max": int(float(row.get("Max Response Time") or 0)),
            }
    return rows


def report_punch_stats(csv_prefix: Path) -> int:
    """Print the one-step summary: how many checked in and out, and what it cost.

    Returns the number of punch requests that failed, so the command's exit status reflects it.
    """
    stats = read_punch_stats(csv_prefix)
    if not stats:
        print(f"\nno punch rows in {csv_prefix}_stats.csv - did the run reach the endpoint?")
        return 0
    print("\npunch results (ms)")
    print(f"  {'endpoint':<34}{'sent':>6}{'failed':>8}{'min':>7}{'median':>8}{'avg':>7}{'p95':>7}{'max':>7}")
    print("  " + "-" * 84)
    failed_total = 0
    for name in sorted(stats):
        row = stats[name]
        failed_total += row["failures"]
        print(
            f"  {name:<34}{row['requests']:>6}{row['failures']:>8}{row['min']:>7}"
            f"{row['median']:>8}{row['avg']:>7}{row['p95']:>7}{row['max']:>7}"
        )
    failures_file = Path(str(csv_prefix) + "_failures.csv")
    if failures_file.is_file():
        with failures_file.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                print(f"  ! {row.get('Name')}: {row.get('Error')} x{row.get('Occurrences')}")
    clock_in = next((stats[n] for n in stats if "Clock In" in n), None)
    clock_out = next((stats[n] for n in stats if "Clock Out" in n), None)
    print(
        f"\n  checked in : {clock_in['requests'] if clock_in else 0}"
        f"   checked out : {clock_out['requests'] if clock_out else 0}"
    )
    return failed_total


def read_all_stats(csv_prefix: Path) -> dict:
    """Every row of Locust's ``*_stats.csv``, keyed by request name, percentiles included.

    ``read_punch_stats`` narrows to the punch endpoints; Phase 3 has to read *two* families -
    the punches and the logins - in one run, so it reads the whole file.
    """
    path = Path(str(csv_prefix) + "_stats.csv")
    if not path.is_file():
        return {}
    rows: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Name") or ""
            if not name or name == "Aggregated":
                continue
            rows[name] = {
                "requests": int(float(row.get("Request Count") or 0)),
                "failures": int(float(row.get("Failure Count") or 0)),
                "p50": int(float(row.get("50%") or 0)),
                "p95": int(float(row.get("95%") or 0)),
                "p99": int(float(row.get("99%") or 0)),
                "max": int(float(row.get("Max Response Time") or 0)),
            }
    return rows


def read_failures(csv_prefix: Path) -> list[dict]:
    """The ``*_failures.csv`` rows, for classifying failures (a 503 is not a 500)."""
    path = Path(str(csv_prefix) + "_failures.csv")
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def set_active_batch(
    session: requests.Session,
    host: str,
    headers: dict,
    roster_path: Path,
    *,
    active: bool,
    hint: bool = True,
) -> int:
    """Turn a roster's accounts back on (or off), then report what the deployment says.

    ``hint`` controls the "now run the punch test with this command" tail, which is useful from
    the standalone flag and redundant from ``--run`` (which is already doing it).

    Nothing is created and no new account is made: ``/admin/users/status`` is the console's own
    switch, and it is the supported way to leave the deployment as it was found, because
    ``/admin/users/delete`` refuses an account that has shifts.

    Reactivating is **not** just a status write, and the reason is a deliberate decision on the
    other side of the API: deactivating an account deletes its face template from disk ("a
    departed worker's face should not stay enrolled for a clock-in they will never make"), and
    reactivating restores the sign-in and nothing else. An account flipped back on is therefore
    one that can sign in and **cannot punch** until a face exists again - so this re-enrols any
    account whose template is gone from the roster's own ``selfie``, which is the same image it
    was created with.
    """
    roster = load_roster_json(roster_path)
    if not roster:
        print(f"no usable roster at {roster_path}")
        return 1

    print(f"{'reactivate' if active else 'deactivate'} {len(roster)} account(s) "
          f"from {roster_path} on {host}")
    if not active:
        # End anything still running first: a shift whose owner can no longer sign in can never
        # be closed by them, so it would sit on the Live Ops board for good.
        close_open_shifts(session, host, headers, {str(entry["user_id"]) for entry in roster})
    failures = []
    for entry in roster:
        user_id = str(entry["user_id"])
        response = session.post(
            host + STATUS_PATH,
            json={"user_id": user_id, "active": active},
            headers=headers,
            timeout=60,
        )
        if response.status_code == 200:
            print(f"  {user_id:>4} ok")
        else:
            failures.append(user_id)
            print(f"  {user_id:>4} FAIL  {response.status_code} {response.text[:120]}")

    # Re-read rather than trusting the writes: the point of the command is the state of the
    # deployment, and a status write that answered 200 is not the same claim as an account
    # that now reads back active with a face on disk.
    users = read_users(session, host, headers)
    ids = [str(entry["user_id"]) for entry in roster]
    present = [user_id for user_id in ids if user_id in users]
    missing = [user_id for user_id in ids if user_id not in users]

    if active and present:
        # See the docstring: a deactivation removed these templates on purpose, so a bare
        # status flip would hand the test accounts that cannot punch. Put the faces back from
        # the roster's own selfie - the image each account was created with.
        by_id = {str(entry["user_id"]): entry for entry in roster}
        need_face = [user_id for user_id in present if not users[user_id].get("face_enrolled")]
        if need_face:
            print(f"  no face on {len(need_face)} account(s); re-enrolling from the roster")
        for user_id in need_face:
            selfie = by_id[user_id].get("selfie")
            if not selfie or not Path(selfie).is_file():
                failures.append(user_id)
                print(f"  {user_id:>4} FAIL  no readable selfie at {selfie!r}")
                continue
            ok, message = enroll_face(session, host, headers, user_id, Path(selfie))
            if not ok:
                failures.append(user_id)
            print(f"  {user_id:>4} {'enrolled' if ok else 'FAIL'}  {message}".rstrip())
        users = read_users(session, host, headers)

    statuses = sorted(
        {str(users[user_id].get("status")) for user_id in present if user_id in users}
    )
    enrolled = sum(1 for user_id in present if users.get(user_id, {}).get("face_enrolled"))
    print(f"  present  : {len(present)}/{len(ids)}")
    print(f"  status   : {statuses}")
    print(f"  enrolled : {enrolled}/{len(present)} have a face")
    if missing:
        print(f"  not found: {missing}")
    if failures:
        print(f"  failed   : {sorted(set(failures))}")
        return 2

    ready = (
        active
        and present
        and enrolled == len(present)
        and all(str(users[user_id].get("status")) == "active" for user_id in present)
    )
    if ready and hint:
        print("\nall accounts are active and enrolled. Run the punch test with:")
        print(f"  LOCUST_ONLY=punch LOCUST_ROSTER={roster_path} LOCUST_SITE_FIX=\"lat,lon\" \\\n"
              f"  locust -f backend/tools/locustfile.py --headless --host {host} "
              f"-u {len(present)} -r 2 -t 10m --only-summary")
        print(f"\nturn their access off again afterwards with: "
              f"--roster {roster_path} --deactivate")
    return 0


def locust_command(host: str, users: int, run_time: str, csv_prefix: Path, spawn_rate: float) -> list[str]:
    """The Locust invocation the single run and every stress stage share."""
    return [
        sys.executable, "-m", "locust",
        "-f", str(LOCUSTFILE),
        "--headless",
        "--host", host,
        "-u", str(users),
        "-r", f"{spawn_rate:g}",
        "-t", run_time,
        "--csv", str(csv_prefix),
        "--only-summary",
    ]


def run_stress(
    session: requests.Session,
    host: str,
    headers: dict,
    args,
    roster_path: Path,
    roster: list[dict],
) -> int:
    """Step the load up until the deployment says no.

    Each stage is a **complete** Locust run of its own, not a slice of one long run, and that is
    the point: a cumulative p95 blends the cheap early stages into the expensive ones, so a
    deployment already failing at 40 workers still reads as healthy overall. A stage's numbers
    are that stage's alone.

    Within a stage every worker punches simultaneously (``LOCUST_PUNCH_GAP=0``), one shift each,
    so a stage answers the question the face engine actually queues on - how many people can hit
    the gate at once. The ladder stops at the first stage whose p95, failure ratio, or shortfall
    in completed punches crosses a threshold, and reports the stage below it as the last one that
    held. One campaign therefore ends with a number rather than a graph to interpret.
    """
    ceiling = min(args.stress_max or len(roster), len(roster))
    if ceiling < args.stress_start:
        print(f"only {len(roster)} accounts, and a ladder needs at least --stress-start "
              f"({args.stress_start}); provision a bigger roster first")
        return 1

    site_fix = args.site_fix or pick_site_fix(session, host, headers)
    if not site_fix:
        print("no --site-fix and no site readable from the deployment")
        return 1

    stages = list(range(args.stress_start, ceiling + 1, args.stress_step))
    if not stages or stages[-1] != ceiling:
        stages.append(ceiling)
    # Generous on purpose. A stage that ends while workers are still on site both under-reports
    # the check-outs and hands the next stage accounts that are already clocked in - and the
    # second effect turns a clean deployment into a fake 400 "Already clocked in!" breach. The
    # cost of the slack is wall-clock; the cost of not having it is a wrong answer.
    run_time = f"{int(args.lead_in + args.stage_seconds) + 60}s"
    roster_ids = {str(entry["user_id"]) for entry in roster}

    print(f"\nstress ladder   : {stages} workers")
    print(f"  per stage    : {args.stage_seconds}s shift, {run_time} of run time, "
          f"workers punch simultaneously")
    print(f"  stop when    : p95 > {args.max_p95:g} ms, or > {args.max_fail_ratio:.0%} failed, "
          f"or a worker cannot complete a punch")
    print(f"  site fix     : {site_fix}")

    rows: list[dict] = []
    broke_at = None
    for users in stages:
        csv_prefix = roster_path.with_name(f"locust_stress_{users}")
        env = dict(os.environ)
        env.update(
            LOCUST_ONLY="punch",
            LOCUST_ROSTER=str(roster_path),
            LOCUST_SITE_FIX=site_fix,
            # Set here rather than inherited: the locustfile gates every punch on this flag, and
            # a ladder that silently ran with it off would report "only 0/N completed a
            # clock-in" as its very first breach - a measurement of a missing env var, not of
            # the deployment.
            LOCUST_ALLOW_PUNCH="1",
            LOCUST_PUNCH_GAP="0",
            LOCUST_ONE_SHIFT="1",
            LOCUST_SHIFT_SECONDS_MIN=str(args.stage_seconds),
            LOCUST_SHIFT_SECONDS_MAX=str(args.stage_seconds),
            LOCUST_RAMP_LEAD_IN=str(args.lead_in),
        )
        print(f"\n--- stage: {users} workers at once ---")
        # Every stage starts from the same state, or it measures the last stage's leftovers.
        closed = close_open_shifts(session, host, headers, roster_ids)
        if closed:
            print(f"  (cleared {len(closed)} shift(s) left open by the previous stage)")
        subprocess.run(locust_command(host, users, run_time, csv_prefix, args.spawn_rate),
                       env=env, check=False)

        stats = read_punch_stats(csv_prefix)
        clock_in = next((stats[name] for name in stats if "Clock In" in name), None)
        clock_out = next((stats[name] for name in stats if "Clock Out" in name), None)
        sent = sum(row["requests"] for row in stats.values())
        failed = sum(row["failures"] for row in stats.values())
        p95 = max((row["p95"] for row in stats.values()), default=0)
        worst = max((row["max"] for row in stats.values()), default=0)
        checked_in = clock_in["requests"] if clock_in else 0
        checked_out = clock_out["requests"] if clock_out else 0
        failure_ratio = (failed / sent) if sent else 1.0

        breached = []
        if failure_ratio > args.max_fail_ratio:
            breached.append(f"{failure_ratio:.0%} of requests failed")
        if p95 > args.max_p95:
            breached.append(f"p95 {p95} ms")
        if checked_in < users:
            breached.append(f"only {checked_in}/{users} completed a clock-in")
        rows.append(
            {
                "users": users,
                "in": checked_in,
                "out": checked_out,
                "failed": failed,
                "p95": p95,
                "max": worst,
                "verdict": ", ".join(breached) or "ok",
            }
        )
        print(f"  checked in {checked_in}/{users}, out {checked_out}, failed {failed}, "
              f"p95 {p95} ms, max {worst} ms")
        if breached:
            broke_at = users
            print(f"  BREACH at {users} workers: {', '.join(breached)}")
            break

    print("\nstress ladder results")
    print(f"  {'workers':>8}{'in':>5}{'out':>5}{'failed':>8}{'p95 ms':>9}{'max ms':>9}   verdict")
    print("  " + "-" * 70)
    for row in rows:
        print(f"  {row['users']:>8}{row['in']:>5}{row['out']:>5}{row['failed']:>8}"
              f"{row['p95']:>9}{row['max']:>9}   {row['verdict']}")
    healthy = [row["users"] for row in rows if row["verdict"] == "ok"]
    print()
    if broke_at is None:
        print(f"  held to {rows[-1]['users']} workers - the roster's limit, not the "
              f"deployment's. Provision more accounts and raise --stress-max to push further.")
    else:
        print(f"  first breach at {broke_at} workers; "
              f"last healthy stage: {healthy[-1] if healthy else 'none'}")
    return 1 if broke_at is not None else 0


def run_punch_test(
    session: requests.Session,
    host: str,
    headers: dict,
    args,
    roster_path: Path,
    roster: list[dict],
) -> int:
    """Run the Locust punch test against ``host`` and report its punch rows. Returns failures."""
    users = args.users or len(roster)
    if users > len(roster):
        # One account per simulated worker, because a second greenlet on one account is refused
        # "Already clocked in!". Saying so beats a run whose extra workers punch nothing.
        print(f"  note: {users} workers but {len(roster)} accounts; capping at {len(roster)}")
        users = len(roster)

    site_fix = args.site_fix or pick_site_fix(session, host, headers)
    if not site_fix:
        print("no --site-fix and no site readable from the deployment; a punch outside every")
        print("geofence is refused with 403, so the run would measure the refusal path")
        return 1

    run_time = args.run_time or f"{int(args.lead_in + users * args.gap + args.shift) + 25}s"
    csv_prefix = Path(args.csv_prefix) if args.csv_prefix else roster_path.with_name("locust_punch")

    env = dict(os.environ)
    env.update(
        LOCUST_ONLY="punch",
        LOCUST_ROSTER=str(roster_path),
        LOCUST_SITE_FIX=site_fix,
        # See run_stress: without this the punches never happen and every number read back is a
        # zero dressed up as a deployment failure.
        LOCUST_ALLOW_PUNCH="1",
        LOCUST_PUNCH_GAP=str(args.gap),
        LOCUST_SHIFT_SECONDS_MIN=str(args.shift),
        LOCUST_SHIFT_SECONDS_MAX=str(args.shift),
        LOCUST_RAMP_LEAD_IN=str(args.lead_in),
        # One shift per worker, always: a single run is one pass, so "checked in 20, checked out
        # 20" is a claim about twenty shifts rather than however many loops fitted in.
        LOCUST_ONE_SHIFT="1",
    )
    command = locust_command(host, users, run_time, csv_prefix, args.spawn_rate)
    print(f"\nrunning: {' '.join(command)}")
    print(f"  plan      : {users} workers, {args.gap}s apart, {args.shift}s shift, "
          f"{args.lead_in}s lead-in, {run_time} of run time")
    print(f"  site fix  : {site_fix}")
    completed = subprocess.run(command, env=env, check=False)
    failed = report_punch_stats(csv_prefix)
    if completed.returncode != 0 and not failed:
        # Locust exits non-zero for its own reasons too (an aborted run, bad arguments); with no
        # failed request recorded, that exit status is the only signal there was a problem.
        print(f"  locust exited {completed.returncode} with no failed requests")
    return failed


# --------------------------------------------------------------------------- #
# Phase 3: the realistic blend
# --------------------------------------------------------------------------- #
def _expected_fresh(users: int, ratio: float) -> int:
    """How many of ``users`` accounts the locustfile marks cold, for the sanity floor.

    Mirrors ``locustfile._fresh_stride``: every ``round(1/ratio)``-th ordinal is fresh.
    """
    if ratio <= 0:
        return 0
    stride = max(1, round(1.0 / ratio))
    return ((users - 1) // stride) + 1


def evaluate_phase3(csv_prefix: Path, users: int, ratio: float, args) -> int:
    """Read a Phase-3 run back and apply the thresholds. Returns 0 only if every one holds."""
    stats = read_all_stats(csv_prefix)
    if not stats:
        print(f"\nno rows in {csv_prefix}_stats.csv - did the run reach the deployment?")
        return 2
    logins = {n: r for n, r in stats.items() if n.startswith(AUTH_NAME_PREFIX)}
    punch = {n: r for n, r in stats.items() if n.startswith(PUNCH_NAME_PREFIX)}
    # The auth threshold measures the *arrivals* - the cold logins the blend is built on - not the
    # lead-in mints. A mint is harness scaffolding: a phone holding a cached token never calls
    # /auth/login at all, and the lead-in fires the whole cached cohort at once, at t=0, which is
    # a stampede with no counterpart in the field. Judging the cold path by that stampede would
    # fail a deployment whose real cold logins are comfortably fast; the runbook names the two
    # apart ("measure[d] separately from the lead-in mints") for exactly this reason.
    arrivals = {n: r for n, r in logins.items() if n == PHASE3_FRESH_LOGIN_NAME}

    auth_p95 = max((r["p95"] for r in arrivals.values()), default=0)
    auth_p99 = max((r["p99"] for r in arrivals.values()), default=0)
    punch_p95 = max((r["p95"] for r in punch.values()), default=0)
    total_requests = sum(r["requests"] for r in stats.values())
    total_failures = sum(r["failures"] for r in stats.values())
    fail_ratio = (total_failures / total_requests) if total_requests else 1.0
    punch_requests = sum(r["requests"] for r in punch.values())
    verify_503 = sum(
        int(float(row.get("Occurrences") or 0))
        for row in read_failures(csv_prefix)
        if str(row.get("Name") or "").startswith(PUNCH_NAME_PREFIX)
        and "503" in str(row.get("Error") or "")
    )
    fresh_logins = sum(r["requests"] for r in arrivals.values())
    expected_fresh = _expected_fresh(users, ratio)
    fresh_floor = int(expected_fresh * PHASE3_THRESHOLDS["fresh_share"])

    print("\nphase-3 results (ms)")
    print(f"  {'endpoint':<46}{'sent':>6}{'fail':>6}{'p50':>7}{'p95':>7}{'p99':>7}{'max':>7}")
    print("  " + "-" * 90)
    for name in sorted({*logins, *punch}):
        r = stats[name]
        print(f"  {name:<46}{r['requests']:>6}{r['failures']:>6}{r['p50']:>7}"
              f"{r['p95']:>7}{r['p99']:>7}{r['max']:>7}")

    breaches: list[str] = []
    if not punch_requests:
        breaches.append("no punch requests reached /attendance/verify")
    if fresh_logins < fresh_floor:
        breaches.append(
            f"only {fresh_logins} cold logins seen (>= {fresh_floor} of {expected_fresh} "
            f"expected) - the blend did not run, so the auth numbers mean nothing"
        )
    if auth_p95 > args.max_auth_p95_ms:
        breaches.append(f"auth p95 {auth_p95} ms > {args.max_auth_p95_ms:g} ms")
    if auth_p99 > args.max_auth_p99_ms:
        breaches.append(f"auth p99 {auth_p99} ms > {args.max_auth_p99_ms:g} ms")
    if punch_p95 > args.phase3_punch_p95_ms:
        breaches.append(f"punch p95 {punch_p95} ms > {args.phase3_punch_p95_ms:g} ms")
    if verify_503 > args.max_verify_503:
        breaches.append(f"{verify_503} verify 503(s) > {args.max_verify_503}")
    if fail_ratio > args.phase3_max_fail_ratio:
        breaches.append(
            f"{fail_ratio:.1%} of requests failed > {args.phase3_max_fail_ratio:.1%}"
        )

    print()
    print(f"  arrival model   : Poisson, {users} workers, {ratio:.0%} cold logins "
          f"({fresh_logins} seen, expected >= {fresh_floor})")
    mint_logins = sum(r["requests"] for n, r in logins.items() if n != PHASE3_FRESH_LOGIN_NAME)
    print(f"  auth p95 / p99  : {auth_p95} / {auth_p99} ms   "
          f"(<= {args.max_auth_p95_ms:g} / {args.max_auth_p99_ms:g})  [arrivals only]")
    if mint_logins:
        worst_mint = max(
            (r["p95"] for n, r in logins.items() if n != PHASE3_FRESH_LOGIN_NAME), default=0
        )
        print(f"  lead-in mints   : {mint_logins} login(s), worst p95 {worst_mint} ms "
              f"(t=0 scaffolding, not counted)")
    print(f"  punch p95       : {punch_p95} ms   (<= {args.phase3_punch_p95_ms:g})")
    print(f"  verify 503s     : {verify_503}   (<= {args.max_verify_503})")
    print(f"  failure ratio   : {fail_ratio:.2%}   (<= {args.phase3_max_fail_ratio:.2%})")
    print()
    if breaches:
        print("  VERDICT: FAIL")
        for breach in breaches:
            print(f"    - {breach}")
        return 2
    print("  VERDICT: PASS - the cached/cold blend and the burst both held")
    return 0


def run_phase3(
    session: requests.Session,
    host: str,
    headers: dict,
    args,
    roster_path: Path,
    roster: list[dict],
) -> int:
    """Blend cached-session punches with arrival-time logins, on a Poisson arrival process.

    This is the Phase-2 ladder's missing half. Phase 2 held logins and punches apart and spread
    the punches evenly, so it proved each path in isolation and never put the two on the core
    together. Here one run carries both: about four in five workers punch on a token their phone
    already held, about one in five opens the app cold and pays a bcrypt login **at its arrival
    instant**, and the arrivals are a single Poisson process so they cluster into the sub-second
    micro-stampedes a real shift start produces.
    """
    users = args.users or len(roster)
    if users > len(roster):
        print(f"  note: {users} workers but {len(roster)} accounts; capping at {len(roster)}")
        users = len(roster)
    ratio = args.fresh_login_ratio
    if not 0 < ratio <= 1:
        print("--phase3 needs --fresh-login-ratio in (0, 1]")
        return 1
    site_fix = args.site_fix or pick_site_fix(session, host, headers)
    if not site_fix:
        print("no --site-fix and no site readable from the deployment")
        return 1
    rate = args.poisson_rate or (users / args.arrival_window)
    if rate <= 0:
        print("--poisson-rate (or --arrival-window) must be positive")
        return 1
    # The Poisson tail can run well past the mean, so budget double the expected span, then the
    # shift and a fixed slack for the login that precedes each cold punch.
    expected_span = users / rate
    run_time = args.run_time or f"{int(args.lead_in + expected_span * 2.0 + args.shift + 30)}s"
    csv_prefix = Path(args.csv_prefix) if args.csv_prefix else roster_path.with_name("locust_phase3")

    env = dict(os.environ)
    env.update(
        LOCUST_ONLY="punch",
        LOCUST_ROSTER=str(roster_path),
        LOCUST_SITE_FIX=site_fix,
        LOCUST_ALLOW_PUNCH="1",
        LOCUST_ARRIVAL="poisson",
        LOCUST_POISSON_RATE=f"{rate:.6f}",
        LOCUST_POISSON_SEED=str(args.poisson_seed),
        LOCUST_FRESH_LOGIN_RATIO=f"{ratio:.4f}",
        # The metronome is off: the arrival process is the schedule in this mode.
        LOCUST_PUNCH_GAP="0",
        LOCUST_ONE_SHIFT="1",
        LOCUST_SHIFT_SECONDS_MIN=str(args.shift),
        LOCUST_SHIFT_SECONDS_MAX=str(args.shift),
        LOCUST_RAMP_LEAD_IN=str(args.lead_in),
    )
    command = locust_command(host, users, run_time, csv_prefix, args.spawn_rate)
    print(f"\nrunning: {' '.join(command)}")
    print(f"  plan      : {users} workers, Poisson {rate:.3g} arrivals/s (seed {args.poisson_seed}), "
          f"{ratio:.0%} cold logins, {args.shift}s shift, {run_time} of run time")
    print(f"  site fix  : {site_fix}")
    print(f"  thresholds: auth p95 <= {args.max_auth_p95_ms:g} ms, auth p99 <= "
          f"{args.max_auth_p99_ms:g} ms, punch p95 <= {args.phase3_punch_p95_ms:g} ms, "
          f"verify 503 <= {args.max_verify_503}, failures <= {args.phase3_max_fail_ratio:.1%}")
    subprocess.run(command, env=env, check=False)
    return evaluate_phase3(csv_prefix, users, ratio, args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create + enrol load-test worker accounts")
    parser.add_argument("--host", default=os.environ.get("LOCUST_HOST"),
                        help="deployment base URL (default: LOCUST_HOST)")
    parser.add_argument("--count", type=int, default=20, help="accounts to create (default 20)")
    parser.add_argument("--password", default=os.environ.get("LOCUST_WORKER_PASSWORD", DEFAULT_PASSWORD),
                        help="password every created account gets")
    parser.add_argument("--photo-dir", default="worker_photos", help="directory of face photos")
    parser.add_argument("--out", default="backend/tools/roster.json", help="roster JSON to write")
    parser.add_argument("--name-prefix", default="Load Test Worker", help="name prefix")
    parser.add_argument("--roster", default=None,
                        help="roster to read for --reactivate/--deactivate (default: --out)")
    parser.add_argument("--reactivate", action="store_true",
                        help="re-enable an existing roster's accounts; creates nothing")
    parser.add_argument("--deactivate", action="store_true",
                        help="turn an existing roster's accounts off again")
    parser.add_argument("--dry-run", action="store_true", help="log in and plan, create nothing")
    parser.add_argument("--run", action="store_true",
                        help="reactivate the roster, run the punch load test, then deactivate it")
    parser.add_argument("--users", type=int, default=None,
                        help="simulated workers for --run (default: the whole roster)")
    parser.add_argument("--gap", type=float, default=0.2,
                        help="seconds between consecutive workers' first punch (default 0.2)")
    parser.add_argument("--shift", type=float, default=20.0,
                        help="seconds each simulated worker stays on site (default 20)")
    parser.add_argument("--lead-in", type=float, default=8.0,
                        help="seconds allowed for the logins before the ramp starts (default 8)")
    parser.add_argument("--spawn-rate", type=float, default=100.0,
                        help="Locust -r for --run (default 100)")
    parser.add_argument("--run-time", default=None,
                        help="Locust -t for --run (default: computed from the ramp)")
    parser.add_argument("--site-fix", default=None,
                        help="lat,lon to punch from (default: the deployment's first site)")
    parser.add_argument("--csv-prefix", default=None,
                        help="where --run writes Locust's CSV (default: beside the roster)")
    parser.add_argument("--stress", action="store_true",
                        help="step the load up stage by stage until it breaks, then report")
    parser.add_argument("--stress-start", type=int, default=5,
                        help="workers in the first stress stage (default 5)")
    parser.add_argument("--stress-step", type=int, default=5,
                        help="workers added per stress stage (default 5)")
    parser.add_argument("--stress-max", type=int, default=None,
                        help="largest stress stage (default: the whole roster)")
    parser.add_argument("--stage-seconds", type=float, default=15.0,
                        help="seconds each worker stays on site in a stress stage (default 15)")
    parser.add_argument("--max-p95", type=float, default=3000.0,
                        help="stop the ladder when a stage's p95 exceeds this, in ms (default 3000)")
    parser.add_argument("--max-fail-ratio", type=float, default=0.05,
                        help="stop the ladder when this share of a stage's requests fails (default 0.05)")
    # -- Phase 3: cached/cold blend on a Poisson arrival process ----------------------
    parser.add_argument("--phase3", action="store_true",
                        help="blend cached-token punches with arrival-time logins on a Poisson "
                             "process, then judge the run against the Phase-3 thresholds")
    parser.add_argument("--fresh-login-ratio", type=float, default=0.2,
                        help="share of accounts that open the app cold and log in at their "
                             "arrival instant (default 0.2)")
    parser.add_argument("--poisson-rate", type=float, default=0.0,
                        help="arrivals per second for the whole roster; 0 derives it from "
                             "--arrival-window")
    parser.add_argument("--arrival-window", type=float, default=30.0,
                        help="seconds the roster should arrive over, when --poisson-rate is 0 "
                             "(default 30)")
    parser.add_argument("--poisson-seed", type=int, default=0,
                        help="seed for the arrival process, for a reproducible draw (default 0)")
    parser.add_argument("--max-auth-p95-ms", type=float, default=PHASE3_THRESHOLDS["auth_p95_ms"],
                        help="fail the run when the cold-login p95 exceeds this, in ms")
    parser.add_argument("--max-auth-p99-ms", type=float, default=PHASE3_THRESHOLDS["auth_p99_ms"],
                        help="fail the run when the cold-login p99 exceeds this, in ms")
    parser.add_argument("--phase3-punch-p95-ms", type=float, default=PHASE3_THRESHOLDS["punch_p95_ms"],
                        help="fail the run when the punch p95 exceeds this, in ms "
                             "(the same gate budget the Phase-2 ladder used)")
    parser.add_argument("--max-verify-503", type=int, default=PHASE3_THRESHOLDS["verify_503"],
                        help="fail the run when more than this many /attendance/verify answer 503")
    parser.add_argument("--phase3-max-fail-ratio", type=float, default=PHASE3_THRESHOLDS["fail_ratio"],
                        help="fail the run when this share of its requests fails (default 0.01)")
    args = parser.parse_args(argv)

    if args.reactivate and args.deactivate:
        print("--reactivate and --deactivate are opposites; pick one")
        return 1
    if args.run and (args.reactivate or args.deactivate):
        print("--run already does both; drop --reactivate/--deactivate")
        return 1
    if args.stress and (args.run or args.phase3 or args.reactivate or args.deactivate):
        print("--stress already does the whole cycle; drop --run/--phase3/--reactivate/--deactivate")
        return 1
    if args.phase3 and (args.run or args.reactivate or args.deactivate):
        print("--phase3 already does the whole cycle; drop --run/--reactivate/--deactivate")
        return 1

    if not args.host:
        print("no --host and no LOCUST_HOST set")
        return 1
    host = args.host.rstrip("/")

    admin_id = os.environ.get("LOCUST_ADMIN_ID", "5000")
    admin_login = os.environ.get("LOCUST_ADMIN_LOGIN", "admin@siteops.com")
    admin_password = os.environ.get("LOCUST_ADMIN_PASSWORD")
    if not admin_password:
        print("set LOCUST_ADMIN_PASSWORD (the head admin's password)")
        return 1

    session = requests.Session()
    token = login(session, host, admin_id, admin_login, admin_password)
    headers = {"Authorization": f"Bearer {token}"}

    # Turning an existing batch on or off, rather than creating a new one. Handled before the
    # photo check on purpose: these modes read a roster and never look at an image.
    if args.reactivate or args.deactivate:
        return set_active_batch(
            session, host, headers, Path(args.roster or args.out), active=args.reactivate
        )

    if args.run or args.stress or args.phase3:
        # The whole cycle in one command: put the accounts back, drive the test, take the access
        # away again. The order matters and so does the ``finally`` - the accounts exist only for
        # the test, so a test that crashes or is interrupted must not leave them usable.
        roster_path = Path(args.roster or args.out)
        roster = load_roster_json(roster_path)
        if not roster:
            print(f"{'--stress' if args.stress else '--run'} needs a roster; none at {roster_path}")
            return 1
        if set_active_batch(session, host, headers, roster_path, active=True, hint=False) != 0:
            print("the roster could not be reactivated; not starting the load test")
            return 1
        # Start clean. An account already on shift refuses its clock-in with a 400
        # "Already clocked in!", which is a business refusal and reads in the report as a
        # failure of the deployment - the least useful way to learn that a previous run from
        # this roster was interrupted.
        leftover = close_open_shifts(session, host, headers, {str(e["user_id"]) for e in roster})
        if leftover:
            print(f"  cleared {len(leftover)} shift(s) left open by an earlier run")
        outcome = 1
        try:
            if args.stress:
                outcome = 0 if run_stress(session, host, headers, args, roster_path, roster) == 0 else 2
            elif args.phase3:
                outcome = 0 if run_phase3(session, host, headers, args, roster_path, roster) == 0 else 2
            else:
                outcome = 2 if run_punch_test(
                    session, host, headers, args, roster_path, roster
                ) else 0
        finally:
            print("\nrestoring the deployment")
            set_active_batch(session, host, headers, roster_path, active=False)
        return outcome

    photos = photos_in(Path(args.photo_dir))
    if not photos:
        print(f"no photos in {args.photo_dir}: the accounts would have no face to clock in with")
        return 1

    print(f"provisioning against {host}")
    taken = used_ids(session, host, headers)
    targets = free_worker_ids(taken, args.count)
    print(f"  roster        : {len(taken)} accounts already in use")
    print(f"  free ids      : {targets[0]}..{targets[-1]} ({args.count} accounts)")
    print(f"  photos        : {len(photos)} file(s), cycled - each account is enrolled with, and "
          f"punches with, the same image")
    for index, user_id in enumerate(targets):
        print(f"    {user_id:>4} -> {photos[index % len(photos)].name}")

    if args.dry_run:
        print("\ndry run: nothing created")
        return 0

    roster = []
    failures = []
    for index, user_id in enumerate(targets):
        photo = photos[index % len(photos)]
        email = f"loadtest{user_id}@example.com"
        ok, message = create_account(
            session, host, headers,
            user_id=user_id,
            name=f"{args.name_prefix} {user_id}",
            password=args.password,
            email=email,
            photo=photo,
        )
        print(f"  {user_id:>4} {'ok' if ok else 'FAIL'}  {message}")
        if ok:
            roster.append({
                "user_id": str(user_id),
                "login": email,
                "password": args.password,
                "selfie": str(photo),
            })
        else:
            failures.append(user_id)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(roster, indent=2), encoding="utf-8")
    print(f"\nroster written : {out} ({len(roster)} accounts)")
    if failures:
        print(f"failed ids     : {failures}")
    print(f"created ids    : {[row['user_id'] for row in roster]}")
    print(
        "\nrun the punch test with:\n"
        f"  LOCUST_ONLY=punch LOCUST_ROSTER={out} \\\n"
        f"  LOCUST_SHIFT_SECONDS_MIN=15 LOCUST_SHIFT_SECONDS_MAX=60 \\\n"
        f"  locust -f backend/tools/locustfile.py --headless --host {host} "
        f"-u {len(roster)} -r 2 -t 10m --only-summary\n"
        "\nclean up afterwards with POST /api/v1/admin/users/delete for each id above."
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
