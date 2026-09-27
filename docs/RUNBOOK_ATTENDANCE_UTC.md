# Runbook — Attendance timestamps in UTC (migration 28)

**Status: prepared, not activated.** Migration 28 and its column contract exist in code and are
tested, but the migration is deliberately **not registered** in `migrations.MIGRATIONS` yet. This
document is the activation plan: it states the invariant, the exact edit list, the migration and
rollback, and the verification. Registering the migration without doing the edit list is the one
way to corrupt payroll data — see "Why activation is atomic" below.

---

## 1. The invariant

> **Every naive timestamp stored in the database is UTC. Every naive timestamp shown to a person,
> fed to a shift window, or aggregated in a report is Kuwait (UTC+3).**

Stored values keep the exact same shape — a naive `YYYY-MM-DD HH:MM:SS` string in a `DATETIME`
column — so `strptime`, `BETWEEN`, string ordering and every existing index keep working. Only the
*meaning* changes, from Kuwait wall clock to UTC. "Timezone-aware" here means *stored as the
unambiguous instant*, not *stored with a `+00:00` suffix* (which would break the format contract).

Kuwait is a fixed UTC+3 with no DST, so the conversion is a constant 3-hour shift in both
directions and is exactly reversible.

---

## 2. The column contract

`migrations.ATTENDANCE_UTC_COLUMNS` is the single source of truth for *which* columns are UTC:

| Table | Columns |
|---|---|
| `active_sessions` | `clock_in_time`, `overtime_notified_at`, `transit_start_time` |
| `attendance_logs` | `timestamp`, `reviewed_at` |
| `punch_queue` | `client_timestamp`, `received_at`, `processed_at`, `anchor_server_time`, `photo_scored_at` |
| `refused_punches` | `created_at` |
| `device_anchors` | `server_time`, `issued_at` |
| `worker_devices` | `last_anchor_at` |
| `overtime_authorisations` | `clock_in_time`, `decided_at` |
| `quick_links` | `created_at`, `expires_at`, `revoked_at`, `last_used_at` |
| `quick_link_uses` | `created_at` |

**Out of scope, on purpose:** the system/forensic tables (`audit_log`, `admin_notifications`,
`worker_notifications`, `worker_notes`, enrollment jobs/invites, `retention_runs`, branding,
`users.enrolled_at`). They are operator surfaces, not payroll; they keep the Kuwait-local
convention. Converting them later is a second, independent migration; converting half of any one
table is the hazard this document exists to prevent.

---

## 3. Why activation is atomic

If the rows are shifted −3 h while any reader still treats the value as Kuwait wall clock, every
shift window, timesheet and live counter is wrong by three hours — the exact drift this whole
workstream removed. So migration 28 and the code in §4 must ship **in one release**:

1. The readers/writers are switched first (they are guarded by tests).
2. `migrations.MIGRATIONS` gains the line
   `(28, "attendance_timestamps_to_utc", migration_28_attendance_timestamps_to_utc),`
   and `SCHEMA_VERSION` returns to `28`.
3. On the next boot, `run_migrations` applies 28 inside its `BEGIN IMMEDIATE` transaction before
   the app serves a request, so there is no window in which new code reads old-meaning rows.

SQLite is single-instance on the mounted volume here, so the restart is atomic. If the deployment
ever moves to multiple replicas, the rewrite must run once, before the new replicas start.

---

## 4. Edit list

### 4.1 Writers — store UTC
Replace the value written to every in-scope column with a naive-UTC value. **Keep the write going
through the module's own `datetime` symbol** so the frozen-time tests still control it:

```python
def _utc_now() -> datetime:
    """Naive UTC, via the module ``datetime`` so ``monkeypatch.setattr(mod, "datetime", …)`` bites."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
```

Sites (function → column):

- `main.py`
  - `verify_worker` clock-in / transit-departure / transit-arrival: `clock_in_time`,
    `transit_start_time`, `attendance_logs.timestamp`.
  - `verify_worker` clock-out: `attendance_logs.timestamp`, `reviewed_at`.
  - `force_clock_in` / `force_clock_out`: `active_sessions.clock_in_time`,
    `attendance_logs.timestamp`, `reviewed_at`.
  - `_insert_log` callers: pass the UTC string.
  - `_record_refused_punch`: `refused_punches.created_at`.
- `offline_sync.py`: `active_sessions.clock_in_time`, `attendance_logs.timestamp`,
  `punch_queue.*`, `device_anchors.*`, `worker_devices.last_anchor_at`.
- `overtime.py`: `attendance_logs.timestamp`, `active_sessions.overtime_notified_at`,
  `overtime_authorisations.clock_in_time`/`decided_at`.
- `quick_links.py`: `quick_links.*`, `quick_link_uses.created_at`, the `active_sessions`
  insert, `attendance_logs.timestamp`.
- `reports.py` / `main.py` seeds in `migrations.py` if any write in-scope columns.

### 4.2 Readers — render Kuwait
`clock.to_kuwait(stored)` at the boundary. Do **not** convert values used only in differences
(elapsed seconds) — those are UTC−UTC and already correct.

- **Shift windows** — `shift_windows.Window.local` must treat a naive input as **UTC**:
  ```python
  if moment.tzinfo is None:
      moment = moment.replace(tzinfo=timezone.utc)
  return moment.astimezone(resolve_timezone(self.timezone))
  ```
  and `main._within_clock_in_window` must pass `_utc_now()`.
- **Elapsed** — `_seconds_on_site`, `list_active_sessions`, `get_workers_live`,
  `force_clock_out` pass `_utc_now()` as the second argument (stored is UTC).
- **API responses** — every `clock_in_time` / `timestamp` returned to a client is
  `clock.to_kuwait(...)` formatted with `clock.TS_FORMAT`.
- **Reports** — `reports._range_bounds`: build the Kuwait-day range and convert both ends with
  `clock.to_utc`; the timesheet's `timestamp` and `clock_in_time` columns render
  `clock.to_kuwait`; arrival verdicts go through `Window.arrival` (already UTC-aware after §4.2).
- **Monthly aggregate** (`main.get_worker_stats`): replace the month string with the UTC
  half-open range for the **Kuwait** month —
  `start = clock.to_utc(first_of_kuwait_month)`, `end = clock.to_utc(first_of_next_month)`, and
  filter `timestamp >= ? AND timestamp < ?`.

### 4.3 Tests
- The frozen-time fakes (`test_overtime_threshold._FrozenDatetime`,
  `test_site_shift_windows._FrozenClock`, `test_site_categories._FrozenClock`,
  `test_phase02_liveness_and_shifts._FrozenDatetime`) already implement `now(cls, tz=None)`, so
  `_utc_now()` is frozen correctly. Verify the `FIXED` moments are interpreted as intended
  (aware values convert exactly; naive values are assumed system-local).
- Rows planted directly with SQL (e.g. `test_overtime_threshold._plant`,
  `test_day_end_precedence`, `test_early_checkout_and_rounding`, `test_admin_shift_visibility`,
  `test_worker_self_service`, `test_phase02_offline_enrollment_reports`) must be planted as UTC.
- Add assertions that a stored stamp rendered back through `clock.to_kuwait` equals the Kuwait
  wall clock the test intended.

---

## 5. Applying the migration

1. **Pre-flight (read-only).** `python tools/timestamp_audit.py --preview 5`. It exits non-zero if
   any value would not parse (those rows are skipped by the migration, not blanked).
2. **Backup.** `python tools/backup.py` (or copy the SQLite file). Record the checksum.
3. **Register** migration 28 and set `SCHEMA_VERSION = 28` (§3). Deploy.
4. **Confirm.** `python tools/timestamp_audit.py` — every in-scope column's min/max must equal the
   pre-migration value minus 3 h, and `clock.to_kuwait(new)` must equal the old value.

## 6. Rollback

Kuwait has no DST, so the inverse is exact and per-column:

```sql
UPDATE "<table>" SET "<col>" = datetime("<col>", '+3 hours')
 WHERE "<col>" IS NOT NULL AND "<col>" <> ''
   AND datetime("<col>", '+3 hours') IS NOT NULL;
```

Run it in one transaction, then redeploy the previous release (whose readers/writers use Kuwait
wall clock) and remove the version-28 row from `schema_migrations`. Because the code and the data
move together, rollback is also a two-part change: restore the data, then the code.

## 7. Change history

- **v28 (prepared, unregistered):** `ATTENDANCE_UTC_COLUMNS`, `migration_28_*`, `clock` UTC helpers
  (`utc_now`, `to_kuwait`, `to_utc`, `kuwait_str_to_utc_str`), `tools/timestamp_audit.py`, tests.
