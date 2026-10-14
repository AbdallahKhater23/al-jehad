"""The mobile client's screens, mounted in a real browser and driven by real taps.

WHY THIS EXISTS
---------------
``test_mobile_punch_flow`` bundles ``src/ui/punch.ts`` and runs the *rule* under Node, and
``test_mobile_client_release_guard`` reads the built bundle for the host names baked into it.
Neither touches a screen. A screen here is a vanilla template string painted into a host
element (``mobile-client/src/ui/dom.ts``), and a delegated ``click`` listener hung off the
element that string was painted into:

    host.innerHTML = '<div id="profile-root"></div>';
    const root = host.querySelector('#profile-root');
    disposers.push(on(root, 'click', (event) => { ... }));

That is three things a typecheck cannot see. ``root`` is re-read on every repaint, the
listener is attached once to a node that has to survive every repaint, and the handler's
own work is an ``async`` function started with ``void`` - so a rejection inside it is
swallowed and the screen simply does nothing when tapped. A control that is drawn and dead
looks exactly like one that works, which is the worst possible failure for the controls on
the profile screen: *Change password*, *Delete my account* and *Sign out*.

So this module is the handset's answer to ``browser.py``: it bundles the screen modules out
of their own tree with esbuild (the same tool Vite uses), serves the bundle to a real
Chromium over HTTP, mounts a screen into it with a seeded session, and hands the test a page
it can tap. The API is small on purpose:

    with phone(browser, base_url, routes=[...]) as handset:
        handset.open(PROFILE)
        handset.tap("delete-account")
        handset.fill("#del-password", secret)
        handset.tap_sheet_button("Delete my account")
        assert handset.requests_to("/workers/me", method="DELETE")[0]["body"] == {
            "current_password": secret
        }

WHAT IT DOES NOT DO
-------------------
It does not run the Android app, and it does not replace the Gradle build - see
``deploy/`` and ``test_mobile_client_release_guard`` for the two halves of that. It mounts
one screen at a time into a bare host element with no stylesheet, so nothing here asserts
anything about *layout*; it asserts what a tap does. ``api_base`` is whatever the build
bakes in, and ``fetch`` is a recorder with scripted answers, so no request leaves the
machine and every request is inspectable. ``Preferences`` is Capacitor's own web
implementation, backed by this browser's ``localStorage``, so "the session was cleared on
the device" is a real assertion about real storage.

Node, esbuild and a Chromium are all optional. Without any of them the suite skips and says
what to install rather than failing the build - the same shape ``test_mobile_punch_flow``
uses for Node and ``browser.py`` for the browser.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import http.server
import json
import pathlib
import shutil
import subprocess
import threading
from typing import Any, Iterator

import browser as browser_support
from harness import PROJECT_ROOT

#: Playwright's own timeout, so a wait that runs out can be reported as a fact about the
#: screen ("the sheet never opened") rather than as a stack trace from the driver.
try:
    from playwright.sync_api import TimeoutError as PLAYWRIGHT_TIMEOUT
except Exception:  # pragma: no cover - playwright is installed wherever these tests run
    PLAYWRIGHT_TIMEOUT = TimeoutError

# ---------------------------------------------------------------------------
# What has to be on the machine
# ---------------------------------------------------------------------------
NODE = shutil.which("node")
MOBILE_DIR = PROJECT_ROOT / "mobile-client"
SRC = MOBILE_DIR / "src"
UI = SRC / "ui"
ESBUILD_JS = MOBILE_DIR / "node_modules" / "esbuild" / "bin" / "esbuild"

#: How long a tap, a paint or a scripted answer may take before the test gives up. Generous
#: because the first paint of a screen waits on its own fetches, and a tight bound here would
#: turn a slow machine into a red suite for no reason.
TIMEOUT_MS = 15_000


def available() -> tuple[bool, str]:
    """Whether a screen can be bundled and driven here, and why not when it cannot."""
    if NODE is None:
        return False, "node is not on PATH, and the screens are TypeScript"
    if not ESBUILD_JS.exists():
        return False, (
            f"{ESBUILD_JS.relative_to(PROJECT_ROOT)} is missing; run `npm install` in "
            "mobile-client"
        )
    return browser_support.available()


def skip_reason() -> str | None:
    """The reason to skip, or ``None`` when everything a screen needs is here."""
    ok, why = available()
    return None if ok else why


# ---------------------------------------------------------------------------
# The screens this can mount
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Screen:
    """One screen, and how to get it drawing.

    ``factory`` is called with the sign-out callback and nothing else, because that is the
    one argument every screen's factory takes or ignores; ``mounted`` is an expression that
    is true only once the screen has *painted* something of its own. The second field is the
    same contract as ``browser.Page.ran``: a screen whose module loaded and then threw while
    drawing looks, to a test that only waited for the host to be non-empty, exactly like one
    that worked.
    """

    name: str
    module: str
    factory: str
    mounted: str
    why: str


PROFILE = Screen(
    name="profile",
    module="screens/profile/profile.ts",
    factory="createProfileScreen",
    # The destructive rows are the point of this screen, so "it painted" is "the row a
    # worker taps to remove their account is on the page".
    mounted="""!!document.querySelector('#profile-root [data-action="delete-account"]')""",
    why="the account card is painted from the session, so its rows mean the screen ran",
)

SCREENS: dict[str, Screen] = {PROFILE.name: PROFILE}


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------
#: The entry esbuild is pointed at. It is written into the app's own tree (and deleted
#: afterwards) so the screens' relative imports resolve exactly as they do in the app, and
#: it exposes the modules a test may need to read state from - the store, the sheet, and
#: Capacitor's Preferences, which is where the session actually lives on a device.
ENTRY = """{imports}
(window as any).MobileScreens = {{
  store,
  Preferences,
  openSheet,
  closeSheet,
  sheetIsOpen,
  screens: {{ {screens} }},
}};
"""


def bundle(screens: tuple[Screen, ...], out_path: pathlib.Path) -> pathlib.Path:
    """Bundle ``screens`` into one ES module. Raises with esbuild's own diagnostics."""
    entry = UI / "__dom_harness.ts"
    imports = "\n".join(
        [
            "import { store } from '../core/store.js';",
            "import { Preferences } from '@capacitor/preferences';",
            "import { openSheet, closeSheet, sheetIsOpen } from './components/sheet.js';",
        ]
        + [f"import {{ {s.factory} }} from './{s.module}';" for s in screens]
    )
    entry.write_text(
        ENTRY.format(
            imports=imports,
            screens=", ".join(f"{s.name}: {s.factory}" for s in screens),
        ),
        encoding="utf-8",
    )
    try:
        built = subprocess.run(
            [
                NODE,
                str(ESBUILD_JS),
                str(entry),
                "--bundle",
                "--platform=browser",
                "--format=esm",
                f"--outfile={out_path}",
                "--log-level=warning",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        entry.unlink(missing_ok=True)
    assert built.returncode == 0, f"esbuild could not bundle the screens:\n{built.stderr}"
    return out_path


PAGE = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8" /><title>mobile screens</title></head>
  <body>
    <div id="host"></div>
    <main id="main"></main>
    <script type="module" src="./app.mjs"></script>
  </body>
</html>
"""


def write_site(directory: pathlib.Path, bundle_path: pathlib.Path) -> pathlib.Path:
    """Lay the bundle and its one page out as a site the browser can open."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "app.mjs").write_text(bundle_path.read_text(encoding="utf-8"), encoding="utf-8")
    (directory / "index.html").write_text(PAGE, encoding="utf-8")
    return directory


@contextlib.contextmanager
def serve(directory: pathlib.Path) -> Iterator[str]:
    """Serve ``directory`` on a loopback port nothing else is using, and clean up after."""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=15)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """A static server that does not write a line per request into pytest's output."""

    def log_message(self, *args: Any) -> None:  # noqa: D102 - see the class docstring
        return


# ---------------------------------------------------------------------------
# The shims the page boots with
# ---------------------------------------------------------------------------
#: Installed before any page script runs, so a request cannot be made without being seen.
#:
#: ``fetch`` is replaced rather than spied on: the answers are scripted by the test through
#: ``__routes``, which is what makes a refusal (401 on the wrong password) testable at all
#: without a server. Everything is recorded with its method, path, parsed body and headers,
#: because "the right request went out" is the difference between a destructive control that
#: works and one that only looks like it does.
SHIMS = r"""
(() => {
  window.__requests = [];
  window.__routes = [];
  window.__rejections = [];

  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    window.__rejections.push(String((reason && reason.stack) || reason));
  });

  // The API prefix is stripped so a test can name the path the way the app does
  // ('/workers/me') rather than the way the transport builds it.
  const pathOf = (url) => {
    try {
      return new URL(url, window.location.href).pathname.replace(/^\/api\/v\d+/, '') || '/';
    } catch (error) {
      return String(url);
    }
  };

  window.fetch = (input, init) => {
    const options = init || {};
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    const method = String(options.method || (input && input.method) || 'GET').toUpperCase();
    const path = pathOf(url);
    let body = null;
    if (typeof options.body === 'string') {
      try { body = JSON.parse(options.body); } catch (error) { body = options.body; }
    } else if (options.body) {
      body = '<' + (options.body.constructor ? options.body.constructor.name : 'body') + '>';
    }
    window.__requests.push({
      url: String(url),
      path,
      method,
      body,
      headers: Object.assign({}, options.headers || {}),
    });

    const route = window.__routes.find(
      (candidate) =>
        candidate.path === path && (!candidate.method || candidate.method === method)
    ) || { status: 200, body: {} };
    const status = route.status === undefined ? 200 : route.status;
    const payload = route.body === undefined ? {} : route.body;
    return Promise.resolve(
      new Response(status === 204 ? null : JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    );
  };
})();
"""

#: Mount one screen, with a session the way a signed-in app holds one.
#:
#: The session is written to *both* places it lives: the in-memory store, which is what the
#: screen draws from, and ``Preferences``, which is what the transport reads its bearer token
#: from and what ``clearPersistedSession`` has to empty. Seeding only the store would make
#: every request anonymous and every "it signed out" assertion meaningless.
MOUNT = r"""
(async () => {
  const S = window.MobileScreens;
  const spec = __SPEC__;
  const waitFor = async (test, tries = 200) => {
    for (let i = 0; i < tries; i += 1) {
      const value = test();
      if (value) return value;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return null;
  };

  if (spec.session) {
    const held = spec.session;
    await S.Preferences.set({ key: 'auth_token', value: held.token });
    await S.Preferences.set({ key: 'auth_user', value: JSON.stringify(held.user) });
    await S.Preferences.set({ key: 'auth_token_version', value: String(held.tokenVersion || 0) });
    if (held.expiresAt) {
      await S.Preferences.set({ key: 'auth_expires_at', value: held.expiresAt });
    }
  }
  S.store.setSession(spec.session);

  window.__signedOut = 0;
  const screen = S.screens[spec.screen](() => { window.__signedOut += 1; });
  window.__unmount = screen.mount(document.getElementById('host'), {
    refresh: () => {},
    main: document.getElementById('main'),
  });

  // ``__MOUNTED__`` is the screen's own expression for "it painted", evaluated until it is
  // true - the same contract ``browser.Page.ran`` has, and for the same reason. It is
  // substituted as *code* rather than as a string: handed a string literal, ``waitFor``
  // would be satisfied on its first look and every mount in this suite would return before
  // the screen had drawn anything.
  const painted = await waitFor(() => (__MOUNTED__));
  return {
    painted: !!painted,
    signed_out: window.__signedOut,
    session: S.store.getState().session,
  };
})()
"""

#: The open sheet, read off the DOM the way a person reads it: its name, its words, the
#: buttons it offers and whether any of them is the danger variant.
SHEET = r"""
(() => {
  const sheet = document.querySelector('.hand-sheet');
  if (!sheet) return { open: false };
  const label = (node) => (node.textContent || '').trim();
  const error = sheet.querySelector('[data-error]');
  return {
    open: true,
    title: label(sheet.querySelector('.hand-sheet__title')),
    body: (sheet.querySelector('.hand-sheet__body') || {}).innerText || '',
    buttons: Array.from(sheet.querySelectorAll('.hand-sheet__foot button')).map((button) => ({
      label: label(button.querySelector('.ui-btn__label') || button),
      danger: button.classList.contains('ui-btn-danger'),
      disabled: button.disabled,
      busy: !!button.querySelector('.hand-spinner'),
    })),
    inputs: Array.from(sheet.querySelectorAll('input')).map((input) => ({
      id: input.id,
      type: input.type,
      value: input.value,
    })),
    error: error ? label(error) : '',
    error_shown: !!error && !error.hidden,
    dismissible: !!sheet.querySelector('[data-close]'),
  };
})()
"""

#: What the device is holding. ``CapacitorStorage.`` is the prefix Capacitor's own web
#: implementation puts in front of a key, and the prefix is the point: a key stored *without*
#: it is a key the app will never read back.
PERSISTED = r"""
(() => {
  const held = {};
  for (let i = 0; i < window.localStorage.length; i += 1) {
    const name = window.localStorage.key(i);
    held[name] = window.localStorage.getItem(name);
  }
  return held;
})()
"""

TOASTS = r"""
Array.from(document.querySelectorAll('.hand-toast:not(.hand-toast--out)')).map((node) => ({
  kind: (node.className.match(/hand-toast--(\w+)/) || [])[1] || '',
  text: ((node.querySelector('.hand-toast__text') || {}).textContent || '').trim(),
}))
"""

# ---------------------------------------------------------------------------
# A handset, tapped
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Mounted:
    """What ``open`` reports back: the screen painted, and the session it painted from."""

    screen: Screen
    painted: bool
    signed_out: int
    session: Any


class Handset:
    """One page, mounted on one screen, with the shims above already in place.

    Every interaction here is a *real* one: Playwright's mouse moves to the element's centre
    and clicks it, so an element that is covered, off screen or too small to hit fails the
    way it would fail for a worker - which is the whole reason this is a browser and not a
    stubbed DOM.
    """

    def __init__(self, tab: Any, base_url: str, errors: list[str], console_errors: list[str]):
        self.tab = tab
        self.base_url = base_url
        self.page_errors = errors
        self.console_errors = console_errors

    # -- getting a screen up -------------------------------------------------
    def open(self, screen: Screen, *, session: dict[str, Any] | None = None) -> Mounted:
        """Mount ``screen`` with ``session`` (a signed-in worker by default)."""
        # The recorders are cleared per mount: a page may hold two screens in one test, and
        # "what did *this* screen ask for" has to start from nothing.
        self.tab.evaluate("() => { window.__requests = []; window.__rejections = []; }")
        script = MOUNT.replace("__SPEC__", json.dumps({"screen": screen.name, "session": session}))
        script = script.replace("__MOUNTED__", f"({screen.mounted})")
        state = self.tab.evaluate(script)
        return Mounted(
            screen=screen,
            painted=bool(state["painted"]),
            signed_out=int(state["signed_out"]),
            session=state["session"],
        )

    def route(self, path: str, body: Any, *, status: int = 200, method: str | None = None) -> None:
        """Script one answer. Later routes win, so a test may re-answer a path mid-flow."""
        self.tab.evaluate(
            "(route) => { window.__routes.unshift(route); }",
            {"path": path, "body": body, "status": status, "method": method},
        )

    # -- tapping -------------------------------------------------------------
    def tap(self, action: str) -> bool:
        """Tap the control carrying ``data-action="<action>"``, with a real mouse.

        Returns whether the control was there to be tapped at all. A missing or hidden
        control is ``False`` rather than a driver timeout, so a test can say what it
        expected to find; the click itself is always a real mouse on the element's centre
        point, which is what makes a covered or unresponsive control fail here.
        """
        return self.tap_selector(f'[data-action="{action}"]')

    def tap_selector(self, selector: str) -> bool:
        """Tap any element, for the controls that are not addressed by ``data-action``."""
        if not self.wait(f"!!document.querySelector({selector!r})"):
            return False
        self.tab.click(selector)
        self.settle()
        return True

    def tap_sheet_button(self, label: str) -> bool:
        """Tap a footer button of the open sheet, by the words on it."""
        button = self.tab.locator(".hand-sheet__foot button").filter(has_text=label).first
        try:
            button.click(timeout=TIMEOUT_MS)
        except PLAYWRIGHT_TIMEOUT:
            return False
        self.settle()
        return True

    def fill(self, selector: str, value: str) -> None:
        """Type into a field, the way a person does: focus, then keystrokes."""
        assert self.wait(f"!!document.querySelector({selector!r})"), (
            f"there is no {selector} to type into.\n{self.describe()}"
        )
        self.tab.fill(selector, value)

    def press(self, key: str) -> None:
        """Send a real key. ``Escape`` is how a sheet is dismissed without a thumb."""
        self.tab.keyboard.press(key)
        self.settle()

    def settle(self, quiet_ms: int = 150) -> None:
        """Let the screen finish what the last interaction started.

        The app's own work after a tap is a promise chain, not a timer - a sheet is appended
        synchronously and a request is answered by this harness's scripted route - so a
        short quiet wait is the honest version of "the click has been handled", and it does
        not depend on how long a scripted answer takes.
        """
        self.tab.wait_for_timeout(quiet_ms)

    def wait(self, expression: str, *, timeout_ms: int = TIMEOUT_MS) -> bool:
        """Wait for ``expression`` to become true, and report rather than raise.

        The whole point of returning a bool is the failure message: a test that asserts on
        this can print the screen's state (``describe``) instead of a Playwright timeout,
        which says only that fifteen seconds went by.
        """
        try:
            self.tab.wait_for_function(f"() => ({expression})", timeout=timeout_ms)
            return True
        except PLAYWRIGHT_TIMEOUT:
            return False

    # -- reading -------------------------------------------------------------
    def sheet(self) -> dict[str, Any]:
        """The open sheet, or ``{'open': False}``."""
        return self.tab.evaluate(SHEET)

    def sheet_open(self) -> bool:
        return bool(self.tab.evaluate("document.querySelector('.hand-sheet') !== null"))

    def text(self, selector: str) -> str:
        """The visible text of one element, trimmed."""
        return (self.tab.evaluate(f"(document.querySelector({selector!r}) || {{}}).innerText") or "").strip()

    def exists(self, selector: str) -> bool:
        return bool(self.tab.evaluate(f"document.querySelector({selector!r}) !== null"))

    def toasts(self) -> list[dict[str, str]]:
        """The toasts on screen that have not faded out yet."""
        return self.tab.evaluate(TOASTS)

    def requests(self) -> list[dict[str, Any]]:
        """Every request the page has made since the screen was mounted."""
        return self.tab.evaluate("window.__requests")

    def requests_to(self, fragment: str, *, method: str | None = None) -> list[dict[str, Any]]:
        """The requests whose path contains ``fragment``, optionally of one method."""
        return [
            seen
            for seen in self.requests()
            if fragment in seen["path"] and (method is None or seen["method"] == method)
        ]

    def persisted(self) -> dict[str, str]:
        """Everything in device storage, keys and all."""
        return self.tab.evaluate(PERSISTED)

    def stored_session(self) -> dict[str, str]:
        """The keys the session lives under, as a device would hold them."""
        held = self.persisted()
        return {
            key: value
            for key, value in held.items()
            if key.split(".", 1)[-1] in {
                "auth_token",
                "auth_user",
                "auth_expires_at",
                "auth_token_version",
            }
        }

    def in_memory_session(self) -> Any:
        """The session the app is drawing from, which is the other half of signing out."""
        return self.tab.evaluate("window.MobileScreens.store.getState().session")

    def signed_out(self) -> int:
        """How many times the screen has called its sign-out callback."""
        return int(self.tab.evaluate("window.__signedOut"))

    def rejections(self) -> list[str]:
        """Promise rejections nothing caught - how a ``void`` handler fails silently."""
        return self.tab.evaluate("window.__rejections")

    def screenshot(self, path: pathlib.Path) -> pathlib.Path:
        """A picture of the page, for the failure message of a test that just went red."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.tab.screenshot(path=str(path), full_page=True)
        return path

    def describe(self) -> str:
        """Everything worth knowing when an assertion here fails."""
        lines = [
            f"  url:       {self.tab.url}",
            f"  sheet:     {self.sheet()}",
            "  requests:  "
            + str([seen["method"] + " " + seen["path"] for seen in self.requests()]),
            f"  session:   {self.in_memory_session()}",
            f"  stored:    {sorted(self.stored_session())}",
            f"  toasts:    {self.toasts()}",
        ]
        for label, found in (
            ("page errors", self.page_errors),
            ("console errors", self.console_errors),
            ("rejections", self.rejections()),
        ):
            for item in found:
                lines.append(f"  {label}: {item.splitlines()[0][:300]}")
        return "\n".join(lines)


@contextlib.contextmanager
def phone(
    browser: Any,
    base_url: str,
    *,
    width: browser_support.Width = browser_support.PHONE,
) -> Iterator[Handset]:
    """A phone-sized tab with the shims installed, open on the harness's page."""
    context = width.context(browser)
    context.add_init_script(SHIMS)
    errors: list[str] = []
    console_errors: list[str] = []
    try:
        tab = context.new_page()
        tab.on("pageerror", lambda exc: errors.append(str(exc)))
        tab.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        tab.goto(f"{base_url}/index.html", wait_until="load")
        tab.wait_for_function("() => !!window.MobileScreens", timeout=TIMEOUT_MS)
        yield Handset(tab, base_url, errors, console_errors)
    finally:
        context.close()


#: A signed-in worker, the shape ``persistSessionFromLoginResponse`` writes.
WORKER_SESSION = {
    "user": {
        "id": "42",
        "name": "Seed Worker",
        "role": "worker",
        "email": "seed.worker@example.test",
        "phone": None,
    },
    "token": "the-stored-token",
    "expiresAt": "2027-01-01T00:00:00Z",
    "tokenVersion": 3,
}
