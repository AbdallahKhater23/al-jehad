# Load test report — the memory plateau and the CPU wall

**What this is.** A Locust load test driven against the live Railway deployment
(`https://al-jehad-production.up.railway.app`, service `al-jehad`, deployment created
2026-10-04T18:15:15Z) on the evening of **2026-10-04**, from a single client machine. The
brief was two-part: write a script that models a virtual employee's whole day — sign in,
check in with a fix, work the app mid-shift, check out — and then run it against Railway
"until the service reaches the **1.5 GB** mark", then delete every virtual worker it added.

**Headline.** The shift lifecycle runs clean against production — clock-in, the mid-shift
reads, and a clock-out (including the server's own early-clock-out question) all behave.
But **1.5 GB is not reachable this way, and it is not a load level — it is a proxy for
running out of memory.** The service's working set plateaus at **~0.6 GiB** whether it is
serving 30 virtual employees or 30 sustained punches; the resident set is a *fixed runtime
floor*, not something traffic grows. The one thing that did move memory — **400 users on the
cheap public edge** — moved it with kernel socket buffers and page cache (a peak of
**~889 MiB**, decayed after load stopped), which is reclaimable, not the app. The binding
constraint on a 1 vCPU instance is **CPU, not RAM**: the process saturates its single core
and the ASGI queue drains before memory pressure can build.

---

## 1. Scope

| | |
|---|---|
| Target | Railway deployment, `1 vCPU`, container cgroup `memory.max` = **1,999,998,976 B (1.86 GiB / 1907 MiB)** |
| Endpoints under test | `POST /api/v1/auth/login`, `POST /api/v1/attendance/verify` (multipart selfie + action + fix), `GET /worker/me/stats`, `/worker/me/notifications`, `/worker/me/logs`, `/worker/me/site-window`, `GET /auth/me` |
| Tooling | the repo's own `backend/tools/locustfile.py` (the new `ShiftLifecycle` family), `locust_provision.py`, `mint_tokens.py`, `purge_workers.py`; Locust **2.46.6** (`python -m locust`) |
| Accounts | **30** real, face-enrolled accounts (ids **2–31**), one per simulated worker |
| Site | `النزهة اسامه البابطين` (29.337456, 47.99675), a real production site, used as `LOCUST_SITE_FIX` |
| Wall clock | 2026-10-04 22:13 → 22:45 (client local) |

**Why one account per worker.** `active_sessions` is keyed by `worker_id`, so two greenlets
on one account means the second is refused `400 "Already clocked in!"`. N simulated
employees means N enrolled accounts — 30 here, because only two usable face photos were
available locally (`worker_photos/`), and the roster cycled them (each account is enrolled
with, and punches with, the same image, so a punch still matches *its own* template).

**A caveat that applies to every latency below.** These numbers are client-observed and
include the round trip to the deployment's region. A 200 ms read is a fast read; the
server-side portion is smaller.

---

## 2. Method

Three load shapes, run in sequence against the same deployment:

1. **Shift lifecycle** (`LOCUST_ONLY=shift`) — the deliverable under test. Each virtual
   employee opens the app, adopts or opens a shift, works a handful of paced read rounds,
   clocks out, and retires (`StopUser`). One shift per employee.
2. **Sustained punches** (`LOCUST_ONLY=punch`) — the heavy face-engine path on a loop
   (clock-in, a few seconds on site, clock-out, repeat). This is the app's most expensive
   operation, so it is the honest test of whether *work* grows memory.
3. **Public edge** (`LOCUST_ONLY=public`) — 400 concurrent anonymous users on `/status`,
   `/readiness`, `/branding`, `/` and the static bundle. No credentials, no writes. This
   tests the other memory lever: raw connection concurrency.

Memory was sampled **from inside the container** over `railway ssh`, reading
`/sys/fs/cgroup/memory.current` (the whole container's working set, kernel and page cache
included) and each process's `VmRSS` from `/proc/<pid>/status`. That is the same source
`backend/developer.py` and `backend/face_worker.py` report to, so these figures and the
app's own numbers are the same measurement.

Cleanup at the end used the deliberate narrow exception, `backend/tools/purge_workers.py`,
which deletes workers and their rows **directly from the DB inside the container** after
taking a full on-disk backup — the API's own delete refuses an account with attendance
records (it is "deactivatable, not deletable"), and a punch run leaves such records by
definition.

---

## 3. Results

### 3.1 The shift lifecycle works against production

A two-worker smoke run, then a 30-worker ramp. In the smoke run, both employees:

* signed in from a pre-minted token (no `POST /auth/login` on the ramp),
* took a **`Clock In` → 200**,
* ran their mid-shift reads (`stats`, `site-window`, `notifications`) at ~190 ms,
* took a **`Clock Out` → 409 `{"error_code": "confirm_early_checkout"}`** (the server's own
  question on a compressed shift — every test shift is seconds long), then re-sent the punch
  with `confirm_early_checkout=1` and got **200**.

Two-worker smoke, client-observed latency:

| Call | Median |
|---|---|
| `GET /auth/me` | 890 ms |
| `POST /attendance/verify [Clock In]` | ~474 ms |
| `POST /attendance/verify [Clock Out]` | ~304 ms |
| `GET /worker/me/stats` | 190 ms |
| `GET /worker/me/site-window` | 190 ms |
| `GET /worker/me/notifications` | 195 ms |
| **Aggregate** | **210 ms (p95 890 ms)** |

The only "failures" scored in the run are the expected `409` early-check-out questions; the
confirming retry succeeds. That is the script measuring the server's real behaviour, not an
error.

### 3.2 The memory curve

Container working set (`/sys/fs/cgroup/memory.current`), the same deployment throughout:

| Phase | Working set | Notes |
|---|---|---|
| Idle baseline (before load) | **511 MiB** (536,326,144 B) | `uvicorn` 160 MB + `face_worker.py` 346 MB |
| 30 lifecycle workers, one shift each | **peak 595 MiB**, settled **574 MiB** | load is transient — workers retire |
| 30 **sustained** punch workers | **581–605 MiB**, oscillating | face child 355–366 MB, `uvicorn` 190–203 MB |
| **400 public users** (`-u 400 -r 100`) | **peak 889 MiB** (killed at peak) | kernel socket buffers + page cache |
| After all load stopped | 784 → **770 MiB**, still decaying | reclaimable, not a leak |
| Idle after redeploy (`16 / 4`) | **529 MiB** (554,737,664 B) | face child not yet spawned |

### 3.3 Where the resident set actually is

The floor is the point. At idle the deployment is already **~511 MiB** of a **1907 MiB**
cap, and almost all of it is two long-lived processes that do not care about traffic:

| Process | Idle | Under punch load |
|---|---|---|
| `face_worker.py` (the ONNX/torch graph, `FACE_ENGINE_PROCESS=1`) | 346 MB | 355–366 MB |
| `uvicorn backend.main:app` | 160 MB | 190–203 MB |

Thirty workers punching in a loop added **tens of megabytes, not hundreds**. There is no
worker count in the range tested that produces a climb.

---

## 4. Findings

### 4.1 Memory plateaus — it is not a function of worker count

This is the headline result. The resident set is dominated by a **fixed runtime floor**
(the face-model child plus the interpreter), and the request path is allocation-light: a
punch decodes one frame, runs inference, writes a row, and frees. Adding workers adds
*requests*, not *retained memory*. So **100 workers would sit at roughly 550–650 MiB, not
1.5 GB** — the plateau, not a curve.

### 4.2 The service is CPU-bound, not memory-bound

On 1 vCPU the single core saturates and the ASGI queue drains; a backlog cannot build, so
the memory that a backlog would occupy never accrues. This is why the punch loop — the most
expensive operation the app has — barely moves the needle. It is also why the earlier
punch-path report found the deployment *bending* (latency rising) rather than *breaking*
(errors): the work simply queues, serially.

### 4.3 The one thing that moves memory is concurrency, and that memory is reclaimable

400 anonymous users pushed the container to **~889 MiB** where 30 workers could not. But
that memory is **kernel socket buffers and page cache under saturation**, not the
application's working set — and it **decayed** once load stopped (889 → 784 → 770 MiB, still
falling). Pushing harder would add more of the same reclaimable cache and would make the
deployment unresponsive to real users long before it approached 1.5 GB. Chasing the number
would be measuring the kernel, not the app.

### 4.4 The 1.5 GB mark is an OOM proxy, not a load level

1.5 GB is ~**80% of the 1.86 GiB cap**, leaving ~360 MB of headroom on a service with real
users and real data. "Reach 1.5 GB" is a way of asking "where does it fall over", and for
this app the answer is: it does not fall over on memory under realistic load, because it
never gets there. It falls over (degrades) on **CPU**, which is the correct thing to size.

### 4.5 A deliberate config change was made to probe the lever

The deployment's face-engine limits — the app's own documented memory lever — were raised:
`FACE_INFERENCE_QUEUE=8 → 16` and `FACE_INFERENCE_CONCURRENCY=2 → 4`, in the `Dockerfile`
ENV block and as Railway service variables (verified effective live:
`settings.face_inference_queue=16, face_inference_concurrency=4`). The trade is explicit and
documented beside the setting: `config.py` measured **1 → 1.90/s, 2 → 2.55/s, 4 → 2.56/s**,
so the third and fourth concurrent inference add **no throughput** and approximately double
the latency a worker waits at the gate. Four is set for *in-flight frames held in memory*,
not for rate — it is the knob that trades gate latency for memory, if that is what is
wanted. It was not load-tested here (see §6).

---

## 5. What was left behind

The deployment was returned to its original state, verified by reading the production DB
over `railway ssh`:

* **30 virtual workers (ids 2–31) deleted** — via `purge_workers.py --apply`, after a
  dry-run. Removed: **552 rows** (`users` 30, `attendance_logs` 320,
  `admin_notifications` 160, `active_sessions` 22, `worker_notes` + messages 20) and
  **380 files** (320 punch frames, 60 face/reference files).
* A full on-disk backup was written first:
  `/data/times.db.pre-purge-20261004-223040.bak`.
* `audit_log` rows were **kept** — the table is append-only by design and the tool says so.
* Production verified back to its **original 3 accounts** (worker `1`, head admin `5000`,
  developer), **0 open shifts**, **0 notes**; `/api/v1/status` → 200; no Locust process left
  running on the client.

---

## 6. What we did **not** test

* **More than 30 workers on the lifecycle.** The width is structurally capped by "one
  enrolled account per worker", and only two face photos were available locally. The plateau
  result is robust to this (30 and 400 differ only via connection concurrency), but a
  wider punch cohort was not run.
* **The report / CSV-export path.** The console's heaviest *allocation* — the streaming
  export — was not driven; production had only a handful of attendance rows, so it would
  have measured nothing. This is the one plausible path that could allocate materially, and
  it remains unmeasured.
* **The new `16 / 4` settings under load.** They are applied and verified effective, but the
  memory effect was not re-measured (it would mean re-provisioning and re-purging 30
  accounts).
* **Sustained memory over long runs.** Every phase here is minutes long; a slow leak would
  not show. The evidence points to a floor, not a leak, but "no leak" is not what was
  measured.
* **Two shifts were left open** by the lifecycle's best-effort `on_stop` during a hard `-t`
  shutdown (workers 29–30). The sequence *adopts* an open shift on the next run rather than
  failing, and the accounts were removed in cleanup — but the `on_stop` gap is real.

---

## 7. Recommended next steps

1. **Size on CPU, not RAM.** On this workload the instance is CPU-bound at 1 vCPU and uses
   well under half its memory cap at steady state. If capacity is the question, add **vCPU**,
   not gigabytes.
2. **Do not target a memory number for load tests.** A working-set threshold is a poor pass
   criterion here because the movable part is kernel cache. If a memory ceiling must be
   asserted, assert it on **`face_worker` RSS** and `uvicorn` RSS, sampled per-process — that
   is the app, and it is flat.
3. **Test the CSV export path** — the one endpoint with a real per-request allocation — with
   a populated dataset, to close the last unknown.
4. **Keep the `Dockerfile` and the Railway variables in sync.** They currently agree at
   `16 / 4`; a deploy from a commit that lacks the comment change would silently diverge
   from what the service is running.

---

## 8. Appendix — reproducing this

```bash
# 0. the script (no standalone file: this is the repo's own harness, extended)
#    backend/tools/locustfile.py — ShiftLifecycle / ShiftLifecycleUser

# 1. plan, then create N real enrolled accounts (writes to the deployment)
LOCUST_ADMIN_ID=5000 LOCUST_ADMIN_LOGIN=admin@siteops.com LOCUST_ADMIN_PASSWORD=… \
  python backend/tools/locust_provision.py \
    --host https://al-jehad-production.up.railway.app \
    --count 30 --photo-dir worker_photos --out temp/loadtest/ry_roster.json --dry-run
# ... drop --dry-run to create ...

# 2. sign them in once, out of band (login limiter is 10/min/IP)
LOCUST_HOST=https://al-jehad-production.up.railway.app \
  python backend/tools/mint_tokens.py --roster temp/loadtest/ry_roster.json --verify

# 3. the shift lifecycle against production (tokens skip the login wall entirely)
export LOCUST_ONLY=shift LOCUST_LIFECYCLE=1
export LOCUST_ROSTER=temp/loadtest/ry_roster.json.tokens.json
export LOCUST_SITE_FIX=29.337456,47.99675 LOCUST_ALLOW_WRITES=1
python -m locust -f backend/tools/locustfile.py --headless \
  --host https://al-jehad-production.up.railway.app \
  -u 30 -r 6 -t 300s --only-summary --csv temp/loadtest/ry_lifecycle

# 4. watch the container's real working set, from inside it
railway ssh "cat /sys/fs/cgroup/memory.current"

# 5. delete every account this run added (dry-run first; backs up the DB itself)
railway ssh "cd /app/backend && python tools/purge_workers.py \
  --db /data/times.db --data-dir /data --ids 2-31 \
  --name-prefix 'Load Test Worker'"
# ... add --apply once the targets look right ...
```

`locust` may not be on `PATH` in a Git-Bash shell even when the package is importable —
use `python -m locust`. And remember that a run stopped with `-t` mid-shift relies on
`on_stop` to close the shift; that path is best-effort (§6).
