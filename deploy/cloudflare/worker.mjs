/**
 * The Cloudflare Worker in front of the app: one origin for the shell and the API.
 *
 * WHY A WORKER AND NOT JUST A STATIC HOST
 * ---------------------------------------
 * This app is single-origin by construction. ``API.resolveBaseURL`` in
 * ``frontend/frontendjavascript.js`` answers ``page origin + /api/v1``, which is what keeps
 * GPS, the camera and the session working wherever the page is served from (see the note in
 * ``backend/serve.py``). Host the frontend on a static host on its own and every call -
 * login, the worker's own stats, the punch, the clock-in window on the punch card - goes to
 * that host's ``/api/v1`` and 404s. It fails quietly, too: the app opens, then the login
 * screen says the server is unreachable while the server is up.
 *
 * So the Worker does the two things that keep one origin true:
 *
 *   1. **serves the frontend** from the ``ASSETS`` binding (``../frontend``, deployed with
 *      the Worker), and
 *   2. **proxies the paths the backend owns** - ``/api``, ``/static``, ``/enroll``, ``/q`` -
 *      to the tunnel in front of the backend.
 *
 * EVERYTHING ELSE IS A FRONTEND ASSET, WHICH IS THE SECURITY HALF. The proxy list is an
 * allow-list, not a deny-list: a path nobody listed is served from the asset bundle (a 404
 * for a name that is not a file), never forwarded to the API with whatever headers the
 * caller sent. A deny-list here would be a public proxy for every URL the backend ever adds
 * - the metrics endpoint, an admin route - under a name nobody thought about.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 * --------------------------------
 * * No CORS. The browser sees one origin, so there is nothing to allow. ``CORS_WORKER_ORIGINS``
 *   stays empty on the backend and the document CSP's ``connect-src 'self'`` keeps holding.
 * * No rewriting of API responses: status, headers and body are passed through as they
 *   arrived, including a refusal. A proxy that tidied up a 403 into a 200 would be a second
 *   implementation of the API's contract.
 * * No configuration it can guess. ``API_ORIGIN`` has no sensible default, so an unset one
 *   answers a 500 that names the variable instead of a 404 that reads like a backend fault.
 */

//: The paths the backend owns. Keep in step with ``backend/main.py``: ``/api/v1`` is the
//: router, ``/static`` is the frontend mount kept for old bookmarks (``main.py`` section 3),
//: and ``/enroll/{token}`` and ``/q/{token}`` are the two pages served for a link - the ones
//: that must keep working for somebody with no account and no session.
const BACKEND_PREFIXES = ['/api', '/static', '/enroll', '/q'];

//: Tunnels that answer an unrecognised client with an HTML interstitial. The frontend sends
//: ``ngrok-skip-browser-warning`` itself when it talks to one of these directly; the Worker is
//: now that client, so it has to send it too. Mirrors ``TUNNEL_HOST_SUFFIXES`` in
//: ``frontend/frontendjavascript.js``.
const TUNNEL_HOST_SUFFIXES = ['ngrok-free.dev', 'ngrok-free.app', 'ngrok.app', 'ngrok.io', 'ngrok.dev'];

//: Methods with no body, so a body is never attached to them.
const BODYLESS_METHODS = new Set(['GET', 'HEAD']);

//: Statuses a ``Response`` may not carry a body for.
const BODYLESS_STATUSES = new Set([101, 204, 205, 304]);

export default {
    /**
     * @param {Request} request
     * @param {{API_ORIGIN?: string, ASSETS?: {fetch: (request: Request) => Promise<Response>}}} env
     */
    async fetch(request, env) {
        const url = new URL(request.url);
        if (isBackendPath(url.pathname)) return proxyToApi(request, env, url);
        return serveFrontend(request, env);
    }
};

/** Whether ``pathname`` belongs to the backend rather than to the frontend bundle. */
export function isBackendPath(pathname) {
    // ``/api/v1/...`` and ``/api``: the trailing slash is stripped first so that a prefix
    // match cannot be fooled by ``/apiary`` - a path that merely *starts with* ``/api`` is a
    // different path, and this list is what decides what leaves the zone.
    const path = String(pathname || '/').replace(/\/+$/, '') || '/';
    return BACKEND_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix + '/'));
}

function isTunnelHost(hostname) {
    const host = String(hostname || '').toLowerCase();
    return TUNNEL_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith('.' + suffix));
}

/** A refusal this Worker owns, in the shape the app's own errors take. */
function problem(status, errorCode, message) {
    return new Response(JSON.stringify({ error_code: errorCode, message }), {
        status,
        headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' }
    });
}

/** The configured tunnel origin, or ``null`` - with the reason, for the message. */
export function resolveOrigin(env) {
    const raw = String((env && env.API_ORIGIN) || '').trim();
    if (!raw) return { origin: null, problem: 'API_ORIGIN is empty' };
    let url;
    try {
        url = new URL(raw);
    } catch (err) {
        return { origin: null, problem: `API_ORIGIN is not a URL: ${raw}` };
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') {
        return { origin: null, problem: `API_ORIGIN must be http(s): ${raw}` };
    }
    // Normalised to an absolute URL with no trailing slash, so joining a path onto it cannot
    // produce ``//api/v1/...`` - which some proxies resolve to a different path than the
    // one the app asked for.
    return { origin: url.origin, problem: null };
}

async function proxyToApi(request, env, url) {
    const { origin, problem: misconfigured } = resolveOrigin(env);
    if (!origin) {
        // A visible failure, not a silent 404: with no origin every API call would look like
        // "the backend is down" and the one thing to fix would be invisible.
        return problem(
            500,
            'worker_misconfigured',
            `This Worker has no API origin to proxy to (${misconfigured}). Set API_ORIGIN to the ` +
                'https address in front of the backend (see deploy/cloudflare/README.md).'
        );
    }

    const target = new URL(url.pathname + url.search, origin);
    const headers = new Headers(request.headers);
    // ``Host`` belongs to the URL being fetched, never to the request we were handed.
    headers.delete('host');
    if (isTunnelHost(target.hostname)) headers.set('ngrok-skip-browser-warning', 'true');
    // The client's real address, so a per-worker rate limit survives the extra hop. Without
    // it every punch arrives from the Worker's egress address and the whole site shares one
    // bucket. ``cf-connecting-ip`` is set by Cloudflare and cannot be forged by the caller.
    const client = request.headers.get('cf-connecting-ip');
    if (client) headers.set('x-forwarded-for', client);

    const init = { method: request.method, headers, redirect: 'manual' };
    if (!BODYLESS_METHODS.has(request.method) && request.body) {
        init.body = request.body;
        // Required by the Fetch implementation when a stream is handed to it (Node's, and
        // the Workers runtime accepts it). Without it a punch's photo upload is rejected
        // before it leaves the zone.
        init.duplex = 'half';
    }

    let response;
    try {
        response = await fetch(target.toString(), init);
    } catch (err) {
        // The tunnel is down or the address is wrong - which is a fact about the deployment,
        // and is worth saying rather than reporting as an auth error on every screen.
        return problem(
            502,
            'api_unreachable',
            `The API at ${origin} could not be reached from this Worker ` +
                `(${(err && err.message) || err}). Check that the tunnel is running.`
        );
    }
    if (BODYLESS_STATUSES.has(response.status)) {
        return new Response(null, {
            status: response.status,
            statusText: response.statusText,
            headers: response.headers
        });
    }
    return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers
    });
}

async function serveFrontend(request, env) {
    if (!env || !env.ASSETS || typeof env.ASSETS.fetch !== 'function') {
        return problem(
            500,
            'worker_misconfigured',
            'This Worker has no ASSETS binding, so it cannot serve the frontend. Deploy the ' +
                '[assets] block from deploy/cloudflare/wrangler.toml with it.'
        );
    }
    // No rewriting, and no index.html fallback: the app routes in the fragment (``#/...``),
    // so a path that is not a file is a 404 the browser can report honestly, not a page.
    return env.ASSETS.fetch(request);
}
