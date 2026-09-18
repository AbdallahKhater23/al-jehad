# Serving the app from Cloudflare

Two pieces, and the difference between them is what makes the app work:

| Piece | What it is |
| --- | --- |
| **The Worker** (`worker.mjs`) | The public frontend. It serves the shell from its asset bundle and proxies the paths the backend owns to the tunnel. |
| **The tunnel** (`cloudflared`) | The backend's door. It publishes the FastAPI app running on this machine, over HTTPS, with no port forwarding and no certificate to install. |

The app is **single-origin by construction**: `API.resolveBaseURL` answers `page origin +
/api/v1`, which is what keeps GPS, the camera and the session working wherever the page is
served from. Host the frontend on a host of its own and every call — login, the worker's own
stats, the punch, the clock-in window on the punch card — goes to that host's `/api/v1` and
404s. It fails quietly: the app opens, and then the login screen says the server is
unreachable while the server is up. (That is what `al-jehad1.abdallahtamet281.workers.dev`
did before this Worker existed: shell `200`, `/api/v1/status` `404`.)

So the Worker serves the frontend *and* forwards `/api`, `/static`, `/enroll` and `/q` to the
tunnel. The browser only ever sees the Worker, so there is no CORS to configure and the
document CSP's `connect-src 'self'` keeps holding.

## 1. Start the backend and its tunnel

```bash
# terminal 1 - the backend on :8000, plain HTTP for the tunnel to wrap
python backend/serve.py --tunnel

# terminal 2 - the tunnel (install once; scoop needs no administrator)
scoop install cloudflared
cloudflared tunnel --url http://localhost:8000
```

A **quick tunnel** prints a random `https://<words>.trycloudflare.com` address. Use one to try
the whole path end to end, and read the next section before you keep it: that address changes
every time cloudflared restarts, and the Worker *holds* it, so a new address means editing the
config and redeploying. ngrok had the same rotating address, but nothing was configured with
it — the tunnel *was* the public address — which is the trade this design makes.

### A stable address (what to use for real)

If the Cloudflare account has a domain (a **zone**), give the tunnel a hostname of your own
and it stops changing:

```bash
cloudflared tunnel login
cloudflared tunnel create attendance
cloudflared tunnel route dns attendance api.example.com   # your zone, your name
cloudflared tunnel run --url http://localhost:8000 attendance
```

Then `API_ORIGIN = "https://api.example.com"` and it is set once, forever.

## 2. Deploy the Worker

```bash
# one browser approval, once per machine
npx --yes wrangler login

cd deploy/cloudflare
npx --yes wrangler deploy
```

Set `API_ORIGIN` in `wrangler.toml` (the `[vars]` block) to the tunnel address, with no
trailing slash. `npx --yes wrangler deploy --dry-run` validates the config without an account,
which is a useful check before the real thing.

Deploying replaces whatever the Worker currently serves — code and assets both. The frontend
bundle in `../../frontend` is uploaded as part of it, so a frontend fix goes out with the
Worker rather than separately.

Deploying from the dashboard instead of wrangler works too, but the asset directory and the
`run_worker_first` list are part of the deploy configuration rather than the code: they live in
the Worker's **Settings → Static assets** panel (`frontend/` as the directory, `ASSETS` as the
binding, and the four path prefixes from `wrangler.toml`). `backend/tests/
test_cloudflare_worker_proxy.py` fails if the code's list and `wrangler.toml`'s list stop
agreeing, so change both together.

## 3. Check it

```bash
curl -s https://al-jehad1.abdallahtamet281.workers.dev/api/v1/status   # JSON, not an HTML 404
```

- `{"error_code":"worker_misconfigured", ...}` — the message names the variable to set.
- `api_unreachable` — cloudflared is not running, or `API_ORIGIN` names a tunnel that has
  since been replaced (see the quick-tunnel note above).
- `502` from Cloudflare's edge instead of either — the tunnel exists but nothing is listening
  on `:8000`; check terminal 1.

Then open the Worker address on a phone and sign in. GPS and the camera need the HTTPS the
Worker provides, which is the reason to serve the app this way rather than over the LAN.

## What the proxy deliberately does and does not do

- **It is an allow-list.** `/api`, `/static`, `/enroll`, `/q` are forwarded; anything else is
  served from the asset bundle, so a path nobody listed is a 404 rather than a public alias for
  whatever the backend serves there (`/metrics`, a future admin route, an endpoint that only
  checks its token). `/apiary` is an asset name, not an API call.
- **Requests and answers pass through untouched.** Method, path, query, body, `Authorization`,
  status, headers — including a 403, which must reach the worker's screen as a refusal and not
  be tidied into a 200 by a second implementation of the API's contract.
- **It does not authenticate anything.** The API's token checks, the geofence and the face
  match are unchanged; this moves the public *address*, it does not add a wall. The tunnel
  address stays private for the same reason it always was: it is the backend, and a Worker
  hostname is easy to guess from the frontend.
- **No CORS, no cookie games.** One origin, so there is no preflight and no
  `Access-Control-Allow-Origin` to get wrong.
- **No interstitial handling for a Cloudflare Tunnel.** Cloudflare does not show the "you are
  about to visit" page ngrok's free tier does, so nothing has to be skipped. The Worker still
  sets `ngrok-skip-browser-warning` for an ngrok origin, because that path is still supported
  and an HTML warning page parsed as JSON is a confusing way to find out.

## Two things to watch after the first deploy

1. **Rate limits and logged addresses.** The Worker forwards the client's address as
   `X-Forwarded-For` (from `CF-Connecting-IP`), and cloudflared forwards its own view of the
   connection on top. The backend only *believes* that header when it arrives from a trusted
   hop, so the worth-checking case is every worker sharing one `15/minute` bucket and seeing
   `429`s together — or readiness reporting `proxy_not_trusted`. `cloudflared` connects to the
   backend from `127.0.0.1`, which is trusted by default, so the documented setup should
   resolve to the worker's own address; if it does not, set `TRUSTED_PROXIES` to the hops in
   front of the backend and check `GET /api/v1/readiness`.
2. **The shell's caching.** The backend serves the frontend with `Cache-Control: no-cache,
   must-revalidate` on purpose — a months-old `frontendjavascript.js` on a phone is how a fixed
   bug keeps reproducing. Cloudflare's asset serving has its own caching rules for files that
   are not content-hashed, and they are not this repo's to set: if a device keeps running an
   old shell after a deploy, check `Cache-Control` on the response and, if needed, add a
   `_headers` file to `frontend/`.
