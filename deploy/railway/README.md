# The backend on Railway: the variables, the day-end line, and the check

The service is defined by the repository itself — `railway.json` (build `DOCKERFILE`, healthcheck
`/api/v1/status`), the `Dockerfile` it names, and `requirements.txt`. Nothing here is needed to
deploy it.

What this directory is for is the other question: a deployment that *runs* and a deployment that is
**configured** are not the same answer, and the application is the only thing that knows the
difference. It runs its own self-test at boot (the gate that refuses to serve on a fatal failure)
and publishes it at `GET /api/v1/readiness` — which keeps answering **200** while checks are
failing, because "the app is up" and "a feature is not what it says it is" are different sentences.
An uptime monitor cannot tell them apart; this script can:

```bash
python deploy/railway/verify_readiness.py                    # exit 0 clean, 1 advisory, 2 fatal, 3 unreadable
python deploy/railway/verify_readiness.py --token "$JWT"     # + the failing checks' reasons
```

## 0. What was measured on 2026-09-22

`verify_readiness.py` against `https://al-jehad-production.up.railway.app`: **exit 1**,
`ok=True degraded=True (37 checks, 3 failing)`.

| Check | Tier | What it means | What clears it |
| --- | --- | --- | --- |
| `worker_push_delivery` | advisory | `PUSH_ENABLED` is on and the channel still cannot send | §1 (the package) **and** §2 (the key pair) |
| `overtime_close_deferred` | advisory | the close is standing down: the stored alert line (8.1 h) sits *above* the paid day (8 h), so nothing is auto-closed at 8 h and the setting that says it is, no longer does that | §3 |
| `network_policy` | advisory | `X-Forwarded-For` is arriving from a peer that is not declared, so the admin allowlist is checking Railway's edge instead of the caller | §2, `TRUSTED_PROXIES` |

## 1. The image has to be able to send (why a rebuild is part of this)

Web Push needs two things that live in different places:

* **the key pair** — an environment setting an operator supplies in a minute (§2);
* **`pywebpush`** — a package, and therefore a *build* decision. It is deliberately not in
  `requirements.txt` (it is an optional extra that degrades: `push` imports it inside a function
  and reports its absence as the reason), and until recently the image did not install it at all,
  because `backend/requirements-optional.txt` was excluded from the build context.

The `Dockerfile` now installs `backend/requirements-optional.txt`, so the image can send. **A
running service does not pick that up until it is rebuilt** — a deploy from a commit that has the
change (connected repo → Deploy, or `railway up` from this checkout). Setting the variables before
the rebuild is not harmful; it just cannot pass, and the readiness body names the missing piece
(`... the optional pywebpush package is not installed`).

## 2. The service variables

`deploy/railway/variables.env` holds them, and is **not in git** (`.gitignore`): the private half
signs pushes as this deployment to every endpoint it has stored. Railway's *Variables → Raw
Editor* takes the file's contents as-is.

| Variable | Value | Why |
| --- | --- | --- |
| `PUSH_ENABLED` | `1` | the channel is on. `0` is a legitimate deployment (the inbox still records every notice) |
| `VAPID_PUBLIC_KEY` | 87 characters | the public half, served to a signed-in app by `GET /api/v1/worker/me/push` |
| `VAPID_PRIVATE_KEY` | 43 characters | signs each push. Never served, never logged, never in readiness output |
| `VAPID_SUBJECT` | `https://…workers.dev` | RFC 8292's contact (`sub`). A `mailto:` is the usual one; swap in a mailbox somebody reads |
| `TRUSTED_PROXIES` | `100.64.0.0/10` + loopback | Railway's edge is not loopback, so without this every worker shares one `15/minute` bucket and the audit log records the proxy as the actor |

`TRUSTED_PROXIES`: `GET /api/v1/admin/readiness` reports the peer the app actually sees under
`network_policy → value → forward_misuse.last_peer`, and `serve.py` prints the list it is using at
startup. Narrow the entry to that peer once you have looked at it rather than trusting a whole
private range on the strength of this file.

The variables were generated with the documented command (which is the same package the sender
uses, so the encoding cannot drift from what pywebpush reads):

```bash
cd backend && python -m push --generate-keys
```

**Rotating the pair invalidates every existing subscription** — each browser subscribed against
the old public key and has to enable notifications again.

The misuse counter that raises `network_policy` is per-process and is never reset inside one, so a
variable change (which redeploys) is also what clears it.

## 3. The day-end line is a database setting, not a variable

`overtime_close_deferred` is not about a missing value: it is about a *pair* of them disagreeing.
`overtime.scan_auto_close` closes a day when its paid hours reach `regular_hours` (8 by default),
and `overtime.scan_overtime` alerts at `overtime_notify_hours` (8.1 as shipped). The close deletes
the session it closed, so a shift the close ended can never be observed crossing the alert line —
which is why `shift_hours.day_end_rules` stands the close down when the line sits above the paid
day. The stored row on this deployment is `8.1 / 8.0`, so the close owns nothing:

* `overtime_alert_reachable` **passes** (the crossing is reported, at 8.1 h);
* `overtime_close_deferred` **fails**, because the rule an operator typed — *close the shift at 8
  paid hours* — no longer does that, and nothing else in the application would say so.

Move the alert line strictly below the paid day — **7.5 h**: the alert fires with half an hour left
of the standard day, and the close keeps owning the end of it. Through the console's shift-rules
screen, or the endpoint the console uses (the change is audited either way):

```bash
curl -sS -X POST https://al-jehad-production.up.railway.app/api/v1/admin/shift_rules \
  -H "Authorization: Bearer $ADMIN_JWT" -H "Content-Type: application/json" \
  -d '{"overtime_notify_hours": 7.5}'
```

`GET /api/v1/admin/shift_rules` echoes the rules back with a `day_end` block — the same verdict the
two checks read — so the console, the API and readiness cannot disagree about it.

The stored row wins over `migrations.DEFAULT_SHIFT_RULES` per column, and the row is what both
checks read: a fresh deployment with no row inherits 8.1/8.0 and gets the same advisory.

## 4. The advisory the keys can switch *on* — read this before the first restart

`worker_notice_backlog` answers "is the channel's output arriving", and while the transport is
unavailable it **passes** with `not measured: …`. The moment push becomes available it becomes a
real reading: notices whose `delivered_at` is still NULL past the 15-minute window
(`PUSH_MAX_AGE_MINUTES`), which is the window `dispatch` refuses to wake a phone for.

The notices that were written *before* push existed are in exactly that state and can never be
delivered — they are past the window by definition — and retention keeps an **unread** notice
whatever its age (`_sweep_worker_notifications`: an unread notification is a task nobody has done).
So on a deployment with history, configuring push can turn one advisory off and this one on. That
is not a fault, and it is not nothing either: it is the channel's history.

What to do about it, in order:

1. read the counts — `GET /api/v1/admin/readiness`, `worker_notice_backlog`'s `value` (`notices`,
   `no_device`, `with_device`) and its `detail`;
2. `with_device` first: a notice for a worker who *has* a live subscription and did not receive it
   is a delivery problem (`last_error` on that subscription), which is what the alert row's
   payload records;
3. `no_device` is the ordinary case for a channel just switched on: nobody has allowed
   notifications yet. A worker's app subscribes in the *Alerts* tab; once a device is subscribed,
   every **new** notice is delivered;
4. the historical rows drain as they are read and then age past `NOTIFICATION_RETENTION_DAYS`
   (180 by default). A deployment with no notices at all — a fresh site, or one where no shift has
   crossed the line — is green immediately.

## 5. Turning push off again

`PUSH_ENABLED=0` is a *pass*, not a warning: `worker_push_delivery` reports "push is switched off
(the worker inbox still records every event)" and `worker_notice_backlog` is not measured. The
inbox is unaffected — it is the record, and the push was only ever the shortcut to it. That is the
right configuration for a site that does not want phones ringing, and it is a one-variable change
away at any point.

## Files here

| File | What it is |
| --- | --- |
| `variables.env` | the values to paste into the service's variables. **Not in git** |
| `verify_readiness.py` | the deployment's own self-test, with an exit code per failing tier |
| `README.md` | this file: what each failing check needs, in the order that makes the first one the one to fix |
