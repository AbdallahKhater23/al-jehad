"""Walk-up registration: one issued link, one photograph, one administrator's decision.

WHY THIS IS NOT A PER-PERSON INVITE
-----------------------------------
``enrollment`` issues a link *per person*, for an account that already exists: an administrator
creates the account, and the link registers its owner's face. That is the right shape for one
named hire and the wrong one for a walk-up, where nobody has applied yet and so there is no
account to hold a face. Here there is one link for the whole company, issued from the console and
sent to whoever should apply, and the account it creates waits for an administrator: it can be
signed into immediately, and it cannot record a single punch until somebody approves it.

WHY THE LINK IS MINTED RATHER THAN PERMANENT
--------------------------------------------
This module used to serve a permanent address at ``/register`` that anybody could open - a public
form with a face on it, reachable by whoever found the URL and never revoked. What replaced it is
a link the console *issues*: ``/register/<token>``, where the token is signed by the deployment's
``SECRET_KEY`` over one integer, so there is nothing secret at rest, the console can show the
address at any time, and replacing the link invalidates every copy of it by arithmetic
(``link_token``, ``rotate_link``, and migration 31's note for the argument). The form is still
reachable without a session - the person filling it in has no account, which is the point - but it
is no longer reachable without having been given the form's address by somebody who works here.

WHAT THE THREE PARTS ARE FOR
----------------------------
* **The intake switch** is the console's, with the deployment's flag as its default.
  ``settings.registration_enabled`` (``REGISTRATION_ENABLED``) is the *deployment's*: whether
  this installation runs walk-up applications out of the box. It ships off - on the same
  reasoning as the calibration switch, a public endpoint that collects a face is not something a
  deployment should *discover* it is running - and an untouched installation follows it exactly.
  The ``registration_settings`` row is what an administrator moves, from the console, with no
  restart: is the permanent link accepting applications today. Moving it is the same action
  whether the flag shipped on or off, because the applicant's sentence ("ask your site
  administrator to open it") has to be something a site administrator can act on.
* **The quarantine** is ``users.status``. A submission writes a real account - the id, the name,
  the contact, the password and the face - as ``pending_approval``, and answers the applicant with
  the id: they sign in with it at once and cannot clock in or out (``main.verify_worker`` refuses
  the status before it looks at the fence or the camera). Approval flips that one value to
  ``active``; a refusal deletes the row and wipes the face, which frees the number.
* **The decision** is a human's. A photograph of a face is judged by an administrator looking at
  it, not by a model - the reviewer's eyes are the decision, and the model work below is what a
  reviewer is *given*, never a verdict of its own.

THE ACCOUNT EXISTS BEFORE THE DECISION, AND THAT IS THE POINT
-------------------------------------------------------------
This replaced a holding table (``registration_requests``, migration 24) in which an approval was
the only thing that created an account. The reason it changed is the one thing the old shape
could not do: tell the applicant what their id was. There the number was minted by the decision,
so the person waiting was anonymous to a system that would not let them in - they could not check
whether they had been approved, could not see a notice waiting for them, and if the administrator
who approved them went home the number existed only on that console's receipt. An account that
exists and is quarantined answers all three, and the quarantine is the same gate as before: no
punch without a decision.

WHAT THE OLD SHAPE COULD NOT ENFORCE, AND THIS ONE CAN
------------------------------------------------------
"Cannot clock in yet" is now a fact the *server* holds about the account, checked on every punch
(``status = 'pending_approval'``) rather than an absence of rows that a second code path had to
remember to preserve. That is why the refusal lives in the punch endpoint beside the geofence and
the face match instead of in whichever routes happened to need an account: an approval that is
reversed, a session that outlives a refusal, a token minted before the decision - all of them
meet the same line.

WHY THE PUBLIC ROUTE NOW RUNS A MODEL
-------------------------------------
It did not before, and the reason it does is the shape above: the face has to be *on the account*
before the account is worth anything, and the account now exists before anybody has decided
anything - so the embedding happens exactly once, at submission, where the photograph is. The
alternative - embed at approval - means holding the applicant's face on disk in a private
directory until an administrator gets round to it, which is the copy this application spends
every other paragraph refusing to keep.

The deployment cost is bounded rather than waved away: the work runs in the shared face-engine
pool (``face_engine.ENGINE``), the same bounded queue a punch waits in, so a burst of walk-ups
cannot run more inferences at once than a burst of punches - it makes both slower, which is the
trade, and the intake switch plus the rate limit are what stop a stranger spending that capacity
at will. Liveness stays advisory for the reason it always was: a file on disk cannot be proven
live, so on this route the liveness verdict is a note for the reviewer and never a refusal of its
own.

WHY THE LOWEST FREE NUMBER, AND NOT THE NEXT ONE
------------------------------------------------
The id is allocated at submission, inside the same write transaction that inserts the account, by
``_next_workforce_id``, and it is the lowest number in the workforce band that nobody holds. That
is a *change*: the previous allocator counted upward, and its docstring argued that a number given
back by a deleted account should not be handed out again because the queue would have spent it
twice. That argument does not survive the account existing - there is no queue, and the number is
not a promise being made to somebody waiting, it is the identity of an account about to be created
or not created at all.

What makes reuse safe here is the *refusal* rather than the allocator: a rejected applicant's row
is deleted and their reference wiped from the biometric store by ``biometrics.remove_files`` before
the number is free, so the next holder of that id cannot be scored against the face of the person
before them. A number a worker *might* meet again is one a retired (deactivated) account still
holds, and that one is not free - the band is scanned for ids in use, not for ids ever used.

Both edges still matter and both are enforced here: the band starts at 1, and it ends below the
administrative tiers (``ADMIN_TIER_ID_FLOOR``), so a public form can never mint a number that looks
like an administrator's - and a band with no room answers 409 by name rather than wrapping around
onto an account that is still in use.

ONE PHOTOGRAPH IS ONE FACE, AND IT LIVES IN THE BIOMETRIC STORE
---------------------------------------------------------------
The upload is staged on disk under the shared policy, read once for the embedding, and removed by
the request that wrote it as soon as the reference has a home. What survives is the reference
under the account's immutable biometric id, where ``retention`` already knows how to find and wipe
it - nothing keeps a second copy in an intake directory for a reviewer to browse, because that
copy is exactly the face that would outlive the account it belongs to. A refusal wipes the
reference rather than filing it: an intake funnel that kept the faces it turned down would
contradict the retention story this system tells about every other face it stores.

WHAT HAPPENED TO ``registration_requests``
------------------------------------------
Migration 24 created it and nothing here reads or writes it any more. The rows stay: they are the
record of who applied and what an administrator decided, which is a question asked *after* the
account exists or does not, and a migration that dropped them would destroy the only copy. See
``migrations.migration_30_self_service_registration``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

import biometrics
import enrollment
import face_engine
import notifications
import overtime
import retention
import textguard
import uploads
from config import settings
from database import db, immediate
from rate_limit import limiter
from security import (
    CurrentUser,
    admin_only,
    hash_password,
    refuse_developer_role,
    validate_password_strength,
)

log = logging.getLogger("attendance.registrations")

#: Where a submission's photo waits for a reviewer. Read at import and overridable by the test
#: suite through ``harness.FILE_TREES``, like every other file tree the application writes.
PHOTOS_DIR = str(settings.registration_photos_dir)

#: The form itself, one segment under its token: ``/api/v1/register/<token>``, beside the page
#: at ``/register/<token>`` that carries it. A request without the token is refused (see
#: ``require_link``); nothing here answers on the bare path any more.
public_router = APIRouter(prefix="/register", tags=["registration"])
#: The review surface. The same audience as every other administrative read of a person.
admin_router = APIRouter(prefix="/admin/registrations", tags=["registration"])

#: The status a self-registered account carries until an administrator decides. A third value
#: for ``users.status`` rather than a flag of its own, so the two states it already had -
#: ``active`` and ``inactive`` - go on meaning exactly what every reader of them thinks they
#: mean, and so the quarantine is a fact about the *account*, checked on the punch itself.
STATUS_PENDING_APPROVAL = "pending_approval"

#: What ``GET /admin/registrations`` will filter on. ``all`` is handled beside these rather than
#: in the tuple: it is not a status, it is the absence of the filter.
QUEUE_STATUSES = (STATUS_PENDING_APPROVAL, "active", "inactive")

#: Prefix of the dedupe key the administrators' "somebody is waiting" notice is filed under. One
#: notice per waiting account, so a submission a phone retried does not bury the queue in copies -
#: and so ``reject_registration`` can take the notice back with the account it was about.
PENDING_NOTICE_PREFIX = "registration_pending:"

#: What a walk-up may ask to be, spelled out here rather than borrowed from the invite flow.
#: These three roles are this form's own contract with a stranger: nobody self-registers as an
#: administrator, because an account that can read the audit trail is not something a forwarded
#: link may mint - and an invite that is allowed to enroll a different set of roles must not be
#: able to change what a public form offers by being edited.
WORKFORCE_ROLES: frozenset[str] = frozenset({"worker", "moallem", "off_office"})

#: The same three in the order the public form offers them: the plainest role first. A set has no
#: order of its own, and a list sorted for determinism would offer "moallem, off_office, worker".
WORKFORCE_ROLES_IN_ORDER: tuple[str, ...] = ("worker", "moallem", "off_office")

#: The first id of the administrative tiers (``admin`` 1000-4999, ``head_admin`` 5000+, and the
#: root band far above both). An approval may never mint a number at or above this floor: an
#: administrator account is created by an administrator in the console, and a queue anybody can
#: submit to must not be able to produce one.
ADMIN_TIER_ID_FLOOR = 1000

#: The last id a walk-up approval may hand out - the workforce band's ceiling.
WORKFORCE_ID_CEILING = ADMIN_TIER_ID_FLOOR - 1

#: What counts as consent on the public form. Deliberately explicit: the field is a checkbox,
#: but the value arrives as a string and ``"false"`` is a string that means no.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The version of the consent wording each submission records. Bump it when the sentence the
#: applicant agreed to changes - the stored value is what makes an old consent readable as a
#: consent to *that* wording rather than to today's.
CONSENT_VERSION = "walk-up-registration-v1"

_TS = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# paths resolved at call time
# ---------------------------------------------------------------------------
def photos_dir() -> str:
    """The directory a submission's photo waits in, created on first use.

    Read at call time, like ``quick_links.photos_dir`` and ``punch_frames.frames_dir``, so the
    test suite's one redirect carries this tree along with the database.
    """
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    return PHOTOS_DIR


# ---------------------------------------------------------------------------
# the account id an approval mints
# ---------------------------------------------------------------------------
def _next_workforce_id(conn: sqlite3.Connection) -> str:
    """The lowest free account id below the administrative tiers. Refuses with ``409`` when the
    band is full.

    WHY THE CEILING IS THE POINT
    ----------------------------
    A submission is the one thing on this surface that creates an account, and it is driven by a
    form a stranger can reach. So it may only ever mint a number inside the band that belongs to
    business accounts: the administrative tiers begin at ``ADMIN_TIER_ID_FLOOR`` and this refuses
    to reach them, rather than trusting that the ids it happens to hand out stay low.

    A full band is reported instead of wrapped around: an id reused above an account that is
    still in use would be a duplicate key, and answering 409 names the band and leaves the choice
    - retire an account, or create this one in the console.

    WHY THE LOWEST FREE NUMBER
    --------------------------
    Counting upward from the highest id in use was the old rule and it is deliberately gone: it
    left every number given back by a refused application permanently unusable, which in a
    deployment that turns applicants down is a band that fills with nothing. What is reused here
    was freed *on purpose* - ``reject_registration`` deletes the row and wipes the reference
    under it - so a new face can never inherit an old one's template. A *deactivated* account
    still holds its number, which is the difference between freed and retired.

    WHY THIS IS ONLY SAFE INSIDE THE CALLER'S WRITE TRANSACTION
    ----------------------------------------------------------
    This is a **read**. Two submissions that both read "1 to 7 are taken" both insert 8, and one
    of them dies on the primary key. What makes the read-then-insert safe is the caller's
    ``BEGIN IMMEDIATE`` (``database.immediate``): SQLite takes the single write lock *before* the
    read, so the second caller cannot read until the first has committed and therefore sees the
    row the first one wrote. The primary key stays the last line of defence.
    """
    # ``CAST`` is 64-bit in SQLite and a non-numeric id casts to 0, which the ``BETWEEN`` below
    # excludes - an account id that is not a number occupies no number in this band.
    used = {
        int(row[0])
        for row in conn.execute(
            "SELECT CAST(id AS INTEGER) FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND ?",
            (WORKFORCE_ID_CEILING,),
        )
        if row[0] is not None
    }
    candidate = 1
    while candidate in used:
        candidate += 1
    if candidate > WORKFORCE_ID_CEILING:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "id_space_exhausted",
                "message": (
                    f"There is no free account id below {ADMIN_TIER_ID_FLOOR} any more: the "
                    f"workforce band ends at {WORKFORCE_ID_CEILING}. Retire an account that is no "
                    "longer needed, or create this account in the console."
                ),
            },
        )
    return str(candidate)


# ---------------------------------------------------------------------------
# the intake switch: an answer to "is the permanent link accepting today"
# ---------------------------------------------------------------------------
#: The link is accepting submissions. The only reason code that means yes.
INTAKE_OPEN = "open"
#: Somebody closed the link from the console, and the console is what opens it again.
INTAKE_CLOSED_BY_CONSOLE = "closed_by_console"
#: Nobody has moved the switch and the deployment's own default is *off*, so the link is shut
#: because it starts shut. The console opens it exactly as it opens the one above - the code
#: exists so a sentence can say "nobody has opened this yet" rather than "somebody closed it",
#: which are the same state and not the same instruction to the person reading.
INTAKE_CLOSED_BY_DEFAULT = "closed_by_default"


def intake_row() -> Any:
    """The console's switch row, or ``None`` when nobody has decided - or the table is newer.

    Never raises, and a database older than migration 29 has no such table: a deployment whose
    migrations have not run yet is not a reason for the permanent public link to answer 500.
    Both read as "no decision", which is the state every deployment that has never opened the
    console is in anyway.
    """
    try:
        with db() as conn:
            return conn.execute("SELECT * FROM registration_settings WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None


def intake_state() -> dict[str, Any]:
    """Whether the link accepts a submission right now, and what is holding it shut if not.

    THE CONSOLE OWNS THIS, AND THE DEPLOYMENT'S OWN FLAG IS ITS DEFAULT
    ------------------------------------------------------------------
    ``settings.registration_enabled`` (``REGISTRATION_ENABLED``) answers what this installation
    does *out of the box*: a deployment nobody has touched follows it exactly, so an operator
    who deliberately runs no walk-up registration never finds one running because a schema
    change shipped it on. It is a starting position rather than a ceiling. The
    ``registration_settings`` row is the switch an administrator or a head administrator moves
    from the console::

        accepting = the console's position, which *is* the deployment's flag until somebody
                    moves it, and is whatever they left it at afterwards

    It used to be a ceiling - ``REGISTRATION_ENABLED and not closed`` - and the difference is
    who can act on it: with the flag off, the public form said "ask your site administrator to
    open it" and the site administrator had no control anywhere in the console that could. So
    a link that ships closed is now a link an administrator can open, which is the decision
    that made this a setting rather than a deploy.

    WHY THE FLAG IS STILL REPORTED
    ------------------------------
    "This deployment does not run registration links" and "nobody has opened this one yet" are
    the same state from the applicant's side and different facts for whoever is standing at the
    console, and the second one is an instruction. ``reason`` carries that as a code (``open`` /
    ``closed_by_console`` / ``closed_by_default``) rather than as a sentence, because the
    sentence is the reader's language and belongs in the console's own translation tables - see
    ``registrationsIntakeWhy*`` in ``frontend/i18n.js``.
    """
    row = intake_row()
    stored: int | None = None
    if row is not None:
        try:
            stored = None if row["intake_open"] is None else int(row["intake_open"])
        except (IndexError, KeyError, TypeError, ValueError):
            # A column this build cannot make sense of means "no decision", not "closed": the
            # failure direction that keeps a walk-up link that used to work working.
            stored = None
    deployment = bool(settings.registration_enabled)
    if stored is None:
        accepting = deployment
        reason = INTAKE_OPEN if accepting else INTAKE_CLOSED_BY_DEFAULT
    else:
        accepting = stored != 0
        reason = INTAKE_OPEN if accepting else INTAKE_CLOSED_BY_CONSOLE
    return {
        "accepting": accepting,
        "reason": reason,
        # The deployment's own default - what an untouched switch follows, and what the console
        # says when it explains why a link it has never been asked about is shut.
        "deployment_enabled": deployment,
        # Whether anybody has moved it. ``NULL`` is the whole of every database before migration
        # 29, and is not the same as a decision: the console draws the same switch either way.
        "decided": stored is not None,
        "updated_at": row["updated_at"] if row is not None else None,
        "updated_by": row["updated_by"] if row is not None else None,
    }


def intake_accepting() -> bool:
    """The one call the gates make: may a submission be taken, may the form offer itself."""
    return bool(intake_state()["accepting"])


#: What the link's signature covers. Versioned like every other canonical string in this
#: application, so that changing what a signature means is a new prefix rather than a silent
#: reinterpretation of the old one.
_LINK_MESSAGE_PREFIX = "registration-link|v1"

#: How many times a submission re-reads the band after the insert it built the id from lost a
#: race. One retry is the common case (the other writer had already committed by the time the
#: statement ran); the bound exists so that a band genuinely being hammered answers 409 rather
#: than spinning.
ALLOCATION_ATTEMPTS = 5


def _link_signature(generation: int) -> str:
    """The signature half of a registration token: nothing secret is stored, see migration 31."""
    message = f"{_LINK_MESSAGE_PREFIX}|{int(generation)}".encode("utf-8")
    return hmac.new(str(settings.secret_key).encode("utf-8"), message, hashlib.sha256).hexdigest()


def link_token(generation: int | None = None) -> str:
    """The token half of the link: the generation it was minted for, and its signature.

    A function of the deployment's ``SECRET_KEY`` and one integer, which is what lets the console
    show the link at any time while the database holds no token at all (migration 31's note has
    the argument for that). It is not a secret that admits anybody: what it opens is a form, and
    what the form produces is an account that cannot clock in until an administrator approves it.
    """
    value = int(generation) if generation is not None else link_generation()
    return f"{value}.{_link_signature(value)}"


def _link_generation_from_token(token: str) -> int | None:
    """The generation a token claims, or ``None`` when the signature does not hold.

    Read defensively and never raising: this is handed a string out of a URL, so it has to answer
    "not a link" for anything at all - a bare word, a number with no signature, a signature of
    the right shape over the wrong generation. ``compare_digest`` rather than ``==`` so the
    comparison does not leak how much of a guessed signature was right.
    """
    raw = str(token or "").strip()
    head, separator, signature = raw.partition(".")
    if separator != "." or not head.isdigit():
        return None
    try:
        generation = int(head)
    except ValueError:  # pragma: no cover - ``isdigit`` already decided this
        return None
    if not hmac.compare_digest(_link_signature(generation), signature.strip().lower()):
        return None
    return generation


def link_generation() -> int:
    """The generation the live link was minted for. ``0`` when nobody has rotated it yet.

    Never raises, for the same reason ``intake_row`` never does: a database older than migration
    31 has no such column, and a link that predates the console having a link is generation 0 -
    which is exactly what the migration's ``DEFAULT 0`` writes for every existing row.
    """
    row = intake_row()
    if row is None:
        return 0
    try:
        return max(0, int(row["link_generation"] or 0))
    except (IndexError, KeyError, TypeError, ValueError):
        return 0


def link_row() -> Any:
    """The settings row, for the rotated-at/by pair the console shows beside the link."""
    return intake_row()


def link_url(request: Request, base_url: str | None = None) -> str:
    """The address to send somebody: the console's base URL, the path, and the token."""
    return f"{enrollment._public_base_url(request, base_url)}register/{link_token()}"


def rotate_link(request: Request, current: CurrentUser) -> str:
    """Replaces the link: every URL minted before this call stops verifying.

    One write, and the revocation is the whole point of it - the signature covers the generation,
    so a token that carried the old one is refused by arithmetic rather than by a row somebody has
    to remember to revoke (migration 31's note). The row is created if this is the first rotation
    on a deployment that has never written one, so a console that was never opened still gets a
    link it can hand out rather than a 500 about a missing settings row.
    """
    stamp = _now()
    with immediate() as conn:
        conn.execute(
            "INSERT INTO registration_settings (id, intake_open, updated_at, updated_by) "
            "VALUES (1, NULL, ?, NULL) ON CONFLICT(id) DO NOTHING",
            (stamp,),
        )
        before = int(
            conn.execute(
                "SELECT COALESCE(link_generation, 0) FROM registration_settings WHERE id = 1"
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE registration_settings SET link_generation = ?, link_rotated_at = ?, "
            "link_rotated_by = ? WHERE id = 1",
            (before + 1, stamp, current.id),
        )
        _audit(
            conn,
            action="registration_link_rotate",
            actor=current,
            entity="registration_settings",
            entity_id="1",
            before={"link_generation": before},
            after={"link_generation": before + 1, "rotated_at": stamp},
            request=request,
        )
    return link_token(before + 1)


def require_link(token: str) -> None:
    """Refuses a request whose URL does not carry the live link, with the server's own reason.

    A 404 rather than a 403, and deliberately the same answer for every way a URL can be wrong -
    an unknown address, an old link somebody replaced, a guessing script, a link from a deployment
    with a different ``SECRET_KEY``. What an applicant can act on is "this link is not valid any
    more, ask for a new one"; which of those it was is an operator's question, and the console
    that can mint a link is where they ask it.
    """
    if _link_generation_from_token(token) != link_generation():
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "registration_link_invalid",
                "message": (
                    "This registration link is not valid any more. Ask your administrator for a "
                    "new one."
                ),
            },
        )


# ---------------------------------------------------------------------------
# audit + notification helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime(_TS)


def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None,
    entity: str,
    entity_id: str,
    before: Any = None,
    after: Any = None,
    request: Request | None = None,
) -> None:
    """Append an administrative event. Never raises - see ``enrollment._audit``.

    ``before`` as well as ``after`` since the intake switch needs it: a decision that sets a
    value is only readable later against the value it replaced (see ``set_intake_switch``), while
    every other caller here is recording something that did not exist before it happened.
    """
    try:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, before_json, after_json, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor.id if actor else None,
                actor.role if actor else "public",
                action,
                entity,
                str(entity_id),
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                request.client.host if request is not None and request.client else None,
                _now(),
            ),
        )
    except sqlite3.Error:
        pass


#: Prefix of the dedupe key an approval's worker notice is filed under. One account is decided
#: once, so this is belt and braces rather than a rule - the same shape every other notice uses.
APPROVED_NOTICE_PREFIX = "registration_approved:"


def _approved_notice(
    row: sqlite3.Row, *, worker_id: str, role: str
) -> dict[str, Any]:
    """What a worker is told when the quarantine is lifted.

    WHY THIS EXISTS AT ALL
    ----------------------
    The applicant already has their number - the form told them, and they signed in to read this
    - so this is not where the id is handed over. What it carries is the other half, which nobody
    can look up: that the decision went their way and the quarantine is over. A worker who signs
    in and finds the clock still refusing them has no way to tell "not yet" from "never", and
    this row is the answer that arrives without them having to ask an administrator.

    WHAT A SIGN-IN STILL NEEDS
    --------------------------
    ``main.login`` matches ``WHERE id = ? AND (email = ? OR phone = ?)`` and then verifies the
    password, so signing in needs this id, the email or phone **as stored on the account**, and
    the password. All three came from the form and none of them changed at approval, so this
    repeats them rather than inventing a credential - the applicant is the one person who cannot
    re-read what they typed.

    The contact is the awkward third, and worth being explicit about: both columns are optional
    on the public form, and the sign-in route matches whatever is stored - so a worker who gave
    neither signs in with that box left empty (the same reason the form's own email-or-phone
    input is deliberately not ``required``).

    ``payload`` carries the same facts as data for a reader that would rather render them than
    read this sentence. Never the password: what the request row holds is a bcrypt hash, and the
    only copy of the credential is in the applicant's head.
    """
    email = str(row["email"] or "").strip()
    phone = str(row["phone"] or "").strip()
    opening = (
        f"Your account was approved: you are registered here as {role}, and you can clock in "
        "from now on."
    )
    if not email and not phone:
        body = (
            f"{opening} To sign in, use id {worker_id} and the password you chose when you "
            "applied, and leave the email-or-phone box empty - you did not give us one."
        )
    else:
        given = []
        if phone:
            given.append(f"the phone number you gave us ({phone})")
        if email:
            given.append(f"the email you gave us ({email})")
        body = (
            f"{opening} To sign in, use id {worker_id}, "
            f"{' or '.join(given)}, and the password you chose when you applied."
        )
    return {
        "title": f"Your account is approved - worker id {worker_id}",
        "body": body,
        "payload": {
            "worker_id": worker_id,
            "role": role,
            "email": email,
            "phone": phone,
        },
    }


# ---------------------------------------------------------------------------
# reading a request for a reviewer
# ---------------------------------------------------------------------------
#: The columns of a ``users`` row this module reads. One list, used by both loaders, so a
#: column cannot be selected by one and forgotten by the other.
ACCOUNT_COLUMNS = (
    "id, name, email, phone, role, status, enrolled_at, biometric_id, registration_note"
)


def _as_pending_user(row: sqlite3.Row) -> dict[str, Any]:
    """One account waiting for approval, as the review surface reads it.

    The credential is not here at all - the column is not selected rather than selected and
    dropped, so there is no path on which a hash reaches a response body. The face is not here
    either: it is served by its own route, so a list of forty applications does not carry forty
    faces, and so a reader without the row's id cannot fetch the bytes. What the photo route needs
    instead is *whether there is one*, which is answered from the biometric store rather than
    from a column, because that is where a face lives now.

    The keys are the ones the console's card already reads - ``full_name``, ``requested_role``,
    ``work_details``, ``created_at`` - kept rather than renamed to the account's own column names,
    because the review screen was built against them and a rename here is a silently blank field
    there.
    """
    user_id = str(row["id"])
    photo = biometrics.resolve_photo(user_id, row["biometric_id"])
    try:
        size = os.path.getsize(photo) if photo else 0
    except OSError:
        size = 0
    return {
        "id": user_id,
        "status": row["status"],
        "full_name": row["name"],
        "phone": row["phone"],
        "email": row["email"],
        "requested_role": row["role"],
        "work_details": row["registration_note"] or "",
        "created_at": row["enrolled_at"],
        "has_photo": bool(photo),
        "photo_bytes": int(size),
    }


def _load_account(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    """One account by id, or ``404``. The subject of every route on this surface."""
    row = conn.execute(
        f"SELECT {ACCOUNT_COLUMNS} FROM users WHERE id = ?", (str(user_id),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No account has that id.")
    return row


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------
def sweep_orphan_photos() -> int:
    """Remove abandoned intake photographs, and say how many.

    The directory is a **staging area now**, not a filing cabinet. A submission stages its upload
    there, reads it once for the embedding, and removes it in the same request once the reference
    has a home in the biometric store - so a file left behind means one of exactly two things: a
    request that died between the write and the reference, or a reference that could not be
    written and whose photograph was deliberately kept so the account can be enrolled from the
    console (see ``submit_registration``).

    Both are residue, and the second has a deadline rather than an exemption: the file is kept
    only for ``registration_photo_stale_hours``, after which this removes it. The account is
    unaffected - it is real, its id works, and it can be enrolled again from a photograph taken
    in the console - whereas a face on disk that nothing is going to look at is the thing this
    module is arranged not to keep. One pass at startup is enough.
    """
    directory = PHOTOS_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    cutoff = datetime.now().timestamp() - float(settings.registration_photo_stale_hours) * 3600.0
    removed = 0
    for name in names:
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) > cutoff:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("removed %s abandoned intake photograph(s)", removed)
    return removed


def _destroy_photo(path: str) -> bool:
    """Wipe one submitted photo. ``True`` when it is gone (or never existed).

    ``retention.wipe_file`` rather than ``os.remove``: a face that has been *decided on* is
    residue, not a temporary file, and it is overwritten before it is unlinked for the same
    reason every biometric file in this application is. A wipe that fails is reported rather
    than swallowed, and the file it could not remove is left to the startup sweep - which is the
    only retry there is, now that the photograph is not pointed at by a row.
    """
    name = os.path.basename(str(path or ""))
    if not name:
        return True
    candidate = os.path.join(PHOTOS_DIR, name)
    if not os.path.exists(candidate):
        return True
    try:
        retention.wipe_file(candidate, directory=PHOTOS_DIR)
        return True
    except (OSError, ValueError) as exc:
        log.warning("could not wipe the registration photo %s (%s)", name, exc)
        return False


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class Decision(BaseModel):
    """What an administrator may add to a decision: a sentence, and nothing else.

    The id, the role and the name come from the applicant's own submission; the one thing a
    reviewer contributes is the *reason*, which is shown to the applicant on a rejection and kept
    on the row for an auditor. ``prose`` rather than ``identifier``: this is writing.
    """

    note: str | None = None

    @field_validator("note")
    @classmethod
    def _plain_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return textguard.prose(
            value, field="Note", max_length=textguard.MAX_NOTE, allow_empty=True
        )


class IntakeSwitch(BaseModel):
    """The console's answer to "is the permanent link accepting today".

    A boolean and nothing else - the *effective* position, not a pair of switches. It is the
    position and not a delta, so two administrators pressing opposite buttons a second apart
    produce one state and not a conflict, and the answer to either of them is the same read.
    """

    open: bool


# ---------------------------------------------------------------------------
# the public link
# ---------------------------------------------------------------------------
@public_router.get("")
@public_router.post("")
async def registration_link_required():
    """The bare path, with no token: the link's own refusal, said out loud.

    A route rather than an accident. Without it the framework answers ``404 {"detail": "Not
    Found"}``, which is true and useless - the person holding the phone cannot tell a broken
    link from a typo, and the console cannot tell a link somebody replaced from one that was
    never cut properly. Answering the same ``registration_link_invalid`` the token check raises
    costs one handler and makes the bare path a *sentence*: this is not a link, ask for one.

    Public, and in the same sense as the two routes below: no session, no data, no policy - just
    the refusal, which is the only thing true of an address with no link behind it.
    """
    require_link("")


@public_router.get("/{token}")
@limiter.limit(settings.registration_rate_limit)
async def registration_intake(request: Request, token: str):
    """What the form has to satisfy, and whether intake is open at all.

    Public because the person filling it in has no account, which is the entire point of the
    form - but not *addressable* by the public: the token in the path is the console's own link,
    and a request without it is refused before a single policy field is answered. The link is
    what makes this a private door rather than a permanent address on the open internet, and the
    console mints, shows and replaces it (``GET``/``POST /admin/registrations/link``).

    Public still, in the sense that matters: no session, no account, and no header. What it
    exposes is the *policy* - the upload ceiling, the accepted formats, the roles somebody may
    ask for, the shortest password - and no data of any kind: not a count of the queue, not a
    name, not an id.

    ``enabled: false`` still answers 200 rather than 404. A link that has been switched off has
    to be able to *say* it is switched off, or an applicant sees a broken page and tries again
    tomorrow.

    ``enabled`` is the *effective* state rather than the deployment flag alone: the console's
    switch is the one that decides (see ``intake_state``), so reading the env flag here would
    offer the form to somebody the very next line would refuse.
    """
    require_link(token)
    state = intake_state()
    # ``enabled`` alone, and no reason code: an applicant gets one sentence either way, and
    # *which* switch closed the link is an operator's question - it is answered with the rest of
    # the state on ``GET /admin/registrations/intake``, where the reader who can move a switch is
    # looking. This response stays the shape the page has always read.
    return {
        "enabled": state["accepting"],
        "roles": list(WORKFORCE_ROLES_IN_ORDER),
        "photo_policy": uploads.policy(),
        "min_password_length": settings.min_password_length,
        "consent_version": CONSENT_VERSION,
        "message": (
            "Submit your details and a photo. An administrator reviews every request before an "
            "account is created."
            if state["accepting"]
            else "Registration is closed at the moment. Ask your site administrator to open it."
        ),
    }


@public_router.post("/{token}")
@limiter.limit(settings.registration_rate_limit)
async def submit_registration(
    request: Request,
    token: str,
    full_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="worker"),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    work_details: str = Form(default=""),
    consent: str = Form(default=""),
    photo: UploadFile = File(...),
):
    """Accept one registration from the console's link, and create the account it is for.

    The order is the argument. Everything that can be refused is refused before a file is
    written: the link, the switch, the text, the role, the consent and the password. Then the
    photo is streamed to disk through the shared policy - one chunk of memory whatever its size -
    decoded once for its embedding, and the account is inserted. Any failure after the file
    exists takes the file with it, so a refused submission cannot leave a face in a directory
    nobody is going to look at.

    WHY THE LINK IS CHECKED BEFORE THE SWITCH
    -----------------------------------------
    They answer two different questions and the link's is the narrower one. A closed intake is
    something an applicant may be told - "ask your site administrator to open it" is a sentence
    somebody can act on, and it is what the form itself will have shown them. A URL that is not
    this deployment's live link is not a person who found the form closed; it is an address that
    should never have reached them, and it is answered as one thing whatever is wrong with it.

    WHAT THE APPLICANT GETS BACK
    ----------------------------
    Their own id, and the sentence that says what is left: sign in now, clock in later. That is
    the whole difference between this and the holding table it replaced (see this module's
    docstring) - the number is the account's from the moment it exists, so the person holding
    the phone can check on themselves instead of waiting to be told.

    WHY THE QUARANTINE IS A STATUS AND NOT AN ABSENT ROW
    ----------------------------------------------------
    ``pending_approval`` is written here and read on every punch. That is what makes the promise
    above safe: "you can sign in but not clock in" is enforced by the punch endpoint refusing
    the status (``main.verify_worker``), not by the account happening not to have a shift yet.

    The cap is enforced **inside the insert's transaction**, together with the insert: a count
    read before the write is advice, and N concurrent submissions would each read ``cap - 1``.
    The switch is read the same way: it is the *effective* intake state (``intake_state``), not
    the deployment flag alone, so closing the link from the console refuses exactly the
    submissions the form has stopped offering.

    ONE LINK, TEN PHONES, AT THE SAME MOMENT
    ----------------------------------------
    The link is shared, which means the band scan and the ``INSERT`` that consumes it are racing
    by design: ten applicants opening one WhatsApp message do not take turns. They are serialized
    by ``immediate()``, so the tenth reads the band after the ninth has committed and the common
    case is ten distinct numbers with no retry - but a writer outside this lock, or a lock taken
    between the scan and the insert, leaves the primary key to catch it. That is a *lost race*
    rather than a bad request, and it is retried here rather than handed to the applicant as
    ``409 registration_conflict`` with "send the form again": they sent the form once, the
    failure was the server's, and the photograph and the embedding are already in hand.
    """
    require_link(token)
    if not intake_accepting():
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "registration_closed",
                "message": "Registration is closed at the moment. Ask your site administrator.",
            },
        )

    try:
        full_name = textguard.identifier(
            full_name, field="Full name", max_length=textguard.MAX_NAME
        )
        phone = textguard.contact(phone, field="Phone")
        email = textguard.contact(email, field="Email")
        work_details = textguard.prose(
            work_details, field="Work details", max_length=textguard.MAX_NOTE, allow_empty=True
        )
    except ValueError as exc:
        raise textguard.http_error(exc) from None

    role = str(role or "").strip().lower()
    if role not in WORKFORCE_ROLES:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "role_not_available",
                "message": (
                    "This link can register a worker, a lead worker (moallem) or an off-office "
                    "worker only. An administrator account is created by an administrator."
                ),
            },
        )
    # Belt and braces: the role is already restricted to the business roles above, and the
    # root tier must be refused *by name* on every path that could mint one rather than by an
    # accident of a list.
    refuse_developer_role(role)

    if str(consent or "").strip().lower() not in _TRUTHY:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "consent_required",
                "message": (
                    "Please confirm that you agree to your photograph being stored for "
                    "attendance verification."
                ),
            },
        )

    # The same policy an administrator's password obeys. This is the one password nobody can
    # reset for the applicant, so a weaker rule here would be a hole with a name on it.
    validate_password_strength(password)

    stored = await uploads.store_photo(photo, photos_dir(), field="photo", prefix="req-")
    assigned = ""
    template_written = True
    template_error: str | None = None
    try:
        # The model work, before the account and outside every lock. It reads the staged file
        # rather than the request body: the upload policy has already streamed the bytes to disk
        # a chunk at a time, so what is held here is one decoded frame - and any refusal from the
        # face engine leaves the staged file to the ``except`` below.
        try:
            image = uploads.face_frame(stored.path, field="photo")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "registration_photo_unreadable",
                    "message": "That photograph could not be read. Please take another one.",
                },
            ) from None
        try:
            decision, embedding = await face_engine.ENGINE.run_async(
                enrollment.embed_reference, image, stage="registration_intake"
            )
        except (face_engine.FaceEngineBusy, face_engine.FaceEngineUnavailable) as exc:
            # The pool is the gate's own. Answering "busy" rather than queueing a stranger ahead
            # of a worker at a gate is the honest order of priorities, and this form can be sent
            # again.
            raise face_engine.http_exception_for(exc) from None

        password_hash = hash_password(password)
        stamp = _now()
        with immediate() as conn:
            waiting = int(
                conn.execute(
                    "SELECT COUNT(*) FROM users WHERE status = ?", (STATUS_PENDING_APPROVAL,)
                ).fetchone()[0]
            )
            cap = int(settings.registration_pending_cap)
            if waiting >= cap:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error_code": "registration_queue_full",
                        "message": (
                            "There are already as many accounts waiting for approval as this "
                            "site accepts. Please try again later, or speak to your "
                            "administrator."
                        ),
                    },
                )
            assigned = ""
            for _attempt in range(ALLOCATION_ATTEMPTS):
                candidate = _next_workforce_id(conn)
                try:
                    conn.execute(
                        "INSERT INTO users (id, name, email, phone, password_hash, role, status, "
                        "enrolled_at, template_version, biometric_id, registration_note) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                        (
                            candidate,
                            full_name,
                            email,
                            phone,
                            password_hash,
                            role,
                            STATUS_PENDING_APPROVAL,
                            stamp,
                            # The face about to be filed can never be confused with whoever held
                            # this number before: the number is reused, and a previous holder's
                            # leftover file is moved aside here (see ``biometrics.new_account_id``).
                            biometrics.new_account_id(candidate),
                            work_details or None,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # The number was taken between the scan and the insert - by a writer outside
                    # this lock, since ``immediate()`` serializes the ones inside it. The write
                    # was a statement, not the transaction, so the band is scanned again here
                    # rather than the applicant being told to send the form a second time: one
                    # shared link is used by many phones at once, and losing that race is the
                    # server's problem to absorb (see the docstring above).
                    continue
                assigned = candidate
                break
            if not assigned:
                # Every attempt lost. Either the band is being written by something that does not
                # take this lock at all, or the queue is far busier than one submission's worth of
                # retries can absorb - and both are answered the same way, because the applicant
                # can only do one thing about either.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "registration_conflict",
                        "message": (
                            "That account number was taken while this was being sent. Please "
                            "send the form again."
                        ),
                    },
                )
            _audit(
                conn,
                action="user_self_registered",
                actor=None,
                entity="users",
                entity_id=assigned,
                after={
                    "name": full_name,
                    "role": role,
                    "status": STATUS_PENDING_APPROVAL,
                    # The consent evidence travels here rather than as a column on the account:
                    # this is the surface that already records who asked for what, from where
                    # and when, and ``request`` is what puts the submitting address in the row.
                    "consent_version": CONSENT_VERSION,
                },
                request=request,
            )
            notifications.notify(
                conn,
                kind=notifications.KIND_REGISTRATION_SUBMITTED,
                severity=notifications.SEVERITY_INFO,
                title="New account awaiting approval",
                body=(
                    f"{full_name} registered as a {role} and is waiting for approval. The "
                    "account can sign in but cannot clock in until you approve it."
                ),
                worker_id=assigned,
                payload={
                    "user_id": assigned,
                    "role": role,
                    "liveness": decision.as_payload(),
                },
                dedupe_key=f"{PENDING_NOTICE_PREFIX}{assigned}",
            )
    except BaseException:
        # Every refusal after the file was written, and every cancellation, takes the file with
        # it - including a full band, a full queue and a face engine that had no room.
        stored.discard()
        raise

    # The account exists. The template goes in *after* the commit, for the reason it always has:
    # ``write_reference`` reads the account's biometric id through its own connection, so inside
    # this request's write lock it would be waiting on the lock it is standing behind.
    try:
        await run_in_threadpool(biometrics.write_reference, assigned, image, embedding)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal: the account is already real
        template_written = False
        template_error = f"{type(exc).__name__}: {exc}"
        log.exception("account %s was created but its face reference could not be written", assigned)
        with db(write=True) as conn:
            notifications.notify(
                conn,
                kind=notifications.KIND_ENROLLMENT_COMPLETED,
                severity=notifications.SEVERITY_WARNING,
                title="A self-registered worker has no face reference",
                body=(
                    f"Account {assigned} ({full_name}) was created, but storing the face "
                    f"reference failed ({template_error}). The photograph is kept at "
                    f"{os.path.basename(stored.path)} in the registration directory until the "
                    "retention sweep collects it, so this account can be enrolled from the "
                    "console before that. Until it is, the account cannot clock in."
                ),
                worker_id=assigned,
                payload={"error": template_error, "photo": os.path.basename(stored.path)},
                dedupe_key=f"registration_template_failed:{assigned}",
            )
    else:
        # The face has a home under an immutable id, so the staged copy is destroyed rather than
        # left behind as a second face nothing sweeps.
        _destroy_photo(stored.path)

    return {
        "status": "success",
        "user_id": assigned,
        "name": full_name,
        "role": role,
        "approval_status": STATUS_PENDING_APPROVAL,
        "template_written": template_written,
        "message": (
            f"Account created. Your user id is {assigned}. You can sign in now, but an "
            "administrator has to approve your account before you can clock in or out."
        ),
    }


# ---------------------------------------------------------------------------
# the review surface
# ---------------------------------------------------------------------------
@admin_router.get("")
async def list_registrations(
    status: str = STATUS_PENDING_APPROVAL,
    limit: int = 200,
    current: CurrentUser = Depends(admin_only),
):
    """The review queue: the accounts waiting for approval, oldest first.

    A queue nobody reads in order is a queue that silently starves whoever applied first - and
    now the person waiting can sign in and watch the clock refuse them, so the order matters to
    somebody who can see it.

    ``status=all`` is the audit view and the other two values are the states a decided account
    can be in; the default is the work to do. The count of everything still waiting comes back
    with the page, so a console can show a badge without asking twice - and it is counted
    separately from ``limit``, because a badge that said "3" because the page was truncated would
    be a badge that lies.

    Ordered by ``CAST(id AS INTEGER)`` rather than by ``id`` as text: these ids are TEXT and the
    allocator hands out the lowest free number, so a lexical order would put 10 before 2 and read
    as a queue that had lost its place.
    """
    wanted = str(status or "").strip().lower()
    limit = max(1, min(int(limit), 1000))
    with db() as conn:
        if wanted and wanted != "all":
            if wanted not in QUEUE_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"status must be one of {', '.join(QUEUE_STATUSES)} or 'all'.",
                )
            rows = conn.execute(
                f"SELECT {ACCOUNT_COLUMNS} FROM users WHERE status = ? "
                "ORDER BY CAST(id AS INTEGER) ASC, id ASC LIMIT ?",
                (wanted, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {ACCOUNT_COLUMNS} FROM users "
                "ORDER BY CAST(id AS INTEGER) DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE status = ?", (STATUS_PENDING_APPROVAL,)
            ).fetchone()[0]
        )
    return {
        "status": "success",
        # What the console's own note about a closed intake is drawn from: a queue that silently
        # stops growing is a queue somebody believes is broken.
        "enabled": intake_accepting(),
        "pending": pending,
        "requests": [_as_pending_user(row) for row in rows],
        "count": len(rows),
    }


@admin_router.get("/intake")
async def read_intake_switch(current: CurrentUser = Depends(admin_only)):  # noqa: ARG001
    """Is the permanent link accepting applications, and which switch decided that.

    Its own route rather than a field on the queue read, because the two questions are asked at
    different moments: the queue is read when a reviewer wants to work, and this is read (and
    written) when an operator wants to change whether the public form is open - which is an
    action on the *link*, not on the applications already waiting on it.

    A site administrator's to read and to write. ``admin_only``, not the root tier: opening or
    closing the company's own public form is an operational decision about this site, which is
    exactly the kind of thing this console exists for. A head administrator is included in that
    guard and has no extra power here - there is one switch, and it is the site's.
    """
    return {"status": "success", **intake_state()}


@admin_router.post("/intake")
async def set_intake_switch(
    request: Request,
    payload: IntakeSwitch,
    current: CurrentUser = Depends(admin_only),
):
    """Open or close the permanent link, from the console, with no restart.

    THE ONE SWITCH THAT DECIDES IT
    ------------------------------
    Whether this deployment runs walk-up registration *out of the box* is
    ``REGISTRATION_ENABLED``, and it is a default rather than a ceiling: writing 1 here accepts
    applications on a deployment whose flag is off, which is the point of the button and the
    reason it is drawn beside the link whoever is holding the phone was given. 0 refuses them
    the same way, whatever the flag said, until somebody opens it again here.

    The answer is read back rather than assumed - the route answers with the state a read would
    - because the console's own panel is about to draw it, and one source of truth for "is this
    form accepting" is the whole reason the read and the write share a shape.

    Audited with the position it replaced, because "the form was open all weekend" is a question
    somebody will ask, and this row's own ``updated_at`` can only answer it for the latest change.
    """
    open_now = bool(payload.open)
    with db(write=True) as conn:
        before = conn.execute("SELECT * FROM registration_settings WHERE id = 1").fetchone()
        # Ensure-then-update rather than an upsert: the row is created by migration 29, but a
        # database whose resets have emptied it - or an operator's first click on a volume the
        # migration never ran against - must not answer 500 to a button.
        conn.execute(
            "INSERT OR IGNORE INTO registration_settings (id, updated_at) VALUES (1, ?)", (_now(),)
        )
        conn.execute(
            "UPDATE registration_settings SET intake_open = ?, updated_at = ?, updated_by = ? "
            "WHERE id = 1",
            (1 if open_now else 0, _now(), current.id),
        )
        _audit(
            conn,
            action="registration_intake_update",
            actor=current,
            entity="registration_settings",
            entity_id=1,
            before={"intake_open": before["intake_open"] if before is not None else None},
            after={"intake_open": 1 if open_now else 0, "requested_open": open_now},
            request=request,
        )
    return {"status": "success", **intake_state()}


def _link_payload(request: Request, state: dict[str, Any]) -> dict[str, Any]:
    """The link's own answer: the address, when it was last replaced, and whether it is open.

    ``accepting`` travels with the URL on purpose. An administrator who copies a link is about to
    send it to somebody, and the one thing that would make that a wasted message - the switch
    being shut - is answered here rather than left for the applicant to discover as a closed
    form. The panel can therefore *move* it as well as say it: the link and the switch are two
    objects (replacing a URL does not close the form, and closing the form does not invalidate
    the URL), so the panel offers both controls instead of pretending they are one.
    """
    row = link_row()
    rotated_at: Any = None
    rotated_by: Any = None
    if row is not None:
        try:
            rotated_at = row["link_rotated_at"]
            rotated_by = row["link_rotated_by"]
        except (IndexError, KeyError):
            pass
    url = link_url(request)
    return {
        "status": "success",
        "url": url,
        "generation": link_generation(),
        "rotated_at": rotated_at,
        "rotated_by": rotated_by,
        "qr_png_data_uri": enrollment._qr_data_uri(url),
        "accepting": state["accepting"],
        "reason": state["reason"],
        "deployment_enabled": state["deployment_enabled"],
    }


@admin_router.get("/link")
async def read_registration_link(
    request: Request, current: CurrentUser = Depends(admin_only)
):  # noqa: ARG001 - the guard is the point
    """The registration link - the URL an administrator sends to somebody who wants to apply.

    WHY THIS IS A GET AND NOT A REVEAL
    ----------------------------------
    There is no "create a link" step and no one-time token to copy: the link is a function of the
    deployment's ``SECRET_KEY`` and one integer (see ``link_token``), so it can be asked for as
    often as anybody wants and is the same answer every time until it is replaced. That is the
    property that makes "a link in the console" work at all - an administrator who wants to send
    it in three messages does not have to mint three links, and one who lost the chat thread does
    not have to revoke the link that is already with an applicant.

    Reachable by a site administrator, ``admin_only`` like the switch beside it: handing out the
    company's own application form is the same kind of decision as opening it.
    """
    return _link_payload(request, intake_state())


@admin_router.post("/link")
async def replace_registration_link(request: Request, current: CurrentUser = Depends(admin_only)):
    """Replaces the link. Every copy already sent stops working, immediately.

    This is the revocation, and it is the only one there is - which is deliberate rather than
    missing. A link is not shared with named people (anybody the company sends it to can use it),
    so "which applicant is this link for" has no answer to revoke against; what can be revoked is
    the link itself, and one bump of the generation does exactly that. An administrator who is
    replacing a link because it reached the wrong person has one action to take and no rows to
    hunt for.

    Nothing already submitted is affected. A submission that arrived under the old link has its
    account, its photograph and its place in the queue; what stops working is the *form*, not the
    applications. The console says so beside the button, because "replace the link" reads like it
    might throw away the queue.
    """
    rotate_link(request, current)
    return _link_payload(request, intake_state())


@admin_router.get("/{user_id}/photo")
async def registration_photo(
    user_id: str, current: CurrentUser = Depends(admin_only)
):  # noqa: ARG001 - the guard is the point
    """The account's reference selfie, for the reviewer who has to judge it.

    A route of its own rather than a field on the list: forty applications in one response would
    otherwise carry forty faces, and the bytes are needed exactly once - by the person looking at
    the one application in front of them.

    Read from the **biometric store**, not from an intake directory: a submission files its face
    straight away (see ``submit_registration``), so that is the only copy there is - and it is the
    copy ``retention`` already knows how to sweep.

    Narrow on purpose: only an account **still waiting** is served. An approved worker's face is
    not a document this screen has any business showing, and a refused one has been wiped - so
    both answer 404, and the console has one sentence for that (``registrationsPhotoGone``) rather
    than a vocabulary that would tell a reader whether a given number is waiting.

    ``no-store`` on the response, because a face is not a document a proxy should keep.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT id, biometric_id, status FROM users WHERE id = ?", (str(user_id),)
        ).fetchone()
    if row is None or str(row["status"] or "") != STATUS_PENDING_APPROVAL:
        raise HTTPException(status_code=404, detail="This account has no photograph on file.")
    path = biometrics.resolve_photo(str(row["id"]), row["biometric_id"])
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="This account has no photograph on file.")
    response = FileResponse(path, media_type="image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_router.post("/{user_id}/approve")
async def approve_registration(
    request: Request,
    user_id: str,
    payload: Decision | None = None,
    current: CurrentUser = Depends(admin_only),
):
    """Lift the quarantine: the account becomes an ordinary, working account.

    WHAT A DECISION NO LONGER HAS TO DO
    -----------------------------------
    The account, the id and the face all exist already - a submission wrote them (see
    ``submit_registration``) - so this route is one compare-and-set on ``users.status`` and
    nothing else. That is the whole point of the shape: the expensive, refusable work happened
    while the applicant was standing there with their phone, and the decision an administrator
    makes an hour or a day later is cheap.

    THE ORDER, AND WHY IT IS THIS ORDER
    -----------------------------------
    **One transaction.** ``UPDATE ... WHERE id = ? AND status = 'pending_approval'`` *is* the
    decision: two administrators clicking approve at the same moment produce one approval and one
    409, because the second sees zero rows updated. The worker's own notice is written in the same
    transaction, so a notice can never exist for an account that was not approved (see
    ``_approved_notice``).

    **The push, after the commit.** ``overtime.deliver_worker_notices`` selects rows that are not
    yet delivered, so inside the write lock that created this one it could not see it - and a
    phone is never worth holding SQLite's single writer for.

    WHAT THIS REPORTS BACK
    ----------------------
    The id and the contact, so the console can write the receipt an administrator reads out - and
    ``template_written``, which is now a *question asked* rather than a step performed: the face
    is checked for on disk here, because an approved account nobody can score cannot clock in, and
    the administrator who just approved it is the person holding the console that can enroll it.
    """
    with db() as conn:
        row = _load_account(conn, user_id)
    if str(row["status"] or "") != STATUS_PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "already_reviewed",
                "message": (
                    f"Account {row['id']} is not waiting for approval any more: its status is "
                    f"{row['status']}."
                ),
            },
        )

    role = str(row["role"] or "").strip().lower()
    if role not in WORKFORCE_ROLES:
        # Only reachable if the row was edited by hand. Refuse rather than bless an account in a
        # role this surface was never allowed to hand out.
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "role_not_available",
                "message": "This account was registered in a role that cannot be approved here.",
            },
        )

    note = (payload.note if payload else None) or None
    notice_written = False
    with immediate() as conn:
        claimed = conn.execute(
            "UPDATE users SET status = 'active' WHERE id = ? AND status = ?",
            (str(row["id"]), STATUS_PENDING_APPROVAL),
        )
        if claimed.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "already_reviewed",
                    "message": "Another administrator decided this account first.",
                },
            )
        _audit(
            conn,
            action="registration_approved",
            actor=current,
            entity="users",
            entity_id=str(row["id"]),
            after={"worker_id": str(row["id"]), "role": role, "note": note},
            request=request,
        )
        # The worker's half of this decision, in the same transaction as the status it is about.
        notice = _approved_notice(row, worker_id=str(row["id"]), role=role)
        notice_written = notifications.notify_worker(
            conn,
            worker_id=str(row["id"]),
            kind=notifications.KIND_WORKER_ACCOUNT_APPROVED,
            title=notice["title"],
            body=notice["body"],
            payload=notice["payload"],
            dedupe_key=f"{APPROVED_NOTICE_PREFIX}{row['id']}",
        )

    # The notice is written and the status is committed; now try to reach the phone. ``push``
    # selects rows that are not yet delivered, so inside the write lock that created this one it
    # could not see it - and a phone is never worth holding SQLite's single writer for.
    overtime.deliver_worker_notices({"worker_notified": notice_written})

    # Asked rather than assumed: the reference was written at submission, and a failure there is a
    # warning on this account until somebody enrolls it. That is a step the administrator who just
    # approved it can take from this console, so it is reported rather than corrected here.
    template_written = biometrics.is_enrolled(str(row["id"]), row["biometric_id"])

    return {
        "status": "success",
        "user_id": str(row["id"]),
        "name": str(row["name"]),
        "role": role,
        # The two values the sign-in screen matches on, so the console can say what to hand over.
        "email": str(row["email"] or ""),
        "phone": str(row["phone"] or ""),
        "template_written": template_written,
        "message": (
            f"Account {row['id']} is approved: {row['name']} can clock in from now on."
            if template_written
            else (
                f"Account {row['id']} is approved, but it has no face reference on file, so it "
                "cannot clock in yet. Enroll this worker from the console."
            )
        ),
    }


@admin_router.post("/{user_id}/reject")
async def reject_registration(
    request: Request,
    user_id: str,
    payload: Decision | None = None,
    current: CurrentUser = Depends(admin_only),
):
    """Turn the applicant down: the account is destroyed and the number goes back.

    WHAT A REFUSAL IS NOW
    ---------------------
    Not a product state on a row that survives - a **deletion**. Under the holding table a refusal
    kept the request, which is what made "may I apply again?" answerable; an account cannot be kept
    that way, because an account that exists can sign in and is a name on the roster. So the
    refusal takes the whole thing: the ``users`` row, the face reference under its biometric id,
    and the administrators' notice that somebody was waiting.

    The decision is still auditable without the row: ``audit_log`` keeps the refusal, with the
    name, the role and the reason, and it is the copy that survives an account being deleted -
    which a ``users`` row could never be.

    THE ORDER MATTERS, AND IT IS THE OPPOSITE OF THE OBVIOUS ONE
    -----------------------------------------------------------
    The row is deleted **first**, inside the compare-and-set (``WHERE status = 'pending_approval'``,
    so a second administrator clicking reject - or one clicking reject after a colleague approved -
    is answered 409 rather than deleting a working account). The files are wiped *after*, with the
    biometric id read before the delete, because ``biometrics.remove_files`` would otherwise have
    to look that id up in a row that no longer exists. A wipe that fails is reported rather than
    swallowed: the account is still gone, and an operator is told which file to remove by hand.

    WHY THE NUMBER IS FREE AFTERWARDS, AND WHY THAT IS SAFE
    -------------------------------------------------------
    A refusal returns the number to the band, and the allocator will hand it to the next applicant
    (see ``_next_workforce_id``). What makes that safe is the wipe in this function - not the
    allocator's arithmetic - so the next holder of this id cannot be scored against the face of
    the person before them.
    """
    with db() as conn:
        row = _load_account(conn, user_id)
    if str(row["status"] or "") != STATUS_PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "already_reviewed",
                "message": (
                    f"Account {row['id']} is not waiting for approval any more: its status is "
                    f"{row['status']}."
                ),
            },
        )

    note = (payload.note if payload else None) or None
    with immediate() as conn:
        deleted = conn.execute(
            "DELETE FROM users WHERE id = ? AND status = ?",
            (str(row["id"]), STATUS_PENDING_APPROVAL),
        )
        if deleted.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "already_reviewed",
                    "message": "Another administrator decided this account first.",
                },
            )
        _audit(
            conn,
            action="registration_rejected_purged",
            actor=current,
            entity="users",
            entity_id=str(row["id"]),
            before={"name": row["name"], "role": row["role"]},
            after={"note": note},
            request=request,
        )
        # The administrators' "somebody is waiting" notice goes with the account it was about: an
        # alert nobody can act on any more is how a queue screen comes to look broken.
        conn.execute(
            "DELETE FROM admin_notifications WHERE dedupe_key = ?",
            (f"{PENDING_NOTICE_PREFIX}{row['id']}",),
        )

    removed, failed = biometrics.remove_files(str(row["id"]), row["biometric_id"])
    if failed:
        log.warning(
            "account %s was refused but these files could not be removed: %s", row["id"], failed
        )

    return {
        "status": "success",
        "user_id": str(row["id"]),
        "photo_destroyed": not failed,
        "message": (
            "The account was refused and its face has been destroyed."
            if not failed
            else (
                "The account was refused, but its face reference could not be removed and is "
                "still on disk. Remove it by hand."
            )
        ),
    }
