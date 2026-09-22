# Runbook: forcing a start past a failing self-test

The application runs a self-test at startup and **refuses to serve** when it finds a fault it
cannot live with. That is deliberate: a deployment that starts anyway has punches, roles and
pay figures of unknown quality, and a silently half-working installation is worse than an
obvious outage.

`STARTUP_OVERRIDE_REASON` is the one deliberate exception. It **fixes nothing**. It records
that a human being chose to serve anyway, and that choice is written to the log, to the
console's alert queue and to every readiness surface. Two of the checks can never be
overridden, and a forced deployment is never reported healthy - read the whole of this page
before you use it.

## What the self-test is

`backend/readiness.py` runs the checks in the app's lifespan, **after** `init_db()` has
applied migrations and **before** the first request. Three tiers:

| tier | what happens |
| --- | --- |
| **fatal** | refuse to serve: the failing checks are logged and the process stops |
| **repairable** | one additive, idempotent repair is attempted (shift rules, `journal_mode`), then the checks run again |
| **advisory** | serve, and report `degraded` - a legitimate deployment may differ from ours |

Without an override, a fatal failure is loud and final:

```
[startup] FATAL: schema_current: database is at schema 18, expected 19: migrations have not been applied
```

then `RuntimeError: startup self-test failed, refusing to serve traffic: ...`, and uvicorn exits
**3** with the port never bound. In a container that is a crash loop with exit code 3; on a host
it is a service that will not come up. Both are the intended behaviour.

## When it is legitimate

The rule has two halves.

1. **It defers a fault you have already measured.** You must be able to say what the failing
   check protects and what you accept losing by serving without it. "I have not read the detail
   line" is not a reason to override; it is a reason to read the detail line.
2. **It must never cover for a build whose guarantees you cannot verify.** The mechanism accepts
   almost any reason. The judgement is not the mechanism's.

Legitimate, in practice:

* **A ledger that is short while the schema is complete.** The designed case: the migration
  could not be *recorded* (a locked table, a full disk during a deploy) but every column the
  code reads already exists. Check that the `FATAL:` line names a migration, and that the
  columns it adds are present.
* **A window you would otherwise lose, with the fix already scheduled.** Give it a deadline
  (`STARTUP_OVERRIDE_UNTIL`), serve the shift, and fix it before the deadline - the next start
  after that moment refuses like any other.
* **A network policy that will not build.** The admin gate then matches nothing and locks every
  administrator out of the console; the worker-facing API is unaffected, which is why the check
  itself names the override as the hatch for this one.

Never legitimate:

* **to make readiness green** - it cannot, by design (see below);
* **as a permanent setting.** An override left in `.env` silently forces every future restart,
  which is the difference between an incident and a bypass;
* **for the checks marked *not in judgement*** in the table below;
* **for a fault the override cannot change at run time.** `liveness_anti_spoofing` in
  `enforce` mode is the case in the table: the same condition that failed the self-test refuses
  every punch once the process is up, so the override starts a deployment that records nothing.
* **for a fault you cannot see in the log.** If there is no `[startup] FATAL:` line, the
  override is not the tool - see Troubleshooting.

## The two checks no override can cover

`readiness.NON_OVERRIDABLE` is exactly `secret_key_configured` and `database_reachable`. If
either fails, the gate refuses **whatever the reason says**, because there is no degraded mode
to degrade into: a deployment with no signing key signs tokens any reader of the repository
could forge, and a deployment with no database records no attendance at all.

A related trap: `init_db()` runs *before* the gate, so a database that cannot be opened,
created or migrated usually fails there first - with a traceback and no `[startup]` line at
all. At that point there is nothing for an override to cover; fix the database.

## The fatal checks, and what forcing each one means

| check | can an override cover it? | what forcing it means |
| --- | --- | --- |
| `secret_key_configured` | no - never | Refused whatever the reason. Without a signing key every token is forgeable, and the app cannot reach the gate with none at all - `python -m config` refuses first. |
| `database_reachable` | no - never | Refused whatever the reason. There is no mode in which attendance is served from no database. |
| `database_writable` | yes | Reads work and writes do not: punches, closes and notifications all fail. You are serving an API that answers and records nothing. |
| `schema_current` | yes | The designed case is the code being *ahead* of the ledger with the columns already present. A database from a **newer** build is overridable too, but nothing the code reads has been verified against that schema - roll the code forward instead. |
| `migrations_all_applied` | yes | The same shape: a migration that could not run. Confirm the columns it adds exist, or the first punch that needs one fails. |
| `schema_drift` | yes | The guard found something it will not repair silently: a column whose type changed, a `NOT NULL` added to a live table. A drift on a column the code *writes* means rows being stored wrong, not a slow boot. |
| `password_hashing_ok` | yes | The service starts and nobody can sign in - every password check fails. Pointless rather than dangerous. |
| `jwt_roundtrip_ok` | yes | The same: no session can be minted or validated, so no role can act. |
| `auth_enforced_on_admin_routes` | yes, but not in judgement | The admin surface is not guarded the way this build claims. That is a security invariant, not a fault to defer - roll back. |
| `api_routes_authorised` | yes, but not in judgement | A route answers without the token its owner requires. The README's route-audience section is the contract being broken. |
| `no_unprefixed_duplicate_routes` | yes, but not in judgement | A route is reachable under a path nobody reviewed - the same class as above, in a different shape. |
| `face_match_band` | yes | Every punch is judged by a band you did not choose. Read "Where a face match is decided" before accepting that for a shift. |
| `network_policy` | yes | Fatal only when the policy **will not build** (an allowlist entry that is not an address): the gate matches nothing and refuses every administrator. The worker-facing API is unaffected. |
| `liveness_anti_spoofing` | yes | Fatal only in `LIVENESS_MODE=enforce` with no usable model or runtime and `LIVENESS_ALLOW_UNAVAILABLE` unset - a configuration that would refuse **every** genuine punch. This is the one row where forcing changes nothing: the gate that refused to start refuses each punch instead, so overriding it buys a running API that accepts no attendance. Fix the model, switch the mode, or set `LIVENESS_ALLOW_UNAVAILABLE=1` to accept unverified frames knowingly. |

## How to set it

```bash
# A reason is required. A bare flag is refused, because an unattributed hatch is
# indistinguishable from a permanent bypass.
STARTUP_OVERRIDE_REASON="shift starts 07:00; migrations blocked by a locked table, fix scheduled 14:00"
# Optional, and strongly recommended: after this instant the override is ignored again.
STARTUP_OVERRIDE_UNTIL=2026-09-21T14:00:00+02:00
```

* `STARTUP_OVERRIDE_REASON` is read when settings are built, and lands in the log line and in
  the alert body. Keep it short and specific: what is broken, and when it will be fixed.
* `STARTUP_OVERRIDE_UNTIL` is compared against the clock **at each startup**. With an offset
  (`+02:00`) it is an absolute instant; without one it is read as local time on the host. A
  value that will not parse counts as **expired**, so the override is inactive - the
  fail-closed direction.
* Set both in the deployment's environment, or in `../.env` for one start. Never commit them.

## What a forced start looks like

The startup log, in this order:

```
[startup] FATAL: schema_current: database is at schema 18, expected 19: migrations have not been applied
[startup] OVERRIDE ACTIVE: serving despite ['schema_current'] - reason: shift starts 07:00; migrations blocked ...
[startup] DEGRADED: ['database_path_is_project_root', 'worker_push_delivery']
[startup] self-test complete in 812ms: 33/36 checks passed (degraded)
```

Two rows in `admin_notifications`, which is the console's alert queue:

```sql
SELECT id, kind, severity, title, body, read_at, acknowledged_at, acknowledged_by,
       acknowledgement_note, dedupe_key
  FROM admin_notifications
 WHERE kind IN ('startup_override', 'startup_degraded') ORDER BY id DESC;
```

* `startup_override` - severity `critical`, **unread and unacknowledged**, with the failing check
  names **and your reason** in the body. It stays unanswered until an administrator accepts it
  (see *Answering a forced start* below), and readiness keeps reporting that;
* `startup_degraded` - severity `warning`, naming the advisory checks that failed.

The override alert is one row **per reason**, not per restart: a restart during the same
incident reuses the row, so an acknowledgement already given for it is not thrown away. A
*different* reason opens a new, unanswered row - an acceptance given for last month's reason
must not answer this month's. Every individual start is its own `audit_log` row, because "how
often has this deployment been forced up" is a question the alert row cannot answer.

The readiness surfaces stay honest. `GET /api/v1/readiness` keeps answering **503** for as long
as any fatal failure stands - the override buys traffic, not a clean bill of health - and
`GET /api/v1/admin/readiness` reports `hardened_build_live: false` with
`admin.startup_override_active: true`. Inside the process the same fact is on the report as
`override_used`, which is what makes the run count as degraded. The **unaccepted** override is
an advisory on those same surfaces (`startup_override_acknowledged`), so "this was forced up and
nobody has accepted it" is visible to a monitor without anybody opening a database - and
answering it is a visible state change rather than a private one.

## Verify what you forced

```bash
cd backend
python -m config
# prints `startup_override_active: True` plus the settings the process actually resolved

curl -s -o /dev/null -w '%{http_code}\n' https://<origin>/api/v1/readiness
# 503 while a fatal failure stands. A 200 here means the override was not needed.
```

Then read what an administrator sees:

```bash
curl -s -H "Authorization: Bearer <admin token>" https://<origin>/api/v1/admin/readiness
```

What to check: `admin.startup_override_active` is `true`; `failed_checks` names exactly the
checks you meant to accept; if you forced a schema check, read `schema.current` against
`schema.expected` and the migration inventory beside it.

## Answering a forced start

A forced start is a decision that lets the deployment serve code its self-test refused. The
*reason* is demanded to open the hatch (`STARTUP_OVERRIDE_REASON` - a bare flag is refused);
the **acknowledgement** is what says somebody accepted it afterwards, in their own words. Until
one exists, readiness reports the deployment as having an unanswered question about it.

* `startup_override_acknowledged` (advisory) fails while the newest `startup_override` alert has
  no acknowledgement, naming the reason and the alert id. It passes the moment one exists, and on
  a deployment that has never been forced up it passes saying so.

Answer it from the console's **Alerts** tab - the row carries the reason and a box for yours -
or from the API:

```bash
curl -s -X POST \
  -H "Authorization: Bearer <admin token>" -H 'Content-Type: application/json' \
  -d '{"note": "Read the ledger by hand: the migration is recorded, the column is there."}' \
  https://<origin>/api/v1/admin/notifications/{notification_id}/acknowledge
```

Substitute the alert's `id` for `{notification_id}` - the readiness detail names this endpoint
with the number already in it, which is the quickest way to get it right.

The note is required and is vetted as plain text: `<script>` and encoded markup are refused
(`422`), an empty note is refused, and a second answer is refused with **409** naming who
answered first and when - the first acceptance is the one on the record, because rewriting it
would leave the trail describing a decision nobody made.

What it writes, and where:

```sql
SELECT action, actor_id, actor_role, entity, entity_id, after_json, created_at
  FROM audit_log WHERE action = 'notification_acknowledge' ORDER BY id DESC LIMIT 5;
```

* the alert row's `acknowledged_at`, `acknowledged_by` and `acknowledgement_note`, and its
  `read_at`/`read_by` (an alert somebody has accepted has by definition been seen);
* an append-only `audit_log` row - actor, role, the note, the alert's kind and title, and the
  request's address and user agent. That is the record that answers *who decided this, and why*,
  and it is the one that survives the alert being swept by retention;
* the readiness check stops failing, so the next person to look at the monitors sees a question
  that has been answered rather than one that reads as nobody looking.

Two things an acknowledgement is **not**:

* **it is not a fix.** The deployment is still serving past a failing check, and
  `/api/v1/readiness` still answers **503** while the fatal failure stands. Acknowledge the
  decision *and* do the work below;
* **it is not the same as reading.** Marking read records that somebody looked; that is the
  right answer for an informational alert (a retention sweep, a note a worker wrote) and the
  wrong one for this. Reading an override and accepting it are two different acts, and both are
  visible separately on the row.

## Getting back to a healthy deployment

1. **Fix the fault.** The `[startup] FATAL:` detail names it. For a schema fault, migrations are
   applied by the app's own startup (`init_db()` calls `migrations.initialize`) - there is no
   migration command to run. Remove whatever prevented them: a lock held by another process, a
   read-only volume, no free space. Then restart.
2. **Remove `STARTUP_OVERRIDE_REASON` and `STARTUP_OVERRIDE_UNTIL`** from the environment and
   from `../.env`. Do it *before* the restart: an override left in place is a deployment that
   will never refuse again.
3. **Restart, and confirm it is genuinely healthy.** `python -m config` prints
   `startup_override_active: False`; `/api/v1/readiness` answers **200**; the log has no
   `OVERRIDE ACTIVE` line and ends in a plain `self-test complete ... checks passed`.
4. **Answer the alert** - acknowledge it with what you actually checked (see *Answering a forced
   start* above), then leave it there: it is the record of what was served, and the audit row
   beside it is the record of who accepted it. Marking it merely read leaves it reported as an
   unanswered decision.
5. **Confirm the deployment does what it promises** before walking away: `python -m retention`
   (report only, it deletes nothing) for the sweeps, and the console's own screens for whichever
   feature you were worried about.
6. **If the fault cannot be fixed, roll the build back** to the previous deployment instead of
   leaving a forced start running. The override is a bridge between two healthy states, not a
   destination.

## Troubleshooting

| symptom | what it means | what to do |
| --- | --- | --- |
| exits 3 and no `OVERRIDE ACTIVE` line | the reason is missing or expired - or a non-overridable check failed | read the `FATAL:` lines. `secret_key_configured` and `database_reachable` cannot be overridden; otherwise re-set the reason and the deadline |
| exits 3 with a traceback and no `[startup]` line | `init_db()` failed before the gate ran | fix the database: path, permissions, free space. The override is never reached |
| readiness still 503 after the fix and the restart | the override is still exported, or a different check is failing now | `python -m config`, then `failed_checks` in the admin body |
| `OVERRIDE ACTIVE` names more checks than you expected | one reason covers every fatal failure at once | read them. If any is marked *not in judgement* above, roll back instead |
| readiness answers 200 while the override is still set | the faults have cleared | remove the variables and restart anyway - otherwise the next fault is forced silently |
| the alert is not in the console | someone marked it read, or retention removed it past `NOTIFICATION_RETENTION_DAYS` | the startup log still has the line, and `admin_notifications` keeps the row until retention does |
| nobody can remember why it was forced | the reason was never recorded | it cannot happen - a start with no reason is refused. If the log line carries no reason, this override was not what started the process |
| readiness says `startup_override_acknowledged` is degraded | the forced start has not been accepted by anybody | that is the check working: open the console's Alerts tab and acknowledge it with what you checked, or remove `STARTUP_OVERRIDE_REASON` and fix the fault so there is nothing left to accept |
| acknowledging answers `409` | somebody answered it first | the refusal names who and when. The first acceptance is the one on the record - add a second note to the ticket, not a second answer here |
| the alert shows an answer nobody remembers giving | the reason was reused, and the row with it | the alert is keyed per reason, so the same reason reuses its row (and its acceptance). `audit_log` has one `startup_override` row per actual start - read those |

## What this runbook is held to

`backend/tests/test_startup_override_runbook.py` reads this file and refuses a drift between it
and the code: every `STARTUP_OVERRIDE_*` variable the code reads is documented here and every
one named here is a variable the code reads; every check the code can make fatal - whether the
fatal tier is written literally or decided at run time, as `liveness_anti_spoofing`'s is - has a
row in the table above, with the rows marked *never* being exactly
`readiness.NON_OVERRIDABLE`; the
endpoints, tables, columns, notification kinds, severities and commands named above exist; both
queries above have their columns compared with the migrations that create them; the
acknowledgement flow named above is the one the code implements - the `audit_log` action an
operator is told to look for is one the acknowledge handler writes, the advisory check named in
*Answering a forced start* is a check `readiness.py` declares, and the endpoint that check
reports really does carry the alert's own id; the two commands above are *run* and their output
checked; and the exit code and the expiry rule described here are measured - uvicorn is started
with a lifespan that fails the way the gate does, and the settings property is exercised for
each of the three deadline cases. A renamed check, a changed severity, a moved route or a
renamed audit action fails a test rather than sending an operator after a knob that no longer
exists.
