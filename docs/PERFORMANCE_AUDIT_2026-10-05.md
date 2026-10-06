# Performance audit — RAM, vCPU, latency

**Date:** 2026-10-05 · **Scope:** the whole repository (backend runtime, SQLite data layer,
frontend payloads, dependency and dead-code surface) · **Status:** findings only at the time
of the audit — **no application code, schema or query was changed during it.** Findings #1 and
#2 were implemented afterwards, on the same day and by explicit authorization; §8 records the
change, its acceptance evidence and the suite results. Everything else remains a proposal, and
nothing is committed.

**Reproduce:** `python temp/perf_audit/perfaudit.py {index|proto|pool|sweep}` (scratch harness,
gitignored, ~100 lines per subcommand). Each subcommand rebuilds its dataset and prints the
numbers quoted here.

---

## 1. Executive summary

The application is a single-process FastAPI + SQLite deployment (1 vCPU, ~1.86 GiB cgroup cap).
Its behaviour under load is already the subject of deliberate tuning (`jemalloc`, ONNX child
process, queue sizing, `scipy` removal — all documented in `Dockerfile` and
`docs/IDLE_RSS_REDUCTION_ANALYSIS.md`). This audit therefore looked for what is **left**, and
found that the remaining cost is concentrated in exactly two places: **one SQLite query that
cannot use an index**, and **per-row Python work in the report that consumes it**.

### Footprint, measured

| Surface | Baseline | Optimized (measured prototype) | Delta |
|---|---|---|---|
| `GET /admin/reports/shifts`, quarter, 150 workers / 9,900 shifts | **697 ms** | **323 ms** | **−54 %, 2.16×** |
| ↳ the timesheet SQL alone (same query, in isolation) | 297 ms | 44.5 ms | −85 %, 6.7× |
| ↳ same query at 604 workers / 39,600 shifts | 1,546 ms | 202 ms | −87 %, 7.7× |
| `GET /worker/me/stats` (6 connections) — interleaved protocol | 8.9 ms | 1.8 ms | −80 %, 5.0× |
| ↳ same endpoint, distribution protocol (see below — baselines differ with page-cache/WAL state) | 20.3 ms | 2.3 ms | −89 %, 9.0× |
| `GET /status/detail` | 5.4 ms | 1.9 ms | −65 %, 2.9× |
| `POST` punch pair (clock in + out) | 54.2 ms | 38.9 ms | −28 %, 1.4× |
| Idle API process, production config | **76.1 MB RSS** | already minimal | no action (see §4) |
| Report payload on the wire | 5.03 MB raw | 0.13 MB gzipped (39.5×) | already handled by GZipMiddleware |

### Latency distribution, throughput and vCPU share (warm, 48 samples per configuration)

| Configuration | p50 | p95 | p99 | Throughput | CPU per request | CPU as % of wall | Peak RSS delta |
|---|---|---|---|---|---|---|---|
| `reports/shifts` — baseline | 917 ms | 958 ms | 998 ms | **1.09 req/s** | 909 ms | 98.8 % | 7.2 MB |
| `reports/shifts` — index + Python | **399 ms** | **439 ms** | **488 ms** | **2.47 req/s** | **397 ms** | 98.1 % | 6.3 MB |
| `worker/me/stats` — baseline | 20.3 ms | 22.4 ms | 33.7 ms | 48.2 req/s | 20.7 ms | 100 % | ~0 |
| `worker/me/stats` — pooled | **2.3 ms** | **2.8 ms** | **2.9 ms** | **425 req/s** | **1.9 ms** | 81.4 % | ~0 |

Three things this says that medians alone did not:

1. **The report is pure CPU — 98.8 % of its wall time is process CPU on a single core.** Halving its
   latency therefore nearly doubles single-core throughput (**1.09 → 2.47 req/s, +126 %**), which is
   the actual capacity argument, not just a comfort argument.
2. **The tail is tight, not pathological:** p99 is within 9 % of p50 in every configuration, so these
   are uniform wins rather than best-case ones.
3. **On small endpoints, essentially all vCPU goes to connection lifecycle.** `worker/me/stats` falls
   from 20.7 ms to **1.9 ms of CPU per request (−91 %)** with identical output — six connections per
   request at ~1.8 ms each *is* the cost. That is what makes finding 3 a vCPU item, not only a
   latency item.

Reproduce with `python temp/perf_audit/dist.py --workers 150 --days 250 --requests 25 --blocks 2`.

### Definitive ROI verdict

| # | Change | Risk | Verdict |
|---|---|---|---|
| 1 | `CREATE INDEX idx_attendance_worker_action_id ON attendance_logs(worker_id, action, id)` | **Very low** — one additive migration; no query text changes; output byte-identical; +1 µs per punch insert | **Strongly Recommended** |
| 2 | Replace `strptime` with `fromisoformat`; memoize the shift window per site in `shift_timesheet_rows` | **Low** — two local edits in one module; 0 mismatches over 361 real timestamps + 13 edge cases; bodies byte-identical | **Strongly Recommended** |
| 3 | Reuse SQLite connections instead of one per `db()` block | **Medium** — thread affinity, test-harness DB rotation, transaction hygiene | **Recommended for console latency; Marginal for raw vCPU** |

Doing 1+2 costs one migration and ~40 lines of Python, and removes over half the latency of the
heaviest read path — on a 1 vCPU box that is also the request that blocks every other request for
its full duration. That is the whole ROI case, and it is decisive.

---

## 2. Tooling actually used (and why)

Nothing here was inferred from reading code alone; every number comes from a run.

| Purpose | Tool | Why this one |
|---|---|---|
| CPU attribution | stdlib `cProfile` / `pstats` | No external profiler was installed (`ruff`, `vulture`, `pyflakes`, `pylint`, `mypy` all absent from this environment — verified). `cProfile` attributes per-line/per-call cost inside app frames without installing anything. |
| Memory | `tracemalloc` + `psutil 7.2.2` | `resource` is unavailable on this platform (Windows). `tracemalloc` gives transient Python-heap peaks; `psutil` gives real RSS. |
| SQL timing | a `sqlite3.Cursor` subclass injected via `cursor_factory` | `sqlite3.Cursor` is an immutable C type, so its methods cannot be monkeypatched — but `cursor_factory` can be set per connection. This separates `execute()` (prepare) from fetch (stepping), which is what exposed the two-phase cost profile. |
| Query plans | `EXPLAIN QUERY PLAN` on the real statement text | Confirms the chosen access path rather than guessing from the SQL. |
| Load / end-to-end | `fastapi.testclient.TestClient` against the real app, plus `backend/tests/harness.py` | The harness is the project's own isolation layer: it clones the live DB into a temp directory, stubs the face engine/detector, and redirects every file tree. Real routes, real middleware, real SQLite — no mocks of the code under test. |
| Existing project tooling | `backend/tools/profile_endpoints.py`, `locustfile.py`, `capacity_test.py`, `face_process_memory.py` | Already present and used to cross-check; this audit does not duplicate them. |
| Static analysis | `ast` + whole-repo identifier scan | No linter was available; a purpose-built scan produced the dead-code and dependency inventory in §5. |

### Two measurement traps this audit hit, and corrected

1. **`tracemalloc` inflates latency 2.9×.** The first baseline for `reports/shifts` read 2,741 ms;
   with allocation tracking disabled the honest number is ~700 ms. Timing and memory are now
   separate passes.
2. **A scratch module named `audit.py` shadowed the application's `backend/audit.py`** on
   `sys.path`, so the app's audit trail raised `AttributeError` during measurement and the punch
   response became an error body. The harness is now named `perfaudit.py` and asserts at boot that
   `import audit` resolves to the application module.

Both are recorded because they are exactly the kind of error that produces a confident, wrong
performance report.

### Method

- **Dataset:** 150 workers × 250 calendar days (weekends excluded) ≈ **53,405 `attendance_logs`
  rows**, 9,900 quarter clock-outs, 14 sites, in the harness's throwaway clone. Scale tests
  multiply the *worker count* (302, 604) keeping each worker's real history — the dimension the
  hot query actually scales on.
- **Protocol:** each configuration is measured as a median of N warm iterations, and every A/B
  comparison is **interleaved and repeated** (A,C,B,D,A,C,B,D) so drift (WAL growth, page-cache
  warmth, GC state) cannot be mistaken for a speedup.
- **Gate:** every optimization is accepted only if the full response body is byte-identical to the
  baseline (volatile keys dropped), on the measured period *and* on a different date slice.
- **Platform caveat:** measured on Windows/Python 3.12.10, SQLite 3.49.1. Absolute millisecond
  values differ on the Linux container; ratios and query plans do not.

---

## 3. Verified optimization opportunities (ranked by impact)

### #1 — The timesheet query has no index for its correlated subquery (Strongly Recommended)

**Location:** [`backend/reports.py:225`](../backend/reports.py#L225) `SHIFT_TIMESHEET_SQL`, consumed
by `shift_timesheet_rows` ([`backend/reports.py:457`](../backend/reports.py#L457)) and reached by
`/api/v1/admin/reports/shifts`, `/api/v1/admin/reports/export` (CSV) and the print sheet.

**Baseline plan** (150 workers, quarter):

```
SCAN l                                     <-- full table scan: 53,405 rows
  SEARCH u USING INDEX sqlite_autoindex_users_1 (id=?)
  SEARCH m USING INDEX sqlite_autoindex_users_1 (id=?)
  SEARCH ci USING INTEGER PRIMARY KEY (rowid=?)
CORRELATED SCALAR SUBQUERY 1
  SEARCH prev USING INDEX idx_attendance_worker_ts (worker_id=?)
  SEARCH notes USING AUTOMATIC COVERING INDEX (worker_id=?)
USE TEMP B-TREE FOR GROUP BY
USE TEMP B-TREE FOR ORDER BY
```

**Root cause.** Two independent access-path failures in one statement:

1. The outer filter is `WHERE l.action = 'Clock Out' AND l.timestamp >= ? AND l.timestamp < ?`. The
   only index that could serve it is `idx_attendance_log_status(status_code, timestamp)`, whose
   leading column is `status_code`, not `action` — so the planner falls back to `SCAN l`.
2. The pairing subquery
   `LEFT JOIN attendance_logs ci ON ci.id = (SELECT MAX(prev.id) FROM attendance_logs prev WHERE prev.worker_id = l.worker_id AND prev.action = 'Clock In' AND prev.id < l.id)`
   is evaluated **once per output row**. The only usable index is
   `idx_attendance_worker_ts(worker_id, timestamp)`: it gives the `worker_id = ?` equality, but
   there is no index on `action` or `id` behind it, so SQLite walks **backward from `l.id` over
   every other worker's rows** until it meets one belonging to this worker with
   `action = 'Clock In'`. The cost per output row therefore grows with the number of workers, not
   with the size of the period.

**Proof — scaling (rows scale linearly, cost does not):**

| Workers | `attendance_logs` rows | Output rows | Baseline | With index | Speedup |
|---|---|---|---|---|---|
| 151 | 53,405 | 9,900 | 288 ms | 43.1 ms | 6.7× |
| 302 | 106,810 | 19,800 | 711 ms | 98.3 ms | 7.2× |
| 604 | 213,620 | 39,600 | 1,546 ms | 201.5 ms | 7.7× |

Baseline grows ≈**2.5× per doubling** (superlinear — the backward walk gets longer); the indexed
form grows ≈2.1×, which is the output size itself. This is the difference between a report that is
slow today and one that becomes unusable as the company hires.

**Proposed diff (schema only — no query change):**

```sql
-- in the migrations chain, additive and idempotent
CREATE INDEX IF NOT EXISTS idx_attendance_worker_action_id
    ON attendance_logs(worker_id, action, id);
```

**After:** the subquery becomes a single seek —

```
CORRELATED SCALAR SUBQUERY 1
  SEARCH prev USING COVERING INDEX idx_attendance_worker_action_id (worker_id=? AND action=? AND id<?)
```

**Cost of the index, measured:**

- Build: **38 ms** at 53k rows, 177 ms at 214k rows — a one-time migration cost.
- Write path: punch `INSERT`+`COMMIT` **0.702 ms → 0.708 ms** (a +1 µs difference, within run-to-run
  noise).
- Coverage: it is not a covering index for the outer scan (that still reads the table), which is
  why the residual is 44 ms rather than ~10 ms.

**A second index was tested and rejected.** `idx(action, timestamp)` does give the outer query a
real range `SEARCH` and removes the `ORDER BY` temp b-tree, and it wins slightly at 150 workers
(40.0 ms vs 44.5 ms) — but it **loses badly at scale**: 204 ms vs 98 ms at 302 workers and 396 ms vs
202 ms at 604 workers, because scanning a low-cardinality index in `action, timestamp` order turns
into random table access. Do not add it.

**Endpoint-level effect:** `/admin/reports/shifts` 697 ms → **440–477 ms**, response body
byte-identical.

*Protocol note:* the interleaved `proto` subcommand measures the report **warm** (~700 ms
baseline). The `sweep` subcommand measures it as the first heavy read of the session and reads
**≈1.0–1.2 s**, which is the same effect a real deployment sees on the first report after a
restart. Both protocols are reported with their own numbers; the ratio is what transfers.

---

### #2 — `shift_timesheet_rows` spends 42 µs per row in Python (Strongly Recommended)

Even with the query fixed, the endpoint is dominated by Python: at 9,900 rows the profiled total
was 948 ms, of which `shift_timesheet_rows` was 897 ms and only ~300 ms was SQLite.

**Hot spots (cProfile, 9,900 rows):**

| Function | Cumulative | Calls | Per call |
|---|---|---|---|
| `reports._arrival_fields` | 459 ms | 9,900 | **46 µs** |
| ↳ `reports._parse_stored_timestamp` | 172 ms | 9,900 | 17 µs |
| ↳ `shift_windows.effective_window` | 153 ms | 9,900 | 15 µs |
| &nbsp;&nbsp;&nbsp;↳ `arrival` / `_pick` / `normalise_hhmm` / `parse_hhmm` / `start_minutes` / `end_minutes` | 74 / 74 / 43 / 41 / 26 / 22 ms | 9,900–39,600 | — |

**Root causes, both mechanical:**

1. **`datetime.strptime` in the hot path.** `_parse_stored_timestamp`
   ([`backend/reports.py:308`](../backend/reports.py#L308)) walks three formats with `strptime` for
   every row. `strptime` is ~14× slower than the ISO parser.
2. **The shift window is re-resolved for every row.** `effective_window`
   ([`backend/shift_windows.py:453`](../backend/shift_windows.py#L453)) is a pure function of the
   site row and the global rules — and both are fixed for the whole report. It is being computed
   9,900 times to produce **12 distinct answers** (one per site), re-parsing `HH:MM` strings and
   re-deriving the `ZoneInfo` each time.

**Proof — the primitive, measured:**

| Implementation | Per call | Speedup |
|---|---|---|
| `datetime.strptime` (current) | 5.483 µs | — |
| `datetime.fromisoformat` | **0.380 µs** | **14.4×** |

**Equivalence (not assumed — checked):** across all **361 distinct stored timestamps** in the
dataset plus 13 edge cases (empty, whitespace, garbage, date-only, `T` separator, invalid field
values, fractional seconds, a real `datetime`, an integer), the replacement returns **0
mismatches**. The one divergence found in a first attempt — a bare `YYYY-MM-DD` parsed as midnight
where the original returns `None` — is closed by the `len(text) < 16` guard, which reproduces the
original's rejection of anything shorter than `YYYY-MM-DD HH:MM`.

**Proposed diff (sketch; final shape is the implementer's):**

```python
# reports.py — same contract, 14x faster
def _parse_stored_timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if len(text) < 16:              # a bare date was, and stays, unreadable
        return None
    for width in (19, 16):
        try:
            return datetime.fromisoformat(text[:width])
        except ValueError:
            continue
    return None

# reports.py — resolve the window once per site, not once per row
def shift_timesheet_rows(...):
    ...
    windows = {name: shift_windows.effective_window(row, rules) for name, row in sites.items()}
    for record in records:
        window = windows.get(str(record["site_name"] or "")) or shift_windows.effective_window(None, rules)
        ...  # pass it into _arrival_fields instead of re-deriving it
```

**Endpoint-level effect, 2×2 matrix (interleaved, repeated, payload-gated):**

| Configuration | Best of 2 runs | vs baseline | Body identical |
|---|---|---|---|
| A — current | 697.5 ms | 1.00× | (reference) |
| B — + index only | 439.8 ms | 1.59× | ✅ |
| C — + Python only | 559.3 ms | 1.25× | ✅ |
| **D — + both** | **322.9 ms** | **2.16×** | ✅ |

Every configuration's response body was byte-identical to the baseline, on the measured quarter
and on a different single-day slice — so the speedup cannot be hiding a behaviour change.

---

### #3 — One SQLite connection per `db()` block costs ~1.8 ms (Recommended / conditional)

**Location:** [`backend/database.py:243`](../backend/database.py#L243) `connect()` and
[`backend/database.py:272`](../backend/database.py#L272) `db()` — every data access opens, pragmas,
uses and closes its own connection.

**Measured per-connection tax:**

| Phase | Cost |
|---|---|
| `database.connect()` (incl. `os.path.realpath` 0.124 ms) | 0.397 ms |
| first statement on a *fresh* connection | **0.991 ms** |
| second statement on the same connection | 0.018 ms |
| `close()` | 0.414 ms |
| **total per `with db()` block** | **≈1.8 ms** |

**How often it is paid** (statements / connections captured per request):

| Endpoint | Statements | Connections |
|---|---|---|
| punch (clock in + clock out pair) | 36 | 12 |
| `GET /worker/me/stats` | 12 | 6 |
| `GET /admin/audit_log` | 8 | 4 |
| `GET /admin/users` | 7 | 3 |
| most reads | 4–8 | 2–3 |

For small endpoints the connection lifecycle *is* essentially the whole request.

**Prototype** (per-thread free list keyed by path/read-only/isolation, rollback before returning,
capped at 8 per key) — all read-endpoint bodies byte-identical:

| Endpoint | Control | Pooled | Speedup |
|---|---|---|---|
| `worker/me/stats` | 8.9 ms | **1.8 ms** | **5.0×** |
| `status/detail` | 5.4 ms | 1.9 ms | 2.9× |
| `reports/pending` | 4.2 ms | 2.1 ms | 2.1× |
| `admin/logs 200` | 19.4 ms | 16.1 ms | 1.21× |
| `admin/users roster` | 25.4 ms | 21.9 ms | 1.16× |
| punch pair | 54.2 ms | 38.9 ms | 1.39× |

**Why the verdict is conditional — three real hazards, not hypotheticals:**

1. **Thread affinity.** FastAPI runs sync endpoints in a threadpool; a connection made on one
   thread must not be used on another. A thread-local pool, or `check_same_thread=False` with
   serialized access, is required.
2. **The test harness rotates the database by design.** `database.resolve_path` resolves the real
   path on every connect precisely so that the tests' junction/symlink rotation gives each test its
   own file (`test_database_rotation_under_concurrency.py` depends on it). A pool must invalidate on
   rotation or it will silently serve the previous generation.
3. **Transaction hygiene.** A reader must never be returned holding an open read transaction (the
   prototype rolls back on return); `immediate()`'s `BEGIN IMMEDIATE` blocks commit/roll back on the
   same connection and must not be pooled naively.

**Punch caveat, stated plainly:** the punch's body-equality gate could **not** be applied. The
liveness model is absent in this environment and the punch response's `liveness` detail field
differs between two consecutive punches **with no pool installed at all** — an environment
artifact, not a pool defect. The punch timing above is directional; the five read endpoints carry
the equality evidence.

**Aggregate vCPU note.** Per request the saving is ~1.8 ms × blocks. On the *small* endpoints this
is not a marginal slice of the request, it is almost all of it: `worker/me/stats` measures 20.7 ms of
CPU per request before and **1.9 ms after (−91 %)**, and its p50 falls 20.3 → 2.3 ms. Across the
console's small reads, connection lifecycle is the dominant vCPU line item; on the heavy report it is
noise. (An earlier interleaved run of the same prototype measured 5.0× rather than 9.0× on this
endpoint — the baseline varies with WAL and page-cache state, so treat the multiplier as a range.)

---

### #4 — Payload size: measured, already handled (no action)

- `GET /admin/reports/shifts` returns **5.03 MB** of JSON (9,900 rows × ~530 B); gzipped it is
  **0.13 MB (39.5×)**, and `GZipMiddleware(minimum_size=1024)` already applies.
- Frontend bundles: 1.92 MB raw → 0.49 MB gzipped (3.9×) overall; `admin_modules.js` alone is
  801 KB raw / 202 KB gzipped.
- The `ar`/`hi`/`ur` translation bundles (357 KB raw / 83 KB gz) are **loaded lazily** via
  `document.createElement('script')`, not shipped eagerly — verified in `frontend/i18n.js:476`.

The remaining lever, if the 5.03 MB body matters, is server-side pagination or column projection on
the report — a product decision, not a defect, and out of scope here.

---

## 4. Verified *good* — hypotheses this audit disproved

Stated explicitly, because these were live concerns and acted-on conclusions need to be revisable.

1. **"cv2 is loaded into the API process (~25 MB)."** `docs/IDLE_RSS_REDUCTION_ANALYSIS.md` ranks
   this #1 with the trigger "still to be confirmed". Measured with the Dockerfile's own env:
   - `FACE_ENGINE_PROCESS=1` (production): **76.1 MB RSS**, and `cv2` is **not in `sys.modules`**.
   - default (no child): **268.3 MB RSS**, `cv2` **is** loaded.
   So the deployment config already keeps cv2 (and the ONNX models) out of the parent —
   `face_detector._models_run_elsewhere()` is the guard. **No action.**
2. **The correlated subquery is not the *only* problem in the report** — at 150 workers it is ~300 ms
   of ~700 ms; the per-row Python is the larger half. Fixing only the query leaves >50 % on the
   table, which is why #1 and #2 are proposed together with a measured combined result.
3. **Gzip is not missing**, and the i18n bundles are not eagerly loaded — both verified rather than
   assumed.
4. **The `attendance_logs` indexes that exist are not idle**: `idx_attendance_worker_ts` is chosen by
   6 of the 48 captured statements and `idx_attendance_log_status` by 1. Neither should be dropped.

---

## 5. Dead & redundant code inventory

Nothing below was removed. Each item carries its blast radius.

### 5.1 Referenced nowhere in the repository — 15 module-level definitions

Verified by whole-repo `grep -w` (excluding `.git`, `venv`, `node_modules`), where each name returns
exactly **one** hit: its own definition. Route handlers were excluded (their decorator registers
them).

| Name | File | Name | File |
|---|---|---|---|
| `_consent_conn` | `backend/corpus.py` | `page_policy` | `backend/sites.py` |
| `allowed_types` | `backend/uploads.py` | `register_reference` | `backend/enrollment.py` |
| `anti_enumeration_response` | `backend/developer.py` | `reload_policy` | `backend/netguard.py` |
| `camera_of` | `backend/corpus.py` | `require_two_tier` | `backend/calibration.py` |
| `load_registry` | `backend/calibration.py` | `time_embedder` | `backend/facenet_ort.py` |
| `log_failure` | `backend/face_engine.py` | `utc_now_str` | `backend/clock.py` |
| `map_sources` | `backend/geofence.py` | `write_registry` | `backend/calibration.py` |
| `nearest_contract` | `backend/face_align.py` | | |

**Blast radius: low, but not zero.** Module-level only (class methods were not scanned); dynamic
dispatch (`getattr`, string registries) was ruled out by the whole-repo scan, but a name could still
be part of an intended operator/debug API. Remove one at a time, with the suite green between
removals.

### 5.2 Indexes never chosen by any plan — 32 of 40

Method: 48 distinct statements captured from a 27-endpoint sweep plus a punch, each planned with
`EXPLAIN QUERY PLAN` on the migrated schema (parameters bound, comments preserved).

**Chosen (11):** `sqlite_autoindex_users_1` (15 statements), `idx_attendance_worker_ts` (6),
`idx_worker_notes_status` (3), `sqlite_autoindex_active_sessions_1` (3), `idx_users_status` (2),
`idx_worker_notifications_inbox` (2), `idx_overtime_authorisations_live` (2),
`idx_attendance_log_status` (1), `idx_notifications_unread` (1), `idx_users_role` (1),
`sqlite_autoindex_site_categories_1` (1).

**Never chosen (32):** `idx_anchors_device`, `idx_attendance_punches_user`, `idx_audit_actor`,
`idx_audit_created`, `idx_corpus_consents_current`, `idx_corpus_consents_worker`,
`idx_developer_alerts_created`, `idx_developer_alerts_dedupe`, `idx_developer_alerts_unread`,
`idx_enroll_items_job`, `idx_enroll_jobs_status`, `idx_invites_kind`, `idx_invites_worker`,
`idx_note_messages_note`, `idx_overtime_authorisations_shift`, `idx_punch_nonce`,
`idx_punch_photo_pending`, `idx_punch_status`, `idx_punch_worker`, `idx_quick_link_uses_link`,
`idx_quick_link_uses_log`, `idx_quick_links_worker`, `idx_refused_punches_created`,
`idx_refused_punches_worker`, `idx_registration_requests_pending_photo`,
`idx_registration_requests_status`, `idx_retention_runs_started`, `idx_users_biometric_id`,
`idx_users_moallem_id`, `idx_worker_notes_worker`, `idx_worker_notifications_undelivered`,
`idx_worker_push_subscriptions_worker`.

**Blast radius: unverified, and that is the point.** "Never chosen in this sweep" is not "dead": the
sweep did not exercise every route (registrations, quick links, enrollment jobs, corpus consent,
push subscriptions and retention were not driven). Each of the 32 needs a targeted plan check on its
own route before removal. The candidates worth checking first are the ones on tables the sweep *did*
query and the planner still ignored — `idx_users_moallem_id`, `idx_users_biometric_id` and
`idx_worker_notes_worker`. Removing an unused index is a pure win (smaller file, cheaper writes);
removing a *used* one is a regression, so this is deliberately gated on more evidence.

### 5.3 Unused pinned dependencies — build/image only, no runtime RSS

The application imports **20** third-party modules: `PIL`, `cryptography`, `cv2`, `dotenv`,
`fastapi`, `jwt`, `numpy`, `onnxruntime`, `openpyxl`, `passlib`, `prometheus_client`, `py_vapid`,
`pydantic`, `pywebpush`, `qrcode`, `requests`, `slowapi`, `starlette`, `uvicorn`, and its own
`tools`.

`requirements.txt` is deliberately a **verbatim freeze including transitive pins**, and
`backend/tests/test_deployment_manifest.py` enforces that policy — so this is an observation, not a
recommendation to hand-prune. Heavy pins that no application module imports, and which therefore
cost image size and build time but **no runtime RAM**: `pandas`, `scikit-learn`, `h5py`, `grpcio`,
`absl-py`, `mtcnn`, `retina-face`, `beautifulsoup4`, `lz4`, `rich`, `tqdm`, `sympy`, `fire`, `gdown`,
`ml_dtypes`. They will drop out on the next full re-freeze (as `deepface`/`tensorflow`/`scipy`
already did).

**One thing to confirm, not a finding:** `openpyxl` and `qrcode` are imported by application code
(`backend/reports.py`, `backend/enrollment.py`); make sure both are pinned in the runtime manifest
by the same policy the manifest test enforces.

---

## 6. Recommended sequencing

1. **Migration:** add `idx_attendance_worker_action_id`. Verify with
   `python temp/perf_audit/perfaudit.py index` (expect ~44 ms vs ~297 ms at 150 workers) and by
   re-reading `EXPLAIN QUERY PLAN`.
2. **`reports.py`:** the parser and the per-site window cache. Verify with
   `python temp/perf_audit/perfaudit.py proto` — **the body-equality gate is the acceptance test**;
   it must print `body_equal=True` for every configuration.
3. **Regression:** the existing suite, with attention to
   `test_phase02_offline_enrollment_reports.py`, `test_admin_dashboard.py` and the print-sheet /
   month-sheet frontend tests, since they assert the report's exact output.
4. **Only then, optionally:** the connection pool, behind its own design for threading, DB rotation
   and transaction hygiene — with `test_database_rotation_under_concurrency.py` as the gate.

**Explicitly not recommended:** adding `idx(action, timestamp)` (measured slower at scale), or
removing any of the 32 unused-looking indexes without a targeted plan check on its own route.

---

## 7. Limitations of this audit

- Latency figures are from one host (Windows, Python 3.12.10, SQLite 3.49.1) with a synthetic
  dataset; the container is Linux with jemalloc. Ratios and query plans transfer, absolute
  milliseconds do not.
- The face engine and detector were stubbed by the harness, so the punch numbers cover the
  non-inference path (auth, geofence, rate limit, photo handling, SQLite, middleware). Inference cost
  is covered by the project's own face benchmarks and `face_process_memory.py`.
- The endpoint sweep is 27 routes, not all ~140; §5.2's index inventory is scoped to what it drove.
- Dead-code detection is static and module-level only; it cannot see dynamic dispatch.
- The latency distribution was measured **sequentially through the in-process ASGI client**, on one
  core, one process — the deployment's own shape, but not a concurrent load test. It therefore reports
  service time and CPU per request, not queueing behaviour under simultaneous arrivals. That follow-up
  has since been done over real HTTP, before and after the fix: see
  [LOAD_TEST_2026-10-05.md](LOAD_TEST_2026-10-05.md) for p50/p95/p99, throughput, peak RSS and vCPU at
  1, 4 and 16 simultaneous clients. Queueing under *punch* concurrency remains the job of the existing
  `backend/tools/locustfile.py` / `capacity_test.py`.
- Warm and cold numbers differ materially for the heaviest read: the same endpoint is ~700 ms warm
  (interleaved protocol) and ~1.0–1.2 s as the session's first report (cold page cache and WAL).
  Anyone re-running the harness should compare like with like.
- One incident to note: the `temp/` directory (gitignored scratch space) was wiped mid-session by
  something outside these tools, so the earlier scratch scripts were rebuilt as
  `temp/perf_audit/perfaudit.py`. All quoted numbers were reproduced with the rebuilt harness after
  the module-shadowing fix described in §2.

---

## 8. Implementation status (added after authorization, 2026-10-05)

Findings **#1** (the `attendance_logs` pairing index) and **#2** (the report's per-row Python
work) are implemented and verified. The rest of this report is still a proposal, and nothing is
committed - the change is in the working tree for review.

### What changed

| File | Change |
|---|---|
| `backend/migrations.py` | New `migration_34_attendance_timesheet_pairing_index`: `CREATE INDEX IF NOT EXISTS idx_attendance_worker_action_id ON attendance_logs(worker_id, action, id)`, idempotent and additive; `SCHEMA_VERSION` 33 -> 34. |
| `backend/reports.py` | `_parse_stored_timestamp` uses `datetime.fromisoformat` behind a length guard (>= 16 chars, so a bare date still returns `None`) on a fast path that only answers what the three `strptime` patterns would; those patterns are retained verbatim as the fallback for shapes the fast path refuses (see the refinement note below); `_arrival_fields` takes the already-resolved `shift_windows.Window` instead of resolving one; `shift_timesheet_rows` resolves one window per site (plus one for shifts whose site row is gone) and looks it up per row. |
| `backend/tests/test_migration_drift.py` | The canary asserting the newest migration's body is visible to its scanners now also accepts an index-only migration (`CREATE INDEX` in the body). A migration with neither a column nor an index still fails, which is the case the canary exists to catch. |
| `backend/tests/test_site_shift_windows.py` | The schema-version checklist assertion is 33 -> 34, with migration 34 described in its comment - the maintenance step that test documents. No other assertion in either file was touched. |

My edits are those four files. `backend/tests/test_admin_shift_visibility.py` and the frontend
files were already modified when the audit began and are not mine.

### Acceptance evidence (gate: `temp/perf_audit/verify_fixes.py`, run against the shipped code)

Nine checks, all passed:

1. the migration created `idx_attendance_worker_action_id`, and
2. migration 34 is recorded as applied in `schema_migrations`;
3. `EXPLAIN QUERY PLAN` for the pairing subquery is now `SEARCH prev USING COVERING INDEX
   idx_attendance_worker_action_id (worker_id=? AND action=? AND id<?)`, from `SEARCH prev USING
   INDEX idx_attendance_worker_ts (worker_id=?)`;
4. the outer scan also reads through the new index rather than the table;
5. the shipped parser is equivalent to the original three-pattern implementation over every
   distinct stored timestamp in the dataset (361) plus 13 edge cases (`None`, empty, whitespace,
   a bare date, garbage, an impossible date, fractional seconds, a `datetime`, an int) - zero
   mismatches;
6. the endpoint returns 200;
7. the report body is **byte-identical** to the same code with the *original* parser put back,
   which isolates the diff's only semantic change;
8. `effective_window` is called once per site (<= sites + 2, measured ~15) for a request that
   covers ~9,900 shifts, against 9,900 calls before;
9. the body from the cached-window run is identical too.

The timing figure itself remains §3's benchmark, which is the reproducible artifact:
`perfaudit.py index` measures the timesheet SQL at 44.5 ms median against 297 ms before at the
same scale. What this gate adds is that the plan behind that number exists on the *migrated*
schema (checks 1-4), not just on a bench clone with the index added by hand.

### Suite results (all run against the patched tree)

| Batch | Result |
|---|---|
| Schema, migration and drift tests (incl. `test_migration_drift.py`, `test_site_shift_windows.py`) | 171 passed |
| Report and print-sheet tests (incl. `test_phase02_offline_enrollment_reports.py`, `test_frontend_print_sheet.py`, `test_frontend_shifts_filter.py`, `test_frontend_worker_month_sheet.py`, `test_frontend_admin_handset.py`) | 202 passed |
| Wider report-surface breadth | 542 passed, 2 skipped |

**915 passed, 0 failed.** The 2 skips are a pre-existing filesystem-symlink limitation,
unrelated to this change. The acceptance criterion was the byte-identical body above, not the
green suite: no application behaviour was observed to change.

### 8.1 Follow-up verification, and a parser refinement it forced

A requested verification pass widened the parser's equivalence corpus and found a real
defect in the first version of it. That version took `fromisoformat` whenever the slice
parsed, which read three shapes the original refused and refused some it accepted:

* `2026-07-01T08:00` (T, no seconds) parsed where `strptime` had returned `None`;
* `2026-07-01 08:00Z` and offset forms parsed to an **aware** datetime, where the old code
always answered naive - and an aware value compared against a naive window boundary is a
`TypeError` inside the report;
* unpadded components (`2026-07-01 8:00`, `08:0`) that `strptime` accepted were refused.

The shipped parser is now a guarded fast path (`fromisoformat` only where the original
patterns would also answer, rejecting any tz-aware result) with the original loop kept
verbatim as the fallback, so the set of readable values and their answers are the pre-change
ones. Measured after the repair:

| Command | Result |
|---|---|
| `python temp/perf_audit/parser_fuzz.py` | 1,524 inputs (all 361 stored values + generated shapes + junk): **0 regressions**. The only difference left is 36 inputs **shorter than 16 characters**, which the length guard is specified to refuse - and every stored value is 19 characters, so no real row is affected. |
| `python temp/perf_audit/perfaudit.py proto` | all eight arm-runs `body_equal=True`, `body_equal_all=True`, exit 0; baseline 961 ms -> +index 608 -> +python 773 -> +both **425 ms (2.26x)**, i.e. no speed lost to the guards. |
| `python -m pytest -q backend/tests/test_phase02_offline_enrollment_reports.py backend/tests/test_admin_dashboard.py` | **71 passed**, exit 0. |
| `python temp/perf_audit/verify_fixes.py` | all nine acceptance checks pass on the refined parser: byte-identical body, covering-index seek, and `effective_window` called 15 times for ~9,900 shifts. |

The `proto` subcommand also had to be repaired to run at all: it drove its 2x2 by patching
`reports._arrival_fields` with the pre-fix signature `(clock_in, site_name, sites, rules)`, so
once the shipped code began passing a resolved `Window` every request raised `TypeError`. It
now swaps in the real pre-fix `shift_timesheet_rows` (the same module `serve_variant.py` uses)
and exits non-zero if any arm's body differs.

*Note:* a `pytest` run regenerates transient `times.db.pre-reset-*.bak-{shm,wal}` sidecars at
the repository root (the developer-backup tests open the tracked `.bak` files); they were
removed again after this session's runs and are untracked noise, not a code change.
