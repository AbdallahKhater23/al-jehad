"""Playwright's sync API must not leave the main thread's event loop marked as running.

WHY THIS EXISTS
---------------
Playwright's synchronous API runs an asyncio event loop from a greenlet, and every time it
hands control back to the caller it re-asserts ``asyncio._set_running_loop(loop)`` (see
``playwright._impl._sync_base.SyncBase._sync``). Its context manager closes that loop on exit
but never clears the flag, so after the browser session fixture tears down the main thread is
left believing a loop is running - over a loop that is now closed.

That flag is per-thread and outlives the fixture. Any ``asyncio.run(...)`` later in the same
pytest process, in a suite that never touched a browser, then dies with::

    RuntimeError: asyncio.run() cannot be called from a running event loop

which reads as a bug in the suite that happens to run after the browser tests, not as
Playwright's residue. ``browser.release_event_loop`` is the cleanup, and ``open_browser``
calls it in a ``finally`` so it runs even when launching the browser failed.

That cleanup only helps once the session has ended, though, and a session-scoped session ends
at the end of the run. While it is open the mark must *stay* set - Playwright reaches its own
dispatcher through it, and a cleared mark makes the very next ``page.goto`` fail with "no
running event loop". So the two requirements can never hold at once, and the fixture's
lifetime is what has to give: the browser fixture is scoped to the module, so Playwright is
gone - and the mark released - before the JSON-payload, face-engine and network-hardening
suites (the three that call ``asyncio.run``) get their turn.

WHY A STAND-IN, NOT PLAYWRIGHT
------------------------------
The stand-in marks a loop running and closes it without clearing the flag - byte for byte the
behaviour being defended against. That lets this file run on a machine with no Playwright at
all, which is the machine most likely to be surprised by the leak, and it keeps the assertion
about the *flag* rather than about a browser download.
"""

from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

import browser as browser_support
import harness

pytestmark = pytest.mark.regression


def _run_something() -> str:
    """The smallest real ``asyncio.run`` call - precisely the operation the leak breaks."""
    return asyncio.run(asyncio.sleep(0, result="ok"))


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = self

    def launch(self, channel: str | None = None) -> _FakeBrowser:
        return _FakeBrowser()


class _LeakySyncPlaywright:
    """``sync_playwright()``, reduced to the two lines that matter.

    On enter it marks a fresh loop as the running loop, exactly as a sync call does; on exit
    it closes the loop and deliberately leaves the flag set, exactly as Playwright does. If
    this class ever stopped leaking, the test below would pass for the wrong reason - so the
    test asserts the flag is set *inside* the session, and a separate case proves the raw leak
    really does break ``asyncio.run``.
    """

    def __enter__(self) -> _FakePlaywright:
        self._loop = asyncio.new_event_loop()
        asyncio.events._set_running_loop(self._loop)
        return _FakePlaywright()

    def __exit__(self, *exc_info: object) -> bool:
        self._loop.close()
        return False


@pytest.fixture
def fake_playwright(monkeypatch):
    """Replace ``playwright`` for one test with the leaking stand-in."""
    package = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = _LeakySyncPlaywright  # type: ignore[attr-defined]
    package.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return sync_api


def test_a_closed_loop_left_marked_running_really_breaks_asyncio_run():
    """The symptom, on its own: this is the failure the guard exists to prevent.

    Asserted independently of the fixture so the cure below cannot pass vacuously - if a
    future Python stopped caring about the flag, this case would fail and say the guard's
    premise changed, rather than silently letting the guard stop proving anything.
    """
    loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(loop)
    loop.close()
    # ``asyncio.run`` refuses before it ever awaits, so the coroutine is closed by hand - the
    # alternative is a RuntimeWarning that reads like a leak in this test rather than the point.
    refused = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="running event loop"):
            asyncio.run(refused)
    finally:
        refused.close()
        asyncio.events._set_running_loop(None)
    assert _run_something() == "ok"


def test_open_browser_clears_the_running_loop_flag_on_the_way_out(fake_playwright):
    """The session tears down clean and leaves ``asyncio.run`` usable for whatever follows."""
    with browser_support.open_browser() as browser:
        assert browser is not None
        # Set while the session is open, which is why the leak is invisible until it closes.
        assert asyncio.events._get_running_loop() is not None

    assert asyncio.events._get_running_loop() is None, (
        "Playwright's running-loop flag outlived the browser session, so a later asyncio.run "
        "in this process would fail"
    )
    assert _run_something() == "ok"


def test_open_browser_clears_the_flag_even_when_no_browser_will_start(fake_playwright, monkeypatch):
    """A launch that failed still entered Playwright, so it still leaves the flag behind.

    ``CHANNELS`` is every channel the machine does not have; the cleanup is in a ``finally``
    for exactly this case, because a checkout with a broken installation is a checkout whose
    browser fixtures raise - and the suite that runs next must not inherit the wreckage.
    """
    def cannot_launch(playwright: object):
        raise browser_support.NoBrowser("no Chromium to drive the pages with")

    monkeypatch.setattr(browser_support, "launch", cannot_launch)
    with pytest.raises(browser_support.NoBrowser):
        with browser_support.open_browser():
            pass  # pragma: no cover - the launch above always raises first

    assert asyncio.events._get_running_loop() is None, (
        "a failed browser launch left Playwright's running-loop flag behind"
    )
    assert _run_something() == "ok"


def test_the_browser_fixture_does_not_outlive_the_module_that_uses_it():
    """The fixture's *lifetime* is half the fix, so pin it rather than trusting the comment.

    Everything above closes the session itself and therefore cannot see this failure. If the
    scope went back to ``session``, the browser modules would pass and the suites after them
    would fail - which is exactly the symptom this file exists to keep out.
    """
    import conftest

    marker = getattr(conftest.browser, "_fixture_function_marker", None) or getattr(
        conftest.browser, "_pytestfixturefunction", None
    )
    assert marker is not None, "cannot read the browser fixture's marker; pytest internals moved"
    assert marker.scope == "module", (
        f"the browser fixture is {marker.scope!r}-scoped, so Playwright's running-loop flag "
        "outlives the browser modules and asyncio.run fails in every suite that follows them"
    )


def test_release_event_loop_is_a_no_op_when_nothing_is_running():
    """Clearing what is not set is not an error, and the clean state stays clean."""
    asyncio.events._set_running_loop(None)
    browser_support.release_event_loop()
    assert asyncio.events._get_running_loop() is None
    assert _run_something() == "ok"


def test_no_module_both_drives_a_browser_and_calls_asyncio_run():
    """The one arrangement the module-scoped fixture cannot survive, checked rather than assumed.

    Two things are true at once and cannot be reconciled *inside* one test: Playwright needs the
    running-loop mark, and ``asyncio.run`` refuses to start while it is set. The split that
    works is by module - the browser modules finish (releasing the mark) before the suites that
    call ``asyncio.run`` begin. Under ``pytest -n auto`` that still holds, and not by luck: each
    worker is handed its tests in collection order, so a module's tests are contiguous *within a
    worker* and the module-scoped fixture is torn down at the module boundary, before the next
    module's first test - even though the two modules' tests interleave across workers.

    So the surviving failure mode is a single module that does both: it would keep the mark set
    for the duration of its own tests and its own ``asyncio.run`` call would die with "cannot be
    called from a running event loop" - which reads as a bug in that suite. ``pytest -n 2`` on
    such a pair of tests was reproduced while this was written. Hence a test: the arrangement is
    invisible in a serial run of a browser module, and cheap to catch here.
    """
    def requests_the_browser(source: str) -> bool:
        for line in source.splitlines():
            match = re.match(r"def (test_\w+)\(([^)]*)\)", line)
            if match and re.search(r"\bbrowser\b", match.group(2)):
                return True
        return False

    offenders = []
    for path in sorted(Path(harness.BACKEND_DIR, "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if requests_the_browser(source) and re.search(r"\basyncio\.run\(", source):
            offenders.append(path.name)

    assert not offenders, (
        f"{offenders} both drive a browser and call asyncio.run. Inside one module the mark is "
        "set for the whole session, so that call fails with 'asyncio.run() cannot be called "
        "from a running event loop' - split the module, or run the async work through "
        "browser_support.release_event_loop"
    )
