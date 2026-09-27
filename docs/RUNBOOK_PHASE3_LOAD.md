# Runbook — Phase 3 load harness (realistic blend + Poisson arrivals)

Phase 2 proved each path in isolation: the face engine held to 50 concurrent punches with flat
p95, and an independent probe found the login wall at ~4.75/s. It never put the two on the one
core together, and it spread the punches with a metronome rather than a human arrival process.
Phase 3 closes both gaps in one run.

## What it does differently

| Dimension | Phase 2 | Phase 3 |
|---|---|---|
| Sessions | every account mints a token in the lead-in | **80% cached** token, **20% cold** login at the arrival instant |
| Auth vs biometrics | temporally separated | **concurrent** — bcrypt runs while other workers punch |
| Arrivals | even ramp, `ordinal × gap` | **Poisson process**, `LOCUST_ARRIVAL=poisson` |
| Blended contention | none | faces the single-core contention directly |

The cold login is a real `POST /api/v1/auth/login` (~0.21 s of CPU), recorded under
`/api/v1/auth/login [phase3 fresh]` so it is measured separately from the lead-in mints.

## Arrival model

The k-th worker arrives at the **k-th point of a Poisson process** of rate `--poisson-rate`
per second — the cumulative sum of exponential inter-arrival times, drawn from a fixed seed so
a run is reproducible. This produces the clustered sub-second micro-stampedes a real shift
start has, which the even ramp averaged away. If `--poisson-rate` is not given it is derived so
the roster arrives over `--arrival-window` seconds (default 30).

## Run it

```bash
cd backend
LOCUST_ADMIN_PASSWORD='...' python tools/locust_provision.py \
  --host https://al-jehad-production.up.railway.app \
  --phase3 --fresh-login-ratio 0.2 --arrival-window 30 --shift 20 \
  --roster /path/to/locust_roster50.json
```

`--phase3` runs the whole cycle: reactivate the roster (re-enrolling faces), run,
evaluate thresholds, then deactivate in a `finally`.

## Pass/fail thresholds

Defined in `tools/locust_provision.py` as `PHASE3_THRESHOLDS` and overridable per flag.

| Metric | Default | Rationale |
|---|---|---|
| auth p95 | `--max-auth-p95-ms 5000` | past ~5 s a phone on a shift-start network shows a spinner a person gives up on |
| auth p99 | `--max-auth-p99-ms 15000` | admits the real single-core tail (measured ~13.4 s at 64 concurrent) without admitting a pathological queue |
| punch p95 | `--phase3-punch-p95-ms 3000` | the same gate budget the Phase-2 ladder used, so numbers are comparable |
| verify 503s | `--max-verify-503 0` | the face engine's overflow answer (`503 + Retry-After`); zero is the only acceptable count |
| failure ratio | `--phase3-max-fail-ratio 0.01` | everything that is not a treated 429; a transient tolerance, not a budget |
| blend sanity | (internal, 80% floor) | at least 80% of the configured cold cohort must actually log in, or the auth threshold measured nothing |

The runner prints a per-endpoint table and a `VERDICT`, and exits non-zero on any breach — so a
CI job or an operator sees a pass/fail number rather than a graph to interpret.

## Notes and limits

- Still one account per simultaneous punch (`active_sessions` holds one open shift per
  `worker_id`), so the roster size caps the burst width.
- `429` is still counted as success by default (`LOCUST_429_IS_SUCCESS=0` to see them); Phase 3
  is exactly the run where the per-IP login limiter may start to matter.
- Run it against a staging deployment before trusting a green result on production data; the
  punches write permanent attendance rows.
