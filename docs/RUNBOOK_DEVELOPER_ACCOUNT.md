# Runbook — the root `developer` account

`developer` is the one role above the administrators. It exists so that *somebody* can operate
the deployment — flip a runtime flag, read the infrastructure alert hub, drop a cache, triage
the punches the face band refused — without those powers living in an account an administrator
could create for themselves. This page is how that account is used, and how it is rotated or
taken away without breaking the thing it protects.

Everything here is checked against the code by `backend/tests/test_developer_runbook.py`: a
renamed route, a removed runtime key, a moved seeder flag or a changed id floor fails that
suite rather than quietly invalidating a step below.

## What the account is

- **One role, one account.** The role is `security.DEVELOPER_ROLE` (`"developer"`). The account
  is minted only by `backend/tools/seed_developer.py`; every API path that writes a `users` row
  refuses the role *by name* (`security.refuse_developer_role`, `security.UNASSIGNABLE_ROLES`),
  so no administrator — and no head admin — can create one or promote themselves into it.
- **An id in the root band.** `security.DEVELOPER_ID_FLOOR` is `309010000000`; the seeded
  default is `developer.DEVELOPER_ID_DEFAULT` = `309010401073`, name `Developer`. The band is
  deliberately far above `head_admin` (5000+): the id is the first thing read in a log line.
- **Concealed from below.** Every administrator-facing read is filtered by `developer.hides` /
  `developer.visible_rows` / `developer.visibility_clause`, so the account appears in no roster,
  no count and no audit list. A concealed id answers a missing one — the same `404` — so the
  concealment does not confirm the account exists. The developer sees itself.

## Signing in (routine use)

| Field | Value |
|---|---|
| URL | `https://al-jehad-production.up.railway.app` |
| **User ID** | `309010401073` |
| **Email / Phone** | *(leave blank — the account has neither)* |
| **Password** | held by the operator; **never** stored in the repo |

Login is `POST /api/v1/auth/login` with `{user_id, email_or_phone, password}`. The lookup is
`WHERE id = ? AND (email = ? OR phone = ?)`; this account's `email` and `phone` are empty, so an
empty `email_or_phone` satisfies it. A password set here must satisfy the deployment's own
policy (`security.validate_password_strength`): **at least 8 characters, at most 72 bytes, and
not in `security.WEAK_PASSWORDS`.**

Sessions are the 30-day persistent tokens of `docs/RUNBOOK_PERSISTENT_SESSIONS.md`. Rotation or
deactivation bumps `token_version`, which kills every outstanding token immediately — a 30-day
TTL is safe precisely because of that.

## What it can do

### The superset — everything an administrator or a worker can

The root tier is a **superset**, and that is one line in `security.require_role`:

```python
if current.role not in allowed and not current.is_developer:
    raise HTTPException(status_code=403, ...)
```

Because `ADMIN_ROLES` includes `developer` and that wildcard is evaluated in the guard rather
than repeated in fifty route declarations, a developer session satisfies `admin_only`,
`head_admin_only`, `any_authenticated` and every worker route — including routes written before
the role existed. It is strictly above `admin`: the admin-targeted guards test
`role == "admin"` (`security._guard_standard_admin`), which a developer is not, so the
restrictions that bind a standard admin do not bind it. On the handset side,
`UI.handsetRoles = ['admin', 'developer']`, so the worker clock-in UI opens for it.

The reverse is not true and is the point of the tier: a route built from `require_developer`
refuses every administrator, because their role is not in the set and they are not the
developer.

### The surfaces only the root tier can reach

Every route below is guarded by `security.require_developer`. All are mounted under `/api/v1`.

| Method | Route |
|---|---|
| `GET` | `/api/v1/developer/notifications` |
| `POST` | `/api/v1/developer/notifications/{notification_id}/acknowledge` |
| `POST` | `/api/v1/developer/notifications/{notification_id}/read` |
| `GET` | `/api/v1/developer/runtime` |
| `PATCH` | `/api/v1/developer/runtime/{key}` |
| `GET` | `/api/v1/developer/alerts` |
| `POST` | `/api/v1/developer/alerts/{alert_id}/read` |
| `GET` | `/api/v1/developer/diagnostics/pool` |
| `GET` | `/api/v1/developer/diagnostics/slow-queries` |
| `GET` | `/api/v1/developer/diagnostics/query-plan/{name}` |
| `POST` | `/api/v1/developer/diagnostics/caches/{name}/flush` |
| `POST` | `/api/v1/developer/diagnostics/caches/flush` |
| `GET` | `/api/v1/developer/audit` |
| `GET` | `/api/v1/developer/refused-punches` |
| `GET` | `/api/v1/developer/refused-punches/{refusal_id}/frame` |
| `POST` | `/api/v1/developer/refused-punches/{refusal_id}/clear` |

The deployment's own notification queue moved here from `/admin/notifications` (which is gone,
not filtered): a forced start, a schema repair, a retention sweep and a coverage verdict are the
*host's* events, and a site administrator has no action to take on any of them. One reader, so
nothing is withheld and the counts mean exactly what the list shows.

### Runtime store

`developer.RUNTIME_KEYS` is a **closed vocabulary**: a key nothing reads is refused rather than
stored, because an operator who sets a key that does nothing cannot tell. Changing a value bumps
a shared version counter in one transaction, and every worker re-reads on its next request
(`developer.runtime_snapshot`) — "flip the flag, no restart, all workers agree", not aspirational.

| Key | Type | Default | What it does |
|---|---|---|---|
| `maintenance_mode` | flag | `false` | Refuse authenticated writes with 503 while the database is worked on; reads, sign-in and the readiness probe stay served. |
| `log_level` | level | `INFO` | Root logger level, applied to this process now and to every other worker on its next read. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `auth_anomaly_alerts` | flag | `true` | Whether repeated credential failures are raised into the alert hub. Off is legitimate behind a scanner that trips it hourly. |

### The alert hub

`developer.ALERT_KINDS` is the hub's vocabulary — also closed, so a filter cannot silently match
nothing. `POST /api/v1/developer/alerts/9/read` acknowledges one.

| Kind | Raised when |
|---|---|
| `startup_degraded` | The deployment started with advisory checks failing. |
| `schema_repair` | The schema was repaired rather than refused, at startup. |
| `retention_sweep` | An automated retention sweep erased data, or could not. |
| `coverage_report` | The standing detector-coverage comparison changed verdict. |
| `db_pool_saturation` | Connections or lock waits past what this deployment is sized for. |
| `db_lock_contention` | SQLite refused a write after `busy_timeout`: a punch may have failed. |
| `slow_query` | One statement took longer than the configured threshold. |
| `rate_limit_spike` | A client is being refused repeatedly: a misconfigured app, or a scan. |
| `unhandled_exception` | A request raised past every handler. Carries the trace id. |
| `auth_anomaly` | Repeated credential failures against the same account. |
| `startup_override` | The deployment was forced up past a failing self-test. |
| `capacity_refusal` | Face verification refused a request for room — a site arriving at once. |
| `developer_account_seeded` | The root account was created, or its credential was rotated. |

The hub never fails its caller: `developer.raise_alert` swallows every error, and its dedupe key
collapses repeats (one row per window, so a saturated pool writes one alert an hour, not one per
tick). The separation is structural — `developer_alerts` is its own table so no
administrator-facing query can leak it by forgetting a filter.

### Diagnostics

Honest, narrow tools rather than invented statistics. `GET /api/v1/developer/diagnostics/pool`
reports what "pool health" means for a single-writer SQLite deployment (journal mode, busy
timeout, lock waits and timeouts, page count) and a `saturated` verdict.
`GET /api/v1/developer/diagnostics/slow-queries` returns the recorded statements — a verb, a
duration and a trace id, **never the SQL text**, because statement text in a database layer can
carry a worker id.

Named queries may be explained on demand from an allowlist (`developer.EXPLAINABLE`) — this is
what replaces `pg_stat_statements`, which SQLite does not have:

| Name | Asks |
|---|---|
| `roster` | The roster scan. |
| `audit_recent` | The most recent audit rows. |
| `unread_notifications` | Unread deployment notifications. |
| `open_notes` | Open worker notes. |
| `pending_reviews` | Punches waiting for a decision. |

`developer.CACHES` is the honest, short list of caches that really exist; each entry has a
reader, so flushing it does something. `POST /api/v1/developer/diagnostics/caches/{name}/flush`
drops one by name, and `POST /api/v1/developer/diagnostics/caches/flush` drops all of them:

| Cache | What dropping it does |
|---|---|
| `runtime_config` | This process's copy of the runtime store; the next read re-reads it. |
| `rate_limit_buckets` | The in-process rate-limiter's counters (slowapi's storage). |

### The audit stream

`GET /api/v1/developer/audit` is the raw, appended history — an incident is reconstructed from
it, so it neither summarises nor hides the developer's own actions from the developer. It
projects a named field set (`SELECT *` is never used) and carries **no hash and no token**. The
`security_actions` it highlights are `developer.SECURITY_ACTIONS`: `login`, `login_failed`,
`password_reset`, `password_change_self`, `user_create`, `user_delete`, `user_edit`,
`user_status`, `role_change`, `startup_override`, `notification_acknowledge`, `retention_sweep`,
`biometric_enroll`, `review_approve`, `review_reject`. Trace ids link an audit row to the alert
raised for the same failure.

### Refused punches

The triage surface for the face band, and the only place a worker's refused face is shown to a
reader — read-only evidence in either case; a refused punch was never attendance and there is no
approve path. `GET /api/v1/developer/refused-punches` defaults to today and caps at a week
(`developer.REFUSED_PUNCH_MAX_DAYS`); `.../{refusal_id}/frame` returns the downscaled frame;
`.../{refusal_id}/clear` drops one row from the list (the evidence stays on disk until retention
sweeps it, and the clear is audited as `refused_punch_clear`).

### The console

Signed in as the developer, the web console offers two tabs no administrator sees — both marked
`rootOnly` in `frontend/frontendjavascript.js`, so they are omitted entirely rather than drawn
disabled:

- **Developer** — the runtime flags, the alert hub, diagnostics (pool, slow queries, query
  plans, cache flush) and the audit stream in one panel.
- **Alerts** — the deployment notification queue (`/api/v1/developer/notifications`) with acknowledge.

The worker clock-in UI is also open (`UI.handsetRoles`).

## Routine operations

```bash
HOST=https://al-jehad-production.up.railway.app

# Sign in and keep the token for the session
TOKEN=$(curl -s -X POST "$HOST/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d '{"user_id":"309010401073","email_or_phone":"","password":"<password>"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# What is set right now, and the version every worker shares
curl -s "$HOST/api/v1/developer/runtime" -H "Authorization: Bearer $TOKEN"

# Open a maintenance window (values are typed by the key)
curl -s -X PATCH "$HOST/api/v1/developer/runtime/maintenance_mode" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"value": true, "note": "db vacuum, ticket 1234"}'

# Close it — and confirm it is closed before you walk away
curl -s -X PATCH "$HOST/api/v1/developer/runtime/maintenance_mode" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"value": false, "note": "vacuum done"}'

# Unread infrastructure alerts, then acknowledge one
curl -s "$HOST/api/v1/developer/alerts?unread_only=true" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$HOST/api/v1/developer/alerts/9/read" -H "Authorization: Bearer $TOKEN"

# Database diagnostics
curl -s "$HOST/api/v1/developer/diagnostics/pool" -H "Authorization: Bearer $TOKEN"
curl -s "$HOST/api/v1/developer/diagnostics/slow-queries?limit=20" -H "Authorization: Bearer $TOKEN"
curl -s "$HOST/api/v1/developer/diagnostics/query-plan/roster" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$HOST/api/v1/developer/diagnostics/caches/flush" -H "Authorization: Bearer $TOKEN"
```

## Rotating the credential

Rotation is the routine hygiene step, and it is also the fastest way to revoke every live
session. Run it inside the deployment (Railway: `railway ssh`, which lands as root; the database
is the volume file `/data/times.db` and the repo is `/app`):

```bash
DEVELOPER_PASSWORD='<new password>' python /app/backend/tools/seed_developer.py --rotate-password
```

What it does, in one transaction: re-hashes the credential, **bumps `token_version`** (so every
outstanding token for the account stops verifying), writes a `developer_account_seeded` alert
(`warning`) and logs the acting operator. It does **not** change the id, the name or anything
else, and it does not disturb the password hash of any other account.

- The password is read from `$DEVELOPER_PASSWORD` and is deliberately **never** a command-line
  argument — an argument lands in shell history, `ps` output and CI logs.
- Re-running the seed **without** `--rotate-password` is safe and writes nothing to the
  credential; that is what lets a deploy pipeline run it on every release without silently
  restoring a password an operator has since changed.
- To invent a password instead of choosing one (printed once, then only its hash is stored):
  `python /app/backend/tools/seed_developer.py --generate --rotate-password`.
- To target a different id or get a machine-readable answer:
  `--id <id> --json`.

The password policy of `security.validate_password_strength` applies: ≥ 8 characters, ≤ 72
bytes, and not in `security.WEAK_PASSWORDS`.

## Revoking access

Three levels, from "their tokens are dead now" to "the account is gone". The account has no
attendance history, so all three are safe; the append-only `audit_log` keeps the record of
everything that happened either way.

### 1. Kill live sessions without changing anything else

Rotate the password (above). `token_version` is compared on every request
(`security.get_current_user`), so `/auth/refresh` cannot renew past the bump. The account can
sign back in with the new password.

### 2. Suspend the account (stop sign-in, keep the row)

```bash
sqlite3 /data/times.db \
  "UPDATE users SET status = 'inactive', token_version = COALESCE(token_version, 0) + 1 \
   WHERE id = '309010401073';"
```

Login then refuses it (`/auth/login` answers **403**, "This account has been deactivated"), and
every outstanding token answers **401** because the version moved. Reactivating — for the rare
case where this was a mistake — is the reverse, but **note the version is not restored**, so
sessions stay dead and the account signs in again:

```bash
sqlite3 /data/times.db "UPDATE users SET status = 'active' WHERE id = '309010401073';"
```

*(The API's `/api/v1/admin/users/status` can deactivate accounts, but the developer's id is
concealed from every administrator read, and this is a root-tier action: do it from the
operator's shell, not through a console that should not be able to see the row.)*

### 3. Retire the account (remove it)

The seeder can always mint a fresh one at the same id, so removal is clean:

```bash
# It has no attendance history; this is what makes the delete safe.
sqlite3 /data/times.db "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = '309010401073';"
sqlite3 /data/times.db "DELETE FROM users WHERE id = '309010401073';"
```

Deleting the row un-conceals the id automatically — `developer.concealed_ids` reads the role
from the database on every call — and the next `--generate` recreates the account with a new
credential. Do this only when the deployment is being handed over or the tier is being
withdrawn; for anything less, suspend.

## Verification

After any of the above, walk through these on the live host:

```bash
HOST=https://al-jehad-production.up.railway.app

# 1. The credential: 200 with the new password, 401 with the old one
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$HOST/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"309010401073","email_or_phone":"","password":"<password>"}'

# 2. The tier: /developer/* answers 200 with a fresh token, and 401 without one
curl -s -o /dev/null -w '%{http_code}\n' "$HOST/api/v1/developer/runtime"
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  "$HOST/api/v1/developer/runtime"

# 3. If suspended or retired: login is 403 (suspended) or 401 (gone),
#    and an old token is refused with 401 on every route.
```

Then confirm the deployment is running with `maintenance_mode` **off** (`GET
/api/v1/developer/runtime`) and that no unread `auth_anomaly` or `db_pool_saturation` alert is
waiting in `/api/v1/developer/alerts?unread_only=true`.

## What not to do

- **Do not create the account through the API.** Every creation path refuses the role by name;
  a "developer" an administrator can create is the escalation this tier exists to prevent.
- **Do not put the password in the repository, a command-line argument, or a shared document.**
  Choose it into `$DEVELOPER_PASSWORD` for the one command that needs it, and store it in a
  password manager.
- **Do not leave `maintenance_mode` on.** It refuses authenticated writes with 503 for everyone;
  a forgotten flag looks exactly like a broken deployment.
- **Do not treat concealment as authorization.** It keeps the account out of administrator
  screens; the real control is that only the operator with shell access can create, rotate or
  remove it.
- **Do not hand the credential around.** It reads the entire audit trail, the infrastructure
  alert hub and the refused-punch evidence including faces. One account, one operator.
