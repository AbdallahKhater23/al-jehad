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

## Going live over a tunnel (Cloudflare Tunnel / ngrok)

A tunnel is the best option for real workers: it gives a **proper trusted HTTPS
certificate**, so there is no "not private" warning, and GPS + camera work on every
device with no per-device setup.

```bash
python backend/serve.py --tunnel          # terminal 1: plain HTTP on :8000 for the tunnel
cloudflared tunnel --url http://localhost:8000   # terminal 2: the public HTTPS address
```

`--tunnel` means "the tunnel provides the HTTPS, so do not make a self-signed
certificate". Then open the printed `https://<words>.trycloudflare.com` address on the
laptop and the phone. `ngrok http 8000` works the same way if that is what you have.

- **No front-end configuration needed.** The page and the API share one origin, so
  the app calls `https://<your-tunnel>/api/v1/...` automatically. (That also means
  no mixed-content blocking: the old hardcoded `http://<host>:8000` API URL would
  have been refused on an HTTPS page.)
- **First visit per browser, on ngrok only:** ngrok's free tier shows a "You are
  about to visit …" warning page. Click **Visit Site** once; after that the app
  loads normally. Cloudflare Tunnel shows no such page.
- **Rate limiting stays per worker.** `serve.py` enables proxy headers, so the
  `15/minute` limit on clock-in applies to each worker's real IP. Without that,
  every worker behind the tunnel would share one bucket and hit 429s together.
- Keep the tunnel URL private. It has no authentication in front of it.
- **The tunnel address is not a frontend.** Anybody opening it gets the app, which
  is fine, but a quick tunnel (`trycloudflare.com`) gets a *new* address every time
  it restarts - so it is not a link to give workers. For that, put the shell on
  Cloudflare and the tunnel behind the API.

### Serving the frontend from Cloudflare Workers

`deploy/cloudflare/` is a Worker that serves the frontend as static assets and proxies
`/api`, `/static`, `/enroll` and `/q` to the tunnel, so the one address workers get is stable
while the backend stays behind a tunnel on this machine. The proxy is not optional: the app is
single-origin by construction, so a shell hosted on Cloudflare with no proxy calls
`<your-worker>/api/v1/...` and gets a 404 from the asset host (which is exactly what
`al-jehad1.abdallahtamet281.workers.dev` did before the Worker existed). That folder holds the
Worker, the `wrangler.toml`, and the two things to watch after the first deploy.

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
| `OVERTIME_WATCHER_ENABLED` | `1` | background timer that alerts the moment a shift passes 8.1 h |
| `OFFLINE_PUNCH_MAX_AGE_HOURS` | `72` | how far back an offline punch may be anchored |
| `NOTES_MAX_OPEN_PER_WORKER` | `20` | how many notes by one worker may be waiting for an answer at once |
| `UPLOAD_MAX_PHOTO_BYTES` | `5242880` (5 MB) | ceiling for **every** photo upload in the app |
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

Every punch and every enrollment is a VGG-Face embedding plus an MTCNN detection, and
those are the most expensive things this application does. They all run in one place,
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

**What this does *not* isolate.** The models still run inside the API process: a crash
inside native TensorFlow (an out-of-memory kill, a corrupt image reaching a half-loaded
model) takes the API process with it, and a saturated pool still competes for the same
CPU. Real isolation means a separate model server - Triton, TorchServe - behind a network
call. `FaceEngine` is the seam for that, so it becomes a second implementation rather
than a rewrite; until then this bounds the damage. `GET /api/v1/readiness` reports the
load (capacity, queued, in-flight, refusals, slowest) as an advisory check, so an operator
can see a saturated engine without it failing the startup gate.

## Per-site shifts (including overnight)

Each site can run its own clock-in window. A site with no window configured uses the global
rule from Admin → Shift rules, and the fallback is **per field** - so a night site can set
only its hours and keep the company timezone:

```bash
curl -X POST localhost:8000/api/v1/admin/sites/edit \
  -H "Content-Type: application/json" \
  -d '{"site_name":"New Capital Zone B","location_input":"29.98,31.75","radius":100,
       "clock_in_window_start":"21:30","clock_in_window_end":"05:30",
       "site_timezone":"Africa/Cairo"}'
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
* `site_timezone` must be an IANA name (`Africa/Cairo`, `Asia/Riyadh`); a typo is refused
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
`worker` or a `moallem` (never an administrator), it is single-use whatever `max_uses`
says, and it refuses an id that is already taken. The Credentials tab has this behind one
button, with the URL, a copy button, a WhatsApp message and the QR code.

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
GET  /api/v1/admin/punch_queue             -> triage, including refused punches
```

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
open. It carries no `hourly_rate` and no `gross_estimate` - an app that pays nobody should
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
approval, unpaid break, shifts, workers) and then the shifts themselves.

The table's columns are the administrator's to arrange. They arrive in this order -
**Date, Employee, User ID, Site, Hours, Awaiting approval, Open notes** - and the *Columns*
panel moves any of them earlier or later, remembering the choice in `localStorage` so it
survives a reload without changing what any other admin sees. A stored order that is stale
or junk is repaired rather than obeyed (unknown columns dropped, new ones appended).

The **Download CSV** button writes a file from the rows shown, so a search downloads
exactly what the table shows (the API export has no free-text filter and always covers the
whole period, which is why the button does not use it). The file holds **`Employee, id,
site, hours`** - the same four columns, in the same order, as `kind=shifts` on the API
export, and the same figure the screen shows for each row to the precision the screen
shows it (the API export writes that figure unrounded, so a script gets the exact number
and a reader gets the same sheet). The screen's column order deliberately does not travel
into the file: a sheet whose columns move from day to day cannot be compared with last
month's.

The period is kept in the URL fragment (`#shifts=2026-08-01..2026-08-31`), so copying the
address bar - or the **Copy link** button - hands a colleague the same figures. Opening
such a link lands on the Shifts tab with that period already loaded; an unrecognisable
fragment is ignored rather than shown as an error, and a fragment still using the old
`#payroll=` is honoured. A search travels in the same fragment (`&q=tower`).

Each row names the **site** that shift was worked at, and the **Notes** column carries the
worker's open notes (open or in progress) - four shifts by one person with a request in is
one outstanding note, not four.

The **Search** box narrows the rows without touching the period, matching on worker name,
worker id, site, day or approval state - every term has to match, so `harbour khan` is the
one shift that is both. The totals above the list then cover only the shifts shown, and the
card block is replaced by "no shift matches" rather than a grid of zeros. A day typed into
the box filters the rows like any other term (a timesheet row *has* a date) and still
offers a one-tap switch that moves the whole period onto it. Searching repaints from the
rows already in hand, so it costs no request - only a new period does.
Hours still awaiting approval are shown beside the approved ones and never added to them.

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
500 - the library is an optional extra, and so is the feature.

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
| `attendance_face_match_score` | histogram | - | distance distribution, bucketed at 0.40/0.60 |
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

# 7. The review queue is filling up: scores drifting towards the 0.60 threshold means the
#    camera fleet, the lighting or the enrollment photos changed - not that workers changed.
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

### Starting an account

The tab has two buttons, because there are two ways an account begins.

**New account** creates it here: id, name, role, a password (generated, or typed by the
admin) and - optionally - a photo. With a photo, `POST /api/v1/admin/users/create` embeds the face, writes the
template and sets `enrolled_at`, so the person can clock in immediately; the password is
shown once to be handed over. Without one, the account exists and cannot clock in until a
face is registered, which the panel says in those words rather than letting a worker
discover it at the gate. The password policy is the same one a registration link is held
to (`MIN_PASSWORD_LENGTH`, plus the common-password list), and an id outside its role's
block (`worker` 1-499, `moallem` 500-999, `admin` 1000-4999, `head_admin` 5000+) is
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

## Notes (what a worker needs to ask for)

The **Notes** tab is the written channel between a worker and the administrator.
`password_reset`, `missing_item`, `shift_hours`, `enrollment`, `working_conditions` and
`other` are the six types, and each is a preset the worker taps rather than a free-text
box to classify on their own.

A note is a **thread**, not a form. The administrator answers, or asks a question, and
the worker answers back - which is the difference between a request that gets handled
and a form somebody submits into silence.

```
POST /api/v1/worker/notes                    open one (any signed-in user, incl. a moallem)
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

```bash
cd backend
./venv/Scripts/python.exe -m pytest tests -q            # Windows (Git Bash)
./venv/Scripts/python.exe -m pytest tests/test_site_shift_windows.py -q   # one suite
```

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
