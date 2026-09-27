"""Locust load test for this app as deployed on Railway.

Written against the *actual* HTTP surface of ``backend/main.py`` and the routers it
mounts (``readiness``, ``reports``, ``notes``, ``quick_links``, ``registrations``,
``offline_sync``, ``enrollment``, ``developer``) - every path, method, field and query
parameter below was read out of the source, not assumed. Nothing here is a "standard"
endpoint this app does not have: there is no session cookie, no ``/api/login``, no
REST-style ``PUT``/``PATCH`` CRUD. Authentication is a bearer **JWT** obtained from
``POST /api/v1/auth/login``; every write is a ``POST``; the punch is a multipart upload.

--------------------------------------------------------------------------
How it is shaped
--------------------------------------------------------------------------
Three user classes, weighted the way real traffic is:

* ``PublicUser``  (weight 5) - the anonymous edge: ``/api/v1/status`` liveness,
  ``/api/v1/readiness``, public branding, and the static bundle. This is the traffic a
  load balancer, a health probe and every phone that has not signed in yet produce.
* ``WorkerUser``  (weight 6) - a signed-in worker's phone: poll stats, the site window
  where it is standing, history, notifications, timesheet. One heavy punch task.
* ``AdminUser``   (weight 3) - the console: the Live Ops board, logs, audit trail,
  the quarterly reports, the review queue.

Reads are heavily weighted, the two genuinely expensive paths - the face punch and the
CSV export - are low weight, and anything that *mutates production state* is off unless
you opt in (see "Safety" below).

--------------------------------------------------------------------------
Safety: this app writes to a real database
--------------------------------------------------------------------------
Run against Railway and you are hitting the company's production data. So the
mutating tasks are gated behind environment flags and are **off by default**:

    LOCUST_ALLOW_PUNCH=1        # /api/v1/attendance/verify - writes real attendance rows
    LOCUST_ALLOW_WRITES=1       # benign self-scoped writes (a note, a column preference)
    LOCUST_ALLOW_DESTRUCTIVE=1  # config/roster/review mutations - READ THE WARNING

The default profile is **read-only**: it is safe to point at production and still
exercises the CPU-bound report queries, the SQLite reads and the whole ASGI stack.
The punch additionally needs a real selfie that matches an enrolled account
(``LOCUST_SELFIE=/path/to/photo.jpg``) - without one the task is disabled, because a
fake JPEG only measures the refusal path.

The rate limiter is per client IP (``config.py``): login ``10/minute``, attendance
``15/minute``, readiness ``60/minute``, notes ``30/minute``, registration ``20/minute``.
A load test is *one* IP, so you will meet these. A 429 is the limiter doing its job, not
a defect; by default it is counted as a *success* so the failure ratio stays meaningful.
Either raise the limits on Railway for the duration of the test, or keep the user count
low enough that the limiter is the thing under test.

--------------------------------------------------------------------------
Running it
--------------------------------------------------------------------------
Web UI locally, pointed at Railway (interactive, 200 users, 20/s ramp):

    pip install locust
    export LOCUST_HOST="https://al-jehad-production.up.railway.app"
    export LOCUST_WORKER_ID=...      LOCUST_WORKER_LOGIN=...    LOCUST_WORKER_PASSWORD=...
    export LOCUST_ADMIN_ID=...       LOCUST_ADMIN_LOGIN=...     LOCUST_ADMIN_PASSWORD=...
    locust -f backend/tools/locustfile.py --host "$LOCUST_HOST" --web-host 127.0.0.1 -u 200 -r 20

Then open http://127.0.0.1:8089.

Headless, as an ephemeral one-shot Railway service (same repo, temporary service):

    locust -f backend/tools/locustfile.py --headless \
        --host "$LOCUST_HOST" -u 300 -r 30 -t 10m \
        --csv /tmp/locust --html /tmp/locust.html --only-summary

The headless form is what you run as a Railway "cron"/one-off job: give the service the
only environment variables it needs (the four account pairs) and let it exit after
``-t``. Its exit code and the ``--csv`` it writes are the artifact.

To reach only the safe public edge (no credentials at all):

    locust -f backend/tools/locustfile.py --headless -u 100 -r 10 -t 3m --tags public

--------------------------------------------------------------------------
20 workers actually clocking in and out
--------------------------------------------------------------------------
This is the ``PunchUser`` class, and it needs more than the read classes do:

* **One real account per worker.** ``active_sessions`` keeps one open shift per
  ``worker_id``, so 20 people checking in needs 20 enrolled accounts - a single shared
  account answers "Already clocked in!" to all but the first. Supply a roster:

      LOCUST_ROSTER=/path/roster.json
      [
        {"user_id": "42", "login": "worker42@example.com", "password": "...", "selfie": "photos/42.jpg"},
        {"user_id": "43", "login": "worker43@example.com", "password": "...", "selfie": "photos/43.jpg"}
      ]

  ``login`` is the email or phone the account signs in with; ``selfie`` is optional per
  account and falls back to ``LOCUST_SELFIE``.

* **A matching, enrolled face.** The punch answers 404 with no template and 422 on a
  mismatch; an unmatched photo measures the refusal path, not the punch path.

* **A fix inside a geofence.** ``LOCUST_SITE_FIX="lat,lon"``, or admin credentials so the
  script can read ``/admin/sites``. Outside every fence the punch is a 403 before the model
  runs.

* **The limiter raised.** ``/api/v1/attendance/verify`` is ``15/minute`` per IP by default
  (``ATTENDANCE_RATE_LIMIT``); 20 workers cycling in and out will meet it almost at once.
  Raise it on the Railway service for the run, or read the 429s as the finding.

Then, exactly 20 punching workers and nothing else:

    LOCUST_ONLY=punch LOCUST_ROSTER=roster.json LOCUST_SITE_FIX="30.05,31.23" \
    LOCUST_SHIFT_SECONDS_MIN=15 LOCUST_SHIFT_SECONDS_MAX=60 \
    locust -f backend/tools/locustfile.py --headless \
        --host https://al-jehad-production.up.railway.app -u 20 -r 2 -t 10m --only-summary

One warning that is not cosmetic: a punch whose face match lands in the *review* band
writes a ``pending_review`` row, and an unresolved review **blocks that worker's clock-out**
(``/attendance/verify`` refuses it by design). A load test with mismatched photos can wedge
accounts instead of measuring them.

--------------------------------------------------------------------------
What to watch in the Railway dashboard while this runs
--------------------------------------------------------------------------
Railway's own graph is thin, which is exactly why the load test has to be read
*beside* it rather than instead of it:

* **CPU throttle / vCPU ceiling.** Railway caps a service at its plan's share of a
  vCPU. The punch and the report queries are the only CPU-heavy paths here; when the
  service is pinned at the cap, p95 latency stops tracking load and starts tracking the
  quota. If ``/api/v1/status`` (trivial) stays fast while ``/reports`` and the punch
  degrade, you have hit the CPU ceiling, not a code fault.
* **Memory / OOM.** The ML stack plus SQLite plus the threadpool is the biggest
  consumer. A rising saw-tooth that never returns to baseline, or a container restart
  mid-test with no deploy, is the OOM killer. Watch RSS against the plan's memory limit;
  a sustained climb with an allocation-heavy endpoint (the CSV export streams through a
  threadpool, the punch spools an upload) is the early signal.
* **Restart count / cold start.** Railway restarts on OOM, on a healthcheck failure, or
  on a deploy. A restart mid-test shows up as a burst of connection errors and a fresh
  ``uptime_seconds`` at ``/api/v1/admin/readiness``. Correlate the timestamp with the
  graph.
* **TCP connections and the threadpool.** Each simulated Locust user opens keep-alive
  connections, and every request runs through anyio's bounded worker threadpool. When
  the pool is saturated, latency for SQLite-bound reads queues behind CPU-bound punches;
  that is a real capacity finding, and the fix is more replicas or a separate punch path,
  not a code change.
* **Egress / connection limits at the edge.** The Railway proxy will refuse new
  connections before the app sees them under a hard ramp. ``conn-error`` in the Locust
  failures panel (status ``0``) with the app itself healthy is an edge limit, not the
  container.
* **Redeploy during a test.** A Railway deploy replaces the container under the load;
  the run is no longer a measurement. Freeze deploys while testing.

The public ``/api/v1/status`` is your control: it should stay flat. If it moves, the
problem is below your code (proxy, platform, network).
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import date, timedelta

import gevent
from gevent.lock import BoundedSemaphore
from locust import HttpUser, between, events, tag, task
from locust.exception import StopUser

log = logging.getLogger("locustfile")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: The Railway domain. ``--host`` overrides it; the attribute on the base class is the
#: fallback so a bare ``locust -f locustfile.py`` still aims somewhere sensible.
DEFAULT_HOST = os.environ.get("LOCUST_HOST", "https://al-jehad-production.up.railway.app")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


#: A 429 is the per-IP limiter answering, and a load test is one IP. Counting it as a
#: failure would drown the real errors; set ``LOCUST_429_IS_SUCCESS=0`` to see them.
TREAT_429_AS_SUCCESS = _flag("LOCUST_429_IS_SUCCESS", True)

#: Mutating tasks - see the module docstring. All off by default.
ALLOW_PUNCH = _flag("LOCUST_ALLOW_PUNCH")
ALLOW_WRITES = _flag("LOCUST_ALLOW_WRITES")
ALLOW_DESTRUCTIVE = _flag("LOCUST_ALLOW_DESTRUCTIVE")

#: ``/api/v1/admin/readiness?deep=1`` runs the full self-test; it is a heavy probe, so
#: the shallow form is the default.
DEEP_READINESS = _flag("LOCUST_DEEP_READINESS")

#: A real selfie belonging to an enrolled account. Required for the punch task.
SELFIE_PATH = os.environ.get("LOCUST_SELFIE")

#: A roster of worker accounts for real clock-in/out traffic: a JSON file holding a list of
#: objects with ``user_id``, ``login``, ``password`` and an optional ``selfie`` (a path).
#: **One virtual worker per account** - ``active_sessions`` holds one open shift per
#: ``worker_id``, so N people checking in and out genuinely needs N enrolled accounts; a
#: single shared account would answer "Already clocked in!" to all but the first.
ROSTER_PATH = os.environ.get("LOCUST_ROSTER")

#: An in-fence ``"lat,lon"`` to punch from. Without admin credentials the script cannot
#: read ``/admin/sites`` to find one, and a fix outside every geofence is refused with 403.
SITE_FIX = os.environ.get("LOCUST_SITE_FIX")

#: ``itertools``-style selection of which classes may spawn: ``LOCUST_ONLY=punch`` zeroes
#: every other class's weight so ``-u 20`` spawns exactly 20 punching workers.
ONLY = {part.strip().lower() for part in os.environ.get("LOCUST_ONLY", "").split(",") if part.strip()}

#: How long a simulated worker "stays on site" between an in and an out, in seconds. A real
#: shift is hours; the point of the load test is the punch endpoints, so the cycle is
#: compressed. Short enough to see many cycles, long enough not to hammer the limiter.
SHIFT_SECONDS = (
    float(os.environ.get("LOCUST_SHIFT_SECONDS_MIN", "15")),
    float(os.environ.get("LOCUST_SHIFT_SECONDS_MAX", "60")),
)

#: Seconds between consecutive workers' **first** punch - the "20 people through the gate"
#: shape. Each worker sleeps ``ordinal * LOCUST_PUNCH_GAP`` before its clock-in, so the
#: check-ins arrive in a ramp; with a fixed ``LOCUST_SHIFT_SECONDS_*`` the clock-outs inherit
#: the same spacing. ``0`` (the default) means no stagger: every worker punches at once.
PUNCH_GAP = float(os.environ.get("LOCUST_PUNCH_GAP", "0") or 0)

#: Each worker does exactly one shift (in, then out) and stops, so a run is one clean pass and
#: the counts mean what they say. On by default in stagger mode (a ramp is one pass by
#: definition); ``LOCUST_ONE_SHIFT=1`` turns it on for a simultaneous burst too, which is what a
#: capacity stage wants - every worker punches once, all at the same instant.
ONE_SHIFT_EACH = PUNCH_GAP > 0 or _flag("LOCUST_ONE_SHIFT")

#: Account credentials. ``*_TOKEN`` wins over the login pair when both are present, so a
#: test can run entirely off tokens minted out of band (no bcrypt, no login rate limit).
WORKER_ID = os.environ.get("LOCUST_WORKER_ID")
WORKER_LOGIN = os.environ.get("LOCUST_WORKER_LOGIN")
WORKER_PASSWORD = os.environ.get("LOCUST_WORKER_PASSWORD")
WORKER_TOKEN_ENV = os.environ.get("LOCUST_WORKER_TOKEN")

ADMIN_ID = os.environ.get("LOCUST_ADMIN_ID")
ADMIN_LOGIN = os.environ.get("LOCUST_ADMIN_LOGIN")
ADMIN_PASSWORD = os.environ.get("LOCUST_ADMIN_PASSWORD")
ADMIN_TOKEN_ENV = os.environ.get("LOCUST_ADMIN_TOKEN")

LOGIN_PATH = "/api/v1/auth/login"

# --------------------------------------------------------------------------- #
# Phase 3: the arrival process and the cached-session blend
# --------------------------------------------------------------------------- #
# The ladders measured an *orchestrated* pipeline: logins finished in the lead-in before any
# punch began, and the punches were spread by a metronome (``ordinal * LOCUST_PUNCH_GAP``).
# Two things that a real shift start does were therefore never exercised - authentication
# competing with the face engine for the one core, and arrivals clustering into sub-second
# bursts. Phase 3 adds both, and leaves the v1/v2 behaviour as the default so no existing
# command changes meaning.

#: The share of accounts that open the app with **no cached token**, so their login (a real
#: bcrypt verification, ~0.2 s of CPU) happens at their arrival instant - on the same core the
#: face engine uses. ``0`` (default) mints every token up front in the lead-in, as before.
FRESH_LOGIN_RATIO = min(
    1.0, max(0.0, float(os.environ.get("LOCUST_FRESH_LOGIN_RATIO", "0") or 0))
)

#: ``LOCUST_ARRIVAL=poisson`` replaces the metronome with a Poisson process of
#: ``LOCUST_POISSON_RATE`` arrivals per second: the k-th worker's arrival is the k-th point of
#: the process, so the punches cluster the way real arrivals do instead of marching evenly.
ARRIVAL_MODE = (os.environ.get("LOCUST_ARRIVAL") or "ramp").strip().lower()
POISSON_RATE = float(os.environ.get("LOCUST_POISSON_RATE", "0") or 0)
POISSON = ARRIVAL_MODE == "poisson" and POISSON_RATE > 0
#: Fixed by default so a run is reproducible; re-roll it (or re-run with another seed) to see
#: a different draw of the same process rather than a different process.
POISSON_SEED = int(os.environ.get("LOCUST_POISSON_SEED", "0") or 0)

#: The request name the *cold* logins are recorded under, so the report and the thresholds can
#: tell an arrival login apart from the one-time lead-in mint.
FRESH_LOGIN_NAME = f"{LOGIN_PATH} [phase3 fresh]"

#: Plausible fixes used for the worker's "where am I standing" lookup. The endpoint
#: validates that a coordinate is a *real* fix (finite, in range, not ``(0, 0)``) and
#: then answers whether a geofence matches - it does not require a match - so a spread of
#: real cities is honest traffic. Centred on the harness's own region.
FALLBACK_FIXES: tuple[tuple[float, float], ...] = (
    (30.05, 31.23),
    (30.06, 31.24),
    (30.04, 31.22),
    (30.07, 31.20),
    (30.03, 31.26),
)

#: Report windows are capped at three years by ``reports._range_bounds``; these stay well
#: inside it and are dates the console's own picker would produce.
REPORT_WINDOWS: tuple[int, ...] = (7, 30, 45, 90, 180, 365)

#: The column vocabulary the timesheet files accept (``reports.REPORT_COLUMNS``).
REPORT_COLUMNS = ("date", "site", "arrival", "break", "hours", "recorded", "approved", "status")

#: The note vocabulary (``notes.CATEGORIES`` / ``notes.PRIORITIES``).
NOTE_CATEGORIES = ("password_reset", "missing_item", "shift_hours", "enrollment", "working_conditions", "other")
NOTE_PRIORITIES = ("low", "normal", "high")

#: The three actions ``/attendance/verify`` accepts.
PUNCH_ACTIONS = ("Clock In", "Clock Out")


def _cb() -> str:
    """A per-request cache buster.

    FastAPI ignores query parameters a route did not declare, so this is safe on every
    GET here, and it keeps a Railway edge cache, a corporate proxy or an intermediate
    CDN from answering a "dynamic" request out of a cached body.
    """
    return f"{random.getrandbits(48):012x}"


def _random_fix() -> str:
    """A ``"lat,lon"`` string - the exact format ``parse_location_input`` accepts."""
    if SITES:
        site = random.choice(SITES)
        # Jitter inside the geofence so consecutive fixes are not byte-identical.
        lat = float(site[0]) + random.uniform(-0.0005, 0.0005)
        lon = float(site[1]) + random.uniform(-0.0005, 0.0005)
        return f"{lat:.6f},{lon:.6f}"
    lat, lon = random.choice(FALLBACK_FIXES)
    return f"{lat + random.uniform(-0.01, 0.01):.6f},{lon + random.uniform(-0.01, 0.01):.6f}"


def _random_window() -> tuple[str, str]:
    """``(start, end)`` ``YYYY-MM-DD`` strings, ``end`` today and inside the 3-year cap."""
    end = date.today()
    start = end - timedelta(days=random.choice(REPORT_WINDOWS))
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------- #
# Token handling
# --------------------------------------------------------------------------- #
# Locust runs thousands of greenlets inside a *handful* of OS processes, and the login
# endpoint is limited to 10/minute per IP. Minting a token per simulated user would both
# spend the whole budget on logins and be unauthentic (a phone signs in once and keeps the
# token for hours). So the token is cached per process per account and shared by every
# greenlet that needs it; a lock serialises the one login that populates it.
_TOKENS: dict[str, str] = {}

#: One gate **per account**, not one for the whole process.
#:
#: A single global lock looked tidy and was wrong: it serialised every account's login, and a
#: bcrypt login costs about a second, so a 20-account roster spent ~20 s logging in one at a
#: time. That is invisible in a loop test and fatal to a *staggered* one - the ramp measured
#: ~1 s between punches instead of the 0.2 s asked for, because each worker's login (not its
#: gap) decided when it punched. Per-account gates still deduplicate the one login that matters
#: (many greenlets, one account) while letting different accounts sign in concurrently.
#:
#: ``BoundedSemaphore`` from gevent rather than ``threading.Lock``: this is held across a
#: network call, and a cooperative primitive yields to the other greenlets for the duration
#: instead of stalling the whole process if threading is not monkey-patched.
_LOGIN_GATES: dict[str, BoundedSemaphore] = {}
_LOGIN_GATES_GUARD = BoundedSemaphore(1)


def _login_gate(label: str) -> BoundedSemaphore:
    with _LOGIN_GATES_GUARD:
        return _LOGIN_GATES.setdefault(label, BoundedSemaphore(1))


def _fresh_stride() -> int:
    """Every ``stride``-th account opens the app cold, so the cohort is exactly the ratio.

    A stride rather than a per-account coin flip: the share of fresh logins a run reports has
    to be the share it was configured for, or the blend becomes a random variable and the
    thresholds read it as one. ``0`` means the blend is off.
    """
    if FRESH_LOGIN_RATIO <= 0:
        return 0
    return max(1, round(1.0 / FRESH_LOGIN_RATIO))


def _poisson_offsets(count: int) -> list[float]:
    """Arrival offsets (seconds from the ramp zero) for ``count`` workers, Poisson-distributed.

    The **cumulative sum** of exponential inter-arrival times *is* a Poisson process, so the
    k-th worker arrives at the k-th point of one and the set as a whole clusters the way real
    arrivals do. Drawing each worker's wait independently instead would be a set of unrelated
    waits - smooth on average, and therefore hiding exactly the micro-stampedes a shift start
    is made of.
    """
    rng = random.Random(POISSON_SEED)
    offsets: list[float] = []
    clock = 0.0
    for _ in range(count):
        clock += rng.expovariate(POISSON_RATE)
        offsets.append(clock)
    return offsets

#: The staggered ramp counts from a fixed instant rather than from each worker's own start, so
#: a slow login shifts nobody's slot. ``_RAMP_ZERO`` is set at ``test_start`` and the lead-in is
#: the window every login has to finish in before the first punch is due.
RAMP_LEAD_IN = float(os.environ.get("LOCUST_RAMP_LEAD_IN", "8"))
_RAMP_ZERO = 0.0

#: The arrival instants for this run, computed once at ``test_start`` from ``POISSON_RATE``.
_ARRIVAL_OFFSETS: list[float] = []


def _arrival_offset(ordinal: int) -> float:
    """This worker's seconds-from-zero arrival, for either process.

    In Poisson mode it is the roster's ``ordinal``-th process point; a worker beyond the drawn
    set (a reloaded roster) falls back to the last one rather than to zero, so it does not
    stampede the front of the run.
    """
    if POISSON and _ARRIVAL_OFFSETS:
        return _ARRIVAL_OFFSETS[min(ordinal, len(_ARRIVAL_OFFSETS) - 1)]
    return ordinal * PUNCH_GAP


def _login(session, *, user_id, login_id, password, label, host: str, name: str | None = None) -> str | None:
    """``POST /api/v1/auth/login`` once, with backoff while the limiter answers 429.

    ``name`` overrides the recorded request name so a Phase-3 arrival login is reported
    separately from the lead-in mints.
    """
    # A roster of accounts each log in once, and the login limiter is 10/minute per IP: at
    # 20 accounts the second half waits for the window to roll, so the retry has to outlast
    # a one-minute limiter, not a momentary burst.
    for attempt in range(6):
        with session.post(
            LOGIN_PATH,
            json={"user_id": user_id, "email_or_phone": login_id, "password": password},
            name=name or f"{LOGIN_PATH} [mint {label}]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                payload = response.json()
                token = payload.get("access_token") or payload.get("token")
                if token:
                    response.success()
                    log.info("minted a %s token against %s", label, host)
                    return token
                response.failure("login answered 200 with no token")
                return None
            if response.status_code == 429:
                response.success()  # the limiter, not a defect
                time.sleep(2.0 * (attempt + 1))
                continue
            response.failure(f"login {response.status_code}: {response.text[:160]}")
            return None
    log.warning("%s login kept meeting the rate limiter; giving up", label)
    return None


def _cached_token(self, label: str, user_id: str | None, login_id: str | None, password: str | None) -> str | None:
    """A token for *this* account, minted at most once per process and shared by every
    greenlet that claims the same label. The lock is held across the one login, so 200
    greenlets starting at once cost one login, not 200."""
    if label in _TOKENS:
        return _TOKENS[label]
    if not (user_id and login_id and password):
        return None
    with _login_gate(label):
        if label in _TOKENS:  # another greenlet won the race
            return _TOKENS[label]
        token = _login(
            self.client,
            user_id=user_id,
            login_id=login_id,
            password=password,
            label=label,
            host=self.host or DEFAULT_HOST,
        )
        if token:
            _TOKENS[label] = token
        return token


def _token(self, label: str) -> str | None:
    """The configured worker/admin token, from the environment or a login."""
    if label in _TOKENS:
        return _TOKENS[label]
    env_token = WORKER_TOKEN_ENV if label == "worker" else ADMIN_TOKEN_ENV
    if env_token:
        _TOKENS[label] = env_token
        return env_token
    if label == "worker":
        return _cached_token(self, "worker", WORKER_ID, WORKER_LOGIN, WORKER_PASSWORD)
    return _cached_token(self, "admin", ADMIN_ID, ADMIN_LOGIN, ADMIN_PASSWORD)


def _forget(label: str) -> None:
    """Drop a cached token after a 401, so the next request re-mints instead of looping."""
    _TOKENS.pop(label, None)


# --------------------------------------------------------------------------- #
# Discovery: real site names and account ids, read from the app itself
# --------------------------------------------------------------------------- #
# ``/admin/workers_live/{site_name}`` and ``/admin/users/{user_id}`` take a value that
# only exists in the deployment's database. Rather than invent one (which would only ever
# produce 404s and a useless graph), the first admin to start reads the roster and the
# site list once and publishes them here.
SITES: list[tuple[float, float, str]] = []   # (lat, lon, site_name)
USER_IDS: list[str] = []
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERED = False


def _discover(admin_token: str | None, session) -> None:
    global _DISCOVERED
    if _DISCOVERED or not admin_token:
        return
    with _DISCOVERY_LOCK:
        if _DISCOVERED:
            return
        headers = {"Authorization": f"Bearer {admin_token}", "Accept": "application/json"}
        try:
            with session.get("/api/v1/admin/sites", headers=headers, name="/api/v1/admin/sites [discover]",
                             catch_response=True) as response:
                if response.status_code == 200:
                    for site in response.json() or []:
                        if site.get("lat") is not None and site.get("lon") is not None:
                            SITES.append((site["lat"], site["lon"], str(site.get("site_name") or "")))
                    response.success()
                else:
                    response.failure(f"site discovery {response.status_code}")
            with session.get("/api/v1/admin/users", headers=headers, name="/api/v1/admin/users [discover]",
                             catch_response=True) as response:
                if response.status_code == 200:
                    USER_IDS.extend(str(row["id"]) for row in (response.json() or []))
                    response.success()
                else:
                    response.failure(f"roster discovery {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            log.warning("discovery failed: %s: %s", type(exc).__name__, exc)
        _DISCOVERED = True
        log.info("discovered %d sites and %d accounts", len(SITES), len(USER_IDS))


# --------------------------------------------------------------------------- #
# Selfies and the punch roster (loaded before the classes, which read ROSTER)
# --------------------------------------------------------------------------- #
def _load_selfie() -> bytes | None:
    if not SELFIE_PATH:
        return None
    try:
        with open(SELFIE_PATH, "rb") as handle:
            return handle.read()
    except OSError as exc:
        log.warning("cannot read LOCUST_SELFIE=%s: %s", SELFIE_PATH, exc)
        return None


def _read_selfie(path: str | None) -> bytes | None:
    """The bytes of one account's selfie, or ``None`` when there is nothing readable."""
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        log.warning("cannot read selfie %s: %s", path, exc)
        return None


def _load_roster() -> list[dict]:
    """The punch roster, read from ``LOCUST_ROSTER`` (JSON). Bad entries are skipped, not
    fatal: a typo in one account must not take the whole run down."""
    if not ROSTER_PATH:
        return []
    import json

    try:
        with open(ROSTER_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("cannot read LOCUST_ROSTER=%s: %s", ROSTER_PATH, exc)
        return []
    accounts = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not entry.get("user_id") or not entry.get("password"):
            continue
        accounts.append(
            {
                "user_id": str(entry["user_id"]),
                "login": str(entry.get("login") or entry.get("email_or_phone") or ""),
                "password": str(entry["password"]),
                "selfie": entry.get("selfie"),
                # A pre-minted bearer token (see ``tools/mint_tokens.py``). When present the
                # account never calls ``/auth/login`` at all: the phone it models is already
                # signed in, which is the steady state a 30-day session produces.
                "token": entry.get("token"),
            }
        )
    return accounts


PUNCH_BYTES = _load_selfie()
ROSTER = _load_roster()

#: Hand each simulated worker a *different* account, round-robin, so two greenlets never
#: punch as the same person (which the open-session rule would refuse).
_ACCOUNT_LOCK = threading.Lock()
_ACCOUNT_NEXT = 0


def _claim_account() -> tuple[int, dict] | None:
    """The next roster account plus its ordinal, which is what orders the staggered punch."""
    global _ACCOUNT_NEXT
    if not ROSTER:
        return None
    with _ACCOUNT_LOCK:
        ordinal = _ACCOUNT_NEXT
        account = ROSTER[ordinal % len(ROSTER)]
        _ACCOUNT_NEXT += 1
        return ordinal, account


def _punch_fix() -> str:
    """A fix to punch from: the explicit override, then a discovered site, then the
    fallbacks - which are *not* inside any geofence and would be refused with 403."""
    if SITE_FIX:
        return SITE_FIX
    return _random_fix()


# --------------------------------------------------------------------------- #
# Base user
# --------------------------------------------------------------------------- #
class ApiUser(HttpUser):
    """Shared request plumbing: bearer auth, cache busting, honest 429 handling."""

    abstract = True
    #: A human reads the screen, switches apps and comes back. 1.5-4 s is the band a
    #: worker on a phone and an operator on the console actually produce.
    wait_time = between(1.5, 4.0)
    host = DEFAULT_HOST

    token: str | None = None
    token_label = ""

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _judge(self, response) -> None:
        if response.status_code < 400:
            response.success()
        elif response.status_code == 429 and TREAT_429_AS_SUCCESS:
            response.success()  # the per-IP limiter, not a failure
        elif response.status_code == 401 and self.token_label:
            _forget(self.token_label)
            response.failure(f"401 (token dropped): {response.text[:120]}")
        else:
            response.failure(f"{response.status_code}: {response.text[:160]}")

    def get_json(self, path: str, *, name: str, params: dict | None = None, headers: dict | None = None) -> None:
        merged = dict(params or {})
        merged["_cb"] = _cb()
        with self.client.get(
            path, params=merged, headers=headers or self.auth_headers(), name=name, catch_response=True
        ) as response:
            self._judge(response)

    def post_json(self, path: str, *, name: str, payload: dict | None = None) -> None:
        with self.client.post(
            path, json=payload or {}, headers=self.auth_headers(), name=name, catch_response=True
        ) as response:
            self._judge(response)

    def maybe_punch(self) -> None:
        """The heavy path: multipart selfie through geofence + liveness + face match.

        Only runs when ``LOCUST_ALLOW_PUNCH=1``, a selfie is configured and the account
        holds a token. The action alternates and the fix is randomised so a committed
        clock-in does not make the next punch a pure "Already clocked in!" refusal, and
        so the model really runs rather than short-circuiting on a cached frame.
        """
        if not (ALLOW_PUNCH and PUNCH_BYTES and self.token and WORKER_ID):
            return
        action = random.choice(PUNCH_ACTIONS)
        files = {"selfie": ("selfie.jpg", PUNCH_BYTES, "image/jpeg")}
        data = {
            "worker_id": WORKER_ID,
            "action": action,
            "location_input": _random_fix(),
        }
        with self.client.post(
            "/api/v1/attendance/verify",
            data=data,
            files=files,
            headers={"Authorization": f"Bearer {self.token}"},
            name="/api/v1/attendance/verify [multipart]",
            catch_response=True,
        ) as response:
            self._judge(response)


# --------------------------------------------------------------------------- #
# Public edge
# --------------------------------------------------------------------------- #
class PublicUser(ApiUser):
    """Anonymous traffic: health probes, the login screen, the static bundle."""

    weight = 5

    def on_start(self) -> None:
        self.token_label = ""

    @tag("public")
    @task(6)
    def status(self) -> None:
        """The cheapest possible liveness ping - the flat control line."""
        with self.client.get("/api/v1/status", name="/api/v1/status", catch_response=True) as response:
            self._judge(response)

    @tag("public")
    @task(3)
    def readiness(self) -> None:
        """The public readiness gate (``60/minute`` per IP)."""
        with self.client.get("/api/v1/readiness", name="/api/v1/readiness", catch_response=True) as response:
            self._judge(response)

    @tag("public")
    @task(2)
    def branding(self) -> None:
        """The login screen's own read - public by design."""
        with self.client.get("/api/v1/branding", name="/api/v1/branding", catch_response=True) as response:
            self._judge(response)

    @tag("public", "static")
    @task(1)
    def index(self) -> None:
        with self.client.get("/", name="/ [index]", catch_response=True) as response:
            self._judge(response)

    @tag("public", "static")
    @task(1)
    def bundle(self) -> None:
        """One of the real assets the SPA loads (served from the ``/`` mount)."""
        asset = random.choice(("/frontendjavascript.js", "/style.css", "/i18n.js", "/logo-mark.svg"))
        masked = asset.lstrip("/").rsplit(".", 1)[0]
        with self.client.get(asset, name=f"/{masked}[ext]", catch_response=True) as response:
            self._judge(response)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class WorkerUser(ApiUser):
    """A signed-in worker's phone."""

    token_label = "worker"
    #: 0 when no worker credentials or token are configured, so the class contributes no
    #: traffic rather than a river of 401s.
    weight = 6 if (WORKER_TOKEN_ENV or (WORKER_ID and WORKER_LOGIN and WORKER_PASSWORD)) else 0

    def on_start(self) -> None:
        self.token = _token(self, "worker")
        if not self.token:
            log.warning("no worker token; WorkerUser will idle (set LOCUST_WORKER_* )")

    def _needs_token(self) -> bool:
        return bool(self.token)

    @task(6)
    def my_stats(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/worker/me/stats", name="/api/v1/worker/me/stats")

    @task(4)
    def site_window(self) -> None:
        """Where this phone is standing, and whether the window is open there."""
        if self._needs_token():
            self.get_json(
                "/api/v1/worker/me/site-window",
                name="/api/v1/worker/me/site-window?location_input=[fix]",
                params={"location_input": _random_fix()},
            )

    @task(5)
    def my_logs(self) -> None:
        if self._needs_token():
            limit = random.choice((20, 50, 100, 200))
            self.get_json(
                "/api/v1/worker/me/logs",
                name="/api/v1/worker/me/logs?limit=[n]",
                params={"limit": limit},
            )

    @task(3)
    def my_report(self) -> None:
        if self._needs_token():
            start, end = _random_window()
            self.get_json(
                "/api/v1/worker/me/report",
                name="/api/v1/worker/me/report?start=[d]&end=[d]",
                params={"start": start, "end": end},
            )

    @task(3)
    def my_notifications(self) -> None:
        if self._needs_token():
            self.get_json(
                "/api/v1/worker/me/notifications",
                name="/api/v1/worker/me/notifications?limit=50",
                params={"limit": 50},
            )

    @task(3)
    def my_notes(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/worker/notes", name="/api/v1/worker/notes")

    @task(2)
    def whoami(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/auth/me", name="/api/v1/auth/me")

    @task(1)
    def push_config(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/worker/me/push", name="/api/v1/worker/me/push")

    @task(1)
    def corpus_consent(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/worker/me/corpus/consent", name="/api/v1/worker/me/corpus/consent")

    @task(1)
    @tag("auth")
    def login_again(self) -> None:
        """A fresh ``POST /api/v1/auth/login`` - the bcrypt + limiter path, measured.

        Low weight on purpose: a real phone signs in once. This exists so the cost of a
        sign-in wave is visible, not so it is simulated continuously.
        """
        if not (WORKER_ID and WORKER_LOGIN and WORKER_PASSWORD):
            return
        with self.client.post(
            LOGIN_PATH,
            json={"user_id": WORKER_ID, "email_or_phone": WORKER_LOGIN, "password": WORKER_PASSWORD},
            name=f"{LOGIN_PATH} [probe]",
            catch_response=True,
        ) as response:
            self._judge(response)

    @task(2)
    @tag("punch")
    def punch(self) -> None:
        self.maybe_punch()

    @task(1)
    @tag("write")
    def set_report_columns(self) -> None:
        """A self-scoped preference write; benign but still a write."""
        if not (ALLOW_WRITES and self._needs_token()):
            return
        columns = random.sample(REPORT_COLUMNS, random.randint(4, len(REPORT_COLUMNS)))
        self.post_json("/api/v1/worker/me/report/columns", name="/api/v1/worker/me/report/columns",
                       payload={"columns": columns})

    @task(1)
    @tag("write")
    def mark_notifications_read(self) -> None:
        """Body-less POST, as the client sends it. Marks the whole inbox read."""
        if not (ALLOW_WRITES and self._needs_token()):
            return
        self.post_json("/api/v1/worker/me/notifications/read",
                       name="/api/v1/worker/me/notifications/read")

    @task(1)
    @tag("write")
    def open_note(self) -> None:
        """Open a worker note. Capped per worker by ``notes_max_open_per_worker`` - expecting
        a 429 here is the cap working, exactly like the punch's business refusals."""
        if not (ALLOW_WRITES and self._needs_token()):
            return
        self.post_json(
            "/api/v1/worker/notes",
            name="/api/v1/worker/notes [open]",
            payload={
                "category": random.choice(NOTE_CATEGORIES),
                "subject": f"Load test note {_cb()[:8]}",
                "body": "Opened by the load test to exercise the write path.",
                "priority": random.choice(NOTE_PRIORITIES),
            },
        )


# --------------------------------------------------------------------------- #
# A worker at the gate: real clock-in / clock-out
# --------------------------------------------------------------------------- #
def _ramp_wait(instance) -> float:
    """No think time until the worker has taken its first punch, then normal.

    ``wait_time`` is not only the pause *between* tasks - Locust also waits once after
    ``on_start`` and before the first task. Left at ``between(1.5, 4.0)`` that added an
    independent random 1.5-4 s to every worker's ramp slot, which is why a run configured for
    0.2 s spacing arrived ~0.65 s apart with a 13 s spread. Returning zero until the first
    punch makes the slot the worker actually waited for the thing that decides when it fires.
    """
    if not getattr(instance, "punched", False):
        return 0.0
    return random.uniform(1.5, 4.0)


class PunchUser(ApiUser):
    """One virtual worker, one real account, a real face, a real clock-in/out cycle.

    Where ``WorkerUser`` is a signed-in phone polling its own data, this class drives the
    **heavy** path: a multipart selfie through the geofence, the liveness model and the face
    match, then an open shift and a clock-out. It needs three things the others do not, and
    it idles without them:

    * ``LOCUST_ROSTER`` - one account per simulated worker. ``active_sessions`` is keyed by
      ``worker_id``, so two greenlets on one account means the second is refused
      "Already clocked in!".
    * an **enrolled** face and a matching selfie per account. The endpoint answers 404 with
      no template and 422 on a mismatch; neither measures the path you care about.
    * a fix inside a site geofence (``LOCUST_SITE_FIX``, or discovered from ``/admin/sites``)
      - anywhere else is a 403 *before* the model runs.
    """

    token_label = "punch"
    weight = 10 if ROSTER else 0
    #: In ramp or Poisson mode the think time is suspended until the first punch lands - see
    #: ``_ramp_wait``: either process says *when* the worker arrives, and a random 1.5-4 s wait
    #: after ``on_start`` would smear that instant.
    wait_time = _ramp_wait if (PUNCH_GAP > 0 or POISSON) else between(1.5, 4.0)

    punched = False
    account: dict | None = None
    selfie: bytes | None = None
    clocked_in = False
    #: Phase 3: this account arrived cold and must log in at its arrival instant instead of
    #: carrying a lead-in token.
    fresh = False
    #: This worker's position in the roster, which is what its staggered punch is timed from.
    ordinal = 0
    #: Wall-clock time the current "shift" ends and the clock-out becomes due. A plain
    #: attribute rather than a per-instance ``wait_time``: Locust normalises ``wait_time``
    #: at class level and a per-instance reassignment is called without the user argument,
    #: which silently killed this greenlet's task loop (see the note in ``punch_cycle``).
    shift_until = 0.0

    def on_start(self) -> None:
        if not ROSTER:
            return
        claimed = _claim_account()
        if not claimed:
            return
        self.ordinal, self.account = claimed
        self.token_label = f"punch:{self.account['user_id']}"
        # Phase 3 cohort. A *fresh* account deliberately starts with no token: its login then
        # happens inside ``punch_cycle`` at its arrival instant, competing with the face engine
        # for the core. Every other account mints once here, in the lead-in - the cached case a
        # worker's phone actually is in, because it holds a token from earlier in the day.
        stride = _fresh_stride()
        self.fresh = bool(stride) and self.ordinal % stride == 0
        if self.account.get("token"):
            # Pre-authenticated: seed the shared token cache and never log in. This is the
            # mode that proves the punch path carries zero bcrypt - if the login wall were
            # the bottleneck, a run with no logins at all would show it immediately.
            _TOKENS[self.token_label] = str(self.account["token"])
            self.fresh = False
            self.token = self.account["token"]
        else:
            self.token = None if self.fresh else _cached_token(
                self,
                self.token_label,
                self.account["user_id"],
                self.account["login"],
                self.account["password"],
            )
        self.selfie = _read_selfie(self.account["selfie"]) or PUNCH_BYTES
        self.clocked_in = False
        self.shift_until = 0.0
        # Learn the real fences so the punches land inside one (no-op after the first user).
        _discover(ADMIN_TOKEN_ENV or _token(self, "admin"), self.client)
        if not (self.selfie and (self.token or self.fresh)):
            log.warning(
                "punch account %s is unusable (token=%s fresh=%s selfie=%s)",
                self.account["user_id"],
                bool(self.token),
                self.fresh,
                bool(self.selfie),
            )
        # Wait for this worker's arrival instant. The deadline is absolute
        # (``_RAMP_ZERO + offset``), so a login or a slow start does not push the punch later and
        # stretch the process - it just leaves less slack. ``gevent.sleep`` because this is one
        # greenlet among many: a blocking sleep would hold them all.
        if POISSON or PUNCH_GAP > 0:
            due = _RAMP_ZERO + _arrival_offset(self.ordinal)
            delay = due - time.time()
            if delay > 0:
                gevent.sleep(delay)
            elif self.ordinal:
                log.warning("worker %s missed its %s arrival by %.2fs (login too slow?)\n"
                            "  raise LOCUST_RAMP_LEAD_IN above %s",
                            self.account["user_id"], "Poisson" if POISSON else "ramp",
                            -delay, RAMP_LEAD_IN)

    def _fresh_login(self) -> None:
        """The cold app open, at the arrival instant: no token, so a real login happens now.

        Recorded under ``FRESH_LOGIN_NAME`` so the report separates it from the lead-in mints and
        the auth threshold measures arrivals rather than warm-up. The per-account login gate still
        applies, so greenlets sharing one account pay for one login between them.
        """
        if self.token:
            return
        self.token = _login(
            self.client,
            user_id=self.account["user_id"],
            login_id=self.account["login"],
            password=self.account["password"],
            label=self.token_label,
            host=self.host or DEFAULT_HOST,
            name=FRESH_LOGIN_NAME,
        )

    def _punch(self, action: str, *, confirmed_early: bool = False) -> bool:
        data = {
            "worker_id": self.account["user_id"],
            "action": action,
            "location_input": _punch_fix(),
        }
        if confirmed_early:
            # A compressed shift is short by definition; ``confirm_early_checkout=1`` is the
            # phone answering the server's own early-out question, not a way round it.
            data["confirm_early_checkout"] = "1"
        if PUNCH_GAP > 0:
            # Client-side proof of the spacing: the offset from the ramp's zero instant, which
            # is the thing the schedule controls. The timestamp the server stores is when the
            # row was written (after the model ran), so it is a poorer witness than this.
            log.info("ramp: worker %s %s at +%.3fs", self.account["user_id"], action,
                     time.time() - _RAMP_ZERO)
        with self.client.post(
            "/api/v1/attendance/verify",
            data=data,
            files={"selfie": ("selfie.jpg", self.selfie, "image/jpeg")},
            headers={"Authorization": f"Bearer {self.token}"},
            name=f"/api/v1/attendance/verify [{action}]",
            catch_response=True,
        ) as response:
            ok = response.status_code == 200
            self._judge(response)
            return ok

    @tag("punch")
    @task(3)
    def punch_cycle(self) -> None:
        """Clock in, stay on site, clock out - one task that does whichever is due.

        The two steps are guarded by ``clocked_in`` and ``shift_until`` rather than being two
        tasks, because only one of them is legal at any moment and the open-session rule makes
        the wrong one a refusal. While the shift is running this task simply returns, and
        ``panel_poll`` (the phone reading its own stats) fills the loop - which is what a
        worker standing on site is actually doing with the app between the two punches.
        """
        # A cold account logs in first - that is the whole point of the Phase-3 blend - so the
        # login (and its bcrypt cost) lands on this worker's arrival instant, alongside every
        # other worker still punching.
        if self.fresh and not self.token:
            self._fresh_login()
        if not (self.token and self.selfie):
            return
        # Not in an arrival-timed mode: the look-around costs two round trips, and the point of a
        # ramp or a Poisson process is that the first punch lands on its arrival instant.
        if (PUNCH_GAP == 0 and not POISSON) and not self.clocked_in \
                and getattr(self, "arrived", False) is False:
            # A worker does not punch the instant they open the app. The clock panel is the
            # first thing they look at: where am I, does this site have a window open, is my
            # last shift settled. Only then do they take the selfie.
            self.arrived = True
            self.arrive()
            return
        if self.clocked_in:
            if time.time() < self.shift_until:
                if PUNCH_GAP > 0:
                    # Park on the due instant rather than checking again on the next loop. The
                    # clock-out is due at an absolute time (its slot plus the shift), and the
                    # post-punch think time is a random 1.5-4 s - so "check when the next task
                    # runs" scattered the clock-outs over ~13 s instead of the 0.2 s asked for.
                    gevent.sleep(self.shift_until - time.time())
                else:
                    return  # still on site; the clock-out is not due yet
            if self._punch("Clock Out", confirmed_early=True):
                self.clocked_in = False
                if ONE_SHIFT_EACH:
                    # One shift done. In stagger mode the run is a single ramp, so retire the
                    # greenlet rather than looping into a second shift nobody asked for.
                    raise StopUser()
        else:
            if self._punch("Clock In"):
                self.clocked_in = True
                self.punched = True
                # Measured from the ramp slot, not from now: the punch itself costs ~0.8 s, and
                # starting the shift clock at the response would push every clock-out late by
                # that much - enough to destroy the spacing the clock-ins got right.
                origin = (
                    _RAMP_ZERO + _arrival_offset(self.ordinal)
                    if (POISSON or PUNCH_GAP > 0)
                    else time.time()
                )
                self.shift_until = origin + random.uniform(*SHIFT_SECONDS)

    @task(6)
    def work_poll(self) -> None:
        """What a worker's phone does for the *whole* shift, not just at the gate.

        This is the part that was missing and the reason the vCPU read as a trickle: a real
        worker opens the app at the gate, then keeps opening it while they are on site -
        their running total, their notifications, their own history, whether the window where
        they are standing is still open. At these weights that is three app reads for every
        punch - roughly what a phone produces across a working day - and unlike the punch they
        are ordinary cheap reads, so they scale the load without being throttled by the face
        engine.
        """
        if not self.token:
            return
        choice = random.random()
        if choice < 0.45:
            self.get_json("/api/v1/worker/me/stats", name="/api/v1/worker/me/stats")
        elif choice < 0.75:
            self.get_json(
                "/api/v1/worker/me/notifications",
                name="/api/v1/worker/me/notifications?limit=50",
                params={"limit": 50},
            )
        elif choice < 0.92:
            self.get_json(
                "/api/v1/worker/me/logs",
                name="/api/v1/worker/me/logs?limit=[n]",
                params={"limit": random.choice((20, 50, 100))},
            )
        else:
            self.get_json(
                "/api/v1/worker/me/site-window",
                name="/api/v1/worker/me/site-window?location_input=[fix]",
                params={"location_input": _random_fix()},
            )

    def arrive(self) -> None:
        """The look-around on opening the app, before the selfie is taken."""
        if not self.token:
            return
        self.get_json(
            "/api/v1/worker/me/site-window",
            name="/api/v1/worker/me/site-window?location_input=[fix]",
            params={"location_input": _random_fix()},
        )
        self.get_json("/api/v1/worker/me/stats", name="/api/v1/worker/me/stats")


# --------------------------------------------------------------------------- #
# Administrator
# --------------------------------------------------------------------------- #
class AdminUser(ApiUser):
    """The administrator console."""

    token_label = "admin"
    weight = 3 if (ADMIN_TOKEN_ENV or (ADMIN_ID and ADMIN_LOGIN and ADMIN_PASSWORD)) else 0

    def on_start(self) -> None:
        self.token = _token(self, "admin")
        if not self.token:
            log.warning("no admin token; AdminUser will idle (set LOCUST_ADMIN_*)")
        _discover(self.token, self.client)

    def _needs_token(self) -> bool:
        return bool(self.token)

    # --- the Live Ops board: the console's own poll loop ------------------- #
    @task(6)
    def active_sessions(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/active_sessions", name="/api/v1/admin/active_sessions")

    @task(4)
    def crossing_count(self) -> None:
        """The nav badge's small count read - polled far more often than the queue."""
        if self._needs_token():
            self.get_json("/api/v1/admin/overtime/crossings/count",
                          name="/api/v1/admin/overtime/crossings/count")

    @task(3)
    def crossings(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/overtime/crossings", name="/api/v1/admin/overtime/crossings")

    # --- roster and logs --------------------------------------------------- #
    @task(5)
    def users(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/users", name="/api/v1/admin/users")

    @task(2)
    def one_user(self) -> None:
        if self._needs_token() and USER_IDS:
            self.get_json(
                f"/api/v1/admin/users/{random.choice(USER_IDS)}",
                name="/api/v1/admin/users/[id]",
            )

    @task(5)
    def logs(self) -> None:
        if self._needs_token():
            limit = random.choice((100, 200, 500))
            self.get_json("/api/v1/admin/logs", name="/api/v1/admin/logs?limit=[n]", params={"limit": limit})

    @task(3)
    def audit_log(self) -> None:
        if self._needs_token():
            limit = random.choice((100, 200, 500))
            self.get_json("/api/v1/admin/audit_log", name="/api/v1/admin/audit_log?limit=[n]",
                          params={"limit": limit})

    @task(2)
    def workers_live(self) -> None:
        if self._needs_token() and SITES:
            site = random.choice(SITES)[2]
            if site:
                self.get_json(
                    f"/api/v1/admin/workers_live/{site}",
                    name="/api/v1/admin/workers_live/[site]",
                )

    # --- configuration reads ---------------------------------------------- #
    @task(3)
    def sites(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/sites", name="/api/v1/admin/sites")

    @task(2)
    def site_categories(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/site_categories", name="/api/v1/admin/site_categories")

    @task(2)
    def shift_rules(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/shift_rules", name="/api/v1/admin/shift_rules")

    # --- the review queue -------------------------------------------------- #
    @task(3)
    def pending_reviews(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/pending_reviews", name="/api/v1/admin/pending_reviews")

    @task(2)
    def registrations(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/registrations", name="/api/v1/admin/registrations")

    @task(2)
    def notes(self) -> None:
        if self._needs_token():
            self.get_json(
                "/api/v1/admin/notes",
                name="/api/v1/admin/notes?status=open&limit=100",
                params={"status": "open", "limit": 100},
            )

    # --- reports: the CPU-bound reads (SQLite is a single file, single writer) #
    @task(3)
    def report_shifts(self) -> None:
        if self._needs_token():
            start, end = _random_window()
            self.get_json(
                "/api/v1/admin/reports/shifts",
                name="/api/v1/admin/reports/shifts?start=[d]&end=[d]",
                params={"start": start, "end": end},
            )

    @task(2)
    def report_attendance(self) -> None:
        if self._needs_token():
            start, end = _random_window()
            self.get_json(
                "/api/v1/admin/reports/attendance",
                name="/api/v1/admin/reports/attendance?start=[d]&end=[d]",
                params={"start": start, "end": end},
            )

    @task(2)
    def report_pending(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/reports/pending", name="/api/v1/admin/reports/pending")

    @task(2)
    @tag("heavy")
    def report_export(self) -> None:
        """The streaming CSV export - the heaviest read in the console."""
        if self._needs_token():
            start, end = _random_window()
            kind = random.choice(("shifts", "attendance", "audit", "offline"))
            self.get_json(
                "/api/v1/admin/reports/export",
                name="/api/v1/admin/reports/export?kind=[k]&format=csv",
                params={"kind": kind, "format": "csv", "start": start, "end": end, "limit": 5000},
            )

    # --- operational surfaces --------------------------------------------- #
    @task(2)
    def readiness(self) -> None:
        if self._needs_token():
            params = {"deep": 1} if DEEP_READINESS else None
            self.get_json(
                "/api/v1/admin/readiness" + ("?deep=[d]" if DEEP_READINESS else ""),
                name="/api/v1/admin/readiness" + ("?deep=[d]" if DEEP_READINESS else ""),
                params=params,
            )

    @task(1)
    def status_detail(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/status/detail", name="/api/v1/status/detail")

    @task(1)
    def coverage_report(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/coverage_report", name="/api/v1/admin/coverage_report")

    @task(1)
    def retention(self) -> None:
        if self._needs_token():
            self.get_json("/api/v1/admin/retention", name="/api/v1/admin/retention")

    @task(1)
    def needs_reenrollment(self) -> None:
        if self._needs_token():
            self.get_json(
                "/api/v1/admin/enrollment/needs_reenrollment",
                name="/api/v1/admin/enrollment/needs_reenrollment",
            )

    @task(1)
    @tag("auth")
    def login_probe(self) -> None:
        if not (ADMIN_ID and ADMIN_LOGIN and ADMIN_PASSWORD):
            return
        with self.client.post(
            LOGIN_PATH,
            json={"user_id": ADMIN_ID, "email_or_phone": ADMIN_LOGIN, "password": ADMIN_PASSWORD},
            name=f"{LOGIN_PATH} [admin probe]",
            catch_response=True,
        ) as response:
            self._judge(response)

    # --- destructive mutations, opt-in only -------------------------------- #
    @task(1)
    @tag("destructive")
    def edit_shift_rules(self) -> None:
        """Rewrite the company-wide window. Only with ``ALLOW_DESTRUCTIVE=1``.

        This is the single most dangerous task in the file: it changes how every future
        punch in the company is judged (late flags, windows). It is included because it is
        a genuine admin write path, and it is behind a flag, a tag and a warning for the
        same reason.
        """
        if not (ALLOW_DESTRUCTIVE and self._needs_token()):
            return
        self.post_json(
            "/api/v1/admin/shift_rules",
            name="/api/v1/admin/shift_rules [write]",
            payload={
                "clock_in_window_start": random.choice(("06:00", "07:00", "08:00")),
                "clock_in_window_end": random.choice(("10:00", "11:00", "12:00")),
            },
        )


# --------------------------------------------------------------------------- #
# One-off wiring
# --------------------------------------------------------------------------- #
def _apply_only() -> None:
    """``LOCUST_ONLY`` restricts which classes may spawn.

    ``LOCUST_ONLY=punch`` with ``-u 20`` spawns exactly 20 punching workers and nothing
    else, which is what "20 users checking in and out" has to mean.
    """
    if not ONLY:
        return
    classes = {"public": PublicUser, "worker": WorkerUser, "admin": AdminUser, "punch": PunchUser}
    defaults = {"public": 5, "worker": 6, "admin": 3, "punch": 10}
    for name, cls in classes.items():
        cls.weight = defaults[name] if name in ONLY else 0


_apply_only()


@events.test_start.add_listener
def _announce(environment, **_kwargs) -> None:
    global _RAMP_ZERO, _ARRIVAL_OFFSETS
    host = environment.host or DEFAULT_HOST
    # The ramp's zero instant. Fires before any user spawns, so every worker in the roster
    # computes its slot from the same clock tick and the lead-in covers the logins.
    _RAMP_ZERO = time.time() + RAMP_LEAD_IN
    # Draw the whole roster's arrival process once, here, rather than per worker: the arrivals
    # are one process, not N independent waits (see ``_poisson_offsets``).
    if POISSON and ROSTER:
        _ARRIVAL_OFFSETS = _poisson_offsets(len(ROSTER))
    punch_on = bool(PunchUser.weight)
    lines = [
        "",
        f"locust -> {host}",
        f"  class selection  : {', '.join(sorted(ONLY)) if ONLY else 'all configured classes'}",
        f"  public edge      : {'on' if PublicUser.weight else 'off'} (status / readiness / branding / static)",
        f"  worker traffic   : {'on' if WorkerUser.weight else 'OFF (no LOCUST_WORKER_* configured)'}",
        f"  admin traffic    : {'on' if AdminUser.weight else 'OFF (no LOCUST_ADMIN_* configured)'}",
        f"  punch cycle      : {'on - ' + str(len(ROSTER)) + ' roster accounts' if punch_on else 'OFF (no LOCUST_ROSTER)'}",
        f"  punch fence      : {SITE_FIX or 'discovered from /admin/sites (needs admin creds)'}",
        f"  punch ramp       : {'every %.2fs, one shift each' % PUNCH_GAP if PUNCH_GAP else 'off (no stagger)'}",
        f"  arrival process  : "
        + (f"Poisson {POISSON_RATE:.3g}/s (seed {POISSON_SEED})" if POISSON
           else (f"even ramp, {PUNCH_GAP:.2f}s apart" if PUNCH_GAP else "all at once")),
        f"  fresh logins     : "
        + (f"{FRESH_LOGIN_RATIO:.0%} of accounts (every {_fresh_stride()}th)"
           if FRESH_LOGIN_RATIO > 0 else "off (every token cached in the lead-in)"),
        f"  ramp lead-in     : {RAMP_LEAD_IN:.1f}s (logins must finish inside it)",
        f"  benign writes    : {'on' if ALLOW_WRITES else 'OFF'}",
        f"  destructive      : {'on' if ALLOW_DESTRUCTIVE else 'OFF'}",
        f"  deep readiness   : {'on' if DEEP_READINESS else 'off'}",
        f"  429 counted as   : {'success (the per-IP limiter)' if TREAT_429_AS_SUCCESS else 'failure'}",
    ]
    if punch_on and not (SITE_FIX or ADMIN_TOKEN_ENV or ADMIN_ID):
        lines.append("  !! no fence and no admin creds: punches will be refused 403 outside every geofence")
    if punch_on and not ROSTER[0].get("selfie") and not PUNCH_BYTES:
        lines.append("  !! no selfie per account and no LOCUST_SELFIE: punches will be refused")
    if ALLOW_DESTRUCTIVE:
        lines.append("  !! destructive tasks run against a LIVE database - shift rules will change")
    log.info("\n".join(lines))


@events.test_stop.add_listener
def _summary(environment, **_kwargs) -> None:
    stats = environment.stats.total
    log.info(
        "locust summary: %d requests, %.1f%% failures, median %d ms, p95 %d ms",
        stats.num_requests,
        stats.fail_ratio * 100.0,
        int(stats.median_response_time),
        int(stats.get_response_time_percentile(0.95) or 0),
    )
