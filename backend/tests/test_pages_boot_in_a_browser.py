"""Every page the app serves, opened in a browser, at a phone and a desktop width.

WHY THIS EXISTS
---------------
The link pages were dead in browsers for as long as they had existed. ``quick.html`` asked
for ``quick.js`` from a URL one segment deep whose segment was a token, so the browser asked
for ``/q/quick.js``, the token route answered with the page itself as ``text/html``, and
strict MIME checking refused to execute it. The screen was the two placeholders the page
ships with - "Checking this link…", "Checking your location…" - which reads as a slow
connection, so nobody reported it. The enrollment page had the same bug, and the Cloudflare
Worker in ``deploy/`` proxied ``/q/*`` and ``/enroll/*`` straight into it.

Every test in this repository before this file asked the *server* what it would send, or ran
the frontend in a VM with a stubbed DOM (``frontend_vm.py``). Neither can see this: the
server sends exactly what it was asked for, and the stub executes whatever script it is
handed. Only a browser refuses, and only a browser can be asked whether the page it put on
the screen is the page the code describes.

WHAT IS ASSERTED
----------------
For each page at each width, three things:

1. the page's own script *ran* - judged by a visible effect, not by a global being defined,
   so a script that loads and then throws while drawing is a failure too;
2. no script the page requested came back as anything but JavaScript - the MIME rule,
   measured rather than described;
3. nothing threw.

And on the app shell, one more: the width reached the application's own layout switch
(``Device.mode``), so "phone width" means the phone UI was actually drawn rather than a
narrow desktop one.

Runs with the browser the machine has - see ``browser.py``. With none installed, it skips
and names ``python -m playwright install chromium``.
"""

from __future__ import annotations

import pytest

import browser as browser_support
from browser import DESKTOP, PAGES, PHONE

pytestmark = pytest.mark.regression


def token_path(page, link_paths: dict[str, str]) -> str:
    """The path to open, with a real token where the page expects one."""
    if "{quick}" in page.path:
        return page.path.format(quick=link_paths["quick"].rsplit("/", 1)[-1])
    if "{enroll}" in page.path:
        return page.path.format(enroll=link_paths["enroll"].rsplit("/", 1)[-1])
    return page.path


@pytest.mark.parametrize("width", (PHONE, DESKTOP), ids=lambda width: width.name)
@pytest.mark.parametrize(
    "page",
    PAGES,
    ids=lambda page: page.name.replace(" ", "-"),
)
def test_its_script_actually_executes(browser, site, link_paths, page, width, tmp_path):
    """The page is the page, at both widths, or this says which part of it is not."""
    seen = browser_support.boot(
        browser, site, page, width, path=token_path(page, link_paths), shots=tmp_path
    )

    assert seen.ran, (
        f"{page.name} did not run its script ({page.script}) at {width.name} width.\n"
        f"{seen.describe()}\n"
        f"A screenshot of what the browser was showing is in {tmp_path}"
    )
    assert seen.refused_scripts == [], (
        f"{page.name} asked for a script and was given something a browser will not "
        f"execute, so none of it ran.\n{seen.describe()}"
    )
    assert seen.page_errors == [], f"{page.name} threw while drawing itself.\n{seen.describe()}"
    if page.answered:
        assert seen.answered, (
            f"{page.name} ran its script but never replaced its own placeholder: the link "
            f"it was opened with did not resolve.\n{seen.describe()}"
        )


def test_a_refused_link_does_not_send_the_worker_into_their_phone_settings(browser, site, tmp_path):
    """A dead link used to ask for a location fix, and then say "Allow location and try again".

    Measured on the real page: the link page ran ``locate()`` at boot, before the server had
    answered, so a token that had been revoked (or expired, or used up) still prompted for
    location and then put "Location is blocked. Allow location for this page and try again"
    under a message saying the link was not valid. A worker reads that as one instruction:
    go into Settings, grant location, come back - and be refused again, identically.

    The fix is an ordering, so this pins the ordering: the fix is only asked for once the
    link is known to be usable. ``innerText`` is what the reader sees, and it excludes
    hidden elements, so the second assertion is about the screen rather than the DOM.
    """
    page = next(p for p in PAGES if p.path.startswith("/q/"))
    never_issued = "/q/this-token-was-never-issued"
    seen = browser_support.boot(
        browser, site, page, PHONE, path=never_issued, shots=tmp_path, watch_geolocation=True
    )

    assert seen.ran, f"the page did not boot at all, so this proves nothing:\n{seen.describe()}"
    assert seen.answered is False, (
        f"this token is supposed to be refused; the page painted an answer for it instead:\n"
        f"{seen.describe()}"
    )
    assert seen.geolocation_calls == 0, (
        f"a refused link asked the phone for a location fix {seen.geolocation_calls} time(s).\n"
        f"{seen.describe()}"
    )
    for instruction in ("Allow location", "Checking your location"):
        assert instruction not in seen.content, (
            f"a refused link is still telling the worker {instruction!r}, which cannot "
            f"change the answer.\n{seen.describe()}"
        )


def test_the_width_reaches_the_applications_own_layout_switch(browser, site, link_paths, tmp_path):
    """A narrow viewport that still draws the desktop UI is not a phone test.

    ``Device.isMobile`` is a media query in the app, and the two worker layouts are
    different DOM rather than different CSS. If this fails, every "phone width" assertion
    above is describing the desktop screen squeezed into 390px.
    """
    shell = next(page for page in PAGES if page.layout)
    modes = {}
    for width in (PHONE, DESKTOP):
        modes[width.name] = browser_support.boot(browser, site, shell, width, shots=tmp_path).layout_mode

    assert modes == {"phone": "mobile", "desktop": "desktop"}, (
        f"the app's own layout switch read {modes} for a 390px and a 1280px viewport"
    )


def test_the_paths_under_test_are_the_paths_the_product_sends(link_paths):
    """The pages are opened at the *token* URLs the console issues, not at the files.

    Which is the whole trap: ``/q/<token>`` is one segment deep and that segment is a
    token, so a test that opened ``/quick.html`` would load the same markup with a URL
    where every relative path happens to resolve - and would have passed on the day both
    pages were dead in a browser.
    """
    assert link_paths["quick"].startswith("/q/")
    assert link_paths["enroll"].startswith("/enroll/")
    for page in PAGES:
        if page.path == "/":
            continue
        opened = token_path(page, link_paths)
        assert not opened.endswith(".html"), (
            f"{page.name} would be opened at {opened}, which is the file rather than the "
            "route a worker is sent"
        )
        assert len(opened.split("/")) == 3, f"{page.name} is not one segment deep: {opened}"
