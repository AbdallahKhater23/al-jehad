"""Fixtures for the security-baseline suite.

The heavy lifting (database isolation, the face-engine and detector stubs, the outbound-network
guard) lives in ``harness.py`` and runs at import time. See that module for the
safety rules this suite depends on.
"""

from __future__ import annotations

import pytest

import browser as browser_support
import harness
from harness import FAKE_ENGINE, jpeg_bytes as _jpeg_bytes


@pytest.fixture(scope="session", autouse=True)
def _outbound_network_guard():
    """No test may reach the internet (today's alert path has hardcoded creds).

    Session-scoped and autouse, so this is also where the throwaway working directory is
    entered - before the first test body, and before ``app_module`` imports the app. It is
    entered here and not at ``harness`` import because pytest-xdist has each worker collect
    its nodeids relative to its working directory: a ``chdir`` during import left every
    worker collecting nothing and the suite reporting "no tests ran". See safety rule 1 and
    ``harness.enter_temp_root``.
    """
    harness.enter_temp_root()
    harness.install_outbound_guard()
    return harness.OUTBOUND


@pytest.fixture(scope="session")
def outbound():
    """Recorded outbound HTTP attempts made by the application."""
    return harness.OUTBOUND



@pytest.fixture(scope="session")
def app_module():
    """The application under test, imported once per session."""
    # The temp working directory before the app is imported, whatever order pytest sets the
    # session fixtures up in. See ``harness.enter_temp_root``.
    harness.enter_temp_root()

    import main

    # Refuse to proceed if the app is somehow pointed at the live database. This runs
    # before any test body, because the failure mode it guards against is silent data
    # loss in the real payroll database.
    harness.assert_database_isolation()

    # Redirect every file tree the application writes into - biometric templates, reference
    # selfies, punch frames, quick-link photos - into the temp directory. Each module reads its
    # global at call time, so setting them here is enough, and they are set to stable paths
    # inside the ``current`` junction so the per-test reset rotates them along with the
    # database. See ``harness.FILE_TREES``.
    harness.redirect_file_directories()
    return main


@pytest.fixture(scope="session")
def client(app_module):
    from fastapi.testclient import TestClient

    # The context manager form also honours any startup/shutdown handlers the
    # fixed application will introduce.
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def face():
    """The face-engine stub, so a test can set FACE_MODE / FACE_COUNT."""
    return FAKE_ENGINE


@pytest.fixture(scope="session")
def jpeg() -> bytes:
    return _jpeg_bytes()


@pytest.fixture(autouse=True)
def deterministic_state(app_module):
    """Pristine database + seed data + cleared rate-limit buckets, before each test."""
    harness.reset_database(app_module)
    yield


# ---------------------------------------------------------------------------
# A real browser against a real server
# ---------------------------------------------------------------------------
# See ``browser.py`` for why these exist and what they will not do. The short version:
# the only assertion that catches "this page's script never executed, and the screen looks
# like a slow connection" is a browser saying so.
@pytest.fixture(scope="module")
def browser():
    """A Chromium, or a skip naming the command that installs one.

    Probed before it is opened, and skipped rather than raised: a checkout with no browser
    is not a checkout with a broken application, and a fixture that errors would fail the
    build for the wrong reason. ``frontend_vm.NODE`` is the same shape for the node suites.

    Scoped to the *module* rather than the session, and that is load-bearing. Playwright's
    sync API keeps the main thread's event loop marked running for as long as its session is
    open - it needs that mark to reach its own dispatcher, and a cleared mark makes the next
    ``page.goto`` fail with "no running event loop". ``asyncio.run`` needs the opposite: the
    mark must be clear or it refuses to start. A single pytest process serves both browser
    modules and the suites that call ``asyncio.run`` (face engine, network hardening, the
    JSON payload walk), so a browser session that lives for the whole run leaves those
    suites failing from a fixture they never used. Ending the session at the module boundary
    is the smallest lifetime that satisfies both: inside a browser module the mark is set and
    Playwright works; between modules ``open_browser`` has closed the loop and released the
    mark, so ``asyncio.run`` works again. Moving this back to ``session`` re-introduces the
    bug, which ``test_browser_event_loop`` now pins.
    """
    ok, why = browser_support.available()
    if not ok:
        pytest.skip(why)
    with browser_support.open_browser() as engine:
        yield engine


@pytest.fixture(scope="session")
def site(app_module, client):
    """The application, served over HTTP, on a port nothing else is using.

    Depends on ``client`` on purpose: the suite's own TestClient is what runs the
    application's lifespan - migrations, the readiness gate, the two watchers - and the
    live server is started with that lifespan switched off so it cannot run it twice. If
    this fixture is ever requested first, the dependency is what keeps that true.
    """
    with browser_support.serve(app_module.app) as running:
        yield running


@pytest.fixture
def link_paths(client):
    """The two token paths a worker is actually sent, minted by the real endpoints.

    The browser needs a URL that exists, and the point of these pages is that the token is
    *in* the path - so this issues one of each through the admin API rather than writing a
    row, so what the browser opens is what the console would have sent.

    Per test, not per session: the suite restores a pristine database before every test and
    a live link is a credential, so a token minted once would be a hash that no longer
    matches a row by the time the browser asked about it.
    """
    quick = client.post(
        "/api/v1/admin/quick_links", headers=harness.bearer(harness.HEAD_ADMIN), json={"worker_id": harness.WORKER}
    )
    assert quick.status_code == 200, quick.text
    invite = client.post(
        "/api/v1/admin/enrollment/invites",
        headers=harness.bearer(harness.HEAD_ADMIN),
        json={"worker_id": harness.WORKER, "kind": "enroll"},
    )
    assert invite.status_code == 200, invite.text
    return {"quick": f"/q/{quick.json()['token']}", "enroll": f"/enroll/{invite.json()['token']}"}
