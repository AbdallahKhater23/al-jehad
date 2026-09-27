# Runbook — Persistent mobile sessions (30-day tokens) + pre-authenticated load test

## The problem, measured

Bcrypt is the only CPU-bound step in this system (~0.21 s of core time per login), and the
deployment has ~1 vCPU. Throughput therefore saturates at `1 / 0.21 ≈ 4.75 logins/s` no matter
how many requests arrive: latency grows linearly instead of throughput. A 40-worker shift start
that forces everyone through `/auth/login` spends ~8 s of pure CPU before the last worker's
first punch, and a 100-worker cold start ~21 s.

The fix is not to make bcrypt faster. It is to **stop asking for it**: a phone that already holds
a session goes straight to the punch.

## Backend

- **30-day tokens.** `config.ACCESS_TOKEN_EXPIRE_DAYS = 30` → `settings.jwt_ttl_hours = 720`.
  `create_access_token` is unchanged; only the default TTL moved. Env overrides:
  `ACCESS_TOKEN_EXPIRE_DAYS=30` (or `JWT_TTL_HOURS=720`). **Set it on Railway too** — the
  deployment's own environment wins over the code default.
- **Password-free refresh.** `POST /api/v1/auth/refresh` re-signs the presented token's claims
  with a fresh `exp`. It costs one JWT signature and **zero password hashing**: the token is
  validated by `security.decode_access_token` and the account re-read for `token_version`. A
  daily user renews here long before expiry and never sees the login screen.
- **The zero-bcrypt dependency.** `security.get_current_user` is what every protected route
  depends on (directly, or via `require_role` / `any_authenticated`). It is a JWT decode plus one
  `users` row read — no password is ever verified on a normal request. The punch endpoint
  (`POST /api/v1/attendance/verify`) depends on `any_authenticated`.
- **Revocation is immediate.** `token_version` is compared on every request. A password change or
  a deactivation (`/admin/users/status`) bumps it, invalidating every outstanding token at once —
  `/auth/refresh` cannot renew past that. This is why a 30-day TTL is safe to ship.

## Frontend (vanilla-JS PWA)

- The session now persists in `localStorage` under `session`
  (`{user: {…, token}, expires_at}`), not `sessionStorage`. `sessionStorage` is dropped when the
  tab — or the installed PWA — closes, which is the every-morning re-login.
- `loadStoredSession()` validates the stored session **before** it becomes `State.user`: a token
  is required, and an expired `expires_at` is refused. A legacy `sessionStorage.user` is migrated
  once, so the upgrade does not sign everyone out.
- At boot, `UI.init()` renders straight to the handset/console (skipping login) and then
  `confirmRestoredSession()` calls `/auth/me` once (no bcrypt) to confirm the token still works,
  and `/auth/refresh` when it is inside the 7-day refresh window.
- A 401 is handled globally by `API.request` (session cleared, login drawn); a network failure
  keeps the session — a phone with no bars must not be signed out.

**Storage caveat.** This is a web PWA, so there is no OS keychain; a JWT in `localStorage` is
readable by any script on the origin. The compensating controls are server-side (`exp`,
`token_version`). The strictly safer browser transport is an **httpOnly cookie**, which no
script can read; that needs a same-origin deployment and CSRF handling and is a follow-up.

## Pre-authenticated load test

Mint tokens once, then run with no logins at all:

```bash
cd backend

# 1. Activate the roster FIRST (deactivation bumps token_version, which revokes old tokens)
LOCUST_ADMIN_PASSWORD='…' python tools/locust_provision.py --reactivate --roster roster50.json

# 2. Mint one token per account (login limiter is 10/min/IP; ~5 min for 50 accounts)
LOCUST_HOST=https://al-jehad-production.up.railway.app \
  python tools/mint_tokens.py --roster roster50.json --verify
#    -> writes roster50.json.tokens.json

# 3. Run 40 pre-authenticated workers arriving 0.2 s apart (~5 arrivals/s)
export LOCUST_HOST=https://al-jehad-production.up.railway.app
LOCUST_ONLY=punch LOCUST_ALLOW_PUNCH=1 \
LOCUST_ROSTER=roster50.json.tokens.json \
LOCUST_SITE_FIX="29.344001,48.016299" \
LOCUST_PUNCH_GAP=0.2 LOCUST_ONE_SHIFT=1 \
LOCUST_SHIFT_SECONDS_MIN=20 LOCUST_SHIFT_SECONDS_MAX=20 \
LOCUST_RAMP_LEAD_IN=10 \
locust -f tools/locustfile.py --headless --host "$LOCUST_HOST" \
  -u 40 -r 40 -t 90s --only-summary --csv locust_token_ramp

# 4. Deactivate when done
LOCUST_ADMIN_PASSWORD='…' python tools/locust_provision.py --deactivate --roster roster50.json
```

- The roster's `token` field makes `PunchUser` seed the token and **never call `/auth/login`**.
- Provide `LOCUST_SITE_FIX` (and no admin creds, or `LOCUST_ADMIN_TOKEN`) for a run with exactly
  zero login requests; the site is otherwise discovered with one admin login.
- The punch payload is the app's own multipart shape: `worker_id`, `action`
  (`Clock In`/`Clock Out`), `location_input`, `selfie`, optional `confirm_early_checkout`.

### Reading the result

`--csv locust_token_ramp` writes `locust_token_ramp_stats.csv`. Expect a `/api/v1/auth/login`
row only if you did not pre-authenticate; with tokens, the auth row should be absent and the
punch p95 should track the Phase-2/3 numbers (~0.9–1.0 s at 40–50 concurrent). Thresholds and a
verdict are enforced by `tools/locust_provision.py --phase3`; for a pure pre-authenticated ramp,
compare p95 and the failure count directly.

## Verification done

- `tests/test_auth_token_contract.py`: `expires_in == 30 days`, `expires_at` ~30 days out,
  `/auth/refresh` re-issues a working token with no password, and `/auth/refresh` refuses an
  anonymous caller.
- `tests/test_frontend_auth_wiring.py`: the session persists to `localStorage`, is cleared on
  401/logout, and a tokenless stored session is never restored.
- `tests/test_api_route_authorisation.py`, `test_role_audience.py`, `test_session_integrity.py`,
  `test_frontend_credentials.py`, `test_pages_boot_in_a_browser.py` all green.
