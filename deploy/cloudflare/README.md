# Serving the app from Cloudflare

Two pieces, and the difference between them is what makes the app work:

| Piece | What it is |
| --- | --- |
| **The Worker** (`worker.mjs`) | The public frontend. It serves the shell from its asset bundle and proxies the paths the backend owns to the backend. |
| **The backend** (Railway) | Where FastAPI runs, at an address of its own that does not change. `railway.json`, `requirements.txt` and `.python-version` at the repository root are its configuration; the root README has the variables to set. |

The app is **single-origin by construction**: `API.resolveBaseURL` answers `page origin +
/api/v1`, which is what keeps GPS, the camera and the session working wherever the page is
served from. Host the frontend on a host of its own and every call — login, the worker's own
stats, the punch, the clock-in window on the punch card — goes to that host's `/api/v1` and
404s. It fails quietly: the app opens, and then the login screen says the server is
unreachable while the server is up. (That is what `al-jehad1.abdallahtamet281.workers.dev`
did before this Worker existed: shell `200`, `/api/v1/status` `404`.)

So the Worker serves the frontend *and* forwards `/api`, `/static`, `/enroll` and `/q` to the
backend. The browser only ever sees the Worker, so there is no CORS to configure and the
document CSP's `connect-src 'self'` keeps holding.

## First deploy, in order

Backend first, then the Worker, and only then anything that depends on them. The order is not
ceremony: the Worker's `API_ORIGIN` names the backend, so deploying the Worker first replaces a
shell that at least loads with an app whose every call answers `api_unreachable`.

1. **Commit and push the code the deployments build from** - `requirements.txt`, `railway.json`
   and `.python-version` at the root, `deploy/cloudflare/` for the Worker. A push does not deploy
   the Worker by itself: either `npx wrangler deploy` by hand (step 4) or Workers Builds wired to
   the repo (bottom of this section).
2. **Railway (browser login)** - connect the repository to the service (Source → connect,
   Root Directory `/`), add a **Volume mounted at `/data`**, set `SECRET_KEY` and
   `DATABASE_PATH=/data/times.db` (plus `BACKUP_DIR=/data/backups`), keep **one replica**, then
   deploy and read **Deployments → View Logs** if it fails. Check
   `https://<service>/api/v1/status` before going on.
3. **`TRUSTED_PROXIES`, once you have a healthy deploy** - the edge is not loopback, so
   `GET /api/v1/admin/readiness` reports `network_policy` with the peer address the app actually
   sees (`forward_misuse.last_peer`); set `TRUSTED_PROXIES` to it (Railway's internal hop is in
   `100.64.0.0/10`). Without it every worker shares one `15/minute` bucket and, with
   `ADMIN_IP_ALLOWLIST` set, the admin gate refuses everybody with `proxy_not_trusted`.
4. **Cloudflare (browser login)** - `npx --yes wrangler login`, then
   `cd deploy/cloudflare && npx --yes wrangler deploy`.
5. **Verify** - `python deploy/cloudflare/verify_live.py --worker <worker-url> --origin <backend-url>`
   (four passes), then again with a worker's credentials, `--location`, `--selfie` and `--punch`
   (seven, with the punch refused by the API's own checks, which is still a pass).

**If you would rather a push did the whole thing:** Workers Builds is the switch - Workers &
Pages → the Worker → Settings → Builds → connect the repository, root directory
`deploy/cloudflare`, deploy command `npx wrangler deploy`. Railway's equivalent is connecting the
repository under Settings → Source. With both connected, `git push` rebuilds both; without them,
a push rebuilds nothing.

## 1. Get the backend answering

Deploy it (root README, *Running the backend on Railway*), then check the address the Worker
will be pointed at — this is the step to do **before** touching the Worker, because a Worker
pointed at an address that 502s turns a working shell into an app that cannot sign in:

```bash
curl -s https://al-jehad-production.up.railway.app/api/v1/status   # {"status": "..."}
```

Three variables have to exist on the service, and one of them is new when the backend leaves
your machine:

- **`SECRET_KEY`** — 32+ random characters. Without it `build_settings()` raises at import, the
  container exits, and Railway's edge answers `502 Application failed to respond`; the reason is
  in the *deploy* log, not the app's.
- **`DATABASE_PATH=/data/times.db`** with a **volume mounted at `/data`**. SQLite is a file, and
  without a volume every deploy replaces the filesystem it lived on. Run **one replica**: two
  instances on one file corrupt it.
- **`TRUSTED_PROXIES`** — set this to the addresses Railway's edge connects from. On a laptop the
  proxy was `127.0.0.1`, which is trusted by default; Railway's edge is not loopback, so leaving
  this unset is silent and expensive: uvicorn will not rewrite the client address, every worker
  shares one `15/minute` bucket and they hit `429`s together, and the audit log records the edge
  as the actor of every punch. `serve.py` prints the list it is using at startup, and
  `GET /api/v1/readiness` reports `proxy_not_trusted` when it is wrong.

`--tunnel` in the start command means "whatever is in front of me has already done TLS" — the
same flag a tunnel uses, and the reason the app does not serve its self-signed certificate
behind Railway's edge (with one, GPS and the camera fail on a phone while the laptop looks fine).

## 2. Point the Worker at it

`API_ORIGIN` in `wrangler.toml` is the backend's address, with no trailing slash:

```bash
# one browser approval, once per machine
npx --yes wrangler login

cd deploy/cloudflare
npx --yes wrangler deploy --dry-run    # validates the config, no account needed
npx --yes wrangler deploy
```

Deploying replaces whatever the Worker currently serves — code and assets both. The frontend
bundle in `../../frontend` is uploaded as part of it, so a frontend fix goes out with the
Worker rather than separately.

Deploying from the dashboard instead of wrangler works too, but the asset directory and the
`run_worker_first` list are part of the deploy configuration rather than the code: they live in
the Worker's **Settings → Static assets** panel (`frontend/` as the directory, `ASSETS` as the
binding, and the four path prefixes from `wrangler.toml`). `backend/tests/test_cloudflare_worker_proxy.py`
fails if the code's list and `wrangler.toml`'s list stop agreeing, so change both together.

## 3. Check it

```bash
# the whole path in one run: backend, the Worker, the shell, and the punch screen's own calls
python deploy/cloudflare/verify_live.py \
    --worker https://al-jehad1.abdallahtamet281.workers.dev \
    --origin https://al-jehad-production.up.railway.app

# ... and with a worker's credentials, the two calls the punch screen makes, through the Worker
python deploy/cloudflare/verify_live.py --worker <worker-url> --location 30.05,31.23 \
    --user-id <id> --email <worker> --password <pw> --selfie <a.jpg> --punch
```

`verify_live.py` checks each piece in the order that makes the first failure the one worth
reading, and names the answer instead of leaving a status code to interpret: a 404 from the
asset layer (no proxy), `worker_misconfigured`, `api_unreachable`, a JSON 502 carrying
`x-railway-fallback` (the host has no healthy service), or Cloudflare's own `error code: 1010`
(your *client* was banned by its bot rules - the deployment may be fine). The punch is expected
to be *refused* most of the time - a geofence, an unenrolled account, a punch outside the
site's window - and a refusal is a pass: what it proves is that the API judged the request,
rather than an asset host answering it. `--password` can come from `$PUNCH_PASSWORD` instead.

The same path by hand, if you want one curl:

```bash
curl -s https://al-jehad1.abdallahtamet281.workers.dev/api/v1/status   # JSON, not an HTML 404
```

| What you get | What it means |
| --- | --- |
| JSON | The whole path works. Open the Worker address on a phone and sign in. |
| `{"error_code":"worker_misconfigured", ...}` | The message names the variable to set (`API_ORIGIN` empty, not a URL, or no `https:`). |
| `{"error_code":"api_unreachable", ...}` | The Worker reached Cloudflare and could not reach the backend: the Railway service is down, or `API_ORIGIN` names an old address. |
| `502` from Cloudflare's edge (no JSON) | The Worker itself is not healthy. |
| A JSON `502` from *Railway*, with `x-railway-fallback: true` | Railway's edge is up and nothing healthy is behind the service — a build, start or crash problem, to be read in the deploy log. |

## Fallback: the backend on this machine, behind a tunnel

Only for while the host is down and the app is needed now. It is the same Worker, pointed at a
tunnel instead of the host:

```bash
python backend/serve.py --tunnel                      # terminal 1
cloudflared tunnel --url http://localhost:8000        # terminal 2 (scoop install cloudflared)
```

Put the printed `https://<words>.trycloudflare.com` address in `API_ORIGIN` and redeploy the
Worker. Two things that make this a fallback rather than the design: that address **changes on
every cloudflared restart** (so the Worker's variable has to change with it, and the app reports
`api_unreachable` until it does), and the machine has to stay awake. With `cloudflared` on the
same host as the backend, `TRUSTED_PROXIES` can stay at its default — `127.0.0.1` is where
cloudflared connects from — which is exactly the setting the host requires you to set by hand.

## What the proxy deliberately does and does not do

- **It is an allow-list.** `/api`, `/static`, `/enroll`, `/q` are forwarded; anything else is
  served from the asset bundle, so a path nobody listed is a 404 rather than a public alias for
  whatever the backend serves there (`/metrics`, a future admin route, an endpoint that only
  checks its token). `/apiary` is an asset name, not an API call.
- **Requests and answers pass through untouched.** Method, path, query, body, `Authorization`,
  status, headers — including a 403, which must reach the worker's screen as a refusal and not
  be tidied into a 200 by a second implementation of the API's contract.
- **It does not authenticate anything.** The API's token checks, the geofence and the face
  match are unchanged; this moves the public *address*, it does not add a wall. The backend's
  address stays as private as it was: a Worker hostname is easy to guess from the frontend, and
  the API behind it is the thing worth protecting.
- **No CORS, no cookie games.** One origin, so there is no preflight and no
  `Access-Control-Allow-Origin` to get wrong.
- **An interstitial header only for a host that shows one.** Railway and Cloudflare Tunnel do
  not, so nothing is sent for them. The Worker still sets `ngrok-skip-browser-warning` for an
  ngrok origin, because an HTML warning page parsed as JSON is a confusing way to find out.

## Two things to watch after the first deploy

1. **Rate limits and logged addresses.** The Worker forwards the client's address as
   `X-Forwarded-For` (from `CF-Connecting-IP`), and then Railway's edge forwards its own view on
   top. The backend only *believes* that header when it arrives from a trusted hop, so the case
   worth checking is every worker sharing one `15/minute` bucket and seeing `429`s together — or
   readiness reporting `proxy_not_trusted`. Set `TRUSTED_PROXIES` to the hops in front of the
   backend and check `GET /api/v1/readiness`.
2. **The shell's caching.** The backend serves the frontend with `Cache-Control: no-cache,
   must-revalidate` on purpose — a months-old `frontendjavascript.js` on a phone is how a fixed
   bug keeps reproducing. Cloudflare's asset serving has its own caching rules for files that
   are not content-hashed, and they are not this repo's to set: if a device keeps running an
   old shell after a deploy, check `Cache-Control` on the response and, if needed, add a
   `_headers` file to `frontend/`.
