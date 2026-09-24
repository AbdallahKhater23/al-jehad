"""No credential may be tracked again - the failure that put a head-admin token in history.

WHY THIS EXISTS
---------------
``backend/_live_token.txt`` was a scratch file holding a live ``head_admin`` JWT, written by hand
while probing a deployed service. It was swept into a commit by a ``git add -A`` beside unrelated
work and pushed: a session credential for the account that can do anything, in the repository's
history, where deleting the file afterwards does not reach it. The commit that carried it had to be
rewritten and every commit after it changed identity - which is the cost this file exists to avoid
paying twice.

SO THERE ARE TWO GUARDS, AND THEY WORK DIFFERENTLY
--------------------------------------------------
* **``.gitignore``** stops the file being *staged*, which is what actually happened: the operator
  did not mean to commit a token, they meant to commit the other eleven files. Ignoring it means
  the next ``git add -A`` cannot pick it up by accident.
* **this file** stops it being *tracked*, which is the state ``.gitignore`` cannot fix. A tracked
  path is exempt from ignore rules - that is the whole reason the leaked file was committable at
  all - so once something credential-shaped is in the index, no ignore rule will save it and only a
  test will say so before it is pushed.

WHAT IT FLAGS, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------
Two shapes, and both are narrow on purpose:

1. **A file whose *name* is a credential scratch file** - ``_live_token.txt``, ``*.jwt``, ``.env``,
   ``*credentials.txt``. Narrow because four suites in this repository legitimately have ``token``,
   ``credentials`` or ``secrets`` in their *names* (``test_auth_token_contract``,
   ``test_frontend_credentials``, ``test_admin_credentials_roster``, ``test_secrets_and_surfaces``)
   and a guard that failed on the suite about token contracts would be deleted by the next person
   to trip over it. Source extensions are therefore excluded by rule, not by allowlist.
2. **A file whose *content* contains a signed token** - three base64url segments whose payload
   decodes to JSON carrying an identity claim. Decode-verified rather than pattern-matched, because
   a regex alone would flag the *fixture* a future test writes to exercise a malformed-token path,
   and because ``eyJ`` is simply base64 for ``{"``:/ a file that mentions the shape in prose must
   not fail a build.

Neither guard reads a credential, sends one anywhere, or needs a network. Both read the *working
tree of tracked files*, which is deliberately earlier than the commit: the point is to fail before
somebody pushes, not after.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest

import harness

# ---------------------------------------------------------------------------
# the two predicates, as pure functions so they can be pinned
# ---------------------------------------------------------------------------
#: Source and documentation files are never a credential scratch file, whatever they are called.
#: This is the rule that keeps ``test_auth_token_contract.py`` out of the net.
SOURCE_EXTENSIONS = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".ts", ".html", ".css", ".md", ".rst", ".txt.md", ".lock"}
)

#: Data-shaped names that hold a secret rather than describe one. Anchored on the basename, and
#: matched case-insensitively; ``variables.env`` is included because that is the name this project
#: actually uses for a file of live keys (see ``.gitignore``).
SCRATCH_CREDENTIAL_NAMES = re.compile(
    r"""^(
        _live[^/]*\.(?:txt|json)          # the name that leaked
      | [^/]*_tokens?\.(?:txt|json)       # a saved bearer token
      | [^/]*[._-]tokens?\.(?:txt|json)
      | [^/]*\.jwt                        # a serialized token
      | [^/]*credentials?\.(?:txt|json|csv)
      | [^/]*secrets?\.(?:txt|json|ya?ml)
      | [^/]*\.env                        # any *.env: .env, variables.env
      | \.env\.[^/]*                      # .env.local, .env.production
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

#: Names that *look* like the above and are configuration, not credentials: the checked-in example
#: a reader copies. Excluded by name because "the template is not the secret" is a fact about these
#: exact files, and every repository has them.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

#: A JWT: base64url header, payload, signature. Deliberately loose - the decode below is what
#: decides whether a match is a credential or a coincidence.
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

#: Claims that make a decoded payload an identity rather than a payload-shaped blob.
IDENTITY_CLAIMS = frozenset({"exp", "sub", "uid", "user_id", "role", "iat", "iss", "aud"})


def looks_like_a_scratch_credential(path: str) -> bool:
    """Whether a tracked path is a credential *file* rather than code that talks about one."""
    name = Path(path).name
    if name.lower() in ENV_TEMPLATES:
        return False
    if Path(name).suffix.lower() in SOURCE_EXTENSIONS and not name.endswith(".env"):
        return False
    return bool(SCRATCH_CREDENTIAL_NAMES.match(name))


def _decode_segment(segment: str) -> dict | None:
    """A base64url JWT payload as a dict, or ``None`` if it is not one."""
    padded = segment + "=" * (-len(segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - not-a-JWT is the common case, not an error
        return None
    return payload if isinstance(payload, dict) else None


def is_a_signed_token(candidate: str) -> bool:
    """Whether this string is a *token*, not merely token-shaped.

    The payload has to decode to a JSON object carrying at least one identity claim. This is the
    step that keeps a fixture written to exercise a *broken* token - or a line of documentation
    quoting one - from failing the build, while still catching every real JWT, because the claims
    are what make it one.
    """
    parts = candidate.split(".")
    if len(parts) != 3:
        return False
    payload = _decode_segment(parts[1])
    return bool(payload) and bool(IDENTITY_CLAIMS.intersection(payload))


def signed_tokens_in(text: str) -> list[str]:
    """Every decode-verified token in a blob of text."""
    return [match for match in JWT_SHAPE.findall(text or "") if is_a_signed_token(match)]


# ---------------------------------------------------------------------------
# reading the repository
# ---------------------------------------------------------------------------
def _git(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=str(harness.PROJECT_ROOT),
        capture_output=True,
        text=True,
        errors="replace",
    )


@pytest.fixture(scope="module")
def repository() -> None:
    """Skip rather than fail where there is no git or no checkout to read."""
    try:
        inside = _git("rev-parse", "--is-inside-work-tree")
    except OSError as missing:  # pragma: no cover - no git on the machine
        pytest.skip(f"git is not installed: {missing}")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip("not a git checkout, so nothing is tracked to inspect")


def tracked_files() -> list[str]:
    """Every path git tracks. ``-z`` because a path may contain anything but NUL."""
    listing = _git("ls-files", "-z")
    assert listing.returncode == 0, listing.stderr
    return [name for name in listing.stdout.split("\0") if name]


def tracked_text_matches(pattern: str) -> list[str]:
    """Tracked files whose *working-tree* content matches. ``-I`` skips binaries.

    The working tree rather than the index, deliberately: the guard should fire when a token is
    written into a tracked file, which is before ``git add`` and well before a push.
    """
    found = _git("grep", "-I", "-l", "-E", pattern, "--", ".")
    if found.returncode not in (0, 1):  # 1 is simply "no match"
        pytest.skip(f"git grep could not run here: {found.stderr.strip()}")
    return [line.strip() for line in found.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. nothing credential-shaped is tracked
# ---------------------------------------------------------------------------
def test_no_tracked_file_is_a_scratch_credential_file(repository):
    """The name half. This is the shape that leaked."""
    offenders = [path for path in tracked_files() if looks_like_a_scratch_credential(path)]
    assert offenders == [], (
        f"a credential scratch file is tracked: {offenders}. A tracked path is exempt from "
        f".gitignore, so no ignore rule can save it now - remove it from the index and from "
        f"history before pushing. `.gitignore` is what stops the next one being staged."
    )


def test_no_tracked_file_contains_a_signed_token(repository):
    """The content half, and the one that survives a rename.

    A token pasted into a file with an innocent name is not caught by any filename rule, which is
    why the content is checked too. Decode-verified, so a fixture for a malformed token or a line
    of prose does not fail a build for the wrong reason.
    """
    offenders = []
    for path in tracked_text_matches(JWT_SHAPE.pattern):
        try:
            text = (harness.PROJECT_ROOT / path).read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - listed but unreadable
            continue
        tokens = signed_tokens_in(text)
        if tokens:
            offenders.append(f"{path} ({len(tokens)} token(s))")
    assert offenders == [], (
        f"a signed token is committed in: {offenders}. Delete it, remove it from history, and "
        f"treat it as leaked - a token in a repository is a credential that has been disclosed, "
        f"whatever the repository's visibility."
    )


def test_the_scratch_file_that_leaked_is_ignored(repository):
    """The prevention half: ``git add -A`` must not be able to pick it up again."""
    text = (harness.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/_live_token.txt" in text, (
        "the ignore rule for the scratch token file is gone; without it the next hand-written "
        "token is one `git add -A` away from being committed beside unrelated work, which is "
        "exactly how the last one got in"
    )


# ---------------------------------------------------------------------------
# 2. the guard itself is pinned, both ways
# ---------------------------------------------------------------------------
def _synthetic_token() -> str:
    """A real-shaped JWT payload, built here rather than copied from one that leaked."""

    def segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        [
            segment({"alg": "HS256", "typ": "JWT"}),
            segment({"sub": "5000", "role": "head_admin", "iat": 1790202191, "exp": 1790245391}),
            "not-a-real-signature",
        ]
    )


def test_the_guard_catches_the_file_that_actually_leaked():
    """The self-test. A guard that cannot fail is worse than none, because it reads as cover."""
    assert looks_like_a_scratch_credential("backend/_live_token.txt")
    assert looks_like_a_scratch_credential("_live_token.txt")
    assert looks_like_a_scratch_credential("deploy/railway/variables.env")
    assert looks_like_a_scratch_credential("session.jwt")
    assert looks_like_a_scratch_credential("prod-credentials.txt")


def test_the_guard_catches_a_signed_token_in_an_innocently_named_file():
    """The content half, on a name no filename rule would question."""
    token = _synthetic_token()
    assert is_a_signed_token(token), "the guard no longer recognises a real-shaped JWT"
    assert signed_tokens_in(f"notes: pasted the admin token {token} to test it") == [token]
    assert not looks_like_a_scratch_credential("backend/notes.txt"), (
        "premise: only the content rule can catch this one"
    )


def test_the_guard_does_not_flag_the_suites_that_talk_about_tokens():
    """The false-positive pin, and the reason this file survives review.

    Four suites here are *about* tokens and credentials. A guard that failed on them would be
    deleted rather than fixed, so the negative is asserted next to the positive.
    """
    for path in (
        "backend/tests/test_auth_token_contract.py",
        "backend/tests/test_admin_credentials_roster.py",
        "backend/tests/test_frontend_credentials.py",
        "backend/tests/test_secrets_and_surfaces.py",
        "backend/tests/test_no_committed_credentials.py",
        "deploy/railway/variables.env.example",
    ):
        assert not looks_like_a_scratch_credential(path), path

    assert not is_a_signed_token("eyJhbGciOiJIUzI1NiJ9.bm90LWpzb24.signature"), (
        "a token-shaped string whose payload is not JSON must not be reported as a credential"
    )
    assert signed_tokens_in("the token starts with eyJ and has two dots") == []
