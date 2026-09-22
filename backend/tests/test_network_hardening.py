"""Network policy: which origins may read what, who may reach the admin surface, and headers.

WHY THIS SUITE EXISTS
---------------------
Everything here is a control that looks correct in a config file and fails open in the
deployment, so each claim is pinned by driving a request rather than by reading a setting:

1. **The two origin classes are actually two.** A worker origin may read the worker API and
   must not be granted CORS on ``/admin/*``; an admin origin may read both. The failure this
   guards against is a single merged list - which is what the app had before this change -
   where any origin allowed to fetch a punch was equally free to call ``/api/v1/admin/users``.
2. **``X-Forwarded-For`` is believed only from a trusted proxy.** The spoofing case is the
   important one: a peer that is *not* a declared proxy sends ``X-Forwarded-For:`` an address
   that *is* in the allowlist. A middleware that reads the header whenever it is present
   hands the admin API to anybody who can type a header, so that request has to be refused
   with ``proxy_not_trusted`` rather than served.
3. **The gate fails closed.** An allowlist that does not parse, a client outside it, a client
   whose address cannot be determined - all refused. A gate that "cannot match" must never
   mean "let them through", and a gate that refuses is asserted not to have reached the
   handler that writes to the database.
4. **Headers are per content type.** A JSON response gets a policy that denies everything; a
   document gets one that names what the pages actually load. HSTS is sent only where TLS
   really is, because pinning a host served over plain HTTP locks workers out of the app.
5. **Nothing here depends on the browser.** A request carrying an unknown ``Origin`` is still
   served - the middleware withholds the headers that let a browser *read* the answer. That is
   what CORS is; the authorization is the bearer token, and it is asserted here so that nobody
   later "hardens" this into refusing legitimate non-browser clients.

The policies are installed by assigning ``netguard._policy``: the middleware resolves the
policy per request (see ``netguard.install``), so a test can exercise a deployment
configuration without rebuilding the application.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import netguard
import telemetry
from config import settings
from harness import ADMIN, WORKER, bearer, db_scalar

WORKER_ORIGIN = "https://app.example.com"
ADMIN_ORIGIN = "https://console.example.com"
IN_ALLOWLIST = "10.1.2.3"
OUTSIDE = "203.0.113.9"
TRUSTED_PROXY = "127.0.0.1"
LAN = "10.0.0.0/8"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def peer_client(app_module, peer: str, *, https: bool = False):
    """A client whose *peer address* is known, which is what the gate actually reads.

    A separate client per peer rather than a header, because ``X-Forwarded-For`` is the thing
    under test: the peer has to be a real input, not something the test also controls through
    the same header. Used without the context-manager protocol on purpose - that is what runs
    lifespan events, and re-running the startup gate and the timers once per peer would only
    re-initialise the database the harness has already seeded for this test.
    """
    scheme = "https" if https else "http"
    instance = TestClient(app_module.app, base_url=f"{scheme}://testserver", client=(peer, 51234))
    try:
        yield instance
    finally:
        instance.close()


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


def refusal_counter(reason: str) -> float:
    """The value of one refusal series.

    Read straight off the client library's counter rather than through a scrape: this is an
    assertion about whether the code counted something, and routing it through the exposition
    format would test the parser as well.

    Two counter shapes exist, and the suite must run against both:

    * **the real library** - ``labels(reason=...)`` returns a *child* object holding that
      series' value, read from its ``_value``;
    * **telemetry's no-op stand-in** - what the module serves when ``prometheus_client`` is
      not installed (the optional-extra contract). ``labels()`` returns the same object for
      every reason and ``inc()`` records nothing, so there is no per-series value to read.
      The increment tests then assert only the *behavioural* half: a refused request, with
      the reason the policy named. The counting half is pinned by
      ``test_metrics.py::test_every_helper_is_silent_when_the_library_is_missing`` instead,
      which is where the degradation contract lives.
    """
    child = telemetry.NETGUARD_REFUSALS.labels(reason=reason)
    if telemetry.AVAILABLE:
        return float(child._value.get())
    return float(_STAND_IN_COUNTS.get(reason, 0))


#: Read side of the stand-in: ``netguard.count_refusal`` records the reason here when the
#: library is absent (see ``telemetry.count_netguard_refusal``), so the suite can still
#: assert the *pair* "this request was refused, for this reason" - just not from the counter,
#: because there is nothing inside it. Keyed by reason like the real counter's child objects.
_STAND_IN_COUNTS: dict[str, int] = {}


def _record_stand_in(**labels) -> None:
    """The stand-in's receiver: one increment of the series these labels name."""
    key = ",".join(f"{value}" for value in labels.values())
    _STAND_IN_COUNTS[key] = _STAND_IN_COUNTS.get(key, 0) + 1


@pytest.fixture(autouse=True)
def _reset_stand_in_counts():
    """Wire the stand-in's read side around each test, like the counter's own isolation."""
    _STAND_IN_COUNTS.clear()
    if hasattr(telemetry.Counter, "record"):
        telemetry.Counter.record = _record_stand_in
    yield
    if hasattr(telemetry.Counter, "record"):
        telemetry.Counter.record = None
    _STAND_IN_COUNTS.clear()


def check_named(name: str):
    import readiness

    for check in readiness.run_checks(None)[0]:
        if check.name == name:
            return check
    raise AssertionError(f"no readiness check named {name}")


# ---------------------------------------------------------------------------
# origins: two classes, two trust levels
# ---------------------------------------------------------------------------
def test_a_worker_origin_may_read_a_worker_route(client, monkeypatch):
    install(monkeypatch, worker_origins=[WORKER_ORIGIN], admin_origins=[ADMIN_ORIGIN])
    response = client.get("/api/v1/status", headers={"Origin": WORKER_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WORKER_ORIGIN
    # Caches must key on the origin, or one origin's grant is served against another's request.
    assert "origin" in response.headers.get("vary", "").lower()


def test_a_worker_origin_is_not_granted_cors_on_an_admin_route(client, monkeypatch):
    """The point of classifying origins: a punch client is not an administrator's console."""
    install(monkeypatch, worker_origins=[WORKER_ORIGIN], admin_origins=[ADMIN_ORIGIN])
    response = client.get("/api/v1/admin/users", headers={"Origin": WORKER_ORIGIN, **bearer(ADMIN)})
    # Served - CORS is enforced by the browser, not by withholding a response - but with no
    # grant, so a page on the worker origin cannot read a byte of that body.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_an_admin_origin_may_read_both_surfaces(client, monkeypatch):
    install(monkeypatch, worker_origins=[WORKER_ORIGIN], admin_origins=[ADMIN_ORIGIN])
    worker_side = client.get("/api/v1/status", headers={"Origin": ADMIN_ORIGIN})
    admin_side = client.get("/api/v1/admin/users", headers={"Origin": ADMIN_ORIGIN, **bearer(ADMIN)})
    assert worker_side.headers.get("access-control-allow-origin") == ADMIN_ORIGIN
    assert admin_side.headers.get("access-control-allow-origin") == ADMIN_ORIGIN


def test_an_unknown_origin_is_served_but_granted_nothing(client, monkeypatch):
    """Deliberate: non-browser clients exist, and CORS was never an authorization control."""
    install(monkeypatch, worker_origins=[WORKER_ORIGIN], admin_origins=[ADMIN_ORIGIN])
    response = client.get("/api/v1/status", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200, "a script that happens to send Origin is not an attacker"
    assert "access-control-allow-origin" not in response.headers


def test_no_origin_header_means_no_cors_headers_at_all(client, monkeypatch):
    install(monkeypatch, worker_origins=[WORKER_ORIGIN])
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert not any(key.startswith("access-control-") for key in response.headers)
    assert "origin" not in response.headers.get("vary", "").lower()


def test_a_subdomain_wildcard_matches_subdomains_and_not_the_bare_domain(client, monkeypatch):
    install(monkeypatch, worker_origins=["https://*.example.com"], admin_origins=[ADMIN_ORIGIN])
    for allowed in ("https://app.example.com", "https://a.b.example.com"):
        assert client.get("/api/v1/status", headers={"Origin": allowed}).headers.get(
            "access-control-allow-origin"
        ) == allowed
    # The bare domain is a different origin, and so is a lookalike suffix.
    for refused in ("https://example.com", "https://notexample.com", "https://evil.example.net"):
        assert "access-control-allow-origin" not in client.get(
            "/api/v1/status", headers={"Origin": refused}
        ).headers, refused


def test_origin_matching_is_normalised_but_the_grant_echoes_what_was_sent(client, monkeypatch):
    """A config file is written by a person; a browser sends its own spelling of the origin."""
    install(monkeypatch, worker_origins=["https://App.Example.com"])
    sent = "HTTPS://app.example.com:443/"
    response = client.get("/api/v1/status", headers={"Origin": sent})
    assert response.headers["access-control-allow-origin"] == sent


def test_a_wildcard_worker_origin_drops_credentials(client, monkeypatch):
    """``*`` and credentials are mutually exclusive by specification, so credentials go."""
    active = install(monkeypatch, worker_origins=["*"], allow_credentials=True)
    assert active.allow_credentials is False
    response = client.get("/api/v1/status", headers={"Origin": "https://anything.example"})
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    assert any("'*'" in remark for remark in active.remarks)


def test_an_admin_wildcard_is_a_problem_not_a_policy():
    """A list that means "anybody's page" cannot answer "which console may drive payroll"."""
    active = netguard.build_policy(admin_origins=["*"])
    assert active.problems, "an admin wildcard must be reported"
    assert "CORS_ADMIN_ORIGINS" in active.problems[0]
    assert active.origin_class("https://anything.example") is None


def test_the_legacy_single_list_still_grants_what_it_used_to():
    """``ALLOWED_ORIGINS`` was one list. Merged as worker origins it changes nothing for a
    deployment that had it set, and it can no longer reach an admin path."""
    fake = SimpleNamespace(allowed_origins=["https://old.example.com"], cors_worker_origins=[])
    active = netguard.policy_from_settings(fake)
    assert active.origin_class("https://old.example.com") == netguard.CLASS_WORKER
    assert active.allows_origin("https://old.example.com", "/api/v1/admin/users") is False
    assert active.allows_origin("https://old.example.com", "/api/v1/status") is True


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
def test_a_preflight_from_a_worker_origin_to_an_admin_route_is_refused(client, monkeypatch):
    install(monkeypatch, worker_origins=[WORKER_ORIGIN], admin_origins=[ADMIN_ORIGIN])
    before = refusal_counter(netguard.REASON_ORIGIN)
    response = client.options(
        "/api/v1/admin/users/add",
        headers={
            "Origin": WORKER_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 403
    payload = response.json()
    assert payload["error_code"] == "cors_refused"
    assert payload["origin"] == WORKER_ORIGIN
    assert refusal_counter(netguard.REASON_ORIGIN) == before + 1
    # A refusal is a response like any other, so it carries the header baseline too.
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_allowed_preflight_advertises_the_methods_and_headers(client, monkeypatch):
    install(monkeypatch, worker_origins=[WORKER_ORIGIN], admin_origins=[ADMIN_ORIGIN])
    response = client.options(
        "/api/v1/attendance/verify",
        headers={
            "Origin": WORKER_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WORKER_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]
    assert response.headers["access-control-allow-credentials"] == "true"
    assert int(response.headers["access-control-max-age"]) > 0
    assert response.content == b""


def test_a_preflight_asking_for_an_unlisted_header_is_refused_by_name(client, monkeypatch):
    """Silently allowing it would be the browser's opaque error; refusing says what to fix."""
    install(monkeypatch, worker_origins=[WORKER_ORIGIN])
    response = client.options(
        "/api/v1/status",
        headers={
            "Origin": WORKER_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-tracking-header",
        },
    )
    assert response.status_code == 403
    assert "x-tracking-header" in response.json()["message"]


def test_the_tunnel_header_the_frontend_really_sends_is_permitted(client, monkeypatch):
    """``ngrok-skip-browser-warning`` is sent by the bundled frontend, so it must not 403."""
    install(monkeypatch, worker_origins=[WORKER_ORIGIN])
    response = client.options(
        "/api/v1/status",
        headers={
            "Origin": WORKER_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "ngrok-skip-browser-warning, authorization",
        },
    )
    assert response.status_code == 200
    assert "ngrok-skip-browser-warning" in response.headers["access-control-allow-headers"].lower()


# ---------------------------------------------------------------------------
# the admin network gate
# ---------------------------------------------------------------------------
def test_without_an_allowlist_admin_routes_are_reachable_from_anywhere(client, monkeypatch):
    """The pre-hardening behaviour, kept as the default so no deployment loses its console."""
    active = install(monkeypatch, worker_origins=[WORKER_ORIGIN])
    assert active.admin_gate_enabled is False
    assert client.get("/api/v1/admin/users", headers=bearer(ADMIN)).status_code == 200


def test_an_admin_route_refuses_a_client_outside_the_allowlist(client, monkeypatch, app_module):
    install(monkeypatch, admin_networks=[LAN])
    before = refusal_counter(netguard.REASON_IP)
    with peer_client(app_module, OUTSIDE) as outside:
        response = outside.get("/api/v1/admin/users", headers=bearer(ADMIN))
    assert response.status_code == 403
    payload = response.json()
    assert payload["error_code"] == netguard.REASON_IP
    assert refusal_counter(netguard.REASON_IP) == before + 1
    # The refusal must not enumerate what *is* allowed: that is the allowlist handed over one
    # guess at a time to whoever is probing.
    assert "10.0.0.0" not in json.dumps(payload)
    assert LAN not in json.dumps(payload)


@pytest.mark.parametrize("path", ["/api/v1/admin/users", "/api/v1/admin/audit_log"])
def test_an_admin_route_serves_a_client_inside_the_allowlist(client, monkeypatch, app_module, path):
    install(monkeypatch, admin_networks=[LAN])
    with peer_client(app_module, IN_ALLOWLIST) as inside:
        response = inside.get(path, headers=bearer(ADMIN))
    assert response.status_code == 200, response.text[:200]


def test_the_gate_leaves_worker_routes_alone(client, monkeypatch, app_module):
    """Site phones are on mobile networks: a gate over everything would stop the attendance
    the payroll is made of."""
    install(monkeypatch, admin_networks=[LAN])
    with peer_client(app_module, OUTSIDE) as outside:
        assert outside.get("/api/v1/status").status_code == 200
        assert outside.post("/api/v1/auth/login", json={}).status_code in (401, 422)


def test_only_the_admin_prefix_is_gated(client, monkeypatch, app_module):
    install(monkeypatch, admin_networks=[LAN])
    with peer_client(app_module, OUTSIDE) as outside:
        # A path that merely *starts with* the letters of the prefix is not the admin surface,
        # and the prefix itself is.
        assert outside.get("/api/v1/administrators").status_code == 404
        assert outside.get("/api/v1/admin").status_code == 403


def test_a_trusted_proxy_may_name_the_real_client(client, monkeypatch, app_module):
    install(monkeypatch, admin_networks=[LAN], trusted_proxies=["127.0.0.1/32"])
    with peer_client(app_module, TRUSTED_PROXY) as through_proxy:
        response = through_proxy.get(
            "/api/v1/admin/users", headers={"X-Forwarded-For": IN_ALLOWLIST, **bearer(ADMIN)}
        )
    assert response.status_code == 200, response.text[:200]


def test_xff_from_an_undeclared_peer_cannot_buy_admin_access(client, monkeypatch, app_module):
    """The spoofing case: a header is not an address, and this is where that is proven."""
    install(monkeypatch, admin_networks=[LAN], trusted_proxies=["127.0.0.1/32"])
    before = refusal_counter(netguard.REASON_PROXY)
    with peer_client(app_module, OUTSIDE) as attacker:
        response = attacker.get(
            "/api/v1/admin/users", headers={"X-Forwarded-For": IN_ALLOWLIST, **bearer(ADMIN)}
        )
    assert response.status_code == 403
    assert response.json()["error_code"] == netguard.REASON_PROXY
    assert refusal_counter(netguard.REASON_PROXY) == before + 1
    # The credential in that request is untouched by this: the gate is a network control, not
    # an authentication one. The same token works from an allowed address.
    with peer_client(app_module, IN_ALLOWLIST) as admin:
        assert admin.get("/api/v1/admin/users", headers=bearer(ADMIN)).status_code == 200


def test_a_multi_hop_chain_is_walked_from_the_right(client, monkeypatch, app_module):
    """``client, proxy1, proxy2``: the nearest hop wrote last, so the scan starts there."""
    install(monkeypatch, admin_networks=[LAN], trusted_proxies=["127.0.0.1/32", "10.0.0.1/32"])
    with peer_client(app_module, TRUSTED_PROXY) as through_proxy:
        allowed = through_proxy.get(
            "/api/v1/admin/users",
            headers={"X-Forwarded-For": f"{IN_ALLOWLIST}, 10.0.0.1", **bearer(ADMIN)},
        )
        # A chain whose real client is outside the allowlist is refused even though a trusted
        # hop sits in the middle of it.
        refused = through_proxy.get(
            "/api/v1/admin/users",
            headers={"X-Forwarded-For": f"{OUTSIDE}, 10.0.0.1", **bearer(ADMIN)},
        )
    assert allowed.status_code == 200
    assert refused.status_code == 403


def test_an_all_proxy_chain_falls_back_to_its_leftmost_entry(client, monkeypatch, app_module):
    install(monkeypatch, admin_networks=["127.0.0.1/32"], trusted_proxies=["127.0.0.1/32"])
    with peer_client(app_module, TRUSTED_PROXY) as through_proxy:
        response = through_proxy.get(
            "/api/v1/admin/users",
            headers={"X-Forwarded-For": "127.0.0.1, 127.0.0.1", **bearer(ADMIN)},
        )
    assert response.status_code == 200


def test_an_unparsable_forwarded_value_is_refused_rather_than_ignored(client, monkeypatch, app_module):
    install(monkeypatch, admin_networks=[LAN], trusted_proxies=["127.0.0.1/32"])
    with peer_client(app_module, TRUSTED_PROXY) as through_proxy:
        response = through_proxy.get(
            "/api/v1/admin/users", headers={"X-Forwarded-For": "not-an-address", **bearer(ADMIN)}
        )
    assert response.status_code == 403
    assert response.json()["error_code"] == netguard.REASON_PROXY


def test_an_ipv4_mapped_peer_matches_an_ipv4_allowlist(client, monkeypatch, app_module):
    """A dual-stack listener reports a v4 client as ``::ffff:10.1.2.3``; the allowlist is v4."""
    install(monkeypatch, admin_networks=[LAN])
    with peer_client(app_module, f"::ffff:{IN_ALLOWLIST}") as mapped:
        assert mapped.get("/api/v1/admin/users", headers=bearer(ADMIN)).status_code == 200


def test_a_valid_but_weak_policy_is_advisory_and_names_what_is_missing(client, monkeypatch, app_module):
    """An operator may have chosen this (a LAN deployment, a tunnel), so it is reported, not
    overruled - but it is reported."""
    install(monkeypatch, worker_origins=[WORKER_ORIGIN])  # no allowlist
    check = check_named("network_policy")
    assert check.ok is True
    assert check.tier == "advisory"
    assert "off" in check.detail
    assert any("ADMIN_IP_ALLOWLIST is empty" in remark for remark in check.value["remarks"])
    assert any("CSP" in remark for remark in check.value["remarks"])


def test_a_misconfigured_allowlist_is_fatal_and_fails_closed(client, monkeypatch, app_module):
    """A security allowlist that cannot be parsed must stop the server, not silently pass."""
    broken = install(monkeypatch, admin_networks=["10.0.0.0/33"], trusted_proxies=["nope"])
    assert broken.problems
    check = check_named("network_policy")
    assert check.tier == "fatal"
    assert check.ok is False
    assert "10.0.0.0/33" in check.detail
    # Fails closed: nothing matches, so even an address the operator meant to allow is refused.
    with peer_client(app_module, IN_ALLOWLIST) as inside:
        assert inside.get("/api/v1/admin/users", headers=bearer(ADMIN)).status_code == 403


def test_a_forwarded_header_from_an_undeclared_proxy_is_noticed_and_reported(
    client, monkeypatch, app_module
):
    """The misconfiguration that is invisible in the config file: the proxy exists, the
    declaration does not. Worker traffic is served, and readiness says what is wrong."""
    install(monkeypatch, admin_networks=[LAN], trusted_proxies=["127.0.0.1/32"])
    with peer_client(app_module, OUTSIDE) as outside:
        assert outside.get("/api/v1/status", headers={"X-Forwarded-For": OUTSIDE}).status_code == 200

    misuse = netguard.forward_misuse()
    assert misuse["count"] == 1
    assert misuse["last_peer"] == OUTSIDE
    check = check_named("network_policy")
    assert check.ok is False and check.tier == "advisory"
    assert "TRUSTED_PROXIES" in check.detail


def test_an_all_peers_proxy_list_is_reported_as_what_it_is(monkeypatch):
    active = install(monkeypatch, trusted_proxies=["*"], admin_networks=[LAN])
    assert any("forge X-Forwarded-For" in remark for remark in active.remarks)


# ---------------------------------------------------------------------------
# security headers
# ---------------------------------------------------------------------------
def test_a_json_response_carries_the_api_policy_and_the_baseline(client, monkeypatch):
    install(monkeypatch)
    response = client.get("/api/v1/status")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    # A JSON body loads nothing and is framed by nothing.
    assert response.headers["content-security-policy"] == netguard.CSP_API
    assert "camera=(self)" in response.headers["permissions-policy"]
    assert "microphone=()" in response.headers["permissions-policy"]


def test_a_document_response_carries_the_document_policy(client, monkeypatch):
    install(monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert policy == netguard.CSP_HTML
    assert "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy
    assert "frame-src 'none'" in policy
    # No third-party origin at all, in either directive. The utility classes the frontend
    # used to have compiled in the browser by the Tailwind Play CDN are components in
    # ``frontend/style.css``; a policy that names that host again means the compiler is
    # back, and a phone at a gate is downloading a 120 KB script to style five classes.
    assert "cdn.tailwindcss.com" not in policy
    assert "script-src 'self';" in policy
    assert "style-src 'self' 'unsafe-inline';" in policy
    assert "img-src 'self' data: blob:" in policy


def test_a_bodiless_response_is_judged_by_the_request_not_by_a_missing_content_type():
    """A ``304`` has no content-type, and picking the CSP from that made pages script-dead.

    Measured on a running server: ``curl -H 'If-None-Match: <the etag>' /quick.html`` - and
    ``/`` - came back ``304 Not Modified`` carrying ``CSP_API``, ``default-src 'none'``. A
    browser merges a 304's headers into the response it has stored, so the refusal replaced
    the policy the page was cached with, every script was blocked from the second load
    onwards, and the page sat on its own "Checking…" placeholder looking like a slow
    connection - after a first load that had worked. Chrome reproduced it: reloading the
    link page left ``typeof Capture === 'undefined'`` and all four CSP violations in the
    console, while the same URL in a fresh context booted normally.

    The document policy is not what is asserted here but the *decision*, because the 304 can
    come from any caching layer in front of the app - a CDN, or the Worker in ``deploy/``
    that proxies ``/q/*`` and ``/enroll/*`` - and any of them can strip it to a bodiless
    response. What is left to judge by is the request, and the two halves of the split are
    pinned together so "send the document policy on every 304" cannot pass either.
    """
    navigation = [(b"accept", b"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")]
    assert netguard._is_document(None, status=304, request_headers=navigation) is True
    assert netguard._is_document(None, status=304, request_headers=[(b"accept", b"*/*")]) is False
    #: No header at all is not a navigation, so the stricter policy is the safe answer.
    assert netguard._is_document(None, status=304, request_headers=[]) is False
    #: And a body that does carry a type is still judged by the type, not by ``Accept``.
    assert netguard._is_document(b"application/json", status=200, request_headers=navigation) is False
    assert netguard._is_document(b"text/html; charset=utf-8", status=200) is True


def test_the_document_policy_refuses_inline_script_elements(client, monkeypatch):
    """The stored-XSS payload needs an inline ``<script>`` *element*; that one is refused.

    The distinction this pins is the whole point of the split directives: a browser reads
    ``script-src`` for elements and ``script-src-attr`` for attributes, so the attribute
    allowance must not be spelled in the first one. A future edit that "simplifies" the two
    lines back into ``script-src 'self' 'unsafe-inline'`` fails here.
    """
    install(monkeypatch)
    policy = client.get("/").headers["content-security-policy"]
    script_src = policy.split("script-src ", 1)[1].split(";", 1)[0]
    assert "'unsafe-inline'" not in script_src, script_src
    assert "'unsafe-eval'" not in script_src, script_src
    assert policy.count("'unsafe-inline'") == policy.count("script-src-attr 'unsafe-inline'") + policy.count(
        "style-src 'self' 'unsafe-inline'"
    )
    # ... and the offline pages carry no inline block for a policy to have to allow.
    page = client.get("/").text
    assert "<script>" not in page.replace("<script src=", "")


def test_a_document_says_where_a_referrer_may_come_from(client, monkeypatch):
    """Documents get the browser-default policy; JSON keeps the stricter one.

    ``strict-origin-when-cross-origin`` is what a page needs to still be usable when it is
    opened across a tunnel; ``no-referrer`` is free on a JSON body, which is never a
    navigation source, and it means an error payload can never be quoted as a referrer.
    """
    install(monkeypatch)
    assert client.get("/").headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert client.get("/api/v1/status").headers["referrer-policy"] == "no-referrer"


def test_static_assets_get_the_baseline_too(client, monkeypatch):
    install(monkeypatch)
    response = client.get("/static/frontendjavascript.js")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == netguard.CSP_API


def test_hsts_is_absent_over_plain_http(client, monkeypatch):
    """Pinning a host that is served over HTTP is how a site LAN loses its punch page."""
    install(monkeypatch, hsts_max_age_seconds=600)
    assert "strict-transport-security" not in client.get("/api/v1/status").headers


def test_hsts_is_present_over_tls(client, monkeypatch, app_module):
    install(monkeypatch, hsts_max_age_seconds=600)
    with peer_client(app_module, TRUSTED_PROXY, https=True) as tls:
        response = tls.get("/api/v1/status")
    assert response.headers["strict-transport-security"] == "max-age=600; includeSubDomains"


def test_a_trusted_proxy_may_report_that_it_terminated_tls(client, monkeypatch, app_module):
    install(monkeypatch, hsts_max_age_seconds=600)
    with peer_client(app_module, TRUSTED_PROXY) as through_proxy:
        trusted = through_proxy.get("/api/v1/status", headers={"X-Forwarded-Proto": "https"})
    assert trusted.headers.get("strict-transport-security") == "max-age=600; includeSubDomains"
    # The same header from a peer nobody declared is not evidence of anything.
    with peer_client(app_module, OUTSIDE) as direct:
        untrusted = direct.get("/api/v1/status", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in untrusted.headers


def test_headers_can_be_turned_off_for_a_deployment_that_sets_them_elsewhere(client, monkeypatch):
    install(monkeypatch, security_headers=False)
    response = client.get("/api/v1/status")
    assert "x-frame-options" not in response.headers
    assert "content-security-policy" not in response.headers


def test_a_route_keeps_the_headers_it_sets_itself(client, monkeypatch):
    """The metrics payload sets ``no-store``; the middleware adds, it does not overrule."""
    if not telemetry.AVAILABLE:
        # The assertion needs a route that serves 200, and /metrics - the one route in this
        # suite that sets its own headers - answers 501 without the optional library. The
        # header contract itself is pinned in ``test_metrics.py`` alongside the exposition.
        pytest.skip("prometheus_client is not installed")
    install(monkeypatch)
    response = client.get("/metrics", headers=bearer(ADMIN))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_header_merge_unions_vary_instead_of_clobbering_it():
    merged = netguard._merge_headers(
        [(b"vary", b"Accept")], [(b"vary", b"Origin"), (b"x-frame-options", b"DENY")]
    )
    values = {key.lower(): value for key, value in merged}
    assert values[b"vary"] == b"Accept, Origin"
    assert values[b"x-frame-options"] == b"DENY"


# ---------------------------------------------------------------------------
# units the requests above depend on
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "entry,ok",
    [
        ("10.0.0.0/8", True),
        ("10.1.2.3", True),
        ("2001:db8::/32", True),
        ("10.0.0.0/33", False),
        ("not-a-network", False),
        ("10.0.0.0/8", True),
    ],
)
def test_network_parsing_accepts_addresses_and_refuses_typos(entry, ok):
    try:
        parsed = netguard.parse_networks([entry], setting="TEST")
    except netguard.NetworkPolicyError:
        assert not ok, f"{entry} should have parsed"
        return
    assert ok, f"{entry} should not have parsed"
    assert parsed


def test_the_gate_is_off_only_when_no_allowlist_is_configured():
    assert netguard.build_policy().admin_gate_enabled is False
    assert netguard.build_policy(admin_networks=[LAN]).admin_gate_enabled is True


def test_a_non_http_scope_passes_straight_through():
    """Lifespan and websocket scopes are not requests; the guard must not invent headers."""
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(netguard.NetworkGuardMiddleware(app)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_the_policy_in_force_is_the_one_from_settings(monkeypatch):
    """``netguard.policy()`` reads the deployment's settings, not a hardcoded default."""
    monkeypatch.setattr(netguard, "_policy", None)
    monkeypatch.setattr(settings, "cors_worker_origins", [WORKER_ORIGIN], raising=False)
    monkeypatch.setattr(settings, "admin_ip_allowlist", [LAN], raising=False)
    active = netguard.policy()
    assert [rule.raw for rule in active.worker_origins] == [WORKER_ORIGIN]
    assert active.admin_gate_enabled is True
    assert active.trusted_proxies, "the loopback default survives an empty setting"


def test_the_shipped_settings_do_not_block_the_bundled_frontend(monkeypatch):
    """No regression for the app as deployed: same-origin pages and workers keep working."""
    monkeypatch.setattr(netguard, "_policy", None)
    active = netguard.policy()
    assert active.admin_gate_enabled is False, "the default must not gate anything"
    assert active.security_headers is True
    assert active.problems == ()


def test_a_refused_request_is_a_well_formed_json_error_and_writes_nothing(
    client, monkeypatch, app_module
):
    """A gate that refuses *after* the handler ran would be a gate in name only."""
    install(monkeypatch, admin_networks=[LAN])
    before = db_scalar("SELECT COUNT(*) FROM construction_sites")
    with peer_client(app_module, OUTSIDE) as outside:
        response = outside.post(
            "/api/v1/admin/sites/add",
            json={"name": "Gate Breach Site", "latitude": 30.0, "longitude": 31.0, "radius_m": 100},
            headers=bearer(ADMIN),
        )
    assert response.status_code == 403
    body = response.json()
    assert set(body) == {"error_code", "message"}
    assert body["message"]
    assert db_scalar("SELECT COUNT(*) FROM construction_sites") == before


def test_the_launcher_hands_the_same_trusted_proxies_to_uvicorn(monkeypatch):
    """One list, two consumers: the gate and uvicorn (which rewrites ``scope["client"]``).

    If these disagreed, uvicorn would stop rewriting the client address - so the audit log and
    the per-IP rate limiter would see the proxy for every worker - while the admin gate would
    refuse the administrator whose address it could no longer determine.
    """
    import serve

    # The launcher reads the configuration the way the running server does - from the
    # environment - rather than from an already-imported Settings object, which is exactly
    # what makes it the same list the application's own gate builds from.
    monkeypatch.setenv("TRUSTED_PROXIES", f"{LAN},127.0.0.1/32")
    assert serve.trusted_proxies_for_uvicorn() == f"{LAN},127.0.0.1/32"

    def broken():
        raise RuntimeError("no configuration yet")

    import config

    monkeypatch.setattr(config, "build_settings", broken)
    assert serve.trusted_proxies_for_uvicorn() == "127.0.0.1", "loopback is the safe fallback"


def test_the_deployment_admin_token_does_not_bypass_the_gate(client, monkeypatch, app_module):
    """Being an administrator is not the same as being on the administrator network."""
    install(monkeypatch, admin_networks=[LAN])
    with peer_client(app_module, OUTSIDE) as outside:
        for token in (ADMIN, WORKER):
            assert outside.get("/api/v1/admin/users", headers=bearer(token)).status_code == 403
