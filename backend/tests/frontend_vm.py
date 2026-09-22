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
  from;
* the print dialog, recorded at the instant it opens, so a printable sheet can be checked
  while it is still on the page - a suite cannot read paper, but it can read what the page
  was showing when the dialog took over.

The shipped files are also loaded **once per process, at import**, before any suite runs: a
file that throws on the way in stops the run there with its own name and line. See
``check_scripts_load`` for why that is not a suite's job.

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
from typing import Final

from harness import PROJECT_ROOT

#: ``node``, or ``None`` when the machine has no Node. The suites mark themselves
#: ``skipif(NODE is None)``: a missing Node is not a broken frontend.
NODE = shutil.which("node")

FRONTEND = PROJECT_ROOT / "frontend"

#: Every file a browser ends up with, in the order it gets them. ``index.html`` itself lists
#: only the first four; the rest are fetched on demand - the console's module the first time
#: an administrator signs in, the two translation tables when a reader asks for one - and are
#: loaded by the harness so that in a test "shipped" and "fetched later" are as
#: distinguishable as they are in the browser. A suite that wants to drive the smaller session
#: a phone really has passes its own list instead (see ``run(scripts=...)``).
#:
#: The list lives here rather than in the prelude so that the import-time load check and the
#: harness's own fallback cannot disagree about what "the shipped files" are - the check names
#: these files when one of them will not load.
DEFAULT_SCRIPTS: Final = (
    "i18n.js",
    "i18n.ar.js",
    "i18n.hi.js",
    "i18n.ur.js",
    "frontendjavascript.js",
    "worker_modules.js",
    "admin_modules.js",
)

#: Everything the app needs before a suite's own code runs: the VM, the stub DOM, the
#: modelled window/localStorage/history/clipboard, and ``boot()``. One string, because it
#: is JavaScript - and because it has to stay the environment the browser gives these
#: files, not a convenient paraphrase of it.
PRELUDE = r"""
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const frontend = process.argv[2];
// The shipped files, filled in from Python's ``DEFAULT_SCRIPTS`` (see there for why the
// list is not written out twice), and overridable by a suite through argv[3].
const DEFAULT_SCRIPTS = __DEFAULT_SCRIPTS__;
const scripts = process.argv[3] ? process.argv[3].split(',') : DEFAULT_SCRIPTS;

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
    const element = {
        id,
        className: '',
        textContent: '',
        value: '',
        style: {},
        dataset: {},
        download: null,
        href: null,
        classList: makeClassList(),
        __clicked: false,
        addEventListener(type, handler) { (this.__handlers = this.__handlers || {})[type] = handler; },
        appendChild(child) { if (child) child.__owner = this; (this.__children = this.__children || []).push(child); },
        removeChild() {}, remove() {}, focus() {}, play() {},
        click() { this.__clicked = true; },
        setAttribute() {}, getAttribute() { return null; },
        querySelector() { return makeElement(id + '-child'); },
        firstElementChild: null
    };
    // ``innerHTML`` *builds* the child nodes, and the app leans on that: the modal attaches its
    // backdrop listener to ``firstElementChild`` the moment it has written the markup. A stub
    // that kept the string and left the tree empty made that a null dereference on every
    // dismissible dialog - a failure only a path no suite had taken could reach, and the help
    // modal (what a worker sees when the camera is refused) is exactly such a path. The markup
    // itself is still the assertion surface; what is modelled here is that writing it puts a
    // node in the tree, and writing an empty string takes it out again.
    let markup = '';
    Object.defineProperty(element, 'innerHTML', {
        enumerable: true,
        get() { return markup; },
        set(html) {
            markup = html === undefined || html === null ? '' : String(html);
            element.firstElementChild = markup ? makeElement(id + '-first-child') : null;
        }
    });
    // A media element's frame size, as a browser reports it: zero until a stream is attached,
    // then the size of the track behind it. The video this app captures from comes out of
    // ``getElementById`` - it is markup inside the camera card's HTML string, not a node
    // ``createElement`` ever saw - so the property is modelled on the element factory rather
    // than on a ``<video>`` this stub could recognise. Its *value* is what a browser gives,
    // which is the part that matters: a video with nothing on it has no frame to measure, and
    // that is exactly the check the shutter makes before it takes a photo.
    element.videoWidth = 0;
    element.videoHeight = 0;
    Object.defineProperty(element, 'srcObject', {
        enumerable: true,
        get() { return element.__stream || null; },
        set(stream) {
            element.__stream = stream || null;
            const track = stream && typeof stream.getVideoTracks === 'function'
                ? stream.getVideoTracks()[0]
                : null;
            const settings = track && typeof track.getSettings === 'function'
                ? track.getSettings()
                : null;
            element.videoWidth = (settings && settings.width) || 0;
            element.videoHeight = (settings && settings.height) || 0;
        }
    });
    return element;
}

function boot(options) {
    const opts = options || {};
    const elements = new Map();
    const requests = [];
    const anchors = [];
    // Every blob the page hands to an anchor: a saved file has to be readable back, or
    // "the download matches the table" is untestable.
    const blobs = [];
    // Every trip through the print dialog, recorded *at the moment it opens*: the only
    // thing a suite can honestly assert about a print is the state of the page while the
    // sheet is still on it, and ``window.print`` is that instant.
    const printed = [];
    const localStorage = makeStorage();
    const sessionStorage = makeStorage();
    if (opts.session) sessionStorage.setItem('user', JSON.stringify(opts.session));

    const document = {
        addEventListener() {},
        visibilityState: 'visible',
        // The tab's title, as ``index.html`` ships it. The print path borrows it for the
        // file name the dialog offers, and has to give it back afterwards.
        title: 'Al-Jehad - Site Attendance',
        // Attributes are *recorded*, not swallowed: ``lang`` and ``dir`` are set from the
        // chosen language, and "Urdu mirrors the layout" is otherwise unobservable in a
        // stub DOM - a suite cannot read text direction off a string.
        documentElement: {
            classList: { toggle() {}, add() {}, remove() {} },
            __attrs: {},
            setAttribute(name, value) { this.__attrs[String(name)] = String(value); }
        },
        // ``body`` collects what is appended to it, exactly like an element does: the
        // camera overlay and the toasts are appended there, and "how many are there?"
        // is the assertion - a no-op appendChild makes that unobservable. Its class list
        // is a real one for the same reason: the print path marks the body and the suite
        // has to be able to read that mark back.
        body: {
            style: {},
            classList: makeClassList(),
            __children: [],
            appendChild(child) { if (child) child.__owner = this; this.__children.push(child); },
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
            // ``remove()`` takes the element back out of whatever ``appendChild`` put it in.
            // The print sheet is built, printed and withdrawn, and "is it still in the page?"
            // is an assertion the suites make - a no-op remove would leave the next print
            // reading the previous sheet's markup.
            el.remove = () => {
                const owner = el.__owner;
                if (!owner || !owner.__children) return;
                const at = owner.__children.indexOf(el);
                if (at >= 0) owner.__children.splice(at, 1);
            };
            if (tag === 'canvas') {
                // The two canvases in this app: the shutter's, which turns a camera frame into
                // a JPEG, and the framing coach's 160px sample, which is read back as pixels.
                // A real canvas starts 300x150 and transparent black, holds whatever was drawn
                // into it, and is asked for a blob of its own size and a type of the caller's
                // choosing - all of which is modelled here, because the alternative is a
                // capture path every suite has to patch before it can run at all.
                el.width = 300;
                el.height = 150;
                let sensor = null;
                el.getContext = () => ({
                    drawImage(source) {
                        // A canvas resamples what it is given, and a VM has no resampler: what
                        // is remembered is *which* camera's picture was drawn, and the pixels
                        // are then asked for at whatever size the caller reads back - which
                        // is what scaling a frame amounts to, one sample at a time.
                        sensor = (source && source.srcObject && source.srcObject.__frame)
                            || cameraFrame;
                    },
                    fillRect() {},
                    clearRect() {},
                    putImageData() {},
                    getImageData(x, y, width, height) {
                        const w = Math.max(1, Math.round(width || el.width || 1));
                        const h = Math.max(1, Math.round(height || el.height || 1));
                        // Undrawn canvas, or a draw from something that is not a camera (an
                        // image, a video with no stream): transparent black, as a browser has.
                        const data = sensor
                            ? sensor(w, h)
                            : new Array(w * h * 4).fill(0);
                        return { data, width: w, height: h };
                    }
                });
                el.toBlob = (done, type, quality) => {
                    // What the app's contract turns on is the *frame*: its size, and the type
                    // the browser was asked to encode it as. A VM cannot write a JPEG, so the
                    // bytes are a statement of exactly those facts rather than an encoding -
                    // and nothing in this application ever reads them back: they are posted to
                    // the server, which is the only thing that could look inside.
                    const mime = type || 'image/png';
                    const stamp = 'modelled frame ' + el.width + 'x' + el.height + ' ' + mime
                        + (quality === undefined ? '' : ' q' + quality);
                    // Asynchronous, like the real one: the encode happens off the pixel path.
                    setTimeout(() => done(new Blob([stamp], { type: mime })), 0);
                };
            }
            return el;
        },
        getElementById(id) {
            // A browser resolves an id to the node in the document. The camera card is built
            // from an HTML string, appended to the body, and *then* given its id - and the
            // shipped ``closeCamera()`` finds it by that id to take it down again, so looking
            // it up in the wrong place made "the camera came down" untestable and left every
            // overlay on the page. Ids that only ever live in ``index.html`` (the toast root,
            // the modal root, the fields of a card's markup) are not nodes in this stub's
            // document at all, and those fall back to the by-id element a suite reads back.
            const attached = document.body.__children.find((node) => node && node.id === id);
            if (attached) return attached;
            if (!elements.has(id)) elements.set(id, makeElement(id));
            return elements.get(id);
        }
    };

    // The address bar, modelled: a fragment, a history that only ever replaces, and a
    // clipboard. Anything the page puts in the URL has to be readable back.
    const replacedUrls = [];
    const copiedUrls = [];
    // What the next ``navigator.clipboard.writeText`` answers: null resolves and the text is
    // recorded, a DOMException name rejects instead. A refused copy is a real state - the
    // clipboard API refuses when the document is not focused, and is *absent* on an insecure
    // origin - and it is the one the app answers with a prompt, so a stub whose clipboard
    // could only ever succeed would hide the branch it exists to fall back on.
    let clipboardRefusal = null;
    // Every ``prompt`` the page opened, recorded. The manual fallback is a dialog a person
    // has to read, and a stub that swallowed it would make "the copy fell back to asking"
    // unobservable - which is exactly the fallback above.
    const prompted = [];
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
        removeEventListener(type, handler) {
            const listeners = windowListeners[type] || [];
            const at = listeners.indexOf(handler);
            if (at >= 0) listeners.splice(at, 1);
        },
        setTimeout,
        // The browser's print dialog, modelled the same way the address bar is: what the
        // page had on it when the dialog opened is the assertion, so it is recorded here
        // rather than swallowed. Nothing is rendered: a suite cannot read paper.
        print() {
            printed.push({
                title: document.title,
                printing: document.body.classList.contains('is-printing-report'),
                sheet: document.body.__children
                    .filter((child) => child.className === 'print-sheet')
                    .map((child) => child.innerHTML).join('')
            });
        }
    };

    let responder = () => ({ status: 200, body: {} });

    // -- the push surfaces, and their seams -----------------------------------
    // ``requestedPermission`` is what the next ``requestPermission`` answers, so a suite
    // can drive "the worker accepted" and "the worker refused" from the outside.
    let notificationPermission = 'default';
    let requestedPermission = null;

    // ``Notification`` is a *constructor* in every browser, and the stub is the environment a
    // suite believes it is testing against - a bare object where a page has a function is a
    // difference no suite can see past. The app only ever reads the two statics below
    // (``permission`` looks live so a suite can set what comes next).
    function makeNotification() {
        function NotificationStub() {}
        Object.defineProperty(NotificationStub, 'permission', {
            get() { return notificationPermission; },
            enumerable: true,
        });
        NotificationStub.requestPermission = async () => {
            notificationPermission = requestedPermission || 'granted';
            return notificationPermission;
        };
        return NotificationStub;
    }
    // A registration records its scope; ``getRegistration`` answers what was stored. The
    // container starts null so ``'serviceWorker' in navigator`` style probes (and the
    // app's own feature test) see a browser without push support until it is installed.
    let serviceWorkerContainer = null;
    const registrations = [];
    function installServiceWorkerSupport() {
        serviceWorkerContainer = {
            async register(scriptURL) {
                const registration = {
                    scriptURL: String(scriptURL),
                    scope: '/',
                    pushManager: {
                        _subscription: null,
                        async getSubscription() { return this._subscription; },
                        async subscribe(options) {
                            const sub = {
                                endpoint: 'https://push.example.com/sub/' + (registrations.length + 1),
                                options,
                                keys: { p256dh: 'B-key-' + (registrations.length + 1), auth: 'auth-' + (registrations.length + 1) },
                                toJSON() { return { endpoint: this.endpoint, keys: this.keys }; },
                                async unsubscribe() { this.unsubscribed = true; return true; },
                            };
                            this._subscription = sub;
                            return sub;
                        },
                    },
                };
                registrations.push(registration);
                return registration;
            },
            async getRegistration() { return registrations[registrations.length - 1] || null; },
            _messageListeners: [],
            addEventListener(type, handler) { if (type === 'message') this._messageListeners.push(handler); },
            // The worker -> page channel: ``notificationclick`` in the real worker posts
            // this, and the page marks the notice read. The suite calls it directly.
            async fireMessage(data) { for (const handler of this._messageListeners) handler({ data }); },
        };
        // The key appears only now: before this call, ``'serviceWorker' in navigator``
        // is false, exactly as on a browser with no push support.
        vmContext.navigator.serviceWorker = serviceWorkerContainer;
        return serviceWorkerContainer;
    }

    // The picture the modelled camera produces. A device that hands out a stream but no
    // frames is only half a device: the shutter has nothing to encode and the framing coach
    // has nothing to measure, so both paths would be untestable for the want of a picture.
    // The default is a flat mid-grey frame - a lens cap, or a wall in shadow - and it is
    // settable, so a suite can give the camera a frame with contrast in it and watch the
    // coach's rules run on pixels that really did come through the device.
    let cameraFrame = (width, height) => {
        const data = new Array(width * height * 4).fill(128);
        for (let at = 3; at < data.length; at += 4) data[at] = 255;
        return data;
    };

    // -- the camera and the location fix, and their seams ---------------------
    // Absent until installed, and modelled only once installed, for the reason the service
    // worker is: the app decides which branch a phone gets from a feature test, so a surface
    // that was always there - even a generous one - would be answering that test with the
    // stub's opinion instead of the browser's. Installed, it is drivable from the suite: the
    // fix or the error, the stream or the refusal, are all set from outside.
    const geolocationCalls = [];
    let geolocationFix = { latitude: 30.05, longitude: 31.23, accuracy: 12 };
    let geolocationError = null;
    function installGeolocationSupport() {
        vmContext.navigator.geolocation = {
            getCurrentPosition(success, error, options) {
                geolocationCalls.push({ options: options || null });
                if (geolocationError) {
                    if (error) error(geolocationError);
                    return;
                }
                success({
                    coords: {
                        latitude: geolocationFix.latitude,
                        longitude: geolocationFix.longitude,
                        accuracy: geolocationFix.accuracy
                    },
                    timestamp: Date.now()
                });
            }
        };
        return vmContext.navigator.geolocation;
    }

    const cameraCalls = [];
    const cameraTracks = [];
    let cameraRefusal = null;
    function installCameraSupport() {
        vmContext.navigator.mediaDevices = {
            async getUserMedia(constraints) {
                cameraCalls.push(constraints || {});
                if (cameraRefusal) {
                    const error = new Error('the stub camera refused the request');
                    error.name = cameraRefusal;
                    throw error;
                }
                const track = {
                    kind: 'video',
                    label: 'stub camera',
                    stopped: false,
                    stop() { this.stopped = true; },
                    getSettings() { return { facingMode: 'user', width: 720, height: 960 }; }
                };
                cameraTracks.push(track);
                // The stream carries the track the app is responsible for releasing, and a
                // pointer to the sensor behind it - so a canvas that draws *this* stream gets
                // this camera's pixels. Nothing else: a stub that also offered a snapshot of
                // its own would be standing in for the part of the app under test.
                return {
                    getTracks: () => [track],
                    getVideoTracks: () => [track],
                    __frame: (width, height) => cameraFrame(width, height)
                };
            }
        };        return vmContext.navigator.mediaDevices;
    }

    const vmContext = vm.createContext({
        console, setTimeout, clearTimeout, TextEncoder, TextDecoder, FormData, Blob, URLSearchParams,
        Date,
        // Node's WebCrypto, so ``crypto.getRandomValues`` is exercised for real instead
        // of the generator's fallback branch being the only one ever covered.
        crypto: require('crypto').webcrypto,
        setInterval: () => 1, clearInterval() {},
        URL: { createObjectURL: (blob) => { blobs.push(blob); return 'blob:stub'; }, revokeObjectURL() {} },
        // ``atob``/``btoa``: the push settings decode the VAPID public key from
        // URL-safe base64 into bytes the browser's ``PushManager.subscribe`` wants.
        // Node has both on the global since v16, so this is passing the real ones
        // through rather than modelling them.
        atob, btoa, Uint8Array,
        // The Notification permission surface, recorded: ``requestPermission`` is what
        // the enable switch calls, and "the worker said no" is one of the states a
        // suite has to be able to drive. ``Notification.permission`` starts at
        // ``default`` exactly as a first visit does.
        Notification: makeNotification(),
        window, document, history, location, localStorage, sessionStorage,
        navigator: {
            onLine: true, userAgent: 'node', platform: 'linux', maxTouchPoints: 0,
            // *Present* as modelled, and deliberately the opposite choice from the service
            // worker, the camera and the location below: this environment declares
            // ``isSecureContext: true``, and a secure context is exactly where the async
            // clipboard exists. The app's guard (``navigator.clipboard && writeText``) is
            // therefore a fallback test rather than a device a suite has to install, and a
            // copy that succeeded is what the console suites read back off ``copiedUrls``.
            clipboard: {
                writeText: (text) => {
                    if (clipboardRefusal) {
                        const error = new Error('the stub clipboard refused the write');
                        error.name = clipboardRefusal;
                        return Promise.reject(error);
                    }
                    copiedUrls.push(String(text));
                    return Promise.resolve();
                }
            },
            // The device surfaces - ``serviceWorker``, ``mediaDevices``, ``geolocation`` -
            // are *absent* until their ``install*Support()`` seam adds them, because "this
            // browser has no camera / no GPS / cannot push" is a state the app is built to
            // survive rather than one the stub should hide: the app decides which branch a
            // phone gets from a feature test of its own (``'serviceWorker' in navigator``,
            // ``navigator.mediaDevices && getUserMedia``, ``'geolocation' in navigator``),
            // and an always-present property answers that test "yes" with nothing behind it.
        },
        alert(message) { throw new Error('unexpected alert: ' + message); },
        confirm: () => true,
        prompt: (message, defaultValue) => {
            prompted.push({
                message: String(message),
                defaultValue: defaultValue === undefined ? null : String(defaultValue)
            });
            return null;
        },
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
        vm.runInContext(fs.readFileSync(path.join(frontend, name), 'utf8'), vmContext, { filename: name });
    }

    return {
        evaluate(expression) { return vm.runInContext(expression, vmContext); },
        setResponder(fn) { responder = fn; },
        requests,
        replacedUrls,
        copiedUrls,
        blobs,
        printed,
        lastAnchor: () => anchors[anchors.length - 1] || null,
        // The browser telling the page something happened that the page does not cause:
        // today, ``afterprint`` - the dialog has closed.
        fireWindowEvent(type) {
            for (const handler of windowListeners[type] || []) handler({ type });
        },
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
        },
        // -- the push seams ---------------------------------------------------
        installServiceWorkerSupport,
        registrations,
        get serviceWorkerContainer() { return serviceWorkerContainer; },
        // What the NEXT ``Notification.requestPermission()`` answers ('granted' or
        // 'denied'); null leaves the default of granting.
        setRequestedPermission(value) { requestedPermission = value; },
        get notificationPermission() { return notificationPermission; },
        // -- the device seams -------------------------------------------------
        installGeolocationSupport,
        // The fix the next ``getCurrentPosition`` hands back, and the error it hands back
        // instead once one is set (``{ code, message }``, read the way the app reads a
        // GeolocationPositionError). Both are the suite's to choose, because "the worker is
        // standing on site" and "the worker refused the prompt" are answers this page acts
        // on differently.
        setGeolocationFix(fix) { geolocationFix = Object.assign({}, geolocationFix, fix || {}); },
        setGeolocationError(error) { geolocationError = error || null; },
        geolocationCalls,
        installCameraSupport,
        // The DOMException name the next ``getUserMedia`` rejects with (``NotAllowedError``
        // for a camera the person refused), or null for a camera that grants.
        setCameraRefusal(name) { cameraRefusal = name || null; },
        cameraCalls,
        cameraTracks,
        // What the modelled camera sees: ``(width, height) => RGBA``. A suite that needs a
        // frame the coach can read - contrast in it, a face-sized bright patch - replaces the
        // default flat one through here, which is the only way the coach's rules can be run
        // against pixels that arrived through the device.
        setCameraFrame(describe) {
            if (typeof describe === 'function') cameraFrame = describe;
            return cameraFrame;
        },
        get cameraFrame() { return cameraFrame; },
        // -- the clipboard seam -----------------------------------------------
        // The DOMException name the next ``writeText`` rejects with, or null for one that
        // accepts: what the app does with a refusal - ask the person to copy by hand - is
        // only reachable if the refusal can be driven.
        setClipboardRefusal(name) { clipboardRefusal = name || null; },
        prompted,
        };
}
""".replace("__DEFAULT_SCRIPTS__", json.dumps(list(DEFAULT_SCRIPTS)))

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


def _execute(
    source: str,
    frontend_dir: Path,
    scripts: list[str] | None,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Run one harness file in Node and hand back the process, unexamined.

    Shared by ``run`` and the import-time load check so both get the same command, the same
    decode and the same temp-dir handling - the two differ only in what they make of the
    result, and a check that quietly ran a *different* command would be worthless.
    """
    with tempfile.TemporaryDirectory(prefix="frontend_vm_") as folder:
        harness = Path(folder) / "harness.js"
        harness.write_text(source, encoding="utf-8")
        command = [NODE, str(harness), str(frontend_dir)]
        if scripts is not None:
            command.append(",".join(scripts))
        return subprocess.run(
            command,
            capture_output=True, timeout=timeout,
            # Node writes UTF-8. Decoding with the console's locale encoding instead
            # (cp1252 on a default Windows box) would mangle every Arabic, Hindi or
            # typographic character in the rendered page, and the suite would be
            # comparing mojibake against the source it just read.
            encoding="utf-8", errors="replace",
        )


def run(
    body: str,
    frontend_dir: Path | None = None,
    *,
    timeout: int = 180,
    scripts: list[str] | None = None,
) -> dict:
    """Run ``body`` against the real frontend files and return its ``results`` object.

    ``scripts`` narrows the files the context starts with, for suites about what a
    session does *not* have yet (a worker's payload has no console module in it).

    A file that cannot be *loaded* is not this function's business: it is checked once when
    this module is imported (see ``check_scripts_load``), so a non-zero exit here means a
    suite's own scenarios threw.
    """
    completed = _execute(script(body), frontend_dir or FRONTEND, scripts, timeout)
    assert completed.returncode == 0, f"frontend harness failed:\n{completed.stderr}"
    if not completed.stdout.strip():
        # A scenario that never settles is the one failure this process reports badly: an
        # ``await`` on a promise nobody resolves leaves Node with an empty event loop, so it
        # exits *successfully* having printed nothing - no stack, no result, and (without this)
        # a JSONDecodeError about an empty string. Naming it here is the difference between a
        # puzzle and a line number: the usual culprit is an await on a stubbed surface that
        # this environment does not model, which returns undefined or never calls back.
        raise AssertionError(
            "the frontend harness printed no results and exited zero, which is what a scenario "
            "that never settles looks like: Node's event loop emptied, so the epilogue never "
            "ran. Look for an await on something this environment does not model - a device or "
            "a promise the stub never settles.\n"
            f"stderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


class FrontendFailedToLoad(RuntimeError):
    """A shipped frontend file threw while it was being loaded.

    Raised when this module is imported, so the message - which leads with the file and the
    line Node reported - arrives before any suite runs, instead of as a mystery inside
    whichever suite happened to boot the environment first.
    """


#: The smallest harness that only *loads* the shipped files: ``boot()`` is what reads and runs
#: them, and this body asks nothing of it afterwards. ``results`` has to be declared because
#: the epilogue prints it - that, and not any assertion, is what makes the process exit 0.
_LOAD_PROBE = "const results = {};\nboot();"


#: What the import-time check loaded, in order; empty when Node is absent. A suite can assert
#: this instead of re-running the check - it is the evidence that the load happened *here*,
#: once, rather than inside a suite.
SHIPPED_SCRIPTS_LOADED: tuple[str, ...] = ()


def _load_failure_message(stderr: str, frontend_dir: Path, scripts: tuple[str, ...]) -> str:
    """Why this failed, what threw, and which files were being loaded when it did.

    The first line is Node's own ``<file>:<line>``, which the harness gets because it passes
    ``filename`` to ``vm.runInContext``. Quoting it first means the header of a pytest report
    names the file and the line, rather than saying "the harness failed" about a harness that
    is working perfectly and a frontend file that is not.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    where = lines[0] if lines else "(node reported nothing)"
    return (
        f"a shipped frontend file did not load: {where}\n"
        f"\n"
        f"{(stderr or '').strip()}\n"
        f"\n"
        "Every frontend suite loads these files before it asserts anything, so a file that "
        "cannot be loaded makes each of them fail somewhere different - which reads as a "
        "mystery in whichever suite happens to boot first, and as several mysteries when it "
        "is several suites. So they are loaded once, here, at import.\n"
        f"\n"
        f"in load order, from {frontend_dir}:\n"
        + "".join(f"  {name}\n" for name in scripts)
    )


def check_scripts_load(
    *,
    frontend_dir: Path | None = None,
    scripts: list[str] | None = None,
    timeout: int = 180,
) -> tuple[str, ...]:
    """Load the shipped files once, and raise ``FrontendFailedToLoad`` if any of them throws.

    This is the environment's own smoke test, and it runs at import (bottom of this module)
    rather than in a fixture: the files are loaded once per *process*, so "the frontend does
    not parse" is a property of the whole run and not of one suite. A syntax error, a stray
    reference at the top level of a file, or a file that ``index.html`` lists but that is not
    on disk all surface here, with the file and the line, and with no suite having run at all.

    Returns the names it loaded, in order, so the caller can record what it checked. A machine
    with no Node returns ``()`` and checks nothing: a missing Node is not a broken frontend.
    """
    if NODE is None:
        return ()
    loaded = tuple(scripts or DEFAULT_SCRIPTS)
    target = frontend_dir or FRONTEND
    completed = _execute(script(_LOAD_PROBE), target, list(loaded), timeout)
    if completed.returncode != 0:
        raise FrontendFailedToLoad(_load_failure_message(completed.stderr, target, loaded))
    return loaded


# ---------------------------------------------------------------------------
# the check, and where it is run
# ---------------------------------------------------------------------------
# At import, deliberately, and not in a fixture. The alternative - leave it to the first
# suite that boots - is what this module did before, and it is how a one-line syntax error in
# ``frontendjavascript.js`` arrived as an ``AssertionError`` in ``test_frontend_branding``
# (or whichever suite ran first that day) with a stack pointing into a temp file named
# ``harness.js``. Importing this module is what every frontend suite has in common, so it is
# the one place the failure is about the frontend rather than about the suite that noticed.
if NODE is not None:
    SHIPPED_SCRIPTS_LOADED = check_scripts_load()
