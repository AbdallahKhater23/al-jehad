"""The frontend VM's own contract: what its stub browser has, and what it must never invent.

WHY THIS EXISTS
---------------
``frontend_vm`` is one environment shared by 29 suites (361 tests): the same prelude, the same
stub DOM, the same ``boot()``. That sharing is the point - every suite drives the real
frontend files in something shaped like a browser - and it is also the hazard. A change to the
*stub* that makes the environment wrong breaks all of them at once, and each failure arrives in
a suite that is about something else entirely, reading as ``frontend harness failed: TypeError
...``. When this last happened the culprit was one line: the navigator stub kept
``serviceWorker`` as an always-present property, so ``'serviceWorker' in navigator`` - the
probe the push feature branches on - answered *yes* with nothing behind it.

So the stub's rules are stated here, in their own suite, because they are not the push
feature's rules: they are the environment every frontend suite runs in. A push edit that
assumes a browser with service workers fails here first, by name, instead of surfacing as a
mystery in whichever suite happened to run next.

WHAT IS PINNED
--------------
* **absence is real absence.** A plain ``boot()`` has no ``navigator.serviceWorker`` at all -
  not an own property, not a getter, not ``null`` behind a true probe - so the app's own
  feature test answers false;
* **the app boots on that browser.** ``UI.init()`` completes, the push channel lands in its
  named unsupported state, nothing is asked of the server, and nobody is prompted. This is the
  crash the contract exists to make local: the line a push edit is most likely to add
  unguarded throws a ``TypeError`` in this environment, and the suite that adds it should be
  the suite that finds out;
* **installing support is the one switch.** ``installServiceWorkerSupport()`` is what makes the
  probe true, and the container behind it behaves like a ``ServiceWorkerContainer``: no
  registration until one is made, then the script URL and scope are remembered;
* **every other capability the app feature-tests is pinned as the one thing it is here.** Two
  are *present as modelled*, because a browser in this environment - a secure context on a
  page that always has a print dialog - always has them: the clipboard records what was copied,
  resolves, and can also be *refused*, since that refusal is the branch the app falls back to a
  prompt for; and ``window.print()`` records the page at the instant the dialog opens, with the
  stub taking none of the app's cleanup on itself. The other two - the camera and the location
  fix - are *absent until installed*, like the service worker, because the app decides which
  branch a phone gets from a feature test of its own, and a device that was always there would
  answer that test with the stub's opinion instead of the browser's;
* **an installed device is drivable, through the app's own path.** ``Camera.start()`` and
  ``Location.current()`` - not a stubbed method - are what a suite runs against the modelled
  device, so what is asserted is the code that ships: the constraints it asks for, the track it
  has to release, the options it passes a phone's GPS, and the browser's own error name when
  the person refuses;
* **the device produces a picture, and the DOM can hold one.** A video reports the size of the
  stream put on it (zero before that, which is the check the shutter makes), a canvas is 300x150
  and transparent until it is drawn into, and then encodes - or reads back - the frame the
  camera gave it, at whatever size the caller asks for. Without this half, the app's capture
  step cannot run in this environment at all, and each suite that wants it patches the DOM
  first: the modelling is what those patches were standing in for;
* **the tree the page builds at runtime exists.** An id resolves to the node that was appended
  and not to a stand-in, and writing ``innerHTML`` puts a child in the tree - so the shipped
  ``closeCamera()`` can take the camera card down and ``Modal.open`` can attach its listener to
  the backdrop it has just written. A stub that failed either was a stub whose own gaps silently
  decided what the suites could see;
* **two boots are two browsers.** Nothing leaks from a suite that installed support into one
  that needs the absence - the property is per boot, not per process;
* **the environment stays one a browser could give.** ``push-service-worker.js`` is never
  loaded *into* it: the file expects ``self``, which a page does not have, and a worker file
  added to the loaded list would take the whole prelude down;
* **a crash names itself.** The harness prints the stack it caught, so a failure points at the
  line that threw instead of ending as a bare non-zero exit - and a scenario that never
  *returns* is named too, by ``run``, because an ``await`` on a promise nobody settles exits
  successfully having printed nothing at all;
* **the shipped files are loaded before any suite runs.** Importing ``frontend_vm`` loads them
  once, so a file that throws on the way in fails *there*, by file and line - rather than in
  whichever suite happened to boot the environment first.

Node is optional: without it these skip, like every other frontend suite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import frontend_vm
import pytest

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
const results = {};

// The worker file's own text, handed in by the suite below as a JavaScript string literal
// (the suite substitutes it), so the *context* can be made to run it later - which is exactly
// what a script list would do to it.
const WORKER_SOURCE = SOURCE_PLACEHOLDER;

// Every await against a modelled device goes through this, because a device that hands back a
// promise nobody settles is the one stub mistake this process reports worst: Node's event loop
// empties, it exits *successfully* having printed nothing, and the failure arrives as an empty
// result rather than as a name. A second is longer than any of these answers takes, so a
// timeout here is only ever a surface that stopped answering - and it is recorded, so a suite
// can say which one.
const hangs = [];
const NO_ANSWER = 'never answered';
async function within(promise, label) {
    let timer = null;
    const guard = new Promise((resolve) => {
        timer = setTimeout(() => {
            hangs.push(label);
            resolve(NO_ANSWER + ': ' + label);
        }, 1000);
    });
    try {
        return await Promise.race([promise, guard]);
    } finally {
        clearTimeout(timer);
    }
}

// 1. The browser the app has to survive: no service worker anywhere in it.
{
    const env = boot();
    results.absent = {
        // The probe the app branches on. ``in`` walks the prototype chain, so a stub that
        // kept a getter - or an inherited property - would answer true with nothing behind
        // it, which is the exact failure this file exists to catch.
        probe: env.evaluate("'serviceWorker' in navigator"),
        own: env.evaluate("Object.prototype.hasOwnProperty.call(navigator, 'serviceWorker')"),
        type: env.evaluate("typeof navigator.serviceWorker"),
        container: env.serviceWorkerContainer,
        // The unguarded line a push edit is most likely to write, and what it does here.
        // This is the crash that would otherwise surface in 29 other suites.
        unguarded: env.evaluate("(function () {\n"
            + "    try { navigator.serviceWorker.addEventListener('message', function () {}); return 'no-throw'; }\n"
            + "    catch (err) { return err.name + ': ' + err.message; }\n"
            + "})()"),
    };
}

// 2. ``Notification`` is a different surface, and a browser can have it without push support:
//    the app asks the *server* whether pushing is configured before it ever asks the browser
//    for permission, so the stub must not conflate the two.
{
    const env = boot();
    results.notification = {
        // A page's ``Notification`` is a constructor; the two statics below are the whole of
        // what this app reads off it.
        type: env.evaluate("typeof Notification"),
        ask: env.evaluate("typeof Notification.requestPermission"),
        permission: env.notificationPermission,
        // Reading the permission never prompts: asking is the app's decision, and the stub
        // must not decide it on the app's behalf.
        after_reading_it: env.notificationPermission,
    };
}

// 3. The app boots on that browser and names the state it is in.
{
    const env = boot();
    env.setResponder(() => ({ status: 200, body: {} }));
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    env.evaluate("State.saveUser({ id: '601', name: 'Bilal Khan', role: 'worker', token: 'tok-worker' })");
    let crashed = null;
    try {
        await env.evaluate("UI.init()");
    } catch (err) {
        crashed = String((err && err.stack) || err);
    }
    // ``initPush`` decides the unsupported state synchronously and the app does not await it,
    // so one turn is all this needs to be sure it ran.
    await new Promise((resolve) => setTimeout(resolve, 0));
    results.boot = {
        crashed,
        reason: env.evaluate("WORKER_MODULES._pushConfig && WORKER_MODULES._pushConfig.reason"),
        supported: env.evaluate("WORKER_MODULES._pushConfig && WORKER_MODULES._pushConfig.supported"),
        subscription: env.evaluate("WORKER_MODULES._pushSubscription"),
        card: env.evaluate("WORKER_MODULES.pushSettingsHtml()"),
        permission: env.notificationPermission,
        // Nothing about a channel this browser cannot have may reach the server.
        push_requests: env.requests.map((r) => r.url).filter((u) => u.indexOf('/push') >= 0),
    };
}

// 4. Installing support is the one switch that flips the answer.
{
    const env = boot();
    const container = env.installServiceWorkerSupport();
    results.installed = {
        probe: env.evaluate("'serviceWorker' in navigator"),
        same_container: container === env.serviceWorkerContainer,
        registrations_before: env.registrations.length,
        registration_before: await container.getRegistration() === null,
    };
    await env.evaluate("navigator.serviceWorker.register('push-service-worker.js')");
    const registration = env.registrations[0] || null;
    results.installed.script = registration ? registration.scriptURL : null;
    results.installed.scope = registration ? registration.scope : null;
    results.installed.registration_after = (await container.getRegistration()) !== null;
    results.installed.registrations_after = env.registrations.length;
}

// 5. Two boots are two browsers: support installed on one is not visible on the other.
{
    const withSupport = boot();
    withSupport.installServiceWorkerSupport();
    await withSupport.evaluate("navigator.serviceWorker.register('push-service-worker.js')");
    const without = boot();
    results.isolation = {
        first: withSupport.evaluate("'serviceWorker' in navigator"),
        second: without.evaluate("'serviceWorker' in navigator"),
        second_container: without.serviceWorkerContainer,
        first_registrations: withSupport.registrations.length,
        second_registrations: without.registrations.length,
        // A second boot's container, if it had one, must not be the first boot's.
        distinct: withSupport.serviceWorkerContainer !== without.serviceWorkerContainer,
    };
}

// 6. The environment stays one a browser could give: the service worker file is registered by
//    URL and never loaded *into* this context, where ``self`` does not exist.
{
    const env = boot();
    // Onto the stub window, which is how a string reaches the context without being quoted
    // into a string that is itself quoted.
    env.evaluate("window").__workerSourceForContract = WORKER_SOURCE;
    results.worker_file = {
        listed: scripts.indexOf('push-service-worker.js') >= 0,
        scripts: DEFAULT_SCRIPTS.slice(),
        on_disk: fs.readFileSync(path.join(frontend, 'push-service-worker.js'), 'utf8').length,
        passed_in: WORKER_SOURCE.length,
        loaded: env.evaluate("(function () {\n"
            + "    try { eval(window.__workerSourceForContract); return 'no-throw'; }\n"
            + "    catch (err) { return err.name + ': ' + err.message; }\n"
            + "})()"),
    };
}

// 7. The clipboard: present as modelled, because this environment says it is a secure
//    context - which is the condition the browser's async clipboard exists under. It is also
//    refusable, because a copy that could only ever succeed would hide the fallback the app
//    keeps for one that does not.
{
    const env = boot();
    const probe = 'https://site.example/q/4f3d2c1b';
    await within(env.evaluate("navigator.clipboard.writeText('" + probe + "')"), 'the clipboard write');
    env.setClipboardRefusal('NotAllowedError');
    results.clipboard = {
        secure: env.evaluate("window.isSecureContext"),
        // The app's own guard, run against the stub: it passes, so the console suites are
        // asserting a real copy rather than the fallback they would otherwise get.
        probe_passes: env.evaluate("!!(navigator.clipboard && navigator.clipboard.writeText)"),
        own: env.evaluate("Object.prototype.hasOwnProperty.call(navigator, 'clipboard')"),
        type: env.evaluate("typeof navigator.clipboard.writeText"),
        copied: env.copiedUrls.slice(),
        refusal: await within(
            env.evaluate("navigator.clipboard.writeText('refused').then(() => 'resolved', (err) => err.name)"),
            'the refused clipboard write'
        ),
        // A refusal must record nothing: a suite reading ``copiedUrls`` has to be able to
        // tell a copy that happened from one that was turned down.
        copied_after_refusal: env.copiedUrls.slice(),
    };
}

// 8. ``prompt``, where a refused copy ends up: recorded, because the manual fallback is a
//    dialog a person has to read and a swallowed one is unobservable.
{
    const env = boot();
    env.evaluate("prompt('Copy this link', 'https://site.example/q/4f3d2c1b')");
    results.prompt = { calls: env.prompted.slice() };
}

// 9. The two devices the app feature-tests - camera and location - absent until installed,
//    and drivable once installed.
{
    const bare = boot();
    results.devices_absent = {
        camera_probe: bare.evaluate("!!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)"),
        camera_own: bare.evaluate("Object.prototype.hasOwnProperty.call(navigator, 'mediaDevices')"),
        camera_type: bare.evaluate("typeof navigator.mediaDevices"),
        camera_app: bare.evaluate("Camera.isSupported"),
        camera_calls: bare.cameraCalls.length,
        // The unguarded call a camera edit is most likely to write, and what it does here.
        camera_unguarded: await within(
            bare.evaluate("Camera.start().then(() => 'resolved', (err) => err.name + ': ' + err.message)"),
            'Camera.start() with no device'
        ),
        gps_probe: bare.evaluate("'geolocation' in navigator"),
        gps_own: bare.evaluate("Object.prototype.hasOwnProperty.call(navigator, 'geolocation')"),
        gps_type: bare.evaluate("typeof navigator.geolocation"),
        gps_app: bare.evaluate("Location.isSupported"),
        // No fix, so the app names the reason rather than inventing a position - and no
        // Permissions API to ask either, which has to read as 'unknown' and not as a grant.
        gps_current: await within(
            bare.evaluate("Location.current().then((v) => 'resolved:' + v, (err) => 'rejected:' + (err && err.code))"),
            'Location.current() with no device'
        ),
        gps_permission: await within(bare.evaluate("Location.permissionState()"), 'Location.permissionState()'),
        gps_permissions_own: bare.evaluate("Object.prototype.hasOwnProperty.call(navigator, 'permissions')"),
        gps_calls: bare.geolocationCalls.length,
    };

    const planted = boot();
    const devices = planted.installCameraSupport();
    const geo = planted.installGeolocationSupport();
    results.devices_installed = {
        camera_probe: planted.evaluate("!!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)"),
        camera_app: planted.evaluate("Camera.isSupported"),
        camera_same: devices === planted.evaluate("navigator.mediaDevices"),
        gps_probe: planted.evaluate("'geolocation' in navigator"),
        gps_app: planted.evaluate("Location.isSupported"),
        gps_same: geo === planted.evaluate("navigator.geolocation"),
    };

    // The app's own capture path, run against the modelled device: this is what a suite
    // cannot get today, because every camera suite overrides ``Camera.start`` outright.
    results.capture = {
        started: await within(
            planted.evaluate("Camera.start().then(() => 'resolved', (err) => String(err))"),
            'the installed Camera.start()'
        ),
        constraints: planted.cameraCalls[0] || null,
        attached: planted.evaluate("!!State.mediaStream"),
        tracks: planted.cameraTracks.length,
        stopped_before: planted.cameraTracks.map((track) => track.stopped),
    };
    planted.evaluate("State.stopCamera()");
    results.capture.stopped_after = planted.cameraTracks.map((track) => track.stopped);
    results.capture.cleared_after = planted.evaluate("State.mediaStream === null");

    // A refused camera: the app gets the browser's own error name, which is what it has to
    // explain to the person standing in front of it.
    planted.setCameraRefusal('NotAllowedError');
    results.camera_refused = {
        name: await within(
            planted.evaluate("Camera.start().then(() => 'resolved', (err) => err.name)"),
            'the refused Camera.start()'
        ),
        attached: planted.evaluate("!!State.mediaStream"),
        calls: planted.cameraCalls.length,
    };

    planted.setGeolocationFix({ latitude: 24.86, longitude: 67.01, accuracy: 8 });
    results.location = {
        fix: await within(planted.evaluate("Location.current()"), 'the installed Location.current()'),
        // The options the app asks for, which are its own rules about a phone's GPS indoors.
        options: (planted.geolocationCalls[0] || {}).options || null,
    };
    planted.setGeolocationError({ code: 1, message: 'User denied Geolocation' });
    results.location.refused = await within(
        planted.evaluate(
            "Location.current().then((v) => 'resolved:' + v, (err) => 'rejected:' + err.code + ':' + err.message)"
        ),
        'the refused Location.current()'
    );
}

// 10. The print dialog: present as modelled, recorded at the instant it opens, and never
//     tidied up by the stub - the page's own cleanup is what the suites around it assert, so
//     the stub must leave the page exactly as the app left it.
{
    const env = boot();
    // A plain Ctrl+P with no sheet on the page: the record has to say exactly that rather
    // than invent a report.
    env.evaluate("window.print()");
    env.evaluate("PrintReport.sheet('<p>row</p>', 'my_hours_2026-09-01_2026-09-19')");
    results.print = {
        probe: env.evaluate("typeof window.print"),
        records: env.printed.length,
        direct: env.printed[0] || null,
        sheet: env.printed[env.printed.length - 1] || null,
        // Nothing of the app's work has been done for it: the title is still borrowed, the
        // sheet is still on the page and the body is still marked.
        title_before_close: env.evaluate("document.title"),
        sheets_before_close: env.evaluate("document.body.__children.filter((node) => node.className === 'print-sheet').length"),
        printing_before_close: env.evaluate("document.body.classList.contains('is-printing-report')"),
    };
    // The dialog closing is the suite's to deliver, and the page's to tidy up.
    env.fireWindowEvent('afterprint');
    results.print.after_close = {
        title: env.evaluate("document.title"),
        sheets: env.evaluate("document.body.__children.filter((node) => node.className === 'print-sheet').length"),
        printing: env.evaluate("document.body.classList.contains('is-printing-report')"),
    };
    // Two calls are two records, and the next boot starts empty.
    const twice = boot();
    twice.evaluate("window.print()");
    twice.evaluate("window.print()");
    results.print_separate = { records: twice.printed.length, first_boot: env.printed.length };
}

// 11. The document tree the page builds as it runs: an id resolves to the node that was
//     appended, and writing markup puts a child in the tree. Both are things the app does and
//     then depends on - ``closeCamera()`` finds the camera card by id to take it down, and
//     ``Modal.open`` attaches its backdrop listener to the child it has just written.
{
    const env = boot();
    results.dom = {
        appended_id: env.evaluate("(function () {\n"
            + "    const card = document.createElement('div');\n"
            + "    card.id = 'contract-card';\n"
            + "    document.body.appendChild(card);\n"
            + "    return document.getElementById('contract-card') === card;\n"
            + "})()"),
        children_after_remove: env.evaluate("(function () {\n"
            + "    document.getElementById('contract-card').remove();\n"
            + "    return document.body.__children.filter((node) => node.id === 'contract-card').length;\n"
            + "})()"),
        // Ids that only ever live in ``index.html`` are not nodes here, so they still have to
        // resolve to something a suite can read back.
        fallback: env.evaluate("typeof document.getElementById('shipped-in-markup-only').appendChild"),
        // The modal's own two lines, in order: write the markup, take the child it made.
        markup_child: env.evaluate("(function () {\n"
            + "    const root = document.getElementById('modalRoot');\n"
            + "    root.innerHTML = '<div class=\"modal-backdrop\"></div>';\n"
            + "    const child = root.firstElementChild;\n"
            + "    return { child: !!child, can_listen: !!(child && child.addEventListener) };\n"
            + "})()"),
        cleared_child: env.evaluate("(function () {\n"
            + "    const root = document.getElementById('modalRoot');\n"
            + "    root.innerHTML = '';\n"
            + "    return root.firstElementChild === null && root.innerHTML === '';\n"
            + "})()"),
    };
}

// 12. The frame path - the half of the device a shutter actually needs: a video that reports
//     the size of the stream put on it, and a canvas that encodes or reads back the picture the
//     camera gave it. Without these the app's own capture step cannot run at all, and every
//     suite that wants one has to patch the DOM before it can start.
{
    const env = boot();
    const video = "document.getElementById('attendanceVideo')";
    results.frame = {
        // A video with nothing on it has no frame to measure. The shutter checks exactly this
        // before it takes a photo, so zero here is what makes that check reachable.
        before_size: env.evaluate(video + ".videoWidth + 'x' + " + video + '.videoHeight'),
        before_stream: env.evaluate(video + '.srcObject'),
        // A real canvas: 300x150 until it is told otherwise, and transparent black until
        // something is drawn into it.
        canvas_default: env.evaluate(
            "(function () { const c = document.createElement('canvas'); return c.width + 'x' + c.height; })()"
        ),
        canvas_blank: env.evaluate("(function () {\n"
            + "    const c = document.createElement('canvas');\n"
            + "    return c.getContext('2d').getImageData(0, 0, 2, 1).data.join(',');\n"
            + "})()"),
    };
    const mediaDevices = env.installCameraSupport();
    await within(env.evaluate('Camera.start()'), 'the installed Camera.start()');
    env.evaluate(video + '.srcObject = State.mediaStream');
    results.frame.same_device = mediaDevices === env.evaluate('navigator.mediaDevices');
    results.frame.after_size = env.evaluate(video + ".videoWidth + 'x' + " + video + '.videoHeight');
    // The shutter's own step, run for real: the app's snapshot, of the modelled frame.
    const blob = await within(
        env.evaluate('Camera.snapshot(' + video + ')'),
        "the app's own snapshot"
    );
    results.frame.blob = {
        type: blob.type,
        size: blob.size,
        text: await blob.text(),
    };
    // And the coach's step: the picture, read back at whatever size the caller asks for.
    env.evaluate("(function () {\n"
        + "    const c = document.createElement('canvas');\n"
        + "    c.width = 4; c.height = 4;\n"
        + "    c.getContext('2d').drawImage(" + video + ", 0, 0, 4, 4);\n"
        + "    window.__readPixels = () => c.getContext('2d').getImageData(0, 0, 1, 1).data.join(',');\n"
        + "})()");
    results.frame.pixels = env.evaluate('window.__readPixels()');
    // The sensor is a suite's to replace, which is the only way the coach's rules can be run
    // against pixels that came through a device rather than out of a fixture. Every channel is
    // filled, alpha included, so the readback proves the bytes are the suite's own and that
    // nothing is stamped over them.
    env.setCameraFrame((width, height) => new Array(width * height * 4).fill(200));
    results.frame.replaced_pixels = env.evaluate('window.__readPixels()');
    // A sensor replaced on one boot is not visible on the next: the device lives in ``boot``,
    // like the service worker container, so a suite that gives the camera a frame cannot
    // change what the suite after it measures.
    const other = boot();
    other.installCameraSupport();
    await within(other.evaluate('Camera.start()'), "the second boot's Camera.start()");
    other.evaluate("document.getElementById('attendanceVideo').srcObject = State.mediaStream");
    other.evaluate("(function () {\n"
        + "    const c = document.createElement('canvas');\n"
        + "    c.width = 1; c.height = 1;\n"
        + "    const ctx = c.getContext('2d');\n"
        + "    ctx.drawImage(document.getElementById('attendanceVideo'), 0, 0, 1, 1);\n"
        + "    window.__secondPixels = ctx.getImageData(0, 0, 1, 1).data.join(',');\n"
        + "})()");
    results.frame.second_boot_pixels = other.evaluate('window.__secondPixels');
}

// 13. And which of the surfaces above stopped answering, if any. A device's promise that is
//     never settled has to be a *result* here, because the process it would otherwise produce
//     says nothing at all.
results.hangs = hangs;
"""


@pytest.fixture(scope="module")
def results():
    """The contract, measured in the shared environment itself.

    The worker file's own text is handed to the context as a *string* (a JSON string literal
    is a JavaScript one) so the context is what runs it: if the harness ever listed that file
    among the scripts it loads, that is the exact code path it would take.
    """
    source = (frontend_vm.FRONTEND / "push-service-worker.js").read_text(encoding="utf-8")
    return frontend_vm.run(HARNESS.replace("SOURCE_PLACEHOLDER", json.dumps(source)))


def test_a_browser_without_push_support_is_expressed_as_absence(results):
    """``'serviceWorker' in navigator`` has to be false when there is nothing behind it.

    This is the rule that was broken: an always-present property answers "yes" to the probe
    the app uses to decide whether a service worker can be registered at all, so the app took
    the supported branch and crashed (or, worse, reported a state it was not in) - in a suite
    that was about the push feature, and in every other suite that shares this environment.
    """
    absent = results["absent"]
    assert absent["probe"] is False, (
        "the stub's navigator answers 'serviceWorker' in navigator with true while it has no "
        "container: the app's own feature test cannot tell a browser without push support from "
        "one with it, and every suite sharing this environment is one push edit away from a "
        "crash in a file it never touched"
    )
    assert absent["own"] is False, (
        "navigator has an own 'serviceWorker' property on a plain boot: absence has to be "
        "absence, or the probe above is answered by the stub rather than by support being "
        "installed"
    )
    assert absent["type"] == "undefined", absent["type"]
    assert absent["container"] is None, absent["container"]
    unguarded = absent["unguarded"]
    assert unguarded.startswith("TypeError"), (
        f"an unguarded navigator.serviceWorker access in this environment returned {unguarded!r}; "
        f"it has to throw, because that is how a push edit that assumes support fails in its own "
        f"suite instead of being hidden by the stub"
    )


def test_notification_stays_a_separate_surface_from_push_support(results):
    """Permission can be asked for in a browser that can never deliver: two different facts.

    The app asks the server whether pushing is *configured* before it asks the browser for
    anything, and a deployment with no keys must never prompt. That reasoning only holds if
    the stub keeps the two surfaces separate - ``Notification`` present, ``serviceWorker``
    absent - which is also what an older browser on site actually looks like.
    """
    notification = results["notification"]
    assert notification["type"] == "function", (
        f"the stub's Notification is a {notification['type']}, so a suite cannot tell a browser's "
        f"constructor from a stand-in object: {notification}"
    )
    assert notification["ask"] == "function", notification
    assert notification["permission"] == "default", (
        "the stub starts with a permission already decided, so a suite cannot tell a prompt "
        "that was never shown from one that was"
    )
    assert notification["after_reading_it"] == "default", (
        "reading Notification.permission prompted, which the app relies on it not doing: it "
        "reads the server's answer before it asks the browser for anything"
    )


def test_the_app_boots_on_a_browser_without_service_workers(results):
    """The whole point: absence is a state the app handles, not a crash it suffers.

    ``UI.init`` is where the push channel is wired in - registration, the config read, and the
    listener that answers a tapped notification - so it is the function a push edit is most
    likely to make throw on a browser without service workers.
    """
    boot = results["boot"]
    assert boot["crashed"] is None, (
        f"UI.init threw on a browser without service workers:\n{boot['crashed']}\n"
        f"A push edit that made this throw would fail in every frontend suite at once; this "
        f"contract suite is where it should be seen first, and it has just seen it"
    )
    assert boot["supported"] is False, boot
    assert boot["reason"] == "pushSwUnsupported", (
        f"a browser without service workers landed in {boot['reason']!r} rather than its own "
        f"named state, so the settings card cannot say what is wrong"
    )
    assert not boot["subscription"], boot["subscription"]
    assert boot["push_requests"] == [], (
        f"the app asked the server about a push channel this browser cannot have: "
        f"{boot['push_requests']}"
    )
    assert boot["permission"] == "default", (
        "the app prompted for a permission nothing could use - the one thing the flow is "
        "built to avoid"
    )
    assert boot["card"].strip() != "", "the settings card rendered nothing"
    assert "data-push-toggle" not in boot["card"], (
        "the card offered a switch in a browser that cannot register a service worker: a "
        "button to nowhere"
    )


def test_installing_support_is_the_one_switch_that_flips_the_answer(results):
    """And it produces a container a suite can then drive, with nothing registered by accident."""
    installed = results["installed"]
    assert installed["probe"] is True, (
        "installServiceWorkerSupport() did not make 'serviceWorker' in navigator true, so the "
        "supported path is unrunnable for every suite that needs it"
    )
    assert installed["same_container"] is True, (
        "navigator.serviceWorker is not the container installServiceWorkerSupport() returned: "
        "a suite would be reading registrations from one object while the app wrote to another"
    )
    assert installed["registrations_before"] == 0, installed
    assert installed["registration_before"] is True, (
        "a fresh container already has a registration, so a suite cannot tell 'never "
        "subscribed' from 'subscribed'"
    )
    assert installed["script"] == "push-service-worker.js", installed["script"]
    assert installed["scope"] == "/", installed["scope"]
    assert installed["registration_after"] is True and installed["registrations_after"] == 1


def test_two_boots_are_two_browsers(results):
    """Support installed in one suite must not be visible to the next boot in the same process.

    Suites that need the absence (the handset card, the alerts tab) and suites that need
    support (the push flow) run in one Node process per suite file, and several of them call
    ``boot()`` repeatedly. If the container or the property lived outside ``boot``, whichever
    test ran first would decide what the rest saw.
    """
    isolation = results["isolation"]
    assert isolation["first"] is True and isolation["second"] is False, isolation
    assert isolation["second_container"] is None, (
        "a second boot inherited the first boot's service worker container"
    )
    assert isolation["first_registrations"] == 1, isolation
    assert isolation["second_registrations"] == 0, (
        "the second boot sees the first boot's registrations"
    )
    assert isolation["distinct"] is True, "two boots share one container"


def test_the_service_worker_file_is_never_loaded_into_this_environment(results):
    """A worker file belongs to a worker, not to the page the VM models.

    ``push-service-worker.js`` is written for a ``ServiceWorkerGlobalScope``: it asks for
    ``self``, which a page does not have. Adding it to the loaded scripts - an easy mistake,
    since it is a frontend file - would take the prelude down for every suite at once, so the
    file is pinned as *registered by URL* and nothing else.
    """
    worker = results["worker_file"]
    assert worker["passed_in"] == worker["on_disk"] > 0, (
        f"the suite handed the context {worker['passed_in']} characters of a {worker['on_disk']} "
        f"character file: the 'it cannot be loaded' proof below would be about a substitute"
    )
    assert worker["listed"] is False, (
        "push-service-worker.js is in the scripts the VM loads: it expects 'self', which this "
        "environment does not have, and every suite sharing the prelude would fail"
    )
    assert "push-service-worker.js" not in worker["scripts"], worker["scripts"]
    for name in ("frontendjavascript.js", "worker_modules.js"):
        assert name in worker["scripts"], (
            f"{name} is not among the files the VM loads, so the push feature is not actually "
            f"under test in this environment: {worker['scripts']}"
        )
    assert worker["loaded"].startswith("ReferenceError"), (
        f"loading push-service-worker.js into the page context did not fail as expected: "
        f"{worker['loaded']}"
    )
    assert "self" in worker["loaded"], (
        f"the failure is not the missing 'self': {worker['loaded']}"
    )


# ---------------------------------------------------------------------------
# the capabilities the app feature-tests, and which of the two things each is
# ---------------------------------------------------------------------------
#: What the clipboard contract copies, so the Python side and the harness agree on the text
#: rather than each holding its own literal that could drift apart quietly.
COPIED_TEXT = "https://site.example/q/4f3d2c1b"


#: The tab title ``index.html`` ships with, which the print path borrows and gives back. The
#: stub holds it as the document's title, so it is the page's starting state, not a brand.
SHIPPED_TITLE = "Al-Jehad - Site Attendance"


def test_the_clipboard_is_present_as_modelled_in_a_secure_context(results):
    """Present, and not by accident: the stub declares the one condition the API needs.

    The async clipboard only exists in a secure context, and this environment says
    ``isSecureContext: true`` - so a modelled clipboard is the honest reading of the stub's own
    page, while the on-site browser that has none is the case the app's guard falls through on
    to a prompt. Four console suites read a copy back off ``copiedUrls``; if the stub ever
    stopped modelling one, or modelled one that dropped the text, they would go on passing
    while asserting ``undefined``.
    """
    clipboard = results["clipboard"]
    assert clipboard["secure"] is True, (
        "the stub no longer says it is a secure context, so a clipboard it still models is a "
        "page a browser would never have given one to"
    )
    assert clipboard["probe_passes"] is True, (
        "the app's own guard (navigator.clipboard && navigator.clipboard.writeText) is false in "
        "this environment, so every console suite asserting a copy is really asserting the "
        f"prompt fallback: {clipboard}"
    )
    assert clipboard["own"] is True, (
        "the clipboard is not an own property of the stub's navigator, so a suite cannot tell "
        "a browser that has one from one the stub assembled: "
        f"{clipboard['own']}"
    )
    assert clipboard["type"] == "function", clipboard
    assert clipboard["copied"] == [COPIED_TEXT], (
        f"a write reached the stub's clipboard and was not recorded as {COPIED_TEXT!r}: "
        f"{clipboard['copied']}"
    )


def test_a_refused_copy_is_a_state_a_suite_can_drive(results):
    """A clipboard that can only ever succeed hides the branch the app keeps for one that can't.

    ``writeText`` genuinely rejects - the document is not focused, the permission was denied -
    and the app answers that with a prompt rather than nothing happening. Modelling only the
    happy path is the same class of lie as a ``Notification`` that was an object where a browser
    has a constructor: it would let a suite pass over code it never ran.
    """
    clipboard = results["clipboard"]
    assert clipboard["refusal"] == "NotAllowedError", (
        "a refused clipboard write did not reject with the browser's own error name, so the "
        f"app cannot tell a refusal from a copy: {clipboard['refusal']!r}"
    )
    assert clipboard["copied_after_refusal"] == [COPIED_TEXT], (
        "the stub recorded a copy it refused, so a suite reading the clipboard back cannot "
        f"tell a copy that happened from one that was turned down: {clipboard}"
    )


def test_the_manual_fallback_is_recorded(results):
    """``prompt`` is where a refused copy ends: a dialog a person reads, so a suite has to see it.

    The app's fallback is a person copying by hand, and the only evidence of it in a headless
    environment is that the dialog was opened with something they could select. A stub that
    returned ``null`` and kept nothing would make "it fell back to asking" unobservable - and
    the fallback unreachable is how it stops being exercised at all.
    """
    calls = results["prompt"]["calls"]
    assert calls == [{"message": "Copy this link", "defaultValue": COPIED_TEXT}], (
        f"the dialog the page opened is not readable back: {calls}"
    )


def test_the_camera_is_absent_until_it_is_installed(results):
    """``Camera.isSupported`` is the app's own feature test, and it has to be false here.

    The expensive failure is a stub that models a camera generously: ``isSupported`` answers
    true, the punch flow opens the overlay, and every suite on a machine with no camera is
    suddenly driving a device that is not there. Absence has to be absence - no property, not a
    null one behind a true probe - so the unguarded call throws here, by name, instead of in
    whichever suite a camera edit touches next.
    """
    absent = results["devices_absent"]
    assert absent["camera_probe"] is False, (
        "a plain boot answers 'this browser has a camera' with true and has no device behind "
        "it, which is the probe ``Camera.isSupported`` is built on"
    )
    assert absent["camera_own"] is False, (
        "navigator has an own 'mediaDevices' property on a plain boot, so absence is decided "
        "by the stub rather than by a device being installed"
    )
    assert absent["camera_type"] == "undefined", absent["camera_type"]
    assert absent["camera_app"] is False, (
        "the app thinks this browser has a camera, so the overlay opens onto nothing: "
        f"Camera.isSupported = {absent['camera_app']}"
    )
    assert absent["camera_calls"] == 0, "a device that is not installed was asked for a stream"
    unguarded = absent["camera_unguarded"]
    assert unguarded.startswith("TypeError"), (
        f"an unguarded Camera.start() in this environment returned {unguarded!r}; it has to "
        f"throw, because that is how a camera edit that assumes a device fails in its own suite "
        f"instead of being hidden by the stub"
    )


def test_the_location_fix_is_absent_until_it_is_installed(results):
    """``'geolocation' in navigator`` is the same class of probe as ``'serviceWorker' in``.

    A punch carries the coordinates, and a browser without a location API is a state this app
    names to the worker ("this browser cannot give a location") rather than a crash. That state
    only exists in the tests if the stub can be in it - and the permission has to read as
    *unknown* rather than as a grant, because there is no Permissions API here either.
    """
    absent = results["devices_absent"]
    assert absent["gps_probe"] is False, (
        "a plain boot answers \"this browser can give a location\" with true and has no "
        "geolocation behind it, so ``Location.isSupported`` cannot be trusted"
    )
    assert absent["gps_own"] is False, absent["gps_own"]
    assert absent["gps_type"] == "undefined", absent["gps_type"]
    assert absent["gps_app"] is False, absent["gps_app"]
    assert absent["gps_current"] == "rejected:4", (
        "the app asks for a fix it cannot have and has to be told why: code 4 is the app's own "
        f"'this browser has no location' answer, and it got {absent['gps_current']!r}"
    )
    assert absent["gps_permissions_own"] is False, (
        "the stub models a Permissions API, so the app cannot be in the state where a phone "
        "cannot say what it granted"
    )
    assert absent["gps_permission"] == "unknown", (
        "with no Permissions API the app has to answer 'unknown'; 'granted' would make an "
        f"unsupported browser look like a consented one: {absent['gps_permission']!r}"
    )
    assert absent["gps_calls"] == 0, absent["gps_calls"]


def test_installing_a_device_is_the_one_switch_that_flips_its_answer(results):
    """And the device behind the probe is the object the suite was handed, as with push."""
    installed = results["devices_installed"]
    assert installed["camera_probe"] is True and installed["camera_app"] is True, (
        f"installCameraSupport() did not make the app's own camera probe true: {installed}"
    )
    assert installed["camera_same"] is True, (
        "navigator.mediaDevices is not the object installCameraSupport() returned, so a suite "
        "would be reading one device while the app wrote to another"
    )
    assert installed["gps_probe"] is True and installed["gps_app"] is True, installed
    assert installed["gps_same"] is True, (
        "navigator.geolocation is not the object installGeolocationSupport() returned"
    )


def test_the_apps_own_capture_path_drives_the_modelled_camera(results):
    """Not a stubbed ``Camera.start``: the shipped one, against a modelled device.

    Every camera suite overrides ``Camera.start`` outright, which means the code that opens the
    camera has never run in front of an assertion. It runs here - and what it is asserted to do
    is what a worker notices: the microphone is not asked for, the front camera is, and the
    track is released when the overlay closes.
    """
    capture = results["capture"]
    assert capture["started"] == "resolved", (
        f"the app's own Camera.start() did not resolve against the modelled device: {capture}"
    )
    constraints = capture["constraints"]
    assert constraints and constraints["audio"] is False, (
        "the capture path asked for audio: a selfie check-in is a camera, and a microphone "
        f"nobody asked for is the permission request that gets a site blocked: {constraints}"
    )
    assert constraints["video"]["facingMode"] == "user", (
        f"the punch camera is no longer the front one, which is the one a worker can point at "
        f"themselves: {constraints['video']}"
    )
    assert capture["tracks"] == 1 and capture["attached"] is True, capture
    assert capture["stopped_before"] == [False], capture
    assert capture["stopped_after"] == [True], (
        "State.stopCamera() did not stop the track, so the camera light stays on after the "
        f"overlay closes - the one thing a worker checks an attendance app for: {capture}"
    )
    assert capture["cleared_after"] is True, capture


def test_a_refused_camera_arrives_as_the_browsers_own_error(results):
    """``Camera.explain`` maps error *names*, so the name is the contract that matters."""
    refused = results["camera_refused"]
    assert refused["name"] == "NotAllowedError", (
        "a camera the person turned down has to arrive as the browser's own error name, or the "
        f"app cannot explain it: {refused['name']!r}"
    )
    assert refused["attached"] is False, (
        "a refused camera still left a stream attached, so the app would go on believing it "
        f"had one: {refused}"
    )
    assert refused["calls"] == 2, refused


def test_the_apps_own_location_path_drives_the_modelled_fix(results):
    """The options are the app's own rules about a phone's GPS, and they are worth pinning.

    ``enableHighAccuracy`` and the 30-second cache are what make a punch work indoors, where a
    coarse network fix can be kilometres out and a fresh lock takes minutes. A reader of a
    failure needs to know which of those was dropped, because the symptom - a geofence refusal
    for a worker standing inside it - looks like the fence and not like the request.
    """
    location = results["location"]
    assert location["fix"] == "24.86,67.01", (
        f"the modelled fix did not arrive as the app writes it ('lat,lon'): {location['fix']!r}"
    )
    options = location["options"]
    assert options["enableHighAccuracy"] is True, (
        f"the location request dropped high accuracy, which is the geofence argument: {options}"
    )
    assert options["timeout"] == 20000, options
    assert options["maximumAge"] == 30000, (
        f"the fix cache is no longer the 30s that makes an indoor check-in work: {options}"
    )
    assert location["refused"] == "rejected:1:User denied Geolocation", (
        "a refused fix has to reach the app as the raw position error, because its code is what "
        f"separates 'blocked' from 'could not tell': {location['refused']!r}"
    )


def test_no_modelled_surface_leaves_a_promise_unsettled(results):
    """The failure this process reports worst, so it is measured instead of waited for.

    A stub that provides a device but never answers - a ``getCurrentPosition`` that keeps the
    callback, a ``getUserMedia`` whose promise nobody settles - empties Node's event loop: it
    exits 0 having printed nothing, and the suite reads an empty result rather than the device
    that never replied. Every await against a modelled surface in this harness is bounded for
    that reason, and a timeout lands here, by name.
    """
    hangs = results["hangs"]
    assert hangs == [], (
        f"a modelled surface never answered, so this run only finished because the harness"
        f"stopped waiting: {hangs}. A device that is absent has to refuse *immediately* - a"
        f"pending promise in its place is indistinguishable from a browser that agreed to"
        f"answer and never did, and that is a hang rather than a state any suite can assert"
    )


def test_the_print_dialog_is_present_and_recorded_at_the_instant_it_opens(results):
    """Present as modelled, and faithful: the record is the page at the moment ``print()`` ran.

    A print is the one thing a suite cannot read back afterwards - the dialog owns the paper -
    so the recorded instant *is* the assertion surface for every report this app produces. That
    only works if the record is honest, including when there is nothing to print.
    """
    printed = results["print"]
    assert printed["probe"] == "function", (
        "a browser always has a print dialog, so the stub must not make it something a suite has "
        f"to install: {printed['probe']!r}"
    )
    assert printed["records"] == 2, (
        f"two calls to the dialog did not record two entries: {printed['records']}"
    )
    direct = printed["direct"]
    assert direct == {"title": SHIPPED_TITLE, "printing": False, "sheet": ""}, (
        "a plain Ctrl+P with no report on the page has to record exactly that, rather than "
        f"inventing a sheet: {direct}"
    )
    sheet = printed["sheet"]
    assert sheet["printing"] is True, (
        "the page was not marked as printing when the dialog opened, which is the stylesheet's "
        f"only signal to take the screen out of the paper: {sheet}"
    )
    assert sheet["title"] == "my_hours_2026-09-01_2026-09-19", (
        f"the dialog names the file from the title, and the title was not borrowed: {sheet}"
    )
    assert "<p>row</p>" in sheet["sheet"], (
        f"the sheet's own markup is not what the record holds: {sheet['sheet']!r}"
    )


def test_the_stub_does_no_of_the_page_s_cleanup(results):
    """The app's tidying-up is another suite's subject, so the stub must not do it for it.

    ``PrintReport`` borrows the document title, marks the body and waits for ``afterprint``
    before putting any of it back - Firefox and Safari render the sheet *after* ``print()``
    returns, so a stub that restored the title the moment it recorded one would hand the reader
    a blank page and make the report suites assert a state the app never reached.
    """
    printed = results["print"]
    assert printed["title_before_close"] == "my_hours_2026-09-01_2026-09-19", (
        "the dialog closing was simulated before it was opened: the title was restored with no "
        f"afterprint at all: {printed['title_before_close']!r}"
    )
    assert printed["sheets_before_close"] == 1, (
        "the sheet left the page while the dialog was open, which is the blank page above: "
        f"{printed['sheets_before_close']}"
    )
    assert printed["printing_before_close"] is True, printed["printing_before_close"]
    after = printed["after_close"]
    assert after == {"title": SHIPPED_TITLE, "sheets": 0, "printing": False}, (
        "the dialog closing is delivered by the suite through fireWindowEvent('afterprint') and "
        f"taken down by the app; it left {after}"
    )


def test_print_records_are_one_per_call_and_per_boot(results):
    """Two boots are two pages, and the record starts empty on the second - as on the first."""
    separate = results["print_separate"]
    assert separate["records"] == 2, (
        f"two prints on a fresh boot produced {separate['records']} records"
    )
    assert separate["first_boot"] == 2, (
        "the first boot's records changed after a second one was made, so the two pages share "
        f"one dialog: {separate}"
    )


def test_an_id_resolves_to_the_node_the_page_appended(results):
    """The card is appended and *then* given its id, and the page finds it by that id later.

    ``closeCamera()`` does exactly that, so a lookup that answers with a stand-in means the
    camera card never leaves the page: the worker's punch succeeds and a live camera overlay
    stays on top of the screen behind it. Nothing in a suite can see that unless the lookup is
    right, which is why it is pinned here rather than worked around in each suite that drives a
    punch.
    """
    dom = results["dom"]
    assert dom["appended_id"] is True, (
        "getElementById did not return the node that was appended, so every id the page sets on "
        "something it built resolves to an object nobody can see - and removing it does nothing"
    )
    assert dom["children_after_remove"] == 0, (
        f"the appended node survived its own remove(): {dom['children_after_remove']}"
    )
    assert dom["fallback"] == "function", (
        "an id that only ever exists in index.html's markup - the toast root, the modal root - "
        "no longer resolves to anything a suite can read back, so pages that ship their own "
        "elements cannot be asserted on at all"
    )


def test_writing_markup_puts_a_child_in_the_tree(results):
    """``Modal.open`` writes its backdrop and immediately attaches a listener to that child.

    The modal is how the app explains a refused camera or a blocked location - the path a worker
    reaches when something has already gone wrong - so a stub that held the markup as a string
    and left the tree empty turned every one of those explanations into a null dereference, in
    a suite that had simply never taken that path.
    """
    dom = results["dom"]
    assert dom["markup_child"]["child"] is True, (
        "writing innerHTML left the element with no first child, so the app's backdrop listener "
        "is attached to null: the help modal cannot open at all"
    )
    assert dom["markup_child"]["can_listen"] is True, dom["markup_child"]
    assert dom["cleared_child"] is True, (
        "clearing innerHTML did not take the child out with it, so a closed modal stays in the "
        "tree and the next open stacks on top of it"
    )


def test_a_video_reports_the_size_of_the_stream_put_on_it(results):
    """The shutter's own guard: ``video.videoWidth`` is how the page knows it has a frame.

    Zero until a stream is attached is not a convenience - it is the state the punch card starts
    in, and the one that produces "the camera is not ready yet" instead of a punch with no photo.
    """
    frame = results["frame"]
    assert frame["before_size"] == "0x0", (
        f"a video with no stream on it reports {frame['before_size']!r} as its frame size, so the "
        f"page cannot tell a live camera from a dark one"
    )
    assert frame["before_stream"] is None, frame
    assert frame["after_size"] == "720x960", (
        f"a video with the modelled stream on it reports {frame['after_size']!r} rather than the "
        f"track's own settings, so a captured frame has no size to encode"
    )
    assert frame["same_device"] is True, (
        "navigator.mediaDevices is not the device installCameraSupport() returned"
    )


def test_a_canvas_encodes_the_frame_it_was_given(results):
    """The app's own snapshot step, end to end: a frame in, a JPEG of that frame out."""
    frame = results["frame"]
    assert frame["canvas_default"] == "300x150", (
        f"a canvas starts {frame['canvas_default']} rather than 300x150, so a snapshot that "
        f"forgets to size its canvas silently sends the wrong picture"
    )
    assert frame["canvas_blank"] == "0,0,0,0,0,0,0,0", (
        f"an undrawn canvas reads back as {frame['canvas_blank']!r} rather than transparent "
        f"black, so a suite cannot tell a frame that was drawn from one that never was"
    )
    blob = frame["blob"]
    assert blob["type"] == "image/jpeg", (
        f"the snapshot encoded as {blob['type']!r}: the punch posts this, and the server's "
        f"limits and the worker's connection are both sized for a JPEG"
    )
    assert blob["size"] > 0, blob
    assert "720x960" in blob["text"], (
        f"the encoded frame does not carry the size of the stream it came from: {blob['text']!r}"
    )
    assert "q0.9" in blob["text"], (
        f"the compression the capture path asks for is gone: {blob['text']!r}. A phone uploading "
        f"full-quality frames at the gate is the difference between a punch and a timeout"
    )


def test_the_canvas_reads_back_the_picture_the_camera_gave_it(results):
    """The coach's step: pixels that came through the device, at the size it asks for."""
    frame = results["frame"]
    assert frame["pixels"] == "128,128,128,255", (
        f"reading back a drawn canvas gave {frame['pixels']!r} rather than the modelled camera's "
        f"own frame: the coach would then be reasoning about nothing in particular"
    )
    assert frame["second_boot_pixels"] == "128,128,128,255", (
        f"a frame installed on one boot is visible on the next ({frame['second_boot_pixels']!r}): "
        f"the camera belongs to a boot, and a suite that gives it a picture would otherwise be "
        f"deciding what a later, unrelated suite measures"
    )
    assert frame["replaced_pixels"] == "200,200,200,200", (
        f"setCameraFrame() did not change what the canvas reads back ({frame['replaced_pixels']!r}): "
        f"a suite cannot drive the coach's rules against a frame of its own choosing, which is "
        f"what the light and contrast rules are about. The readback is the frame that was "
        f"installed, every channel of it"
    )


def test_a_body_that_throws_is_reported_with_its_stack():
    """The harness's own failure contract: a crash names itself, with the line that threw.

    The shared environment turns a JavaScript failure into a Python ``AssertionError``. If that
    message were only "harness failed", every stub mistake would arrive as an unexplained
    failure in whichever suite ran first; the stack is what makes the culprit findable.
    """
    with pytest.raises(AssertionError) as raised:
        frontend_vm.run("throw new Error('stub browser exploded');")
    message = str(raised.value)
    assert "frontend harness failed" in message, message
    assert "stub browser exploded" in message, (
        f"the harness swallowed the error the body threw: {message}"
    )
    assert "harness.js" in message, (
        f"the harness's own file is not named in the failure, so the stack cannot be followed "
        f"back to the line that threw: {message}"
    )


def test_a_scenario_that_never_settles_is_reported_rather_than_silently_green():
    """The one failure the harness cannot raise from inside, because there is no throw.

    ``await`` on a promise nobody resolves leaves Node with an empty event loop, and it exits
    *successfully* with both streams empty - no assertion, no stack, no results. ``run`` names
    that case, because the alternative is downstream: a ``JSONDecodeError`` about an empty
    string, which says nothing about the code that never returned.
    """
    with pytest.raises(AssertionError) as raised:
        frontend_vm.run("const results = {};\nawait new Promise(() => {});")
    message = str(raised.value)
    assert "printed no results" in message, (
        f"a scenario that never settles was not reported as such: {message}"
    )
    assert "never settles" in message, (
        f"the failure does not say what an empty result means, so the reader is left with "
        f"Node exiting zero on them: {message}"
    )


# ---------------------------------------------------------------------------
# the load check: a broken shipped file fails the import, not a suite
# ---------------------------------------------------------------------------
def _a_copy_of_the_frontend(tmp_path: Path) -> Path:
    """The shipped files in a throwaway directory, so a test can break one of them.

    Copied rather than edited in place for the reason the whole harness exists: the checkout's
    frontend is the thing under test, and a test that corrupts it to watch an error is a test
    that leaves it corrupted when it fails.
    """
    target = tmp_path / "frontend"
    target.mkdir()
    for name in frontend_vm.DEFAULT_SCRIPTS:
        shutil.copy(frontend_vm.FRONTEND / name, target / name)
    return target


def test_the_shipped_files_are_loaded_when_this_module_is_imported():
    """The control for the three tests below, and the pin on *when* the load happens.

    ``SHIPPED_SCRIPTS_LOADED`` is set by the call at the bottom of ``frontend_vm`` and by
    nothing else, so if that call is ever removed the suites go back to reporting a frontend
    syntax error from whichever of them boots first - which is the failure this pins against.
    """
    assert frontend_vm.SHIPPED_SCRIPTS_LOADED == frontend_vm.DEFAULT_SCRIPTS, (
        "frontend_vm did not load the shipped files when it was imported, so a file that cannot "
        "be loaded is again every suite's own problem: "
        f"{frontend_vm.SHIPPED_SCRIPTS_LOADED}"
    )


def test_the_list_of_shipped_files_is_written_once():
    """The check and the harness have to load the same files, so the list lives in Python.

    Two lists would drift, and the drift would be invisible: the check would pass over a file
    the harness does not load, or name files the harness never reads.
    """
    assert json.dumps(list(frontend_vm.DEFAULT_SCRIPTS)) in frontend_vm.PRELUDE, (
        "the prelude's list of files is no longer the one Python holds in DEFAULT_SCRIPTS, so "
        "the import-time check can pass while the harness loads something else"
    )


def test_an_unbroken_copy_loads(tmp_path):
    """Copies load, so the failures below are the breakage and not the copying."""
    frontend_vm.check_scripts_load(frontend_dir=_a_copy_of_the_frontend(tmp_path))


def test_a_syntax_error_is_reported_with_its_file_and_line(tmp_path):
    """The case this exists for: one line of a shipped file that will not parse.

    Before the check ran at import, this arrived as ``frontend harness failed`` inside a suite
    about something else, with a stack pointing into a temp file called ``harness.js`` and no
    mention of the file that was actually broken - and once per suite, since each of them boots
    the environment for itself.
    """
    frontend = _a_copy_of_the_frontend(tmp_path)
    (frontend / "worker_modules.js").write_text("const ok = 1;\nconst broken = ;\n", encoding="utf-8")

    with pytest.raises(frontend_vm.FrontendFailedToLoad) as raised:
        frontend_vm.check_scripts_load(frontend_dir=frontend)

    message = str(raised.value)
    assert "worker_modules.js:2" in message.splitlines()[0], (
        f"the failure does not lead with the file and the line: {message.splitlines()[0]}"
    )
    assert "SyntaxError" in message, (
        f"the failure does not say what was wrong with the file: {message}"
    )


def test_a_file_that_throws_while_loading_is_reported_with_its_file_and_line(tmp_path):
    """Not just parsing: a file that runs but throws on its first line is the same hazard.

    A top-level reference to something that no longer exists parses perfectly and still takes
    the whole environment down, so the check has to *run* the files rather than only compile
    them.
    """
    frontend = _a_copy_of_the_frontend(tmp_path)
    (frontend / "worker_modules.js").write_text("const boom = missing_at_load_time;\n", encoding="utf-8")

    with pytest.raises(frontend_vm.FrontendFailedToLoad) as raised:
        frontend_vm.check_scripts_load(frontend_dir=frontend)

    message = str(raised.value)
    assert "worker_modules.js:1" in message.splitlines()[0], (
        f"the failure does not lead with the file and the line: {message.splitlines()[0]}"
    )
    assert "missing_at_load_time" in message, (
        f"the failure does not name what was missing: {message}"
    )


def test_a_listed_file_that_is_not_on_disk_is_reported(tmp_path):
    """A file the list names but the directory does not have: a rename, or a bad merge."""
    frontend = _a_copy_of_the_frontend(tmp_path)
    (frontend / "i18n.hi.js").unlink()

    with pytest.raises(frontend_vm.FrontendFailedToLoad) as raised:
        frontend_vm.check_scripts_load(frontend_dir=frontend)

    assert "i18n.hi.js" in str(raised.value).splitlines()[0], (
        f"the failure does not name the file it could not read: {str(raised.value).splitlines()[0]}"
    )
