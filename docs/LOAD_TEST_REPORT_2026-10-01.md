# Load test report — the punch path and the sign-in wall

**What this is.** A Locust load test driven against the live Railway deployment
(`https://al-jehad-production.up.railway.app`, `version phase02`) on the night of
2026-09-30 into 2026-10-01, from a single client machine. The goal was the user's own:
put real workers through the gate — clock in, wait, clock out — at increasing concurrency,
until the deployment breaks.

**Headline.** The punch path does not break. It bends: a clock-in goes from a 1.5 s
median at 50 workers at the gate to **4.3 s at 150**, with an 8 s worst case — and not one
server error, no 500, no 503, no connection failure, across 1,225 requests in the widest
stage. The only failures the deployment ever produced were its own rate limiter
(`429`). The thing that actually breaks a shift start is **signing in**: 150 accounts
opening the app cold wait **28.7 seconds**, while 150 accounts punching take about five.

---

## 1. Scope

| | |
|---|---|
| Target | Railway deployment, single uvicorn process, `limit_concurrency=64` (`backend/serve.py`) |
| Endpoint under test | `POST /api/v1/attendance/verify` (multipart: selfie + action + location) |
| Satellites | `/worker/me/stats`, `/site-window`, `/logs`, `/notifications`, `/auth/login` |
| Tooling | the repo's own harness — `backend/tools/locustfile.py`, `locust_provision.py`; Locust 2.46.6 |
| Accounts | 50, then 150 real accounts (ids 6–155), one per simulated worker, each face-enrolled |
| Site | `office` (29.344001, 48.016299), the deployment's first site; window 05:00–08:00 Asia/Kuwait |
| Wall clock | 2026-09-30 23:31 → 2026-10-01 00:16 (client local) |

**Why one account per worker.** `active_sessions` holds one open shift per `worker_id`, so
N people checking in genuinely needs N enrolled accounts — a shared account answers
"Already clocked in!" to all but the first. That is a hard structural limit on burst width
and it is what eventually capped this test at 150.

**A caveat that applies to every latency below.** This machine is roughly **400 ms** from
the deployment's region — `/api/v1/status`, a trivial endpoint, medians at 410 ms. Every
number here is client-observed and therefore includes that round trip. A 4.3 s clock-in is
about 3.9 s of server-side work, not 4.3 s.

---

## 2. Method

Three distinct load shapes, and the difference between them matters:

1. **Read-only baseline** — the public edge (`--tags public`), no credentials, writes nothing.
2. **Ramp** — 50 workers, first punch staggered `LOCUST_PUNCH_GAP=0.2` apart, a 20 s shift
   each. This is "50 people arriving at the gate over ten seconds".
3. **Simultaneous burst** — every worker punches within the same 50 ms
   (`LOCUST_PUNCH_GAP=0.001`), one shift each. This is "the gate opens".

### How true simultaneity was achieved (and the trap in it)

The harness's `--stress` ladder sets `LOCUST_PUNCH_GAP=0` and describes its stages as
"workers punch simultaneously". **They do not.** With `PUNCH_GAP == 0` the `PunchUser`
takes a random 1.5–4 s think time before its first task and then spends its first
`punch_cycle` call on a two-request "look-around", so the clock-ins scatter over tens of
seconds. Evidence: p95 was **880 ms at 5 workers and 1100 ms at 20** — flat, because the
server never actually saw them at once.

Setting `LOCUST_PUNCH_GAP=0.001` changes the code path rather than the interval:
`PUNCH_GAP > 0` puts the worker on the arrival-timed branch, which **skips the look-around
and returns zero think time until the first punch**, and schedules each worker at
`ordinal × 0.001 s`. All N then punch inside a 50 ms window. Every "simultaneous" number
below comes from that.

### Hygiene between stages

A stage that ends while a worker is still on site leaves that shift open, and the *next*
stage's worker on the same account is refused `400 "Already clocked in!"`. Left alone this
manufactures fake failures at scale (it produced a **52.9% failure rate** that was entirely
two stuck accounts retrying). `close_shifts.py` now force-closes every leftover shift for
the roster before and after each stage; the ramp and burst stages in this report all ran
against a clean board.

---

## 3. Results

### 3.1 Public read-only baseline (30 users, 45 s, writes nothing)

222 requests, **0 failures**.

| endpoint | reqs | median | p95 |
|---|---|---|---|
| `/api/v1/status` | 117 | 410 ms | 1000 ms |
| `/api/v1/readiness` | 43 | **6900 ms** | **16000 ms** |
| `/api/v1/branding` | 28 | 480 ms | 1000 ms |
| `/` + static bundle | 34 | ~730 ms | ~900 ms |

The trivial endpoint is the control line and it is flat. `/api/v1/readiness` is by far the
most expensive public read — 6.9 s median, 16 s p95 — because it runs the full self-test.
It is cheap to hammer and expensive to serve.

### 3.2 The ramp — a real shift start (50 workers, 0.2 s apart, 20 s shift)

**100 punches, 0 failures, all 102 records read back `Approved`.**

| endpoint | sent | failed | min | median | p95 | max |
|---|---|---|---|---|---|---|
| `verify [Clock In]` | 50 | 0 | 607 | 810 | **990** | 1054 |
| `verify [Clock Out]` | 50 | 0 | 419 | 600 | **780** | 846 |

Workers actually arrived +0.37 s to +11.5 s — the configured 10 s ramp, slightly stretched
by ~0.8 s of service time per punch. That is a sustained ~4.5 punches/s with ~4 requests in
flight at any instant, and it costs the server about 400 ms per verified clock-in.

**The logins in that same run are the story.** All 50 accounts opened cold inside a 25 s
lead-in, and the mint latencies came back at **9.6–10.9 s each**.

### 3.3 The simultaneous burst — the curve

One shift per worker, all punches inside a 50 ms window, `429` counted as a *failure* so
nothing could hide behind the harness's default leniency.

| at once | clock-ins | in fails | in p50 | in p95 | in max | clock-outs | out fails | out p50 | out p95 |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 10 | 0 | 1200 | 1400 | 1360 | 10 | 0 | 720 | 760 |
| 20 | 20 | 0 | 960 | 1300 | 1280 | 20 | 0 | 670 | 1100 |
| 30 | 30 | 0 | 990 | 1600 | 1825 | 30 | 0 | 1900 | 2000 |
| 40 | 40 | 0 | 1200 | 2000 | 2250 | 40 | 0 | 1900 | 2200 |
| 50 | 50 | 0 | 1500 | 2100 | 2186 | 50 | 0 | 1900 | 2300 |
| 100 | 100 | 0 | **2800** | **4900** | 4975 | 102 | 2 | 800* | 3100 |
| 150 | 152 | 2 | **4300** | **6500** | **7966** | 182 | **39** | 630* | 5100 |

\* Misleading, and worth reading past. At 100 and 150 the clock-out p50 is dragged down by
**fast 429 rejections** (~370–700 ms) mixed into the same percentile. Excluding them, the
served clock-outs were **2.8 s at p75 and 5.1 s at p95** in the 150 stage. The `72%` figure
in Locust's own output is the honest one.

**Reading the curve.** Nothing at all happens up to 50. Past 50 the latency climbs roughly
linearly — about **+26 to +30 ms of queue per additional concurrent clock-in** — and the
worst case goes 2.2 s → 5.0 s → 8.0 s. Throughput, estimated from how fast each stage
drained: **19–24 verified punches per second across the three widest stages**. So the gate
saturates somewhere around **20 punches per second**, and beyond that the queue simply grows.

### 3.4 What actually failed

Every one of the 41 failures in the widest stage was:

```
429: {"error":"Rate limit exceeded: 15 per 1 minute"}
```

— the deployment's own `ATTENDANCE_RATE_LIMIT` (default `15/minute`), refusing the request
before it reached the face engine. Breakdown: **2 at 100 workers** (both clock-outs),
**41 at 150** (39 clock-outs, 2 clock-ins). There were **no 5xx responses, no 503s from the
face engine, no 422 face mismatches and no connection errors** — Locust's exceptions file is
empty for every stage — and the deployment reported `ok: true, degraded: false` immediately
afterwards.

The clock-out path is consistently the heavier of the two from 30 workers up
(p50 1900 ms vs 990 ms at 30; 1900 vs 1500 at 50) — it settles a shift and writes the
derived hours as well as the row.

### 3.5 Signing in is the wall

Measured per login, from the same burst stages (each account signing in cold):

| burst | cold logins | min | median | p95 | max |
|---|---|---|---|---|---|
| 100 | 100 | 1168 ms | **20 754 ms** | 20 877 ms | 21 237 ms |
| 150 | 150 | 1459 ms | **28 743 ms** | 28 821 ms | 28 853 ms |

The signature — **median ≈ max**, and both equal to roughly `N × 0.2 s` — is exactly what a
fully serialised CPU-bound step looks like: 100 × ~0.2 s = 20.7 s, 150 × ~0.19 s = 28.7 s.
Bcrypt runs on one core at **~5 logins/s**, so every login in the wave waits for the whole
CPU budget rather than 1/N of it. An earlier 50-worker ramp showed the same thing
(9.6–10.9 s per login).

Put beside §3.3: **150 workers punch through the gate in ~5–6 s while 150 workers opening the
app cold wait ~29 s.** The gate is not the bottleneck, and no amount of punch-path work
will move the shift-start experience.

---

## 4. Findings beyond the latency curve

### 4.1 Anti-spoofing is not running

Every successful punch returns:

```json
"liveness": {"verdict": "unavailable", "is_live": false, "available": false,
             "error_code": "liveness_unavailable",
             "model": "/app/backend/models/minifasnet.onnx",
             "detail": "FileNotFoundError: ..."}
```

`minifasnet.onnx` is a `.gitignore`d binary that has to be placed by hand
(`backend/models/README.md`) and is absent from the image. Because the check is **advisory**
rather than fatal, the deployment boots and serves — and **all 1,278 punches this session
were auto-approved with no liveness check at all** (`"Auto-Approved (Clocked In at office)"`).
This is a security posture question, not a performance one.

### 4.2 The per-IP rate limiter is erratic

Three probes, all from one client, all against the same route with a `15/minute` limit:

| probe | result |
|---|---|
| 22 rapid punch calls in 29 s (invalid action, writes nothing) | **0 × 429** |
| 14 rapid logins in 18 s | **0 × 429** |
| ~200 verify calls in ~90 s at 100 concurrent | **2 × 429** |
| 334 verify calls at 150 concurrent | **41 × 429** (≈12%) |

A `15/minute` per-IP limit cannot admit 200 calls in 90 s from one address with two
rejections. The limiter is keyed with `get_remote_address` (`rate_limit.py:18`) and the
deployment trusts `X-Forwarded-For` from Railway's edge, so the key is resolving to
something that varies per request or per edge hop rather than to the caller. The practical
consequences are both bad in opposite directions: ordinary abuse is not rate limited, and
a legitimate shift start gets randomly refused once it is loud enough. `TRUSTED_PROXIES` /
the `network_policy` readiness check is the knob, and `readiness` reports it as **advisory**
only.

### 4.3 Two measurement traps in the harness

* **Locust's CSV races its own console.** The first ladder reported `only 19/20 completed a
  clock-in` and stopped as a breach; the console from the same run says **20 clock-ins,
  0 failures**. The statistics file was flushed before the last request landed. Any verdict
  computed from `*_stats.csv` alone (which is what `locust_provision.py` does) can be wrong
  by one, which at a threshold reads as a false breach.
* **`--stress` does not clean up between stages.** Its own docstring warns that a stage
  ending mid-shift "turns a clean deployment into a fake 400 'Already clocked in!' breach",
  but it only force-closes shifts *before* a stage, not after — so the last stage's tail
  always leaks into the next one.
* **Simultaneity and repetition are mutually exclusive.** `ONE_SHIFT_EACH = PUNCH_GAP > 0 or
  _flag("LOCUST_ONE_SHIFT")`, so the switch that produces simultaneity (`PUNCH_GAP > 0`)
  *forces* one shift per worker and the flag cannot override it. A sustained soak — 150
  workers cycling continuously — is therefore not currently expressible without a code change.

---

## 5. What was left behind

| | |
|---|---|
| Accounts created | **150** (`Load Test Worker N`, ids 6–155), plus the 7 that already existed |
| Attendance rows | **1,278** (639 clock-ins, 639 clock-outs), all at site `office` |
| Final account state | all 150 **inactive**, faces deleted, `0/150` enrolled |
| Open shifts | **0** |
| Consent before acting | burst width, and the 100 extra accounts, both chosen by the user |

Deactivating deletes the face templates by design ("a departed worker's face should not stay
enrolled for a clock-in they will never make"), and `--reactivate` re-enrols them from the
roster. The accounts **cannot be deleted** — `users/delete` refuses an account with
attendance records — so deactivate is their terminal state. The only remaining cost of a
re-run is one row per shift.

Artifacts (gitignored, under `temp/loadtest/`): `roster150.json` (carries passwords),
`run.log`, `stress_a.log`, `big_100.log`, `big_150.log`, and the `*_stats.csv` /
`*_failures.csv` for every stage.

---

## 6. What we did **not** test

Stated plainly, because a report that only lists what passed is not useful:

* **Sustained load.** Every stage was one shift per worker. Nothing here says what happens
  after ten minutes of continuous punching — no soak, so no evidence about SQLite WAL
  growth, connection reuse over time, memory leaks or a delayed OOM.
* **Mixed load.** Punchers never competed with the console. The CPU-bound report/CSV-export
  paths sharing one core with the face engine are untested, and that is the most likely way
  to actually saturate this instance.
* **Multi-site behaviour.** Everything punched from one geofence (`office`); geofence
  matching cost is measured for one site only.
* **Anything writing outside the punch.** No enrollment, registration, approvals, notes,
  overtime crossings, offline sync or push delivery. `LOCUST_ALLOW_DESTRUCTIVE` and
  `LOCUST_ALLOW_WRITES` were never enabled.
* **Platform limits.** Railway's CPU throttle, memory ceiling, restart count and egress
  connection limits were never instrumented — the deployment's own `/api/v1/status` and
  `readiness` are the only health witnesses, and the dashboard was not consulted.
* **The wrong-face path at volume.** Face mismatch was observed once, by accident
  (score 0.5603 → `422 face_mismatch`), which confirms the band works for one request;
  the refusal path was never measured under load.

---

## 7. Recommended next steps

1. **Treat the sign-in wall as the defect.** 150 cold logins → 29 s is the only number in
   this report that describes a broken user experience, and it is unaffected by anything in
   the punch path. Pre-computed hashes, a cheaper KDF calibration, or making the login burst
   asynchronous are all worth looking at.
2. **Establish whether the rate limiter is enforcing anything.** A single address sent ~200
   verify calls in 90 s past a `15/minute` limit. Either the key is wrong in the deployment
   or the limits are raised for the load test and should be lowered again.
3. **Decide about liveness.** Ship `minifasnet.onnx` into the image, or make its absence a
   louder signal than an advisory check — right now it is invisible unless you read a punch
   response body.
4. **Fix the harness before trusting it again.** Compare the console table to the CSV (or
   sum both) before declaring a breach; clean up shifts after every stage; and decouple
   `ONE_SHIFT_EACH` from `PUNCH_GAP` so simultaneity and repetition can be combined.
5. **If a genuine break is still wanted**, the next lever is mixed load — 150 punchers
   competing with report exports on the same core — not more punchers. §3.3 suggests the
   punch path alone will simply queue rather than fail.

---

### Appendix — reproducing this

```bash
# 1. accounts (writes real accounts + faces to the deployment)
LOCUST_ADMIN_ID=5000 LOCUST_ADMIN_LOGIN=admin@siteops.com \
LOCUST_ADMIN_PASSWORD=… python backend/tools/locust_provision.py \
  --host https://al-jehad-production.up.railway.app \
  --count 150 --photo-dir worker_photos --out temp/loadtest/roster150.json

# 2. put them back on (re-enrols faces a deactivation removed)
LOCUST_ADMIN_PASSWORD=… python backend/tools/locust_provision.py \
  --host https://al-jehad-production.up.railway.app \
  --roster temp/loadtest/roster150.json --reactivate

# 3. a simultaneous burst of N — the gap is what makes it simultaneous
export LOCUST_ONLY=punch LOCUST_ROSTER=temp/loadtest/roster150.json
export LOCUST_SITE_FIX=29.344001,48.016299 LOCUST_ALLOW_PUNCH=1
export LOCUST_PUNCH_GAP=0.001 LOCUST_ONE_SHIFT=1
export LOCUST_SHIFT_SECONDS_MIN=10 LOCUST_SHIFT_SECONDS_MAX=10
export LOCUST_RAMP_LEAD_IN=75 LOCUST_429_IS_SUCCESS=0
for N in 50 100 150; do
  python temp/loadtest/close_shifts.py temp/loadtest/roster150.json
  python -m locust -f backend/tools/locustfile.py --headless \
    --host https://al-jehad-production.up.railway.app \
    -u $N -r 300 -t 140s --csv temp/loadtest/burst_$N --only-summary
done

# 4. put the deployment back as it was found
LOCUST_ADMIN_PASSWORD=… python backend/tools/locust_provision.py \
  --host https://al-jehad-production.up.railway.app \
  --roster temp/loadtest/roster150.json --deactivate
```

`LOCUST_RAMP_LEAD_IN` must exceed the sign-in wave or the punches fire late: at 150 accounts
budget **≥60 s** (§3.5). `LOCUST_429_IS_SUCCESS=0` is deliberate — leaving it at its default
counts rate-limit refusals as successes and hides the only failure mode this test found.
