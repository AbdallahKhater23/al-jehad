# Who may read each API answer

Every readable `GET` route of this deployment on one page: **who may read it**, the **status** the recorded answer has, and every **feature-switch state** the route may legitimately be in, each in the words of the declaration that owns it. Nothing on this page is written by hand - it is rendered from those declarations, and `backend/tests/test_read_access_document.py` fails when the two disagree.

* The **readiness gate** (`backend/readiness.py`) decides who may read a route and is enforced at start-up; a route that is neither guarded nor declared with a reason stops the deployment rather than answering.
* The **record** (`backend/tests/api_response_shapes.json`) holds the status every read answered and the field names it carried. The names are not repeated here; that file and `backend/tests/test_api_response_shapes.py` are where they live and how they are checked.
* The **feature switches** (`readiness.SWITCHED_STATES`) hold the states a route may legitimately answer in when a switch moves it, so a toggled-off endpoint is a declared state rather than a re-record - and the gate itself reports which of them this deployment is in, warning at startup about a state nobody has declared.

**Regenerate with:** `cd backend && python -m tools.route_readers --write`

70 readable `GET` route(s) recorded: 8 open to an anonymous caller, 2 self-gated, 60 guarded by an audience, 0 accounted for nowhere by the gate, and 20 more the gate advertises that a plain reader cannot address.

---

## Open to an anonymous caller (8 routes)

Anybody who can reach the port may read these, and each reason below is the gate's own argument for why that is safe. A route that stops answering without a session is a change of access, not a re-record.

- **`GET /`** - answered `200`, 1 field name(s): `#body`
  Why it is open: it serves one of the frontend's own HTML files and answers no data of ours - `readiness.PAGE_ROUTES` keeps the pages apart from the endpoints that deliberately parse a request.

- **`GET /api/v1/branding`** - answered `200`, 7 field name(s)
  Why it is open: the company's own name, lines and mark: the sign-in screen needs them before anybody has signed in, and the same strings are already on every printed sheet. Setting them stays admin-only.

- **`GET /api/v1/branding/logo`** - answered `404`, 1 field name(s): `detail`
  Why it is open: the mark the public branding read above points at.
  The recorded answer is a refusal of the *request*, and the gate does not read it as a disagreement with its own advert: a 404 or a 422 is the handler refusing the request rather than the caller (`api_surface.gate_conflict`).
  Declared feature-switch states (2):
  - `mark_configured` - answered `200`, 1 field name(s): `#body`. Legitimate because: an administrator uploaded a mark: the route serves bytes this server re-encoded itself, so the answer is a document rather than a mapping - the one answer on the API whose shape is `#body`. Reached by `POST /api/v1/admin/branding/logo`.
  - `no_mark` - answered `404`, 1 field name(s): `detail`. This is the state the record was taken in. Legitimate because: the deployment has no mark of its own, so the route answers one 404 and the screens print the lockup this application ships with. Reached by `DELETE /api/v1/admin/branding/logo`, which is idempotent; this is the state the seeded fixture is in.

- **`GET /api/v1/readiness`** - answered `200`, 89 field name(s)
  Why it is open: the deployment's verdict, for the load balancer and `curl -f`: booleans and check *names* only, never a detail string (those embed absolute paths).

- **`GET /api/v1/register`** - answered `404`, 2 field name(s): `detail.error_code`, `detail.message`
  Why it is open: the registration link's path with no token on it - what a link pasted without its last segment asks for. It answers one 404 `registration_link_invalid` and nothing else, deliberately: the alternative is the framework's `Not Found`, which is true and gives the person holding the phone nothing to act on.
  The recorded answer is a refusal of the *request*, and the gate does not read it as a disagreement with its own advert: a 404 or a 422 is the handler refusing the request rather than the caller (`api_surface.gate_conflict`).

- **`GET /api/v1/status`** - answered `200`, 1 field name(s): `status`
  Why it is open: the liveness probe: `{"status": "active"}` and nothing else.

- **`GET /register`** - answered `200`, 1 field name(s): `#body`
  Why it is open: it serves one of the frontend's own HTML files and answers no data of ours - `readiness.PAGE_ROUTES` keeps the pages apart from the endpoints that deliberately parse a request.

- **`GET /sites/new`** - answered `200`, 1 field name(s): `#body`
  Why it is open: it serves one of the frontend's own HTML files and answers no data of ours - `readiness.PAGE_ROUTES` keeps the pages apart from the endpoints that deliberately parse a request.

---

## Self-gated: refuses an anonymous caller itself (2 routes)

No role guard can express what these accept, so each refuses an anonymous caller *itself*, inside the handler, for a credential the caller has to present. A 2xx recorded for one of these is the failure this list exists to name.

- **`GET /api/v1/metrics`** - answered `401`, 2 field name(s): `detail.error_code`, `detail.message`
  Why it refuses an anonymous caller itself: `METRICS_TOKEN` compared with `compare_digest` when configured, otherwise an admin JWT. It cannot use a `Depends` role guard because a scrape token is one of the two accepted credentials.
  The recorded answer is the refusal the gate advertises: the record is a read with *no* session, and a self-gated or guarded route is expected to refuse exactly that read.
  Declared feature-switch states (3):
  - `enabled_no_scrape_token` - answered `401`, 2 field name(s): `detail.error_code`, `detail.message`. This is the state the record was taken in. Legitimate because: metrics are served and the reader presented no credential at all: the mount refuses with the scrape-token message. This is the state the seeded fixture is in, and the reason the mount is declared self-gated rather than open.
  - `optional_extra_missing` - answered `501`, 2 field name(s): `detail.error_code`, `detail.message`. Legitimate because: `prometheus_client` is an optional extra and this deployment does not have it, so the mount reports the missing library instead of failing a scrape with a 500. Reachable on a deployment that installed only `requirements.txt`.
  - `switched_off` - answered `404`, 1 field name(s): `detail`. Legitimate because: `METRICS_ENABLED=0`: the surface answers 404 rather than 403, deliberately, because a disabled endpoint should not confirm to a stranger that it exists.

- **`GET /metrics`** - answered `401`, 2 field name(s): `detail.error_code`, `detail.message`
  Why it refuses an anonymous caller itself: the same handler, mounted at the bare path a scrape config assumes.
  The recorded answer is the refusal the gate advertises: the record is a read with *no* session, and a self-gated or guarded route is expected to refuse exactly that read.
  Declared feature-switch states (3):
  - `enabled_no_scrape_token` - answered `401`, 2 field name(s): `detail.error_code`, `detail.message`. This is the state the record was taken in. Legitimate because: the same handler mounted at the bare path a scrape config assumes, so it answers exactly what the scoped path answers.
  - `optional_extra_missing` - answered `501`, 2 field name(s): `detail.error_code`, `detail.message`. Legitimate because: the same handler, the same missing-library report.
  - `switched_off` - answered `404`, 1 field name(s): `detail`. Legitimate because: the same handler, the same `METRICS_ENABLED=0` 404.

---

## Guarded by an audience (60 routes)

These require a session, and only the audience named. The audience is read off the guard's own marker, which is the same fact `readiness.api_routes_authorised` enforces at start-up and `test_ownership_matrix.py` reads with a real session.

- **`GET /api/v1/admin/active_sessions`** - answered `200`, 10 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/audit_log`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/corpus_consents`** - answered `200`, 7 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/coverage_report`** - answered `200`, 79 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/dashboard`** - answered `200`, 76 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/devices`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/enroll/needs_reenrollment`** - answered `200`, 5 field name(s): `count`, `detector`, `enrolled_checked`, `pipeline`, `stale#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/enrollment/invites`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/enrollment/jobs`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/live_ops/count`** - answered `200`, 13 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/logs`** - answered `200`, 16 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/notes`** - answered `200`, 9 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/overtime/crossings`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/overtime/crossings/count`** - answered `200`, 1 field name(s): `count`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/pending_reviews`** - answered `200`, 19 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/punch_queue`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/quick_links`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/readiness`** - answered `200`, 298 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/registrations`** - answered `200`, 5 field name(s): `count`, `enabled`, `pending`, `requests#len`, `status`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/registrations/intake`** - answered `200`, 7 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/registrations/link`** - answered `200`, 9 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/reports/attendance`** - answered `200`, 24 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/reports/export`** - answered `200`, 1 field name(s): `#body`
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/reports/payroll`** - answered `200`, 18 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/reports/pending`** - answered `200`, 19 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/reports/shifts`** - answered `200`, 18 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/retention`** - answered `200`, 35 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/shift_rules`** - answered `200`, 16 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/site_categories`** - answered `200`, 9 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/sites`** - answered `200`, 22 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/admin/users`** - answered `200`, 15 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/attendance/devices`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/auth/me`** - answered `200`, 3 field name(s): `id`, `name`, `role`
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/developer/alerts`** - answered `200`, 31 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/audit`** - answered `200`, 10 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/db/backups`** - answered `200`, 39 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/db/integrity`** - answered `200`, 18 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/db/stats`** - answered `200`, 26 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/diagnostics/pool`** - answered `200`, 15 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/diagnostics/slow-queries`** - answered `200`, 5 field name(s): `explainable#len`, `explainable[]`, `note`, `recent#len`, `threshold_ms`
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/engine/process-stats`** - answered `200`, 16 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/ml/diagnostics`** - answered `200`, 91 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/ml/shadow-summary`** - answered `200`, 15 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/notifications`** - answered `200`, 3 field name(s): `notifications#len`, `unacknowledged`, `unread`
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/offline/devices`** - answered `200`, 7 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/offline/tamper-alerts`** - answered `200`, 8 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/refused-punches`** - answered `200`, 1 field name(s): `#len`
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/runtime`** - answered `200`, 13 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/developer/sessions`** - answered `200`, 9 field name(s)
  Read as: a session whose role is one of `developer`.

- **`GET /api/v1/geofence`** - answered `200`, 9 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/sites`** - answered `200`, 8 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/status/detail`** - answered `200`, 28 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`.

- **`GET /api/v1/worker/me/corpus/consent`** - answered `200`, 3 field name(s): `capture_enabled`, `granted`, `history#len`
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/worker/me/logs`** - answered `200`, 15 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/worker/me/notifications`** - answered `200`, 8 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/worker/me/push`** - answered `200`, 6 field name(s): `available`, `enabled`, `max_age_minutes`, `public_key`, `reason`, `subscriptions`
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/worker/me/report`** - answered `200`, 17 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/worker/me/site-window`** - answered `422`, 6 field name(s): `detail#len`, `detail[].input`, `detail[].loc#len`, `detail[].loc[]`, `detail[].msg`, `detail[].type`
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.
  The recorded answer is a refusal of the *request*, and the gate does not read it as a disagreement with its own advert: a 404 or a 422 is the handler refusing the request rather than the caller (`api_surface.gate_conflict`).

- **`GET /api/v1/worker/me/stats`** - answered `200`, 18 field name(s)
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

- **`GET /api/v1/worker/notes`** - answered `200`, 6 field name(s): `categories#len`, `categories[]`, `max_open`, `notes#len`, `open`, `unread`
  Read as: a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.

---

## Accounted for nowhere by the gate (0 routes)

The gate has no statement about who may read these at all - neither a guard nor a line in one of its three tables. `readiness.api_routes_authorised` is fatal, so an entry here means the gate is failing rather than that the route is fine.

None.

---

## Reads the record holds no answer for (20 routes)

These are `GET` routes too, and the gate states a policy for each, so they are named here rather than left to the shape of the silence: a page of the read surface that omitted them would read as though they did not exist. None of them has a recorded answer to show, and almost all of them cannot have one: a path parameter carries somebody's record or a credential, and one route answers until the client goes away, so none can be read *twice at one held-still instant*. `backend/tests/test_api_read_drift.py` declares the reason for each, which is what keeps a new one from shipping unlisted - and a route here that a plain reader *can* address is a finding rather than a category.

- **`GET /api/v1/admin/enrollment/jobs/{job_id}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/live_ops/stream`** - a session whose role is one of `admin`, `head_admin`.
  Not read because it answers until the client goes away, so there is no second read at one instant.

- **`GET /api/v1/admin/notes/{note_id}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/pending_review_frame/{log_id}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/quick_link_photo/{use_id}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/quick_links/{link_id}/uses`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/registrations/{user_id}/photo`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/users/{user_id}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/admin/workers_live/{site_name}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/developer/diagnostics/query-plan/{name}`** - a session whose role is one of `developer`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/developer/refused-punches/{refusal_id}/frame`** - a session whose role is one of `developer`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/enroll/{token}`** - self-service enrollment: the invite token in the path is the credential, so a worker who has no account yet can open the link they were handed.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

- **`GET /api/v1/q/{token}`** - one-tap clock link: the link token in the path is the credential. Reusing one does not authenticate anybody else.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

- **`GET /api/v1/register/{token}`** - the registration link's own page reads the policy it has to satisfy - the upload ceiling, the accepted roles, the shortest password - before anybody has an account. The link token in the path is the credential: it is an HMAC of the deployment's SECRET_KEY over the link's generation, so a request without it is refused before the policy is answered, and replacing the link invalidates every copy by arithmetic. It answers with that policy and no data of any kind, and it says so when intake is switched off rather than 404-ing a link the company already handed out.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

- **`GET /api/v1/register/{token}/moallems`** - the same link, asked which moallems the applicant may be assigned to: the dropdown on the same page, read without an account by the person filling it in. The path's token is the credential here too (`require_link`), and what it answers is the *roster of supervisors* - name and id of active moallem accounts, and nothing else about them. It is deliberately not a public `/moallems`: a staff list reachable from any URL would be the first thing about this deployment a stranger could enumerate.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

- **`GET /api/v1/worker/notes/{note_id}`** - a session whose role is one of `admin`, `head_admin`, `moallem`, `off_office`, `worker`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /api/v1/worker/stats/{worker_id}`** - a session whose role is one of `admin`, `head_admin`.
  Not read because the path carries somebody's record, which is a different experiment (see `RECORDS`).

- **`GET /enroll/{token}`** - it serves one of the frontend's own HTML files and answers no data of ours - `readiness.PAGE_ROUTES` keeps the pages apart from the endpoints that deliberately parse a request.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

- **`GET /q/{token}`** - it serves one of the frontend's own HTML files and answers no data of ours - `readiness.PAGE_ROUTES` keeps the pages apart from the endpoints that deliberately parse a request.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

- **`GET /register/{token}`** - it serves one of the frontend's own HTML files and answers no data of ours - `readiness.PAGE_ROUTES` keeps the pages apart from the endpoints that deliberately parse a request.
  Not read because the token in the path *is* the permission, so there is nothing to put in the path.

---

## What this page is not

* Not the record, and not the check: the field names are in `backend/tests/api_response_shapes.json`, held by `backend/tests/test_api_response_shapes.py`, and a release can verify them without pytest with `python -m tools.response_shapes`.
* Not the write surface: `POST`, `PATCH` and `DELETE` routes have their own audiences, checked the same way by `readiness.api_routes_authorised`.
* Not the deployment's own switch state: a state listed here is one the code and the record agree is legitimate, and the record holds 5 answer(s) that are deliberate refusals.
* Not a field list, and `#body` is not a field: it is the record's marker for an answer that is a document rather than a JSON mapping (`api_surface.shape`), which the pages and the branding mark answer with.
