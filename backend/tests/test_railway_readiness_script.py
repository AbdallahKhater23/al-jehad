"""The deployment checker's own verdict: what an operator's exit code means.

WHY THIS EXISTS
---------------
``deploy/railway/verify_readiness.py`` is run by hand against the live service, so nothing else in
the suite executes it - and its answer is what somebody acts on the moment they are least able to
second-guess it. Three ways it could lie, each pinned below:

* **"clean" on a body that is not a report.** The endpoint answers 200 while checks are failing
  (degraded is not down), so a script that treats a 200 or an empty check list as success is a
  script that reports a clean deployment during the exact window an operator is watching it. A
  body without a ``checks`` mapping has to be an *unreadable* answer, never an empty one.
* **an advisory read as a fatal, or a fatal as an advisory.** They send somebody to different
  places: an advisory is a feature that is not doing what it says (a phone that will not ring), a
  fatal is a gate that refused to serve. The exit code separates them, and a fatal outranks.
* **the reasons printed for a caller who cannot be trusted with them.** ``detail`` names absolute
  paths and is served by the admin route only; the public body deliberately carries just the
  verdict. The script fetches the reasons *only* with a token, which is the property that keeps a
  probe anybody can run from becoming a map of the host.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from harness import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "deploy" / "railway" / "verify_readiness.py"


@pytest.fixture(scope="module")
def checker():
    """The script loaded by path: it is operator tooling, not an importable package."""
    spec = importlib.util.spec_from_file_location("railway_verify_under_test", SCRIPT)
    assert spec and spec.loader, f"{SCRIPT} is missing"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _body(**checks) -> bytes:
    """A readiness body in the app's own shape: ``checks`` keyed by name, each with ok/tier."""
    return json.dumps(
        {
            "ok": not any(not item["ok"] for item in checks.values() if item["tier"] == "fatal"),
            "degraded": any(not item["ok"] for item in checks.values()),
            "checks": checks,
            "failed_checks": [name for name, item in checks.items() if not item["ok"] and item["tier"] == "fatal"],
            "degraded_checks": [name for name, item in checks.items() if not item["ok"] and item["tier"] != "fatal"],
        }
    ).encode()


def _answers(checker, monkeypatch, public: bytes, *, status: int = 200, admin: bytes | None = None, seen=None):
    """Stand in for the two routes the script can read."""

    def fake_get(url, *, token, timeout):
        if seen is not None:
            seen.append((url, token))
        if url.endswith(checker.ADMIN_PATH):
            if admin is None:
                return 404, b""
            return 200, admin
        return status, public

    monkeypatch.setattr(checker, "_get", fake_get)


def test_a_clean_deployment_exits_zero_and_says_so(checker, monkeypatch, capsys):
    _answers(
        checker,
        monkeypatch,
        _body(
            worker_push_delivery={"ok": True, "tier": "advisory"},
            overtime_close_deferred={"ok": True, "tier": "advisory"},
            schema_current={"ok": True, "tier": "fatal"},
        ),
    )
    assert checker.main(["--origin", "https://example.invalid"]) == checker.CLEAN
    out = capsys.readouterr().out
    assert "no check is failing" in out
    assert "FAIL" not in out


def test_one_failing_advisory_is_not_clean(checker, monkeypatch, capsys):
    """The whole point of the script: `ok: true, degraded: true` is a deployment to fix."""
    _answers(
        checker,
        monkeypatch,
        _body(
            worker_push_delivery={"ok": False, "tier": "advisory"},
            worker_notice_backlog={"ok": True, "tier": "advisory"},
        ),
    )
    assert checker.main(["--origin", "https://example.invalid"]) == checker.ADVISORY_FAILED
    out = capsys.readouterr().out
    assert "worker_push_delivery" in out
    assert "degraded=True" in out


def test_a_fatal_check_outranks_the_advisories(checker, monkeypatch, capsys):
    _answers(
        checker,
        monkeypatch,
        _body(
            database_reachable={"ok": False, "tier": "fatal"},
            worker_push_delivery={"ok": False, "tier": "advisory"},
        ),
        status=503,
    )
    assert checker.main(["--origin", "https://example.invalid"]) == checker.FATAL_FAILED
    out = capsys.readouterr().out
    assert "database_reachable" in out


def test_a_body_that_is_not_a_report_is_never_read_as_clean(checker, monkeypatch, capsys):
    """An edge's HTML page, a moved route, a JSON error: all unreadable, none of them 'no failures'."""
    for payload in (b"<html>502</html>", b"{}", b'{"checks": []}', b'{"checks": {"a": 5}}'):
        _answers(checker, monkeypatch, payload)
        assert checker.main(["--origin", "https://example.invalid"]) == checker.UNREADABLE, payload
    err = capsys.readouterr().err
    assert "is not a readiness report" in err


def test_the_reasons_are_read_from_the_admin_route_only_with_a_token(checker, monkeypatch, capsys):
    """A probe anybody can call must not be handed the internals; an empty token is no token."""
    seen: list[tuple[str, str | None]] = []
    _answers(
        checker,
        monkeypatch,
        _body(worker_push_delivery={"ok": False, "tier": "advisory"}),
        admin=json.dumps(
            {
                "admin": {
                    "checks": [
                        {
                            "name": "worker_push_delivery",
                            "ok": False,
                            "detail": "a worker with the app closed is not notified: no VAPID key pair",
                        }
                    ]
                }
            }
        ).encode(),
        seen=seen,
    )

    assert checker.main(["--origin", "https://example.invalid", "--token", "admin-jwt"]) == checker.ADVISORY_FAILED
    out = capsys.readouterr().out
    assert "no VAPID key pair" in out
    assert [token for url, token in seen if url.endswith(checker.ADMIN_PATH)] == ["admin-jwt"]

    seen.clear()
    assert checker.main(["--origin", "https://example.invalid"]) == checker.ADVISORY_FAILED
    assert not [url for url, _ in seen if url.endswith(checker.ADMIN_PATH)], (
        "without a token the admin route must not be called at all"
    )
    assert "no VAPID key pair" not in capsys.readouterr().out


def test_the_token_defaults_to_the_environment(checker, monkeypatch):
    """So it does not have to appear in a shell history or a process list."""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setenv("ADMIN_TOKEN", "from-the-environment")
    _answers(
        checker,
        monkeypatch,
        _body(worker_push_delivery={"ok": False, "tier": "advisory"}),
        admin=b'{"admin": {"checks": []}}',
        seen=seen,
    )
    assert checker.main(["--origin", "https://example.invalid"]) == checker.ADVISORY_FAILED
    assert [token for url, token in seen if url.endswith(checker.ADMIN_PATH)] == ["from-the-environment"]


def test_the_default_origin_is_the_deployment_the_worker_points_at(checker):
    """One command with nothing to mistype - and it has to be the address the edge proxies to."""
    text = (PROJECT_ROOT / "deploy" / "cloudflare" / "wrangler.toml").read_text(encoding="utf-8")
    assert checker.DEFAULT_ORIGIN in text, (
        "the checker's default origin is not the one the Worker proxies to: re-verifying would "
        "run against a deployment nobody uses"
    )
