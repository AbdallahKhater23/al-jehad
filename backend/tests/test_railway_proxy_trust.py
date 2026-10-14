"""Railway's edge proxy: who may set ``X-Forwarded-*``, and what that changes.

WHY THIS SUITE EXISTS
---------------------
The deployment terminates TLS at Railway's edge and forwards plain HTTP to the container, so the
peer this process sees is the *edge*, not the worker. With the trust list left at loopback only,
three things fail silently and simultaneously, and each one was measured on this deployment:

1. **uuicorn never rewrites the client address**, so ``slowapi`` counts every browser in the
   country against one bucket - the punch endpoint's 15/minute answering a whole site, and later
   workers refused by a limit none of them had reached.
2. **HSTS is never emitted**: the network gate believes ``X-Forwarded-Proto`` only from a trusted
   peer, so a deployment that is entirely HTTPS sends no response saying so.
3. **Every audit row records the edge's address as the actor.**

The fix is a trust list that names the platform's *private* networks rather than ``*``: a request
from the public internet arrives through that network and cannot present itself as a peer on it,
so the two properties above are bought without making ``X-Forwarded-For`` client-controlled. The
tests below pin both halves - the headers becoming usable, and the spoofing case *staying*
refused.

Read with ``test_network_hardening.py``: that suite owns the gate's policy logic, this one owns
the deployment's configuration of it and the rate limiter that reads the same resolution.
"""

from __future__ import annotations

import contextlib

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

import config
import netguard
import rate_limit
import serve
from harness import ADMIN, bearer

#: Railway's private network. The edge connects from here, so this is the peer a real request has.
PLATFORM_EDGE = "100.64.0.7"
PLATFORM_RANGE = "100.64.0.0/10"
#: A worker's phone, or a scanner: a public address, which is what the edge *reports* per client.
CLIENT = "203.0.113.9"
#: The address the admin allowlist is expected to contain in the tests that use one.
IN_ALLOWLIST = "10.1.2.3"
LAN = "10.0.0.0/8"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def peer_client(app_module, peer: str, *, https: bool = False):
    """A client whose *peer address* is known - the input the gate and the limiter both read."""
    scheme = "https" if https else "http"
    instance = TestClient(app_module.app, base_url=f"{scheme}://testserver", client=(peer, 51234))
    try:
        yield instance
    finally:
        instance.close()


def fake_request(peer: str | None, forwarded: str | None = None) -> Request:
    """A ``Request`` with a known peer and ``X-Forwarded-For``, for the key function.

    Built directly rather than driven through the app, because ``rate_limit.client_key`` is
    called by ``slowapi`` with whatever request the route is handling - including one whose
    ``client`` may be ``None`` - and the unit is the resolution, not the routing.
    """
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/attendance/verify",
        "raw_path": b"/api/v1/attendance/verify",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": (peer, 51234) if peer else None,
    }
    return Request(scope)


def install(monkeypatch, **kwargs) -> netguard.NetworkPolicy:
    """Put a policy in force for one test; monkeypatch restores the deployment's afterwards."""
    active = netguard.build_policy(**kwargs)
    monkeypatch.setattr(netguard, "_policy", active)
    return active


@pytest.fixture(autouse=True)
def _isolate_netguard_state(monkeypatch):
    """The misuse counter and the log throttle are process-global; tests must not leak them."""
    monkeypatch.setattr(
        netguard, "_forward_misuse", {"count": 0, "last_peer": None, "last_at": None}
    )
    monkeypatch.setattr(netguard, "_THROTTLE", netguard._Throttle())
    yield


# ---------------------------------------------------------------------------
# 1. The configuration default follows the platform
# ---------------------------------------------------------------------------
def test_a_laptop_trusts_loopback_and_nothing_else(monkeypatch):
    """The pre-hardening default is kept where it is correct: a local run, and a LAN address is
    never believed. Widening this on a development laptop is how a spoofed header starts working
    in the one place nobody is watching the logs."""
    for name in config.PLATFORM_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    assert config.default_trusted_proxies() == list(config.LOOPBACK_TRUSTED_PROXIES)
    assert "10.0.0.0/8" not in config.default_trusted_proxies()


def test_a_managed_platform_trusts_its_own_private_networks(monkeypatch):
    """On Railway the edge *is* the peer, so the private networks it lives on are trusted."""
    for name in config.PLATFORM_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    assert config.on_a_managed_platform() is True
    trusted = config.default_trusted_proxies()
    # Loopback is still there (the container's own health probe and a sidecar), plus the ranges a
    # platform edge connects from. CGNAT is the one Railway's private network actually uses.
    for entry in config.LOOPBACK_TRUSTED_PROXIES:
        assert entry in trusted
    for entry in ("10.0.0.0/8", "172.16.0.0/12", "100.64.0.0/10", "fc00::/7"):
        assert entry in trusted, f"{entry} is missing from the platform default: {trusted}"
    # And never the wildcard: that is the setting this default exists to avoid needing, because
    # it makes X-Forwarded-For client-controlled on every connection.
    assert "*" not in trusted


def test_the_setting_itself_picks_up_the_platform_default(monkeypatch):
    """``build_settings`` is the path the server, the tools and the tests all take."""
    for name in config.PLATFORM_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)

    assert "100.64.0.0/10" not in config.build_settings().trusted_proxies

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert "100.64.0.0/10" in config.build_settings().trusted_proxies


def test_an_explicit_setting_still_wins(monkeypatch):
    """An operator with a proxy this default does not know about names it, and gets exactly it."""
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("TRUSTED_PROXIES", "192.0.2.0/24")
    assert config.build_settings().trusted_proxies == ["192.0.2.0/24"]


def test_uvicorn_and_the_gate_are_handed_the_same_list(monkeypatch):
    """Two consumers, one list. If they disagreed, uvicorn would stop rewriting the client
    address while the gate still expected the header it could no longer see - invisible, and
    expensive: one shared rate-limit bucket and an admin gate that refuses its administrator."""
    for name in config.PLATFORM_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    forwarded = serve.trusted_proxies_for_uvicorn()
    assert "100.64.0.0/10" in forwarded, forwarded
    assert forwarded == ",".join(config.build_settings().trusted_proxies)


# ---------------------------------------------------------------------------
# 2. The rate limiter keys on the client, not on the edge
# ---------------------------------------------------------------------------
def test_a_trusted_proxy_may_name_the_client_the_limit_is_counted_against(monkeypatch):
    install(monkeypatch, trusted_proxies=["127.0.0.1/32"])
    assert rate_limit.client_key(fake_request("127.0.0.1", CLIENT)) == CLIENT
    # A chain is walked from the right: the hop nearest us wrote last.
    assert rate_limit.client_key(fake_request("127.0.0.1", f"{CLIENT}, 127.0.0.1")) == CLIENT


def test_the_platform_edge_resolves_the_client_for_the_limiter(monkeypatch):
    """The production shape: the peer is the edge, the client arrives in the header."""
    install(monkeypatch, trusted_proxies=[PLATFORM_RANGE])
    assert rate_limit.client_key(fake_request(PLATFORM_EDGE, CLIENT)) == CLIENT
    # Two phones behind the *same* edge get two buckets - which is the bug this fixes.
    assert rate_limit.client_key(fake_request(PLATFORM_EDGE, "198.51.100.4")) != rate_limit.client_key(
        fake_request(PLATFORM_EDGE, CLIENT)
    )


def test_an_untrusted_peer_cannot_move_its_own_bucket_by_typing_a_header(monkeypatch):
    """The spoofing case at the limiter: the header is ignored and the *peer* is counted.

    Ignored rather than refused, because this is a rate limit and not an authorization gate -
    what matters is that rotating ``X-Forwarded-For`` buys a fresh bucket from nobody.
    """
    install(monkeypatch, trusted_proxies=["127.0.0.1/32"])
    assert rate_limit.client_key(fake_request(CLIENT, IN_ALLOWLIST)) == CLIENT
    assert rate_limit.client_key(fake_request(CLIENT, "198.51.100.4")) == CLIENT


def test_a_direct_connection_still_keys_on_its_own_address(monkeypatch):
    install(monkeypatch)
    assert rate_limit.client_key(fake_request(CLIENT)) == CLIENT
    # A missing peer is not a crash: slowapi's key function has to answer with a string.
    assert rate_limit.client_key(fake_request(None)) == "unknown"


def test_the_limiter_is_keyed_by_the_resolved_client():
    """``get_remote_address`` is ``request.client.host`` - the edge's address behind a proxy, and
    one bucket for the whole deployment. A regression guard, because putting it back is a
    one-line "simplification" that reads as correct.

    Asserted on the limiter's own key function rather than on the module's source: the module
    *names* ``get_remote_address`` in the comment explaining why it is not used, and a text scan
    that fails on its own documentation is a test nobody keeps.
    """
    wired = getattr(rate_limit.limiter, "_key_func", None)
    if wired is None:  # a future slowapi spelling; never silently skip the assertion
        wired = getattr(rate_limit.limiter, "key_func", None)
    assert wired is rate_limit.client_key, (
        "the limiter is not keyed by rate_limit.client_key, so every per-IP limit behind the "
        "edge is one shared bucket again"
    )
    assert "get_remote_address" not in getattr(rate_limit.limiter, "__dict__", {})


# ---------------------------------------------------------------------------
# 3. HSTS is emitted where TLS really is
# ---------------------------------------------------------------------------
def test_the_edge_may_report_tls_and_the_deployment_answers_with_hsts(client, monkeypatch, app_module):
    """``X-Forwarded-Proto: https`` from the platform's edge is evidence, and HSTS follows it."""
    install(monkeypatch, trusted_proxies=[PLATFORM_RANGE], hsts_max_age_seconds=600)
    with peer_client(app_module, PLATFORM_EDGE) as through_edge:
        response = through_edge.get("/api/v1/status", headers={"X-Forwarded-Proto": "https"})
    assert response.headers.get("strict-transport-security") == "max-age=600; includeSubDomains"


def test_plain_http_through_the_edge_carries_no_hsts(client, monkeypatch, app_module):
    """Pinning a host that is served over HTTP is how a site LAN loses its punch page, so the
    header follows the *protocol* and not merely the presence of a trusted proxy."""
    install(monkeypatch, trusted_proxies=[PLATFORM_RANGE], hsts_max_age_seconds=600)
    with peer_client(app_module, PLATFORM_EDGE) as through_edge:
        response = through_edge.get("/api/v1/status")
    assert "strict-transport-security" not in response.headers


def test_a_public_peer_cannot_claim_tls(client, monkeypatch, app_module):
    """The same header from an address nobody declared is worth nothing."""
    install(monkeypatch, trusted_proxies=[PLATFORM_RANGE], hsts_max_age_seconds=600)
    with peer_client(app_module, CLIENT) as direct:
        response = direct.get("/api/v1/status", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in response.headers


# ---------------------------------------------------------------------------
# 4. Widening to the private ranges is not a spoofing hole
# ---------------------------------------------------------------------------
def test_a_public_client_still_cannot_forge_its_way_into_an_allowlist(client, monkeypatch, app_module):
    """The property the platform default must not cost.

    With the platform ranges trusted, a peer *inside* them is believed - the edge. A peer
    *outside* them sending an allowlisted address is still refused ``proxy_not_trusted``, which
    is the whole reason the default names networks instead of ``*``.
    """
    install(monkeypatch, trusted_proxies=[PLATFORM_RANGE], admin_networks=[LAN])
    with peer_client(app_module, CLIENT) as attacker:
        refused = attacker.get(
            "/api/v1/admin/users",
            headers={"X-Forwarded-For": IN_ALLOWLIST, **bearer(ADMIN)},
        )
    assert refused.status_code == 403
    assert refused.json()["error_code"] == netguard.REASON_PROXY

    # ...and the same request arriving the way the edge really sends it is served, so the fix
    # did not simply close the door it was meant to open.
    with peer_client(app_module, PLATFORM_EDGE) as through_edge:
        served = through_edge.get(
            "/api/v1/admin/users",
            headers={"X-Forwarded-For": IN_ALLOWLIST, **bearer(ADMIN)},
        )
    assert served.status_code == 200, served.text[:200]


# ---------------------------------------------------------------------------
# 5. A cross-origin DELETE is a request the browser is allowed to send
# ---------------------------------------------------------------------------
def test_delete_is_advertised_in_a_preflight(client, monkeypatch):
    """The app calls the deletion endpoint cross-origin (its page is ``https://localhost`` in the
    WebView), and a verb the preflight does not name is a request the browser refuses to send -
    which the person experiences as a button that does nothing."""
    assert "DELETE" in netguard.DEFAULT_ALLOWED_METHODS
    install(monkeypatch, worker_origins=["https://app.example.com"])
    response = client.options(
        "/api/v1/workers/me",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200, response.text[:200]
    assert "DELETE" in response.headers["access-control-allow-methods"]
