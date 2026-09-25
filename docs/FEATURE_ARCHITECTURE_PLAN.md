# Feature Architecture & Integration Plan (Phase 02)

Construction workforce attendance system — FastAPI + SQLite + DeepFace, single-origin
frontend served by the same app.

Status legend: **DONE** (shipped, covers the requirement) · **PARTIAL** (foundation in
place, behaviour missing) · **MISSING**.

Baseline audit of this revision (before the Phase 02 edits):

| Area | Status | Evidence |
| --- | --- | --- |
| Startup gate / readiness | DONE | `readiness.py` (~30 checks, fatal vs repairable), `schema_guard.py` compiles expected schema from baseline + migrations |
| AuthN/AuthZ | DONE | JWT with pinned algorithm, role re-read per request, `require_role` guards on every route (85 as shipped), and the identity is always the token - never a request field. The whole surface is *checked* at startup, not just `/admin`: `api_routes_authorised` (fatal) requires each route to be guarded or declared open with a reason. See §8 |
| Append-only audit log | DONE | `audit_log` + `UPDATE`/`DELETE` triggers that `RAISE(ABORT)` |
| Twilio/WhatsApp removal | DONE | no credentials, no network path; `send_whatsapp_alert()` is a deprecated no-op |
| `admin_notifications` + endpoints | DONE | table with `dedupe_key UNIQUE`, `/api/v1/admin/notifications`, `.../{id}/read` |
| Shift rules (04:00–06:30) | DONE | `shift_rules` table, `/admin/shift_rules` GET/POST, late arrival flagged + notified |
| ~~11 h hard cutoff as safety net~~ | **REMOVED** | it invented hours: a genuine 13 h shift was force-closed at 11 h and paid as 11, and the missing 2 h were invisible. `enforce_11h_cutoff()` is gone, the offline path no longer flattens hours to the cutoff, and nothing closes a forgotten shift on a timer - the overtime watcher alerts and a human closes it |
| Post-shift >8.1 h approval routing | DONE | clock-out sets `Pending Overtime Approval` / `pending_overtime`, requires `/admin/approve_review`; the standard day is credited while the extra hours wait, so an undecided overtime shift is not an unpaid day |
| **Overtime alert *at the crossing*** | **MISSING** | no watcher; a shift that never clocks out never alerts |
| **MiniFASNet passive liveness** | **MISSING** | columns `liveness_class`/`liveness_score` are written as `NULL`; no ONNX runtime |
| **Self-service enrollment** | **MISSING** | only admin `POST /admin/enroll` |
| **Bulk CSV + ZIP onboarding** | **MISSING** | — |
| **Offline punch sync** | **DONE** | device keys (no secret at rest), server-signed time anchors, HMAC per punch, replay prevention, per-punch refusal stored not dropped, materialisation in effective-time order — and nothing materialised is approved: every arriving punch is held for review, and the selfie the phone kept is uploaded and scored when the queue lands (see §5) |
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

### 2a-bis. An administrator's own face, from the console (`POST /api/v1/worker/me/enroll`)

An ``admin`` who works a site reaches the clock (``/attendance/verify`` takes any authenticated
role) but a punch needs a stored template, and every path that produced one needed somebody
else: ``/admin/enroll`` takes a ``worker_id``, a link has to be opened on a phone, and the
console's create form only runs while the account is being made. So the third door:

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/v1/worker/me/enroll` | `admin` | `multipart/form-data`: `photo` (a live console capture) |

* **The subject is the token.** No `worker_id` on the wire, and one that is sent is ignored, so
  the only template it can write is the caller's. ``/admin/enroll`` stays what it is - an
  administrator enrolling *somebody* - and this is not a narrowing of it.
* **The role list is the clock list** (``main.SELF_ENROLL_ROLES`` = ``UI.handsetRoles``): the
  roles that run the console *and* work a shift. A worker's template stays the company's act
  through an invite; a ``head_admin`` works no rota and has no punch card to need one.
* **Liveness runs** (``enrollment.embed_reference``, ``ENROLLMENT_LIVENESS_MODE`` inherited from
  ``LIVENESS_MODE``), because the photo is a camera frame - unlike the create form and the
  roster import, which take a file from disk and can only be advisory about it.
* **The replacement is recorded as one**: ``template_replaced`` in the response, a before-image
  on the ``biometric_self_enroll`` audit event, and an ``enrollment_completed`` notification -
  a credential minted with nobody else in the loop is the event an operator wants to see.

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
  `liveness_class='unverified_offline'` — which is a statement about *when* the frame was checked,
  not a permanent one: it is replaced with the real verdict when the queued selfie reaches
  `POST /attendance/sync/photo` (see §5, "The queued selfie is scored at sync").

---

## 4. Shift rules, working hours & overtime

Twilio/WhatsApp is already purged (**DONE**): no dependency, no credential, no outbound call;
`send_whatsapp_alert()` remains only as a deprecated no-op that returns `False` (it is referenced by
the frozen acceptance test). All alerts are rows in `admin_notifications`, exposed for polling at
`/api/v1/developer/notifications` and `/api/v1/developer/notifications/{id}/read` - the queue is the
root tier's, so the two paths this section was written with (`/api/v1/admin/notifications*`) are
gone rather than left answering a filtered list.

Rules live in the `shift_rules` singleton row, editable via `GET/POST /api/v1/admin/shift_rules`:
`clock_in_window_start 04:00`, `clock_in_window_end 06:30`, `regular_hours 8.0`,
`overtime_notify_hours 8.1`, `site_timezone Asia/Kuwait`,
`working_days`. (`hard_cutoff_hours` survives only as a column default, read as a *review
mark* for offline shifts - it caps nothing.)

| Rule | Behaviour |
| --- | --- |
| Clock-in window | arrival outside 04:00–06:30 (site timezone) is **accepted and flagged**: `active_sessions.late_flag`, `flag_reason`, notification `late_arrival` (deduped per worker per day) |
| End of the day, and which rule owns it | `overtime.scan_auto_close()` ends a shift at `regular_hours` **paid** (8 h - 8.5 h on site) as `auto_closed_8h`, payable, because that close *is* the policy decision. It writes both halves of the event in one transaction: the administrator's `shift_auto_closed` alert and the worker's own `shift_auto_closed` notice (`worker_notifications`, `KIND_WORKER_SHIFT_AUTO_CLOSED`) - the close is the one path that ends a shift unattended, so the worker is told rather than left to discover it when their next clock-out is refused. It **stands down** when the overtime line sits above the paid day (the shipped 8.1 vs 8.0): a shift closed at 8 h could never be observed crossing 8.1 h, so the shift runs on, the crossing is reported, and the hours past the line wait for approval. The close is therefore shipped **off** (`auto_close_at_regular` is 0 by default): with those two figures the switch closed nothing while reading as on, and a fresh volume was born reporting `overtime_close_deferred`. Decided once in `shift_hours.day_end_rules()`, logged at startup, published as `day_end` on `GET/POST /admin/shift_rules` and raised as the `overtime_alert_reachable` / `overtime_close_deferred` readiness checks - see the README's *Which rule ends the day* |
| **Overtime crossing at exactly 8.1 h** | `overtime.scan_overtime()` runs on a daemon thread (`OVERTIME_WATCHER_INTERVAL_SECONDS=60`, started in `lifespan`) **and** on demand via `POST /api/v1/admin/overtime/scan`. When `now >= clock_in + 8.1 h` and `active_sessions.overtime_notified_at IS NULL`, it stamps the column and inserts a `dedupe_key`-guarded `overtime_exceeded` notification carrying the *crossing* timestamp. Idempotent ⇒ safe to run in several workers |
| **The worker's half of that crossing** | `overtime.announce_crossing()` is the one writer of the worker's notice (`worker_notifications`, kind `overtime_crossed`). The watcher calls it for a shift that is still open (`still_open=True`); every path that can *end* a shift calls it at the moment it makes the overtime decision — the punch (`main`), the quick link (`quick_links`), the offline materialization (`offline_sync.materialize_worker`, which returns its results so the two callers can dispatch after their transaction), and `POST /admin/force_clock_out`. The figures come from the shift's own stored clock-in, and the `dedupe_key` names the shift (`worker_overtime_live:{worker}:{clock_in}`), so the two writers cannot announce one crossing twice. `push.dispatch_async()` is called after the caller's transaction commits, via `overtime.deliver_worker_notices()` (the same post-commit delivery the auto-close's worker notice uses) |
| **The answer is its own notice** | the crossing notice is about the *shift* (a line was crossed, the excess needs approval) and was the last word the worker had until their clock-out, so the decision the Approvals queue records gets its own notice, written by `overtime.decide_crossing()` in the same transaction as the decision row: kind `overtime_authorised` (or `overtime_declined` for a refusal — opposite statements, so a kind each, the same reasoning the auto-close's twin notice follows), stating the ceiling, the figure that had been worked when it was answered, who authorised it, and the hours a further decision would have to cover. Its `dedupe_key` names the **decision** (`worker_overtime_decision:{id}`) rather than the shift, so a ceiling the shift outgrows and an operator extends reaches the worker twice — the extension is the answer their clock-out settles at — while a repeated request cannot. The operator's note is deliberately excluded: it is written for the record, and the console promises a worker never sees a reviewer's note. `push.dispatch_async()` runs after the endpoint's transaction commits, through `overtime.deliver_worker_notices()`, on the new answer only |
| **Where the worker reads those notices** | the handset's *Alerts* tab is that inbox: `GET /worker/me/notifications` (the token's own rows, newest first, with a read state and an `unread` count) and `POST /worker/me/notifications/read`, which marks one notice or the whole inbox. The clock panel badges the count on the tab and carries a band above the clock button naming the newest unread notice with the way in — the close writes its notice precisely because the next clock-out refuses the worker, so it has to be in front of them *before* the tap. One number, read once (`unread_only=true&limit=1`), paints both badges; the single-notice read sends `notification_id` in the **query string**, which is where the route reads it (a JSON body is answered exactly like "no id", i.e. the whole inbox read). On a deployment with no VAPID key pair the tab is the entire delivery path, and `readiness` reports the missing push keys as an advisory rather than as the notice being lost. **The push half** (`push-service-worker.js`, registered at boot from `UI.init`; the profile tab's settings card) delivers the notice with the app closed: the worker registers before any permission is asked, the card reads `GET /worker/me/push` and names its state honestly, enabling subscribes against the deployment's VAPID key and POSTs it (a backend refusal releases the browser subscription too), disabling releases the browser side whatever the server answered, and a tap posts `alert-clicked` to the page, which marks the notice read the same way the inbox does. The worker file holds no token and no state |
| What the line counts, and who decides it | `overtime_notify_hours` is a **paid-hours** figure - time on site less the unpaid break, the same basis `regular_hours` uses (`shift_hours.OVERTIME_BASIS`), never time on site. Resolved once by `shift_hours.overtime_assessment()` / `overtime_rule()`, read by all four clock-out paths (punch, quick link, offline materialization, admin force-clock-out) and both watcher scans; the comparison is on **seconds** with `>=`, so the card, the alert and the gate agree on the same second |
| Post-shift approval | a clock-out whose `overtime_assessment()['needs_approval']` is true - paid time *at or past the line* **and** time past the regular paid day - writes `status='Pending Overtime Approval'`, `status_code='pending_overtime'`, `overtime_hours = paid - regular`, and appears in `/admin/pending_reviews`. `POST /admin/approve_review` sets `approved_hours` (capped at recorded hours), `reviewed_by`, `reviewed_at` and marks the notification read. Payroll counts **approved** hours only — and for a pending overtime row that means the standard day it has provisionally earned (`paid - overtime_hours`), not zero, so a hold on the extra hours is not a hold on the eight nobody is questioning. A line set below the paid day warns early without holding ordinary full days, because the second half of the rule is required |
| No hard cutoff | nothing caps a shift at a fixed hour. Past the overtime line the watcher alerts and the shift keeps counting; a late offline clock-out keeps its real hours and is flagged for review rather than flattened. A forgotten shift (one nobody clocks out and the close has stood down for) is ended only by a human: `POST /admin/force_clock_out` records the hours actually worked. Worth knowing: an open shift refuses that worker's **next** clock-in, so the alert says to end it rather than leaving it |

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

### Nothing arrives approved (DONE)

Materialising a punch is not confirming it. A punch taken with no signal was never checked — not the
face, not the frame, not even the site — and the only evidence for it is a photo on the worker's own
phone. So the **clock-out** — the row that carries the hours — is written as `pending_review`, or
`pending_overtime` when the shift crossed the overtime line (that hold is a fact about the hours and
wins over the generic one), with the provenance sentence recorded in `flag_reason` — a queue entry
nobody can explain is a queue entry nobody acts on. `reports.PAYABLE_CODES` contains neither code, so
the hours sit in the timesheet's awaiting-approval column and in the worker's own `pending_hours`
until an administrator approves or adjusts them through `POST /admin/approve_review`.

The **arrival** gets a code of its own, `unverified_offline` (`migrations.STATUS_CODE_UNVERIFIED_OFFLINE`),
and the difference is load-bearing rather than cosmetic. It carries no hours, so there is no money
for a reviewer to decide; and `pending_review` is not a passive status — the online clock-out and
the quick links both **refuse while any such row exists**. An arrival held that way would leave a
worker who had come in during a dead spot unable to close their shift, and the day would never be
recorded. Unverified says exactly what the row is: recorded, never checked, and not payable either
(it is in no `reports.PAYABLE_CODES`). One notification is sent per offline shift, on the row that
carries the hours (`review_pending`, dedupe `offline_review:{worker}:{punch}`, and none for the
arrival); a shift past the line keeps its existing overtime notice and sends no second one.

### The queued selfie is scored at sync (DONE)

`POST /api/v1/attendance/sync/photo` takes the frame the phone kept, one multipart request per
photo, and runs the online punch's **own** checks over it — MiniFASNet liveness, then the VGG-Face
comparison through `main.compare_faces_sync` (imported late: `main` imports `offline_sync`), with the
band re-derived from the live pipeline so a stubbed comparison cannot leave the verdict path
untested. What is *not* shared is the policy, and it cannot be: an online punch may refuse the
worker standing in front of the camera, while an offline one has already happened. So a finding is
recorded for the reviewer — the distance, the liveness class and score, and a `sync selfie: …`
sentence appended to `flag_reason` — and never turned into an approval or a refusal.

| Rule | Where |
| --- | --- |
| The bytes must be the frame that punch was signed against | `sha256(upload) == punch_queue.photo_sha256`, which is inside the HMAC — checked before anything is decoded |
| One upload policy for every photo in the app | `uploads.read_photo` (ceiling enforced *while reading*, type decided from the bytes) |
| Scored exactly once | `punch_queue.photo_scored_at` (migration 17); a retry is answered `200 already_scored` from the row, so the sentence is not stacked and the administrator is not told twice |
| A busy engine is not a verdict | `FaceEngineBusy` → `503` + `Retry-After`, nothing written and nothing marked scored, so the phone keeps the frame and the next sync is a first attempt |
| A frame that cannot be confirmed | recorded on the row (`score` stays the `0.0` "no score" sentinel) with a `liveness_spoof` (critical) or `liveness_degraded` (warning) notification, deduped by punch id |
| Telemetry | the online punch's own labels (`liveness_spoof`, `frame_refused`, `rejected`, `flagged_review`, `approved`) — `approved` here means the *frame* was confirmed, never the punch |

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
| Selfie upload | `OFFLINE.uploadPhoto()` (called from the replay loop) | one multipart `POST /attendance/sync/photo` per settled punch that still has its frame and was not rejected (a rejected punch has no review row to score). Best-effort: a `503` or a lost connection leaves the photo on the phone and the punch settled — the queue is never held up by a face, and the replay stops rather than making a batch POST certain to fail |
| Offline UI | `WORKER_MODULES.fetchStatus()` / `renderClockPanel()` | the last server-confirmed shift state is cached and queued punches are folded on top, so the panel offers the right action with no signal; a banner shows the queue size with a "Sync now" action |

**Capture fallback.** `UI.submitAttendance()` keeps the existing online path and only diverts to the
queue when the request failed for *connectivity* reasons (`API.request` marks those errors
`.offline = true`). A real HTTP error is still shown to the worker: queueing a punch the server
rejected is not offline support, it is a lost audit trail.

**Authorisation.** The queued punch is authorised by the session token that replays it, not by the
password typed at capture time — with no network the server cannot check a password. The password
prompt is retained (muscle memory, and it keeps a stranger from tapping a phone that is already
signed in), but it is not stored and not proof.

**Retention.** Settled local copies expire after 7 days. A photo is kept while its punch is queued,
and after that only while the server has not got it — the upload deletes the frame as soon as the
scoring confirms it, and `OFFLINE.prune()` is the backstop for the paths that could not (a failed
upload, a worker who never synced again), applying the same 72 h window. The server row is the
authoritative one, and a face does not belong on a worker's phone once it has been read.

**Rounding.** `Number.toFixed()` rounds halves away from zero, Python's `format()` rounds
half-to-even, and an exact tie is reachable with genuine GPS data (an accuracy of exactly `8.25`).
The client therefore implements half-to-even formatting — without it such a punch can be signed but
never verified, because the server re-formats every value before checking the signature.

**Verification.** `backend/tests/test_offline_client_signing.py` runs this file under Node and
(a) compares `canonicalPunch()`/`signPunch()` with `offline_sync` across eight field shapes,
(b) checks the offset arithmetic against Python `datetime`, and (c) feeds a browser-signed punch
through `POST /api/v1/attendance/sync`. `test_frontend_offline_selfie_upload.py` drives
`OFFLINE.syncNow()` under Node against a small fake IndexedDB and a fetch whose answers the test
chooses, pinning the upload (endpoint, multipart shape, no client-set `Content-Type`, the frame
dropped once the server has it, the frame kept when it does not, and nothing uploaded for a rejected
punch), and `test_offline_selfie_scoring.py` covers the server side of both halves — the review
routing with its reason, the hash binding, the scored-once marker, the busy engine, and the two
properties that scoring must never acquire (approving or refusing on its own).

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
`offline_device_register` / `_revoke`, `offline_punch_resolve`, `overtime_detected`, and
`biometric_self_enroll` - the last one distinct from `biometric_enroll` on purpose: who minted
the face matters as much as the fact that one was minted.

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
| `GET /api/v1/admin/reports/shifts?start&end&site&worker_id` | the timesheet: one row per shift (`log_id`, `date`, `timestamp`, `worker_id`, `worker_name`, `role`, `site_name`, `hours`, `recorded_hours`, `approved_hours`, `break_hours`, `status_code`, `status`, `awaiting_approval`, `awaiting_approval_hours`, `open_notes`), plus `fields` and `totals` (`hours`, `approved_hours`, `awaiting_approval_hours`, `awaiting_approval`, `shifts`, `workers`, `break_hours`, `workers_with_open_notes`) |
| `GET /api/v1/admin/reports/attendance?start&end` | per worker: `role` beside the name, days present, expected working days (from `shift_rules.working_days`), **attendance rate**, late arrivals, auto-closed shifts, unresolved flags |
| `GET /api/v1/admin/reports/export?kind=shifts\|payroll\|attendance\|audit\|offline&format=csv\|xlsx` | streamed download; `csv` via stdlib, `xlsx` via `openpyxl` with a clear `501` (and instructions) when it is not installed |
| `GET /api/v1/branding` | the company's own name and mark — **public**, because the login panel is the screen that needs it and has no session. `null` per line is "nobody configured this" (the shipped lockup prints), `""` is "the company removed this line" (nothing prints there), and `logo_url` is a *path* with the stored version on it (`?v=`) for the client to resolve |
| `GET /api/v1/branding/logo` | the stored mark, as bytes. Public like the name beside it, `404` when there is none, and cached `max-age=31536000, immutable` because the URL carries its version |
| `POST /api/v1/admin/branding` | the four lines, each optional and nullable (`admin_only`, audited) |
| `POST`/`DELETE /api/v1/admin/branding/logo` | set the mark from an uploaded image (re-encoded server-side), or take it down so the shipped one is used again |

Approval rule: **a row counts only once somebody has approved it** — with one deliberate
exception, because there are two different holds. `pending_review` (a doubt about whether the
work happened: a liveness verdict, a selfie that did not match) and legacy `auto_closed`
marked `awaiting_approval=true`, and the whole row sits in `awaiting_approval_hours` until an
admin decides.

`pending_overtime` is the other kind: the hold is on the **extra** hours, not on the work. A
worker/moallem works 8 h normally, so the standard day has already been earned and is
credited as the row is filed; only the time past it waits. A 9.0 h paid shift therefore
reports `approved_hours = 8.0`, `awaiting_approval_hours = 1.0` while pending. Holding the
whole shift made an ordinary day conditional on somebody pressing a button, and it
contradicted the refusal path beside it in the same queue, which has always paid the standard
day. `reports._shift_hours()` is the one place that splits them, every status satisfies
`counted == approved + awaiting`, and the row carries `awaiting_approval_hours` so the console
totals a filtered view with the server's arithmetic instead of assuming an awaiting shift
holds all of its hours. A `pending_overtime` row with no overtime figure fails closed and
credits nothing.

An approved row then reports the *approved* figure (8.25 h of a 9.5 h shift) — the decision
overwrites the provisional day rather than adding to it — with `recorded_hours` kept beside
it; a rejected row counts nothing past the standard day (`overtime_rejected`, which is
payable).

`kind=shifts|payroll` on the export writes exactly four columns — `Employee,id,site,hours` —
the same four, in the same order, as the admin console's own Download CSV button. The
screen's column order is the administrator's (kept in `localStorage`), so it deliberately does
not travel into the file.

**A normal administrator works shifts too.** `/worker/me/enroll` let one put their own face
on file and `/attendance/verify` always took their token, so an administrator's own hours
have been in `attendance_logs` since then — and the surfaces that read those rows have to
say *whose* they are, or the one row a reviewer has to pick out is the one row that looks
like every other:

* **Live Ops** (``/admin/active_sessions``, which has always returned ``role``) labels it in
the reader's language on the table row and on the phone card — the card previously showed no
role at all — and the board's filter matches both the code and the label;
* the **Shifts tab** gained a ``role`` column (beside the name, in the default order, so an
older stored order gets it appended rather than staying hidden) whose cell is the translated
label, and ``shiftsMatches`` searches the code *and* the label, which is what makes "whose
shifts are these" a filter rather than something to scan by eye;
* ``/admin/reports/attendance`` carries ``role`` on every row and as the third column of the
attendance export: the report's figures never had a role branch — an administrator's day is
counted, expected and rated by exactly the same query — but a reader could not attribute
them;
* both review queues (``/admin/pending_reviews``, the console's Approvals tab, and
``/admin/reports/pending``) return ``role``, which the Approvals card already knew how to
label. An administrator whose own shift runs past the paid day waits in that queue like
anybody else, with its hours out of ``approved_hours`` while it waits.

Pinned by ``test_admin_shift_visibility.py``: the four backend surfaces, plus a browser run
of the board and the tab (the labels, the phone card, the filter in English and Arabic).

**A sheet for one worker's month, printed from their own row.** The Shifts tab's PDF is the
*period* — every shift, every worker, for the dates on screen — and "print what this person
did this month" is the other half of that question, asked while looking at one of their
shifts. Each shift row carries a printer control (the only cell on a row that is not a
column: not in the chooser, not in the CSV, not searchable) which asks
``/admin/reports/shifts?...&worker_id=`` for **one worker's calendar month**. Three decisions
carry it:

* the **subject** is the row's own worker, sent as ``worker_id`` and escaped into the query
  string, so the server answers with one person's rows — filtering the rows on screen would
  print whatever that screen happened to hold (a search, a day, a shared link's range);
* the **period** is the calendar month the on-screen period starts in, clipped to today
  while that month is running (the presets' own rule), read off the period rather than the
  clock so "this person's month" means the month the administrator is looking at;
* the **identity columns** (employee, role, id) move off the table and onto the header line,
  once: on a sheet about one person they would repeat the same three facts down every row.

It is built through the same ``PrintReport.sheetHtml`` frame as the period sheet and the
worker's own month, and the cells are the tab's own, so the paper and the screen agree. A
month nobody worked is refused in words rather than printed as a titled blank sheet, and a
failed request prints nothing and leaves the page as it found it. Pinned by
``test_frontend_worker_month_sheet.py`` (the request, the rows and the exclusions, the
figures, the month rule, the two refusals, the escaping of a hostile worker id, the phone
cards, the translated sheet) and by a real-browser run in
``test_signed_in_sessions_in_a_browser.py``, which is the only place the delegated click on a
painted row is observable at all.

### Company identity (`branding.py`, `company_settings`)

**Whose document this is, at the top of every sheet.** The wordmark, the legal suffix, the
founding year, the tagline and the logo were a JavaScript object and an SVG beside it, so every
surface that says whose payroll this is — and the sheet handed to payroll — needed a code change
and a redeploy to carry a different name. They are a single settings row now, next door to
``shift_rules`` and edited on the same console tab (**Admin → Company**).

* **``null`` and ``""`` are different, on purpose.** ``NULL`` is "nobody configured this" and
the shipped lockup is what prints — which is what makes this an override rather than a
migration, and why a deployment that never opens the panel is byte-for-byte unchanged. An
**empty string** is a decision: the line is not printed. A company with no legal suffix has to
be able to *remove* that line, and ``shift_rules``' "blank means the shipped value" rule (right
there, because a blank clock-in window is a real hours bug) would put it straight back. The
console panel therefore has both a Save, which sends strings including empty ones, and **Use the
shipped lockup**, which sends ``null``s: "print nothing here" and "stop deciding" are separate
keystrokes.
* **Reading is public, writing is `admin_only`.** The screen that most needs the company's name
is the login panel, which by definition has no session; what that exposes is the name on the
company's own front door and the mark it prints on its own documents. Writing is the same
authority the shift rules take, and every write is audited — the mark's *shape* (type,
dimensions, bytes) rather than its bytes, because the question the log answers is who put this
logo on our payslips.
* **The mark is re-encoded, never stored as sent.** ``uploads`` decides (5 MB, signature sniffed,
40 MP ceiling checked from the header before any decode), then ``branding.encode_logo`` writes
PNG when the image has transparency to keep and JPEG when it does not, scaled to 512 px on the
longest edge — a 40 KB mark instead of a 400 KB one for something that prints 34 mm wide. So the
bytes a browser can fetch afterwards are bytes this server drew: an uploaded polyglot, an HTML
document with an image header, or a JPEG with an embedded payload is not what comes back.
  ``logo_version`` is bumped on every write and travels in the URL (``?v=``), so a replaced mark
is a *different* URL and no cache anywhere can keep serving the old one — including the mark a
company just took down, whose old URL now answers `404`.
* **The lines are checked as prose, not as identifiers.** A newline is refused by name (a lockup
line prints on one line), and everything else is ``textguard.prose``'s: markup, encoded markup
and ``javascript:`` URLs refused, zero-width and bidi characters stripped. ``prose`` rather than
``identifier`` because "Smith & Sons" and "STONE · MARBLE · GRANITE" are ordinary company
writing that the identifier allowlist would refuse; what makes that safe is that every
interpolation is escaped where it is drawn, and the content rules are the second lock for the
renderer that is not.
* **One resolved record in the frontend (`Brand`).** The shipped lockup is the default, the
settings row is merged over it once, and every surface that says whose this is reads the same
object — login panel, handset header, console rail, the footer sentence (which used to have the
company name typed into a translated constant) and the header of every printed sheet. The
cache is applied before the first paint and the network answer only corrects what changed since,
deliberately not awaited: a phone at a gate must not wait on a cell tower to learn the company's
name. The repaint that answer may cause is skipped when a field already holds something typed,
and a settings read that fails leaves the last known lockup in place — which is what keeps an
offline handset from naming the wrong company.
* **The header is in the shared sheet frame.** ``PrintReport.brandHtml`` draws the name, the
suffix and year joined without a dangling separator, the tagline, and the mark, and
``sheetHtml`` puts it above the report's own title — so it is not a parameter a sheet can be
built without, for the same reason the note at the foot is not. Lines the company emptied are
dropped rather than drawn as empty paragraphs, on the login panel as well as on paper.

Pinned by ``test_branding.py`` (public read, `admin_only` writes, the `null`/`""` round trip and
both in the audit row, the length and line-break refusals, markup refused and ordinary company
writing accepted, invisible characters stripped, PNG/JPEG choice and transparency kept, the
pixel ceiling refused from the header, version bump on removal, one row by schema and by
endpoint), ``test_frontend_branding.py`` (the merge, the offline cache, the repaint guard, the
panel's save/reset/upload/refusal/removal, the header on all three sheets, marking-up a company
name, and the new strings in all four tables) and ``test_frontend_print_sheet.py``, which keeps
the brand classes in the one file that builds the frame.

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
* **A column is introduced only by the newest migration that names it.** The concrete failure
  that taught this rule: `punch_frame` shipped inside `migration_2_provenance_columns`, which
  every existing database had already recorded as applied — so the column was never created
  anywhere, the schema guard passed (its expectation is compiled from baseline + migrations,
  which agreed with each other and were both wrong against reality), and every clock-out died
  with `no such column: punch_frame` until migration 18 landed. Two guards now hold the rule:
  the drift pin adds `punch_frame` to the columns a fresh prototype database must gain, and
  `test_a_column_the_code_writes_is_never_declared_inside_an_older_migration` reads the
  migration sources and fails when a punch-written column is named by any migration older than
  the newest one — covering both the literal `add_column` form and the tuple form migration 2
  actually uses.
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
| `test_api_route_authorisation.py` | the whole-surface gate: passes on the shipped app; fails on an unguarded route, a stale exemption, a redundant exemption, a missing reason and a traversal that enumerated nothing; a new verb does not inherit a path's exemption; and each declared list is held to its own claim (public answers an anonymous caller, self-gated refuses one) |
| `test_role_audience.py` | the audience matrix: each guarded route's declared roles equal the audience its path implies (audiences stated independently of the guards, plus documented exceptions); no stale audience exception; every guarded route refuses an anonymous caller; and a live session from each role outside an audience is refused, with an in-audience control so a guard that denies everyone cannot pass |
| `test_ownership_matrix.py` | object-level ownership: every id-bearing route is accounted for (admin-only / credential / declared ownership check) with no stale entries; a second worker is refused a colleague's note (read, reply, close) and signing device, with the row asserted unmoved and the owner's access asserted as the control; a colleague's `worker_id` in a query string changes no self-scoped answer; and `POST /attendance/verify` refuses a complete form naming somebody else, writing no attendance row |
| `test_push_endpoint_authorisation.py` | where a notification may be sent: every shipped push service and its subdomains accepted; private, loopback, link-local, RFC 1918, metadata and single-label hosts refused; a public IP literal refused (the case a private-range denylist would allow); an unknown service refused, including a longer name that merely *ends with* an allowed one and a URL whose userinfo hides the real host; malformed shapes refused (non-https, non-443 port, embedded credentials, CRLF/NUL, over-length); the subscribe endpoint refuses at 422 **and stores nothing**, with a real endpoint accepted and an anonymous caller refused as controls; a planted subscription row is refused at send time with `harness.OUTBOUND` proving no request was made; a refusal is counted apart from a failure in the dispatch summary; and every `PUSH_ENDPOINT_HOSTS` entry is held to the same policy, including that the check is registered and advisory |
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

---

## 8. Authorization surface, and the gate that checks it

The model, in the order a request meets it:

1. **Token.** A signed JWT with a pinned algorithm; `token_version` is re-read from the
   `users` row on every request, so a password reset or a deactivated account ends existing
   sessions on every ASGI worker at once rather than only the one that handled the reset.
2. **Role guard.** `security.require_role(*roles)` is a FastAPI dependency, with
   `any_authenticated` / `admin_only` / `head_admin_only` pre-built. Each guard sets
   `_auth_marker` and `_allowed_roles` so it can be *inspected* by the startup gate instead
   of trusted.
3. **Ownership.** Self-scoped routes carry no id to tamper with (`/worker/me/*`). Where a
   record is reached by id, `ensure_self_or_role` decides, and a refusal is a **404** with
   the same text as a record that does not exist - "not yours" and "not there" must be
   indistinguishable or the endpoint enumerates which ids are real. The same rule applies to
   worker notes (`_fetch_note(..., worker_id=...)`).
4. **Payload identity is ignored, not obeyed.** Legacy `admin_id` / `creator_id` fields are
   accepted for wire compatibility and have no authority. `/attendance/verify` still takes
   the client's `worker_id` and rejects it when it names anybody but the token's owner.

The invariant that used to be missing is the **whole-surface** one. `auth_enforced_on_admin_routes`
(fatal) walked only paths containing `/admin/`; the other eighty-odd routes were guarded by
convention, so an endpoint added under `/api/v1/worker/` with no `Depends` at all was
reachable by anyone who could reach the port while the gate reported health.

`api_routes_authorised` (`backend/readiness.py`, **fatal**) enumerates the surface and
requires every route to be accounted for: guarded by `require_role`, or named in
`PUBLIC_ROUTES` / `SELF_GATED_ROUTES` / `PAGE_ROUTES` **with the reason beside it, keyed by
method and path**. Three properties beyond the obvious one:

* a **new verb** on a public path does not inherit the exemption - it must be declared and
  explained in the diff;
* a **stale** exemption (a route that no longer exists) and a **redundant** one (on a route
  that is already guarded) both fail, because a list that drifts stops being a review
  artifact;
* an enumeration that yields **nothing** fails rather than reporting `0 guarded, 0 missing`
  - that exact vacuous pass is how the sibling check once certified a surface it had never
  looked at.

Counts as shipped: **85** guarded, **12** declared open with a reason, **3** page routes.
`backend/tests/test_api_route_authorisation.py` holds the gate to all of it, including that
each declared list is honest when probed with no credential.

### 8a. Audience, which coverage cannot see

A gate can prove a route *has* a guard. It cannot prove the guard is the **right** one -
`any_authenticated` satisfies every coverage check while being the wrong answer for an
administrator's route. That is the failure that costs money, and nothing structural caught
it, because a guard cannot be wrong against itself.

`backend/tests/test_role_audience.py` supplies the missing reference point: the audience is
declared **independently of the guards**, derived from the shape of the API (an
`/api/v1/admin/` path belongs to the administrator roles, everything else to every signed-in
role) plus a short list of documented exceptions - `/worker/stats/{worker_id}` and
`/status/detail` are administrators'; `/worker/me/enroll` is one administrator's own face;
`/admin/admins/add` is head-admin-only. A table read back out of `_allowed_roles` would agree
with itself however wrong the guards were, so it is deliberately not read from there.

Checks, all of which fail the suite:

* a route **broader** than its audience (the security direction);
* a route **narrower** than its audience, which refuses somebody it was built for and is how
a 403 nobody reads gets shipped;
* an audience exception naming a route that no longer exists, which would widen a path by
name;
* a live probe: a session from a role outside the audience is refused, and one from a role
inside it is not - so a guard that denied everyone cannot pass.

The live probes are bodiless by design: FastAPI resolves dependencies before request
validation, so the refusal follows from the credential alone and the sweep writes nothing.
The division of labour between the two suites is exact - widening
`/worker/stats/{worker_id}` to `any_authenticated` fails the audience suite's live half while
the coverage gate passes all of its tests, and "fixing" it by removing the guard entirely is
caught by the coverage gate instead.

### 8b. Object-level ownership, which the audience check cannot see

Audience settles which **roles** may call a route; it is blind to a route any worker may call
that takes an **id**, where the id decides whose record is read or written. "May I call this
endpoint" and "may I have this record" are different questions, and a role guard answers only
the first - which is how an endpoint ships correctly guarded and still lets a worker read a
colleague's note, hours or attachment by counting up from 1.

`backend/tests/test_ownership_matrix.py` covers it. Every id-bearing route is accounted for by
exactly one of three answers, declared in the file as a review artifact:

| Answer | Routes |
| --- | --- |
| administrators only, so no cross-worker case exists | the whole `/api/v1/admin/**` id surface (14 routes) |
| the path is a **credential**, not an id | `/api/v1/enroll/{token}`, `/api/v1/q/{token}` (+ the two HTML pages) - hashed at rest, single-use, revocable, expiring, so a guessed token is the attack they already answer to |
| declared with the mechanism that decides the owner | `notes._fetch_note(..., worker_id=current.id)` (read, reply, close) and `offline_sync._device_lookup(conn, current.id, device_id)` (device revoke) |

The live half aims a **real second session at a real record owned by somebody else** and
asserts the refusal *and that the row did not move* - a 404 that still wrote is not a refusal -
with the owner's own access asserted beside it so a blanket deny cannot pass.

A third form of the mistake has no id in the path to find it by, so it is probed directly: an
id smuggled in through a **query string** or a **form field**. Every self-scoped route is asked
twice, with a colleague's `worker_id` and without, and the answers must be byte-identical
(asserted between responses, not against a payload shape). `POST /attendance/verify` gets a
complete form with the wrong `worker_id`, because that refusal is in the handler - every role
may punch so it cannot carry a role guard - and a bodiless probe would only prove a 422.

Two of those cases had no test anywhere before: a worker revoking a **colleague's signing
device** - which bumps their key epoch, silently invalidating every offline punch their phone
has queued, discovered at the gate rather than in the console - and a colleague's `worker_id`
appended to a self-scoped route. Probes that proved each half bites: scoping the device lookup
by id alone turned the revoke into a 200 for a colleague; accepting `worker_id` on
`/worker/me/logs` made the two answers differ.

**Not adopted: Clerk (or any hosted IdP).** There is no `package.json` in this repository and
no JS framework to integrate: the frontend is plain ES modules served by the same FastAPI
process, and the credential a punch actually turns on is not a password at all - it is a
geofence plus a live face matched against the enrolled template, with an offline path whose
punches are HMAC-signed against server-issued time anchors and whose device key is
reconstructible on request (nothing signed is stored at rest). A hosted identity provider
would sit in front of that and replace the password step, which this system deliberately
removed from the punch already - it would not replace the geofence, the liveness verdict,
the device epoch or the review queue, and it would add an outbound dependency to a product
whose offline mode is a requirement rather than a feature. The audit is the useful part, and
it is what produced §8's gate.

### 8c. Outbound destinations, which no role check can see

The three layers above decide who may *call* something. This one decides where the server
may *go*, and it is the only place in the application where a value a worker supplies becomes
a URL the server fetches. A push subscription endpoint is exactly that: the browser hands
over a URL, the server POSTs a notification to it, and it does so again on every event that
follows. Supplied-and-fetched is the definition of a server-side request forgery primitive,
and the request leaves from inside the network with no credential of the worker's attached.

**The control is an allowlist (`PUSH_ENDPOINT_HOSTS`), and that choice is the design.**
Refusing private ranges is the obvious alternative and it is the wrong property: nothing
about the attack requires an internal address. `https://collector.attacker.test/hook` is a
public host, reachable from anywhere, and confirms to its owner that the server fetched it -
and a denylist would accept it. The set of hosts a real browser can produce is small and
known, so "not internal" is replaced with "is a push service". Two rules sit behind the list
as a second lock, so a hand-added entry cannot quietly reopen the hole: an **address** is
never a push service however public it is, and a **private suffix** is never one however it
is spelled.

| Layer | Where | What it refuses |
| --- | --- | --- |
| the policy | `push.validate_endpoint` | not `https`, port not 443, credentials in the URL, whitespace or control characters, over-length, an IP literal, a single-label or private-suffix host, an unknown service |
| the door | `POST /worker/me/push/subscribe` | the same, at 422, **and the row is not stored** - a refusal that still wrote would leave the send path holding the URL |
| the send path | `push.deliver` | the same, re-applied per subscription, so a row written before the check existed, by an older build or by an operator's script, is never fetched |

The endpoint is parsed with `urlsplit` rather than matched as a string, because the gap
between those two *is* the attack: `https://fcm.googleapis.com@evil.test/` has the host
`evil.test`, and any check that looked for an allowed name anywhere in the string accepts it.
The host comparison matches on the **dot boundary** - a plain `endswith("notify.windows.com")`
accepts `notify.windows.com.evil.test`, which an attacker registers for the price of a domain.
Subdomains must still match, because WNS hands out `<region>.notify.windows.com`.

A refusal is counted **apart from a failure** in the dispatch summary, and the inbox row's
`last_error` names it: "the push service answered with an error" and "this server declined to
fetch that URL" are different events, and reporting both as failed sends an operator to the
wrong logs for a request that never left the server. The **allowlist itself** is held to the
same policy at startup (`readiness._check_push_endpoint_allowlist`, advisory), because a dead
entry there is the quietest failure in this area: the worker subscribes, the browser reports
success, and no notification ever arrives - indistinguishable from the vendor being down.

`backend/tests/test_push_endpoint_authorisation.py` covers all three layers plus the
allowlist, and the send-path half is asserted against `harness.OUTBOUND`, which records every
outbound HTTP attempt the application makes and performs none of them: "the server did not
send" is a claim about the network, so it is checked at the network seam rather than inferred
from a return value.
