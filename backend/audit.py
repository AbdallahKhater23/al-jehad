"""The one appender for ``audit_log``.

WHY THIS MODULE EXISTS
----------------------
``audit_log`` is written from nine modules - the admin surfaces in ``main``, the fence
editor, site creation, enrollment, registrations, worker notes, offline sync, the overtime
scanner and the startup gate - and each of them had grown its own copy of the same insert:
the SQL string, the ``json.dumps`` pair, the ip read out of the request, and the never-raise
``except sqlite3.Error``. The copies had drifted in ways nobody chose: some recorded the user
agent, some fell back to a role and some left it NULL, and a reviewer comparing the rows
could not tell policy from accident. The statement lives here once; a caller passes what its
own event means.

WHY A LEAF MODULE (AND NOT ``main._audit``)
-------------------------------------------
Half of these modules are imported *by* ``main`` (``geofence``, ``sites``, ``notes``, ...), so
reaching for a helper that lives in ``main`` would close an import cycle. This module imports
only the standard library, so every one of them - ``main`` included - can depend on it.

WHAT EACH CALLER STILL DECIDES
------------------------------
Four fields are policy rather than plumbing and stay at the call site:

* ``actor_role`` - the role recorded when there is no actor: ``None`` for the surfaces that
  always have a session in hand, ``"public"`` where an anonymous visitor acted, ``"system"``
  for the timer-driven writers.
* ``user_agent`` - whether the request's User-Agent belongs in the row. The browser-facing
  admin surfaces record it; the ingest paths leave the column NULL rather than filling the
  trail with mobile-client strings.
* ``created_at`` - formatted by the caller, from *its* clock. The suite freezes time by
  replacing the ``datetime`` symbol in a module's namespace (see ``clock``'s docstring on why
  the calling code keeps ``datetime.now()``), so a helper that stamped rows from its own
  import would write straight through every frozen-time test.
* the action, the entity and the entity id, which are the event itself.

The columns are named once, in full: ``audit_log`` declares no DEFAULT for any of them, so
omitting a column and passing NULL write the same row - which is what lets one statement
serve callers that used to name three different subsets.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_INSERT = (
    "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, "
    "before_json, after_json, ip, user_agent, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def record(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: Any = None,
    entity: str | None = None,
    entity_id: Any = None,
    before: Any = None,
    after: Any = None,
    request: Any = None,
    actor_role: str | None = None,
    user_agent: bool = False,
    created_at: str,
) -> None:
    """Append one event to ``audit_log``. Never raises.

    ``actor`` is anything with an ``id`` and a ``role`` (a ``security.CurrentUser``); a missing
    actor writes NULL for the id and ``actor_role`` for the role. ``request`` is a FastAPI
    ``Request``: its client address is always recorded when there is one, its User-Agent only
    when ``user_agent`` says so. ``before``/``after`` are dumped with ``default=str``, so a
    row can hold a shape SQLite has no type for.

    A failure to append is swallowed on purpose - the event has already happened, and no audit
    row is worth failing the decision it describes. ``audit_log`` is append-only *in the
    database* (triggers reject UPDATE and DELETE), so a correction is a new row rather than a
    rewrite.
    """
    try:
        conn.execute(
            _INSERT,
            (
                actor.id if actor else None,
                actor.role if actor else actor_role,
                action,
                entity,
                str(entity_id) if entity_id is not None else None,
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                request.client.host if request is not None and request.client else None,
                request.headers.get("user-agent") if user_agent and request is not None else None,
                created_at,
            ),
        )
    except sqlite3.Error:
        pass
