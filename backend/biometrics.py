"""Where a person's biometric files live - and the only place that knows.

Two files make up a person's biometric record:

* the **template** (``local_references/<name>.json``) - the VGG-Face embedding every
  punch is scored against;
* the **reference selfie** (``worker_photos/<name>.jpg``) - what an administrator
  looks at when a match is disputed.

This module owns the ``<name>``. Nothing else may build that filename.

Why the name is a random id and not the account id
-------------------------------------------------
The files used to be named after the account: ``worker_photos/1.jpg``. Two
consequences, both about faces rather than about tidiness:

* **``1.jpg`` names a person from a number anybody can iterate.** A directory
  listing, a stray backup, a misconfigured static mount or a path traversal then maps
  a *face* to a worker id - and the id is the login half of the credential, so one
  number joins a face to an account and to a name.
* **Account ids are recycled.** An administrator may delete an account (the console
  refuses it while history is attached, but a mistyped id has none), and the next
  worker is issued the id that just came free. A template whose deletion failed -
  ``/admin/users/delete`` *reports* those failures, it does not hide them - was
  therefore inherited by whoever took the id next: the new worker's punch would be
  matched against the previous worker's face. A random id cannot be inherited,
  because a new account never reuses one.

An id is minted once per account, stored in ``users.biometric_id`` (migration 11
backfills every existing row), and never changes. Re-enrolling replaces the files the
id names; it does not move the account to a new id, so an audit row, a backup or an
operator's note that recorded the name still points at the same person.

What happens to the faces already on disk
-----------------------------------------
``adopt_legacy_files()`` renames what is already stored to the id its account now
holds, and is idempotent. The readers here keep a **legacy fallback** - if the
id-named file is absent and a file named after the account id is present, that one is
used - so a rename blocked by a running backup cannot take a worker's clock-in away,
and upgrading a live site mid-shift needs no downtime. The fallback is the migration
path, not the steady state: a file left under a sequential name is reported by
``readiness`` so an operator can see that the adoption had work left to do.

The one thing the fallback could get wrong is handled where it would happen: a
*new* account created on an id that somebody else used to hold clears any leftover
legacy file for that id first (``new_account_id``), so it can never resolve to a
face that is not its own.

What produced the embedding (and why it is recorded)
----------------------------------------------------
The file used to be a bare JSON list - ``[0.013, -0.24, ...]`` - and every reader had
to know that by convention. That is not enough any more, because the *crop* the
embedding is computed from is part of the template's meaning: the face detector was
replaced (MTCNN -> YuNet, see ``face_detector``) and its box around a face is ~19%
different, so an embedding made under the old detector does not describe the same
picture a new punch produces. Scoring the two against each other gives a number that
means nothing - and worse, a number that can look like a *mismatch*, accusing a worker
of being somebody else when the real answer is "this template is from the old pipeline".

So the template now records what made it::

    {"model": "VGG-Face", "pipeline": "yunet-2023mar", "dimensions": 4096,
     "embedding": [0.013, -0.24, ...]}

A bare list is still *read* - an upgraded site has thousands of them and none of them
is unreadable - but it is read as ``pipeline = None``: "made before we recorded this",
which is exactly as stale as the recorded names say it is. The two shapes are handled
in one place, ``read_reference``, so no caller has to ask which one it is holding.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from database import db

log = logging.getLogger(__name__)

#: 128 bits of ``secrets``, hex. The name of a biometric file has to be unguessable,
#: and ``random`` is not random enough for a name that stands between a face and the
#: internet.
ID_BYTES = 16

#: Suffix given to a legacy-named file that is moved out of the way rather than
#: deleted (see ``quarantine_legacy``). Nothing reads a name with this on the end.
QUARANTINE_SUFFIX = ".unclaimed"

#: How wide the reference selfie kept beside the template is allowed to be. Also what
#: ``main.enroll_worker`` has always used, so the stored photo looks the same.
PHOTO_MAX_EDGE = 800
PHOTO_QUALITY = 88


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------
def new_id() -> str:
    """A fresh biometric id: 32 hex characters, never derived from the account."""
    return secrets.token_hex(ID_BYTES)


def id_from(row: Any) -> str | None:
    """The id on an already-read ``users`` row, or ``None`` when the row has none.

    Read defensively on purpose: callers hold rows selected before this column existed
    (a stale cursor, a hand-written query), and "this row does not carry an id" must fall
    through to the legacy name rather than raise inside a clock-in.

    ``None`` for a NULL column, never the string ``"None"``: that string is a filename,
    and every account sharing a NULL would then share one template at ``None.json`` - the
    exact class of collision this module exists to prevent. (Found by the suite: a row
    whose column was NULL resolved to a template called ``None.json`` rather than being
    given an id of its own.)
    """
    try:
        value = row["biometric_id"]
    except (IndexError, KeyError, TypeError):
        return None
    if value is None:
        return None
    return str(value).strip() or None


def ensure_id(conn: sqlite3.Connection, user_id: str) -> str:
    """``user_id``'s id, minting one when the row does not carry it yet.

    The migration backfills every existing row, so this is the safety net for a row
    that arrived some other way (a hand-run script, a restore from before the
    migration). It writes, because an id that is never recorded is not an id.
    """
    row = conn.execute("SELECT biometric_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError(f"no users row for {user_id!r}")
    current = id_from(row)
    if current:
        return current
    fresh = new_id()
    conn.execute("UPDATE users SET biometric_id = ? WHERE id = ?", (fresh, user_id))
    return fresh


def id_for(user_id: str) -> str | None:
    """``user_id``'s id, or ``None`` when the database cannot answer.

    A read of a biometric file must not fail because this lookup did: the caller has a
    legacy name to fall back on, and answering 500 at the gate for a missing column
    would be far worse than answering with the old filename.
    """
    try:
        with db(write=True) as conn:
            return ensure_id(conn, user_id)
    except (sqlite3.Error, LookupError):
        return None


def new_account_id(user_id: str) -> str:
    """An id for an account that is being created, with leftovers for that id cleared.

    Called by every path that inserts a ``users`` row. The quarantine is the whole
    point: if a previous holder of this account id left a legacy-named template behind
    (a delete whose file removal failed, for instance), leaving it there would let the
    new worker's punch resolve to *that* face. Nothing else reassigns an id, so this is
    the only place the two account ids can meet.
    """
    quarantine_legacy(user_id)
    return new_id()


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def directories() -> tuple[str, str]:
    """``(references_dir, photos_dir)``, read from ``main`` on every call.

    Read lazily on purpose: ``main`` imports this module, and the test suite redirects
    ``main.LOCAL_REFS_DIR`` / ``main.WORKER_PHOTOS_DIR`` into a temp directory so a test
    can never write a face over a real worker's. Capturing them at import time would
    defeat that.
    """
    import main

    return main.LOCAL_REFS_DIR, main.WORKER_PHOTOS_DIR


def reference_path(biometric_id: str) -> str:
    """The template path for an id. Does not check that it exists."""
    refs_dir, _ = directories()
    return os.path.join(refs_dir, f"{biometric_id}.json")


def photo_path(biometric_id: str) -> str:
    """The reference selfie path for an id. Does not check that it exists."""
    _, photos_dir = directories()
    return os.path.join(photos_dir, f"{biometric_id}.jpg")


def legacy_reference_path(user_id: str) -> str:
    """Where this account's template lived before the id existed."""
    refs_dir, _ = directories()
    return os.path.join(refs_dir, f"{user_id}.json")


def legacy_photo_path(user_id: str) -> str:
    """Where this account's reference selfie lived before the id existed."""
    _, photos_dir = directories()
    return os.path.join(photos_dir, f"{user_id}.jpg")


def resolve_reference(user_id: str, biometric_id: str | None = None) -> str:
    """The template to read for this account.

    The id-named file if it is there, otherwise a legacy-named one if *that* is there,
    otherwise the id name again. Returning the canonical path for a person who has no
    template at all keeps the caller's "not registered" answer and its 404 exactly what
    they were: nothing here knows what a missing file means.
    """
    resolved = (biometric_id or "").strip() or id_for(user_id)
    canonical = reference_path(resolved) if resolved else None
    if canonical and os.path.exists(canonical):
        return canonical
    legacy = legacy_reference_path(user_id)
    if os.path.exists(legacy):
        return legacy
    return canonical or legacy


def resolve_photo(user_id: str, biometric_id: str | None = None) -> str | None:
    """The reference selfie on disk for this account, or ``None`` if there is none."""
    resolved = (biometric_id or "").strip() or id_for(user_id)
    if resolved:
        canonical = photo_path(resolved)
        if os.path.exists(canonical):
            return canonical
    legacy = legacy_photo_path(user_id)
    return legacy if os.path.exists(legacy) else None


def is_enrolled(user_id: str, biometric_id: str | None = None) -> bool:
    """Whether a template can be read for this account, under either name."""
    return os.path.exists(resolve_reference(user_id, biometric_id))


# ---------------------------------------------------------------------------
# what a template is made of
# ---------------------------------------------------------------------------
#: Why a template can be present and still not usable, in a sentence an administrator can
#: act on. The value is a *code*; the words beside it are written where they are shown.
STALE_UNREADABLE = "unreadable"
STALE_NO_PROVENANCE = "no_provenance"
STALE_OTHER_PIPELINE = "other_pipeline"


@dataclass(frozen=True)
class Reference:
    """A stored template: the vector, and what produced it.

    ``pipeline`` is ``None`` for a file written before provenance was recorded - the bare
    JSON lists every existing site is full of. ``None`` is not "unknown, probably fine":
    it means the template predates the detector change and is exactly the case that needs
    re-enrollment.
    """

    embedding: list[float]
    pipeline: str | None
    model: str | None = None

    def __len__(self) -> int:
        return len(self.embedding)


def current_pipeline() -> str:
    """The pipeline this build writes, from the detector that is actually compiled in.

    Read from ``face_detector`` rather than duplicated as a constant here, so the version
    recorded beside a template and the version a punch compares it against cannot drift -
    which is the one failure that would make every template look current or every template
    look stale. ``active_pipeline``, not the prototype: on a host without the detector model
    the application falls back to the previous one, and a template written there has to be
    labelled with what made it.
    """
    import face_detector

    return face_detector.active_pipeline()


def _model_name() -> str:
    """The recognition model beside the pipeline, for a log line or a forensic read."""
    import face_engine

    return face_engine.FACE_MODEL


def read_reference(path: str) -> Reference:
    """The template at ``path``, in either shape. Raises on a file that is neither.

    The only reader. Everything that needs an embedding or needs to know what made one
    comes through here, so the two on-disk formats are one concept with one parser.
    """
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        embedding = payload.get("embedding")
        if not isinstance(embedding, list):
            raise ValueError(f"template at {os.path.basename(path)} carries no embedding")
        pipeline = payload.get("pipeline")
        model = payload.get("model")
        return Reference(
            embedding=[float(value) for value in embedding],
            pipeline=str(pipeline) if pipeline else None,
            model=str(model) if model else None,
        )
    if isinstance(payload, list):
        return Reference(embedding=[float(value) for value in payload], pipeline=None)
    raise ValueError(f"template at {os.path.basename(path)} is neither a vector nor a record")


def stale_reason(reference: Reference, *, expected_dimensions: int | None = None) -> str | None:
    """Why ``reference`` cannot be scored, or ``None`` when it can.

    Ordered by what an administrator has to do about it, most actionable first. A wrong
    *dimension* is checked too: an embedding of a different size cannot be compared at
    all, and the failure that produces is a shape error deep inside a numpy call, which
    surfaces to a worker as "Internal processing error" - a 500 that says nothing. Here it
    is the same refusal as a stale template, which names the fix.
    """
    if expected_dimensions is not None and len(reference.embedding) != expected_dimensions:
        return STALE_UNREADABLE
    if reference.pipeline is None:
        return STALE_NO_PROVENANCE
    if reference.pipeline != current_pipeline():
        return STALE_OTHER_PIPELINE
    return None


def reference_health(
    user_id: str, biometric_id: str | None = None, *, expected_dimensions: int | None = None
) -> tuple[bool, str | None]:
    """``(usable, reason)`` for this account's template. Never raises.

    ``usable`` is ``False`` when a punch against this template would be meaningless. A
    caller that only wants "can this person clock in" should keep using ``is_enrolled``:
    that answers whether a *file* is there, which is a different question from whether the
    template in it still describes the same kind of picture.
    """
    path = resolve_reference(user_id, biometric_id)
    if not os.path.exists(path):
        return False, "missing"
    try:
        reference = read_reference(path)
    except (OSError, ValueError, TypeError):
        return False, STALE_UNREADABLE
    reason = stale_reason(reference, expected_dimensions=expected_dimensions)
    return reason is None, reason


#: What each reason means to the person who has to clear it. One place, so the roster, the
#: admin list and a log line all say the same thing.
STALE_EXPLANATIONS: dict[str, str] = {
    STALE_UNREADABLE: (
        "the stored face template cannot be used (it is unreadable, or was made by a "
        "different face model) and needs to be taken again"
    ),
    STALE_NO_PROVENANCE: (
        "the stored face template was made before the face detector was replaced; it "
        "pictures a different crop of the face and has to be re-enrolled"
    ),
    STALE_OTHER_PIPELINE: (
        "the stored face template was made by a different face pipeline and has to be "
        "re-enrolled"
    ),
}


def stale_references(*, expected_dimensions: int | None = None) -> dict[str, Any]:
    """Every enrolled account whose template cannot be scored, and why.

    The re-enrollment worklist. Reported rather than repaired: replacing a face template
    means a human taking a photograph, which is the whole point of the enrollment flow -
    so this answers "who has to be photographed again", and the existing enrollment paths
    (``/admin/enroll``, or an enrollment link for a worker who is somewhere else) do the
    repair.

    An account with *no* template is deliberately absent: it is not stale, it is
    unenrolled, and the roster already says so.
    """
    import face_detector

    worklist: list[dict[str, Any]] = []
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, name, role, biometric_id FROM users "
                "WHERE biometric_id IS NOT NULL AND biometric_id <> '' "
                "ORDER BY CAST(id AS INTEGER) ASC"
            ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - the database is not there yet
        return {
            "pipeline": face_detector.active_pipeline(),
            "stale": [],
            "count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }

    for row in rows:
        user_id = str(row["id"])
        path = resolve_reference(user_id, str(row["biometric_id"]))
        if not os.path.exists(path):
            continue
        reason: str | None
        pipeline: str | None = None
        try:
            reference = read_reference(path)
            pipeline = reference.pipeline
            reason = stale_reason(reference, expected_dimensions=expected_dimensions)
        except (OSError, ValueError, TypeError):
            reason = STALE_UNREADABLE
        if reason is None:
            continue
        worklist.append(
            {
                "id": user_id,
                "name": row["name"],
                "role": row["role"],
                "reason": reason,
                "template_pipeline": pipeline,
                "file": os.path.basename(path),
            }
        )

    return {
        "pipeline": face_detector.active_pipeline(),
        "detector": face_detector.active_detector(),
        "stale": worklist,
        "count": len(worklist),
        "enrolled_checked": len(rows),
    }


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def write_reference(user_id: str, image, embedding: list[float]) -> str:
    """Store an embedding and its reference selfie under the account's id.

    Written to a temporary name and ``os.replace``d into position, both files: a crash
    halfway through must not leave a truncated template, which would make every
    subsequent punch fail with an unreadable embedding. The temporary name carries
    random bytes as well, because half a template is still half a face on disk.

    Returns the template path.
    """
    resolved = id_for(user_id)
    if resolved is None:
        raise RuntimeError(f"cannot store a reference for {user_id!r}: no biometric id was available")
    refs_dir, photos_dir = directories()
    os.makedirs(refs_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)

    reference = reference_path(resolved)
    temporary_reference = f"{reference}.{secrets.token_hex(6)}.tmp"
    with open(temporary_reference, "w", encoding="utf-8") as handle:
        # Provenance beside the vector, in the same file and the same write. A separate
        # sidecar could be orphaned by a crash between the two writes, and the one thing
        # this record must never do is describe a template that is not the one on disk.
        json.dump(
            {
                "model": _model_name(),
                "pipeline": current_pipeline(),
                "dimensions": len(embedding),
                "embedding": [float(value) for value in embedding],
            },
            handle,
        )
    os.replace(temporary_reference, reference)

    thumbnail = image.copy()
    thumbnail.thumbnail((PHOTO_MAX_EDGE, PHOTO_MAX_EDGE))
    photo = photo_path(resolved)
    temporary_photo = f"{photo}.{secrets.token_hex(6)}.tmp"
    thumbnail.save(temporary_photo, format="JPEG", quality=PHOTO_QUALITY)
    os.replace(temporary_photo, photo)
    return reference


def remove_files(user_id: str, biometric_id: str | None = None) -> tuple[list[str], list[str]]:
    """Remove a person's template and selfie under **both** names. ``(removed, failed)``.

    Both, deliberately. An account enrolled before the id existed may have left a
    legacy-named file behind, and the reason this module exists is that a deleted
    account's face must not be inherited by whoever is issued that id next - so
    deleting only the id-named file would leave exactly the file that could be.

    ``failed`` is returned rather than logged: the caller tells the administrator which
    files it could not remove, because a silent failure here is invisible forever.
    """
    resolved = (biometric_id or "").strip() or id_for(user_id)
    candidates: list[str] = []
    if resolved:
        candidates += [reference_path(resolved), photo_path(resolved)]
    candidates += [legacy_reference_path(user_id), legacy_photo_path(user_id)]

    removed: list[str] = []
    failed: list[str] = []
    for path in candidates:
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(os.path.basename(path))
        except OSError:
            failed.append(os.path.basename(path))
    return removed, failed


def quarantine_legacy(user_id: str) -> list[str]:
    """Move any legacy-named file for ``user_id`` out of reach of the id-based readers.

    Renamed, never deleted: it is still somebody's face, and a file the application
    silently destroys is worse than one an operator can see and remove deliberately.
    The new name is never read by anything (see ``QUARANTINE_SUFFIX``).
    """
    moved: list[str] = []
    for path in (legacy_reference_path(user_id), legacy_photo_path(user_id)):
        if not os.path.exists(path):
            continue
        target = f"{path}{QUARANTINE_SUFFIX}-{secrets.token_hex(4)}"
        try:
            os.replace(path, target)
            moved.append(os.path.basename(target))
        except OSError:  # pragma: no cover - a locked file; the caller still gets a fresh id
            log.warning("legacy biometric file %s could not be moved aside", path)
    return moved


# ---------------------------------------------------------------------------
# the migration of what is already on disk
# ---------------------------------------------------------------------------
def _adopt_pair(legacy: str, target: str, summary: dict[str, list]) -> None:
    """Move one legacy-named file to its id-named counterpart, once.

    Three outcomes, and each is worth telling an operator about:

    * the legacy file is there and the target is not -> rename;
    * both are there -> *leave it alone* and report it as ``superseded``: the id-named
      file is the one the application wrote, so the legacy file is an older copy of the
      same person's template, and deciding to delete a face is a human's call;
    * neither is there -> nothing to do.
    """
    if not os.path.exists(legacy):
        return
    if os.path.exists(target):
        summary["superseded"].append(os.path.basename(legacy))
        return
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(legacy, target)
        summary["renamed"].append(os.path.basename(target))
    except OSError as exc:
        summary["failed"].append({"file": os.path.basename(legacy), "error": str(exc)})


def adopt_legacy_files() -> dict[str, Any]:
    """Give every existing account's stored files the name its id asks for.

    The filesystem half of the change; the database half is migration 11, which mints
    an id per row. Kept out of the migration itself so that the migration stays what
    this codebase requires every migration to be - a pure, additive, replayable
    statement about the schema that tools can run against an in-memory copy - while
    this is idempotent filesystem work that a running backup can interrupt and a second
    boot can finish. Called from ``main.init_db()``; a failure here is logged and
    survives, because the readers fall back to the legacy names anyway.

    Reports what it did, including what it deliberately did not touch: a live site
    adopting this change should be able to read one summary and know whether any face
    is still filed under an account id.
    """
    summary: dict[str, list[Any]] = {"renamed": [], "superseded": [], "failed": [], "orphans": []}
    refs_dir, photos_dir = directories()
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, biometric_id FROM users "
                "WHERE biometric_id IS NOT NULL AND biometric_id <> ''"
            ).fetchall()
            known = {str(row["id"]) for row in conn.execute("SELECT id FROM users").fetchall()}
    except sqlite3.Error as exc:  # pragma: no cover - the database is not there yet
        log.warning("biometric file adoption could not read the user table: %s", exc)
        return {**summary, "error": f"{type(exc).__name__}: {exc}"}

    for row in rows:
        user_id = str(row["id"])
        resolved = str(row["biometric_id"])
        _adopt_pair(legacy_reference_path(user_id), reference_path(resolved), summary)
        _adopt_pair(legacy_photo_path(user_id), photo_path(resolved), summary)

    # A legacy-named file whose account is gone is a face nobody owns any more. It is
    # left on disk (deleting it is an operator's decision) but it is counted here, since
    # it is the one file that could still be reached by the fallback if that account id
    # is ever issued again - and ``new_account_id`` would move it aside if it were.
    for folder, suffix in ((refs_dir, ".json"), (photos_dir, ".jpg")):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            stem, extension = os.path.splitext(name)
            if extension == suffix and stem.isdigit() and stem not in known:
                summary["orphans"].append(name)
    return summary


def legacy_files_remaining() -> dict[str, int]:
    """How many files are still filed under an account id. For readiness to report.

    A deployment that has upgraded should reach zero here (``superseded`` files, where
    an account was re-enrolled after the change, need a human's decision and are counted
    separately by ``adopt_legacy_files``). Any other answer means the adoption was
    interrupted and the readers are still relying on the fallback.
    """
    counts = {"references": 0, "photos": 0}
    refs_dir, photos_dir = directories()
    try:
        with db() as conn:
            known = {str(row["id"]) for row in conn.execute("SELECT id FROM users").fetchall()}
    except sqlite3.Error:
        return counts
    for folder, suffix, key in ((refs_dir, ".json", "references"), (photos_dir, ".jpg", "photos")):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        counts[key] = sum(
            1
            for name in names
            if name.endswith(suffix) and os.path.splitext(name)[0].isdigit()
            and os.path.splitext(name)[0] in known
        )
    return counts
