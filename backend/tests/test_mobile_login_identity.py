"""The Android client's second sign-in credential: optional, and never an email-only field.

WHY THIS TEST EXISTS
--------------------
``POST /auth/login`` matches the account with::

    WHERE id = ? AND (email = ? OR phone = ?)

so the second credential is compared against *either* column. Two ways of getting that wrong are
invisible to every server-side test, because both of them fail at the keyboard, before a request
is sent:

* **Refusing a blank field.** ``users.email`` and ``users.phone`` both default to ``''``, and the
  console's create-account form offers both as optional, so an account holding neither is one
  ordinary action away. The only value that can match such an account is the empty string, and a
  client-side "required" check strands its owner on a screen asking them to fill in a box they
  have nothing to put in it. The console's own sign-in form had exactly this bug; the suite that
  caught it is ``test_frontend_login_credentials.py``, and this is the Android client's half of
  the same contract.
* **Insisting on an email.** A worker can be registered by phone and sign in with it -
  ``test_walk_up_registration.py`` hands over ``+965 555 0199`` and then signs in with that
  number as the second credential. An email-shaped rule would refuse a value the server accepts.

WHAT IS CHECKED HERE, AND HOW
-----------------------------
The rule lives in ``mobile-client/src/ui/credentials.ts`` and is deliberately pure,
so it can be bundled with esbuild - the same tool Vite uses - and run under Node without a
device, a browser or a Capacitor plugin. Node and esbuild are optional; without them the suite
skips rather than fails, which is how the other mobile suites behave.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from harness import PROJECT_ROOT

NODE = shutil.which("node")
MOBILE_DIR = PROJECT_ROOT / "mobile-client"
ESBUILD_JS = MOBILE_DIR / "node_modules" / "esbuild" / "bin" / "esbuild"
UI_DIR = MOBILE_DIR / "src" / "ui"
CREDENTIALS_TS = UI_DIR / "credentials.ts"

pytestmark = pytest.mark.skipif(
    NODE is None or not ESBUILD_JS.exists() or not CREDENTIALS_TS.exists(),
    reason="node + mobile-client/node_modules are required to bundle the TypeScript module",
)

#: Ask the real rule once and report everything the assertions below need.
HARNESS_TS = """
import {
  identityError,
  loginRequestBody,
  normalizeIdentity,
  validateCredentials,
} from './credentials.ts';

type Kind = 'value' | 'null' | 'undefined';

interface Case {
  name: string;
  userId: string;
  password: string;
  kind: Kind;
  value?: string;
}

const chunks: Buffer[] = [];
process.stdin.on('data', (c) => chunks.push(c as Buffer));
process.stdin.on('end', () => {
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8')) as { cases: Case[] };
  const answers = payload.cases.map((entry) => {
    // The shapes a form can actually hand over: a string the worker typed, the ``null`` a
    // cleared field sometimes yields, and the ``undefined`` of a field never rendered. JSON has
    // no ``undefined``, so the kind carries that case across the boundary.
    const raw: string | null | undefined =
      entry.kind === 'null' ? null : entry.kind === 'undefined' ? undefined : entry.value;

    const credentials = { userId: entry.userId, emailOrPhone: raw, password: entry.password };
    const errors = validateCredentials(credentials);
    const body = loginRequestBody(credentials) as Record<string, unknown>;

    return {
      name: entry.name,
      normalized: normalizeIdentity(raw),
      error: identityError(raw),
      errors,
      has_key: Object.prototype.hasOwnProperty.call(body, 'email_or_phone'),
      is_string: typeof body['email_or_phone'] === 'string',
      email_or_phone: body['email_or_phone'],
      user_id: body['user_id'],
    };
  });
  process.stdout.write(JSON.stringify(answers));
});
"""


@pytest.fixture(scope="module")
def answers(tmp_path_factory) -> dict[str, dict]:
    """Bundle the real module out of its own tree, so its relative imports resolve."""
    bundle = tmp_path_factory.mktemp("mobile_login") / "credentials_harness.mjs"
    # Beside the module it imports, the way the punch-flow suite stages its own harness.
    harness = UI_DIR / "__credentials_harness.ts"
    harness.write_text(HARNESS_TS, encoding="utf-8")
    try:
        completed = subprocess.run(
            [NODE, str(ESBUILD_JS), str(harness), "--bundle", "--platform=node",
             "--format=esm", f"--outfile={bundle}", "--log-level=warning"],
            capture_output=True, text=True, timeout=180,
        )
    finally:
        harness.unlink(missing_ok=True)
    assert completed.returncode == 0, f"esbuild failed:\n{completed.stderr}"

    completed = subprocess.run(
        [NODE, str(bundle)], input=json.dumps({"cases": payload_cases()}),
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, f"harness failed:\n{completed.stderr}"
    return {entry["name"]: entry for entry in json.loads(completed.stdout)}


BASE = {"userId": "480", "password": "correct-horse-battery"}


def payload_cases() -> list[dict]:
    """Every case, carrying the two valid credentials the second one is judged beside.

    A case that names its own ``userId`` or ``password`` overrides the base, which is how the
    two still-required fields get checked with the optional one left blank.
    """
    return [{**BASE, **case} for case in ALL_CASES]

#: Everything the form can hand over for the *second* credential, and what should come of it.
#: ``expect`` is what must reach the server, ``''`` meaning the field was left alone - never
#: ``null``, and never a missing key. ``refused`` is the one case worth a message.
IDENTITY_CASES: list[dict] = [
    # --- absent: none of these is a failure ------------------------------------------------
    {"name": "cleared", "kind": "value", "value": "", "expect": ""},
    {"name": "spaces", "kind": "value", "value": "   ", "expect": ""},
    {"name": "tabs-and-newlines", "kind": "value", "value": "\t\n ", "expect": ""},
    {"name": "null", "kind": "null", "expect": ""},
    {"name": "undefined", "kind": "undefined", "expect": ""},
    # --- recognized: travel as typed, trimmed, case intact ---------------------------------
    {"name": "email", "kind": "value", "value": "admin@siteops.com",
     "expect": "admin@siteops.com"},
    {"name": "email-padded", "kind": "value", "value": "  a.b@sub.example.co  ",
     "expect": "a.b@sub.example.co"},
    {"name": "email-mixed-case", "kind": "value", "value": "Admin@SiteOps.com",
     "expect": "Admin@SiteOps.com"},
    {"name": "phone-international", "kind": "value", "value": "+965 555 0199",
     "expect": "+965 555 0199"},
    {"name": "phone-plain", "kind": "value", "value": "+200000000000",
     "expect": "+200000000000"},
    {"name": "phone-hyphenated", "kind": "value", "value": "555-0199", "expect": "555-0199"},
    # --- unrecognized: the only thing worth a message --------------------------------------
    {"name": "words", "kind": "value", "value": "not an email", "expect": "not an email",
     "refused": True},
    {"name": "no-dot", "kind": "value", "value": "a@b", "expect": "a@b", "refused": True},
    {"name": "no-local-part", "kind": "value", "value": "@b.co", "expect": "@b.co",
     "refused": True},
    {"name": "space-in-address", "kind": "value", "value": "a b@c.co", "expect": "a b@c.co",
     "refused": True},
    {"name": "punctuation", "kind": "value", "value": "!!!", "expect": "!!!", "refused": True},
]

#: The two credentials that are still required, with the optional one left blank beside them.
CREDENTIAL_CASES: list[dict] = [
    {"name": "blank-id", "kind": "value", "value": "", "userId": "   ", "password": "pw"},
    {"name": "blank-password", "kind": "value", "value": "", "userId": "480", "password": ""},
]

ALL_CASES = IDENTITY_CASES + CREDENTIAL_CASES
IDENTITY_IDS = [case["name"] for case in IDENTITY_CASES]


@pytest.mark.parametrize("entry", IDENTITY_CASES, ids=IDENTITY_IDS)
def test_absence_is_not_a_failure_and_junk_is(answers, entry):
    """The field is silent while empty, and complains only about text that matches nothing."""
    answer = answers[entry["name"]]
    if entry.get("refused"):
        assert answer["error"], f"{entry['name']!r} should be refused: {answer}"
        assert answer["errors"].get("email_or_phone") == answer["error"], answer
    else:
        assert answer["error"] == "", f"{entry['name']!r} must not be refused: {answer}"
        assert "email_or_phone" not in answer["errors"], (
            "an acceptable second credential must not produce an error key at all: "
            f"{answer['errors']}"
        )


@pytest.mark.parametrize("entry", IDENTITY_CASES, ids=IDENTITY_IDS)
def test_the_optional_field_never_excuses_the_other_two(answers, entry):
    """Absent means absent: an empty second credential does not silence the ID or the password."""
    answer = answers[entry["name"]]
    assert "user_id" not in answer["errors"], answer
    assert "password" not in answer["errors"], answer


def test_an_absent_value_normalizes_to_nothing_and_a_filled_one_keeps_its_text(answers):
    """The one predicate the field's behaviour turns on: does anything survive normalization?

    A single space, a tab and a newline are all *absent* rather than invalid - which is what
    stops the field from complaining about a value the worker never really entered.
    """
    for name in ("cleared", "spaces", "tabs-and-newlines", "null", "undefined"):
        assert answers[name]["normalized"] == "", name
    for name in ("email", "phone-international", "email-padded"):
        assert answers[name]["normalized"] != "", name


def test_the_payload_carries_an_empty_string_rather_than_null_or_a_missing_key(answers):
    """Both alternatives are a 422, and the empty string is what an account with neither matches.

    ``LoginRequest.email_or_phone`` is a required ``str``, so a missing key and an explicit
    ``null`` are the same refusal - and neither can match an account whose ``email`` and
    ``phone`` are both ``''``.
    """
    for entry in IDENTITY_CASES:
        answer = answers[entry["name"]]
        assert answer["has_key"], f"{entry['name']!r} omitted the field: {answer}"
        assert answer["is_string"], f"{entry['name']!r} sent a non-string: {answer}"
        assert answer["email_or_phone"] is not None, entry["name"]
        assert answer["email_or_phone"] == entry["expect"], entry["name"]


def test_a_recognized_value_travels_unchanged_apart_from_its_whitespace(answers):
    """The server compares with a plain ``=`` against a plain ``TEXT`` column.

    ``users.email`` has no ``COLLATE NOCASE``, so folding the case on the way out would turn a
    correctly typed address into a 401 for every account whose stored address has a capital.
    """
    assert answers["email-mixed-case"]["email_or_phone"] == "Admin@SiteOps.com"
    assert answers["email-padded"]["email_or_phone"] == "a.b@sub.example.co"
    assert answers["phone-international"]["email_or_phone"] == "+965 555 0199"


def test_a_phone_number_is_still_a_credential(answers):
    """Guards the regression an email-only rule would introduce, which no server test can see.

    ``test_walk_up_registration.py`` registers a worker by phone and signs in with that number,
    so a client that refused it would lock out a credential the server accepts.
    """
    for name in ("phone-international", "phone-plain", "phone-hyphenated"):
        assert answers[name]["error"] == "", f"{name} is a credential the server matches on"
        assert "email_or_phone" not in answers[name]["errors"], name


def test_a_blank_identity_does_not_excuse_a_missing_id_or_password(answers):
    """The two fields that are still required keep their own complaints."""
    blank_id = answers["blank-id"]
    blank_password = answers["blank-password"]
    assert "user_id" in blank_id["errors"], blank_id
    assert "password" in blank_password["errors"], blank_password
    # ...and neither of them is blamed on the field that was allowed to be empty.
    assert "email_or_phone" not in blank_id["errors"], blank_id
    assert "email_or_phone" not in blank_password["errors"], blank_password
