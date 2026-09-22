/**
 * The one place the API base URLs live.
 *
 * Loaded first by every page (``index.html``, ``enroll.html``, ``quick.html``), before any
 * script that talks to the network. Everything downstream - ``API.baseURL`` in
 * ``frontendjavascript.js``, the standalone link pages, the offline queue - resolves its
 * base through here, so there is exactly one file to edit when the deployment moves.
 *
 * Resolution order (highest wins):
 *   1. ``window.API_BASE_URL`` set BEFORE this file loads - the deploy-time switch.
 *   2. ``localStorage.apiBaseURL`` - a per-browser operator override.
 *   3. The page's own origin + ``/api/v1`` - ``serve.py``, the Worker's ASSETS binding,
 *      a LAN address, a tunnel: whenever the page and the API share an origin, which is
 *      the deployment's declared design (the CSP says ``connect-src 'self'``).
 *   4. The production Worker base - for a page detached from any deployment: opened from
 *      disk, or hosted on a static dev server (VS Code Live Server).
 *
 * Endpoint paths are appended WITHOUT the ``/api/v1`` prefix; this module owns the prefix.
 */
(function (global) {
    "use strict";

    var URLS = {
        /** Production entry point: the Cloudflare Worker fronts the FastAPI backend. */
        production: "https://al-jehad1.abdallahtamet281.workers.dev/api/v1",
        /** Direct origin, behind the Worker. Diagnostics only, never default traffic. */
        fallback: "https://al-jehad-production.up.railway.app/api/v1"
    };

    global.API_BASE_URLS = URLS;
    // The deploy-time switch: whoever hosts the page can repoint every client by setting
    // this one global before this file loads. Unset, it is the production Worker.
    if (!global.API_BASE_URL) global.API_BASE_URL = URLS.production;
    // The diagnostics switch, in the same spirit: API.useFallback() in the console moves
    // traffic straight to Railway for one session, bypassing the Worker entirely.
    if (!global.API_FALLBACK_BASE_URL) global.API_FALLBACK_BASE_URL = URLS.fallback;

    /** Static dev servers that host the frontend folder on its own origin. */
    var STATIC_DEV_SERVER_PORTS = { "5500": true, "5501": true };

    /**
     * Normalises a base URL: trimmed, no trailing slash, and the ``/api/v1`` prefix kept
     * exactly once - so a configured value ending in ``/`` or ``/api/v1/`` cannot join
     * into ``...//attendance`` or ``/api/v1/api/v1/...``.
     */
    global.normalizeAPIBaseURL = function (candidate) {
        var url = String(candidate || "").trim();
        if (!url) return "";
        url = url.replace(/\/+$/, "");
        // Already ends with the prefix (any version): keep it, once.
        if (/(^|\/)api\/v\d+$/.test(url)) return url;
        // A value that carries the prefix somewhere mid-path (a pasted full endpoint):
        // cut back to the prefix so the caller's path is appended, not doubled.
        var cut = url.match(/^(https?:\/\/[^/]+)\/api\/v\d+(?=\/|$)/);
        if (cut) return cut[0];
        return url;
    };

    /**
     * Resolves the base URL for the page this runs on. See the order at the top of the
     * file. ``loc`` is injectable (tests, diagnostics): anything shaped like
     * ``window.location``.
     */
    global.resolveAPIBase = function (loc) {
        loc = loc || global.location;
        var configured = global.normalizeAPIBaseURL(global.API_BASE_URL || "");
        var stored = null;
        try { stored = global.localStorage && global.localStorage.getItem("apiBaseURL"); } catch (err) { stored = null; }
        if (stored) return global.normalizeAPIBaseURL(stored);
        var origin = loc && loc.origin ? loc.origin : "";
        var protocol = loc && loc.protocol ? loc.protocol : "";
        var port = loc && loc.port ? String(loc.port) : "";
        // A page opened from disk has no origin that fronts anything: production it is.
        if (protocol === "file:") return configured || global.normalizeAPIBaseURL(URLS.production);
        // The deployment serves its own page and API from one origin, whatever the port.
        if (!STATIC_DEV_SERVER_PORTS[port]) return global.normalizeAPIBaseURL(origin + "/api/v1");
        // A static dev server: the API is elsewhere, so point at production.
        return configured || global.normalizeAPIBaseURL(URLS.production);
    };
})(typeof window !== "undefined" ? window : globalThis);
