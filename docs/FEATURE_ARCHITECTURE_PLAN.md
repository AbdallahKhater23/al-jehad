# Feature Architecture & Integration Plan (Phase 02)

Construction workforce attendance system — FastAPI + SQLite + DeepFace, single-origin
frontend served by the same app.

Status legend: **DONE** (shipped, covers the requirement) · **PARTIAL** (foundation in
place, behaviour missing) · **MISSING**.

Baseline audit of this revision (before the Phase 02 edits):

| Area | Status | Evidence |
| --- | --- | --- |
| Startup gate / readiness | DONE | `readiness.py` (~30 checks, fatal vs repairable), `schema_guard.py` compiles expected schema from baseline + migrations |
| AuthN/AuthZ | DONE | JWT with pinned algorithm, role re-read per request, `require_role` guards on all 16 admin routes |
| Append-only audit log | DONE | `audit_log` + `UPDATE`/`DELETE` triggers that `RAISE(ABORT)` |
| Twilio/WhatsApp removal | DONE | no credentials, no network path; `send_whatsapp_alert()` is a deprecated no-op |
| `admin_notifications` + endpoints | DONE | table with `dedupe_key UNIQUE`, `/api/v1/admin/notifications`, `.../{id}/read` |
| Shift rules (04:00–06:30) | DONE | `shift_rules` table, `/admin/shift_rules` GET/POST, late arrival flagged + notified |
| ~~11 h hard cutoff as safety net~~ | **REMOVED** | it invented hours: a genuine 13 h shift was force-closed at 11 h and paid as 11, and the missing 2 h were invisible. `enforce_11h_cutoff()` is gone, the offline path no longer flattens hours to the cutoff, and nothing closes a forgotten shift on a timer - the overtime watcher alerts and a human closes it |
| Post-shift >8.1 h approval routing | DONE | clock-out sets `Pending Overtime Approval` / `pending_overtime`, requires `/admin/approve_review` |
| **Overtime alert *at the crossing*** | **MISSING** | no watcher; a shift that never clocks out never alerts |
| **MiniFASNet passive liveness** | **MISSING** | columns `liveness_class`/`liveness_score` are written as `NULL`; no ONNX runtime |
| **Self-service enrollment** | **MISSING** | only admin `POST /admin/enroll` |
| **Bulk CSV + ZIP onboarding** | **MISSING** | — |
| **Offline punch sync** | **PARTIAL** | `worker_devices` + `punch_queue` tables exist (migration 3); no endpoints, no signature verification, no materialization |
| **Shift / attendance reporting** | **MISSING** | raw `/admin/logs` only; no aggregates, no export |
| **`users.token_version`** | **BUG** | used by `security.py` and 2 endpoints but created by no migration → login returns 500 on the live database |
| Tests for the above | PARTIAL | 58 security tests exist (52 pass, 6 fail on the `token_version` bug) |

---

## 0. Non-negotiable integration rules

1. **Additive only.** Every schema change is `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD
   COLUMN`, or `INSERT OR IGNORE`. No `DROP`, no table rebuild, no data rewrite. Existing
   `users`, `attendance_logs`, `active_sessions`, `construction_sites` keep their shape and rows.
2. **One writer path.** New features open connections through `database.db(write=True)` so
   `busy_timeout` and WAL semantics stay identical to the existing code.
3. **New endpoints never move old ones.** The frontend's existing calls
   (`/api/v1/attendance/verify`, `/admin/enroll`, `/admin/logs`, …) keep their paths, payloads and
   response shapes. New capability is added beside them.

   *One deliberate exception, made later and on purpose:* `/api/v1/attendance/verify` no longer
   wants the `password` form field. It asked for the worker's password a second time, seconds
   after they signed in with it, on a phone in the sun — and a mistyped one answered 401, which
   signed the worker out mid-punch and lost them the tap. The session is the credential now
   (signature plus `token_version`, so a rotated password or a deactivated account is refused
   before anything is recorded), and the punch is still gated by the geofence, the liveness check
   and the face match. An older client that still posts the field is unaffected: the extra field
   is ignored, nothing else about the call changed.
4. **Fail closed on biometrics, fail open on notifications.** A notification write failure must
   never fail an attendance request (already the rule in `notifications.notify()`); a liveness or
   signature check failure must never be silently ignored in `enforce` mode.
5. **Every new module is optional-dependency safe.** `onnxruntime`, `qrcode` and `openpyxl` are
   *not* installed in `backend/venv`. They are imported lazily inside the function that needs them
   and degrade to a documented, non-crashing behaviour.

---

## 1. Mandatory pre-work backup protocol

Already implemented in `backend/tools/backup.py` (**DONE**) and used for this build:

```bash
python backend/tools/backup.py --prefix pre_feature_build_phase02
# -> backups/pre_feature_build_phase02_YYYYMMDD_HHMMSS/
```

Protocol (what the routine does, and why each part is necessary):

| Step | Mechanism | Why it is not optional |
| --- | --- | --- |
| Copy source | `shutil.copy2` of `main.py`, `serve.py`, config/db/security/notifications, `frontend/`, `backend/tests/`, `backend/tools/`, `README.md`, `.gitignore`, `.env` | gives a runnable tree, not just a database |
| Snapshot `times.db` | `VACUUM INTO` (atomic, self-consistent) | a `cp` of a live WAL database can capture a torn page state |
| Stale copy | also copies `backend/times.db` when present | documents the historical "two databases" accident |
| Biometrics | `local_references/`, `worker_photos/`, `backend/certs/` | the *only* copy of every enrolled face template |
| Environment | `pip freeze` for both interpreters, `python -V`, `git rev-parse HEAD`, timestamp | reproduces the build that produced the snapshot |
| Hashes | `MANIFEST.sha256` over every file, relative paths | `sha256sum -c MANIFEST.sha256` works in place |
| Proof | `VERIFY.json` + `PRAGMA integrity_check`, table row counts, `db_sha256` | a checksum alone cannot say whether the bytes are a *usable* database |
| Verification | `verify_snapshot()` re-hashes everything and re-runs `integrity_check` | exit code 1 blocks the change |

Gate: **no schema or code change proceeds unless `verify_snapshot` returns PASS.** The startup gate
(`readiness.py`) additionally checks that a verified snapshot is younger than
`BACKUP_MAX_AGE_HOURS` (default 24 h), which is what makes this protocol enforceable rather than
optional.

---

## 2. Rapid worker enrollment pipeline

### Shared data lifecycle

```
invite created ──► token issued (hash stored) ──► worker captures photo ──► liveness gate
                                                                              │
                users.enrolled_at / template_version ◄── local_references/<id>.json
                                                          worker_photos/<id>.jpg
                                                              + audit_log + notification
```

### 2a. Self-service mobile enrollment (tokenized URL / QR)

New table `enrollment_invites`:

| Column | Purpose |
| --- | --- |
| `token_hash` UNIQUE | `sha256` of a `secrets.token_urlsafe(32)`. The plaintext exists only in the URL/QR; a database leak cannot be replayed |
| `worker_id` | who the invite is for (must already exist in `users`) |
| `expires_at`, `max_uses`, `uses` | single-use and time-boxed by default (72 h, 1 use) |
| `revoked_at`, `completed_at`, `last_used_at`, `last_used_ip` | lifecycle + forensic trail |
| `created_by`, `note` | accountability |

Endpoints:

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/v1/admin/enrollment/invites` | admin | returns the URL once + optional QR data-URI |
| GET | `/api/v1/admin/enrollment/invites` | admin | list, filter `active=1` |
| POST | `/api/v1/admin/enrollment/invites/{id}/revoke` | admin | audited |
| GET | `/api/v1/enroll/{token}` | public, rate-limited | masked worker name, expiry, remaining uses |
| POST | `/api/v1/enroll/{token}` | public, rate-limited | `multipart/form-data`: `photo` (+ optional `phone`, `email`) |

The token page is served at `GET /enroll/{token}` from a standalone `frontend/enroll.html`
(camera capture + `enctype=multipart` POST), because a public worker must not have to log in.

Enrollment is the one place where liveness runs in **`enforce`** mode by design: a face template is
permanent, so a printed photo must never become the reference.

### 2b. Bulk batch onboarding (CSV + ZIP)

`POST /api/v1/admin/enrollment/bulk` — `multipart/form-data`:
`roster` (CSV), `photos` (ZIP), `dry_run` (bool), optional `default_role`.

CSV columns: `user_id,name,role,email,phone,photo` (only `user_id` and `name` required; `photo`
defaults to `<user_id>.jpg`). The response is immediate (`202`-style) with a `job_id`; the heavy
work runs via `BackgroundTasks`.

New tables `enrollment_jobs` (counters, status) and `enrollment_job_items` (per-row status and
error code) — a mutable job record belongs in a table, not in a background task's memory, so
progress survives a restart and a half-finished batch is resumable with
`python -m enrollment --job N`.

Pre-flight (before any embedding work): ZIP member count/size limits, CSV row count limit,
duplicate `user_id` detection, unknown `user_id` detection, missing photo members. A row that fails
one of the cheap checks never reaches DeepFace.

### Trade-offs, and the choice

| | Self-service mobile | Bulk CSV + ZIP |
| --- | --- | --- |
| Data quality | **best** — the worker is present, one face per frame, camera is the same one used at clock-in | weakest — arbitrary crops, group photos, wrong faces, HEIC/rotated files |
| Liveness | enforceable (live camera, single frame) | only advisory (you cannot prove a file was captured live) |
| Operator cost | one invite per worker, no upload wrangling | lowest for an existing roster; needs the ZIP prepared correctly |
| Failure mode | needs the worker to have a phone with signal | one bad row must not abort the batch (per-row status, never all-or-nothing) |
| Abuse surface | public endpoint → rate-limited, single-use, expiring token, hashed at rest | admin-only, audited |

**Selected design: self-service first, bulk as a supervised import.** Self-service is the reliable
one because the biometric quality is the bottleneck of the whole system — a bad reference template
produces false rejections forever and costs far more than the two minutes it saves. Bulk is kept
for migrating an existing roster, runs *advisory* liveness, and every imported template is marked
`source='bulk_import'` so an admin can see which references were not camera-captured.

---

## 3. Passive liveness & anti-spoofing (MiniFASNet ONNX)

`backend/liveness.py` — single-frame, passive, no user action.

* **Model**: MiniFASNet (V2/V3 `80x80` crop) exported to ONNX at
  `backend/models/minifasnet.onnx` (`LIVENESS_MODEL_PATH`). Input `1x3x80x80` float32, RGB,
  `[0,1]`. Outputs handled: 3-class `[live, print, replay]` (mini_fasnet_v2), 2-class, or a single
  logit (binocular-style).
* **Provider**: `onnxruntime.InferenceSession`, created once (cached), `providers=["CPUExecutionProvider"]`,
  intra-op threads pinned. Session creation is the only expensive part (~50 ms) and it happens at
  startup/readiness, not per request.
* **Positioning in the pipeline** — early rejection, before any DeepFace call:

```
upload ─► decode/EXIF ─► resize 640 ─┬─► LIVENESS (MiniFASNet, ~10 ms) ─► reject?  → 422 + code
                                     └─► (only if live) DeepFace.represent (VGG-Face, ~500 ms+)
                                                                       └─► cosine vs reference
```

  Liveness is ~50× cheaper than `DeepFace.represent(img, VGG-Face, mtcnn)`, so a presentation attack
  costs the server almost nothing and cannot be used as a CPU exhaustion vector.
* **Thresholds** (`config.py`): `LIVENESS_ACCEPT_THRESHOLD=0.70` (genuine probability required to
  accept), `LIVENESS_REJECT_THRESHOLD=0.55` (confidence in an attack class required to reject
  outright). The gap is deliberate: between the two the verdict is *low confidence* and is treated
  as **not live** — biometrics fail closed.
* **Modes** — `LIVENESS_MODE`:
  * `off` — skip entirely (tests, emergency).
  * `advisory` (default) — record `liveness_class` / `liveness_score` on the log, raise
    `liveness_spoof` / `liveness_degraded` notifications, **do not block**. This is the default
    because a mis-calibrated threshold must never stop a site from recording attendance.
  * `enforce` — reject before DeepFace.
* **Error codes** (stable, machine-readable, returned in `detail.error_code`):

| Code | HTTP | Meaning / handling |
| --- | --- | --- |
| `spoof_detected` | 422 | attack class above reject threshold; notification `liveness_spoof`, audited |
| `liveness_low_confidence` | 422 | genuine probability below accept threshold; fail closed |
| `liveness_unavailable` | 422 in `enforce` | runtime/model missing. Fail closed unless `LIVENESS_ALLOW_UNAVAILABLE=1`, in which case the request proceeds and is logged + notified as `liveness_degraded` |
| `liveness_error` | 422 | inference raised; same policy as unavailable |
| `face_not_found` / `multiple_faces` | 400 | existing DeepFace outcomes, unchanged |

* **Fallback handling**: no ONNX runtime, no model file, or a checksum mismatch all resolve to
  `available=False` with a reason string surfaced by `/api/v1/status` and the readiness gate. The
  gate reports it as **advisory** (never fatal) so a missing optional model cannot block startup,
  and `LIVENESS_MODE=enforce` with no model reports as a **fatal** misconfiguration instead of
  silently rejecting every punch.
* Every attendance row stores `liveness_class`, `liveness_score`, and (when applicable)
  `flag_reason`, so spoof attempts are auditable after the fact. Offline materialized punches store
  `liveness_class='unverified_offline'`.

---

## 4. Shift rules, working hours & overtime

Twilio/WhatsApp is already purged (**DONE**): no dependency, no credential, no outbound call;
`send_whatsapp_alert()` remains only as a deprecated no-op that returns `False` (it is referenced by
the frozen acceptance test). All alerts are rows in `admin_notifications`, exposed at
`/api/v1/admin/notifications` and `/api/v1/admin/notifications/{id}/read` for polling.

Rules live in the `shift_rules` singleton row, editable via `GET/POST /api/v1/admin/shift_rules`:
`clock_in_window_start 04:00`, `clock_in_window_end 06:30`, `regular_hours 8.0`,
`overtime_notify_hours 8.1`, `site_timezone Africa/Cairo`,
`working_days`. (`hard_cutoff_hours` survives only as a column default, read as a *review
mark* for offline shifts - it caps nothing.)

| Rule | Behaviour |
| --- | --- |
| Clock-in window | arrival outside 04:00–06:30 (site timezone) is **accepted and flagged**: `active_sessions.late_flag`, `flag_reason`, notification `late_arrival` (deduped per worker per day) |
| No 8 h auto-close | nothing closes a session at 8 h; `active_sessions` keeps running and hours keep accruing |
| **Overtime crossing at exactly 8.1 h** | `overtime.scan_overtime()` runs on a daemon thread (`OVERTIME_WATCHER_INTERVAL_SECONDS=60`, started in `lifespan`) **and** on demand via `POST /api/v1/admin/overtime/scan`. When `now >= clock_in + 8.1 h` and `active_sessions.overtime_notified_at IS NULL`, it stamps the column and inserts a `dedupe_key`-guarded `overtime_exceeded` notification carrying the *crossing* timestamp. Idempotent ⇒ safe to run in several workers |
| Post-shift approval | clock-out with `hours > 8.1` writes `status='Pending Overtime Approval'`, `status_code='pending_overtime'`, `overtime_hours = hours - 8.0`, and appears in `/admin/pending_reviews`. `POST /admin/approve_review` sets `approved_hours` (capped at recorded hours), `reviewed_by`, `reviewed_at` and marks the notification read. Payroll counts **approved** hours only |
| No hard cutoff | nothing closes a forgotten shift on a timer. Past 8.1 h the watcher alerts and the shift keeps counting; past 11 h an offline clock-out keeps its real hours and is flagged for review rather than flattened. Only a human ends a shift: the worker clocks out, or an admin uses `POST /admin/force_clock_out`, which records the hours actually worked |

Watcher safety: each pass opens its own short-lived connection, catches every exception (a watcher
that dies silently is worse than none), and never runs during tests unless
`OVERTIME_WATCHER_ENABLED=1` is set explicitly.

---

## 5. Offline punch synchronization & tamper-proof queuing

The hard problem is **time**: a phone's wall clock is worker-controlled, so a punch cannot carry its
own authority.

### Schema (`punch_queue`, extended by migration 5)

| Column | Meaning |
| --- | --- |
| `client_punch_id` UNIQUE | client-generated UUID ⇒ idempotent replay of a whole batch |
| `worker_id`, `device_id` | subject and signing device |
| `action` | `Clock In` / `Clock Out` |
| `client_timestamp` | device wall clock — **audit only**, never authoritative |
| `anchor_server_time`, `monotonic_offset_s` | the authoritative pair: server-signed anchor + device monotonic elapsed time |
| `client_offset_s` | `client_timestamp − anchor_server_time`, a tamper *signal* |
| `nonce` UNIQUE per device | replay ledger |
| `signature`, `signature_version` | HMAC over the canonical field string |
| `lat`, `lon`, `accuracy`, `location_trusted` | geofence evidence |
| `photo_sha256` | binds the punch to the selfie actually captured offline |
| `received_at`, `status`, `rejection_code`, `materialized_log_id` | server-side lifecycle |

### Cryptographic strategy

1. **Device key without a stored secret.** At `POST /api/v1/attendance/devices/register` the server
   generates a random `key_salt` and returns
   `device_key = HMAC-SHA256(SECRET_KEY, "device|v1|<worker_id>|<device_id>|<epoch>|<salt>")`.
   Only the salt is stored. Nothing usable leaks from a database dump, and revocation is
   `key_epoch += 1`.
2. **Signed time anchors.** `POST /api/v1/attendance/anchors` returns
   `{anchor_id, server_time, signature}` signed with the device key. Offline, the client can only
   *use* an anchor it obtained while online — it cannot mint one, so it cannot backdate a punch
   beyond the last online moment.
3. **Monotonic elapsed time.** The elapsed part of a punch comes from `performance.now()` /
   `CLOCK_MONOTONIC`, which the user cannot set backwards. Effective time is therefore
   `anchor_server_time + monotonic_offset_s`, computed **server-side**.
4. **Canonical string + HMAC-SHA256.**
   `v1\n<device_id>\n<worker_id>\n<action>\n<client_punch_id>\n<nonce>\n<effective_ts>\n<lat>\n<lon>\n<accuracy>\n<photo_sha256>`
   verified with `hmac.compare_digest` against the derived key. Any edit to a timestamp, a
   coordinate or the action invalidates it.
5. **Replay prevention, three layers.** `client_punch_id` UNIQUE (batch re-upload is a no-op),
   `(device_id, nonce)` UNIQUE (a captured request cannot be re-inserted), and
   `(worker_id, action, effective_ts)` within a tolerance window (duplicate punches from a
   double-tap are collapsed).
6. **Window + skew policy.** `0 ≤ monotonic_offset_s ≤ OFFLINE_PUNCH_MAX_AGE_HOURS×3600` (72 h),
   `effective_ts ≤ now + OFFLINE_PUNCH_CLOCK_SKEW_SECONDS` (300 s) and
   `effective_ts ≥ now − 72 h`. `|client_offset_s| > skew` is recorded as `clock_tampered`.

### Acceptance order (cheapest and safest first)

`device known/active` → `client_punch_id not seen` → `nonce not seen` → `signature valid` →
`anchor signature valid` → `time window ok` → `clock tamper signal` → `geofence`.

Rejections are **stored, not dropped**: `status='rejected'` + `rejection_code`, visible at
`GET /api/v1/admin/punch_queue?status=rejected`, because a punch that legitimately happened at a
site with no signal must never vanish without a trace.

A punch that verifies but lands **outside every geofence** is accepted as `status='flagged'`
(`flag_reason='offline punch outside geofence'`), materialized, and notified — the worker is on
payroll and the admin decides.

### Materialization

Accepted punches are applied in **effective-time order** (a batch can contain both an IN and an
OUT): `Clock In` creates `active_sessions` with `start_source='offline'` and the late-window flag
computed from the *effective* time; `Clock Out` computes hours from the session, applies the same
`>8.1 h → pending_overtime` routing as the online path, and writes an `attendance_logs` row with
`source='offline'`, `device_id`, `client_timestamp`, `request_id=<client_punch_id>` and
`liveness_class='unverified_offline'`. Ordering violations (`Clock Out` with no open session,
`Clock In` while already clocked in) are rejected per punch with a specific code rather than
corrupting the session table. Offline sessions are subject to the same rule as online ones:
nothing caps them. A clock-out far past `overtime_notify_hours` keeps its punched hours, is
flagged with the claimed figure, and lands in the overtime approval queue.

The batch response returns a **fresh anchor**, so a client that has just reconnected is immediately
ready for the next offline window without another round trip.

### Client implementation — `frontend/offline_queue.js` (DONE)

The browser half of the contract, loaded by `index.html` beside `frontendjavascript.js`.
`OFFLINE_CRYPTO.canonicalPunch()` is a byte-for-byte port of `offline_sync.canonical_punch()`;
everything else is plumbing around it.

| Behaviour | Entry point | Notes |
| --- | --- | --- |
| Device registration | `OFFLINE.ensureDevice()` | once per (worker, phone): `device_id = web-<worker>-<rand>`, key kept in IndexedDB. A `409` means the key is gone (`deviceState='key_lost'`) and the worker is offered `rotateDevice()`, which is refused while punches are still queued — a key-epoch bump would invalidate signatures that have not been delivered yet |
| Anchor | `OFFLINE.refreshAnchor()` | fetched at boot, after every successful *online* punch, and on every reconnect; the sync response's `next_anchor` replaces it, so a phone that just came back is immediately ready for the next outage |
| Queue | `OFFLINE.queuePunch()` | `monotonic_offset_s` from `performance.timeOrigin + performance.now()` (immune both to a clock change and to a page reload), `effective_timestamp` by naive-string arithmetic interpreted as UTC (so a DST boundary cannot shift a punch by an hour), `client_offset_s` as wall-clock drift, the photo hash and the HMAC. Record + photo Blob in IndexedDB |
| Replay | `OFFLINE.syncNow()` | per-punch verdicts; adapts to `413` by halving the batch, stops on `401` / `403 device_revoked` / `404` **without discarding anything**, and stops with `no_progress` if a batch settles nothing instead of retrying in a loop |
| Offline UI | `WORKER_MODULES.fetchStatus()` / `renderClockPanel()` | the last server-confirmed shift state is cached and queued punches are folded on top, so the panel offers the right action with no signal; a banner shows the queue size with a "Sync now" action |

**Capture fallback.** `UI.submitAttendance()` keeps the existing online path and only diverts to the
queue when the request failed for *connectivity* reasons (`API.request` marks those errors
`.offline = true`). A real HTTP error is still shown to the worker: queueing a punch the server
rejected is not offline support, it is a lost audit trail.

**Authorisation.** The queued punch is authorised by the session token that replays it, not by the
password typed at capture time — with no network the server cannot check a password. The password
prompt is retained (muscle memory, and it keeps a stranger from tapping a phone that is already
signed in), but it is not stored and not proof.

**Retention.** Settled local copies expire after 7 days, photos after 72 h (`OFFLINE.prune()`). The
server row is the authoritative one.

**Rounding.** `Number.toFixed()` rounds halves away from zero, Python's `format()` rounds
half-to-even, and an exact tie is reachable with genuine GPS data (an accuracy of exactly `8.25`).
The client therefore implements half-to-even formatting — without it such a punch can be signed but
never verified, because the server re-formats every value before checking the signature.

**Verification.** `backend/tests/test_offline_client_signing.py` runs this file under Node and
(a) compares `canonicalPunch()`/`signPunch()` with `offline_sync` across eight field shapes,
(b) checks the offset arithmetic against Python `datetime`, and (c) feeds a browser-signed punch
through `POST /api/v1/attendance/sync`.

---

## 6. Management audit trails & timesheet reporting

### Audit trail (append-only)

`audit_log` already exists with `BEFORE UPDATE` / `BEFORE DELETE` triggers that `RAISE(ABORT)` —
enforced by the database, not by convention, so a correction must be appended as a new event.

Recorded columns: `actor_id`, `actor_role`, `action`, `entity`, `entity_id`, `before_json`,
`after_json`, `ip`, `user_agent`, `created_at`.

Actions covered: `attendance_clock_in`, `attendance_clock_out`, `user_create`, `admin_create`,
`password_reset`, `biometric_enroll`, `site_create`, `site_edit`, `site_delete`,
`review_approve`, `force_clock_in`, `force_clock_out`, `shift_rules_update` — plus Phase 02's
`enrollment_invite_create` / `_revoke`, `enrollment_completed`, `enrollment_bulk_started`,
`offline_device_register` / `_revoke`, `offline_punch_resolve`, `overtime_detected`.

Lifecycle: append on every mutating admin/bio-metric operation inside the *same transaction* as the
change (so an audited change cannot commit without its audit row), read via
`GET /api/v1/admin/audit_log?action=&actor_id=&start=&end=&limit=`, exportable, and never editable.

### Reporting

**Renamed:** the shifts report shipped as `reports/payroll`, and the admin tab as *Payroll*.
Since the app never pays anybody, both are now named for what they hold — hours and approval
status. `GET /api/v1/admin/reports/payroll` and `kind=payroll` still answer as aliases so a
saved link or script does not break; the frontend uses `#shifts=` in the URL fragment and
still reads the older `#payroll=`.

**And it stopped being a payroll report at all.** The shifts report used to aggregate hours
per worker and carry `hourly_rate` and a `gross_estimate`; it is a *timesheet* now — one row
per shift, the day it ended, who worked it, where, the hours it counts for, whether an
administrator has signed it off, and that worker's open notes. Nothing in it is money.

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/admin/reports/shifts?start&end&site&worker_id` | the timesheet: one row per shift (`log_id`, `date`, `timestamp`, `worker_id`, `worker_name`, `role`, `site_name`, `hours`, `recorded_hours`, `approved_hours`, `break_hours`, `status_code`, `status`, `awaiting_approval`, `open_notes`), plus `fields` and `totals` (`hours`, `approved_hours`, `awaiting_approval_hours`, `awaiting_approval`, `shifts`, `workers`, `break_hours`, `workers_with_open_notes`) |
| `GET /api/v1/admin/reports/attendance?start&end` | per worker: days present, expected working days (from `shift_rules.working_days`), **attendance rate**, late arrivals, auto-closed shifts, unresolved flags |
| `GET /api/v1/admin/reports/export?kind=shifts\|payroll\|attendance\|audit\|offline&format=csv\|xlsx` | streamed download; `csv` via stdlib, `xlsx` via `openpyxl` with a clear `501` (and instructions) when it is not installed |

Approval rule: **a row counts only once somebody has approved it.** `pending_overtime` /
`pending_review` / legacy `auto_closed` rows are marked `awaiting_approval=true`, and their
hours sit in `awaiting_approval_hours` rather than `approved_hours` until an admin approves
them — that is the whole point of the 8.1 h gate. An approved row then reports the *approved*
figure (8.25 h of a 9.5 h shift), with `recorded_hours` kept beside it; a rejected row counts
for nothing.

`kind=shifts|payroll` on the export writes exactly four columns — `Employee,id,site,hours` —
the same four, in the same order, as the admin console's own Download CSV button. The
screen's column order is the administrator's (kept in `localStorage`), so it deliberately does
not travel into the file.

---

## 7. Migration & validation strategy

### Migrations

`migrations.py` is already the right shape: a versioned list, `schema_migrations` bookkeeping, one
`BEGIN IMMEDIATE` transaction, rollback on failure, `PRAGMA foreign_keys` untouched.

* **Migration 4 (drift repair)** `users.token_version INTEGER NOT NULL DEFAULT 0`. This is a real
  bug fix: `security.py` compares the claim against `COALESCE(token_version, 0)` and two endpoints
  bump it, but neither `baseline_schema()` nor migrations 1–3 create the column, so on the live
  database `POST /auth/login` raises `OperationalError: no such column: token_version`.
  `DEFAULT 0` means every *existing* session token (which carries `ver: 0`, or none) stays valid —
  the remediation cannot lock out a site that is already signed in.
* **Migration 5 (Phase 02)** `enrollment_invites`, `enrollment_jobs`,
  `enrollment_job_items`, extra `punch_queue` columns (`nonce`, `anchor_server_time`,
  `monotonic_offset_s`, `signature_version`, `liveness_class`, `processed_at`), and the supporting
  indexes (`punch_queue(status)`, unique `(device_id, nonce)`, `enrollment_jobs(status)`).
* `baseline_schema()` is updated in the same commit so a fresh install and an upgraded install
  converge on byte-identical schema — `schema_guard` compiles the expected schema from exactly
  those two sources and fails loudly if they disagree.
* Safety: additive DDL only; SQLite executes `ALTER TABLE ADD COLUMN` without rewriting existing
  rows, so `users`, `attendance_logs`, `active_sessions` and `construction_sites` keep their data.
  A column added with a non-constant default would fail on a non-empty table, which is why every
  new column is `NULL`-able or `DEFAULT` a literal.
* Every migration is preceded by a verified backup (§1) and followed by
  `python -m tools.backup --prefix post_migration_phase02`.

### Test protocol (`pytest`, `backend/tests/`)

| Suite | Covers |
| --- | --- |
| `test_security_baseline.py` (exists) | auth, roles, rate limits, ID ranges, no unguarded admin route |
| `test_secrets_and_surfaces.py` (exists) | no baked-in secret, no outbound network, docs disabled |
| `test_session_integrity.py` (exists) | revocation across processes |
| `test_migration_and_drift.py` | migration 4 repairs `users.token_version` on a clone of the *live* database; login/change-password then succeed; `schema_guard.inspect()` reports no fatal drift and a fresh `baseline + migrations` database equals the migrated one |
| `test_enrollment_pipeline.py` | invite lifecycle (create → masked peek → complete → reuse refused → expired refused → revoked refused), token stored only as a hash, self-service capture writes reference + `template_version`, bulk dry-run classification, bulk batch per-row status for good row / unknown user / missing photo member, and that one bad row never aborts the batch |
| `test_liveness_gate.py` | a stubbed ONNX session: genuine frame → accepted and reaches DeepFace; print/replay frame → `422 spoof_detected` and DeepFace is **never** called (the early-rejection assertion); low-confidence → `liveness_low_confidence`; model missing → allowed in `advisory`, rejected in `enforce` with `liveness_unavailable`; `enforce` + missing model is fatal at the readiness gate |
| `test_shift_windows_and_overtime.py` | 04:00–06:30 boundary minutes (04:00 in, 06:30 in, 06:31 flagged, 03:59 flagged); session still open past 8 h (no auto-close); `scan_overtime()` at 8.09 h does nothing, at 8.11 h notifies exactly once and is idempotent on a second pass; clock-out at 8.2 h → `pending_overtime` + `pending_reviews`; approval sets `approved_hours` and payroll moves the hours from unapproved to approved; a forgotten shift past 11 h is still open and still counting (the administrator is notified, not obeyed), and a forced close records the real hours |
| `test_offline_sync.py` | register → anchor → sign → sync happy path; tampered `action`/`anchor_server_time` → `bad_signature`; replayed `client_punch_id` and replayed `nonce` → `duplicate_punch` / `replayed_nonce`; device clock 3 h behind → effective time still anchored (`clock_tampered` recorded, punch placed at the anchored time); punch older than 72 h → `punch_too_old`; future beyond skew → `punch_in_future`; revoked device → `device_revoked`; out-of-order batch (OUT before IN) applies in timestamp order; offline overtime routes to `pending_overtime`; `source='offline'` on the materialized log |
| `test_reports_and_export.py` | the shifts report is a timesheet (one row per shift, no money fields); unapproved hours are marked and excluded from `approved_hours`; attendance rate against `working_days`; CSV export header (`Employee,id,site,hours`) + row count and correct content type; XLSX returns either a real workbook or a documented `501` |

Harness rules stay as they are: clone the live database into a temp dir, stub DeepFace, stub the
ONNX session, chdir out of the project, and block outbound network — so the suite can never touch
live attendance data, load TensorFlow, or message anybody.

### Rollout order

1. Verified backup (§1) → 2. migration 4 + login fix + tests green → 3. liveness in `advisory`
mode (observe `liveness_class` on real traffic for a few days) → 4. `enforce` at enrollment →
5. overtime watcher → 6. offline sync (server, then the client queue — both shipped) → 7. reports →
8. frontend surfacing (notification bell, enrollment page, reports tab) → 9. post-change backup.
