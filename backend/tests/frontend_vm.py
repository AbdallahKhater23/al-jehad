"""Run the real frontend files in a Node VM, against a stubbed DOM.

WHY THIS EXISTS
---------------
Some behaviour only exists in the browser: which endpoint a tab calls, what it puts on
screen, what a button does with the response, and what the URL ends up saying. A backend
test cannot see any of that, and neither can a person clicking through the console three
months from now.

So these suites load the same files ``index.html`` loads, in the same order, in one shared
scope, with:

* a DOM whose ``innerHTML`` is a string the test can read back (the app repaints whole
  views from strings, so the rendered HTML *is* the assertion surface);
* a ``fetch`` that records every request and answers from a per-test responder;
* a modelled address bar - a fragment, a history that only replaces, a clipboard - for
  the parts of the app that live in the URL;
* blobs and anchors, so a download can be read back and compared with the table it came
  from.

Each test file supplies the *middle*: its own fake API answers and a script that drives
the UI into a ``results`` object. This module supplies the two ends (the environment, and
the epilogue that prints the result) so a suite does not carry 200 lines of DOM stub to
assert one screen.

Usage::

    import frontend_vm

    BODY = r\"\"\"
    // fake API answers + scenarios; declares ``const results = {}`` and fills it in
    \"\"\"

    @pytest.fixture(scope="module")
    def results():
        return frontend_vm.run(BODY)

Node is optional: check :data:`NODE` and skip when it is ``None``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from harness import PROJECT_ROOT

#: ``node``, or ``None`` when the machine has no Node. The suites mark themselves
#: ``skipif(NODE is None)``: a missing Node is not a broken frontend.
NODE = shutil.which("node")

FRONTEND = PROJECT_ROOT / "frontend"

#: Everything the app needs before a suite's own code runs: the VM, the stub DOM, the
#: modelled window/localStorage/history/clipboard, and ``boot()``. One string, because it
#: is JavaScript - and because it has to stay the environment the browser gives these
#: files, not a convenient paraphrase of it.
PRELUDE = r"""
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const frontend = process.argv[2];
// Every file ``index.html`` loads, in the same order. ``worker_modules.js`` is the
// worker half of the app (the notes tab lives there), and a suite that could not reach
// it would be testing a console nobody's phone ever runs.
const scripts = ['i18n.js', 'frontendjavascript.js', 'worker_modules.js', 'admin_modules.js'];

function makeStorage() {
    const map = new Map();
    return {
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => { map.set(String(k), String(v)); },
        removeItem: (k) => { map.delete(k); },
        clear: () => map.clear()
    };
}

function makeClassList() {
    const names = new Set();
    return {
        add(name) { names.add(name); },
        remove(name) { names.delete(name); },
        contains(name) { return names.has(name); },
        toggle(name, force) {
            const wanted = force === undefined ? !names.has(name) : !!force;
            if (wanted) names.add(name); else names.delete(name);
            return wanted;
        }
    };
}

function makeElement(id) {
    return {
        id,
        className: '',
        innerHTML: '',
        textContent: '',
        value: '',
        style: {},
        dataset: {},
        download: null,
        href: null,
        classList: makeClassList(),
        __clicked: false,
        addEventListener(type, handler) { (this.__handlers = this.__handlers || {})[type] = handler; },
        appendChild(child) { (this.__children = this.__children || []).push(child); },
        removeChild() {}, remove() {}, focus() {}, play() {},
        click() { this.__clicked = true; },
        setAttribute() {}, getAttribute() { return null; },
        querySelector() { return makeElement(id + '-child'); },
        firstElementChild: null
    };
}

function boot(options) {
    const opts = options || {};
    const elements = new Map();
    const requests = [];
    const anchors = [];
    // Every blob the page hands to an anchor: a saved file has to be readable back, or
    // "the download matches the table" is untestable.
    const blobs = [];
    const localStorage = makeStorage();
    const sessionStorage = makeStorage();
    if (opts.session) sessionStorage.setItem('user', JSON.stringify(opts.session));

    const document = {
        addEventListener() {},
        visibilityState: 'visible',
        documentElement: { classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {} },
        // ``body`` collects what is appended to it, exactly like an element does: the
        // camera overlay and the toasts are appended there, and "how many are there?"
        // is the assertion - a no-op appendChild makes that unobservable.
        body: {
            style: {},
            classList: { toggle() {} },
            __children: [],
            appendChild(child) { this.__children.push(child); },
            removeChild(child) {
                const at = this.__children.indexOf(child);
                if (at >= 0) this.__children.splice(at, 1);
            }
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        createElement(tag) {
            const el = makeElement(tag);
            if (tag === 'a') anchors.push(el);
            return el;
        },
        getElementById(id) { if (!elements.has(id)) elements.set(id, makeElement(id)); return elements.get(id); }
    };

    // The address bar, modelled: a fragment, a history that only ever replaces, and a
    // clipboard. Anything the page puts in the URL has to be readable back.
    const replacedUrls = [];
    const copiedUrls = [];
    const windowListeners = {};
    const location = {
        protocol: 'http:', hostname: 'localhost', port: '8000',
        origin: 'http://localhost:8000', href: 'http://localhost:8000/', pathname: '/', search: '',
        hash: opts.hash || ''
    };
    const history = {
        replaceState(state, title, url) {
            replacedUrls.push(String(url));
            location.hash = String(url);
            window.location.href = 'http://localhost:8000/' + String(url);
        }
    };
    const window = {
        location: location,
        isSecureContext: true,
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        addEventListener(type, handler) { (windowListeners[type] = windowListeners[type] || []).push(handler); },
        setTimeout
    };

    let responder = () => ({ status: 200, body: {} });
    const context = vm.createContext({
        console, setTimeout, clearTimeout, TextEncoder, TextDecoder, FormData, Blob, URLSearchParams,
        Date,
        // Node's WebCrypto, so ``crypto.getRandomValues`` is exercised for real instead
        // of the generator's fallback branch being the only one ever covered.
        crypto: require('crypto').webcrypto,
        setInterval: () => 1, clearInterval() {},
        URL: { createObjectURL: (blob) => { blobs.push(blob); return 'blob:stub'; }, revokeObjectURL() {} },
        window, document, history, location, localStorage, sessionStorage,
        navigator: {
            onLine: true, userAgent: 'node', platform: 'linux', maxTouchPoints: 0,
            clipboard: { writeText: (text) => { copiedUrls.push(String(text)); return Promise.resolve(); } }
        },
        alert(message) { throw new Error('unexpected alert: ' + message); },
        confirm: () => true,
        prompt: () => null,
        fetch: async (url, init) => {
            const headers = (init && init.headers) || {};
            requests.push({ url: String(url), headers, method: (init && init.method) || 'GET', body: (init && init.body) });
            const answer = responder(String(url), init);
            return {
                ok: answer.status < 400,
                status: answer.status,
                json: async () => answer.body,
                blob: async () => ({ size: 1, type: answer.contentType || 'text/csv' })
            };
        }
    });
    window.window = window;

    for (const name of scripts) {
        vm.runInContext(fs.readFileSync(path.join(frontend, name), 'utf8'), context, { filename: name });
    }

    return {
        evaluate(expression) { return vm.runInContext(expression, context); },
        setResponder(fn) { responder = fn; },
        requests,
        replacedUrls,
        copiedUrls,
        blobs,
        lastAnchor: () => anchors[anchors.length - 1] || null,
        // The text of the most recently saved blob (the file the page just wrote).
        async lastBlobText() {
            const blob = blobs[blobs.length - 1];
            return blob ? await blob.text() : null;
        },
        // A browser changing the fragment without a reload - what a pasted link or the
        // back button does, and the only navigation this single-page app has.
        fireHashChange(hash) {
            location.hash = hash;
            for (const handler of windowListeners.hashchange || []) handler({ type: 'hashchange' });
        }
    };
}
"""

_EPILOGUE = """
    process.stdout.write(JSON.stringify(results));
})().catch((err) => {
    console.error((err && err.stack) || String(err));
    process.exit(1);
});
"""


def script(body: str) -> str:
    """The shared environment, a suite's scenarios, and the exit that prints them.

    The body is the *inside* of an async IIFE: it declares ``const results = {}``, drives
    the UI, and fills ``results`` in. Returning it is this module's job, so a suite never
    has to remember the ``process.stdout.write`` contract.
    """
    return PRELUDE + "\n(async () => {\n" + body + "\n" + _EPILOGUE


def run(body: str, frontend_dir: Path | None = None, *, timeout: int = 180) -> dict:
    """Run ``body`` against the real frontend files and return its ``results`` object."""
    source = script(body)
    with tempfile.TemporaryDirectory(prefix="frontend_vm_") as folder:
        harness = Path(folder) / "harness.js"
        harness.write_text(source, encoding="utf-8")
        completed = subprocess.run(
            [NODE, str(harness), str(frontend_dir or FRONTEND)],
            capture_output=True, timeout=timeout,
            # Node writes UTF-8. Decoding with the console's locale encoding instead
            # (cp1252 on a default Windows box) would mangle every Arabic, Hindi or
            # typographic character in the rendered page, and the suite would be
            # comparing mojibake against the source it just read.
            encoding="utf-8", errors="replace",
        )
    assert completed.returncode == 0, f"frontend harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)
