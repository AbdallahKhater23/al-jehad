# Site Attendance

Facial-recognition attendance for construction sites: workers clock in/out from a
phone with a GPS-stamped selfie, admins review shifts, approve the long ones and read
the hours per worker over any period. (The app does not run payroll - it records
time and who signed it off.)

**A punch asks for no password.** The worker signs in once; the session is the
credential, so the clock prompt is a camera and a shutter. What the record rests on is
the token (revoked the moment the password changes or the account is deactivated), the
gate's geofence, the liveness check and a face match against the enrolled template - not
a password typed at the gate, which everyone standing nearby can also read.

- `backend/` — FastAPI + DeepFace + SQLite (`main.py`, `serve.py`)
- `frontend/` — single-page app (no build step, Tailwind via CDN)

## Running the server

```bash
cd backend
./venv/Scripts/python.exe serve.py      # Windows (Git Bash)
source venv/bin/activate && python serve.py   # macOS / Linux
```

It prints two addresses:

```
  Laptop:  https://localhost:8000
  Phone:   https://192.168.x.x:8000   <-- open this on mobile
```

**Use `serve.py`, not `uvicorn main:app`.** Browsers only expose GPS
(`navigator.geolocation`) and the camera (`getUserMedia`) on a *secure context*:
`https://` or `http://localhost`. Over plain HTTP from a phone the browser blocks
them instantly and returns `error.code === 1` (PERMISSION_DENIED) without ever
prompting — which is why clock-in used to fail with "Permission denied (Code 1)".

Useful flags:

| Command | What it does |
| --- | --- |
| `python serve.py` | HTTPS on `0.0.0.0:8000` (the normal case) |
| `python serve.py --http` | Plain HTTP, laptop only; GPS/camera will not work on phones |
| `python serve.py --port 8443` | Different port |
| `python serve.py --reload` | Auto-restart on code changes |
| `python serve.py --gen-cert-only` | Re-create the certificate and exit |

The self-signed certificate lives in `backend/certs/` (git-ignored) and is
regenerated automatically whenever the machine's LAN IP changes.

### One-time setup per device

1. Open the `https://192.168.x.x:8000` address on the phone.
2. The browser warns about the certificate: **Advanced → Proceed / visit this website**.
3. Allow **Location** and **Camera** when prompted.

After that the origin counts as secure and GPS, camera and "Add to Home Screen"
all work. Workers can also self-check from **Profile → Device status** (Test
location / Test camera).

## Going live

The app calls its own page origin for everything (`page origin + /api/v1`, see
`API.resolveBaseURL`), so publishing it is two pieces that keep it one origin:

| Piece | Where it runs |
| --- | --- |
| **The backend** | A host - the Railway service below - at an https address that does not change. |
| **The frontend** | Cloudflare, as a Worker that serves the shell and proxies the API paths to that address. |

Both give a **proper trusted HTTPS certificate**, so there is no "not private" warning and GPS +
camera work on every device with no per-device setup. Running both on this machine behind a
tunnel is the fallback at the end of this section, and it works - it is just the version that
stops when the machine sleeps.

### The frontend on Cloudflare Workers

`deploy/cloudflare/` is a Worker that serves the frontend as static assets and proxies
`/api`, `/static`, `/enroll` and `/q` to the backend address in its `API_ORIGIN`. The proxy is
not optional: the app is single-origin by construction, so a shell hosted on Cloudflare with no
proxy calls `<your-worker>/api/v1/...` and gets a 404 from the asset host (which is exactly what
`al-jehad1.abdallahtamet281.workers.dev` did before the Worker existed). That folder holds the
Worker, the `wrangler.toml`, `verify_live.py` (the one command that checks the whole path,
including the punch screen's own calls through the public URL), and the two things to watch
after the first deploy.

### The backend on a host (Railway, or any host that injects a port)

The backend runs on a host rather than this laptop, which is what makes the address the Worker
points at stop changing - and what stops the app depending on a machine being awake.
`railway.json`, `.python-version`, `requirements.txt` and the `Dockerfile` that `railway.json`
names are the whole config: build the image (Docker, not Nixpacks - the builder is stated in
`railway.json`), start with `python backend/serve.py --tunnel`, and check `GET /api/v1/status`
before releasing traffic.

```bash
railway up                     # or connect the repo in the dashboard
railway volume add --mount-path /data
```

Set these as **service variables** (not in a committed file):

| Variable | Why |
| --- | --- |
| `SECRET_KEY` | **Required.** 32+ random chars (`python -m config --write-env`). Without it `build_settings()` raises and the app never imports - which on a host looks like a 502 from the edge, not like a configuration error. |
| `DATABASE_PATH=/data/times.db` | Points the database at the mounted volume. Set `BACKUP_DIR=/data/backups` too. |
| `TRUSTED_PROXIES` | The host's edge is not loopback, so until this lists it every worker is bucketed under one address: they hit `429`s together and the audit log records the proxy as the actor. Railway's internal hop is in `100.64.0.0/10`; the exact peer the app sees is in `GET /api/v1/admin/readiness` under `network_policy.value.forward_misuse.last_peer`. `serve.py` prints the list it is using at startup; `GET /api/v1/readiness` reports `network_policy`. |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | The Web Push key pair. Without it the worker inbox still records every notice and `readiness` reports `worker_push_delivery` as an advisory - the phones simply do not ring. Generate the pair with `python -m push --generate-keys`; `docs/RUNBOOK_WORKER_PUSH.md` is the whole procedure and `deploy/railway/README.md` is the deployment half of it. |
| `ENABLE_API_DOCS=0` | The default. Leave it unless you are debugging. |

The service's own self-test is the check that all of this landed: `python
deploy/railway/verify_readiness.py` reads the deployed `GET /api/v1/readiness` and exits non-zero
when any check is failing, and `deploy/railway/README.md` says what each failing check needs.

Three things that are easy to get wrong and silent when you do:

- **A volume is not optional.** SQLite lives in a file; without one, every deploy and every
  restart replaces the container's filesystem and the punches are gone. Run **one replica**:
  SQLite is single-writer, and two instances on one file corrupts it.
- **Memory.** The image loads the embedding model (87 MB) and the YuNet detector at start, on top
  of onnxruntime and OpenCV. The training framework the engine used to run on is gone, so this is
  lighter than it was, and a first start is still slower than a restart while the models load.
  `healthcheckTimeout` is 300s in `railway.json` for that reason - a shorter one marks a healthy
  deploy unhealthy.
- **Onnxruntime is a runtime dependency** (`requirements.txt`), so face *matching* works on any
  host built from this repository. The MiniFASNet liveness model is a separate download
  (`backend/models/README.md`): absent, the API says so and liveness degrades while matching is
  unaffected. Readiness names each as a check rather than as a broken deploy.

Verify the host's own address before pointing anything at it - this deployment's is
`https://al-jehad-production.up.railway.app`, so
`https://al-jehad-production.up.railway.app/api/v1/status` must answer `{"status": ...}`. If it answers **502 "Application failed to respond"**, the edge is up
and nothing healthy is behind it - read the deploy log, not the app log; the usual causes are a
missing `SECRET_KEY` (see above), a builder with no `requirements.txt` (which is why the file is
now committed), or an app bound to a port nobody forwards to (`PORT` is read by `serve.py`).

### Fallback: a tunnel from this machine (cloudflared / ngrok)

For when the host is down and the app is needed now. The same Worker is pointed at this address
instead of the host's:

```bash
python backend/serve.py --tunnel                  # terminal 1: plain HTTP on :8000 for the tunnel
cloudflared tunnel --url http://localhost:8000     # terminal 2: the public HTTPS address
```

`--tunnel` means "the tunnel provides the HTTPS, so do not make a self-signed certificate". Put
the printed `https://<words>.trycloudflare.com` address in the Worker's `API_ORIGIN` and redeploy
it; `ngrok http 8000` works the same way if that is what you have.

- **The address changes on every restart.** That is the whole reason this is the fallback: the
  Worker *holds* the address, so a new one means editing the config and redeploying, and the app
  answers `api_unreachable` until you do.
- **No front-end configuration needed.** The page and the API share one origin, so
  the app calls `https://<your-tunnel>/api/v1/...` automatically. (That also means
  no mixed-content blocking: the old hardcoded `http://<host>:8000` API URL would
  have been refused on an HTTPS page.)
- **First visit per browser, on ngrok only:** ngrok's free tier shows a "You are
  about to visit …" warning page. Click **Visit Site** once; after that the app
  loads normally. Cloudflare Tunnel shows no such page.
- **Rate limiting stays per worker**, and here it needs no setting: `serve.py` enables proxy
  headers and cloudflared connects from `127.0.0.1`, which `TRUSTED_PROXIES` trusts by default.
  On a host the edge is not loopback, so that variable has to be set by hand - see above.
- Keep the address private. It has no authentication in front of it.

## Troubleshooting

- **Still "GPS blocked" on a phone** — check the badge in Profile → Device status.
  If it says "Location blocked (HTTP)", the page was opened over HTTP; open the
  HTTPS address instead.
- **Clock-in says location rejected** — the worker is outside the site geofence.
  Site coordinates and radii are managed in Admin → Sites.
- **Every arrival at one site is flagged late** — that site's clock-in window does not
  contain the hours people actually arrive. Check the site's own window (Admin → Sites,
  or `GET /api/v1/admin/sites`); a window that starts after it ends is overnight, and
  `04:00`–`06:30` at a site that starts at 21:30 is the usual mistake.
- **Nothing is styled / blank white page** — Tailwind is loaded from a CDN, so the
  device needs internet on the first load.
- **The app loads but signs in nowhere ("Cannot reach the server")** — the page is being
  served from a host that is not serving `/api/v1`. The app calls the page's own origin on
  purpose, so the fix is a proxy in front of the backend, not a frontend setting: see
  `deploy/cloudflare/README.md`.
- **Photos or faces are still on disk past their retention period** — run
  `cd backend && python -m retention` for the report (it deletes nothing), then
  `--apply`. A file that cannot be removed is listed by name in the report and in the
  compliance event; `GET /api/v1/readiness` names the same files under
  `retention_residue`.

## Security notes (read before exposing this publicly)

This section used to say the backend had **no API authentication** and fell back to
**hardcoded Twilio credentials**. Both of those were true of the prototype and are no
longer true of the application, so for the avoidance of doubt:

* Every administrative route requires a session token (`Authorization: Bearer`), and the
  token is checked for both validity and role - a worker's token is refused on
  `/api/v1/admin/*` with 403, not 401, so the client does not sign them out over it.
  Passwords are bcrypt hashes; there is no readable copy in the database.
* `SECRET_KEY` is mandatory (the app refuses to start without one), the interactive API
  documentation is off by default, outbound messaging is an internal notification table
  rather than a Twilio call, and no code path falls back to a committed credential.
* Every stored free-text field is validated at the boundary (`backend/textguard.py`) and
  escaped again when the frontend renders it, and the document CSP refuses inline
  `<script>` elements. Both halves, and the two allowances that remain, are in **What text
  the server will store** and **Security headers** below.
* Rate limits, the biometric retention sweeper, and the Prometheus endpoint's own
  authentication are described in their own sections below.

What remains true: this is a **LAN-first application**. Serve it on a site network, or put
it behind a tunnel *and* the controls in the next section - the token gates the API, the
network policy decides who may even attempt it.

## Network hardening (CORS, admin IP allowlist, security headers)

Three controls that were either missing or not separable, all in `backend/netguard.py` and
all driven by environment settings. Nothing here is enabled by default in a way that can
lock out an existing deployment: with no `ADMIN_IP_ALLOWLIST` and no CORS origins set, the
application behaves as it did before (same-origin clients keep working).

### 1. CORS with two origin classes

`ALLOWED_ORIGINS` was one list for both audiences, so any origin allowed to fetch a punch
was equally free to call `/api/v1/admin/users`. There are now two classes, because they are
two trust levels:

```bash
# A phone/web app that has been handed a session. May read everything except /admin/*.
CORS_WORKER_ORIGINS=https://app.example.com,https://*.workers.example.com
# A managed console. May read the whole API, including /admin/*.
CORS_ADMIN_ORIGINS=https://console.example.com
# Optional: '*' is accepted for WORKER origins only (credentials are then dropped,
# because a browser forbids the combination). An admin wildcard is refused at startup.
```

* Entries are exact origins, or one host wildcard (`https://*.example.com` matches the
  subdomains and **not** the bare domain - list both if you want both). Scheme case and a
  default port (`:443`, `:80`) are normalised, so `https://Console.Example.com:443/` matches
  `https://console.example.com`.
* A worker origin asking for an admin path gets **no CORS grant** - the request is still
  served, but a browser cannot read the response. That is what CORS is: a browser-enforced
  policy, not an authorization check, which is exactly why the admin API's real gate is the
  session token plus the network allowlist below.
* Preflights are answered by this middleware (the app has no `OPTIONS` handlers), so a
  refused preflight is a readable `403 {"error_code": "cors_refused"}` naming the header or
  the origin, rather than the browser's opaque network error. The headers this client
  actually sends are allowed by default, including `ngrok-skip-browser-warning`.
* `ALLOWED_ORIGINS` still works: it is merged in as a *worker* origin list, so a deployment
  that had it set keeps the access it had and does not silently gain admin access.

```bash
# Knobs, all optional
CORS_ALLOWED_METHODS=            # empty = GET, POST, OPTIONS
CORS_ALLOWED_HEADERS=            # empty = the defaults listed above
CORS_ALLOW_CREDENTIALS=1
CORS_MAX_AGE_SECONDS=600
```

### 2. Admin IP allowlisting (with proxy validation)

```bash
# Addresses permitted to reach /admin/* and /api/v1/admin/*.
# Empty = no gate (today's behaviour): the routes are still token-guarded, but any
# address may attempt them.
ADMIN_IP_ALLOWLIST=10.20.0.0/16,192.168.1.50,2001:db8::/32

# Which peers may set X-Forwarded-For / X-Forwarded-Proto. Loopback by default, which
# covers the bundled TLS server and a tunnel running on this host.
TRUSTED_PROXIES=127.0.0.1/32,::1/128
```

The proxy rule is the part worth reading twice. `X-Forwarded-For` is a *request header*: any
client can send it. It is believed **only** when the immediate peer is in `TRUSTED_PROXIES`,
and the chain is then walked from the right, skipping trusted proxies, until an address that
is not a proxy is found. Consequences, in order of how often they bite:

* **A proxy on another host must be declared.** Otherwise the allowlist checks the proxy's
  address instead of the administrator's. The app notices: it refuses the admin request with
  `proxy_not_trusted`, counts it, **and** reports it in `GET /api/v1/readiness` as a failing
  advisory check (`network_policy`) naming `TRUSTED_PROXIES`. `serve.py` now feeds this same
  setting to uvicorn, so one list drives both the gate and the client address the audit log
  and rate limiter see.
* **Spoofing does not work.** A client that is not a trusted proxy sending
  `X-Forwarded-For: <an allowed address>` is refused, never upgraded: the header is not an
  address. That case has its own test.
* **A misconfigured policy fails closed and loudly.** An entry that is not an address or
  CIDR (`10.0.0.0/33`) makes the startup gate **fatal**: the process refuses to serve and
  names the line, rather than running a gate that matches nothing and locking every
  administrator out in silence. `TRUSTED_PROXIES=*` (a tunnel with no fixed egress) is
  allowed but reported, because it means any peer may forge the header.
* The gate applies to exactly the prefixes in `ADMIN_ALLOWLIST_PATHS` (`/admin`,
  `/api/v1/admin` by default - add `/metrics` if your scraper should be gated too), never to
  worker routes: a site's phones are on mobile networks and an allowlist that covered the
  punch endpoint would stop the attendance the payroll is made of.
* A refusal writes nothing: no session, no audit row, no notification. The response never
  names the allowed networks - that would hand the allowlist over one guess at a time.

```bash
# Scrape this and alert on any increment: a step is either a misconfigured proxy or
# somebody probing the admin surface.
attendance_netguard_refusals_total{reason="ip_not_allowed"}
attendance_netguard_refusals_total{reason="proxy_not_trusted"}
```

| symptom | cause |
|---|---|
| `403 {"error_code": "ip_not_allowed"}` | The client's address is outside `ADMIN_IP_ALLOWLIST`, or its address could not be determined. |
| `403 {"error_code": "proxy_not_trusted"}` | `X-Forwarded-For` arrived from a peer that is not in `TRUSTED_PROXIES` (or the value was unparsable). Add the proxy, or stop sending the header. |
| `403 {"error_code": "cors_refused"}` | A preflight from an origin no list names, or asking for a header the policy does not allow. |
| Startup aborts with `network_policy` fatal | `ADMIN_IP_ALLOWLIST` / `TRUSTED_PROXIES` contains something that is not an address or CIDR. |
| Readiness `network_policy` failing (advisory) | `X-Forwarded-For` has arrived from an undeclared peer: the gate is checking the proxy's address. |

The startup gate that produces those aborts runs the checks described under
[Tests](#tests) and a good few besides; the fatal ones, what each one protects, which of them
an operator may consciously serve through, and how to get back to a healthy deployment
afterwards are [`docs/RUNBOOK_STARTUP_OVERRIDE.md`](docs/RUNBOOK_STARTUP_OVERRIDE.md), kept
honest by `backend/tests/test_startup_override_runbook.py`. Two of those checks are not
overridable at all - a deployment with no signing key or no database has no
`STARTUP_OVERRIDE_REASON` that could save it.

A forced start is a decision, and the application treats it as one: the *reason* is required to
open the hatch, and an **acknowledgement** is required to settle it. `STARTUP_OVERRIDE_REASON`
writes one `admin_notifications` row per reason - critical, unread, carrying the failing checks
and the operator's own words - and `startup_override_acknowledged` reports it as an open
question on the readiness surfaces until somebody answers it. Answering is
`POST /api/v1/developer/notifications/{id}/acknowledge` with a note, from the console's
**Alerts** tab or the API - the alert queue is the root tier's, so both the console's Alerts
tab and the route behind it answer that tier and nobody else. The note is required and vetted
as plain text, the actor and their reason go into
the append-only `audit_log` (with the request's address and user agent), the alert is marked
read with it, and a second answer is refused with a `409` naming who answered first - because
the first acceptance is the one that was actually made. Two acts stay distinct the whole way
through: **reading** an alert records that somebody looked, which is the right answer for a
sweep or a note, and **acknowledging** it records that somebody decided. Every individual forced
start is its own `audit_log` row, so "how often, and with what reason" outlives the alert row
it belongs to.

### 3. Security headers

Sent on every response, including refusals and static assets:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin   (documents) / no-referrer (JSON)
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Permissions-Policy: camera=(self), geolocation=(self), microphone=(), display-capture=(), payment=(), usb=(), serial=()
Content-Security-Policy: <one of two, see below>
Strict-Transport-Security: max-age=15552000; includeSubDomains   (only over TLS)
```

* **The CSP splits by content type.** A JSON body has no business loading anything:
  `default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`.
  An HTML document gets:

  ```
  default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none';
  frame-src 'none'; form-action 'self';
  script-src 'self' https://cdn.tailwindcss.com; script-src-attr 'unsafe-inline';
  style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com;
  img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:;
  connect-src 'self'; worker-src 'self' blob:; manifest-src 'self'
  ```

* **No inline `<script>` blocks.** The pages carried four of them (a boot diagnostics panel,
  the Tailwind class-mode config, and one per standalone capture page). They are files now -
  `frontend/boot.js`, `tailwind_boot.js`, `enroll.js`, `quick.js` - so `script-src` names no
  `'unsafe-inline'` and a browser refuses an injected `<script>`. That is the one thing a
  stored-XSS payload actually needs, and it is verified in a browser rather than asserted:
  load the pre-change page under this policy and the console says *"Refused to execute inline
  script"* twice, while the current page loads every script it needs. Moving the code into
  files also exposed one piece of it that had been leaning on `eval`: the boot panel probed for
  its globals with `new Function`, which this policy refuses, so the probe threw, the `catch`
  read the throw as "not loaded", and the panel - only ever shown after a blank page - named
  every script as missing. It names each global with `typeof` now, and
  `tests/test_frontend_xss.py` refuses both the string-built call and `eval` coming back.
* **Referrer policy splits too.** A document gets `strict-origin-when-cross-origin` (the
  browser default, which still works when a page is opened across a tunnel); a JSON response
  gets `no-referrer`, because a body that is never a navigation source does not need to send
  one, and an error payload should not be quotable as a referrer.
* **HSTS is sent only where TLS really is**: an `https` request, or `X-Forwarded-Proto:
  https` *from a trusted proxy*. Sending it blind would pin the host for browsers while the
  site server speaks plain HTTP, which is how a worker's phone stops reaching the punch
  page. `HSTS_MAX_AGE_SECONDS=0` disables it.
* **Honest limit, stated as two specific allowances.** `script-src` still names
  `https://cdn.tailwindcss.com` (the frontend has no build step, so its utility CSS is
  compiled in the browser; committing a Tailwind build removes the host) and
  `script-src-attr 'unsafe-inline'` still permits **event-handler attributes**, because the
  console builds its markup as strings and puts the handler in an `onclick=`. The second one
  is the real remaining gap: an injected `<img onerror=...>` runs. Closing it is one
  delegated `addEventListener` per container instead of a handler per button - 96 call sites
  today, pinned by `tests/test_frontend_xss.py` so the count can only fall. `GET
  /api/v1/readiness` reports `csp_html_inline_script_elements` (false),
  `csp_html_inline_event_attributes` (true) and the script origins, so this is visible rather
  than implied. A route that sets its own header keeps it (the middleware adds a baseline, it
  does not overrule a handler).

```bash
SECURITY_HEADERS=1              # 0 sends none of the above (a proxy may set them)
HSTS_MAX_AGE_SECONDS=15552000
CSP_HTML=                       # empty = the built-in document policy
CSP_API=                        # empty = the built-in JSON policy
```

### What this is not

It is not an authorization layer (an allowed address still has to authenticate), not a WAF
(it does not look at bodies or parameters), and not a substitute for TLS. `CORS` remains a
browser-enforced policy; the controls that actually keep an attacker out of the payroll API
are the session token, the allowlist, and TLS in front of both.

## What text the server will store (stored XSS, and how it is prevented twice)

Every string a client sends is stored and then rendered back out - into the roster, the notes
inbox, the audit log, an administrator's notification, a CSV an operator opens in Excel. A
worker named `<img src=x onerror=...>` is therefore not a worker with a funny name; it is code
waiting for whoever reads a name next. There are two defences, and they are deliberately
independent:

**1. The value cannot be markup** (`backend/textguard.py`, called by every request model and by
the multipart endpoints).

| Profile | Used for | Rule |
| --- | --- | --- |
| identifier | worker names, site names, labels, categories | allowlist: Latin **and Arabic** letters (all blocks a keyboard produces), Western and Arabic-Indic digits, harakat, spaces, `. , - _ ( ) /` |
| prose | note subjects and bodies, rejection reasons, link notes, invite notes | everything *except* the four active shapes: HTML tags (including the percent-encoded `<%2F`), HTML entities that decode into markup (`&lt;script&gt;`), `javascript:`/`vbscript:` and content-carrying `data:` URLs, and inline `on*=handler` text |
| contact | email and phone | letters, digits, `@ . _ + ( ) , - /` |

* **Arabic is a first-class case**, tested as one: `محمد كمال`, `مُحَمَّد` (with harakat) and
  `٠١٢٣٤` (Arabic-Indic digits) all pass unchanged. The ranges are written out explicitly
  rather than relying on `\w`, so "does an Arabic name pass?" is answered by reading a constant.
* **Apostrophes are refused in identifiers, kept in prose.** `O'Brien` cannot be a stored
  name - an apostrophe both breaks an unquoted SQL literal and escapes an HTML attribute. A
  note keeps `it's the second time & nobody came`, because a note is a sentence and the
  renderer escapes it. A deployment that needs the apostrophe widens
  `IDENTIFIER_PUNCTUATION` in one place.
* **Bidirectional overrides and control characters are stripped, not refused**: `U+202E`
  reverses everything after it (a stored name can render as a *different* name) and a NUL makes
  SQLite and Python disagree about the same string's length. `U+200C`/`U+200D` are explicitly
  **kept** - they are Arabic and Persian orthography - and values are NFKC-folded so `Ａhmed`
  and `Ahmed` are one name rather than two keys.
* **Passwords are never touched by any of this.** They are hashed and never rendered;
  restricting their characters would only weaken them. `LoginRequest` is left alone for the
  same reason - it compares a credential, and refusing a character there locks a person out of
  their own account.
* **Refusals are 422 (JSON models) or 400 (multipart forms)**, and always name the character:
  *"Name may contain letters (English or Arabic) ... It contains U+003C '<' (Less-Than Sign),
  which is not one of them."* A 422 that still wrote the row would not be a defence, so the
  suite checks the row count afterwards.

**2. The renderer escapes everything it interpolates** (`frontend/*.js`: `escapeHtml` on every
value that did not originate in the file). This is the half that covers rows written *before*
the rule existed - and there are such rows, which is why both halves exist:

* `tests/test_frontend_xss.py` renders hostile values (`<img onerror>`, `<script>`-named sites,
  `svg/onload` statuses) through the app's own rendering functions in a Node VM and asserts the
  DOM receives text, in both the phone-card and the desktop-table layouts - and it caught a real
  gap while being written (`${err.message}` in a table, since fixed).
* **What to do when you add a screen is written down**: `docs/FRONTEND_RENDERING.md` has the
  audit (the eight interpolation classes that were unescaped, with the before/after for each),
  the seven rules with examples, the list of sinks to grep for, and the cleanup still
  outstanding. The short version is rule 1: escape every interpolation that is not a literal, a
  number you computed, or a value you built in the same file.
* `GET /api/v1/readiness` reports a **`stored_text`** check: values already in the database
  that today's rules would refuse, per table and column. It is advisory and never repaired
  automatically - rewriting a worker's name or a note somebody wrote is a decision for a
  person. The read path keeps serving those rows, escaped.

**What this is not.** It is not output encoding (nothing is stored as `&lt;`; that double-escapes
the moment a correct renderer touches it), and it is not a substitute for the CSP above or for
not putting a name into an SQL string. It is the boundary check that makes the *other* 20
consumers of these rows safe without each of them having to remember.

## Configuration (required)

The API refuses to start without a signing secret - a deployment with no `SECRET_KEY`
would sign tokens any reader of the repository could forge.

```bash
cd backend && python -m config --write-env     # writes ../.env with a generated key
python -m config                               # verify the configuration
```

Key settings (all optional except `SECRET_KEY`, full list in `backend/config.py`):

| Variable | Default | What it does |
| --- | --- | --- |
| `LIVENESS_MODE` | `advisory` | `off` / `advisory` (record + notify, never block) / `enforce` (refuse a spoofed frame before face matching) |
| `LIVENESS_MODEL_PATH` | `backend/models/minifasnet.onnx` | MiniFASNet ONNX file; see `backend/models/README.md` |
| `ENROLLMENT_LIVENESS_MODE` | `inherit` | liveness policy used when a *new reference template* is enrolled |
| `OVERTIME_WATCHER_ENABLED` | `1` | the background timer that ends a shift at the paid day and alerts past it - which of the two acts, whether the close acts at all, and whether it stands down, is decided by the switch and the thresholds below; read *Which rule ends the day* before retuning either; `0` stops both rules |
| `OFFLINE_PUNCH_MAX_AGE_HOURS` | `72` | how far back an offline punch may be anchored |
| `NOTES_MAX_OPEN_PER_WORKER` | `20` | how many notes by one worker may be waiting for an answer at once |
| `UPLOAD_MAX_PHOTO_BYTES` | `5242880` (5 MB) | ceiling for **every** photo upload in the app |
| `LOCAL_REFS_DIR` | `local_references` | face templates. Set it with the three below when something else owns the disk, and remember that a **child process** reads it too: any script that imports this app writes where these point |
| `WORKER_PHOTOS_DIR` | `worker_photos` | reference selfies, one per enrolled account |
| `PUNCH_FRAMES_DIR` | `punch_frames` | the downscaled evidence frame stored with a punch |
| `QUICK_LINK_PHOTOS_DIR` | `quick_link_photos` | the selfie a quick-link punch arrives with |
| `REGISTRATION_ENABLED` | `0` | the walk-up onboarding link (`GET`/`POST /api/v1/register`). Off by default, so a deployment publishes it deliberately; while off the link answers as closed and every submission is refused |
| `REGISTRATION_PHOTOS_DIR` | `registration_photos` | the selfie a walk-up applicant sends, held until an administrator approves or rejects it |
| `REGISTRATION_PENDING_CAP` | `200` | how many applications may wait for review at once; the next is refused rather than letting the queue grow without bound |
| `REGISTRATION_RATE_LIMIT` | `20/minute` | per-IP ceiling on submissions to the public link |
| `REGISTRATION_PHOTO_STALE_HOURS` | `168` | how long an application photo no row references is kept before the startup sweep deletes it |
| `MIN_PASSWORD_LENGTH` | `8` | shortest password the server will set, in the console and on a registration link |
| `ENROLLMENT_TOKEN_TTL_HOURS` | `72` | how long an enrollment or registration link stays usable |
| `OVERTIME_WATCHER_INTERVAL_SECONDS` | `60` | how often that timer runs |
| `FACE_INFERENCE_CONCURRENCY` | `2` | how many face verifications may run at once - see below before raising it |
| `FACE_INFERENCE_QUEUE` | `64` | how many may wait for a slot before the server answers `503` + `Retry-After` |
| `FACE_INFERENCE_WAIT_SECONDS` | `20` | how long a caller waits for room before that `503` |
| `FACE_MODEL_PRELOAD` | `1` | load VGG-Face at startup instead of on the first punch |
| `RETENTION_ENABLED` | `1` | run the in-process retention sweeper (see below) |
| `RETENTION_PUNCH_PHOTO_DAYS` | `30` | how long a quick-link punch selfie is kept. `0` = forever |
| `RETENTION_BIOMETRIC_DAYS` | `7` | how long biometric **residue** is kept (a face whose account is gone). `0` = forever |
| `RETENTION_AUDIT_DAYS` | `365` | how long administrative audit rows are kept. `0` = forever |
| `RETENTION_PUNCH_QUEUE_DAYS` | `90` | how long a raw offline punch is kept once it has a record. `0` = forever |
| `RETENTION_MAX_ITEMS_PER_SWEEP` | `20000` | how many items one sweep removes. `0` = no cap (the one setting where `0` is not "keep forever") |
| `RETENTION_ACTOR` | *(none)* | who a hand-run sweep is attributed to in the audit event |
| `RETENTION_DRY_RUN` | `0` | run the sweep but delete nothing, on a timer |

Optional extras (`pip install -r backend/requirements-optional.txt`): `onnxruntime`
for liveness, `qrcode` for enrollment QR codes, `openpyxl` for XLSX export. Each one
degrades with an explicit message instead of failing.

## Face verification capacity

Every punch and every enrollment is a VGG-Face embedding plus a YuNet detection (the
previous detector, MTCNN, still runs on a host without the YuNet model), and those are the
most expensive things this application does. They all run in one place,
`backend/face_engine.py`, which is a small queue with a fixed number of worker threads -
so the answer to "how much inference is running right now" is a number in the config, not
"as much as happened to be asked for".

**Why two.** Measured on a CPU-only host, eight verifications per setting:

| concurrent verifications | total | per call | throughput |
| --- | --- | --- | --- |
| 1 | 4.21 s | 0.53 s | 1.90/s |
| 2 | 3.13 s | 0.78 s | 2.55/s |
| 4 | 3.13 s | 1.54 s | 2.56/s |

The third and fourth add **no throughput at all** and double how long a worker stands at
the gate. Raise `FACE_INFERENCE_CONCURRENCY` only if a measurement on your own hardware
says otherwise (more cores, or a GPU).

**When it is full, it says so.** What absorbs a site arriving at 04:00 is the queue, not
the concurrency: a punch takes one slot for its liveness check and one for its
verification, so the default 64 covers a burst well past that. Past the queue a request
is answered immediately:

```json
{"detail": {"error_code": "face_check_busy",
            "message": "The server is verifying a lot of faces right now. Try again in a few seconds - your photo and your account are fine.",
            "retry_after_seconds": 5}}
```

with `Retry-After: 5`. The wording is deliberate: a worker who is told their *photo* is
the problem when the server is merely busy goes and gets re-enrolled. The offline punch
queue covers a worker whose phone retried into a full server. A refusal writes nothing -
no session, no log row.

**The memory ceiling, and who checks it.** The queue above is what absorbs a burst, and the
thing that actually took the 512 MB instance down was memory rather than time: a punch used
to decode its frame *before* taking a queue slot, so every waiting punch held about 11 MB of
pixels and the busiest minute's high-water mark became the process's. The frame is now
decoded inside the worker that scores it, and a waiting punch holds a spooled file instead.
That is checked rather than assumed: `backend/tools/punch_saturation.py` starts this
application from the checkout, drives eight simultaneous punches per round for four rounds
past its capacity of two, samples the process the models actually live in, and fails when the
peak crossed its ceiling (400 MiB resident, 384 MiB anonymous) or the memory did not come back
down after the burst. `.github/workflows/punch-memory.yml` runs it on every push and pull
request, so the budget is a gate and not a habit.

It is the fast half, and it is honest about that: there is no cgroup here, so nothing kills
the process at the line - the gate is a prediction of the container's behaviour. The other
half is `backend/tools/capacity_test.py`, which runs the real image in the capped container
shape (`--memory 512m --memory-swap 512m --cpus 1`, swap off) and reads the kernel's own
counters - `memory.current` and `memory.stat anon`, `memory.peak`, `memory.events`
(`oom_kill`), and `cpu.stat` throttling - with a pass/fail table and an exit code. Run that
one before a release; it needs docker and a few minutes of real inference.

**What this does *not* isolate.** The models still run inside the API process: a crash
inside native TensorFlow (an out-of-memory kill, a corrupt image reaching a half-loaded
model) takes the API process with it, and a saturated pool still competes for the same
CPU. Real isolation means a separate model server - Triton, TorchServe - behind a network
call. `FaceEngine` is the seam for that, so it becomes a second implementation rather
than a rewrite; until then this bounds the damage. `GET /api/v1/readiness` reports the
load (capacity, queued, in-flight, refusals, slowest) as an advisory check, so an operator
can see a saturated engine without it failing the startup gate.

## Keeping the detector comparison current

Which detector settings find *these* faces, and whether SCRFD finds more of them than YuNet, is
a measurement rather than a setting: `backend/tools/coverage_sweep.py` runs the four
configurations (YuNet at 320, at 640, at 640 tiled, and SCRFD at 640) over a corpus and reports
what each one loses. Run by hand, that answer belongs to the month somebody sampled a corpus -
and the decision it supports (see `docs/RUNBOOK_EMBEDDER_MIGRATION.md`) is then argued months
later from stale frames.

`backend/coverage_report.py` is the same sweep, standing. It watches the gate's own frame folder
(`PUNCH_FRAMES_DIR` by default - the frames a punch actually stored, not a curated export), and
re-measures when **both** are true:

* the folder has grown by at least `STANDING_SWEEP_MIN_NEW_FRAMES` frames since the last
  measurement, and
* a whole `STANDING_SWEEP_INTERVAL_SECONDS` has passed since it.

A folder that *shrank* - retention erasing frames on its schedule - re-baselines the trigger
instead of blocking it forever. Each measurement reads the **newest** `STANDING_SWEEP_SAMPLE`
frames, which is the whole cost control: four configurations over a couple of hundred frames is a
minute or two of CPU on the box that serves punches, so the cadence is daily and the first run
waits past the model preload. The work runs on a daemon thread for the same reason the retention
sweeper does - it is blocking CPU work, and as an asyncio task it would hold the event loop every
punch shares.

**What it writes.** `latest.json`, one `run-<timestamp>.json` snapshot per measurement
(`STANDING_SWEEP_KEEP` of them kept), and `state.json` with the corpus baseline the trigger reads
- all under `STANDING_SWEEP_DIR`. Put that on the volume (`/data/coverage_reports`) if the
history is wanted across deploys. The verdict is SCRFD against the **best** YuNet configuration of
that run, as a margin **in frames**: a one-frame margin is a tie in everything but arithmetic, so
`STANDING_SWEEP_MIN_MARGIN_FRAMES` sets the step that counts.

**When the answer moves, it says so.** SCRFD overtaking YuNet raises an administrator
notification (`coverage_report`, warning, once per direction per day) - and so does SCRFD ceasing
to be ahead, because both are the same finding: the cameras or the corpus changed and the
comparison has to be re-read. A verdict that did not change raises nothing, and a run where SCRFD
was not measured raises nothing at all (a comparison nobody ran cannot have overtaken anything).

**And when it stops standing.** The report is silent by construction - nobody notices a
measurement they did not ask for - so `GET /api/v1/readiness` carries an advisory
`coverage_report` check: it fails when new frames are waiting and the last measurement is past a
cadence plus the startup delay, and when the last measurement has three lines where it should
have four (SCRFD left out, which is the paper-assumption the fourth configuration exists to
replace). Advisory, never fatal: no punch depends on a detector comparison. The same verdict is
readable live at `GET /api/v1/admin/coverage_report` (administrators only), and a deployment that
would rather schedule it elsewhere sets `STANDING_SWEEP_ENABLED=0` and runs
`python -m coverage_report` from cron - the check says so rather than letting a stopped
timer look like a quiet one.

## Where a face match is decided

A distance is not a verdict, and a threshold is not a number: it is a decision measured for
one crop and one model. Both live together in `backend/face_detector.py`, keyed by exactly
that pair:

| pipeline | model | approved at or below | review up to | refused above |
| --- | --- | --- | --- | --- |
| `yunet-2023mar` | VGG-Face | 0.40 | 0.50 | 0.50 |
| `mtcnn` (when the YuNet model is absent) | VGG-Face | 0.40 | 0.55 | 0.55 |

**How those lines were derived.** Two boundaries can be measured: the *genuine ceiling* (the
highest distance between a photograph and the same person's template) and the *impostor
floor* (the lowest distance between two different people - what a false accept has to clear).
Measured on this deployment's own material and on a public multi-identity corpus, through this
pipeline's own code path:

| boundary | value | what it rests on |
| --- | --- | --- |
| genuine ceiling | **0.233** | every same-person pair this deployment has produced - 18 capture variants (0.019-0.144) and both enrolled accounts against each other (0.165-0.233) |
| impostor floor | **0.676** | the closest of 495 different-people pairs at punch scale (240 px JPEG q60, the size a phone sends), and of 1,269 across four corpora and both pipelines (0.676-0.796) |

The approve line is the geometric centre of that window (0.40: the point with the largest
margin to both boundaries), and the refuse line sits 1.35x below the impostor floor, rounded
to the 0.05 a decision is actually made at. `MatchBand.derived()` recomputes both lines from
the evidence recorded beside them, and a test asserts the shipped numbers are what that rule
produces - a line cannot be moved without moving the measurement that justifies it.

**Why they are bound to the pipeline.** The pair this replaced - 0.40 and 0.60 - was
DeepFace's published setting for *DeepFace's* crop, inherited when detection moved to YuNet,
tied to nothing, and re-derived by nobody. It happened to sit inside the new window, so
nothing failed and nobody looked. The table is keyed by the pipeline name and the recognition
model, `band_for()` has no default, and `/api/v1/readiness` carries a **fatal**
`face_match_band` check: a build that can run a crop whose lines were never derived refuses to
open the port rather than approve against numbers measured elsewhere. A recognition-model swap
is the same event wearing the same pipeline name, which is why `biometrics` also refuses a
stored template whose recorded model is not the live one.

**Re-measuring after a crop or model change.** Both boundaries come from one shape of
measurement: embed a corpus in which you know *who* each photograph is (one directory per
identity), through this application's own path (`face_engine.ENGINE.represent_direct`), then
take every within-identity pair as genuine and every across-identity pair as impostor, and
read off the maximum of the first and the minimum of the second. Do it in the deployment's
own capture conditions - the phone's 240 px JPEG, not the original file - because that is
where the floor sits lowest (0.676 at punch scale against 0.730 at full resolution, measured).
Record both boundaries on the new band, add it to `face_detector.BANDS`, and let
`test_face_match_bands` tell you whether the lines you typed are what the rule derives from
the evidence you recorded.

**What is still unmeasured.** The corpora are clean, frontal and evenly lit, which is the
*easiest* end of impostors, so 0.676 is an upper bound on the real floor. On the harshest
corpus - the same person, a decade and a camera apart - genuine pairs reach 0.885, *above*
that floor. That overlap is why the band has a middle: a distance this deployment has never
produced for either class goes to a human, and only a distance past the refuse line is
answered at the gate with "take it again". `attendance_face_match_score` is the histogram that
shows how the live distribution sits against these lines; if `flagged_review` climbs, the
lines were measured on a deployment that no longer exists and want re-deriving.

## Per-site shifts (including overnight)

Each site can run its own clock-in window. A site with no window configured uses the global
rule from Admin → Shift rules, and the fallback is **per field** - so a night site can set
only its hours and keep the company timezone:

```bash
curl -X POST localhost:8000/api/v1/admin/sites/edit \
  -H "Content-Type: application/json" \
  -d '{"site_name":"New Capital Zone B","location_input":"29.98,31.75","radius":100,
       "clock_in_window_start":"21:30","clock_in_window_end":"05:30",
       "site_timezone":"Asia/Kuwait"}'
```

**From the console** (the way it is meant to be set): *Sites* → **Edit** on a site, or fill the
window in on the *Add a site* form. Each card names the window **in force** and whether each half
of it came from the site or from *Admin → Shift rules*, so "why was this arrival flagged?" is
answered on the same screen as the site. Blank times mean "follow the company window", and *Use
the company window* on an existing site empties them - which is what produces the explicit nulls
that clear an override. The company window itself now sits on *Admin → Shift rules* next to the
paid hours: it takes the same `HH:MM` and IANA-timezone checks as a site's, and emptying a box
puts it back to the shipped `04:00`–`06:30`. Before this, neither window could be chosen in the
console at all - the endpoint existed and nothing called it, which for the person at the gate is
the same as the feature not existing.

`21:30`–`05:30` is an overnight window: it runs past midnight, and **both** 23:15 and 04:30 are
inside it. That is the case the old global rule could not express - a window whose start is
after its end was never true, so every arrival at that site was flagged late, on-time ones
included. Boundaries are inclusive to the minute (04:00–06:30 contains 04:00 and 06:30),
a one-minute window is written `06:00`–`06:00`, and a site that is open around the clock is
`00:00`–`23:59`.

The comparison happens on the **site's** clock, not the server's, so moving the deployment to
another machine (or another country) does not change who is late.

* `clock_in_window_start` / `clock_in_window_end` must be strict 24-hour `HH:MM`. `7:30` is
  refused where it is typed, and the column has a database `CHECK` behind it for anything that
  arrives another way.
* `site_timezone` must be an IANA name (`Asia/Kuwait`, `Asia/Riyadh`); a typo is refused
  rather than silently falling back.
* The company window on `POST /api/v1/admin/shift_rules` is checked by the **same** rules, at
  the same endpoint the Shift rules panel posts to. An emptied box clears it back to the
  shipped `04:00`–`06:30` - stored as `''`, because those columns are `NOT NULL`, and read as
  "nothing configured" by both the rules loader and the resolver. A `25:00` typed here used to
  be stored and then resolved to `00:00`–`23:59`, i.e. a company window that made nobody late.
* Sending `null` (or `""`) for a field clears the override, so the site inherits again.
  **Omitting** it from an edit leaves it alone: a form written before this feature existed, or an
  integration that only moves a pin, must not erase a shift. The console's own editor sends all
  three, because it *is* the window editor.
* `GET /api/v1/admin/sites` returns both the configured columns and the resolved `window`,
  with a `source` per field saying whether it came from the site or the global rule.

An arrival outside the window is **flagged, never refused**: the punch is recorded, the
session carries a `late_flag`, and an administrator gets a notification naming the window that
was actually applied. `GET /api/v1/readiness` reports any stored window the application could
not parse - which is the only way a bad value becomes visible, because the punch path falls
back to the global rule rather than failing a worker's arrival.

### Site categories (a warehouse, a factory, a project)

Between the site and the company rules there is a third layer: a **category**. Sites are grouped
into categories - `مخزن` (warehouse), `مصنع` (factory), `مشاريع` (projects) are seeded on a fresh
database - and a category may carry the clock-in window for every site inside it. The order is
**site → category → company**, still resolved per field:

* a site that has set its own hours ignores its category's entirely;
* a category that sets only an opening time leaves the closing time to the company rules;
* a site in no category resolves exactly as it did before categories existed.

The retune Ops asked for - "the warehouses open at 07:00 now" - is therefore **one edit**, and it
moves every warehouse at once, including sites that have not been visited in months. Nothing is
copied onto the member sites: their own columns stay `NULL`, so `GET /api/v1/admin/sites` can
still say *which layer* each half of the window in force came from, and a later category edit
cannot stamp over hours somebody deliberately set on one site.

```bash
# What is in the list, and how many sites follow each one
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/admin/site_categories

# The retune itself. The id is the key: renaming is display-only.
curl -X POST localhost:8000/api/v1/admin/site_categories/edit \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"category_id":1,"name":"مخزن","clock_in_window_start":"07:00","clock_in_window_end":"15:00"}'

# Put a site into one, on the site form (null - or the empty option - takes it out again)
curl -X POST localhost:8000/api/v1/admin/sites/edit \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"site_name":"Downtown Tower A","location_input":"30.05,31.23","radius":65,"category_id":1}'
```

* `GET|POST /api/v1/admin/site_categories[/add|/edit|/delete]` is the whole surface, and it
  mirrors `/admin/sites` deliberately: the same absent-key rule (omitting a window field leaves it
  alone, `null` clears the override), the same `HH:MM` and IANA-timezone checks, and the same
  audit trail (`site_category_create|edit|delete`).
* **Deleting is refused while sites still belong to it** (409, naming the count), rather than
  reassigning them: every destination would be a guess about those sites' opening hours. Move them
  to another category, or to none, first.
* On the Shifts tab the categories appear as chips beside the period presets, each carrying the
  number of shifts **in this period** behind it - so a chip cannot offer an empty table - and the
  search box matches a site's category, so typing `مخزن` finds every warehouse shift. The chosen
  category travels in the shareable link (`#shifts=…&q=…&c=…`) and narrows the printed and
  downloaded timesheet exactly as it narrows the screen.
* The **Sites** tab gets a category picker on both forms - with *No category* first, which is what
  every existing site is - and a *Site categories* panel to add, rename, retune and delete them.
  A card names the category the site is in, and a window that came from a category says so.
* `GET /api/v1/readiness` checks the categories too, not only the sites: a category whose zone no
  runtime can resolve moves every site inside it at once, and it is reported as `category <name>`
  rather than as one of its sites - which is the screen where the fix is made.

## Which rule ends the day (the automatic close and the overtime alert)

One timer, two rules, and only one of them can end a day. `overtime.py` runs both every
`OVERTIME_WATCHER_INTERVAL_SECONDS`:

| Rule | Acts at | What it does |
| --- | --- | --- |
| **Automatic close** (`auto_close_at_regular`) | `regular_hours` **paid** - 8 by default | Writes the clock-out itself *at the boundary*, marks the shift `Auto-Closed (8h Limit)` / `auto_closed_8h`, and notifies the administrator **and the worker whose day it ended**. It ships **off** - see *which rule ends the day* below. |
| **Overtime crossing alert** (`overtime_notify_hours`) | 8.1 **paid** by default | Alerts the administrator that a shift is **still open** past the threshold. |

They run in the same pass, and the close deletes the session it closed - so a shift the close
has ended can never be observed crossing the alert line. Whichever rule acts first therefore
decides whether a crossing is reported at all. **Which rule wins is decided in one place**,
`shift_hours.day_end_rules()`, read by both scans so the figures they act on cannot drift:

| Settings (close on) | Who ends the day | Why |
| --- | --- | --- |
| alert **below** the paid day (`8.1`? no - `7.5` < `8.0`) | the **close**, at 8 h | the crossing is reported at 7.5 h while the shift is open, then the standard day still ends at 8 h. |
| alert **above** the paid day (`8.1` > `8.0`) | the **overtime workflow** | a shift closed at 8 h could never reach 8.1 h, so the close **stands down**: the shift runs on, the crossing is reported at 8.1 h, and hours past 8 h are clocked out into overtime review. **Nothing is auto-closed at 8 paid hours.** |
| alert **on** the paid day (`8.0`) | the **close**, at 8 h | there is nothing to observe: "crossed the paid limit and is still working" is false at that figure, and the alternative is paging the manager on every ordinary full day. |
| close **off** - the shipped default | a **clock-out** (or nobody) | the alert is then the only thing watching, and it always fires. |

The shipped pair is the **last row**: `auto_close_at_regular` is off, and that is the honest
form of what the shipped alert line already asked for. With 8.1 above 8.0 a switched-on close
stands down, so a deployment that shipped it on switched nothing while the console showed it as
*on* - and every fresh volume was born reporting `overtime_close_deferred`. An alert line above
the paid day only means anything if shifts are allowed to reach it, so what ships is the
arrangement with nothing standing down: the close is off, the alert is the rule that acts.

**Answering a crossing is its own permission.** The tables above govern the close at the
**paid day**. An *approval* in the Approvals queue - the operator naming a ceiling for a live
shift - arms the same close at that ceiling whether or not `auto_close_at_regular` is on and
whether or not it is standing down. A worker still on the clock at 14 h authorised for 12 is
checked out at the 12 h boundary; a worker at 9 h authorised for 12 is left running and closed
the moment they reach it. Both reasons the gates exist are answers the approval has already
given: the switch off means "a human ends the day", and the deferral exists so a crossing can
be *observed* - an answered crossing has been. A **refusal** is not an approval, so declining a
crossing never ends a day from a surface about money. The close runs on the watcher's pass, so
an approved shift ends within `OVERTIME_WATCHER_INTERVAL_SECONDS` (a minute by default).

To get the automatic close back, do **both** halves of the pair - switch it on *and* set the
overtime line **strictly below** the paid day (7.5 h). That is also what being warned *before*
the day ends looks like. `OVERTIME_WATCHER_ENABLED=0` stops *both* rules.

A deferral is never silent, and neither is the one unreachable case (the alert on the paid
day). A deployment that has not been reconfigured reports neither: the close ships off, so both
advisories come back green on a new volume. The verdict travels in four places:

* a **startup log line** from `overtime.start_watcher()` - `WARNING` when the close has stood
down or the alert cannot fire, naming both numbers and the way out;
* `GET /api/v1/admin/shift_rules` returns a `day_end` block (`regular_hours`, `notify_hours`,
`close_at_paid_hours` - `null` when the close is off, or has stood down - `close_defers`,
`alert_reachable`, `day_ended_by`, and the sentence explaining it); the console's **Shift
rules** panel warns from it on the screen where the numbers are typed;
* `GET /api/v1/readiness` reports both halves as advisory checks - `overtime_alert_reachable`
(the alert can fire at all) and `overtime_close_deferred` (the automatic close is standing
down, so a switch that still reads as *on* is no longer closing anything);
* `overtime.scan_auto_close()` returns `deferred: true` with the reason instead of a quiet
zero, and the watcher logs each pass where it stands down.

**The cost of shipping the close off** is the one that was already documented for switching it
off, and it is now what a deployment starts with: **nothing ends a forgotten shift.**
Two consequences, and the second one is easy to miss:

1. a worker who never clocks out keeps accruing hours that wait for approval, and the alert
   fires once when the crossing happens;
2. **an open shift refuses the next clock-in** (`Already clocked in!`), so the same worker
   cannot start the next day until somebody ends it. The alert says so, and *Force clock
   out* in the console is how an administrator ends it.

If you want the system to end the day for you, switch the close on and put the overtime line
below the paid day - and accept the alert that comes with it, because a crossing the close can
see is a crossing the manager is told about.

## The overtime line, and what it counts

`overtime_notify_hours` (8.1 by default) is a **paid-hours** figure - the same hours
`regular_hours` counts, i.e. time on site **less the unpaid break**. It is never time on
site: a worker on site for 8.5 h has been paid for 8, so reading the line as on-site hours
would put every ordinary full day on overtime. The basis is named once, in
`shift_hours.OVERTIME_BASIS`, and the console says *paid* wherever the line is named.

**One resolver, every path.** `shift_hours.overtime_assessment()` is what answers "has this
shift crossed the line, and how much of it is held back?". The punch path, the quick-link
path, the offline materialization, the clock-out an administrator forces, and **both**
watcher scans read it - none of them resolves the column itself. That is not tidiness: the
four paths used to resolve it their own way and disagreed. The gate was strict (`>`) where
the watcher was inclusive (`>=`), two paths compared the *recorded* hours (which round a
shift up to the paid day) against a line meant for real paid hours, and one of them wrote a
sentence into `flag_reason` calling the overtime line the "regular threshold".

**The rule it encodes** (`needs_approval`):

| | |
| --- | --- |
| the comparison is on | seconds - the resolution the timestamps are stored at |
| the operator is | `>=` - *reached*, not passed, so the worker's card, the administrator's alert and the clock-out gate fire on the same second |
| a shift is held when | its paid time has reached the line **and** it leaves time past the regular paid day |
| what is held | the paid time **past the regular day** (`overtime_hours`) - what the approval queue, the payroll totals and the rejection arithmetic read |

Both halves of the third row matter. A line *below* the paid day is a warning set early, and
without the second half every ordinary full day would be held for approval with a zero
overtime figure on it - which is what the code did before this was one function. A shift that
ends between the paid day and the line is therefore an ordinary approved day: **the line
triggers the alert, the paid day starts the overtime.**

### What a shift is worth *before* anybody decides

A worker/moallem works eight hours as a matter of course, so **the standard day is earned the
moment the shift is filed** and only the time past it waits on a manager. A 9.0 h paid shift
that has crossed the line reads:

| | pending | after approval of 8.25 h | after refusal |
| --- | --- | --- | --- |
| `approved_hours` | 8.00 | 8.25 | 8.00 |
| `awaiting_approval_hours` | 1.00 | 0 | 0 |
| `overtime_hours` | 1.00 | 0.25 | 0 |
| status | `pending_overtime` | `approved` | `overtime_rejected` |

Withholding the *whole* shift made an ordinary day conditional on somebody pressing a button,
and it contradicted the refusal path beside it in the same queue, which has always paid the
standard day. The two halves of that queue now agree, and the approval still overwrites the
provisional day rather than adding to it - the decision is the decision.

**The other hold is not the same thing.** `pending_review` credits nothing: it is a doubt
about whether the work happened at all (a liveness verdict, a selfie that did not match, a
punch with no face), so there is no standard day to credit ahead of it. Only
`pending_overtime` - the hold that is about the *extra* hours rather than about the work -
gets the split. A `pending_overtime` row carrying no overtime figure (or a nonsense one)
fails closed and credits nothing, because there is then no figure to separate the day out with.

`reports._shift_hours()` is the one place that decides this, and every status satisfies
`counted == approved + awaiting` by construction - which is what makes the two columns add up
to the hours the sheet shows. The server sends `awaiting_approval_hours` **per row** so the
console totals a filtered view with the server's own arithmetic rather than assuming an
awaiting shift holds all of its hours; the fallback to `row.hours` is for a payload from an
older server, and keeps the two figures summing either way.

**The worker is told, whichever way the shift ends.** The crossing alert is not the watcher's
alone: a timer can only see a shift that is still open, so a worker who crossed the line and
clocked out before the next pass used to learn nothing while the administrator's review alert
arrived. `overtime.announce_crossing()` is now the one writer of that notice - the watcher
calls it for a shift that is still running, and so does every path that can end one (the
punch, the quick link, the offline punch materialized later, and the clock-out an
administrator forces). One sentence, the figures taken from the shift's own clock-in, and a
`dedupe_key` naming the *shift* rather than the caller - so a worker alerted while still on
site and then clocking out is told once, not twice. It lands in the worker's own inbox
(`worker_notifications`, kind `overtime_crossed`) and is pushed to their phone after the
transaction commits (`push.dispatch_async()`; a deployment with no VAPID key pair configured
simply has no push - the inbox row is the record, and `readiness` reports the missing key
pair as an advisory).

The crossing notice is about the *shift*, though, and it was the last word the worker had until
their clock-out - hours of not knowing whether staying on was paid. So the **answer is its own
notice**: `overtime.decide_crossing()` writes it in the same transaction as the decision it
reports, stating the ceiling, who authorised it and what is left unauthorised (kind
`overtime_authorised`; a refusal is `overtime_declined`, a different kind because "authorised up
to 10.5 h" and "no extra time is authorised" are opposite statements that no single kind could
carry). Its `dedupe_key` names the **decision**, not the shift: a ceiling an operator extends has
to reach the worker twice, because the extension is the answer their clock-out settles at, while
a repeated request cannot (same row, same key). The operator's note is deliberately *not* in it -
it is written for the record, and the console promises a worker never sees a reviewer's note - so
what the worker gets is the figure and who set it. `push.dispatch_async()` runs after the
endpoint's transaction commits, through the same `deliver_worker_notices()` the crossing uses.

The record is **readable on the phone**, not only pushed to it: the handset's *Alerts* tab is
that inbox (`GET /worker/me/notifications`, the notifications the token's own account was
sent, newest first, with a read state), and the clock panel carries the count above the clock
button - a band naming the newest unread notice with the way in. The band is on the clock
panel on purpose: the close writes its notice because the next clock-out refuses the worker,
so the sentence has to be in front of them *before* they tap that button. Two details are
deliberate and pinned by tests: the badge is one number read once, from the endpoint with
`unread_only=true`, so the tab and the band can never disagree; and marking one read sends its
id in the **query string**, which is where the route reads it (`notification_id`), because the
same request with a JSON body would be answered like "no id" and mark the whole inbox read.
Nothing about a notice the system wrote - an auto-close, a crossing - is lost on a deployment
with no push at all: the row is what reaches the worker, and the push is only the shortcut to
it.

The **push half** closes the gap the inbox cannot: a notice reaches a worker whose app is
closed. `push-service-worker.js` is registered at boot - before any permission is asked, and
for every account - because a web push cannot exist without a service worker, and the first
enable must be one tap on a site's cellular link, not a tap plus a registration wait. Nothing
prompts at boot: a permission fired unprompted is how an app teaches people to refuse prompts.
The profile tab's card reads `GET /worker/me/push` first and names its state honestly -
configured with a count of devices, not configured *with the server's own reason*, or a
browser too old to register a worker - and offers a switch only where one can work. Enabling
asks the permission, creates the subscription against the deployment's VAPID public key, and
POSTs it; a backend refusal releases the browser-side subscription too, so the card never says
"on" for a subscription no server will send to. Disabling releases the browser side whatever
the server answered - the worker asked for off, and a row the server could not retire is
cleaned up by the next failed delivery, not by this phone. A tap on the notification focuses
(or opens) the app and hands it the notice id; the page marks it read in the query string the
same way the inbox does, so the badge on the lock screen and the badge on the tab agree.
The worker file holds no token and no state - it only ever *receives* - and the settings
card's strings ship in all four languages.

The operator's side of that - generating the key pair, which variables carry it, and how to
watch a notice travel from a shift that ran long to a phone that is in a pocket - is
[`docs/RUNBOOK_WORKER_PUSH.md`](docs/RUNBOOK_WORKER_PUSH.md), kept honest by
`backend/tests/test_worker_push_runbook.py`, which fails when a renamed setting, a changed
default or a moved route makes the runbook wrong. The sender is an **optional** dependency
(`pywebpush`, in `backend/requirements-optional.txt`): a deployment without it behaves exactly
as one that never configured push, and says so with its own reason rather than failing.

The channel's own failure mode is a **silence**, and a silence is the one thing an
event-driven alert cannot report: a push service answering 401, a subscription retired under
the worker's feet, a permission revoked - each leaves every setting valid and no row behind.
So the backlog is measured instead. `push.stranded_notices()` reads the notices that passed
`PUSH_MAX_AGE_MINUTES` still undelivered (past that window nothing is retried, so those will
*never* be pushed), and keeps the two halves an operator has to act on differently: notices
whose worker has no live device - nothing was sent, nothing can be until that worker allows
notifications - and notices whose device is registered, where the push service refused or
failed the send. The reading reaches three surfaces an operator already watches: an
`admin_notifications` row (`push_undelivered`, warning, one per hour so a persistent backlog is
re-said without becoming an alarm nobody reads), the `worker_notice_backlog` advisory on
`GET /api/v1/readiness` (the one an uptime monitor can poll), and a WARNING line on the watcher
tick. It is taken on two clocks - the watcher pass and every `push.dispatch` - because a channel
that has gone quiet is exactly the case where nobody is punching, and the punches that would
otherwise take the reading stop arriving with the notices they would have reported. The rule
that keeps it trustworthy is the quiet one: a deployment that does not intend to push is never
told about its own inbox. With no key pair, or `PUSH_ENABLED=0`, a backlog is the expected
state, and `worker_notice_backlog` says "not measured" rather than inventing a fault - so the
check has teeth exactly where nothing else can see the problem.

The **automatic close** has its own notice, in its own kind (`shift_auto_closed`), written in
the same transaction as the administrator's alert: it ends a shift with nobody asking it to,
so the worker is told *that it happened* - when the day was closed, what it was paid, and
that their next clock-out will find nothing open - rather than discovering it from the
refusal at the gate. The kind is deliberately not `overtime_crossed`: a close records exactly
the paid day, so there are no held-back hours for a crossing sentence to be about.

The sentence states the event and the rule - you passed the line at this instant, and time
past the paid day needs approval before it is paid - never the outcome, because on the
*Force clock out* path the hours have already been authorised by the administrator who ended
the shift and a notice reading "waiting for approval" would be false there. The one
difference between an open shift and a finished one is the closing clause ("clock out when
you finish"). A clock-out path announces only when hours are actually held: a shift that
stops between an early line and the paid day has crossed the line and is paid in full, so
there is nothing to tell the worker about their pay.

The held hours are written to `attendance_logs` as `Pending Overtime Approval` /
`pending_overtime`: visible on the timesheet, excluded from approved hours until an
administrator decides them, and a rejection leaves the standard paid day intact.
`overtime_notify_hours` is range-checked on write like `regular_hours` (0 < x ≤ 24) - a line
at 0 held every shift of every length for approval, and a line at 900 silently meant nobody
was ever on overtime.

## Worker enrollment

**Self-service (preferred).** An admin issues a one-time link:

```bash
curl -X POST localhost:8000/api/v1/admin/enrollment/invites \
  -H "Authorization: Bearer <admin token>" -H 'Content-Type: application/json' \
  -d '{"worker_id":"1"}'
```

It returns a URL (and a QR data-URI when `qrcode` is installed). The worker opens it on
their own phone, captures their face at `/enroll/<token>`, and the template is written.
The token is single-use, expires (72 h default), and only its SHA-256 is stored, so a
database dump cannot be replayed.

**Registration links (self-service, for a person who does not exist yet).** The same
endpoint with `"kind":"register"` reserves a *new* account and lets its owner finish it:

```bash
curl -X POST localhost:8000/api/v1/admin/enrollment/invites \
  -H "Authorization: Bearer <admin token>" -H 'Content-Type: application/json' \
  -d '{"worker_id":"77","kind":"register","name":"Ali Hassan","role":"worker"}'
```

They open the link at `/enroll/<token>`, choose their own password and take their own
photo; `POST /api/v1/enroll/<token>/register` creates the account, writes the face
template and consumes the link, in that order - every step that can fail happens before
anything is written, so a rejected password or an unreadable photo costs a retry rather
than the link. The **id and the role are the administrator's**: a link can create a
`worker`, a `moallem` or an off-office worker (never an administrator), it is single-use whatever `max_uses`
says, and it refuses an id that is already taken or already **reserved by another live link**.
The reservation is real rather than cosmetic: the number lives on the invite row until the link
is claimed, and the walk-up allocator below treats it as taken, so the two creation flows can
never hand the same id to two people. Revoking, expiring or claiming the invite releases it. The Credentials tab has this behind one
button, with the URL, a copy button, a WhatsApp message and the QR code.

**Walk-up registration (one standing link, no account yet).** An invite is issued per
person; a *walk-up* link is the opposite - a single public URL a site can print or display,
where anybody applies and nothing exists until an administrator decides. `GET /api/v1/register`
reports whether it is open and what the form must satisfy, `POST /api/v1/register` streams the
photo to disk and writes a `registration_requests` row (`PENDING_REVIEW`) - never an account,
a face template or an id. An administrator lists the queue at `GET /api/v1/admin/registrations`,
views the photo at `.../{id}/photo`, and *then* approves or rejects:

```
GET  /api/v1/register                      -> whether it is open, the roles, the photo policy
POST /api/v1/register                      multipart: photo + name, phone, role, password,...
GET  /api/v1/admin/registrations           the queue, oldest first, no password, no path
GET  /api/v1/admin/registrations/{id}/photo     the application photo, `Cache-Control: no-store`
POST /api/v1/admin/registrations/{id}/approve   {"note": "..."} -> creates the account
POST /api/v1/admin/registrations/{id}/reject    {"note": "..."} -> wipes the photo, keeps the row
```

Approval is the only thing that creates an account, and it is atomic: it allocates the
**lowest free id in the role's block** - an id a live registration invite is holding counts as
taken, so an approval can never spend a number another administrator already promised to
somebody - writes the user row and its face template behind one write lock, and clears the
pending row in the same movement, so two administrators approving the last slot cannot both
think they won. The password the applicant chose is never returned,
logged or selected by the queue - the applicant would otherwise learn the account's credentials
from a screenshot of the review list. The link is off unless `REGISTRATION_ENABLED=1`, capped by
`REGISTRATION_PENDING_CAP`, rate-limited per IP (`REGISTRATION_RATE_LIMIT`), and deliberately
does **no model work** on the public route: the 1 vCPU that serves the punch gate must not be
spent on a stranger's upload. Liveness, if it applies at all, runs at review.

**Bulk import.** `POST /api/v1/admin/enrollment/bulk` with `roster` (CSV: `user_id,name`
plus optional `role,email,phone,photo`) and `photos` (ZIP). Add `?dry_run=true` to
validate without writing any biometric data. Progress is pollable at
`/api/v1/admin/enrollment/jobs/{id}`; embedding happens in the background, and one bad
row never aborts the batch.

The admin console does not ship a dedicated enrollment screen any more: the
**Credentials** tab (see *Accounts and access* below) replaced the old enroll dashboard.
It reports whether each account already has a face template, it can create an account
with its photo in one step, and it can issue the link above. Enrolling - by invite link,
by registration link, by bulk job, or by `POST /api/v1/admin/enroll` - is all still
available.

**One upload policy, everywhere.** The clock-in selfie, the console's enrollment photo,
the photo a registration link carries, the self-service capture and every photo inside a
bulk ZIP are all measured by `backend/uploads.py`:

* **5 MB** (`UPLOAD_MAX_PHOTO_BYTES`), enforced *while reading* the body - a limit checked
after `await upload.read()` is not a limit, the memory has already been spent;
* **a real image, decided by the bytes** - the `Content-Type` and the filename are both
chosen by whoever sends the request, so the first bytes must carry a JPEG, PNG or WebP
signature *and* the file must decode as an image;
* **a pixel ceiling** (40 MP), because 5 MB of JPEG can describe a 300 megapixel image and
decoding that is where the memory actually goes.

A refusal is a small JSON body with a stable `error_code` (`photo_too_large`,
`unsupported_media_type`, `empty_upload`, `not_an_image`, `unreadable_image`,
`image_too_large`) and a sentence the person holding the phone can act on. Both the
console and the public capture page check the size and the type before uploading too, but
only as a courtesy: those checks are the ones an attacker chooses to skip.

## Offline punches

Workers with no signal still clock in and out. The client registers a signing device
once, fetches a server-signed *time anchor* while online, and later submits punches
signed with HMAC-SHA256. The server derives the authoritative time from the anchor plus
the device's monotonic elapsed time, so a changed phone clock cannot move a punch.
Details, including the residual risk of a long offline window, are in
`backend/offline_sync.py`.

```
POST /api/v1/attendance/devices/register   -> device key (returned once)
POST /api/v1/attendance/anchors            -> {anchor_id, server_time, anchor_signature}
POST /api/v1/attendance/sync               -> batch of signed punches
POST /api/v1/attendance/sync/photo         -> the selfie a queued punch kept, scored here
GET  /api/v1/admin/punch_queue             -> triage, including refused punches
```

**Materialising a punch is not confirming it.** A punch taken with no signal was never checked —
not the face, not the frame, not even the site — so nothing that comes out of the queue is approved.
The clock-out, the row that carries the hours, lands in `pending_review` (or `pending_overtime` when
the shift crossed the overtime line, which is the same hold with a sharper reason) with the
provenance in `flag_reason`; the hours are recorded and are not payable until an administrator
approves them from the review queue, and the phone is told exactly that: `pending_hours` in the
worker's own totals, not `regular_hours`.

The arrival is recorded as `unverified_offline` instead, and that difference is deliberate: it
carries no hours for anybody to decide, and a `pending_review` row blocks that worker's next
clock-out (the online punch and the quick links both refuse while one exists) — so holding the
arrival would leave somebody who came in during a dead spot unable to close their shift, with the
day never recorded at all.

The selfie the phone kept is uploaded afterwards, through the same size-and-type policy as every
other photo in the app, and bound to its punch by the `photo_sha256` inside the signature — a
different image is refused rather than scored. The server then runs the online punch's own checks
over it (liveness, then the face comparison) and writes the verdict onto the review row: the score,
the liveness verdict and a sentence the reviewer reads. It is evidence, never a decision — a model
that runs hours later is not a witness, so scoring cannot approve an offline punch and cannot refuse
one either, and a spoof it finds is recorded and notified without touching what the shift is worth.

## Reporting

```
GET /api/v1/admin/reports/shifts?start=&end=       the timesheet: one row per shift
GET /api/v1/admin/reports/attendance?start=&end=   attendance rate per worker
GET /api/v1/admin/reports/pending                  everything awaiting a decision
GET /api/v1/admin/reports/export?kind=shifts&format=csv|xlsx
```

`/admin/reports/shifts` was called `/admin/reports/payroll` until the tab was renamed, and
`kind=payroll` on the export was the matching default. Both still answer, unchanged, so a
saved command or a link shared before the rename keeps working; the app, the docs and the
tests all use the new names. (`/admin/reports/export` is the export for scripts and for
`format=xlsx`; the admin console builds its CSV from the rows on screen instead.)

`/admin/reports/shifts` is a **timesheet**, not a payroll report: one row per shift, with the
day it ended, the worker, their id, the site, the hours it counts for, whether an
administrator has signed it off (`awaiting_approval`), and how many notes that worker has
open. It also carries the **arrival** the shift started with - `arrival_time` (the clock-in
that was paired with this shift), `arrival_verdict` (`on_time` / `early` / `late`) and
`arrival_minutes` (how far outside the window, `0` when on time) - and
`totals.late_arrivals` beside the hour totals. It carries no `hourly_rate` and no `gross_estimate` - an app that pays nobody should
not ship a sheet that looks like it is about to. The rate is still a field on the user
record (the Credentials tab edits it); nothing multiplies it.

A shift is only worth its hours once somebody has approved it: until then
`awaiting_approval` is true, its hours are in `awaiting_approval_hours` and **not** in
`approved_hours`, and an administrator approves it through
`POST /api/v1/admin/approve_review`. An approved row then reports the *approved* figure
rather than the raw clock - 8.25 h of a 9.5 h shift, with `recorded_hours` kept beside it.

In the admin console, the **Shifts** tab opens on this month and lets the admin pick a
period - or tap *This month* / *Last month* / *This week*, the week running from Sunday to
match `working_days`. It shows the totals (hours, approved hours, hours and shifts awaiting
approval, unpaid break, shifts, workers, late arrivals) and then the shifts themselves.

The table's columns are the administrator's to arrange. They arrive in this order -
**Date, Employee, Role, User ID, Site, Arrival, Hours, Awaiting approval, Open notes** - and
the *Columns* panel moves any of them earlier or later, remembering the choice in
`localStorage` so it survives a reload without changing what any other admin sees. A stored
order that is stale or junk is repaired rather than obeyed (unknown columns dropped, new
ones appended).

**Role** is the column that answers *whose shifts are these*. A normal administrator who
also works a site clocks in like anybody else, and their row is the one a reviewer has to be
able to pick out of a month of rows. It is written in the reader's language -
*Administrator*, not `admin` - and the search box below matches it in both forms, so
`administrator` finds their shifts and so does the Arabic word for the role. A row whose
account no longer exists says the role is unknown rather than leaving the cell empty, which
would read as "worker".

**Arrival** says whether the shift's own clock-in was inside the window its site applies -
*On time*, *Late 42 min*, *Early 5 min* - and *No clock-in* when there is no arrival on file
for that shift (a force-clock-out, or an auto-close whose clock-in predates the pairing).
Unknown is not the same as punctual, so it is stated rather than rendered as a tick. The
verdict is computed by the same function the gate uses (`shift_windows`), from the site's
own hours and timezone with the company rule as the per-field fallback, so the column cannot
contradict the flag the worker's card showed at the time. The window applied is the one in
force when the report is read: this application has never stored the window a punch was
judged against, so a site that has since changed its hours is judged by the new ones.

That column makes the timesheet answer the question a foreman actually asks - **who arrived
late, and by how much**. Typing `late` in **Search** narrows the list to the late arrivals
and the *Late arrivals* figure above it to the same set; `late 42` is the ones 42 minutes
outside their window, and `on time` is the rest. The arrival is *not* in the CSV export: that
file is fixed at `Employee, id, site, hours` so two months of sheets line up column for
column, and the arrival is a judgement that changes if the window does. The printed PDF
sheet is the table on screen, so it keeps the column.

The **Download CSV** button writes a file from the rows shown, so a search downloads
exactly what the table shows (the API export has no free-text filter and always covers the
whole period, which is why the button does not use it). The file holds **`Employee, id,
site, hours`** - the same four columns, in the same order, as `kind=shifts` on the API
export, and the same figure the screen shows for each row to the precision the screen
shows it (the API export writes that figure unrounded, so a script gets the exact number
and a reader gets the same sheet). The screen's column order deliberately does not travel
into the file: a sheet whose columns move from day to day cannot be compared with last
month's.

The **Download** button writes either of two files, picked in the *Format* box beside it:
the CSV sheet described above, or the same report as a **PDF**. The PDF is the browser's own
print dialog - the console builds the sheet from the rows on screen and calls
`window.print()`, so *Save as PDF* is what writes the file, the reader's own fonts render
Arabic and Hindi names correctly, and the host needs nothing installed to produce it (`pip
install reportlab` on a server whose office is in Cairo is a font problem nobody asked
for). The sheet keeps the columns the administrator arranged rather than the CSV's fixed
four - paper is read by whoever set the table up - and carries the period and the total;
the dialog offers the period as the file name, the same name the CSV gets. As with the CSV,
a search travels into the sheet, and a period with nothing on screen prints nothing. The
paper rules themselves - the sheet, the file name the dialog offers, and putting the page
back when it closes - live in one `PrintReport`, shared with the reader's own timesheet
below, because they are the same three rules for either reader. The *frame* of the sheet is
shared for the same reason and lives there too: the title, the period line, the table, the
total and the note that says only approved hours count are built once
(`PrintReport.sheetHtml`), and each screen supplies only what is its own - its columns, its
rows, its total, what an empty period reads. A second hand-built frame is how one screen
ends up printing a sheet that states the note at its foot, or the arrow between two dates,
slightly differently from the other.

**Whose timesheet this is, at the top of every sheet.** That frame also carries the
company's own name and mark, so the paper an administrator signs says whose payroll it is -
and neither is a constant any more. The wordmark, the legal suffix, the founding year, the
tagline and the logo are a settings row (`company_settings`, one row, edited on the console's
**Admin → Company** panel), and `GET /api/v1/branding` is the public read: the login panel,
which by definition has no session, is the screen that most needs to say whose app this is,
and a wordmark on the one screen whose job is to answer *is this the app my administrator
told me about?* is the thing this replaced. Writing is `POST /api/v1/admin/branding` and the
logo endpoints beside it, `admin_only` like the shift rules, and audited - the log records
the mark's type, size and dimensions rather than its bytes. An uploaded logo is decoded,
bounded (40 MP, 5 MB, JPEG/PNG/WebP only) and **re-encoded** by the server: PNG when it has
transparency to keep, JPEG when it does not, scaled to 512 px on its longest edge, so what a
browser fetches afterwards is never the file that was posted, and a 4000-pixel phone photo
of a business card is a 40 KB mark rather than a 400 KB one. `?v=` in the served URL is the
stored version, so a replaced mark is a different URL and no cache can keep serving the old
one.

`null` and `""` are different answers here, deliberately: a line nobody configured is the one
this application ships with, and a line the company *emptied* is not printed - a business
with no legal suffix must be able to take that line off its own sheet, and a rule of "blank
means the shipped value" would put it straight back. So the four lines are nullable with no
default, the empty string survives the round trip to paper, and the console's panel has a
**Use the shipped lockup** button for "stop deciding" as distinct from "print nothing here".
The frontend merges the row over the shipped lockup once, in `Brand`, and every surface that
says whose this is reads that one record - login panel, handset header, console rail, footer
sentence, and the header of every printed sheet. The cache is applied before the first paint
and the network answer only corrects what changed since, so a phone at a gate never waits on
its connection to learn the company's name; a settings read that fails leaves the last lockup
that was known in place, which is what keeps an offline handset from naming the wrong
company.

**One worker's month, from that worker's row.** Every shift row carries a printer button -
the one cell on the row that is not a column - and it prints **that person's calendar
month**, which is a different document from the period sheet above in two ways that matter.
The subject comes from the row: the request is `/admin/reports/shifts?start=…&end=…&worker_id=…`,
so the server answers with one person's shifts rather than the period's, and a month longer
than the period on screen is a real month rather than a filter over whatever the table
happened to be holding. The period is the calendar month the chosen period falls in - the
tab may be showing a week, a single day, or a shared link's odd range and the sheet is still
the month - clipped to today while that month is still running, exactly as the presets are,
so a sheet never describes work that has not happened. On the sheet the three identity
columns leave the table and appear once above it (`Seed Lead · Moallem · id 600`), because a
month of one person's shifts would otherwise repeat the same three facts on every line; the
columns that remain keep the administrator's own order and the tab's own cells, and the file
the dialog offers is named after the person as well as the month, so two of these in one
folder are not told apart by opening them. A month that person did not work prints nothing
and says so, rather than handing over a titled blank sheet that reads as a lost timesheet.

**Your own month, for the person it is about.** The same figures are available to whoever
they describe, without an administrator passing them on: the handset's **History** tab leads
with the reader's own timesheet for a month they pick - hours worked, approved, still
awaiting approval, overtime, and the per-site split that answers "how much did I work
*where*" - from `GET /worker/me/report`, which is scoped by the token and takes no worker
id, so there is nothing in the request to point at somebody else's month. **Download CSV**
writes that list with the fixed English columns (`Date, Site, Arrival, Break hours, Hours,
Approved hours, Status`) and **Download PDF** prints it as a sheet: the same rows and the
same totals as the console's, because both are built from the report already on screen
rather than re-derived on the server - and the two sheets come off the same `PrintReport`,
so the row an administrator prints for one worker and the row that worker prints for
themselves are the same document seen from either side. A normal administrator who also works a site reaches
this screen by stepping onto the handset from the console (**Check In**), and a lead worker
by signing in, so all three roles read and print their own month the same way; a month with
nothing in it prints nothing rather than a blank sheet with a title on it.

**Which columns those two files carry is the worker's own choice.** The card's *Columns in
the file* panel is one checkbox per column - `date`, `site`, `arrival`, `break`, `hours`,
`recorded` (time on site beside the hours it counts for), `approved`, `status` - and
**Save columns** remembers it through `POST /worker/me/report/columns`, on the *account*
rather than in the browser: a timesheet is handed in from wherever the worker is standing,
so a choice kept in one device's storage would give the same account two different files and
would be lost with the phone. `reports.REPORT_COLUMNS` is the vocabulary, and it is the only
list either side reads - the server drops ids it does not know (so a client one release
ahead can still save, and a column this build cannot fill never becomes a column of empty
cells), refuses an empty choice (a file with no columns is not a report; the screen stops the
last box from coming off and says why), and stores the *default* choice as nothing, so an
account that has never narrowed anything gains a column in the release that adds one. A tick
takes effect on the very next download and Save is what makes it outlive the page - the same
rule the files already follow, that what is written cannot disagree with what is on screen -
and the sheet prints the chosen columns under their headings in the reader's language while
the CSV keeps its fixed English ones.

The period is kept in the URL fragment (`#shifts=2026-08-01..2026-08-31`), so copying the
address bar - or the **Copy link** button - hands a colleague the same figures. Opening
such a link lands on the Shifts tab with that period already loaded; an unrecognisable
fragment is ignored rather than shown as an error, and a fragment still using the old
`#payroll=` is honoured. A search travels in the same fragment (`&q=tower`).

Each row names the **site** that shift was worked at, and the **Notes** column carries the
worker's open notes (open or in progress) - four shifts by one person with a request in is
one outstanding note, not four.

The **Search** box narrows the rows without touching the period, matching on worker name,
worker id, role, site, day, approval state or arrival - every term has to match, so `harbour khan` is the
one shift that is both. The totals above the list then cover only the shifts shown, and the
card block is replaced by "no shift matches" rather than a grid of zeros. A day typed into
the box filters the rows like any other term (a timesheet row *has* a date) and still
offers a one-tap switch that moves the whole period onto it. Searching repaints from the
rows already in hand, so it costs no request - only a new period does.
Hours still awaiting approval are shown beside the approved ones and never added to them.

**Who is in the attendance figures.** `/admin/reports/attendance` counts the day somebody
walked in, so an administrator who covered a shift appears in it like anyone else - the same
`days_present` on the same `expected_days`, the same arithmetic turning it into a rate, and
no branch in the query for a role. Each row carries **`role`** beside the name for the same
reason the Shifts tab has the column: the figures are reviewed by role, and a row that names
the person without saying which of them is the administrator is a row somebody has to
reconcile by hand. The attendance export (`kind=attendance`) writes it as its third column,
`worker_id,worker_name,role,...`, so it travels into the file. The two review queues an
operator works through - `/admin/pending_reviews` (the console's **Approvals** tab) and
`/admin/reports/pending` - carry `role` too, so a long shift waits for a decision under the
name of whoever it belongs to.

**Live Ops** is the other half of reading a shift: the ones that are still open. Each row
names the person, the site, the clock-in time and how long they have been on site, and the
role is on the row - `Administrator · 1000`, not `admin · 1000` - and on the phone cards,
which previously showed no role at all. The board's filter matches the role in both forms as
well. A board where an administrator's own shift reads like anybody else's is a board whose
reader has to recognise a name before they can tell who is on site, and the role is the one
fact that decides who reviews the hours afterwards.

## Data retention and erasure

This system holds five kinds of personal data, and all five used to be kept forever:
quick-link punch selfies (`quick_link_photos/`), face templates and reference selfies
(`local_references/`, `worker_photos/`), administrative audit rows, raw offline punches
with their coordinates, and the hours record itself. `backend/retention.py` is the one
place that decides how long each is kept, erases what is past its period, and writes a
compliance event saying exactly what it removed.

**The periods, and what `0` means.** Every period is in days and `0` means *keep
forever* - the same value in every knob, so nobody has to remember which one spells
"off" differently. The defaults are a month of punch selfies, a year of audit, and seven
days of biometric residue.

**One sweep does a bounded amount of work.** It holds SQLite's single write lock while it
runs, and the thing waiting on that lock is a worker clocking in - so
`RETENTION_MAX_ITEMS_PER_SWEEP` (default 20 000) bounds a run, and everything left over is
reported as `deferred` and picked up by the next one. A first run against a large backlog
therefore drains over a handful of sweeps instead of blocking the gate for a minute.
(This is the one setting where `0` means *no cap* rather than *keep forever*.)

**Deactivation is still immediate.** `POST /api/v1/admin/users/status` deletes a face
the moment the account is closed; the seven days is the net underneath that, not a grace
period for keeping it. What the sweeper hunts is the residue that delete cannot always
finish - a file held open by a backup, a template left by a crashed enrollment, a
`.unclaimed-*` file from the biometric-id migration, half a template in a `.tmp` name.
That is also what the daily *residue* check reports, and it counts from the
**filesystem**, not the database: a face whose row is gone is invisible to a query and
obvious to `os.listdir`, and that is exactly the case that matters.

**Erasing a face means erasing the file and the reference to it**, in that order -
overwrite the bytes, fsync, unlink, then clear the row. The reverse order has a window in
which nothing on any screen could explain whose face is on the disk. A wipe that fails is
reported, never swallowed, because that is the one fact an operator has to act on.

**`audit_log` is still append-only.** Retention is the single sanctioned exception, and
it takes the database's own guard off to do it: the trigger is read out of
`sqlite_master`, dropped, the rows deleted, and the trigger restored from that same text,
all inside one transaction. A crash leaves the guard in place and the rows undeleted.
Nothing reachable over HTTP can take that path.

**The hours record is never deleted.** `attendance_logs` is what pay is calculated from;
the sweep reports its size and age and deletes nothing. There is no setting to change
that, because "retention" that silently removes a timesheet is a wage dispute with a
timestamp.

```bash
cd backend
python -m retention --policy     # print the periods in force
python -m retention              # DRY RUN: what would be erased, deleting nothing
python -m retention --apply      # do it (and record it)
python -m retention --apply --vacuum   # + VACUUM, which rewrites the file so the deleted
                                       # rows are unreadable rather than merely unreferenced
```

The dry run **opens the database read-only**, so an accidental write raises instead of
quietly doing half a sweep - that is a structural guarantee, not a convention. The same
report is available to an administrator as `POST /api/v1/admin/retention/dry-run`, and
`GET /api/v1/admin/retention` shows the policy, whether the timer is running, the last
sweep, and what is still on disk past its window.

**What "wiped" honestly means.** `retention.wipe_file` overwrites the bytes in place,
fsyncs and unlinks - which is the most a portable application can do, and less than it
sounds. SSDs write to a new cell and retire the old one; copy-on-write filesystems and
snapshots keep the old blocks by design; and every backup taken while the row existed
still contains it, somewhere this process cannot reach. Deleted database rows are also
not erased bytes: SQLite frees the page and leaves its contents in the file until
`VACUUM` (which is why `--vacuum` exists and why every sweep reports the freelist size).
The only construction that makes erasure unconditional is **cryptographic deletion** -
encrypt each subject's data under a per-subject key and destroy the key - and this
codebase does not do that yet.

**If a period is being missed:** `GET /api/v1/readiness` has two advisory checks for it.
`retention_sweep` fails when the last recorded sweep is past 2x its interval (a policy
nobody is running is the finding, not the mitigation) or when a sweep reported failures;
`retention_residue` fails when files past their window are still on disk and names
them. The sweep's own record is the `retention_runs` table, which survives a restart -
a timer that is silently dead looks exactly like a timer that is working, right up until
somebody checks.

**Running it from cron instead.** Set `RETENTION_ENABLED=0` and call
`python -m retention --apply` from a timer, naming the job with `RETENTION_ACTOR` so the
audit event says who ran it. Set `RETENTION_DRY_RUN=1` to have the in-process sweeper
report without deleting while you evaluate it; that combination is visible in the
readiness check rather than implied.

## Metrics (Prometheus)

`GET /metrics` publishes the exposition format. It is **never anonymous**: the scrape job
sends `Authorization: Bearer $METRICS_TOKEN`, or an administrator's session token may be
used instead (there is no third way, and the token is compared in constant time).

```bash
# one scrape, by hand
curl -s -H "Authorization: Bearer $METRICS_TOKEN" https://host/metrics | head -40

# what is monitored, and in which mode this process is running
curl -s https://host/api/v1/readiness | python -m json.tool | grep -A 6 telemetry
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: attendance
    scrape_interval: 15s
    authorization:
      credentials_file: /etc/prometheus/attendance-metrics-token
    static_configs:
      - targets: ["attendance.internal:8000"]
```

Set `METRICS_TOKEN` to a random string (`python -m config` shows whether one is set, and
never the value). With no token configured the endpoint requires an admin JWT, which is
what makes it safe to leave on by default; `METRICS_ENABLED=0` answers 404 instead, and an
installation without `prometheus_client` answers 501 with the install command rather than
500 - the library is an optional extra, and so is the feature. The degraded mode is two-way
contractual: without the library every counter still accepts the application's increments
silently (write-path parity, pinned by
`test_metrics.py::test_every_helper_is_silent_when_the_library_is_missing`), and the stand-in
`Counter` can hand the label values of each increment to a receiver (`Counter.record`), which
is how the network-policy suite keeps asserting "refused, for this reason" on an interpreter
that has no `prometheus_client` at all. The exposition format itself is never faked:
`render()` returns empty bytes and `/metrics` answers 501, because a plausible-looking scrape
of nothing would be worse than an honest 501.

**Why hand-rolled rather than `prometheus-fastapi-instrumentator`.** Its middleware answers
the HTTP half of this question and nothing else: it cannot see that a punch spends half a
second in `DeepFace.represent`, that two concurrent inferences add no throughput, or that a
writer waited four seconds for SQLite's lock - which are the four numbers this deployment
actually gets paged about. Those counters are incremented where the event happens
(`face_engine`, `database`, the punch handler), on a private registry, with the middleware
in `telemetry.instrument_app` covering the HTTP layer in the same style. It also means the
application starts without the library, which a hard import would not.

| metric | type | labels | answers |
|---|---|---|---|
| `attendance_http_requests_total` | counter | route, method, status | traffic and errors, by route *template* |
| `attendance_http_request_seconds` | histogram | route, method | request latency including the punch |
| `attendance_http_in_progress` | gauge | - | concurrency right now (unlabelled: see below) |
| `attendance_face_model_seconds` | histogram | operation, model, detector | **inference cost** (`represent`, `detect`) |
| `attendance_face_cosine_seconds` | histogram | - | the 4096-float comparison, on its own |
| `attendance_face_match_score` | histogram | - | distance distribution, on a fixed grid bracketing both pipelines' decision lines |
| `attendance_face_model_failures_total` | counter | operation, error | the model raised, by exception type |
| `attendance_face_engine_job_seconds` | histogram | job, outcome | pooled job time |
| `attendance_face_engine_queue_wait_seconds` | histogram | job | time spent waiting for a worker |
| `attendance_face_engine_refusals_total` | counter | reason | `busy` (503) and `nested` |
| `attendance_face_engine_capacity` / `_queued` / `_in_flight` | gauge | - | the pool, read at scrape time |
| `attendance_verifications_total` | counter | outcome | approved, flagged_review, rejected, liveness_spoof, frame_refused |
| `attendance_punches_total` | counter | action, status | punches actually written |
| `attendance_active_sessions` | gauge | site_name | open shifts, read at scrape time |
| `attendance_sqlite_statements_total` | counter | operation | statements by verb, never by text |
| `attendance_sqlite_lock_wait_seconds` | histogram | operation | **time waiting for the write lock** |
| `attendance_sqlite_transaction_seconds` | histogram | mode | implicit (a punch) vs explicit (migrations, retention) |
| `attendance_sqlite_lock_errors_total` | counter | operation | "database is locked" after `busy_timeout` |
| `attendance_sqlite_connections_total` | counter | access | connections opened, read-only vs read-write |
| `attendance_build_info` | gauge | version, schema_version, liveness_mode, face_inference_concurrency | always 1: did the intended build land? |

Three deliberate choices worth knowing before writing a query against these:

* **Labels are bounded by construction.** A route label is the *template*
  (`/api/v1/q/{token}`), never the requested path, because a punch link carries its token in
  the path - labelling by path would mint a series per issued link and put a live credential
  in the label. A worker id, a site name from a request, a filesystem path and SQL statement
  text are all asserted absent by the suite.
* **`attendance_http_in_progress` is unlabelled.** The increment happens before the router
  has chosen a route, so a per-route label would have to be incremented under one label and
  decremented under another. It is a concurrency signal (the scrape itself counts as 1); the
  per-route breakdown arrives a moment later on the counter and histogram.
* **The write-lock wait is measured, not the query.** The Python SQLite API returns a cursor
  and produces rows on `fetch`, so timing `execute()` reports the query *plan*: a histogram
  built that way shows microseconds for a slow scan. What is measured is the thing that
  actually blocks a punch - acquiring the write lock - plus statement counts by verb.

### Alerting on inference spikes

```promql
# 1. Inference itself got slow (p95 over 5m, seconds per embedding).
#    Healthy on the deployment host is ~0.5s; 1.5s means the machine is oversubscribed,
#    TensorFlow is thrashing, or the model on disk changed.
histogram_quantile(0.95,
  sum by (le) (rate(attendance_face_model_seconds_bucket{operation="represent"}[5m]))
) > 1.5

# 2. Pupils are queueing: p90 wait for a worker is past a breath.
histogram_quantile(0.90,
  sum by (le) (rate(attendance_face_engine_queue_wait_seconds_bucket[5m]))
) > 2

# 3. The pool is refusing work - a worker is standing at a gate being told 503.
#    Any value above zero is a page, not a warning: it means somebody lost their tap.
sum(increase(attendance_face_engine_refusals_total{reason="busy"}[10m])) > 0

# 4. Throughput collapsed while punches keep arriving: the pool is saturated or wedged.
#    (inference per second vs punch requests per second)
sum(rate(attendance_face_model_seconds_count[5m]))
  / sum(rate(attendance_http_requests_total{route="/api/v1/attendance/verify"}[5m])) < 0.5

# 5. A punch is waiting on SQLite instead of on the model. p99 lock acquisition > 1s.
histogram_quantile(0.99,
  sum by (le) (rate(attendance_sqlite_lock_wait_seconds_bucket{operation="transaction"}[5m]))
) > 1

# 6. Contention that actually failed (after busy_timeout). A code bug is *not* counted here -
#    only "database is locked"/"busy" - so this alert means what it says.
sum(increase(attendance_sqlite_lock_errors_total[10m])) > 0

# 7. The review queue is filling up: scores drifting towards the refuse line (see "Where a
#    face match is decided" for the lines in force) means the camera fleet, the lighting or the
#    enrollment photos changed - not that workers changed.
sum(rate(attendance_verifications_total{outcome="flagged_review"}[15m]))
  / sum(rate(attendance_verifications_total[15m])) > 0.10

# 8. Presentation attacks. One is noise; a run of them is somebody trying.
sum(increase(attendance_verifications_total{outcome="liveness_spoof"}[15m])) > 5

# 9. The model is failing (out of memory, a corrupt weight file). The punch answers 500 and
#    is counted as frame_refused, so punches alone would not surface it.
sum(increase(attendance_face_model_failures_total[10m])) > 0

# 10. Server faults generally - the punch endpoint failing is 5xx.
sum(rate(attendance_http_requests_total{status=~"5.."}[5m])) > 0.05
```

**Multiple uvicorn workers.** The registry is per-process and `serve.py` runs one process,
so a scrape sees everything. A deployment that adds `--workers N` must set
`PROMETHEUS_MULTIPROC_DIR` (a directory all workers can write) and export the multiprocess
collector; without it a scrape lands on one worker and reports a fraction of the traffic -
worse than no metric, because an alert tuned to it fires late. `GET /api/v1/readiness`
says which mode this process is in.

## Accounts and access

The console's account screen is the **Credentials** tab: one row per account with its id,
role, contact details, whether a face template is enrolled, the password *state*, and how
many sessions a reset has invalidated.

It does **not** show passwords, because there are none to show: passwords are stored as
bcrypt hashes (`backend/security.py`), which are one-way. A column of "the password" would
mean keeping a readable copy of every worker's password in the database and in every
backup of it. What the tab shows instead is state - set / never set, and when it last
changed (read from the append-only `audit_log`, not duplicated into a `users` column that
could drift) - and it lets an admin set a new one. That password is either typed by the
admin or generated for them (16 characters, four classes, no `O`/`0` or `I`/`l`/`1` so it
survives being read out over a phone); the field opens on a generated one and *Generate
another* replaces it, so nobody has to invent a password they then have to read out. What
is in the field is what gets saved, and both paths are held to the server's policy. It is
shown once with a copy button at the moment it is saved, and forgotten afterwards.

Saving goes through `POST /api/v1/admin/users/edit_password`, which also bumps the
account's `token_version`: every existing session dies, including one already on a phone.
That is the point of a reset, and the panel says so before the button. A standard admin
cannot change an administrator's password - the server answers 403, and the button is
hidden for the same reason rather than shown and then refused.

### How the API decides who may do what

Authorization here is three layers, and the third one is checked at startup rather than
remembered.

**The credential is a signed session token, and the subject is always the token.** No
endpoint takes an identity from a request field: `/attendance/verify` still accepts the
`worker_id` the shipped client sends, and rejects it if it names anybody but the token's
owner; every self-scoped read (`/worker/me/*`) has no id in it to tamper with. That is
why the old "send `admin_id` and be believed" shape is gone - identity is not a form field.

**The role guard is a dependency, not a branch.** `security.require_role("admin")` is a
FastAPI dependency (`any_authenticated`, `admin_only`, `head_admin_only` are the pre-built
ones), and each carries an `_auth_marker` so the guard can be *inspected* rather than
trusted. The token's `token_version` is compared against the row on every request, so a
password reset or a deactivated account kills existing sessions on every ASGI worker at
once, not just the one that handled the reset.

**Object-level ownership is enforced in the query.** A worker asking for a colleague's
note gets the same 404 as a note that does not exist - "not yours" and "not there" must be
indistinguishable, or the endpoint becomes a way to enumerate which ids are real.
`ensure_self_or_role` does the same for a record reached by id.

The third layer is the part that used to be missing. `auth_enforced_on_admin_routes`
walks every path containing `/admin/` and fails the startup gate when one of them has no
role guard - and it said nothing at all about the other eighty-odd routes, which were
covered only by convention: they carried `Depends(any_authenticated)` because whoever
wrote them was careful. An endpoint added under `/api/v1/worker/` with no `Depends` at all
would have been readable by anybody who could reach the port, with the gate reporting a
clean bill of health.

`api_routes_authorised` (`backend/readiness.py`) closes that, and it is **fatal**: the
process refuses to serve rather than running a surface nobody verified. Every route must
be *accounted for* - carrying a `require_role` guard, or named in one of three explicit
lists with the reason written beside it:

| List | What it claims | Who is on it |
| --- | --- | --- |
| `PUBLIC_ROUTES` | answers without a session, on purpose | sign-in, the company branding the sign-in screen needs, the enrollment and quick-link token paths, readiness and status |
| `SELF_GATED_ROUTES` | refuses you itself, inside the handler | `/metrics` and `/api/v1/metrics` - it accepts a scrape token *or* an admin JWT, so it cannot carry a role guard |
| `PAGE_ROUTES` | serves an HTML file and no data of ours | `/`, `/enroll/{token}`, `/q/{token}` |

Entries are keyed by **method and path**, so adding a verb to a path that is public today
(a `DELETE /api/v1/branding`, say) does not inherit the exemption - it has to be declared
and explained in the diff. As shipped: 85 routes guarded by `require_role`, 12 declared
open with a reason, 3 pages.

The check also fails on the two ways a list like this rots. A **stale** exemption is one
for a route that no longer exists: it reads as "public on purpose" for a path nothing
serves, and quietly covers for the rename that is now unaccounted for. A **redundant**
exemption is one on a route that is already guarded, which does nothing but misinform the
next reader. And a traversal that enumerates *nothing* fails rather than reporting `0
guarded, 0 missing` - that exact vacuous pass is how the sibling check once certified a
surface it had never looked at.

`backend/tests/test_api_route_authorisation.py` holds all of it: that the gate passes on
the app as shipped, that it bites on each failure mode above, and that the exemption lists
are **honest** - a route declared public really does answer an anonymous caller, and one
declared self-gated really does refuse one.

That gate proves every route *has* an authorization decision. It cannot tell whether the
decision is the **right** one: a route guarded by `any_authenticated` satisfies it
perfectly, even when the route is an administrator's. `backend/tests/test_role_audience.py`
closes that by stating the audience **independently of the guards** and holding the guards to
it. `_intended_roles()` derives the audience from the shape of the API - a `/api/v1/admin/`
path belongs to the administrator roles, everything else to every signed-in role - with a
short list of documented exceptions (`/worker/stats/{worker_id}` and `/status/detail` are
administrators'; `/worker/me/enroll` is one administrator's own face). Independence is the
point: a table read back out of `_allowed_roles` would agree with itself however wrong the
guards were.

It fails in **both** directions, because both are defects. A route *broader* than its path
implies is the one that costs money - a payroll read any worker can call. A route *narrower*
than it implies refuses somebody it was built for, which is how a 403 nobody reads gets
shipped. Alongside that it presents a real session from every role outside an audience and
asserts a refusal, and one from a role inside it and asserts the opposite, so a guard that
denied all four roles cannot look like a pass. Those probes are sent with **no body** on
purpose: FastAPI resolves dependencies before it validates a request, so the refusal is
decided from the credential alone and the sweep cannot write anything.

The matrix answers *which roles may call a route*. It is blind to the layer underneath - a
route any worker may call that takes an **id**, where the id decides whose record is read
or written. "May I call this endpoint" and "may I have this record" are different
questions, and a role guard only answers the first. `backend/tests/test_ownership_matrix.py`
covers that layer, in both halves:

* **structural** - every route whose path carries somebody's record is accounted for. Three
  legitimate answers exist and each is declared in the file: the audience is administrators
  only, so no cross-worker case exists; the path is a *credential* rather than an id (an
  invite or quick-link token, hashed at rest, single-use, revocable); or it is listed with
  the one mechanism that decides the owner - `notes._fetch_note(..., worker_id=...)` and
  `offline_sync._device_lookup(conn, current.id, device_id)`. A new worker-reachable id route
  cannot ship unlisted.
* **live** - a real second session aiming at a real record owned by somebody else. The
  refusal is asserted *and the row is asserted unmoved*, because a 404 that still wrote is
  not a refusal; the owner's own access is asserted beside it, so an endpoint that refuses
everybody cannot pass.

The third form of the same mistake has no id in the path to find it by, so it is probed
directly: an id smuggled in through a **query string** or a **form field**. Every self-scoped
route (`/worker/me/*`) is asked twice - once with a colleague's `worker_id` and once without -
and the two answers must be identical, which is asserted as an equality between responses
rather than against a payload's shape so it cannot start passing because a field was added.
`POST /attendance/verify` gets a *complete* form with the wrong `worker_id`, because that
refusal lives in the handler (every role may punch, so it cannot carry a role guard) and a
bodiless request would only prove that a missing form is a 422.

Two of those live cases had no test anywhere before this file: a worker revoking a
**colleague's signing device** (the quiet one - it bumps their key epoch, so every punch their
phone has queued stops verifying and the worker finds out at the gate), and a colleague's
`worker_id` appended to a self-scoped route. The notes half is covered in depth by
`test_worker_notes.py`; this file holds the matrix-level probe so the list is complete in one
place.

### Where the server is allowed to send

All three layers above decide *who may call*. One endpoint also decides *where the server
may go*, and it is the only place in the application where a value a worker supplies becomes
a URL the server fetches: a push subscription endpoint. The browser hands over a URL, the
server POSTs a notification to it, and it does so again on every event that follows. That is
a server-side request forgery primitive with a retry, and the request leaves from inside the
network with no credential of the worker's attached.

**The control is an allowlist, and that choice is the design.** Refusing private ranges is
the obvious alternative and the wrong property: nothing about the attack needs an internal
address. `https://collector.attacker.test/hook` is a public host, reachable from anywhere,
and confirms to its owner that the server fetched it - and a denylist would accept it. The
hosts a real browser can produce are few and known, so "not internal" is replaced with "is a
push service" (`PUSH_ENDPOINT_HOSTS`). Two rules sit behind the list as a second lock, so a
hand-added entry cannot quietly reopen the hole: an **address** is never a push service
however public, and a **private suffix** is never one however it is spelled.

| Layer | Where | What it refuses |
| --- | --- | --- |
| the policy | `push.validate_endpoint` | not `https`, port not 443, credentials in the URL, whitespace or control characters, over-length, an IP literal, a single-label or private-suffix host, an unknown service |
| the door | `POST /worker/me/push/subscribe` | the same, at 422, **and the row is not stored** - a refusal that still wrote would leave the send path holding the URL |
| the send path | `push.deliver` | the same, re-applied per subscription, so a row written before the check existed, by an older build or by an operator's script, is never fetched |

The endpoint is parsed with `urlsplit`, not matched as a string, because the gap between
those two *is* the attack: `https://fcm.googleapis.com@evil.test/` has the host `evil.test`,
and any check that looked for an allowed name anywhere in the string accepts it. The host
comparison matches on the **dot boundary** - a plain `endswith("notify.windows.com")` accepts
`notify.windows.com.evil.test`, which an attacker registers for the price of a domain -
while still accepting the `<region>.notify.windows.com` subdomains that WNS really hands out.

A refusal is counted **apart from a failure** in the dispatch summary, and the inbox row's
`last_error` names it, so an operator is not sent to the push service's logs for a request
that never left the server. The allowlist itself is held to the same policy at startup
(`push_endpoint_allowlist`, advisory via `validate_host`), because a dead entry there is the
quietest failure in this area: the worker subscribes, the browser reports success, and no
notification ever arrives - indistinguishable from the vendor being down.

`backend/tests/test_push_endpoint_authorisation.py` covers all four, and the send-path half
is asserted against `harness.OUTBOUND`, which records every outbound HTTP attempt the
application makes and performs none of them: "the server did not send" is a claim about the
network, so it is checked at the network seam rather than inferred from a return value.

### Starting an account

The tab has two buttons, because there are two ways an account begins.

**New account** creates it here: id, name, role, a password (generated, or typed by the
admin) and - optionally - a photo. With a photo, `POST /api/v1/admin/users/create` embeds the face, writes the
template and sets `enrolled_at`, so the person can clock in immediately; the password is
shown once to be handed over. Without one, the account exists and cannot clock in until a
face is registered, which the panel says in those words rather than letting a worker
discover it at the gate. The password policy is the same one a registration link is held
to (`MIN_PASSWORD_LENGTH`, plus the common-password list), and an id outside its role's
block (`worker` 1-499, `moallem` 500-749, off-office 750-999, `admin` 1000-4999, `head_admin` 5000+) is
refused in the browser and again on the server.

**Registration link** does not create anything: it issues the `register` invite described
under *Worker enrollment*, for the case where the person is not in front of you. The id
and the role are still chosen here; only the password and the face are theirs.

```
POST /api/v1/admin/users/create                 id, name, role, password[, photo]  (multipart)
POST /api/v1/admin/enrollment/invites           {"kind":"register", ...} -> url + token + QR
POST /api/v1/enroll/<token>/register            password + photo -> the account
```

The console's three old account tabs are gone: **Users** (the enroll dashboard, whose only
action was capturing an enrollment photo) and the never-wired **Pass** and **Enroll** tabs,
which only ever rendered "module coming soon". `/api/v1/admin/enroll` itself is untouched
and still documented above.

### Your own face

An administrator who works a site as well as running it needs a face reference like anybody
else, and every path that produced one put somebody else in the middle: `/admin/enroll`
takes a `worker_id` this account should not have to be given, an enrollment link has to be
opened on a phone, and the create form above only runs while the account is being made. So
the **Credentials** tab offers **Enroll my face** - or **Replace my photo**, when a template
is already on file - on the reader's own row, which is the one row the tab otherwise refuses
to manage, because a standard administrator may not touch an administrator's account. The
control is drawn from the same list the console uses for who may step onto the clock
(`UI.handsetRoles`), and the endpoint checks that list, so a role that cannot punch is never
offered it and a role that is offered it never meets a 403.

```
POST /api/v1/worker/me/enroll                   photo (multipart) -> your own template
```

**The subject is the token.** There is no `worker_id` on the wire to point at somebody else,
and a field that tries is ignored - which is the difference between this and
`/admin/enroll`, where the id is a parameter an administrator is trusted to choose. This one
can only ever write the caller's own face, which is why it is safe to hand to a role that is
not a head administrator.

**The photo is a live capture** from the console's camera, so the enrollment liveness policy
runs on it (`ENROLLMENT_LIVENESS_MODE`, `inherit` by default): a frame off a camera is exactly
what passive anti-spoofing is built to judge, and a printed photo must not become a permanent
template - unlike the create form above, which takes a file from disk and cannot pretend to
prove it live. A capture the policy refuses answers 422, writes nothing, and keeps the camera
open so the reader can simply try again.

**A replacement is recorded as one.** The response says `template_replaced`, and the audit
event (`biometric_self_enroll`) carries a before-image, because "a face was enrolled" and
"the face this account was using was replaced" are different events to whoever reads the log
a year later - and a credential minted without anybody else in the loop is worth an
`enrollment_completed` notification on the same board as everything else.

Finally, the refusal at the gate says where *that* reader's fix is. A punch with no template
has always answered `404 "Facial reference not registered. Please contact your administrator
to enroll."`, which is right for a worker and a dead end for an administrator, who is one:
they now read "Register your own photo from the console, then clock in again."

## Notes (what a worker needs to ask for)

The **Notes** tab is the written channel between a worker and the administrator.
`password_reset`, `missing_item`, `shift_hours`, `enrollment`, `working_conditions` and
`other` are the six types, and each is a preset the worker taps rather than a free-text
box to classify on their own.

A note is a **thread**, not a form. The administrator answers, or asks a question, and
the worker answers back - which is the difference between a request that gets handled
and a form somebody submits into silence.

```
POST /api/v1/worker/notes                    open one (any signed-in user, incl. a moallem or off-office worker)
GET  /api/v1/worker/notes                    your own, plus the open/unread counts
GET  /api/v1/worker/notes/{id}               one thread (reading it clears "new reply")
POST /api/v1/worker/notes/{id}/replies       answer it (reopens a finished note)
POST /api/v1/worker/notes/{id}/close         the worker says it is done
GET  /api/v1/admin/notes?status=&category=&worker_id=
GET  /api/v1/admin/notes/{id}                the whole thread, internal notes included
POST /api/v1/admin/notes/{id}/replies        answer, optionally internal, optionally moving the status
POST /api/v1/admin/notes/{id}/status         open | in_progress | resolved | closed
```

The rules that are not obvious from the endpoints:

- **The author is the token.** `worker_id` in the body is ignored, and asking for a
  colleague's note answers `404`, the same as a note that does not exist - otherwise the
  endpoint would be a way to enumerate what everybody is complaining about.
- **`resolved` is the admin's claim, not the truth.** A worker reply on a resolved or
  closed note puts it back to `open`, clears `resolved_by`, and raises a
  ``worker_note_reopened`` notification, because an admin who believed it was finished
  will not reopen the inbox on their own.
- **Internal replies are filtered server-side**, not hidden by the UI. The worker's
  endpoint never carries one, so "the worker does not see it" does not depend on the
  console remembering to hide it.
- **Opening a note is what clears the badge** (`GET` records the read). The alternative is
  a second endpoint a client can forget to call, and a badge that never goes away.
- **A new note raises an `admin_notifications` row** (`worker_note`), so it lands in the
  same alert queue as an overtime crossing or a spoofed frame. Notes have no `dedupe_key`:
  two notes from the same worker are two obligations.
- **`NOTES_MAX_OPEN_PER_WORKER` (default 20) caps open notes per worker**, not per IP:
  a whole site behind one tunnel shares an address, and one worker's backlog must not
  silence their colleagues. Resolving one makes room again.

**Password help is answered, not stored.** For a note whose author's password may be
reset, the dashboard offers a password box beside *Set a new password*: type one, or leave
it empty and the button generates one. Either way it calls the same
`/admin/users/edit_password` the **Credentials** tab calls - so the same
`token_version` bump signs the account out everywhere - reveals the password
once, and prefills a reply. The password is deliberately **not** written into the note:
`notes.body` and every reply are stored in plaintext, and this database keeps passwords
as bcrypt hashes only. Anything else would put a readable password in exactly the file
and every backup of it that the hash-only rule exists to keep clean. An administrator's
password is still a head-admin-only action, and the offer is hidden where the server
would answer 403.

## Tests

`backend/tests/test_verify_live_script.py` covers `deploy/cloudflare/verify_live.py` without any
network: whose answer a response is (the API's JSON refusal, the Worker's `error_code`, or an
asset host's HTML), that Cloudflare's own 403 `error code: 1010` is never reported as a broken
deployment, that the host's `x-railway-fallback` 502 is named as the host's problem, and that the
hand-built multipart punch body is a form the API can parse.

`backend/tests/test_api_route_authorisation.py` covers the startup gate described under
*Accounts and access*: that every route on the shipped application is either guarded by a
role or declared open with a reason, that the gate fails on an unguarded route, a stale
exemption, a redundant exemption, a missing reason and a traversal that enumerated nothing,
and that the public and self-gated lists mean what they say when probed with no credential.

`backend/tests/test_ownership_matrix.py` goes one layer below the audience question: every
route whose path takes a record id must be accounted for (administrator-only, a credential
rather than an id, or declared with the ownership check that guards it), and a real second
session is aimed at a real record owned by somebody else at every worker-reachable id route -
asserting both the refusal and that the row did not move. It also proves an id smuggled in
through a query string or a form field changes nothing.

`backend/tests/test_role_audience.py` covers the other half of that question - not whether a
route is guarded, but whether it is guarded **correctly**. It states each route's intended
audience independently of its guard and fails when a route reaches too far or too little,
then re-proves it live by presenting a session from each role outside the audience and
expecting a refusal. Widening `/worker/stats/{worker_id}` to `any_authenticated` fails the
live half without the startup gate noticing at all, which is exactly the gap it fills.

`backend/tests/test_push_endpoint_authorisation.py` covers the one place a worker-supplied
value becomes a URL the server fetches. It holds the policy (private and internal addresses,
metadata services, unknown push services, and the two ways an allowlist is defeated - a
longer name that ends with an allowed one, and a URL whose userinfo hides the real host), the
door (the subscribe endpoint refuses at 422 *and stores nothing*), and the send path (a
planted subscription row is refused, with `harness.OUTBOUND` proving no request was made).
It also holds the allowlist itself to the same policy at startup, since a dead entry there is
silent.

`backend/tests/test_deployment_manifest.py` keeps the deployment files honest: runtime imports
stay pinned in `requirements.txt` (and test-only packages stay out of it), the healthcheck path is
a real route that answers 200 without a session, the start command in `railway.json` still parses
and still means plain HTTP behind the host's TLS, and a `PORT` that is not a port number falls
back to 8000 instead of failing the start.

```bash
cd backend
./venv/Scripts/python.exe -m pytest tests -q            # Windows (Git Bash)
./venv/Scripts/python.exe -m pytest tests/test_site_shift_windows.py -q   # one suite
./venv/Scripts/python.exe -m pytest tests -n auto -q    # every core (the whole file: ~5 min)
```

The parallel run is the one to use, and it is worth knowing how the suite is arranged to make it
safe: each worker is a separate process, so a test's environment is its own, and each worker is
handed its tests in collection order - which is what keeps a module's tests together in one worker
(a module-scoped fixture is therefore never torn down while another module's test is running).

The suite **never touches your data.** At import it copies `times.db` into a temp directory,
points the application at the copy, and refuses to run if the app resolves anywhere else
(`harness.assert_database_isolation`). Before every test it restores that copy byte-for-byte,
re-runs the migrations, and **empties every table that records what people did** - punches,
sessions, notifications, the audit log, quick links, enrolled phones, notes, the offline queue.
A shift somebody worked this morning, or a quick link the console issued ten minutes ago,
cannot change a test's answer.

The clone supplies the **schema and the configuration**; the tests supply their own traffic: a
roster of four accounts, two sites, two completed shifts for the report suite, and the company
shift rules at their shipped values. Two consequences worth knowing:

* the live `times.db` has to *exist* - it is the only source of the schema (an empty file is
  fine, a missing one is not);
* a test that needs history seeds it. `tests/test_fixture_state.py` fails if the schema grows a
table that is not classified as activity, shipped configuration or seeded, so this cannot rot
silently - and it plants rows like real usage and proves a reset clears them.

`audit_log` is append-only in the database itself, so the clear takes the trigger off, deletes
and puts the same text back inside one transaction - the same rule the retention engine follows.

## Front-end layout modes

The app ships two dedicated layouts and switches automatically:

- **Mobile (< 768px, or a touch device under 1024px)** — app shell with sticky
  header, a fixed bottom tab bar (Clock / History / Profile), one big thumb-sized
  clock button and a full-screen camera capture overlay.
- **Laptop/desktop** — two-column worker dashboard and an admin console with a
  sidebar.

Force a mode for testing with `localStorage.layoutOverride = 'mobile' | 'desktop'`,
and point the front-end at another backend with `localStorage.apiBaseURL`.

### The phone console's sticky band

On a phone the console's only pinned element is the **tab strip**, and nothing else. It
used to be a 149px band carrying the tab's title, the brand mark and the four action
buttons as well - a sixth of an iPhone viewport, held there permanently, on a screen whose
whole job is showing an operator a table. It was spent on things that do not need to survive
scrolling: the title answers "what am I looking at" once, when the tab is tapped, and "which
admin am I" is asked before a destructive button, not while scrolling payroll. The title,
its hint and the actions now scroll away with the pane (`.admin-mobile-bar`, the first thing
in the frame); the strip stays, because it is the one control that has to be reachable from
anywhere in a long table. The band is strip-sized - about 57px plus the inset.

Both ends of the screen are handled in the stylesheet, because `viewport-fit=cover` puts the
status bar and the home indicator on the page rather than around it:

* the top inset is **padding on the sticky element**, so the band's background fills the
  notch while its content sits below it. As a margin it would scroll the strip up under the
  status bar and paint the tabs across the clock;
* the bottom inset is added to the frame's bottom padding, so the last row of a table - or
  the credit under it - cannot be painted over by the gesture bar. Note the phone's own
  `@media (max-width: 1023px)` override of `.admin-frame`: an inset set only on the base
  rule is an inset that never applies where it was needed, which is exactly how it was
  written the first time, and the browser test caught it;
* every inset is `env(safe-area-inset-*, 0px)` through a `--safe-*` custom property, with a
  `0px` fallback so a device without a notch is unaffected.

`backend/tests/test_signed_in_sessions_in_a_browser.py` measures this in a real browser. It
cannot emulate a notch - Playwright's device profiles report no insets - so instead it *puts*
one there by setting the custom property and asserts the geometry moves by exactly that
much, which is the only part the CSS is responsible for; `viewport-fit=cover` in
`index.html` is asserted separately as what makes a real inset non-zero on a device.
