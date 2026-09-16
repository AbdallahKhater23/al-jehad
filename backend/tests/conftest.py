"""Fixtures for the security-baseline suite.

The heavy lifting (database isolation, the DeepFace stub, the outbound-network
guard) lives in ``harness.py`` and runs at import time. See that module for the
safety rules this suite depends on.
"""

from __future__ import annotations

import pytest

import harness
from harness import FAKE_FACE, PHOTOS_DIR, REFS_DIR, jpeg_bytes as _jpeg_bytes


@pytest.fixture(scope="session", autouse=True)
def _outbound_network_guard():
    """No test may reach the internet (today's alert path has hardcoded creds)."""
    harness.install_outbound_guard()
    return harness.OUTBOUND


@pytest.fixture(scope="session")
def outbound():
    """Recorded outbound HTTP attempts made by the application."""
    return harness.OUTBOUND


@pytest.fixture(scope="session")
def app_module():
    """The application under test, imported once per session."""
    import main

    # Refuse to proceed if the app is somehow pointed at the live database. This runs
    # before any test body, because the failure mode it guards against is silent data
    # loss in the real payroll database.
    harness.assert_database_isolation()

    # Redirect biometric writes/reads into the temp directory: the module reads
    # these globals at call time, so patching them here is enough.
    main.LOCAL_REFS_DIR = str(REFS_DIR)
    main.WORKER_PHOTOS_DIR = str(PHOTOS_DIR)
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
    """The DeepFace stub, so a test can set FACE_MODE / FACE_COUNT."""
    return FAKE_FACE


@pytest.fixture(scope="session")
def jpeg() -> bytes:
    return _jpeg_bytes()


@pytest.fixture(autouse=True)
def deterministic_state(app_module):
    """Pristine database + seed data + cleared rate-limit buckets, before each test."""
    harness.reset_database(app_module)
    yield
