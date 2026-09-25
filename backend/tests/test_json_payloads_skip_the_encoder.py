"""The list endpoints answer with their own ``JSONResponse``; these payloads must earn it.

WHY THIS EXISTS
---------------
FastAPI runs every non-``Response`` return value through ``jsonable_encoder``, which walks
the structure one value at a time - an ``isinstance``/``dataclass`` test per leaf. On these
endpoints that walk changes no byte of the answer, and it is one of the largest costs of
them: measured on this machine at ~6.6 ms for the 129 KB ``/admin/pending_reviews`` body and
~2.5 ms for ``/admin/logs``, against ~0.3-2 ms to serialise the same payload directly.

So the four list endpoints in ``main.py`` hand back ``main._json(...)``, which is a
``JSONResponse`` built from the payload as-is. That is only safe while every value in the
payload is a dict, a list, a string, a number or ``None``: the moment a ``datetime`` or a
``Decimal`` reaches a row, the encoder would have coerced it and ``JSONResponse`` raises
instead. This file is where that must fail - naming the endpoint - rather than becoming a
500 on a page nobody was profiling. ``reports.py``'s ``_encoded`` has the same guardrail in
``test_phase02_offline_enrollment_reports.py``.

HOW IT IS CHECKED
-----------------
The payload is captured as the endpoint hands it to ``_json``, not read back from the
serialised body - a body that has already been through ``json.dumps`` would be JSON-native
by construction and the assertion would prove nothing.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

import developer
import harness
import main
import security
from security import CurrentUser

pytestmark = pytest.mark.regression

#: An administrator, built rather than signed in: these endpoints are called directly, so
#: there is no request to carry a token. The role is the only thing the dependency would
#: have contributed to the payload.
ADMIN_USER = CurrentUser(id=harness.ADMIN, name="Seed Admin", role="admin", token_version=0)

#: The alert queue's reader, for the same reason: it is the root tier's route now, and the
#: dependency is the only thing its role would have contributed here.
ROOT_USER = CurrentUser(
    id=developer.DEVELOPER_ID_DEFAULT, name="Developer", role=security.DEVELOPER_ROLE,
    token_version=0,
)

#: label -> the endpoint call. Called as the ``Depends`` machinery would have called it,
#: with ``current`` supplied.
CALLS: dict[str, object] = {
    "/admin/audit_log": lambda: main.list_audit_log(limit=200, current=ADMIN_USER),
    "/admin/pending_reviews": lambda: main.list_pending_reviews(current=ADMIN_USER),
    "/admin/logs": lambda: main.get_logs(limit=200, current=ADMIN_USER),
    "/developer/notifications": lambda: main.list_notifications(limit=100, current=ROOT_USER),
}


def _capture(monkeypatch) -> dict[str, object]:
    """Run every endpoint, recording the exact payload each handed to ``_json``."""
    captured: dict[str, object] = {}

    def spy(payload):
        # The endpoints are exercised in ``CALLS`` order, so the Nth call is the Nth label.
        captured[list(CALLS)[len(captured)]] = payload
        return JSONResponse(content=payload)

    monkeypatch.setattr(main, "_json", spy)
    for label, call in CALLS.items():
        result = asyncio.run(call())  # type: ignore[operator]
        assert isinstance(result, JSONResponse), (
            f"{label} no longer answers with its own JSONResponse, so FastAPI is walking "
            f"the payload again (it returned {type(result).__name__})."
        )
    assert set(captured) == set(CALLS), f"captured {sorted(captured)}"
    return captured


def test_the_list_payloads_are_json_native(client, monkeypatch):
    """Every value the fast path is handed must already be something JSON can carry."""
    for label, payload in _capture(monkeypatch).items():
        try:
            json.dumps(payload)
        except TypeError as error:
            pytest.fail(f"{label} is not JSON-native, so it may not skip the encoder: {error}")
        assert jsonable_encoder(payload) == payload, (
            f"{label} returns a value the encoder would change, so ``main._json`` may not "
            f"skip it - fix the row builder rather than the endpoint."
        )


def test_the_walk_was_worth_skipping(client, monkeypatch):
    """The endpoints whose payloads are large enough to have paid for the walk.

    A guard against the guardrail quietly losing its subject: if these endpoints stop
    returning rows at all, ``test_the_list_payloads_are_json_native`` still passes while
    testing nothing.
    """
    captured = _capture(monkeypatch)
    pending = captured["/admin/pending_reviews"]
    logs = captured["/admin/logs"]
    assert isinstance(pending, list) and pending, "the review queue is empty; nothing was measured"
    assert isinstance(logs, list) and logs, "the log list is empty; nothing was measured"
    # All of them are lists of dicts with string keys, which is what the fast path assumes.
    for label, payload in captured.items():
        rows = payload if isinstance(payload, list) else payload.get("notifications", [])
        for row in rows:
            assert isinstance(row, dict), f"{label} returned a non-dict row: {type(row).__name__}"
            assert all(isinstance(key, str) for key in row), f"{label} returned a non-string key"
