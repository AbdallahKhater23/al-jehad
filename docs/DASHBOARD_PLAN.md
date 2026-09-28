# Dashboard — the console's front door

A console with eleven tabs and no front door makes an administrator open three screens to learn
nothing is wrong. This is the screen that says what the deployment *is* — sites, categories,
accounts, who is on site, what is waiting on a person — in the five seconds before somebody
decides whether to keep reading.

Decided with the team on 2026-09-28: a **new Dashboard tab that is the landing screen** for every
console role, holding four panels in the first version — **people & places counts**, **waiting on
a person**, **now**, and **period metrics** — with the deployment-health panels reserved for the
root tier (the Developer tab already owns them).

---

## 1. The decision that shapes everything: counted, not downloaded

**None of these numbers exists as a read today.** They are derived by downloading lists:
`fetchLiveOps()` already `Promise.all`s `/admin/active_sessions`, `/admin/users`, `/admin/sites`
and `/admin/shift_rules` — four full payloads, on a poll loop, so that a board can count what is
in them. `GET /admin/users` computes `password_set` per row and joins the audit log for each
account's last password change; `GET /admin/sites` resolves a three-layer clock-in window per
site. Both are the right shape for the screen that *edits* them and the wrong shape for a screen
that counts them.

So: **one aggregate read, computed with `COUNT`/`GROUP BY` over indexed columns, never by
fetching rows to count them in Python.** A dashboard built from the existing list reads would pay
the cost it exists to remove, and would grow with the deployment instead of with the number of
panels.

The second half of the same decision: **every number is scoped by the rules of the list it
summarizes.** `developer.visibility_clause` hides the root account *inside the query* precisely
because a Python-side filter still hands back the count of what it removed, and a count is an
enumeration. A total that ignores the clause re-opens the hole the roster closed.

---

## 2. The read

```
GET /api/v1/admin/dashboard?days=7          admin_only
POST /api/v1/admin/dashboard/export?...     the period as CSV/XLSX, reusing reports/export
```

```json
{
  "status": "success",
  "as_of": "2026-09-28 14:03:11",
  "day": {"start": "2026-09-28", "end": "2026-09-29", "timezone": "Asia/Kuwait"},
  "people":  {"accounts": 41, "active": 38, "deactivated": 3, "by_role": {"worker": 30},
              "enrolled": 36, "no_face": 5, "no_password": 0,
              "new_this_week": 4, "never_clocked_in": 2},
  "places":  {"sites": 6, "categories": 3, "by_category": [{"category": "Warehouse", "sites": 2}],
              "no_category": 0, "overriding_window": 2, "unmanned_today": ["Site C"]},
  "now":     {"on_shift": 12, "by_site": [{"site_name": "Depot", "workers": 4}],
              "overtime_open": 1, "offline_waiting": 3, "refused_24h": 2},
  "waiting": {"reviews": 4, "registrations": 2, "notes": 1,
              "alerts": null, "oldest_seconds": 8100},
  "period":  {"days": 7, "preset": "days", "start": "2026-09-22", "end": "2026-09-28",
              "workers": 31, "expected_days": 5, "present_days": 132,
              "late_arrivals": 4, "approved_hours": 488.5, "overtime_hours": 6.0,
              "awaiting_approval_hours": 12.0, "average_attendance_rate": 0.85,
              "quietest": [{"worker_id": "43", "worker_name": "Row 43", "present_days": 1,
                            "rate": 0.2, "late": 2}],
              "most_late": [{"worker_id": "12", "worker_name": "Row 12", "present_days": 4,
                             "rate": 0.8, "late": 3}]}
}
```

Four fields beyond the first draft of this table, all of them the window rather than a figure:
``start``/``end`` are the dates it covers (the range control marks itself from ``preset``, and the
extremes' links carry all three), and ``days`` is how many days long it is. §13 says why.

`as_of` is not decoration: this is a **snapshot a human refreshes**, not a live board (see §7).

### Definitions — the part that decides whether anyone trusts it

| Number | Definition | Source |
| --- | --- | --- |
| `people.accounts` | every account this reader may see, root account excluded by the query | `users` + `developer.visibility_clause` |
| `active` | `status = 'active'` - the status `main.verify_worker` lets record attendance | `users.status` |
| `pending_approval` | the status a walk-up submission files and an approval lifts: a real account that can sign in and may not punch. Counted **apart from** `deactivated`, because the two are opposite facts - one is waiting for a person, the other was switched off - and folding them together makes a deployment with a queue look like a roster that has been disabled | `users.status`, `registrations.STATUS_PENDING_APPROVAL` |
| `deactivated` | the complement over the other two: `accounts - active - pending_approval` | `users.status` |
| `by_role` | the five roles; `off_office` and `moallem` counted apart, never folded into "workers" | `users.role` |
| `enrolled` / `no_face` | an enrollment is **recorded** (`enrolled_at` set and `biometric_id` present). Deliberately not "the template file exists": that is a filesystem stat per row, and the row is the count. Credentials owns the file truth, per account | `users` |
| `no_password` | `password_hash` empty or null — an account that cannot sign in | `users` |
| `never_clocked_in` | active account with no `attendance_logs` row **ever** | `attendance_logs` |
| `sites` / `categories` | rows in `construction_sites` / `site_categories` | both tables |
| `overriding_window` | site with its own clock-in window set (not inheriting from category or company) | `construction_sites` |
| `unmanned_today` | a site with **zero** attendance rows in the current Kuwait day | `attendance_logs` |
| `now.on_shift` | rows in `active_sessions` — the same source the Live Ops board draws | `active_sessions` |
| `now.overtime_open` | open shifts past the line with no standing authorisation | `overtime.open_crossings` |
| `now.offline_waiting` | a punch handed over by a phone that is **not an attendance row yet**: `materialized_log_id IS NULL` and a verified status (`offline_sync.PENDING_STATUSES`). Deliberately not `processed_at IS NULL`, which is stamped on rejections too and so counts a refused signature as work somebody still owes | `punch_queue` |
| `now.refused_24h` | refused punches in the last 24 h. **A rate to watch, not a work queue**: the row has no triage state (see `refused_punches`), so nothing here can be "cleared" | `refused_punches` |
| `waiting.reviews` | `status_code IN ('pending_review', 'pending_overtime')` — the same predicate `/admin/reports/pending` already uses | `attendance_logs` |
| `waiting.registrations` | accounts in quarantine (`status = 'pending_approval'`) - a submission files a *real* account now, so the queue is a set of accounts and the concealment clause travels with it exactly as it does over the roster | `users.status`, `registrations.STATUS_PENDING_APPROVAL` |
| `waiting.notes` | notes in `notes.OPEN_STATUSES` | `worker_notes` |
| `waiting.alerts` | unacknowledged alerts — **root tier only**; `null` for an administrator, because the queue is not theirs to read | `admin_notifications` |
| `period.*` | `reports.attendance_rows` over the range, aggregated: `present_days` = distinct worker-days, `expected_days` = weekdays in the range from `shift_rules.working_days` (floored at 1), `rate = present / expected` capped at 1 | `reports.py` |
| `period.approved_hours` / `awaiting_approval_hours` / `overtime_hours` | the **timesheet's** own arithmetic (`_shift_hours`, written once for SQL and pinned against the Python by test): an approved row counts its approved figure, a `pending_overtime` row credits the standard day it already earned and holds the rest, every other undecided row waits in full, a refusal counts for nothing; `overtime_hours` is `SUM(overtime_hours)` over the same rows - the hours that needed a decision | `reports.py` |
| `period.quietest` / `most_late` | the attendance report's own rows, capped at `reports.EXTREMES_LIMIT` (3) and ordered by lowest `attendance_rate` then most `late_arrivals` (the reverse pair for `most_late`), worker id last so a tie is stable — each entry carrying the worker's name and both figures, and each drawn as a link into that person's own rows over the panel's window | `reports.py` |
| `period.start` / `end` / `days` / `preset` | the window itself, as the route resolved it: `preset` is `days` or `month`, `days` its length in calendar days. The control marks itself from `preset` and the two linkages carry `start`/`end`, so a link describes the period the figure beside it came from | `dashboard._period_window` |

**The reports module owns the period numbers.** `attendance_rows()` already returns exactly this
vocabulary (`days_present`, `expected_days`, `attendance_rate`, `late_arrivals`) plus a `totals`
block, and it already decides what "present" and "expected" mean. The dashboard must consume
those definitions rather than restate them — a second implementation is how two screens start
disagreeing about a worker's attendance. Its `totals` gains the aggregate figures the summary
needs (accepted as a small, additive change to that module, not a fork).

---

## 3. The panels

**Waiting on a person** — first, because it is the only panel that changes behaviour. Pending
reviews, pending registrations (with the intake switch state, which the Registrations tab already
reads), open notes, and — root tier only — unacknowledged alerts. Each count is a link into the
queue that lists it, ordered by how long the oldest item has waited, which is the rule the
registration queue already follows.

**Now** — on shift (total and by site), open overtime crossings, offline punches waiting to
materialize, refused punches in the last day. This is where the dashboard earns its place beside
Live Ops rather than duplicating it: the board answers *who*, this answers *how many and is that
normal*.

**People** — accounts by role and state, enrolled vs no face on file, no password set, new this
week, never clocked in. The last two are the hygiene signals: an account created and never used
is a hire who never started, or a typo in a roster.

**Places** — sites, categories, sites by category, sites overriding their window, sites nobody
has clocked into today.

**Period** (last 7 days by default, this month on tap) — present days, expected days, late
arrivals, approved hours, overtime hours, hours awaiting approval, average attendance rate, plus
the two extremes worth acting on: the lowest attendance and the most late arrivals. The window is
drawn with the figures ("2026-09-22 to 2026-09-28") and the control is two buttons rather than a
date picker, because this panel offers two questions - "how is this week going" and "how did this
month go" - and a date picker answers neither in one tap.

**Health — root tier only.** Push backlog, retention overdue, readiness advisories, engine and
database state. The Developer tab already holds all of it; the dashboard reuses those reads for
the root tier and draws nothing for an administrator, exactly as the Alerts tab is drawn only for
the tier that can read its route.

---

## 4. Beyond the four panels — worth taking, in order

1. **Dormant accounts.** Active accounts with no punch in N days. Nobody notices a worker who
   stopped showing up until payroll or a headcount review, and this is one query.
2. **Onboarding watch.** Accounts approved or created in the last N days that have never clocked
   in — the follow-on to the approval notice, and the fastest way to catch "they never signed in"
   or "their face was never enrolled".
3. **Payroll readiness.** Shifts still awaiting approval in the period and the period's approved
   hours total. "Is payroll ready to run" is the question this application exists to answer, and
   today it is answered by reading a report.
4. **Unmanned sites today**, ranked by size, not alphabetically. A site with twelve accounts and
   no clock-in is a different fact from a site with one.
5. **Attendance-rate leaders and laggards** over the period, as a *link into the attendance
   report for that person* rather than as a leaderboard. The data is already there; the value is
   the route to the context, not the ranking.
6. **Export the period** from the dashboard, reusing `/admin/reports/export` — a summary that
   cannot hand you the artifact sends the reader to another screen to get it.

Two things I would deliberately **not** add: a chart (a number an administrator has to look at
twice is worse than the number printed plainly, and the console has no chart library to carry),
and anything that only the root tier can act on inside an administrator's panel.

---

## 5. What stays in its own tab

The dashboard summarizes; it does not become the place everything happens. The rule, stated so it
can be enforced:

> A panel shows a **count, a state, or a trend with a link**. If a screen needs a control, a list,
> or a decision, it lives in the tab that owns that work — and the dashboard links to it.

Live Ops stays exactly as it is (the live board, polling, with the force-in controls). Credentials
keeps the roster. Reports keeps the tables and the exports.

---

## 6. Audience

- **Administrator / head administrator:** every panel except Health and Alerts. Counts are scoped
  to what they may see, root account excluded *in the query*.
- **Root tier:** everything, including Health and Alerts.
- No panel is drawn for a role that cannot act on it, and a failed sub-read degrades to that panel
  saying so — never to a blank page, and never to a zero standing in for unknown.

---

## 7. Not live, and why

Live Ops polls. The dashboard does not: one read with a visible `as_of` stamp and a refresh
control. Silently drifting totals are worse than totals that admit they are a minute old, and a
polled aggregate is a query every few seconds against the same SQLite writer that serves the gate.
This is a deliberate inconsistency with the board next door and has to be *written down* in the
panel itself, or an operator will assume the figures are live.

---

## 8. i18n

Every label, every panel heading, every state sentence, in all four tables (`i18n.js`,
`i18n.ar.js`, `i18n.hi.js`, `i18n.ur.js`) — the parity suite counts keys and fails on a
placeholder mismatch, so this is a mechanical cost to budget, not an afterthought. Number
formatting goes through the existing degree/day formatters rather than a new one.

---

## 9. Phasing

| Phase | Contents |
| --- | --- |
| 1 - built | `GET /admin/dashboard` with `people`, `places`, `waiting` (no Health); the Dashboard tab, first in `ADMIN_TABS`, painted from one read; the landing-tab assertions updated. What shipped, and where it went past this plan, is in §11 |
| 2 - built | `now` panel (active sessions, crossings, punch queue, refused punches), reusing `overtime.open_crossings`. What shipped, and the three definitions that moved under it, is in §12 |
| 3 - built | `period` panel: the additive `totals` change in `reports.py`, the range control, the extremes with links. What shipped is in §13 |
| 4 | Export, dormant accounts, onboarding watch, payroll readiness |
| 5 | Root-tier Health from the Developer reads |

Landing on the dashboard needs no new routing: `renderAdminTab` already falls back to the first
tab this reader is offered, so tab *order* is the switch. Two tests currently pin that landing to
Live Ops — `test_frontend_admin_alerts.py` and `test_frontend_developer_console.py` — and both
become "Dashboard", which is the honest cost of the change and the reason to make it deliberately.

---

## 10. Test strategy

Written before the UI, in the shape the backend suites already use:

1. **A planted-data count test per number.** Seed a known world (three sites in two categories,
   N accounts across roles with some deactivated, some enrolled, some never clocked in), assert
   every figure exactly. A dashboard off by one is trusted, which makes it worse than none.
2. **Concealment regression.** With a root account present, assert it appears in no total — and
   that the *counts* do not reveal it either.
3. **Period boundaries.** 23:59 vs 00:01 of the Kuwait day; a shift that started yesterday and is
   still open; an offline punch that materializes after midnight; a range with no working days.
4. **Cross-surface agreement.** `waiting.reviews` equals what `/admin/reports/pending` returns;
   `waiting.registrations` equals the queue read's `pending`; `now.on_shift` equals the Live Ops
   board's row count from the same fixture.
5. **Degradation.** A dashboard whose metrics sub-read fails still paints the rest; a fresh
   deployment of zeroes looks deliberate, not broken (the readiness precedent: a closed intake is
   a state, not a fault).
6. **Cost.** A test that the endpoint issues no full-list read — the payload is counts, so a
   regression here is a payload that grew with the roster.
7. **Frontend, in the VM harness:** one read on paint, no inline handler (the CSP budget only
   falls), every number's link present and pointing at the tab that owns it, and the `as_of` stamp
   drawn.

---

## 11. Phase 1, as built

`backend/dashboard.py` holds the read and the reasoning for it; `GET /api/v1/admin/dashboard` is
`admin_only`. The three panels are exactly the ones §2 specifies, in the shapes it specifies -
`people`, `places`, `waiting`, plus `as_of` and `day`. `now` and `period` are **absent rather than
`null`**, so a client that wants them is a phase-2/3 change on both sides and not a panel that
looks like it failed.

### Decisions made while building it, that this plan did not state

* **`_ACTIVE_SQL`** (people.active, and the scope of `never_clocked_in`) treats a missing `status`
  as active, matching the login gate (`str(status or 'active')`). The column is `NOT NULL DEFAULT
  'active'`, so this is belt and braces for a hand-written or restored row - what it must not do is
  *disagree* with the door, because an account this screen calls deactivated and the login route
  admits is a number that sends somebody looking for a bug that is not there.
* **The alert count is an inline `COUNT`, not `notifications.unacknowledged_count`.** That function
  answers `0` when its query fails, which is the one output this module forbids, and this panel
  needs the age of the oldest alert anyway. The predicate is the same; the error handling is not,
  and the docstring says so.
* **`new_this_week` has three sources**, because there is no `users.created_at`: the audit actions
  `ACCOUNT_CREATION_ACTIONS` (the credentials console and `admin_create`), and a walk-up approval
  read from the request row it decided, since `registrations` files its audit entry against the
  *request*. Both are joined back to `users`, so a deleted account's audit row cannot inflate the
  count - and the suite drives the real console endpoint to hold the tuple to the door that
  actually writes it.
* **`by_category` includes a category with no sites in it** (a `LEFT JOIN`): it is a real answer,
  and it keeps `len(by_category) == categories` an invariant a test can hold.
* **The day window is half-open** (`00:00:00` to *next* midnight), so a punch recorded in the last
  second of a day cannot fall between two days. `_day_bounds` is the only place "today" is
  decided, and the docstring marks the two boundaries the attendance-UTC migration moves.

### What moved, and one thing this plan did not know about

§9 says two tests pin the landing tab to Live Ops. **Four did**, and there was a fifth switch this
plan did not mention at all: `State.adminTab` was itself the literal `'Live Ops'`, so tab *order*
alone would not have moved the landing - `renderAdminTab(State.adminTab)` was what the first
console frame painted. Both now name the Dashboard, and `test_frontend_dashboard.py` asserts
`State.adminTab === ADMIN_TABS[0].id` so the two cannot drift apart again.

The four: `test_frontend_admin_alerts.py`, `test_frontend_developer_console.py`,
`test_frontend_credentials.py` (the tab inventory), and two assertions in
`test_frontend_shifts_filter.py` that read the tab a link left the reader on. That last pair now
names the landing tab once, as a module constant.

### Tests, as shipped

* `backend/tests/test_admin_dashboard.py` (17). A planted world whose every figure is stated
  exactly; the complements adding up; `as_of` and the day block; cross-surface agreement with
  `/admin/reports/pending`, the registrations queue and the notes queue; `oldest_seconds`
  following the queue that holds the oldest item; concealment (the root account moves no total,
  and the root tier's own read still sees it); the alert queue being `null` for an administrator
  and the *unanswered* pair being what ages it; the half-open day; a dropped table answering
  `null` for one panel and nothing for the others; an empty deployment reading zeroes; the real
  console endpoint feeding `new_this_week`; and the cost test, which traces the SQL the read
  issues and requires **every statement to be a count** and the *number* of them to be identical
  for a ten-account roster and a two-hundred-and-ten-account one.
* `backend/tests/test_frontend_dashboard.py` (13, Node VM). The tab is first and a console lands
  on it; **one** request on paint and it is the aggregate endpoint; every figure drawn from the
  server's own field; the stamp and the "not on a timer" sentence; each count linking to the tab
  that owns its queue; the alert row drawn for the root tier and not for an administrator; a
  `null` panel drawn as unread while the other two paint; a failed *read* still leaving the
  refresh control on screen; the empty deployment; a `<img onerror>` site name staying text; the
  bindings reached by hook (no new inline handler, so the CSP budget only falls); the refresh
  re-reading and moving the stamp; and the same keys answering in all four tables.

## 12. Phase 2, as built

The `now` panel is in `dashboard._now` and drawn by `dashboardNowHtml`. One figure is borrowed
whole and three are counted, and each was a decision rather than a translation:

* **`on_shift` and `by_site` are the Live Ops board's own join** - the same `JOIN users`, so the
  same rows, including dropping a session whose account is gone. The plan's §10.4 asked for the
  board's row count on the same fixture; the suite goes one further and compares the *by-site*
  split too, because that is the half an implementation gets wrong by counting a different join.
  The board is not concealed, and neither is this: a dashboard that hid a session the board names
  would be two answers to one question, and §6's "no panel for a role that cannot act on it" is
  about *surfaces*, not about people standing at a gate.
* **`overtime_open` stays borrowed** from `overtime.open_crossings` (`needs_answer`, the same guard
  the nav badge counts) - and the suite holds it against *both* reads that serve that queue: the
  Approvals payload and `/admin/overtime/crossings/count`.
* **`offline_waiting` borrows a predicate rather than restating one.** It is
  `materialized_log_id IS NULL AND status IN offline_sync.PENDING_STATUSES` - the materialiser's own
  guard, now published as a constant so the backlog the dashboard reports is the work that module
  will actually pick up. §2 originally wrote this as "rows with no `processed_at`", which is the
  near-miss it turned out to be: that column is stamped on *rejection* as well, so it would report a
  signature this deployment already refused as work somebody still owes. The suite plants exactly
  that row, so the two definitions are demonstrably different numbers.
* **`refused_24h` is a window over `refused_punches`, scoped through `COALESCE(worker_id, '')`** -
  the one table here where a punch can be recorded with *no* worker at all (a face the engine could
  not match), and the plain clause would drop every one of them, because `NULL NOT IN (...)` is not
  true. It is the panel's only **rate rather than a queue**, and the note under the rows says so:
  nothing about a refused punch can be cleared.

### Two definitions that moved under phase 1, and are now fixed in §2

The walk-up flow was rebuilt while this panel was being written (a submission files a real
quarantined account instead of a row in a holding table), which made three of phase 1's definitions
wrong rather than merely stale:

1. `waiting.registrations` is a count of **accounts in quarantine**, not of request rows - which
   makes it the one panel whose scoping is load-bearing rather than belt and braces.
2. `people` gained **`pending_approval`**, and `deactivated` is now the complement over it. Before
   that, every application in the queue counted as a *deactivated account*: the same number, read as
   the opposite fact, and the one an administrator would go looking for a cause for.
3. `now.offline_waiting`'s predicate, above.

### Tests, as shipped (phase 2)

`test_admin_dashboard.py` gained the planted `now` fixture (four sessions, one of them for an account
that does not exist; four queued punches, two of them un-filed and two deliberately not; three
refusals, one outside the window and one naming nobody) and four tests: the Live Ops agreement, the
crossing agreement against both reads, the materialiser's queue against `/admin/punch_queue`, and the
refusal window - plus the root account's own queued punch being visible to nobody else.
`test_frontend_dashboard.py` gained the panel's four figures, its by-site split, the refusal note,
the empty deployment saying nobody is on site, and the two rows that carry a link against the one
that must not.

### Still owed

Phases 4-5: export, dormant and onboarding watch, payroll readiness, and the root-tier Health
panel. The Arabic, Hindi and Urdu strings for this screen were written by the person who wrote the
English ones and have not been reviewed by a native speaker.

## 13. Phase 3, as built

The `period` panel is `reports.attendance_period`, drawn by `dashboardPeriodHtml`; the window it covers
is chosen by `dashboard._period_window` from the route's one parameter, and `GET
/api/v1/admin/dashboard?days=` takes a day count or the word `month`.

### The decision that shaped it: a counted twin, not a summary of a download

§2 said the dashboard must consume `reports.attendance_rows`' definitions. It cannot consume
`attendance_rows` itself: that function returns one row per worker present *and* reads the whole
`users` table for names, which is exactly the cost this endpoint exists to remove, and its price
would grow with the roster rather than with the number of panels. So the reports module gained
`attendance_period` - the same questions, answered with aggregate SQL over the same range and the
same definitions:

* presence is one statement (`COUNT`, `COUNT(DISTINCT worker || char(31) || day)`, `AVG`) over a
  `GROUP BY worker_id`, with the denominator from ``_expected_days`` - the helper `attendance_rows`
  now uses too, so "expected" cannot mean two things;
* the hours are a `CASE` aggregate built from the module's *own* tuples (`PAYABLE_CODES`,
  `PENDING_OVERTIME_CODE`, `AWAITING_APPROVAL_CODES`), so a status added to one reaches the other;
* the extremes are one query each, `ORDER BY` the two figures and cut to `LIMIT`.

`attendance_rows` keeps its rows and its per-worker figures; its `totals` gained `present_days`,
`late_arrivals` and the three hour figures, which is the additive change §2 asked for.

### Decisions this plan did not state

* **The window travels in the payload.** `start`, `end`, `days` and `preset` are in `period`, because
  the control marks itself from `preset` and each linkage is only honest if it carries the window the
  figure beside it describes. `days` stays a *length* so the first draft's shape survives; `preset` is
  the token the route took.
* **`month` is a word, not a number.** A saved link has to mean the same window next week, and
  `days=11` on the 11th would pin the link to one morning. A pattern refuses anything else with a 422
  (a window that quietly became seven days because somebody typed `days=week` is a wrong answer that
  looks like a right one), and a longer window than this build counts is clamped to 366 days rather
  than refused, so a link from a future build still answers.
* **One late predicate, two readers.** The markers live in `LATE_FLAG_MARKERS` and drive both the
  Python classifier (`attendance_rows`) and the SQL aggregate, because the panel's figure has to be
  the sum of the column underneath it. Two wordings are in the wild - "clock-in window" and "late" -
  and both are planted.
* **The extremes are not the report's ordering.** §2 said "the same two fields `reports` already
  sorts by", and `attendance_rows` actually sorts by rate and then worker id, best first. The panel
  answers two different questions, so `quietest` breaks a rate tie by the late count and `most_late`
  by the rate, with the id last so a tie is stable across reads. The suite holds the panel's *set*
  equal to the report's worst-rate workers - not the order, which is asserted in the panel's own
  test - so the claim "the report owns the figures" is tested and the claim "the orders match" is not
  made.
* **A worker with no Clock In row is in neither list.** The report's rows are workers who *were*
  present; "nobody has seen this person in a week" is the dormant-account question (§4.1), and
  answering it here would put an absent worker at the top of a list about attendance rates. The
  fixture plants exactly that worker (a refusal on file, no arrival) and asserts they are not named.
* **Not scoped by the concealment clause**, for phase 2's reason: this summarizes the attendance
  report, which is not concealed either - and the root account cannot appear in it at all, because
  the gate refuses the developer role a punch (`main.verify_worker`). If that ever changed, the
  report would name the account too and both would move together.
* **`overtime_hours` follows `/worker/me/report`**: `SUM(overtime_hours)` over the same Clock Out
  rows, never added into the other two figures. It is the hours that needed a decision, not hours
  worked twice.

### Frontend

The panel is the fifth in the shell, `data-dashboard-panel="period"`. Its window control is two
`data-dashboard-preset` chips (the one in effect carries `aria-pressed`, which is already the
console's selected treatment - no new CSS), bound by hook in `bindDashboardControls`; the choices are
held in `State.dashboardDays`, so they survive a tab switch and a language change, and a tap re-reads
the tab rather than re-filtering the payload on screen. Each extreme row carries
`data-dashboard-worker`/`-start`/`-end`, and `dashboardOpenWorker` writes the period and the person
into the Shifts tab's own state (clearing a stale category filter, which would otherwise hide the very
rows the link promises) and lets that tab's `syncShiftsUrl` put the window in the URL. No inline
handler was added: the CSP budget only falls.

### Tests, as shipped (phase 3)

`test_admin_dashboard.py` (29, +7, plus the payload-shape, empty-deployment, cost and
borrowed-predicate tests extended): a window whose every figure is hand-derived (`PERIOD_SHIFTS`,
including both late wordings, an adjusted approval, a pending overtime split, a refusal, and a day
either side of the range); the half-open edges to the second; the counted hours held against
`_shift_hours` over every status code; the panel held against `/admin/reports/attendance` for the same
window, figures and names; the window resolution (`7`, `1`, `month`, `0`, the 366-day clamp, and the
422 for junk); the month preset counting the days the week no longer covers; and degradation by making
the read raise, which is the only way to isolate *this* panel - `attendance_logs` is the places panel's
own table.

The cost test was rewritten around what the panel actually is: **no statement may return an unbounded
set of rows** - each one aggregates, or caps itself (the rules by primary key, and the board's
`LIMIT 5` scan of the oldest readable clock-ins). The two extremes are per-worker rows, so "every
statement is a COUNT" was a rule the panel could only have kept by not existing; `LIMIT` is the
boundary that makes them safe, and the statement count is still identical for a ten-account roster and
a two-hundred-and-ten-account one.

`test_frontend_dashboard.py` (18, +4): the window drawn with its figures and exactly one preset in
effect; both linkages opened - the person, the window and the tab, with the URL fragment written by
the Shifts tab itself; a tap on the other preset re-asking the server with `?days=month`; and the two
states that are not numbers - a window nobody worked (zeroes and two "nobody was present" lines, with
the window still described) and a panel that could not be read (no figures, no window control, the
panels beside it untouched).
