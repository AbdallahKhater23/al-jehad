"""What every test *starts from*: the seeded state of the throwaway database.

WHY THIS EXISTS
---------------
``harness.seed_database`` is the reason a test can address a worker as ``"1"`` or a flagged
record as ``900001`` and expect an answer. All of that was stated in one docstring and one
comment per block - "the tests assert on exact counts", "one in-progress session is created so
clock-out tests are stable", "a day with only a clock-in would be a shift the report never
shows", "the row is put back to unconfigured on every reset" - and **none of it was asserted
anywhere**. The three facts the rest of the suite leans on hardest were the ones nothing read
back:

* the roster is rewritten from ``SEED_USERS``, so no test depends on whichever credentials the
  live server happens to have;
* each seeded row carries its stable ``biometric_id``, because ``INSERT OR REPLACE`` drops
  every column the insert does not name - which is how this was found the first time, with
  seeded faces resolving to ``<id>.json`` files that no longer existed;
* there is exactly **one** ``pending_review`` record and **one** open session, whichever
  flagged rows the live database arrived with.

A fixture that silently stops seeding is the most expensive kind of breakage here: the tests
that depend on it fail in ways that look like feature regressions, in files that are fine.

Numbers are read from the harness's own tables (``SITES``, ``SEEDED_HISTORY_DAYS``,
``DEFAULT_SHIFT_RULES``) rather than written out again, so a seed change that a test should
follow does not have to be made twice.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import harness
from harness import (
    EMAILS,
    PASSWORDS,
    ROLES,
    SEEDED_HISTORY_DAYS,
    SEEDED_HISTORY_HOURS,
    SEEDED_PENDING_LOG_ID,
    SEED_BIOMETRIC_IDS,
    SEED_USERS,
    SITES,
    WORKER,
    db_rows,
    db_scalar,
)

# ---------------------------------------------------------------------------
# the roster
# ---------------------------------------------------------------------------


def test_every_seeded_account_is_the_one_the_tests_name(app_module):
    """The credentials a test signs in with are in the row it signs in against.

    This is what "tests never depend on whichever credentials the live server happens to have"
    means in practice, and the failure it prevents is nasty: a stale hash still *exists*, so a
    login returns 401 and the test reports a broken authentication path.
    """
    for user_id, (name, role, password, email) in SEED_USERS.items():
        row = db_rows("SELECT name, email, password_hash, role FROM users WHERE id = ?", (user_id,))
        assert row, f"user {user_id} is missing from the seeded roster"
        stored_name, stored_email, stored_hash, stored_role = row[0]
        assert (stored_name, stored_email, stored_role) == (name, email, role)
        assert app_module.pwd_context.verify(password, stored_hash), (
            f"the password {user_id} is documented as having does not verify against the hash "
            "in the database the tests run against"
        )
        assert (PASSWORDS[user_id], EMAILS[user_id], ROLES[user_id]) == (password, email, role), (
            "the derived lookup tables disagree with SEED_USERS, so a test importing one of them "
            "is signing in as something the seeder does not create"
        )


def test_the_stored_biometric_id_survives_the_reseed(app_module):
    """``INSERT OR REPLACE`` deletes and re-inserts, so unnamed columns fall back to defaults.

    The id is named explicitly in the insert for this reason. If it ever stops being, every
    seeded account resolves its face to a file that no longer exists, and the symptom is a 500
    from enrolment rather than anything that points at the fixture.
    """
    for user_id in SEED_USERS:
        assert db_scalar("SELECT biometric_id FROM users WHERE id = ?", (user_id,)) == (
            SEED_BIOMETRIC_IDS[user_id]
        ), (
            f"{user_id}'s biometric_id is not the value the harness names, so the template written "
            "for it is orphaned while the row still looks enrolled"
        )


# ---------------------------------------------------------------------------
# the one flagged record and the one open session
# ---------------------------------------------------------------------------


def _flagged_rows() -> list[tuple]:
    placeholders = ",".join("?" * len(SEED_USERS))
    return db_rows(
        f"SELECT id, worker_id, status FROM attendance_logs "
        f"WHERE status = 'pending_review' AND worker_id IN ({placeholders})",
        tuple(SEED_USERS),
    )


def test_exactly_one_flagged_record_and_it_is_the_seeded_id(app_module):
    """Approval tests address this row by id, so a second one makes counts ambiguous."""
    rows = _flagged_rows()
    assert len(rows) == 1, (
        f"{len(rows)} flagged records for the seeded accounts, so \"the\" review the approval "
        f"tests act on is whichever one the live database happened to carry: {rows}"
    )
    assert rows[0][0] == SEEDED_PENDING_LOG_ID, rows
    assert rows[0][1] == WORKER, rows


def test_flagged_records_are_normalised_by_the_seed_itself(app_module):
    """Reseeded **in place**, the way ``with_site_windows`` does it, not through a reset.

    That distinction is the test. A reset rotates in a fresh generation built from the pristine
    snapshot, so a planted row would vanish for a reason that has nothing to do with the
    ``DELETE`` inside ``seed_database`` - and the claim would pass untested. Reseeding in place
    is how the normalisation is actually reachable: a test that has created its own flagged
    record (which is most of them) and then re-seeds a site window to change the rules.
    """
    before = len(_flagged_rows())
    connection = sqlite3.connect(str(harness.current_db_path()))
    try:
        connection.execute(
            "INSERT INTO attendance_logs (worker_id, site_name, action, timestamp, hours, score, "
            "status, status_code) VALUES (?,?,?,?,?,?,?,?)",
            (
                WORKER,
                "Downtown Tower A",
                "Clock In",
                "2026-09-01 05:00:00",
                0.0,
                0.5,
                "pending_review",
                "pending_review",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    assert len(_flagged_rows()) == before + 1, "the planted row did not land, so this proves nothing"

    harness.seed_database(app_module)

    rows = _flagged_rows()
    assert len(rows) == 1 and rows[0][0] == SEEDED_PENDING_LOG_ID, (
        f"a flagged record the test itself created survived a reseed ({rows}); the approval tests "
        "address one review by id, so a second one makes every count ambiguous"
    )


def test_exactly_one_session_is_open_and_it_is_the_seeded_one():
    """Clock-out tests are stable only while the open session is the fixture's own."""
    rows = db_rows("SELECT worker_id, site_name FROM active_sessions")
    assert len(rows) == 1, f"{len(rows)} open sessions: {rows}"
    assert rows[0] == (WORKER, "Downtown Tower A"), rows


# ---------------------------------------------------------------------------
# the history and the configuration the reports read
# ---------------------------------------------------------------------------


def test_the_seeded_history_is_a_pair_of_complete_shifts(app_module):
    """``SEEDED_HISTORY_DAYS`` is documented as *completed* shifts, and the report selects
    ``action = 'Clock Out'`` - so a day seeded with only a clock-in is a shift the hour tests
    silently cannot see, not a smaller fixture.
    """
    for day in SEEDED_HISTORY_DAYS:
        rows = db_rows(
            "SELECT action, status, approved_hours FROM attendance_logs "
            "WHERE worker_id = ? AND timestamp LIKE ? ORDER BY action",
            (WORKER, f"{day}%"),
        )
        assert [row[0] for row in rows] == ["Clock In", "Clock Out"], (day, rows)
        assert all(row[1] == "Approved" for row in rows), (day, rows)
        clock_out = next(row for row in rows if row[0] == "Clock Out")
        assert clock_out[2] == SEEDED_HISTORY_HOURS, (
            f"{day}'s clock-out carries approved_hours {clock_out[2]!r}, not "
            f"{SEEDED_HISTORY_HOURS!r} - every report that sums the day reads this column"
        )


def test_the_sites_are_the_seeded_geofences(app_module):
    """The coordinates a punch is inside or outside of are the fixture's, not the live server's."""
    seeded = {row[0]: (row[1], row[2], row[3]) for row in db_rows(
        "SELECT site_name, lat, lon, radius FROM construction_sites"
    )}
    assert seeded == SITES, (
        "the seeded geofences do not match SITES, so \"inside exactly one site\" and \"outside all "
        f"sites\" are assertions about the live database's sites: {seeded} vs {SITES}"
    )


def test_the_shift_rules_are_the_shipped_ones(app_module):
    """Company rules are configuration an administrator can change from the console.

    A test that asserts "an inheriting site is late at noon" has to be asserting the *shipped*
    window; otherwise it is measuring whatever shift the company moved to this week.
    """
    defaults = app_module.DEFAULT_SHIFT_RULES
    row = db_rows(
        "SELECT %s FROM shift_rules WHERE id = 1" % ", ".join(defaults), ()
    )
    assert row, "the seeded shift rules row is missing"
    assert dict(zip(defaults, row[0])) == dict(defaults), (
        "shift_rules does not carry the shipped defaults, so every window assertion is measuring "
        "a live setting"
    )


def test_the_company_identity_is_unconfigured_on_every_reset(app_module):
    """``NULL`` is the meaningful state here: "nobody configured this", which is what makes the
    built-in lockup the default rather than something somebody chose.

    The counter is deliberately left alone (it is a cache-buster, not a setting), so it is not in
    this list.
    """
    cleared = ("company_name", "company_legal", "company_est", "company_tagline",
               "logo_bytes", "logo_mime", "logo_width", "logo_height", "updated_by")
    row = db_rows("SELECT %s FROM company_settings WHERE id = 1" % ", ".join(cleared))
    assert row, "the company settings row is missing"
    configured = {name: value for name, value in zip(cleared, row[0]) if value is not None}
    assert not configured, (
        f"{sorted(configured)} survived the reset, so a test of \"a deployment that never opened "
        "the panel\" is reading a real company's logo and name"
    )


# ---------------------------------------------------------------------------
# the templates the seeded accounts are matched against
# ---------------------------------------------------------------------------


def test_every_seeded_account_is_enrolled_inside_the_throwaway_tree(app_module):
    """``/attendance/verify`` refuses without a stored reference, and the *name* is the contract.

    The template is named by the account's immutable biometric id, so a reader that built the
    name by hand would look at a path nothing writes. Asserted through the application's own
    resolver (``harness.template_exists``), and against the location as well: the harness's
    safety rule 2 is that no template may be written into the checkout.
    """
    for user_id in SEED_USERS:
        assert harness.template_exists(user_id), (
            f"{user_id} is seeded without a template, so every punch test fails with 'Facial "
            "reference not registered' - a fixture failure that reads as a feature regression"
        )
        resolved = Path(os.path.realpath(str(harness.reference_path(user_id))))
        assert harness.REFS_DIR.resolve() in resolved.parents, (
            f"{user_id}'s template is at {resolved}, outside the throwaway tree - a test run must "
            "never put a face in the checkout's own directory"
        )
        assert harness.LIVE_REFS.resolve() not in resolved.parents, (
            f"{user_id}'s template was written into the live reference directory ({resolved})"
        )


def test_seeding_a_template_outside_the_throwaway_directory_is_refused(monkeypatch):
    """The guard that makes the previous test safe to rely on, and it has to *raise*.

    A resolver that answered with a live path is not hypothetical: it is what a template written
    before ``conftest`` has redirected the directories looks like (see ``reference_path``'s own
    docstring, where a first attempt at this did exactly that).

    The path used here is inside the temp root but outside ``REFS_DIR`` rather than the live
    reference directory, and that is deliberate: if this guard is ever removed, the test has to
    fail *without* leaving a face in the checkout for it to be testing against.
    """
    outside = harness.TMP_ROOT / "a-template-that-must-not-be-written.json"
    monkeypatch.setattr(harness, "reference_path", lambda user_id: outside)

    with pytest.raises(RuntimeError, match="outside the throwaway directory"):
        harness.seed_reference(WORKER)
    assert not outside.exists(), f"the refusal happened after writing {outside}"


# ---------------------------------------------------------------------------
# the two helpers the fixture state rests on
# ---------------------------------------------------------------------------


def test_a_site_window_helper_restores_what_it_found_even_when_the_test_fails(app_module):
    """Lifetime, not just application: a window left behind is a night shift on a site for
    every test after it, which is how one failing test turns into a suite-wide mystery.
    """
    original = dict(harness.SEED_SITE_WINDOWS)
    night = {"Downtown Tower A": ("22:00", "23:30", None)}
    try:
        with pytest.raises(RuntimeError):
            with harness.with_site_windows(app_module, night):
                assert harness.SEED_SITE_WINDOWS == night
                raise RuntimeError("a failing test body")
    finally:
        restored = dict(harness.SEED_SITE_WINDOWS)
        harness.SEED_SITE_WINDOWS.clear()
        harness.SEED_SITE_WINDOWS.update(original)

    assert restored == original, (
        f"the window mapping was left as {restored} instead of {original}: a failing test would "
        "hand the next one a night shift"
    )


def test_the_isolation_guard_refuses_a_database_that_is_not_the_clone(app_module, monkeypatch):
    """The one safety property everything else rests on, asserted rather than assumed.

    The guard's whole value is that it *raises*: if it ever answered quietly - or compared the
    wrong thing - a run would mutate real payroll data and the first symptom would be silent.
    """
    import config

    harness.assert_database_isolation()  # the state every test actually runs in

    monkeypatch.setattr(config.settings, "database_path", str(harness.LIVE_DB))
    with pytest.raises(RuntimeError, match="must never touch live data"):
        harness.assert_database_isolation()


def test_the_reset_clears_the_rate_limit_buckets(client, app_module):
    """The other half of "one test starts where the last one left off": the limiter is state.

    Asserted behaviourally - exhaust the login limit, reset, and get a real answer again -
    because that is the contract the suite depends on. Asserting on the limiter's internals
    would pin an implementation the application does not own.
    """
    per_minute = int(str(app_module.settings.login_rate_limit).split("/")[0])
    body = {
        "user_id": WORKER,
        "email_or_phone": EMAILS[WORKER],
        "password": "not-the-password",
    }
    statuses = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(per_minute)]
    assert set(statuses) == {401}, f"the first {per_minute} attempts were not all refused: {statuses}"

    limited = client.post("/api/v1/auth/login", json=body)
    assert limited.status_code == 429, (
        f"the limiter did not engage after {per_minute} attempts (got {limited.status_code}), so "
        "this test is not setting up the state it means to reset"
    )

    harness.reset_rate_limits(app_module)

    after = client.post("/api/v1/auth/login", json=body)
    assert after.status_code == 401, (
        f"a bucket filled by one test still refuses the next one ({after.status_code}); that is "
        "a 429 in a suite that has no idea why"
    )
