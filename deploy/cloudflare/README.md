# Serving the frontend from Cloudflare Workers

The app is **single-origin by construction**: `API.resolveBaseURL` answers `page origin +
/api/v1`, which is what keeps GPS, the camera and the session working wherever the page is
served from. Host the frontend on a host of its own and every call — login, the worker's own
stats, the punch, the clock-in window on the punch card — goes to that host's `/api/v1` and
404s. It fails quietly: the app opens, and then the login screen says the server is
unreachable while the server is up. (That is what `al-jehad1.abdallahtamet281.workers.dev`
did before this Worker: shell `200`, `/api/v1/status` `404`.)

So the Worker does the two things that keep one origin true: it serves the frontend from its
asset bundle, and it forwards the paths the backend owns to the tunnel.

| File | What it is |
| --- | --- |
| `worker.mjs` | The Worker: asset serving, and an allow-list of proxied paths. |
| `wrangler.toml` | The deploy config: the asset directory, the binding, `run_worker_first`, and `API_ORIGIN`. |

## Deploy

```bash
# terminal 1 - the backend, plain HTTP for the tunnel to wrap
python backend/serve.py --tunnel

# terminal 2 - the public HTTPS address for the backend
ngrok http 8000

# terminal 3 - the frontend + proxy, from this directory
npx wrangler deploy
```

Set `API_ORIGIN` in `wrangler.toml` (the `[vars]` block) to the tunnel address, with no
trailing slash, before the first request. Nothing else needs changing: `CORS_WORKER_ORIGINS`
stays empty and the document CSP's `connect-src 'self'` keeps holding, because the browser
only ever sees one origin.

Deploying from the dashboard instead of wrangler works too, but the asset directory and the
`run_worker_first` list are part of the deploy configuration rather than the code: they live
in the Worker's **Settings → Static assets** panel (`frontend/` as the directory, `ASSETS` as
the binding, and the four path prefixes from `wrangler.toml`). `backend/tests/
test_cloudflare_worker_proxy.py` fails if the code's list and `wrangler.toml`'s list stop
agreeing, so change both together.

## Check it

```bash
curl -s https://<your-worker>/api/v1/status      # JSON, not an HTML 404
```

If that answers `{"error_code":"worker_misconfigured", ...}` the message names the variable to
set. If it answers `api_unreachable`, the tunnel is not running. Every other API call behaves
like this one: the Worker adds no error handling of its own beyond the two it cannot pass on.

## What the proxy deliberately does and does not do

- **It is an allow-list.** `/api`, `/static`, `/enroll`, `/q` are forwarded; anything else is
  served from the asset bundle, so a path nobody listed is a 404 rather than a public alias
  for whatever the backend serves there (`/metrics`, a future admin route, an endpoint that
  only checks its token). `/apiary` is an asset name, not an API call.
- **Requests and answers pass through untouched.** Method, path, query, body,
  `Authorization`, status, headers — including a 403, which must reach the worker's screen as
  a refusal and not be tidied into a 200 by a second implementation of the API's contract.
- **It does not authenticate anything.** The API's token checks, the geofence and the face
  match are unchanged; this moves the public *address*, it does not add a wall. Keep the
  tunnel address private as before — a Worker hostname is easy to guess from the frontend.
- **No CORS, no cookie games.** One origin, so there is no preflight and no
  `Access-Control-Allow-Origin` to get wrong.

## Two things to watch after the first deploy

1. **Rate limits and logged addresses.** The Worker forwards the client's address as
   `X-Forwarded-For` (from `CF-Connecting-IP`), but the backend only *believes* that header
   when it arrives from a trusted hop — otherwise every punch carries the proxy's address and
   the whole site shares one `15/minute` bucket. `ngrok`'s agent connects from `127.0.0.1`,
   which is trusted by default, so the documented setup works; if a burst of workers start
   seeing `429`s together, or readiness reports `proxy_not_trusted`, set `TRUSTED_PROXIES` to
   the proxy hops in front of the backend and check `GET /api/v1/readiness`.
2. **The shell's caching.** The backend serves the frontend with `Cache-Control: no-cache,
   must-revalidate` on purpose — a months-old `frontendjavascript.js` on a phone is how a
   fixed bug keeps reproducing. Cloudflare's asset serving has its own caching rules for
   files that are not content-hashed, and they are not this repo's to set: if a device keeps
   running an old shell after a deploy, check `Cache-Control` on the response and, if needed,
   add a `_headers` file to `frontend/`.
