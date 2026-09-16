"""The anonymous readiness probe must report a verdict, not a map of the host.

``/api/v1/readiness`` is called by clients that hold no credential -- a load
balancer, a ``curl -f`` in a deploy script, an uptime monitor -- so whatever it
returns is world-readable. Every check writes its ``detail`` for an operator
reading a startup log, and those strings name absolute paths: the database the
process actually opened, the backup directory, the liveness model, and the
``OSError`` text from the biometric directories.

The rule these tests pin: the public body carries the verdict, and per check only
``ok``/``tier``. The full ``detail``/``value`` and the app/schema/fingerprint
inventory stay behind ``admin_only`` on ``/api/v1/admin/readiness``.
"""

from __future__ import annotations

import re

import pytest
from harness import ADMIN, TMP_ROOT, WORKER, bearer

PUBLIC = "/api/v1/readiness"
ADMIN_ROUTE = "/api/v1/admin/readiness"

#: An absolute path -- POSIX or Windows -- or a sqlite URI. Deliberately broad:
#: the test must fail if *any* check detail leaks back into the public body, not
#: only the ones that leak today.
_PATH_LIKE = re.compile(
    r"""
    [A-Za-z]:[\\/]                     # C:\ or C:/
    | file:[^\s\"']*                   # file:/tmp/times.db?mode=ro
    | (?:^|[\s\"'(:=])/(?:[^/\s\"']+/)+  # /srv/app/backend, /tmp/x/y
    """,
    re.VERBOSE,
)


def _strings(payload):
    """Every string in a JSON body, keys included."""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield key
            yield from _strings(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _strings(item)


def _path_like(payload) -> list[str]:
    return [text for text in _strings(payload) if _PATH_LIKE.search(text)]


@pytest.mark.regression
def test_the_public_readiness_body_carries_no_filesystem_paths(client):
    """The whole point: no path the process knows may reach an anonymous caller."""
    response = client.get(PUBLIC)
    assert response.status_code in (200, 503), response.text[:300]

    offenders = _path_like(response.json())
    assert not offenders, (
        "SECURITY GAP: the anonymous readiness probe discloses filesystem paths: "
        f"{offenders[:5]}. The verdict is public; the detail belongs to "
        "/api/v1/admin/readiness."
    )


@pytest.mark.regression
def test_the_public_body_does_not_name_the_files_it_works_with(client):
    """Belt and braces: a bare ``times.db``/directory name is disclosure too."""
    text = client.get(PUBLIC).text
    for internal in (str(TMP_ROOT), "times.db", "worker_photos", "local_references"):
        assert internal not in text, f"the public readiness body names {internal!r}"


@pytest.mark.regression
def test_the_public_body_is_the_verdict_and_nothing_else(client):
    """Shape guard: a new check with a verbose ``detail`` cannot widen this route."""
    body = client.get(PUBLIC).json()
    assert set(body) == {"ok", "degraded", "checks", "failed_checks", "degraded_checks"}, body.keys()
    assert isinstance(body["ok"], bool)
    assert body["checks"], "the verdict is useless without the per-check ok/tier"

    for name, check in body["checks"].items():
        assert set(check) == {"ok", "tier"}, f"{name} exposes {sorted(check)}"
        assert isinstance(check["ok"], bool)
        assert check["tier"] in {"fatal", "repairable", "advisory"}
    assert "detail" not in client.get(PUBLIC).text


@pytest.mark.regression
def test_the_full_check_details_still_reach_the_admin_route(client):
    """Hardening the public route must not blind the operator surface."""
    response = client.get(ADMIN_ROUTE, headers=bearer(ADMIN))
    assert response.status_code in (200, 503), response.text[:300]

    checks = response.json()["admin"]["checks"]
    assert checks, "the admin surface must still expose every check"
    for check in checks:
        assert set(check) >= {"name", "tier", "ok", "detail", "value"}, check

    # The paths the public route hides are exactly what an admin can still see.
    assert _path_like(response.json()["admin"]), (
        "the admin route lost the detail it needs: no check reported a path or value"
    )
    assert "database_path" in response.json()["admin"]


@pytest.mark.regression
def test_the_detailed_readiness_surface_is_admin_only(client):
    assert client.get(ADMIN_ROUTE).status_code == 401
    assert client.get(ADMIN_ROUTE, headers=bearer(WORKER)).status_code == 403
