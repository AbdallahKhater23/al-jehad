"""A real browser and a real server, for the failure only they can show.

WHY THIS EXISTS
---------------
``quick.html`` and ``enroll.html`` are served at ``/q/<token>`` and ``/enroll/<token>`` -
one segment deep, where that segment is a token rather than a directory. Both loaded their
script with a bare ``src="quick.js"``, so the browser asked for ``/q/quick.js``, the token
route matched it and answered with the page itself as ``text/html``, and the browser
refused to execute it under strict MIME checking. Nothing on the screen said so: the two
placeholders the page ships with - "Checking this link…" and "Checking your location…" -
were the entire screen, which reads as a slow connection rather than as a broken asset.
The enrollment page had the same bug and nobody reported it.

``test_quick_links.py`` now pins the *shape* of that fix: every script ``src`` on both
pages is one level up, and each one comes back from the server as JavaScript. That is a
good test and it is also a test that had to know, in advance, that a relative path was the
problem. This module pins the *behaviour*: it opens the page in a browser and fails if the
script never ran - whatever the reason turns out to be.

It is the throwaway harness that found the bug, committed. That harness was a hand-written
preview server and a hand-edited HTML file kept outside the repository; its finding was
worth more than its code, so the code is here now: a real Chromium against the
application's own app object, on the suite's own test database.

WHAT IT DOES NOT DO
-------------------
It starts the server with uvicorn's lifespan switched *off*. The suite's own ``client``
fixture has already brought the application up once - migrations, the readiness gate, the
overtime and retention watchers - and a second startup would start those watchers twice,
then call ``face_engine.ENGINE.shutdown()`` on the way out and take the rest of the suite
down with it. The live server borrows that state and only serves.

No download is needed to run it. Playwright's bundled Chromium is used when it is present,
and a Chromium-based browser the machine already has - Edge, then Chrome - otherwise. With
neither, the browser tests skip with the command that fetches one.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# The browsers this can drive
# ---------------------------------------------------------------------------
#: In order of preference. ``None`` is Playwright's own Chromium - what
#: ``playwright install chromium`` downloads. The two channels are the Chromium-based
#: browsers a machine already has, so the suite runs on a checkout that has never
#: downloaded a browser; Edge first because it ships with Windows.
CHANNELS: tuple[str | None, ...] = (None, "msedge", "chrome")

#: What to tell somebody whose machine has none of them.
INSTALL_HINT = "python -m playwright install chromium"


#: Records each call the page makes for a location fix. Installed before the page's own
#: scripts run, so a page cannot ask for permission without this seeing it - which is the
#: only way to tell "the page did not ask" from "the page asked and the browser said no".
GEOLOCATION_SPY = """
(() => {
    window.__geolocationCalls = 0;
    if (!navigator.geolocation) return;
    const real = navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);
    navigator.geolocation.getCurrentPosition = function (...args) {
        window.__geolocationCalls += 1;
        return real(...args);
    };
})();
"""


class NoBrowser(RuntimeError):
    """Raised when no Chromium this module can drive is installed."""


def launch(playwright: Any):
    """The first browser that will start, or :class:`NoBrowser`.

    Each attempt is kept as its own line in the message: a bare "browser not found" hides
    whether the bundled one is missing, the channel is unsupported, or something else
    entirely is wrong with the installation.
    """
    attempts: list[str] = []
    for channel in CHANNELS:
        try:
            if channel is None:
                return playwright.chromium.launch()
            return playwright.chromium.launch(channel=channel)
        except Exception as exc:  # noqa: BLE001 - any failure here means "try the next one"
            attempts.append(f"{channel or 'bundled chromium'}: {str(exc).splitlines()[0]}")
    raise NoBrowser(
        "no Chromium to drive the pages with. Tried:\n  "
        + "\n  ".join(attempts)
        + f"\nInstall one with: {INSTALL_HINT}"
    )


#: Probed once per session: launching a browser is not free, and every browser test asks
#: the same question. ``(False, reason)`` means the fixture skips rather than errors - a
#: checkout with no browser installed should not fail the build, it should say what to
#: install. This is the same shape ``frontend_vm.NODE`` uses for the node-only suites.
_AVAILABILITY: tuple[bool, str] | None = None


def available() -> tuple[bool, str]:
    """Whether a browser can be started, and why not when it cannot."""
    global _AVAILABILITY
    if _AVAILABILITY is None:
        try:
            with open_browser():
                _AVAILABILITY = (True, "")
        except NoBrowser as missing:
            _AVAILABILITY = (False, str(missing))
    return _AVAILABILITY


@contextlib.contextmanager
def open_browser():
    """A browser, for the length of the session."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - a checkout without the dev extra
        raise NoBrowser(f"playwright is not installed ({exc}). Add it to requirements-dev.txt.") from exc

    with sync_playwright() as playwright:
        browser = launch(playwright)
        try:
            yield browser
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The server the browser talks to
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Site:
    """A running application, and the address it answers on."""

    url: str

    def __call__(self, path: str) -> str:
        return self.url.rstrip("/") + path


@contextlib.contextmanager
def serve(app: Any, *, host: str = "127.0.0.1", ready_seconds: float = 30.0):
    """Serve ``app`` on an ephemeral port until the context exits.

    Port 0 and a read of the socket that was actually bound, rather than a fixed port:
    another thread in another checkout may already be using whichever port looked free a
    moment ago, and a collision would present as a browser test that mysteriously fails.
    """
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=0,
        log_level="warning",
        access_log=False,
        # See the module docstring: the suite has already run the lifespan once, and a
        # second one starts a second overtime watcher and shuts the face engine down.
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="pages-in-a-browser", daemon=True)
    thread.start()

    deadline = time.monotonic() + ready_seconds
    while time.monotonic() < deadline:
        if server.started and server.servers:
            break
        if not thread.is_alive():
            raise RuntimeError("the live server thread exited before it was ready")
        time.sleep(0.05)
    else:
        raise RuntimeError(f"the live server did not come up within {ready_seconds:.0f}s")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Site(f"http://{host}:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=15)


# ---------------------------------------------------------------------------
# The widths, and what "the script ran" means on each page
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Width:
    name: str
    viewport: dict[str, int]
    is_mobile: bool
    has_touch: bool

    def context(self, browser: Any):
        # ``locale`` rather than the machine's: these pages pick their language from the
        # reader's, and a test that reads differently on a developer's laptop than in CI is
        # a test that fails for a reason nobody can see. ``en-US`` is the language the
        # markup is written in.
        return browser.new_context(
            viewport=self.viewport, is_mobile=self.is_mobile, has_touch=self.has_touch, locale="en-US"
        )


#: The two the app actually draws differently, either side of its own breakpoint
#: (``(max-width: 767px)`` in ``frontendjavascript.js``).
PHONE = Width("phone", {"width": 390, "height": 844}, True, True)
DESKTOP = Width("desktop", {"width": 1280, "height": 900}, False, False)
WIDTHS: tuple[Width, ...] = (PHONE, DESKTOP)


@dataclasses.dataclass(frozen=True)
class Page:
    """One page, and the evidence that its script executed.

    ``ran`` is a JavaScript expression that is true only after the page's own script has
    run. It is deliberately about a *visible effect* rather than a global: a page whose
    script was refused by the MIME rule has neither, but a page whose script ran and then
    threw while drawing has the global and not the effect - which is the more common bug
    and the one worth failing on.
    """

    name: str
    path: str
    script: str
    ran: str
    why: str
    #: Whether this page decides its own layout from the viewport, so the test can insist
    #: the width reached the app rather than assuming the media query fired.
    layout: bool = False
    #: For a page opened at a token: an expression that is true once the *server's answer*
    #: has been painted. Both link pages ship a "Checking…" placeholder and overwrite it,
    #: so an unreplaced one means the script ran into a link it could not use - which is a
    #: different failure from a script that never ran, and worth telling apart.
    answered: str | None = None


PAGES: tuple[Page, ...] = (
    Page(
        name="app shell",
        path="/",
        script="frontendjavascript.js",
        ran="document.getElementById('app').children.length > 0",
        why="index.html ships an empty #app; the sign-in screen in it is drawn by JavaScript",
        layout=True,
    ),
    Page(
        name="quick link",
        path="/q/{quick}",
        script="capture.js and quick.js",
        ran=(
            "typeof Capture !== 'undefined'"
            " && document.getElementById('photo-policy').textContent.length > 0"
        ),
        why=(
            "the photo policy line is written by quick.js after capture.js has loaded, so "
            "an empty one means one of the two never executed"
        ),
        answered="!/Checking/.test(document.getElementById('who').textContent)",
    ),
    Page(
        name="enrollment",
        path="/enroll/{enroll}",
        script="capture.js and enroll.js",
        ran=(
            "typeof Capture !== 'undefined'"
            " && document.getElementById('photo-policy').textContent.length > 0"
        ),
        why="the same two files, the same line, on the page nobody reported",
        answered="!/Checking/.test(document.getElementById('who').textContent)",
    ),
)


# ---------------------------------------------------------------------------
# One load, observed
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Observation:
    """What the browser did with one page at one width."""

    page: Page
    width: Width
    url: str
    ran: bool
    answered: bool | None
    layout_mode: str | None
    #: How many times the page asked the browser for a location fix, when it was watched.
    geolocation_calls: int | None
    scripts: list[dict[str, Any]]
    page_errors: list[str]
    console_errors: list[str]
    content: str
    #: Extra expressions the caller asked to be evaluated once the screen was up, keyed by
    #: name. A dataclass field rather than one per assertion, because the questions a
    #: signed-in screen raises ("is this selector here", "is that module defined") are
    #: known to the test and not to this module.
    probes: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def refused_scripts(self) -> list[dict[str, Any]]:
        """Script responses that were not JavaScript - the MIME refusal, measured."""
        return [s for s in self.scripts if s["status"] != 200 or "javascript" not in s["content_type"]]

    def fetched(self, filename: str) -> bool:
        """Whether the page pulled ``filename`` itself, by name.

        The deferred console module is requested by a ``<script>`` the page appends after
        sign-in, so its presence in ``scripts`` is the difference between "the console drew"
        and "the console module was in the initial payload" - which is the whole point of
        deferring it.
        """
        return any(
            s["url"].split("?", 1)[0].rsplit("/", 1)[-1] == filename for s in self.scripts
        )

    def describe(self) -> str:
        """The whole observation, in the form a failure message needs."""
        lines = [
            f"{self.page.name} at {self.width.name} width ({self.page.path})",
            f"  url:      {self.url}",
            f"  ran:      {self.ran}   <- {self.page.why}",
        ]
        if self.answered is not None:
            lines.append(f"  answered: {self.answered}   <- the link was accepted and painted")
        if self.geolocation_calls is not None:
            lines.append(f"  location fixes requested: {self.geolocation_calls}")
        if self.layout_mode:
            lines.append(f"  layout:   {self.layout_mode}")
        lines.append("  scripts:")
        lines.extend(
            f"    {s['status']} {s['content_type'] or '(no content-type)'}  {s['url']}" for s in self.scripts
        ) or lines.append("    (none requested)")
        if self.refused_scripts:
            lines.append("  REFUSED: a script that does not come back as JavaScript is not executed")
        for label, found in (("page errors", self.page_errors), ("console errors", self.console_errors)):
            for item in found:
                lines.append(f"  {label}: {item.splitlines()[0][:300]}")
        lines.append(f"  body text, as the reader gets it: {self.content[:400]!r}")
        return "\n".join(lines)


def boot(
    browser: Any,
    site: Site,
    page: Page,
    width: Width,
    path: str | None = None,
    shots: Path | None = None,
    watch_geolocation: bool = False,
) -> Observation:
    """Load ``page`` at ``width`` and report what happened, without judging it.

    ``shots`` is where to write a PNG of the page when it did not boot. The screenshot is
    the difference between "a JavaScript expression came back false" and being able to see
    what the browser was showing when it did, which is the whole reason the throwaway
    harness was worth building in the first place.
    """
    console_errors: list[str] = []
    page_errors: list[str] = []
    scripts: list[dict[str, Any]] = []

    context = width.context(browser)
    if watch_geolocation:
        context.add_init_script(GEOLOCATION_SPY)
    try:
        tab = context.new_page()
        tab.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        tab.on("pageerror", lambda exc: page_errors.append(str(exc)))
        tab.on(
            "response",
            lambda response: scripts.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "content_type": (response.headers or {}).get("content-type", ""),
                }
            )
            if _is_script(response.url)
            else None,
        )

        url = site(path if path is not None else page.path)
        tab.goto(url, wait_until="load")
        # The pages fetch their own state after the load event, so the effect that proves
        # the script ran is not necessarily there yet. Wait for it rather than sleep.
        with contextlib.suppress(Exception):
            tab.wait_for_function(f"() => ({page.ran})", timeout=8000)

        ran = bool(tab.evaluate(f"({page.ran})"))
        answered = bool(tab.evaluate(f"({page.answered})")) if page.answered else None
        if not ran and shots is not None:
            shots.mkdir(parents=True, exist_ok=True)
            tab.screenshot(
                path=str(shots / f"{page.name.replace(' ', '-')}-{width.name}.png"), full_page=True
            )

        return Observation(
            page=page,
            width=width,
            url=url,
            ran=ran,
            answered=answered,
            geolocation_calls=(
                int(tab.evaluate("window.__geolocationCalls")) if watch_geolocation else None
            ),
            layout_mode=tab.evaluate("typeof Device === 'undefined' ? null : Device.mode") if page.layout else None,
            scripts=scripts,
            page_errors=page_errors,
            console_errors=console_errors,
            content=tab.evaluate("document.body.innerText") or "",
        )
    finally:
        context.close()


def _is_script(url: str) -> bool:
    """Whether this response is worth watching: a script this origin serves.

    Filtered by extension rather than by resource type, because the failure being watched
    for is a *script request answered as a document* - and a browser that has already
    decided the response is not a script reports exactly that.
    """
    without_query = url.split("?", 1)[0]
    return without_query.endswith(".js") and "/api/" not in without_query


# ---------------------------------------------------------------------------
# Signing in, and the screen each role is owed
# ---------------------------------------------------------------------------
#: The console's module, and the reason this whole section exists. No worker screen can
#: reach a line of it and it is the largest file in the frontend, so it is not in
#: ``index.html``: the console fetches it on demand, once per session, after an
#: administrator signs in. A test that only checked ``#adminContent`` was painted would
#: still pass on the day somebody put it back in ``index.html`` and paid for it on every
#: phone at a gate - so what is pinned is the *fetch*.
CONSOLE_MODULE = "admin_modules.js"


@dataclasses.dataclass(frozen=True)
class Identity:
    """Somebody the suite can sign in as, and the screen their role is owed.

    ``landed`` is a JavaScript expression that is true only once the role's own screen is
    on the page, with the same contract as :attr:`Page.ran` and the same reason for it: a
    global being defined says a file loaded, not that the role got its screen.
    """

    role: str
    user_id: str
    email: str
    password: str
    landed: str
    why: str


def sign_in(
    browser: Any,
    site: Site,
    identity: Identity,
    width: Width,
    *,
    until: str | None = None,
    probe: dict[str, str] | None = None,
    shots: Path | None = None,
) -> Observation:
    """Fill in the real sign-in form and report what the role landed on.

    Through the form, not an injected token: the live server and the seeded database are
    already here, and a session planted in ``localStorage`` would skip the one step whose
    failure (a login that authenticates and then renders nothing) is worth catching.

    ``until`` waits for a narrower effect than ``identity.landed`` when the test knows which
    width's layout it asked for; ``probe`` evaluates extra expressions once the screen is
    up. Both are read *before* the tab closes, which is the only moment they can be.
    """
    console_errors: list[str] = []
    page_errors: list[str] = []
    scripts: list[dict[str, Any]] = []

    context = width.context(browser)
    try:
        tab = context.new_page()
        tab.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        tab.on("pageerror", lambda exc: page_errors.append(str(exc)))
        tab.on(
            "response",
            lambda response: scripts.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "content_type": (response.headers or {}).get("content-type", ""),
                }
            )
            if _is_script(response.url)
            else None,
        )

        url = site("/")
        tab.goto(url, wait_until="load")
        tab.wait_for_selector("#loginForm", timeout=8000)
        tab.fill("#userId", identity.user_id)
        tab.fill("#email", identity.email)
        tab.fill("#password", identity.password)
        tab.click("#loginForm button[type=submit]")
        # The console paints only after its module has been fetched and parsed, so this
        # single wait covers the network round trip and the render that depends on it.
        with contextlib.suppress(Exception):
            tab.wait_for_function(f"() => ({until or identity.landed})", timeout=15000)

        ran = bool(tab.evaluate(f"({identity.landed})"))
        if not ran and shots is not None:
            shots.mkdir(parents=True, exist_ok=True)
            tab.screenshot(
                path=str(shots / f"{identity.role.replace(' ', '-')}-{width.name}.png"),
                full_page=True,
            )

        page = Page(
            name=f"{identity.role} session",
            path="/",
            script="frontendjavascript.js",
            ran=identity.landed,
            why=identity.why,
            layout=True,
        )
        return Observation(
            page=page,
            width=width,
            url=url,
            ran=ran,
            answered=None,
            geolocation_calls=None,
            layout_mode=tab.evaluate("typeof Device === 'undefined' ? null : Device.mode"),
            scripts=scripts,
            page_errors=page_errors,
            console_errors=console_errors,
            content=tab.evaluate("document.body.innerText") or "",
            probes={name: tab.evaluate(f"({expression})") for name, expression in (probe or {}).items()},
        )
    finally:
        context.close()
