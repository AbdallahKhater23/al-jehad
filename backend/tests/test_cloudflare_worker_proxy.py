"""The Cloudflare Worker in front of the app: what it proxies, and what it must not.

WHY THIS EXISTS
---------------
Deploying the frontend to a host of its own breaks the app in a way that looks like a backend
outage. The app is single-origin by construction - ``API.resolveBaseURL`` answers ``page origin
+ /api/v1`` - so a shell served from ``something.workers.dev`` calls ``something.workers.dev/
/api/v1/...`` for login, for the punch, for the clock-in window on the punch card, and gets a
404 from the asset host on every one of them. That is exactly what the deployed link did before
this Worker existed (checked with a request, not assumed: the shell answered 200, ``/api/v1/
status`` answered 404).

So the Worker serves the frontend and forwards the paths the backend owns to the backend's own
address. What is asserted here is the part that would be a security bug if it were wrong:

* **the proxy list is an allow-list.** ``/api/v1/status`` is forwarded; ``/apiary`` is not. A
  path nobody listed is served from the asset bundle, never forwarded with the caller's
  headers, so a route nobody thought about (a new admin endpoint, ``/metrics``) is not
  reachable through a name it did not ask for;
* **the request survives the hop** - method, path, query, body and ``Authorization`` - and the
  response comes back untouched, refusals included. A proxy that turned a 403 into a 200 would
  be a second implementation of the API's contract;
* **the two headers that exist for a reason** are set: the interstitial skip for a host that
  shows a warning page (ngrok's free tier would otherwise answer a fetch with HTML) and the
  client's address, which is what keeps a per-worker rate limit from becoming one shared bucket;
* **a misconfigured deployment says so.** No ``API_ORIGIN`` answers a 500 naming the variable,
  not the 404 that reads like a backend fault - the failure this suite was written from.

The last test needs no Node: it compares the Worker's prefix list with ``wrangler.toml``'s
``run_worker_first``, because those two lists are the same decision written twice.

Node is optional; without it the Node-driven tests skip rather than fail.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


import pytest

# ``harness`` for the project root only: this suite drives a Worker file with Node and never
# opens the database, but importing it is what ``tests/conftest.py`` does for every suite here,
# so the fixture cost is already paid by the time this module loads.
from harness import PROJECT_ROOT

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DEPLOY_DIR = PROJECT_ROOT / "deploy" / "cloudflare"
WORKER = DEPLOY_DIR / "worker.mjs"
WRANGLER = DEPLOY_DIR / "wrangler.toml"

TUNNEL = "https://tunnel.example.ngrok-free.dev"
#: The backend's own address in the deployment this Worker is written for - the Railway
#: service, at a hostname that does not change. Named as the representative of "a host that
#: needs no interstitial header skipped".
HOST = "https://al-jehad-production.up.railway.app"


@pytest.fixture(scope="module")
def results() -> dict:
    """Run the scenarios against the real Worker file and return what it did."""
    script = """
import { pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';

const worker = (await import(pathToFileURL(process.argv[2]).href)).default;
const { isBackendPath } = await import(pathToFileURL(process.argv[2]).href);

// The two addresses come from the Python half, so both halves are asserting against one
// literal rather than two that agreed when they were written.
const TUNNEL = '__TUNNEL__';
const HOST = '__HOST__';

/** What the Worker's outbound call looked like, so the hop can be asserted. */
let calls = [];
/** The answer the tunnelled API gives, per scenario. */
let    answer = () => new Response('{"status":"ok"}', {
    status: 200, headers: { 'content-type': 'application/json' }
});

const realFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: (init && init.method) || 'GET', init: init || {} });
    return answer(String(url), init || {});
};

function env(overrides) {
    return Object.assign({
        API_ORIGIN: 'https://tunnel.example.ngrok-free.dev',
        ASSETS: { calls: [], async fetch(request) { this.calls.push(request); return new Response('<html>shell</html>', {status: 200}); } }
    }, overrides || {});
}

async function call(request, environment) {
    calls = [];
    const response = await worker.fetch(request, environment);
    const body = response.status === 204 || response.status === 304 ? null : await response.text();
    return { status: response.status, body: body, headers: Object.fromEntries(response.headers) };
}

/** A request as a browser sends it, with the address Cloudflare adds. */
function incoming(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    opts.headers = Object.assign({
        'authorization': 'Bearer tok-600',
        'cf-connecting-ip': '41.33.7.9',
        'user-agent': 'Mozilla/5.0'
    }, opts.headers);
    return new Request('https://al-jehad1.example.workers.dev' + path, opts);
}

async function forwardedBody(init) {
    if (!init.body) return null;
    return await new Response(init.body).text();
}

const results = {};

// 1. a backend path is forwarded, with the hop's headers
{
    const out = await call(incoming('/api/v1/status'), env());
    const forwarded = calls[0];
    results.api = {
        calls: calls.length,
        url: forwarded.url,
        method: forwarded.method,
        skip_interstitial: forwarded.init.headers.get('ngrok-skip-browser-warning'),
        authorization: forwarded.init.headers.get('authorization'),
        client_ip: forwarded.init.headers.get('x-forwarded-for'),
        answer: out
    };
}

// 2. the query string - what the punch card's window look-up is - survives
{
    const out = await call(incoming('/api/v1/worker/me/site-window?location_input=30.05%2C31.23'), env());
    results.query = { url: calls[0].url, status: out.status };
}

// 3. a POST keeps its method and its body
{
    const request = incoming('/api/v1/attendance/verify', {
        method: 'POST', body: 'selfie-bytes', headers: { 'content-type': 'multipart/form-data' }
    });
    await call(request, env());
    results.post = {
        method: calls[0].method,
        body: await forwardedBody(calls[0].init),
        content_type: calls[0].init.headers.get('content-type')
    };
}

// 4. the paths the backend owns, and the path that merely looks like one
{
    const paths = ['/api/v1/status', '/api', '/static/logo.svg', '/enroll/abc123', '/q/xyz'];
    results.proxied = {};
    for (const path of paths) {
        await call(incoming(path), env());
        results.proxied[path] = calls.length === 1 && calls[0].url.startsWith(TUNNEL)
            ? calls[0].url.slice(TUNNEL.length)
            : null;
    }
    // Names that merely *start* with a prefix, plus two paths that belong to nothing. The
    // bare prefixes (``/api``, ``/q``) are in the list above: they are the backend's
    // namespace, and its own 404 is the truthful answer for one.
    const nearMisses = ['/apiary', '/apix', '/enrollments', '/staticx/file', '/'];
    results.not_proxied = {};
    for (const path of nearMisses) {
        const environment = env();
        await call(incoming(path), environment);
        results.not_proxied[path] = {
            forwarded: calls.length,
            asset_requests: environment.ASSETS.calls.length
        };
    }
}

// 5. an asset is served by the binding, and the network is never touched
{
    const environment = env();
    const out = await call(incoming('/frontendjavascript.js'), environment);
    results.asset = {
        forwarded: calls.length,
        asset_requests: environment.ASSETS.calls.length,
        asset_url: environment.ASSETS.calls.length ? environment.ASSETS.calls[0].url : null,
        answer: out
    };
}

// 6. the API's answer comes back as it was sent - a refusal and a bodiless status included
{
    answer = () => new Response(JSON.stringify({ detail: 'Already clocked in!' }), {
        status: 403, headers: { 'content-type': 'application/json', 'x-request-id': 'abc' }
    });
    results.refusal = await call(incoming('/api/v1/attendance/verify', { method: 'POST', body: 'x' }), env());

    answer = () => new Response(null, { status: 204, headers: { 'cache-control': 'no-store' } });
    results.empty = await call(incoming('/api/v1/admin/sites/delete', { method: 'POST', body: 'x' }), env());

    answer = () => new Response(null, { status: 302, headers: { location: 'https://example.test/next' } });
    results.redirect = await call(incoming('/q/token'), env());
}

// 7. a real host - the deployment this Worker is written for - needs no interstitial header
{
    await call(incoming('/api/v1/status'), env({ API_ORIGIN: HOST }));
    results.host = {
        url: calls[0].url,
        skip_interstitial: calls[0].init.headers.get('ngrok-skip-browser-warning')
    };
}

// 8. a misconfigured Worker says which variable is missing, and never proxies the wrong place
{
    results.no_origin = await call(incoming('/api/v1/status'), env({ API_ORIGIN: '' }));
    results.scheme_origin = await call(incoming('/api/v1/status'), env({ API_ORIGIN: 'localhost:8000' }));
    results.bad_origin = await call(incoming('/api/v1/status'), env({ API_ORIGIN: 'not a url at all' }));
    results.no_assets = await call(incoming('/index.html'), env({ ASSETS: undefined }));

    // A trailing slash must not become ``//api/v1/...``: that is a different path to some
    // proxies, and a 404 that looks like the backend's fault.
    await call(incoming('/api/v1/status'), env({ API_ORIGIN: 'https://tunnel.example.ngrok-free.dev/' }));
    results.trailing_slash = calls[0].url;
}

// 9. a backend that is down is reported as a backend that is down
{
    globalThis.fetch = async () => { throw new Error('connect ECONNREFUSED'); };
    results.unreachable = await call(incoming('/api/v1/status'), env());
    globalThis.fetch = realFetch;
}

results.is_backend_path = {};
for (const path of ['/api/v1/x', '/api', '/static/a.css', '/enroll/1', '/q/1', '/apiary', '/', '/metrics']) {
    results.is_backend_path[path] = isBackendPath(path);
}

process.stdout.write(JSON.stringify(results));
"""

    with tempfile.TemporaryDirectory(prefix="worker_vm_") as folder:
        harness = Path(folder) / "harness.mjs"
        harness.write_text(
            script.replace("__TUNNEL__", TUNNEL).replace("__HOST__", HOST),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [NODE, str(harness), str(WORKER)],
            capture_output=True, timeout=120, encoding="utf-8", errors="replace",
        )
    assert completed.returncode == 0, f"worker harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def test_a_backend_path_is_forwarded_to_the_backend_with_the_hop_s_headers(results):
    api = results["api"]
    assert api["calls"] == 1
    assert api["url"] == f"{TUNNEL}/api/v1/status"
    assert api["method"] == "GET"
    assert api["skip_interstitial"] == "true", (
        "without this the free tunnel answers a fetch with its browser-warning page, and the "
        "app reports an HTML page as a JSON parse error"
    )
    assert api["authorization"] == "Bearer tok-600", "the session has to survive the hop"
    assert api["client_ip"] == "41.33.7.9", (
        "without the client's own address every worker shares one rate-limit bucket"
    )
    assert api["answer"]["status"] == 200
    assert api["answer"]["body"] == '{"status":"ok"}'


def test_the_query_string_is_part_of_the_forwarded_path(results):
    """The punch card's window look-up is a query parameter, so this is that feature."""
    assert results["query"]["url"] == f"{TUNNEL}/api/v1/worker/me/site-window?location_input=30.05%2C31.23"
    assert results["query"]["status"] == 200


def test_a_punch_keeps_its_method_and_its_body(results):
    assert results["post"]["method"] == "POST"
    assert results["post"]["body"] == "selfie-bytes"
    assert results["post"]["content_type"] == "multipart/form-data"


def test_every_path_the_backend_owns_is_forwarded(results):
    expected = {
        "/api/v1/status": "/api/v1/status",
        "/api": "/api",
        "/static/logo.svg": "/static/logo.svg",
        "/enroll/abc123": "/enroll/abc123",
        "/q/xyz": "/q/xyz",
    }
    for path, forwarded in expected.items():
        assert results["proxied"][path] == forwarded, f"{path} was not forwarded there"


def test_a_path_that_merely_looks_like_a_backend_path_never_leaves_the_zone(results):
    """The allow-list boundary: ``/apiary`` is an asset name, not an API call.

    A ``startsWith('/api')`` without the separator would forward it - and with it any path
    anybody chose, carrying the caller's headers, to whatever the backend serves there.
    """
    for path, seen in results["not_proxied"].items():
        assert seen["forwarded"] == 0, f"{path} was proxied to the API"
        assert seen["asset_requests"] == 1, f"{path} was not served as an asset"


def test_the_frontend_is_served_by_the_assets_binding(results):
    asset = results["asset"]
    assert asset["asset_requests"] == 1
    assert asset["forwarded"] == 0, "a frontend file must never go to the API"
    assert asset["asset_url"].endswith("/frontendjavascript.js")
    assert asset["answer"]["status"] == 200
    assert "shell" in asset["answer"]["body"]


def test_a_refusal_from_the_api_comes_back_exactly_as_it_was_sent(results):
    """Status, body and headers pass through: the app keys on all three."""
    refusal = results["refusal"]
    assert refusal["status"] == 403
    assert refusal["headers"]["content-type"] == "application/json"
    assert refusal["headers"]["x-request-id"] == "abc"
    assert "Already clocked in!" in refusal["body"]

    # A 204 and a 302 carry no body, and constructing one with a body throws.
    assert results["empty"]["status"] == 204
    assert results["empty"]["body"] is None
    assert results["redirect"]["status"] == 302
    assert results["redirect"]["headers"]["location"] == "https://example.test/next"


def test_a_host_that_shows_no_warning_page_gets_no_interstitial_header(results):
    """A real host (Railway, or a Cloudflare Tunnel) shows no warning page, so nothing is sent.

    It is one header, but it is the difference between a JSON API call and an HTML page
    parsed as JSON - so the hosts that need it and the hosts that do not are asserted
    separately rather than assumed.
    """
    assert results["host"]["url"] == f"{HOST}/api/v1/status"
    assert results["host"]["skip_interstitial"] is None


def test_a_misconfigured_worker_names_the_variable_instead_of_404_ing(results):
    """The failure this Worker exists to remove: a 404 that reads like a backend fault."""
    missing = results["no_origin"]
    assert missing["status"] == 500
    assert "worker_misconfigured" in missing["body"]
    assert "API_ORIGIN" in missing["body"]

    # A value that is not an address at all, and one that parses to a scheme the Worker will
    # not fetch. Both name the variable and echo what was set, because "500" on every screen
    # is not something an operator can act on.
    bad = results["bad_origin"]
    assert bad["status"] == 500
    assert "API_ORIGIN is not a URL" in bad["body"]

    scheme = results["scheme_origin"]
    assert scheme["status"] == 500
    assert "API_ORIGIN" in scheme["body"] and "localhost:8000" in scheme["body"]

    no_assets = results["no_assets"]
    assert no_assets["status"] == 500
    assert "ASSETS" in no_assets["body"]


def test_a_trailing_slash_in_the_origin_does_not_double_up_the_path(results):
    assert results["trailing_slash"] == f"{TUNNEL}/api/v1/status"


def test_an_unreachable_backend_is_reported_as_an_unreachable_backend(results):
    assert results["unreachable"]["status"] == 502
    assert "api_unreachable" in results["unreachable"]["body"]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/v1/x", True),
        ("/api", True),
        ("/static/a.css", True),
        ("/enroll/1", True),
        ("/q/1", True),
        ("/apiary", False),
        ("/", False),
        ("/metrics", False),
    ],
)
def test_the_worker_s_own_router_agrees(results, path, expected):
    assert results["is_backend_path"][path] is expected


def test_the_committed_api_origin_is_a_real_address_not_a_placeholder():
    """The variable the deployment lives or dies by, checked before a deploy instead of after.

    ``API_ORIGIN`` is the one setting whose wrong value is invisible until a worker is standing
    at a site: an empty or placeholder one answers a 500 on every call, and a *stale* one (the
    quick tunnel from last week, whose hostname is now dead) answers ``api_unreachable``. Both
    look like a backend outage from the phone. The committed value is the deployed backend -
    the Railway service - so this asserts that it is an https address and not one of the
    placeholders this file has used.
    """
    config = WRANGLER.read_text(encoding="utf-8")
    match = re.search(r'^API_ORIGIN\s*=\s*"([^"]*)"', config, re.M)
    assert match, "wrangler.toml no longer declares API_ORIGIN"
    origin = match.group(1).strip()

    assert origin.startswith("https://"), f"API_ORIGIN must be https, not {origin!r}"
    assert origin.rstrip("/") == origin, "a trailing slash becomes //api/v1 and 404s"
    for placeholder in ("REPLACE", "YOUR-", "example.", "localhost", "trycloudflare"):
        assert placeholder.lower() not in origin.lower(), (
            f"API_ORIGIN is still a placeholder ({origin!r}): a quick-tunnel address also "
            "rotates on every restart, so it is not an address to commit"
        )


def test_the_two_copies_of_the_proxy_list_agree():
    """``worker.mjs`` decides what is proxied; ``wrangler.toml`` decides what reaches it.

    Two lists for one decision, so they are compared rather than trusted: a prefix added to
    the Worker but not to ``run_worker_first`` still works (the asset layer passes an unknown
    path through), but a prefix in ``run_worker_first`` and not in the Worker is a path the
    runtime hands to code that answers it with a 404 from the asset bundle.
    """
    worker_source = WORKER.read_text(encoding="utf-8")
    match = re.search(r"const BACKEND_PREFIXES = \[(.*?)\]", worker_source, re.S)
    assert match, "worker.mjs no longer declares BACKEND_PREFIXES where this test reads it"
    worker_prefixes = {part.strip().strip("'\"") for part in match.group(1).split(",") if part.strip()}

    config = WRANGLER.read_text(encoding="utf-8")
    globs = re.search(r"run_worker_first = \[(.*?)\]", config, re.S)
    assert globs, "wrangler.toml no longer declares run_worker_first"
    config_prefixes = {part.strip().strip("'\"").rstrip("/*") for part in globs.group(1).split(",") if part.strip()}

    assert worker_prefixes == config_prefixes, (
        f"the Worker proxies {sorted(worker_prefixes)} and wrangler hands it "
        f"{sorted(config_prefixes)}"
    )
    assert "directory" in config and "frontend" in config, (
        "the assets directory must point at the frontend this Worker serves"
    )
