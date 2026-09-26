"""The walk-up registration link, reported at every boot.

``registration_enabled`` ships off, and a closed intake is a supported state rather than a
fault - so a check that simply passed when the switch is off would say nothing on the day it
is switched on. ``readiness._check_registration_intake`` reports the three facts that are
otherwise invisible until somebody complains: whether intake is open, how full the review
queue is, and whether the photo directory can be written.

This suite pins each state the check can report:

* closed - an ok check that says so, not a silent pass;
* open with room - the pending count against the cap, the numbers an operator schedules on;
* full - a *failing* check, because the queue is full the one way this feature fails quietly:
  the next applicant is refused and nobody is told;
* an unwritable photo directory while open - reported with the directory, never raised and
  never fatal, because taking the punch gate down for an optional intake is the wrong trade;
* an uncountable queue - reported, never raised, for the same reason;
* membership - the check is in ``readiness.CHECKS``, so a refactor that drops it fails here
  rather than silencing the boot log.
"""

from __future__ import annotations

import secrets

import pytest

import readiness


@pytest.fixture()
def app_module(app_module):
    """The app imported and pointed at the temp database and file trees."""
    return app_module


@pytest.fixture()
def _open(monkeypatch):
    """Intake switched on, which is the only state the check's counts are about."""
    import config

    monkeypatch.setattr(config.settings, "registration_enabled", True)


def _seed_pending(count: int) -> None:
    """Write ``count`` pending requests straight into the review queue."""
    import database

    with database.db() as conn:
        for index in range(count):
            conn.execute(
                """
                INSERT INTO registration_requests
                    (status, full_name, requested_role, photo_path, photo_sha256,
                     created_at, updated_at)
                VALUES ('PENDING_REVIEW', ?, 'worker', ?, ?, datetime('now'), datetime('now'))
                """,
                (f"Applicant {index}", f"/tmp/{index}.jpg", secrets.token_hex(16)),
            )
        conn.commit()


def _registration_check() -> readiness.Check:
    checks, _ = readiness.run_checks()
    matches = [check for check in checks if check.name == "registration_intake"]
    assert len(matches) == 1, "the check must run exactly once"
    return matches[0]


def test_the_check_is_in_the_startup_surface():
    assert readiness._check_registration_intake in readiness.CHECKS, (
        "a queue that fills silently is exactly the thing a boot report exists to surface: "
        "a check that exists but never runs is silence with extra steps"
    )


def test_closed_intake_is_an_ok_check_that_says_so(app_module):
    """Off is a legitimate configuration, so it is reported, not failed."""
    check = _registration_check()
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is True
    assert "closed" in check.detail, check.detail


def test_open_with_room_reports_the_counts_a_decision_needs(_open, monkeypatch, app_module):
    """The countdown names what it has, not just that it is short."""
    import config

    monkeypatch.setattr(config.settings, "registration_pending_cap", 5)
    _seed_pending(2)

    check = _registration_check()
    assert check.ok is True
    assert check.value.get("pending") == 2
    assert check.value.get("cap") == 5
    assert "2 of 5" in check.detail, check.detail


def test_a_full_queue_fails_the_check(_open, monkeypatch, app_module):
    """Full is the state that refuses applicants without telling anybody."""
    import config

    monkeypatch.setattr(config.settings, "registration_pending_cap", 3)
    _seed_pending(3)

    check = _registration_check()
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is False
    assert check.value.get("pending") == 3
    assert check.value.get("cap") == 3
    assert "full" in check.detail, check.detail


def test_an_unwritable_photo_directory_is_reported_never_raised(
    _open, monkeypatch, tmp_path, app_module
):
    """A warning that names the directory is what an operator needs; a boot refusal is not."""
    import registrations
    import tempfile

    monkeypatch.setattr(
        registrations, "photos_dir", lambda: tmp_path / "registration_photos"
    )

    def refuse(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)

    check = _registration_check()
    assert check.tier == readiness.TIER_ADVISORY
    assert check.ok is False
    assert "not writable" in check.detail, check.detail
    assert "registration_photos" in check.detail, check.detail


def test_an_uncountable_queue_is_reported_never_raised(_open, monkeypatch, app_module):
    """An advisory check that throws becomes 'check raised' - which hides the reason."""
    import database

    def broken_db(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(database, "db", broken_db)

    check = _registration_check()
    assert check.ok is True, "a queue that cannot be counted is not a broken deployment"
    assert "could not be counted" in check.detail, check.detail
