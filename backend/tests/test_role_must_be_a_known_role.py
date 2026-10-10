"""A role must be one of the roles this deployment defines.

WHY THIS SUITE EXISTS
---------------------
Every *guard* in this application compares a role to a literal: ``require_role``
intersects it with an allow-list, ``_guard_standard_admin`` tests it against
``"admin"``, ``security.ADMIN_ROLES`` holds it as a frozenset. Those questions are all
asked about a role that is **already stored on an account**.

Nothing asked whether the role a caller was *creating* was a role at all. ``users.role``
is ``TEXT NOT NULL`` with no ``CHECK`` constraint, ``UserAddRequest.role`` was a bare
``str`` with no validator, and the only two refusals on the creation paths answered
narrower questions:

* ``security.refuse_developer_role`` refuses exactly one role, by name, so that no API can
  mint the root tier. It said nothing about anything else.
* the standard-admin escalation check refused ``req.role in ("admin", "head_admin")`` - a
  literal tuple compared against the raw string, so it was case-sensitive and silent about
  every other value.

``enrollment.VALID_ROLES`` is the list that *looked* like the allow-list in the roster
import. Nothing ever called it.

WHAT THE GAP WAS
----------------
A head administrator (or any holder of ``admin_only``) could create an account in a role
this application has never heard of. The row was written and the console listed it, and the
account could then sign in and reach nothing at all, because ``require_role`` refuses a role
that is in no allow-list - a person locked out of every screen by a mistake nobody could see
in the roster. ``POST /admin/users/add`` even echoed the made-up role back in its success
message.

THE ORDER THIS LANDED IN, AND WHY IT IS WORTH KNOWING
-----------------------------------------------------
This file was written and run **before** the fix: four of the tests below were red, with
``200`` and a written row as the evidence. The same file, unchanged, was green after the
fix. Both creation paths are covered because they are the same door with two content types
- and the multipart one is the endpoint a real administrator's browser actually posts to,
so a rule enforced on only one of them is a rule anybody who reads the code can walk around.

The walk-up registration queue (``registrations.py``) already did this correctly: it
normalises the role, checks it against ``WORKFORCE_ROLES`` and answers ``400``. Its pattern
is what ``security.validate_assignable_role`` generalises rather than a second rule invented
beside it.

``developer`` IS one of the six roles and is still **not assignable**: it is refused by name
with its own 403 (``refuse_developer_role``), which is asserted here so a narrower
allow-list can never quietly become the thing that protects the root tier.
"""

from __future__ import annotations

import pytest

from harness import ADMIN, HEAD_ADMIN, assert_denied, bearer, db_scalar

#: A role that appears nowhere in the application: not in ``security.KNOWN_ROLES``, not in
#: any ``require_role`` call, not in either client's label map. If a creation path accepts
#: it, it accepted anything.
UNKNOWN_ROLE = "supervisor"

#: Account ids, chosen clear of the harness's seeded accounts (1, 600, 800, 1000, 5000) and
#: of the root tier's own id. Each test asserts the id is free *before* it posts, so an
#: "id already exists" answer can never be mistaken for the refusal under test.
UNKNOWN_JSON_ID = "4901"
UNKNOWN_MULTIPART_ID = "4902"
CASE_ESCALATION_ID = "4903"
BLANK_ROLE_ID = "4904"
DEVELOPER_ATTEMPT_ID = "4905"

STRONG_PASSWORD = "site-attendance-2026"


def _take_id(user_id: str) -> None:
    """The id must be free, or the refusal being asserted could be about the id."""
    assert db_scalar("SELECT role FROM users WHERE id = ?", (user_id,)) is None, (
        f"fixture error: account {user_id} already exists in the cloned snapshot, so this "
        f"test cannot tell 'the role was refused' from 'the id was taken'"
    )


def _refused(response, user_id: str, endpoint: str) -> None:
    """The request must be refused *and* must have written nothing.

    The status is not pinned beyond "a refusal": a validator answers ``422``, a
    hand-written check answers ``400``, and an authorization decision answers ``403``. Any
    of the three is the rule being enforced; what none of them may do is leave an account
    behind, which is what the red version of these tests caught.
    """
    stored = db_scalar("SELECT role FROM users WHERE id = ?", (user_id,))
    assert response.status_code in (400, 403, 422), (
        f"a role this deployment does not define was accepted at {endpoint}. "
        f"Got {response.status_code}: {response.text[:200]!r}, and the account was written "
        f"with role={stored!r}"
    )
    assert stored is None, (
        f"the refusal at {endpoint} left an account behind with role={stored!r}"
    )


def add_user(client, *, user_id, role, as_role=HEAD_ADMIN, name="Unknown Role"):
    """``POST /admin/users/add`` - the JSON creation path."""
    return client.post(
        "/api/v1/admin/users/add",
        headers=bearer(as_role),
        json={
            "user_id": user_id,
            "name": name,
            "email": "",
            "phone": "",
            "password": STRONG_PASSWORD,
            "role": role,
        },
    )


def create_user(client, *, user_id, role, as_role=HEAD_ADMIN, name="Unknown Role"):
    """``POST /admin/users/create`` - the multipart path the Credentials tab posts to."""
    return client.post(
        "/api/v1/admin/users/create",
        headers=bearer(as_role),
        data={
            "user_id": user_id,
            "name": name,
            "role": role,
            "password": STRONG_PASSWORD,
        },
    )


# ---------------------------------------------------------------------------
# an unknown role is refused, and nothing is written
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_add_user_refuses_a_role_the_application_does_not_define(client):
    """The JSON creation path must not write a role that does not exist."""
    _take_id(UNKNOWN_JSON_ID)
    response = add_user(client, user_id=UNKNOWN_JSON_ID, role=UNKNOWN_ROLE)
    _refused(response, UNKNOWN_JSON_ID, "POST /api/v1/admin/users/add")


@pytest.mark.regression
def test_create_user_refuses_a_role_the_application_does_not_define(client):
    """The multipart path is the same door with a different content type."""
    _take_id(UNKNOWN_MULTIPART_ID)
    response = create_user(client, user_id=UNKNOWN_MULTIPART_ID, role=UNKNOWN_ROLE)
    _refused(response, UNKNOWN_MULTIPART_ID, "POST /api/v1/admin/users/create")


@pytest.mark.regression
def test_a_blank_role_is_refused(client):
    """Empty is the value a form sends when it was never filled in."""
    _take_id(BLANK_ROLE_ID)
    response = add_user(client, user_id=BLANK_ROLE_ID, role="")
    _refused(response, BLANK_ROLE_ID, "POST /api/v1/admin/users/add")


@pytest.mark.regression
def test_a_standard_admin_cannot_reach_an_administrator_role_by_case(client):
    """The escalation check compared a raw string, so case walked past it.

    ``req.role in ("admin", "head_admin")`` was exact, which meant ``"HEAD_ADMIN"`` was not
    an administrator role as far as that line was concerned - and the row was written with
    ``role = 'HEAD_ADMIN'``. The stored spelling is inert rather than privileged, because
    every guard compares exactly too, so the harm was the written row and the bypassed
    guard rather than an escalation. Normalising the role before anything compares it is
    what makes both questions read the same string.
    """
    _take_id(CASE_ESCALATION_ID)
    response = add_user(client, user_id=CASE_ESCALATION_ID, role="HEAD_ADMIN", as_role=ADMIN)
    _refused(response, CASE_ESCALATION_ID, "POST /api/v1/admin/users/add")


# ---------------------------------------------------------------------------
# the root tier, and the roles that must keep working
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_developer_role_is_still_unassignable(client):
    """``security.refuse_developer_role`` is the named 403 that protects the root tier.

    Asserted here so the allow-list cannot become the only thing standing between an
    administrator and a ``developer`` account: the refusal is by name, and it answers 403.
    """
    for creator in (ADMIN, HEAD_ADMIN):
        response = add_user(client, user_id=DEVELOPER_ATTEMPT_ID, role="developer", as_role=creator)
        assert_denied(
            response,
            endpoint="/admin/users/add",
            detail=f"a {creator} created a developer account through the API",
        )
    assert db_scalar("SELECT role FROM users WHERE id = ?", (DEVELOPER_ATTEMPT_ID,)) is None, (
        "the refusal answered 401/403 but still wrote the account"
    )


@pytest.mark.regression
def test_a_defined_role_is_still_accepted(client):
    """The rule must not be so strict that it refuses the roles that are real.

    Only the business tiers are exercised: ``developer`` is deliberately not assignable
    through any API (see the test above), and a ``head_admin`` creating a ``head_admin`` is
    the console's own decision rather than this suite's.
    """
    for index, role in enumerate(("worker", "moallem", "off_office", "admin")):
        user_id = str(4910 + index)
        response = create_user(client, user_id=user_id, role=role, name=f"Real {role}")
        assert response.status_code == 200, (
            f"a head admin could not create a {role}: "
            f"{response.status_code} {response.text[:200]!r}"
        )
        assert db_scalar("SELECT role FROM users WHERE id = ?", (user_id,)) == role


@pytest.mark.regression
def test_the_role_is_stored_in_its_canonical_spelling(client):
    """``  Moallem `` is the moallem role, stored the one way this application spells it.

    A role that reached the database with its whitespace or its capitals intact would be a
    role no guard recognises - the same lockout as an unknown one, arrived at from a value
    that looks correct on the form.
    """
    user_id = "4920"
    response = create_user(client, user_id=user_id, role="  Moallem ", name="Padded")
    assert response.status_code == 200, (
        f"a correctly-spelled role was refused: {response.status_code} {response.text[:200]!r}"
    )
    assert db_scalar("SELECT role FROM users WHERE id = ?", (user_id,)) == "moallem"
