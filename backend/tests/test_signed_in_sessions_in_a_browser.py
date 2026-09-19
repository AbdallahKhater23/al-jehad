"""The two signed-in screens, in a browser: a worker gets a handset, an admin a console.

WHY THIS EXISTS
---------------
The console's module was moved out of ``index.html`` so a worker's phone stops downloading
the back office, and the console fetches it on demand instead
(``App.loadConsoleModule``). Two things can go wrong with that move and neither is visible
to a test that only reads the server or runs the frontend in a VM:

1. the deferral is undone - somebody puts ``admin_modules.js`` back in ``index.html`` and
   every phone at a gate pays for it again, while every screen still looks correct; and
2. the console signs an administrator in and then never paints, because the on-demand fetch
   failed, was refused by strict MIME checking, or raced the first frame.

The same is true of the role split itself. ``App.renderApp`` sends a worker to
``renderWorkerPortal`` and an administrator to ``renderAdminConsole``, and each branch then
picks a phone or a desktop layout from ``Device.isMobile``. A branch that renders the *other*
role's screen is a real failure - an administrator's console shown to a worker is both a
usability bug and an authorization one - and it is only observable in a browser.

WHAT IS ASSERTED
----------------
Each role, signed in through the real form against the live server and the seeded database,
at the width the width's own layout belongs to:

* the role's own layout is on the page, and the *other* width's layout for that role is not
  (so "phone" is not the desktop screen squeezed into 390px);
* a worker's session never has the console's content host, and never fetches the console's
  module - while an administrator's session does both;
* the console module is fetched *after* sign-in, not shipped in the initial payload: an
  anonymous visit to the shell is watched for the same request and must not make it.

Runs with the browser the machine has - see ``browser.py``. With none installed, it skips
and names ``python -m playwright install chromium``.
"""

from __future__ import annotations

import pytest

import browser as browser_support
import harness

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Who signs in, and the screen they are owed
# ---------------------------------------------------------------------------
#: Credentials come from the seeded roster, not from literals: the harness reseeds these
#: accounts before every test, so a password here that drifted from ``SEED_USERS`` would be
#: a test that fails for a reason unrelated to the application.
WORKER_LANDED = (
    "document.querySelector('.hand-app') !== null"
    " && document.querySelector('.hand-credit') !== null"
)
CONSOLE_LANDED = (
    "document.getElementById('adminContent') !== null"
    " && document.getElementById('adminContent').children.length > 0"
)

WORKER_SIGN_IN = browser_support.Identity(
    role="worker",
    user_id=harness.WORKER,
    email=harness.EMAILS[harness.WORKER],
    password=harness.PASSWORDS[harness.WORKER],
    landed=WORKER_LANDED,
    why="the handset and its credit line are the worker's own DOM, drawn only once a session exists",
)
ADMIN_SIGN_IN = browser_support.Identity(
    role="administrator",
    user_id=harness.ADMIN,
    email=harness.EMAILS[harness.ADMIN],
    password=harness.PASSWORDS[harness.ADMIN],
    landed=CONSOLE_LANDED,
    why=(
        "the console writes a tab into #adminContent only after admin_modules.js has been "
        "fetched and parsed, so a painted console is proof the deferred module arrived"
    ),
)
IDENTITIES = {"worker": WORKER_SIGN_IN, "administrator": ADMIN_SIGN_IN}

#: role -> width -> (the layout that width draws, the layout it must not).
#:
#: The desktop worker is a two-column split where the phone worker is a stack with a tab
#: bar; the desktop console has a rail where the phone console has a head with a tab strip.
#: Each pair is asserted both ways, because "the right one is there" and "the wrong one is
#: not" are different bugs - a repaint that leaves the old layout in the DOM passes the
#: first and fails the second.
LAYOUTS = {
    ("worker", "phone"): (".hand-tabs", ".hand-desk"),
    ("worker", "desktop"): (".hand-desk", ".hand-tabs"),
    ("administrator", "phone"): (".admin-mobile-head", ".admin-rail"),
    ("administrator", "desktop"): (".admin-rail", ".admin-mobile-head"),
}


def _sign_in(browser, site, role: str, width, tmp_path, *, until: str | None = None, probe=None):
    """Sign in and return the observation, with the usual assertions applied once."""
    seen = browser_support.sign_in(
        browser, site, IDENTITIES[role], width, until=until, probe=probe, shots=tmp_path
    )
    assert seen.ran, (
        f"signing in as the {role} did not reach that role's screen.\n{seen.describe()}\n"
        f"A screenshot of what the browser was showing is in {tmp_path}"
    )
    assert seen.refused_scripts == [], (
        f"the {role} session asked for a script and was given something a browser will not "
        f"execute.\n{seen.describe()}"
    )
    assert seen.page_errors == [], f"the {role} session threw while drawing itself.\n{seen.describe()}"
    return seen


@pytest.mark.parametrize(
    "role,width_name",
    sorted(LAYOUTS),
    ids=lambda value: value,
)
def test_each_role_renders_its_own_layout(browser, site, role, width_name, tmp_path):
    """A worker gets the handset, an administrator the console, and each at its own width."""
    own, other = LAYOUTS[(role, width_name)]
    width = browser_support.PHONE if width_name == "phone" else browser_support.DESKTOP
    seen = _sign_in(
        browser,
        site,
        role,
        width,
        tmp_path,
        until=f"document.querySelector({own!r}) !== null",
        probe={
            "own": f"document.querySelector({own!r}) !== null",
            "other": f"document.querySelector({other!r}) !== null",
            "console_host": "document.getElementById('adminContent') !== null",
        },
    )

    assert seen.probes["own"], (
        f"the {role} at {width_name} width did not draw {own}, its own layout.\n{seen.describe()}"
    )
    assert not seen.probes["other"], (
        f"the {role} at {width_name} width still has {other} in the page - the other width's "
        f"layout.\n{seen.describe()}"
    )
    # The width reached the application's own switch, so the layout was chosen rather than
    # inherited from whatever the previous screen happened to leave behind.
    expected_mode = "mobile" if width_name == "phone" else "desktop"
    assert seen.layout_mode == expected_mode, (
        f"the app's own layout switch read {seen.layout_mode!r} for the {role} at {width_name} "
        f"width.\n{seen.describe()}"
    )
    # A worker has no console: not the content host, wherever the module is.
    assert seen.probes["console_host"] is (role == "administrator"), (
        f"the {role} session's #adminContent presence was {seen.probes['console_host']!r}.\n"
        f"{seen.describe()}"
    )


def test_the_console_module_is_fetched_on_demand_and_only_by_the_console(browser, site, tmp_path):
    """The console's module is deferred, fetched after sign-in, and never a worker's cost.

    Three observations of one fact. The anonymous visit pins that the module is *not* in the
    initial payload - which is what makes the next two meaningful rather than "it happens to
    be cached by now". The administrator's fetch pins that deferring it did not strand the
    console without its code. The worker's pin is the point of the whole change.
    """
    shell = next(p for p in browser_support.PAGES if p.name == "app shell")
    anonymous = browser_support.boot(browser, site, shell, browser_support.PHONE, shots=tmp_path)
    assert anonymous.ran, f"the shell did not boot, so this proves nothing:\n{anonymous.describe()}"
    assert not anonymous.fetched(browser_support.CONSOLE_MODULE), (
        f"the console's module was requested before anybody signed in, so it is in the "
        f"initial payload again and every worker's phone pays for it.\n{anonymous.describe()}"
    )

    admin = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.DESKTOP,
        tmp_path,
        probe={"module": "typeof UI_MODULES !== 'undefined'"},
    )
    assert admin.fetched(browser_support.CONSOLE_MODULE), (
        f"the administrator's console painted without the browser ever requesting "
        f"{browser_support.CONSOLE_MODULE}: either it is in index.html after all, or the "
        f"console is drawing from something else.\n{admin.describe()}"
    )
    assert admin.probes["module"], (
        f"the console fetched {browser_support.CONSOLE_MODULE} but UI_MODULES is still "
        f"undefined - the module loaded and did not define itself.\n{admin.describe()}"
    )

    worker = _sign_in(
        browser,
        site,
        "worker",
        browser_support.PHONE,
        tmp_path,
        probe={"module": "typeof UI_MODULES !== 'undefined'"},
    )
    assert not worker.fetched(browser_support.CONSOLE_MODULE), (
        f"a worker's phone downloaded {browser_support.CONSOLE_MODULE}, which no worker "
        f"screen can use.\n{worker.describe()}"
    )
    assert not worker.probes["module"], (
        f"UI_MODULES exists in a worker's session.\n{worker.describe()}"
    )
