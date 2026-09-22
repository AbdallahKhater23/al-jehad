# Runbook: turning on worker push

The application records every worker notice in `worker_notifications` and the handset's
*Alerts* tab is that record - it is there on a deployment with no push at all, and it is the
thing a worker reads. Push is the other half: the same sentence, delivered to a phone whose
app is **closed**. Without it a worker who was told their shift ran long at 17:20 finds out
when they next open the app; with it their phone buzzes.

This is the operator's side. The worker's side is one switch on the profile tab, and it only
offers itself when this runbook has been done - the app asks the server first, and a server
with no keys is never asked for a permission it cannot use.

Read [*Where the server is allowed to send*](../README.md#where-the-server-is-allowed-to-send)
before you widen `PUSH_ENDPOINT_HOSTS`: a subscription endpoint is a URL a worker supplies
that this server POSTs to, which is a request-forgery primitive with a retry, and the
allowlist is the control.

## What changes when you turn it on

| | without push | with push |
| --- | --- | --- |
| where the notice lives | `worker_notifications`, read in the *Alerts* tab | the same row, unchanged |
| when the worker learns of it | the next time they open the app | within a minute, on a locked phone |
| what `readiness` says | advisory `worker_push_delivery` fails, with the reason | advisory passes |
| what is at risk | nothing - the record is the record | a deployment secret (the private key) and a stored endpoint per device |

Nothing is lost without push, which is why a missing key pair is an **advisory** and not a
fault: the server starts, punches, and closes days exactly as before. `PUSH_ENABLED=0` is a
pass rather than a warning - an operator who decided nobody's phone should ring has got what
they asked for.

## Prerequisites

1. **An HTTPS origin.** `navigator.serviceWorker` and `PushManager` require a secure context.
   A phone reaching the app at `http://192.168.1.20:8000` cannot subscribe, and the settings
   card will say so rather than fail silently. (`localhost` is the browser's exception, which
   is why a desktop test at the default origin works.)
2. **The optional sender package.** `pywebpush` is deliberately not a hard requirement - it is
   listed in `backend/requirements-optional.txt`, and a deployment that does not install it
   starts normally with `transport_available()` naming it as the reason:

   ```bash
   cd backend
   pip install -r requirements-optional.txt
   ```

3. **A browser that supports service workers**, per the phone in question. The card reports
   `pushSwUnsupported` when it does not, and the inbox still works.

## Step 1 - generate the VAPID key pair

```bash
cd backend
python -m push --generate-keys
```

The command uses the optional dependency rather than hand-rolling the encoding: a VAPID key
pair is EC P-256, and "almost right" key encoding is the kind of bug that works against one
push service and fails against the next. If the package is missing it prints the `pip install`
line instead - install it and re-run. Its output is two lines:

```
VAPID_PUBLIC_KEY=<the public half>
VAPID_PRIVATE_KEY=<the private half>
```

The public half is a 65-byte uncompressed P-256 point in base64url with the padding stripped,
so it is about 87-88 characters. **Compare the length before you deploy**: a truncated paste
is accepted by this server, served to the browser, and then refuses at subscribe time with a
browser-side error that names nothing useful.

Two properties of the pair are worth stating plainly:

- **the private half is a deployment secret.** Whoever holds it can sign pushes as this
  deployment to every endpoint it has stored. It goes in the host's environment, never in the
  repository, the frontend, a log, an issue or a screenshot.
- **rotating the pair invalidates every existing subscription.** Each browser subscribed
  against the old public key. Rotate deliberately, and expect every device to re-enable.

## Step 2 - set the variables

| Variable | Default | What it does |
| --- | --- | --- |
| `VAPID_PUBLIC_KEY` | *(none)* | the public half; served to a signed-in app by `GET /api/v1/worker/me/push` |
| `VAPID_PRIVATE_KEY` | *(none)* | the private half; signs each push. Never served, never logged, never in readiness output |
| `VAPID_SUBJECT` | `mailto:admin@example.invalid` | the `sub` claim sent to the push service - the contact address a provider can reach you at. Set a real `mailto:` or `https:` URL |
| `PUSH_ENABLED` | `1` | `0` switches the channel off entirely: no subscribe, no send. The inbox is unaffected |
| `PUSH_MAX_AGE_MINUTES` | `15` | a notice older than this is not worth waking a phone for. It stays in the inbox; it is not an error |
| `PUSH_ATTEMPT_LIMIT` | `3` | how many times a notice may be attempted before it stops being retried |
| `PUSH_ENDPOINT_HOSTS` | the provider list in `backend/config.py` | the push services this server will POST to. Add your own provider here; every other host is refused at subscribe time **and** on the send path |

Where they go: the host's environment (Railway: *Variables*), or the repository-root `.env`
that `python -m config --write-env` lays down. A real environment variable wins over `.env`
(`load_dotenv(override=False)`), which is what lets a host inject the private key without it
ever reaching disk in the checkout.

Then check the configuration itself:

```bash
cd backend && python -m config
```

It exits non-zero with a named reason if anything is malformed; `Configuration OK (secret
fingerprint ...)` is the line you want.

## Step 3 - restart, and read the server's own answer

On the next start the startup gate runs the same checks the readiness route publishes.

- **the startup log** loses its `[startup] DEGRADED: [...]` line for `worker_push_delivery`.
  While it is unconfigured you also get an administrator notification (`Server started
  degraded`), so an operator who never runs this runbook is told the phone half is missing
  rather than discovering it from a worker's complaint.
- **`GET /api/v1/readiness`** (no session) carries the verdict: `degraded_checks` should no
  longer name `worker_push_delivery`, and `push_endpoint_allowlist` should be `ok`. That check
  is why a mistyped host in `PUSH_ENDPOINT_HOSTS` is caught at startup rather than by a worker
  whose switch says "on" and whose phone stays silent.
- **`GET /api/v1/admin/readiness`** (administrator token) adds each check's `detail` and
  `value`. For `worker_push_delivery` expect `available: true`, `enabled: true`,
  `public_key_configured: true`, `private_key_configured: true`. Those are **booleans, never
  the values** - readiness output is logged, mailed and pasted into issues, and if the key
  itself ever appears in it, that is a bug, not a convenience.
- **`GET /api/v1/worker/me/push`** with a worker's token is the answer the app acts on:
  `available: true`, `reason: "ready"`, `public_key` present, and `subscriptions` counting that
  worker's live devices.

## Step 4 - subscribe one device

1. Sign in as that worker on the phone, over HTTPS.
2. Open the profile tab. The *Push notifications* card reads `GET /api/v1/worker/me/push` and
   names its state: an offer to enable, the **server's own reason** it cannot, or a browser too
   old to register a worker. It offers a switch only where one can work.
3. Tap *Enable*, allow the browser prompt. The card says on. `subscriptions` becomes `1`.
4. Confirm the row on the server:

   ```sql
   SELECT id, worker_id, user_agent, created_at, revoked_at, last_error
   FROM worker_push_subscriptions WHERE revoked_at IS NULL ORDER BY id DESC LIMIT 5;
   ```

   Do not print `endpoint`, `p256dh` or `auth` in anything you keep or share: the endpoint is a
   capability to send to that phone, and the two keys are what makes the payload readable to
   it. That is also why the API never echoes an endpoint back to anybody but its own worker.

One row is one **browser**, not one worker: a second phone is a second row, and turning
notifications off on one leaves the other's alone.

## Step 5 - see a notice arrive with the app closed

1. Cause a real notice - one of the two paths that write one on their own:
   - the overtime watcher (`OVERTIME_WATCHER_INTERVAL_SECONDS`, default `60`): a shift crossing
     the overtime line, or a shift auto-closed at the paid day;
   - a worker's clock-out that crosses the line, or `POST /api/v1/admin/force_clock_out`.
2. **Close** the app on the phone - swipe it away, do not merely background it.
3. The notification arrives within a minute of the watcher's pass. Tapping it opens (or
   focuses) the app and marks that notice read, so the badge on the lock screen and the badge
   on the *Alerts* tab agree.
4. Confirm the server's side of the same delivery:

   ```sql
   SELECT id, kind, created_at, delivered_at, delivery_attempts, last_error
   FROM worker_notifications ORDER BY id DESC LIMIT 5;
   ```

   `delivered_at` set with `delivery_attempts = 1` means the push service accepted it.
   `last_error` set means it did not, and the sentence says which kind of not:
5. The subscription's last word:

   ```sql
   SELECT id, last_ok_at, last_error, revoked_at
   FROM worker_push_subscriptions ORDER BY id DESC LIMIT 5;
   ```

   `last_ok_at` stamps a send the push service accepted. `revoked_at` set **without you doing
   anything** means the service answered 404/410: the endpoint is gone (browser data cleared,
   app uninstalled, permission revoked). The row is retired rather than deleted, so "why did my
   alerts stop" has an answer on it, and the worker re-enables to get a new one.

The dispatch summary in the application log uses these words, counted apart on purpose:

| word | meaning |
| --- | --- |
| `delivered` | at least one of the worker's subscriptions accepted the notification |
| `no_subscription` | the worker has no live subscription. The notice waits while it is inside the age window; past it, the inbox is where it is read |
| `failed` | the push service answered with an error |
| `revoked` | the service answered 404/410; the endpoint is retired |
| `refused` | **this server** declined to fetch that URL (shape or allowlist). Counted separately from `failed` so nobody hunts a provider's logs for a request that never left the server |
| `skipped` | the channel is not usable at all; the sentence carries the reason |

## Step 6 - make sure you would hear about it going quiet

The failure mode of a push channel is not an error, it is a **silence**: a service answering
`401`, a subscription retired under the worker's feet, a permission revoked on the phone. Every
setting stays valid, nothing is logged as a failure, and the phones simply stop ringing - and
from the outside that looks exactly like a workforce with nothing to be told. So the backlog is
measured rather than waited for. A worker notice past `PUSH_MAX_AGE_MINUTES` that is still
undelivered will *never* be pushed (past that window nothing is retried), and that is what is
counted.

Three places report it, in increasing order of how likely you are to be looking:

1. **the application log**, a `WARNING` on the watcher tick that names the count, the window
   and the age of the oldest stranded notice;
2. **an administrator notification** (`admin_notifications`, kind `push_undelivered`, severity
   `warning`). It is written at most once an hour, so a backlog that persists is re-said and a
   backlog that is merely old is not:

   ```sql
   SELECT id, kind, severity, title, created_at, read_at, dedupe_key
   FROM admin_notifications WHERE kind = 'push_undelivered' ORDER BY id DESC LIMIT 5;
   ```

   The row's `payload` carries the counts (`notices`, `workers`, `no_device`, `with_device`,
   `attempted`, `oldest`, `kinds`). It deliberately carries **no** endpoint and no key: an alert
   is a row that gets logged, mailed and pasted into issues;
3. **`GET /api/v1/readiness`**, where it is the `worker_notice_backlog` check - advisory, and
   the one an automated monitor can watch. Its `detail` on the admin route names the two halves
   separately, because they need different people: notices with **no live device** are workers
   who never allowed notifications (nothing was sent, nothing will be), while notices whose
   worker **has a registered device** went to a push service and did not arrive - their
   subscription's `last_error` says which.

Two properties of the check are worth knowing before you trust it:

- **it is silent when this deployment does not intend to push.** With no key pair, or with
  `PUSH_ENABLED=0`, a backlog is the expected state and the check reports `not measured` and
  passes. That is deliberate: an hour-by-hour alarm about a decision you made yourself is how a
  real one gets ignored.
- **it reads the same window `PUSH_MAX_AGE_MINUTES` that dispatch refuses to wake a phone for.**
  Nothing newer is counted, because nothing newer is stranded yet. If you raise that window you
  are also raising the age at which a backlog becomes reportable, and the age at which a phone
  may still be woken by old news.

Nothing here is a data loss: every notice is still in the worker's own inbox, which is what
their app shows. What is lost while this reports is the buzz.

## Troubleshooting

| symptom | what it means | what to do |
| --- | --- | --- |
| card says the server cannot send notifications, with a reason | `transport_available()` refused | the reason names the missing piece: no keys, no `pywebpush`, or `PUSH_ENABLED=0`. Finish Steps 1-2 and restart |
| card says the browser cannot show push notifications | no service worker, or not a secure context | serve the app over HTTPS. The inbox still works on that phone |
| `POST /api/v1/worker/me/push/subscribe` answered `409` | the deployment cannot push at all, so **nothing was stored** | finish Steps 1-2 and restart, then enable again |
| subscribe answered `422` | the endpoint failed the shape or allowlist check; nothing was stored | the browser's push service host is not in `PUSH_ENDPOINT_HOSTS`, or a proxy rewrote the URL. Add the host deliberately - read the README's allowlist section first |
| one worker gets nothing | their subscription was revoked, or the browser's permission was turned off | look at `revoked_at`/`last_error` for their rows; the card on their phone is the truth |
| nothing is delivered and nothing is logged as failed | the notice is older than `PUSH_MAX_AGE_MINUTES`, or its attempts are exhausted | this is by design: an ancient notice is an inbox item, not a waking buzz. Raise the window only if you mean to wake phones about old news |
| every push fails with the same error | the pair or the subject is wrong | `python -m config`, check the public key's length, and remember a rotated pair needs every device to re-enable |
| it worked, then stopped for everyone | a change to the allowlist, or a rotated key | `push_endpoint_allowlist` in readiness, then the subscription rows' `last_error` |
| readiness itself names the key | never expected | that is a bug - report it. The check reports booleans only, on purpose |
| `worker_notice_backlog` is degraded and the log says the channel is quiet | notices are passing the window undelivered: a service erroring, or a subscription table emptied | read the alert row's `payload` (Step 6) - `with_device` half first, then `last_error` on those subscriptions. `no_device` half is a phone that never allowed notifications, and only that worker can change it |
| the backlog check says `not measured` | this deployment is not configured to push, so there is nothing to measure | expected: either finish Steps 1-2, or leave it - the inbox is the record |
| the alert repeats every hour and nobody has acknowledged it | the backlog is still there | it is deduplicated per hour on purpose. Silencing it without fixing it removes the only signal that the phones are not ringing - `read_at` on the row is how you record having seen it |

## Turning it off

- **one device**: the card's switch. The browser subscription is released and the row retired
  by endpoint. This works even if the server cannot be reached to retire it - the worker asked
  for off.
- **the whole deployment**: `PUSH_ENABLED=0` and restart. Readiness reports a pass, nothing is
  sent, and every notice still lands in the inbox. The stored subscriptions are left alone, so
  setting it back to `1` restores the same devices.
- **retiring dead endpoints**: retention removes subscriptions that are *revoked* past
  `NOTIFICATION_RETENTION_DAYS` (default `180`), along with read notifications past the same
  window - a revoked row is a URL anyone who reads the database could POST to, with no
  operational value left.

## What this runbook is held to

`backend/tests/test_worker_push_runbook.py` reads this file and refuses a drift between it and
the code: every push setting the code reads must be documented here, every `PUSH_*` / `VAPID_*`
name used here must be one the code actually reads, the defaults in the table must be the
code's own, and the commands, endpoint paths, readiness check names, tables and columns named
above must exist. A renamed variable, a changed default or a moved route fails a test rather
than sending an operator after a setting that no longer exists.
