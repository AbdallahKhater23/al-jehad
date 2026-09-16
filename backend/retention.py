"""Automated data retention: deciding what this system may still be holding, and erasing it.

WHY THIS IS A MODULE AND NOT A CRON LINE
----------------------------------------
This application accumulates four kinds of personal data and, before this module, kept all
four forever:

* **raw punch selfies** — ``quick_link_photos/*.jpg``, one per tap of a clock link, with the
  worker's face, the site and the time in the same row (``quick_link_uses``);
* **face templates and reference selfies** — ``local_references/*.json`` and
  ``worker_photos/*.jpg``, an embedding of a face and a photograph of it, keyed to an
  account (``biometrics`` owns the naming);
* **audit rows** — who did what to whom, with IP and user-agent;
* **raw offline punches** — ``punch_queue``, carrying lat/lon to roughly phone accuracy.

Nothing deleted any of them, so "we comply with a retention policy" was an aspiration with no
mechanism behind it. The cost of that is not abstract: under GDPR art. 5(1)(e) and 17, and
under biometric statutes that require a documented destruction schedule, an attendance
database that keeps biometrics indefinitely is the finding, not the mitigation.

The four decisions this module makes, and why each is made the way it is:

1. **Retention lives in one place, as data.** :class:`Policy` is built from ``settings``, is
   printed by ``python -m retention --policy``, and is copied verbatim into the compliance
   event each sweep writes. A retention period that only exists in a schedule someone
   remembers is not a retention period.

2. **A purge is an event, not an absence.** Every applied sweep appends an ``audit_log`` row
   naming the cutoffs, the counts, the byte volume and a **digest of what was removed** — so
   the erasure can be proven afterwards without keeping the data that was erased. Deleting
   audit rows is itself audited, in the same transaction, after the deletion.

3. **The database rows are only half of it.** A row that points at a photo which is still on
   disk is a face that is still on disk. So a file is wiped *and* its reference is cleared
   (:func:`_sweep_punch_photos`), and the file-side sweeps look at the directory itself, not
   at the database — which is the only way to find a face whose row is already gone.

4. **Dry run by default, everywhere.** ``sweep(dry_run=True)`` opens the database
   **read-only**, so a dry run cannot write even by accident: an attempted write raises
   ``attempt to write a readonly database``. That is a stronger guarantee than "we pass a flag
   around and hope", and it is what makes ``python -m retention`` safe to hand an operator
   before the first real run.

WHAT THIS DOES NOT DO — READ THIS BEFORE PROMISING ANYONE ERASURE
-----------------------------------------------------------------
**Overwriting a file is best effort, not a guarantee.** :func:`wipe_file` overwrites the
file's bytes in place, fsyncs, and unlinks. That is the most a portable application can do,
and on a modern deployment it is worth less than it sounds:

* **SSDs and flash** write to a fresh cell and retire the old one; the controller decides when
  the physical page is erased, and the filesystem cannot address the old copy at all. Wear
  levelling means the bytes may outlive the write.
* **Copy-on-write filesystems** (btrfs, ZFS, APFS, and any snapshot) keep the old blocks alive
  by design; an overwrite writes new blocks and the old ones remain in the snapshot.
* **Backups** are outside this process. A deleted row is still in every dump taken while it
  existed, and pruning a live database does nothing to a nightly ``.db`` copy. Retention
  periods in a backup rotation have to be configured there too.
* **Deleted database rows are not erased bytes.** SQLite frees the page and leaves its
  contents in the file (and in the WAL until checkpoint). ``PRAGMA freelist_count`` is
  reported by every sweep so an operator can see the reclaimed space; making it *unreadable*
  needs ``VACUUM``, which rewrites the whole file and needs an exclusive lock, so it is an
  operator's deliberate command (``--vacuum``) and not something a timer does behind their
  back.

The only construction that makes erasure unconditional is **cryptographic deletion**: encrypt
each subject's data under a per-subject key, and destroy the key. Then the ciphertext's
location stops mattering. This codebase does not do that today — ``decisions`` in the README
records it as the honest next step — and until it does, "wiped" here means *overwritten and
unlinked, best effort*, which is exactly what these docstrings and the compliance event say.

THE ORDER OF OPERATIONS, AND WHY IT IS THIS ORDER
-------------------------------------------------
For a photo: **wipe the file, then clear the row.** The reverse order has a window in which
the row is gone and the file is not — an unattributed face on disk that nothing on any screen
could ever explain. The order used here has the failure mode of a row pointing at a missing
file, which the readers already treat as "no photo" (the console's URL answers 404, the same
way it does for a punch whose selfie was discarded at the time).

Across targets: each one runs inside its own ``SAVEPOINT``. A prune that cannot run — a locked
audit table, a permissions error, a missing column on a database somebody restored from an
older dump — must not cancel the three that can. Failures are collected, reported, and written
into the compliance event, and the *next* sweep picks them up again.

WHAT IS NEVER DELETED
---------------------
``attendance_logs``. It is the record of hours worked and therefore of money owed, and a
retention policy that silently removes a timesheet is a wage dispute with a timestamp. The
sweep reports its size, its age, and how many rows are past the audit horizon, and deletes
nothing — the only automated way to lose a pay record would be a bug in this file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

import biometrics
import database
import notifications
from config import settings

log = logging.getLogger("attendance.retention")

#: Every timestamp in this application is naive local time in this format, so retention
#: cutoffs are too. Comparing them as strings is the same comparison the rest of the code
#: makes (the shift queries, the notification queue) and needs no date parsing to be correct.
TS = "%Y-%m-%d %H:%M:%S"

#: Overwrite granularity. Big enough that a 4 MB selfie is four writes, small enough that a
#: multi-gigabyte directory of punch selfies does not become a memory event.
WIPE_CHUNK_BYTES = 1024 * 1024

#: How many individual names a report keeps before it stops listing them. The counts and the
#: digest are always complete; this only bounds the size of the report itself.
MAX_LISTED = 20

#: The trigger that makes ``audit_log`` append-only, added by migration 1. Re-created from
#: ``sqlite_master`` rather than from a copy of the statement text, so the guard a retention
#: delete temporarily removes is *provably* the same guard that was there before.
AUDIT_DELETE_GUARD = "audit_log_no_delete"


def _now() -> datetime:
    return datetime.now()


def _stamp(moment: datetime) -> str:
    return moment.strftime(TS)


def digest(names: Iterable[str]) -> str:
    """A stable SHA-256 over a set of identifiers, for the compliance record.

    This is what lets an erasure be *proven* later without retaining what was erased: the
    event carries a fingerprint of the exact rows and files that went, so an auditor who
    still has a copy can verify that the same set is missing from the live system.
    """
    hasher = hashlib.sha256()
    for name in sorted(names):
        hasher.update(str(name).encode("utf-8", "replace"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _listed(names: list[str]) -> list[str]:
    """At most :data:`MAX_LISTED` names, with the remainder summarised."""
    if len(names) <= MAX_LISTED:
        return names
    return [*names[:MAX_LISTED], f"... and {len(names) - MAX_LISTED} more"]


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Policy:
    """Retention periods, in days. ``0`` always means *keep forever*.

    Zero is the escape hatch for every knob, and it is deliberately the same value in all of
    them: an operator who wants the sweeper to leave one category alone never has to remember
    whether that category uses ``0``, ``-1`` or ``None`` to say so.
    """

    #: Raw punch selfies (quick clock links). The single largest accumulation of faces here:
    #: one photograph per tap, for people who may never have been enrolled at all.
    punch_photo_days: int = 30
    #: Biometric residue: files with no live owning account, plus half-written templates.
    #: Note that *deactivation deletes a face immediately* (``main.deactivate_user``) and this
    #: window is the safety net for the cases where that could not finish — a file that was
    #: locked by a backup, a template left by a crashed enrollment, a quarantined legacy file.
    biometric_days: int = 7
    #: Administrative audit rows. One year by default: long enough to answer "who changed
    #: this worker's rate in March", short enough that the IP addresses do not outlive the
    #: relationship by much.
    audit_days: int = 365
    #: Read notifications. Unread ones are an administrator's work queue and are never
    #: deleted, only reported — a notification nobody has looked at yet is a task, not slops.
    notification_days: int = 180
    #: Raw offline punches, once the punch has a record in ``attendance_logs`` (or was
    #: refused). These carry coordinates to phone accuracy, so they are location history.
    punch_queue_days: int = 90
    #: Offline replay anchors. Pure anti-replay metadata; only ever useful for a punch that
    #: has not arrived yet, and a queued punch older than the sync window is refused anyway.
    anchor_days: int = 7
    #: How many items one sweep will remove. **The one knob where 0 does not mean "keep it":
    #: it means no cap**, which a deployment with a large first-run backlog should think about
    #: before setting. The reason the cap exists is the gate: the sweep takes SQLite's single
    #: write lock (``BEGIN IMMEDIATE``) for as long as its work takes, and a punch that arrives
    #: while a first run is deleting a year of audit rows waits on ``busy_timeout`` and can
    #: fail. 20 000 rows is well under a second on this database, so a backlog drains over a
    #: handful of sweeps while a site keeps clocking in - and every sweep reports how many
    #: items it deferred, so "draining" is visible rather than assumed.
    max_items_per_sweep: int = 20_000

    @property
    def cap(self) -> int | None:
        """``max_items_per_sweep`` as a SQL ``LIMIT``, or ``None`` for no cap."""
        return int(self.max_items_per_sweep) if int(self.max_items_per_sweep) > 0 else None

    def as_dict(self) -> dict[str, int]:
        return {
            "punch_photo_days": int(self.punch_photo_days),
            "biometric_days": int(self.biometric_days),
            "audit_days": int(self.audit_days),
            "notification_days": int(self.notification_days),
            "punch_queue_days": int(self.punch_queue_days),
            "anchor_days": int(self.anchor_days),
            "max_items_per_sweep": int(self.max_items_per_sweep),
        }

    def cutoff(self, days: int, *, now: datetime | None = None) -> str | None:
        """The oldest timestamp that is still inside the window, or ``None`` for "forever"."""
        days = int(days)
        if days <= 0:
            return None
        return _stamp((now or _now()) - timedelta(days=days))

    def cutoff_epoch(self, days: int, *, now: datetime | None = None) -> float | None:
        """The same cutoff as an mtime, for files: mtimes are epoch seconds, not strings."""
        days = int(days)
        if days <= 0:
            return None
        return (now or _now()).timestamp() - days * 86400.0


def policy() -> Policy:
    """The policy as currently configured.

    Read per call rather than captured at import, so a test (or ``python -m retention``
    reading a different ``.env``) gets the values it set.
    """
    return Policy(
        punch_photo_days=settings.retention_punch_photo_days,
        biometric_days=settings.retention_biometric_days,
        audit_days=settings.retention_audit_days,
        notification_days=settings.notification_retention_days,
        punch_queue_days=settings.retention_punch_queue_days,
        anchor_days=settings.retention_anchor_days,
        max_items_per_sweep=settings.retention_max_items_per_sweep,
    )


def _cap_sql(cap: int | None) -> str:
    """``" LIMIT n"`` or nothing, for a statement that must honour the per-sweep cap."""
    return f" LIMIT {int(cap)}" if cap is not None else ""


# ---------------------------------------------------------------------------
# erasure primitives
# ---------------------------------------------------------------------------
def _fsync_directory(path: str) -> None:
    """Persist a directory entry change. Silent on platforms that cannot do it.

    Windows refuses to open a directory as a file, and the durability this buys on POSIX
    (the unlink surviving a power cut) is not available there through this call. Neither
    outcome may fail a sweep, so the failure is swallowed — the wipe has already happened.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows, or a directory we cannot open
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - some filesystems refuse fsync on a directory
        pass
    finally:
        os.close(descriptor)


def wipe_file(path: str, *, directory: str) -> int:
    """Overwrite a file's bytes, then unlink it. Returns the bytes overwritten.

    Three refusals before any writing, because this function is reachable with a string that
    came out of the database, and a path from a database is attacker-influenced input in
    exactly the same way a URL is:

    * the **basename** is what gets used, so ``../../.env`` cannot name a file outside the
      directory being swept;
    * the resolved parent must *be* that directory, which catches a symlinked directory;
    * a symlink at the target is refused rather than followed, since following one would
      overwrite whatever it points at while reporting the sweep's own directory.

    The overwrite is random rather than zeros: zeros and a known pattern are just as
    detectable as the original on a medium that retains either. Whether the old blocks are
    really gone is the storage stack's business, not this function's — see the module
    docstring, which says so rather than implying a guarantee.
    """
    real_directory = os.path.realpath(directory)
    candidate = os.path.join(directory, os.path.basename(path))
    if os.path.islink(candidate):
        raise ValueError(f"refusing to wipe a symlink: {os.path.basename(candidate)}")
    target = os.path.realpath(candidate)
    if os.path.dirname(target) != real_directory:
        raise ValueError(f"refusing to wipe outside {real_directory}: {os.path.basename(candidate)}")

    size = os.path.getsize(target)
    with open(target, "r+b", buffering=0) as handle:
        remaining = size
        while remaining > 0:
            chunk = min(WIPE_CHUNK_BYTES, remaining)
            handle.write(os.urandom(chunk))
            remaining -= chunk
        handle.flush()
        os.fsync(handle.fileno())
    os.remove(target)
    _fsync_directory(real_directory)
    return size


def secure_remove(name: str, *, directory: str, dry_run: bool) -> int:
    """Wipe one named file if it is there. Returns bytes; ``0`` when there was nothing.

    The name is taken as a basename here so every caller cannot forget it, and a wipe that
    fails is *counted and reported*, never swallowed: a face that could not be removed is the
    one fact an operator has to see.
    """
    candidate = os.path.join(directory, os.path.basename(name))
    if not os.path.exists(candidate):
        return 0
    if dry_run:
        return os.path.getsize(candidate)
    return wipe_file(candidate, directory=directory)


def _directory_files(directory: str, suffixes: tuple[str, ...]) -> list[str]:
    """Names in ``directory`` ending in one of ``suffixes``. Missing directory -> none."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if name.endswith(suffixes))


def _mtime(path: str) -> float:
    """An mtime, or ``0`` for a file that vanished between listing and stat.

    ``0`` means "older than any cutoff", which would make a vanished file eligible for a wipe
    that then does nothing (``secure_remove`` returns 0 for a missing file). That is the safe
    direction: the alternative is treating an unreadable file as if it had just been written
    and leaving it forever.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
def _blank() -> dict[str, Any]:
    """One target's result. Uniform so the report is a contract, not a set of special cases."""
    return {
        "matched": 0,          # rows or files the cutoff selected
        "deleted": 0,          # rows removed / files unlinked
        "bytes": 0,            # bytes overwritten
        "references": 0,       # database references cleared because their file went
        "deferred": 0,         # items past the window that the cap left for the next sweep
        "skipped": None,       # why a target did nothing, when that needs saying
        "digest": None,        # fingerprint of the exact set removed
        "listed": [],          # a few of the names, for a human reading the report
        "failures": [],        # (name, error) pairs, never swallowed
    }


def _fail(result: dict[str, Any], name: str, error: Exception) -> None:
    result["failures"].append({"name": os.path.basename(name), "error": f"{type(error).__name__}: {error}"})


def _target(
    conn: sqlite3.Connection | None,
    name: str,
    function,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Run one target inside its own SAVEPOINT; a failure affects only this target.

    Without the savepoint, a single failing statement leaves the transaction unusable and the
    whole sweep rolls back — so the one target that cannot run would take the twelve that can
    down with it, every night, forever.
    """
    if conn is None:
        return {"error": "no database connection", **_blank()}
    try:
        conn.execute(f"SAVEPOINT retention_{name}")
    except sqlite3.Error as exc:  # pragma: no cover - a database that cannot savepoint
        return {"error": f"{type(exc).__name__}: {exc}", **_blank()}
    try:
        result = function(conn, dry_run=dry_run)
        conn.execute(f"RELEASE retention_{name}")
        return result
    except Exception as exc:  # noqa: BLE001 - one target must never cancel the others
        log.warning("retention target %s failed: %s", name, exc)
        try:
            conn.execute(f"ROLLBACK TO retention_{name}")
            conn.execute(f"RELEASE retention_{name}")
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
        return {"error": f"{type(exc).__name__}: {exc}", **_blank()}


# ---------------------------------------------------------------------------
# target: raw punch selfies (quick clock links)
# ---------------------------------------------------------------------------
def _sweep_punch_photos(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    """Wipe punch selfies past the window, and clear the rows that pointed at them.

    Two passes, because they answer different questions. The first is the *policy*: rows older
    than the cutoff whose ``photo_path`` is set. The second is the *residue*: files in the
    directory that no row claims and that are older than the window — the half of a
    wipe-then-clear that a crash can interrupt, plus anything an older version left behind.
    Without the second pass a failed reference update would leave a face on disk with nothing
    in the database able to name it, which is the one state this module exists to prevent.
    """
    import quick_links

    result = _blank()
    cutoff = policy().cutoff(policy().punch_photo_days)
    directory = str(quick_links.PHOTOS_DIR)
    if cutoff is None:
        result["skipped"] = "punch photo retention is 0 (keep every selfie)"
        return result

    rows = conn.execute(
        "SELECT id, photo_path FROM quick_link_uses "
        "WHERE created_at < ? AND photo_path IS NOT NULL AND photo_path <> '' "
        f"ORDER BY id{_cap_sql(policy().cap)}",
        (cutoff,),
    ).fetchall()
    result["matched"] = len(rows)
    removed: list[str] = []
    claimed: set[str] = set()
    for row in rows:
        name = os.path.basename(str(row["photo_path"]))
        try:
            size = secure_remove(name, directory=directory, dry_run=dry_run)
        except OSError as exc:
            _fail(result, name, exc)
            continue
        # A size of zero means the file was already gone (a crash between wipe and clear, or an
        # operator's ``rm``). The row is still cleared either way: a reference to a file that
        # no longer exists is not a record, it is a broken link, and leaving it makes every
        # later sweep report the same row again.
        result["bytes"] += size
        removed.append(name)
        claimed.add(name)
        if not dry_run:
            conn.execute("UPDATE quick_link_uses SET photo_path = NULL WHERE id = ?", (row["id"],))
        result["references"] += 1

    # Residue: files no row points at, older than the same window.
    referenced = {
        os.path.basename(str(row["photo_path"]))
        for row in conn.execute(
            "SELECT photo_path FROM quick_link_uses WHERE photo_path IS NOT NULL AND photo_path <> ''"
        ).fetchall()
    }
    epoch_cutoff = policy().cutoff_epoch(policy().punch_photo_days)
    for name in _directory_files(directory, (".jpg", ".jpeg", ".png")):
        if name in referenced or name in claimed:
            continue
        if epoch_cutoff is not None and _mtime(os.path.join(directory, name)) >= epoch_cutoff:
            continue
        try:
            size = secure_remove(name, directory=directory, dry_run=dry_run)
        except OSError as exc:
            _fail(result, name, exc)
            continue
        result["bytes"] += size
        removed.append(name)

    result["digest"] = digest(removed)
    result["listed"] = _listed(removed)
    # ``deleted`` counts what was actually removed, so a dry run reports zero of them however
    # many it matched - the same meaning the column has in the run row and the compliance
    # event. ``matched`` (and the listed names) is where a dry run says what *would* go.
    result["deleted"] = 0 if dry_run else len(removed)
    return result


# ---------------------------------------------------------------------------
# target: biometric templates and reference selfies
# ---------------------------------------------------------------------------
def _sweep_biometric_files(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    """Remove biometric files that belong to nobody, or to an account that was deactivated.

    Three classes, and the distinction between them is a person's rights:

    * **a deactivated account's files** go immediately, whatever their age. Deactivation is
      the moment the relationship ends and ``main.deactivate_user`` already deletes them; this
      is the retry for when that could not finish, so it must not wait a week to try again.
    * **orphans** — a template or selfie whose account id and biometric id match no row at all
      — wait out the window first. Their owner is unknown, so there is no event to key off,
      and a file that appeared seconds ago may be the other half of an enrollment in flight.
    * **half-written files** (``*.tmp``, the atomic-write staging names) are never legitimate
      at rest; they wait the same window and then go.

    A file named after an *active* account — including under the legacy account-id naming that
    ``biometrics`` still falls back to — is never touched. Neither is a ``.unclaimed``
    quarantine file newer than the window: it is somebody's face, and the module that
    quarantined it said explicitly that deleting it is a human's decision. Past the window the
    decision is made for them, because "we kept it forever in case" is not a policy.
    """
    result = _blank()
    policy_now = policy()
    window = int(policy_now.biometric_days)
    if window <= 0:
        # Zero means keep forever *for the residue pass*, and deactivation is unaffected by it:
        # ``main.deactivate_user`` still deletes the face at the moment the account is closed,
        # whatever this is set to. What zero turns off is the safety net underneath that.
        result["skipped"] = "biometric residue is kept forever (retention_biometric_days is 0)"
        return result
    epoch_cutoff = policy_now.cutoff_epoch(window)
    refs_dir, photos_dir = biometrics.directories()

    users = conn.execute("SELECT id, status, biometric_id FROM users").fetchall()
    live: set[str] = set()
    deactivated: set[str] = set()
    for row in users:
        names = {str(row["id"])}
        identifier = biometrics.id_from(row)
        if identifier:
            names.add(identifier)
        if str(row["status"] or "active").strip().lower() == "active":
            live |= names
        else:
            deactivated |= names

    removed: list[str] = []
    for directory, suffixes in ((refs_dir, (".json",)), (photos_dir, (".jpg",))):
        for name in _directory_files(directory, suffixes):
            stem = os.path.splitext(name)[0]
            path = os.path.join(directory, name)
            if stem in live:
                continue
            if stem not in deactivated:
                # Unknown owner: only eligible once the window has passed, so an enrollment
                # that is being written right now cannot be swept out from under itself.
                if epoch_cutoff is not None and _mtime(path) >= epoch_cutoff:
                    continue
            result["matched"] += 1
            try:
                result["bytes"] += secure_remove(name, directory=directory, dry_run=dry_run)
            except OSError as exc:
                _fail(result, name, exc)
                continue
            removed.append(name)

        # Quarantined legacy files: ``<something>.unclaimed-<hex>``. Nothing reads these - the
        # suffix exists precisely so that no reader can - and they are still somebody's face,
        # which is why they are swept on the window rather than at the moment they are moved.
        for name in _names_containing(directory, biometrics.QUARANTINE_SUFFIX + "-"):
            if epoch_cutoff is not None and _mtime(os.path.join(directory, name)) >= epoch_cutoff:
                continue
            result["matched"] += 1
            try:
                result["bytes"] += secure_remove(name, directory=directory, dry_run=dry_run)
            except OSError as exc:
                _fail(result, name, exc)
                continue
            removed.append(name)

        # Staging files from an interrupted ``biometrics.write_reference``.
        for name in _directory_files(directory, (".tmp",)):
            if epoch_cutoff is not None and _mtime(os.path.join(directory, name)) >= epoch_cutoff:
                continue
            result["matched"] += 1
            try:
                result["bytes"] += secure_remove(name, directory=directory, dry_run=dry_run)
            except OSError as exc:
                _fail(result, name, exc)
                continue
            removed.append(name)

    result["digest"] = digest(removed)
    result["listed"] = _listed(removed)
    result["accounts_deactivated"] = len(deactivated)
    result["deleted"] = 0 if dry_run else len(removed)
    return result


def _names_containing(directory: str, fragment: str) -> list[str]:
    """Files whose name contains ``fragment``. Quarantine names are not suffix-clean."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if fragment in name)


# ---------------------------------------------------------------------------
# target: audit rows
# ---------------------------------------------------------------------------
def _sweep_audit_log(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    """Delete audit rows past the window, having first taken the app's own guard off.

    ``audit_log`` is append-only and the *database* enforces it: migration 1 installs a
    ``BEFORE DELETE`` trigger that aborts every delete, precisely so that no bug, no injection
    and no over-eager endpoint can rewrite history. Retention is the single sanctioned
    exception, and it is sanctioned in the one way that stays auditable:

    * the guard is read out of ``sqlite_master`` and restored from that same text, so what is
      re-created is provably what was removed — not a second copy of the statement that could
      drift from the migration;
    * the drop, the delete and the restore happen in **one transaction** (SQLite DDL is
      transactional), so a crash leaves the guard in place and the rows undeleted;
    * nothing in the HTTP application can reach this path — it needs a database handle that
      this module only ever opens itself;
    * the row count, the cutoff, the id range and a digest of the deleted rows are written
      into the compliance event (see :func:`compliance_event`).

    A missing guard is reported rather than silently treated as normal: it means the database
    has drifted from the schema, and ``schema_guard`` restores it at the next boot.
    """
    result = _blank()
    cap = policy().cap
    cutoff = policy().cutoff(policy().audit_days)
    if cutoff is None:
        result["skipped"] = "audit retention is 0 (keep every audit row)"
        return result

    # The same ``WHERE … ORDER BY id LIMIT n`` drives the digest, the delete and the count of
    # what is left over, so the event's fingerprint names exactly the rows the statement
    # removes - a cap applied to one of the three would make them disagree.
    eligible = "FROM audit_log WHERE created_at < ? ORDER BY id"
    rows = conn.execute(
        f"SELECT id, action, actor_id, created_at {eligible}{_cap_sql(cap)}", (cutoff,)
    ).fetchall()
    result["matched"] = len(rows)
    result["listed"] = _listed([f"#{row['id']} {row['action']}" for row in rows])
    result["digest"] = digest(
        f"{row['id']}|{row['created_at']}|{row['action']}|{row['actor_id'] or ''}" for row in rows
    )
    if rows:
        result["oldest"] = str(rows[0]["created_at"])
        result["newest"] = str(rows[-1]["created_at"])
    total_eligible = int(
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE created_at < ?", (cutoff,)).fetchone()[0]
    )
    result["deferred"] = max(0, total_eligible - len(rows))
    if dry_run or not rows:
        return result

    guard = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (AUDIT_DELETE_GUARD,)
    ).fetchone()
    guard_sql = str(guard["sql"]) if guard is not None and guard["sql"] else None
    result["guard_was_missing"] = guard_sql is None
    if guard_sql:
        conn.execute(f"DROP TRIGGER IF EXISTS {AUDIT_DELETE_GUARD}")
    try:
        result["deleted"] = int(
            conn.execute(
                f"DELETE FROM audit_log WHERE id IN (SELECT id {eligible}{_cap_sql(cap)})",
                (cutoff,),
            ).rowcount
            or 0
        )
    finally:
        if guard_sql:
            conn.execute(guard_sql)
    return result


# ---------------------------------------------------------------------------
# target: read notifications
# ---------------------------------------------------------------------------
def _sweep_notifications(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    """Drop *read* notifications past the window. Unread ones are reported, never deleted.

    An unread notification is a task somebody has not done — a late arrival nobody has
    reviewed, a liveness flag nobody has triaged. Age does not make it done, so retention has
    no business removing it; what it can do is say how many are sitting there, which is the
    number an administrator actually needs.
    """
    result = _blank()
    days = int(policy().notification_days)
    cutoff = policy().cutoff(days)
    if cutoff is None:
        result["skipped"] = "notification retention is 0 (keep every notification)"
        return result

    # The delete is spelled out here rather than reached through a helper so that the rows the
    # digest names and the rows the statement removes are selected by the *same* cutoff. A
    # helper computing its own "now" would make the event's fingerprint of the deletion
    # something an auditor could only approximately verify against.
    cap = policy().cap
    eligible = "FROM admin_notifications WHERE read_at IS NOT NULL AND created_at < ? ORDER BY id"
    rows = conn.execute(f"SELECT id {eligible}{_cap_sql(cap)}", (cutoff,)).fetchall()
    result["matched"] = len(rows)
    result["listed"] = _listed([f"#{row['id']}" for row in rows])
    result["digest"] = digest(str(row["id"]) for row in rows)
    result["stale_unread"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM admin_notifications WHERE read_at IS NULL AND created_at < ?",
            (cutoff,),
        ).fetchone()[0]
    )
    total_eligible = int(
        conn.execute(
            "SELECT COUNT(*) FROM admin_notifications WHERE read_at IS NOT NULL AND created_at < ?",
            (cutoff,),
        ).fetchone()[0]
    )
    result["deferred"] = max(0, total_eligible - len(rows))
    if dry_run or not rows:
        return result
    result["deleted"] = int(
        conn.execute(
            f"DELETE FROM admin_notifications WHERE id IN (SELECT id {eligible}{_cap_sql(cap)})",
            (cutoff,),
        ).rowcount
        or 0
    )
    return result


# ---------------------------------------------------------------------------
# target: raw offline punches and replay anchors
# ---------------------------------------------------------------------------
def _sweep_punch_queue(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    """Drop queued punches that are already accounted for, and stale replay anchors.

    A ``punch_queue`` row is a raw client punch: coordinates, an accuracy figure, a photo hash
    and a device id. It is also redundant the moment the punch has an ``attendance_logs`` row
    (``materialized_log_id``) or was refused, so past the window the queue row is pure
    location history and goes.

    The criterion is deliberately *not* "old enough". A row that is neither materialized nor
    rejected is a punch that has no record anywhere else, and deleting it would erase an
    arrival rather than a duplicate; those are counted and left for a human.
    """
    result = _blank()
    cutoff = policy().cutoff(policy().punch_queue_days)
    anchors_cutoff = policy().cutoff(policy().anchor_days)
    cap = policy().cap
    if cutoff is not None:
        eligible = (
            "FROM punch_queue WHERE received_at < ? "
            "AND (materialized_log_id IS NOT NULL OR status = 'rejected') ORDER BY id"
        )
        rows = conn.execute(f"SELECT id {eligible}{_cap_sql(cap)}", (cutoff,)).fetchall()
        result["matched"] = len(rows)
        result["digest"] = digest(str(row["id"]) for row in rows)
        result["retained_unaccounted"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM punch_queue WHERE received_at < ? "
                "AND materialized_log_id IS NULL AND status != 'rejected'",
                (cutoff,),
            ).fetchone()[0]
        )
        total_eligible = int(
            conn.execute(
                "SELECT COUNT(*) FROM punch_queue WHERE received_at < ? "
                "AND (materialized_log_id IS NOT NULL OR status = 'rejected')",
                (cutoff,),
            ).fetchone()[0]
        )
        result["deferred"] = max(0, total_eligible - len(rows))
        if not dry_run and rows:
            result["deleted"] = int(
                conn.execute(
                    f"DELETE FROM punch_queue WHERE id IN (SELECT id {eligible}{_cap_sql(cap)})",
                    (cutoff,),
                ).rowcount
                or 0
            )
    else:
        result["skipped"] = "punch queue retention is 0 (keep every queued punch)"

    # Anchors are replay protection, not a record. An anchor older than the sync window can
    # only ever refuse a punch, and ``offline_sync`` already refuses those on age alone.
    if anchors_cutoff is not None:
        anchors = conn.execute(
            f"SELECT id FROM device_anchors WHERE server_time < ? ORDER BY id{_cap_sql(cap)}",
            (anchors_cutoff,),
        ).fetchall()
        result["anchors_matched"] = len(anchors)
        if not dry_run and anchors:
            result["anchors_deleted"] = int(
                conn.execute(
                    f"DELETE FROM device_anchors WHERE id IN "
                    f"(SELECT id FROM device_anchors WHERE server_time < ? ORDER BY id{_cap_sql(cap)})",
                    (anchors_cutoff,),
                ).rowcount
                or 0
            )
    return result


# ---------------------------------------------------------------------------
# target: report only
# ---------------------------------------------------------------------------
def _report_attendance(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:  # noqa: ARG001
    """Describe the hours record without touching it. See the module docstring.

    Reported rather than pruned because these rows are pay. What an auditor needs is not for
    them to be deleted but to know how far back the system can account for, which is what this
    returns: the range, the volume, and how many rows are past the audit horizon.
    """
    result = _blank()
    horizon = policy().cutoff(policy().audit_days)
    row = conn.execute(
        "SELECT COUNT(*) AS rows_total, MIN(timestamp) AS oldest, MAX(timestamp) AS newest FROM attendance_logs"
    ).fetchone()
    result["rows_total"] = int(row["rows_total"] or 0)
    result["oldest"] = row["oldest"]
    result["newest"] = row["newest"]
    result["past_audit_horizon"] = (
        int(conn.execute("SELECT COUNT(*) FROM attendance_logs WHERE timestamp < ?", (horizon,)).fetchone()[0])
        if horizon is not None
        else 0
    )
    result["skipped"] = "attendance_logs is never deleted automatically (hours worked are pay records)"
    return result


TARGETS = (
    ("punch_photos", _sweep_punch_photos),
    ("biometric_files", _sweep_biometric_files),
    ("audit_log", _sweep_audit_log),
    ("notifications", _sweep_notifications),
    ("punch_queue", _sweep_punch_queue),
    ("attendance_logs", _report_attendance),
)

#: The targets that appear in the compliance event's digest of "what this sweep removed".
PRUNING_TARGETS = tuple(name for name, _ in TARGETS if name != "attendance_logs")


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------
def _database_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Free pages and page size: how much of the file is rows that no longer exist.

    Reported because a retention policy that claims rows are gone while their bytes sit in the
    file is misleading — and because it is the number that tells an operator whether a
    ``VACUUM`` (which needs an exclusive lock) is worth scheduling.
    """
    stats: dict[str, Any] = {}
    try:
        stats["freelist_pages"] = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        stats["page_size"] = int(conn.execute("PRAGMA page_size").fetchone()[0])
        stats["freelist_bytes"] = stats["freelist_pages"] * stats["page_size"]
    except sqlite3.Error:  # pragma: no cover - defensive
        pass
    return stats


def sweep(
    *,
    dry_run: bool | None = None,
    actor: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Run every retention target once. Returns the report; applies changes unless dry-run.

    ``dry_run=None`` means "use the configured default" (``RETENTION_DRY_RUN``), and the
    default is off — a policy that only ever runs when somebody remembers to pass ``--apply``
    is the same aspiration this module replaced. Every deployment starts making the operator
    read the first run: ``python -m retention`` prints this report without touching anything,
    and the scheduled sweep logs what it did.

    Passing ``conn`` runs inside a caller's transaction (the tests do this). Otherwise the
    sweep opens its own connection — **read-only** when it is a dry run, so a dry run is
    incapable of writing rather than merely instructed not to.
    """
    if dry_run is None:
        dry_run = bool(settings.retention_dry_run)
    started = _now()
    current = policy()
    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "started_at": _stamp(started),
        "policy": current.as_dict(),
        "actor": actor or _default_actor(),
        "targets": {},
    }
    cutoffs = {
        "punch_photos": current.cutoff(current.punch_photo_days, now=started),
        "biometric_files": current.cutoff(current.biometric_days, now=started),
        "audit_log": current.cutoff(current.audit_days, now=started),
        "notifications": current.cutoff(current.notification_days, now=started),
        "punch_queue": current.cutoff(current.punch_queue_days, now=started),
        "device_anchors": current.cutoff(current.anchor_days, now=started),
    }
    report["cutoffs"] = cutoffs

    own_connection = conn is None
    handle: sqlite3.Connection | None = conn
    try:
        if own_connection and dry_run:
            handle = database.connect(read_only=True)
        elif own_connection:
            # ``isolation_level=None``: this sweep manages its own BEGIN/COMMIT, the same way
            # ``migrations.run_migrations`` does. The alternative was measured and rejected —
            # ``sqlite3`` only opens an implicit transaction for DML, so the ``DROP TRIGGER``
            # on ``audit_log`` would autocommit and a crash between the drop and the restore
            # would leave the append-only guard off until the next boot repaired it.
            handle = database.connect(isolation_level=None)
            handle.execute("BEGIN IMMEDIATE")

        for name, function in TARGETS:
            report["targets"][name] = _target(handle, name, function, dry_run=bool(dry_run))

        if handle is not None:
            report["database"] = _database_stats(handle)

        # Totalled *before* the compliance event is written, because the event quotes them: an
        # event that carried a null total would be a record of a deletion that cannot be
        # accounted for, which is worse than no record at all.
        summarise(report)
        report["finished_at"] = _stamp(_now())

        if not dry_run and handle is not None:
            _finish(report, handle, started=started)
            if own_connection:
                handle.execute("COMMIT")
        elif own_connection and handle is not None:
            handle.rollback()
    except Exception as exc:  # noqa: BLE001 - a sweep that cannot start is reported, not fatal
        if own_connection and handle is not None and not dry_run:
            try:
                handle.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["deleted_total"] = 0
        report["bytes_total"] = 0
        # Counted as a failure so the exit status, the notification and the readiness check all
        # agree that this sweep did not do what it was supposed to. A timer that raises instead
        # would kill the sweeper thread, and nobody would hear from retention again.
        summarise(report)
        report["failures_total"] = int(report["failures_total"]) + 1
        report["finished_at"] = _stamp(_now())
        log.warning("retention sweep failed: %s", exc)
        return report
    finally:
        if own_connection and handle is not None:
            handle.close()

    report["finished_at"] = _stamp(_now())
    summarise(report)
    return report


def summarise(report: dict[str, Any]) -> dict[str, Any]:
    """Fill in the totals and the cross-target digest. Idempotent, and pure arithmetic.

    Its own function because the values are needed twice: once as the report an operator reads,
    and once inside the compliance event, which is written while the sweep is still holding the
    transaction open. Computing them in two places is how the event and the report end up
    disagreeing about what was deleted.
    """
    targets = report.get("targets", {})
    report["deleted_total"] = sum(int(target.get("deleted", 0) or 0) for target in targets.values())
    report["bytes_total"] = sum(int(target.get("bytes", 0) or 0) for target in targets.values())
    # Two kinds of failure, counted together because an operator cares about one number: a file
    # that could not be wiped, and a target that could not run at all. A target that raised has
    # an ``error`` and no ``failures`` list, and reporting that sweep as healthy would be the
    # exact silent failure this whole module is written against.
    report["targets_failed"] = sorted(name for name, target in targets.items() if target.get("error"))
    report["failures_total"] = sum(len(target.get("failures", [])) for target in targets.values()) + len(
        report["targets_failed"]
    )
    # ``.get`` rather than indexing: this is also called on a sweep that never got as far as
    # running any target (the connection could not be opened), and an exception raised from
    # inside the handler that is *reporting* a failure would be the worst possible outcome.
    report["compliance_digest"] = digest(
        f"{name}:{(targets.get(name) or {}).get('digest') or ''}" for name in PRUNING_TARGETS
    )
    return report


def _default_actor() -> str:
    """Who to credit a sweep to when nobody said.

    A scheduled run is the system; an operator running ``--apply`` by hand can name themselves
    with ``RETENTION_ACTOR``, because "the system deleted 1 400 audit rows" and "A. Engineer
    deleted 1 400 audit rows" are different sentences in an investigation.
    """
    return str(settings.retention_actor or "system:retention-sweeper")


def _finish(report: dict[str, Any], conn: sqlite3.Connection, *, started: datetime) -> None:
    """Write the compliance event and the run row. Both inside the sweep's transaction.

    The order matters: the compliance event is appended *after* the audit prune, so the event
    describing the deletion is not itself deleted by it, and so an auditor reading the table
    sees the deletion and its record together — or neither, if the sweep rolled back.
    """
    event_id = compliance_event(conn, report)
    report["compliance_event_id"] = event_id
    report["run_id"] = _record_run(conn, report, started=started)
    _notify(conn, report)


def compliance_event(conn: sqlite3.Connection, report: dict[str, Any]) -> int | None:
    """Append the retention event to ``audit_log``. Returns its id, or ``None``.

    Written straight into the table rather than through ``main._audit`` because there is no
    ``CurrentUser`` behind a timer, and because the shape here is the point: the policy, the
    cutoffs, the counts, the byte volume, the digests of what went, and the failures. It is
    the evidence an auditor asks for, and it is written by the same transaction that did the
    deleting, so it cannot describe a sweep that did not happen.
    """
    import json

    after = {
        "dry_run": report.get("dry_run"),
        "policy": report.get("policy"),
        "cutoffs": report.get("cutoffs"),
        "targets": {
            name: {
                key: value
                for key, value in target.items()
                if key in {"matched", "deleted", "bytes", "references", "skipped", "digest", "failures", "listed"}
            }
            for name, target in report.get("targets", {}).items()
        },
        "deleted_total": report.get("deleted_total"),
        "bytes_total": report.get("bytes_total"),
        "failures_total": report.get("failures_total"),
        "targets_failed": report.get("targets_failed"),
        "database": report.get("database"),
        "compliance_digest": report.get("compliance_digest"),
    }
    try:
        cursor = conn.execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, entity, entity_id, after_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                None,
                "system",
                "retention_sweep",
                "retention",
                _stamp(started_of(report)),
                json.dumps(after, default=str, sort_keys=True),
                _stamp(started_of(report)),
            ),
        )
        return int(cursor.lastrowid)
    except sqlite3.Error as exc:  # pragma: no cover - audit_log is there by migration 1
        log.warning("retention compliance event could not be written: %s", exc)
        return None


def started_of(report: dict[str, Any]) -> datetime:
    """The sweep's start time as a datetime, for the from-audit helpers that take one."""
    try:
        return datetime.strptime(str(report.get("started_at")), TS)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return _now()


def _record_run(conn: sqlite3.Connection, report: dict[str, Any], *, started: datetime) -> int | None:
    """One row per applied sweep, so readiness can answer "when did this last actually run?"."""
    import json

    try:
        cursor = conn.execute(
            "INSERT INTO retention_runs "
            "(started_at, finished_at, dry_run, actor, policy_json, summary_json, "
            " deleted_total, bytes_wiped, failures_total, digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.get("started_at"),
                report.get("finished_at") or _stamp(_now()),
                0,
                report.get("actor"),
                json.dumps(report.get("policy"), sort_keys=True),
                json.dumps(report.get("targets"), default=str, sort_keys=True),
                int(report.get("deleted_total", 0) or 0),
                int(report.get("bytes_total", 0) or 0),
                int(report.get("failures_total", 0) or 0),
                report.get("compliance_digest"),
            ),
        )
        return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        log.warning("retention run could not be recorded: %s", exc)
        return None


def _notify(conn: sqlite3.Connection, report: dict[str, Any]) -> None:
    """Tell an administrator, once a day, that data was erased — and immediately if it failed.

    Deduplicated per day so a six-hourly sweep does not turn the dashboard into a log, but a
    *failure* is never deduplicated: a file that could not be wiped is exactly what somebody
    has to act on, and suppressing the second report of it would be the wrong kind of quiet.
    """
    failures = int(report.get("failures_total", 0) or 0)
    deleted = int(report.get("deleted_total", 0) or 0)
    if not failures and not deleted:
        return
    parts = [
        f"{name}: {target.get('deleted', 0)} removed"
        for name, target in report.get("targets", {}).items()
        if target.get("deleted")
    ]
    notifications.notify(
        conn,
        kind=notifications.KIND_RETENTION_SWEEP,
        severity=notifications.SEVERITY_WARNING if failures else notifications.SEVERITY_INFO,
        title=(
            f"Retention sweep: {deleted} item(s) erased, {failures} failure(s)"
            if failures
            else f"Retention sweep: {deleted} item(s) erased"
        ),
        body=(
            "; ".join(parts)
            + (f" | could not remove: {failures} (see the audit event)" if failures else "")
            + f" | {report.get('bytes_total', 0)} bytes overwritten"
        ),
        payload={
            "policy": report.get("policy"),
            "cutoffs": report.get("cutoffs"),
            "compliance_event_id": report.get("compliance_event_id"),
        },
        dedupe_key=None if failures else f"retention:{datetime.now().strftime('%Y-%m-%d')}",
    )


def last_run(*, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """The most recent applied sweep, or ``None``. For readiness and the admin endpoint."""
    import json

    own = conn is None
    handle = conn
    try:
        if own:
            handle = database.connect(read_only=True)
        try:
            row = handle.execute(
                "SELECT * FROM retention_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "actor": row["actor"],
            "deleted_total": row["deleted_total"],
            "bytes_wiped": row["bytes_wiped"],
            "failures_total": row["failures_total"],
            "digest": row["digest"],
            "policy": json.loads(row["policy_json"]) if row["policy_json"] else None,
            "targets": json.loads(row["summary_json"]) if row["summary_json"] else None,
        }
    finally:
        if own and handle is not None:
            handle.close()


def residue() -> dict[str, Any]:
    """Files that retention *should* have removed and that are still on disk. For readiness.

    Counted from the filesystem, not from the database, and that is the whole point: a face the
    database no longer knows about is exactly the case a query cannot find. Read-only, so a
    readiness check cannot delete anything.
    """
    import quick_links

    current = policy()
    epoch_cutoff = current.cutoff_epoch(current.biometric_days)
    deactivated: set[str] = set()
    live: set[str] = set()
    try:
        with database.db() as conn:
            for row in conn.execute("SELECT id, status, biometric_id FROM users").fetchall():
                names = {str(row["id"])}
                identifier = biometrics.id_from(row)
                if identifier:
                    names.add(identifier)
                (live if str(row["status"] or "active").strip().lower() == "active" else deactivated).update(names)
    except sqlite3.Error as exc:  # pragma: no cover - the database is not there
        return {"error": f"{type(exc).__name__}: {exc}"}

    refs_dir, photos_dir = biometrics.directories()
    owned_by_a_deactivated_account: list[str] = []
    orphaned: list[str] = []
    staging: list[str] = []
    for directory, suffixes in ((refs_dir, (".json",)), (photos_dir, (".jpg",))):
        for name in _directory_files(directory, suffixes):
            stem = os.path.splitext(name)[0]
            if stem in live:
                continue
            if stem in deactivated:
                owned_by_a_deactivated_account.append(name)
            elif epoch_cutoff is None or _mtime(os.path.join(directory, name)) < epoch_cutoff:
                orphaned.append(name)
        for name in _directory_files(directory, (".tmp",)):
            # Aged like everything else, and for once the reason is not the owner but the
            # process: a ``.tmp`` written seconds ago belongs to an enrollment that is being
            # written right now, and counting that as residue would make this check cry wolf
            # every time somebody uploads a photo.
            if epoch_cutoff is not None and _mtime(os.path.join(directory, name)) >= epoch_cutoff:
                continue
            staging.append(name)
    for directory in (refs_dir, photos_dir):
        for name in _names_containing(directory, biometrics.QUARANTINE_SUFFIX + "-"):
            if epoch_cutoff is None or _mtime(os.path.join(directory, name)) < epoch_cutoff:
                orphaned.append(name)

    punch_dir = str(quick_links.PHOTOS_DIR)
    referenced = set()
    try:
        with database.db() as conn:
            referenced = {
                os.path.basename(str(row["photo_path"]))
                for row in conn.execute(
                    "SELECT photo_path FROM quick_link_uses WHERE photo_path IS NOT NULL AND photo_path <> ''"
                ).fetchall()
            }
    except sqlite3.Error:  # pragma: no cover - the database is not there
        pass
    punch_photo_cutoff = current.cutoff_epoch(current.punch_photo_days)
    punch_orphans = [
        name
        for name in _directory_files(punch_dir, (".jpg", ".jpeg", ".png"))
        if name not in referenced
        and (punch_photo_cutoff is None or _mtime(os.path.join(punch_dir, name)) < punch_photo_cutoff)
    ]

    return {
        "biometric_files_for_deactivated_accounts": len(owned_by_a_deactivated_account),
        "orphaned_biometric_files": len(orphaned),
        "biometric_staging_files": len(staging),
        "orphaned_punch_photos": len(punch_orphans),
        "listed": _listed(sorted(owned_by_a_deactivated_account + orphaned + punch_orphans)),
    }


# ---------------------------------------------------------------------------
# the scheduled sweep
# ---------------------------------------------------------------------------
_stop_event = threading.Event()
_thread: threading.Thread | None = None
LAST_REPORT: dict[str, Any] | None = None


def _loop(interval: int, initial_delay: int) -> None:
    """Timer body: sweeps once after startup, then on the interval.

    The initial delay is not politeness. A sweep at boot competes with the model preload, the
    schema guard and the first shift's punches for the same CPU and the same SQLite write
    lock, and the work it does is not urgent by hours. Five minutes in, the site is running.
    """
    global LAST_REPORT
    if _stop_event.wait(max(0, int(initial_delay))):
        return
    while not _stop_event.is_set():
        try:
            LAST_REPORT = sweep()
            log.info(
                "retention sweep removed %s item(s), %s failure(s)",
                LAST_REPORT.get("deleted_total"),
                LAST_REPORT.get("failures_total"),
            )
        except Exception as exc:  # pragma: no cover - the timer must survive a bad night
            log.warning("retention sweep failed: %s", exc)
        if _stop_event.wait(max(30, int(interval))):
            return


def start_watcher(*, interval: int | None = None, enabled: bool | None = None, initial_delay: int | None = None) -> bool:
    """Start the daemon sweeper. Returns whether it is running.

    A thread rather than an asyncio task, deliberately, and for the reason the whole module
    exists: the work is blocking file overwrites plus SQLite writes, so as an awaitable it
    would hold the event loop for the duration of the largest directory — stalling every punch
    at the gate while retention tidies up. ``main.lifespan`` starts it after the readiness gate
    has passed, exactly like the overtime watcher, and ``python -m retention`` is the
    cron/Celery-shaped entry point for a deployment that would rather schedule it externally.
    """
    global _thread
    on = settings.retention_enabled if enabled is None else enabled
    if not on:
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event.clear()
    seconds = int(interval if interval is not None else settings.retention_interval_seconds)
    delay = int(initial_delay if initial_delay is not None else settings.retention_initial_delay_seconds)
    _thread = threading.Thread(target=_loop, args=(seconds, delay), name="retention-sweeper", daemon=True)
    _thread.start()
    log.info("retention sweeper started (first sweep in %ss, then every %ss)", delay, seconds)
    return True


def stop_watcher(*, timeout: float = 5.0) -> None:
    """Signal the sweeper to stop. A sweep in flight finishes; the next one does not start.

    The timeout is a join, not a kill: Python cannot stop a thread mid-``os.remove``, and
    ``timeout=0`` in the tests just means "do not wait for it".
    """
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
        _thread = None


def watcher_running() -> bool:
    return _thread is not None and _thread.is_alive()


# ---------------------------------------------------------------------------
# operator entry point
# ---------------------------------------------------------------------------
def _print_report(report: dict[str, Any], *, as_json: bool) -> None:
    import json

    if as_json:
        print(json.dumps(report, indent=2, default=str, sort_keys=True))
        return
    mode = "DRY RUN - nothing was written and nothing was deleted" if report["dry_run"] else "APPLIED"
    print(f"Retention sweep ({mode})  started {report['started_at']}  actor {report['actor']}")
    print(f"  policy (days): {report['policy']}")
    print("  cutoffs:")
    for name, cutoff in report["cutoffs"].items():
        print(f"    {name}: {cutoff if cutoff else 'keep forever'}")
    print("  targets:")
    for name, target in report["targets"].items():
        line = (
            f"    {name}: matched {target.get('matched', 0)}, deleted {target.get('deleted', 0)}, "
            f"references cleared {target.get('references', 0)}, {target.get('bytes', 0)} bytes"
        )
        if target.get("skipped"):
            line += f" [{target['skipped']}]"
        print(line)
        for failure in target.get("failures", []):
            print(f"      ! {failure['name']}: {failure['error']}")
    print(f"  totals: {report.get('deleted_total', 0)} items, {report.get('bytes_total', 0)} bytes, "
          f"{report.get('failures_total', 0)} failures")
    if report.get("compliance_event_id"):
        print(f"  compliance event: audit_log #{report['compliance_event_id']} (digest {report.get('compliance_digest')})")
    if report["dry_run"]:
        print("  pass --apply to perform these deletions")


def main(argv: list[str] | None = None) -> int:
    """``python -m retention [--apply|--dry-run] [--json] [--policy] [--vacuum]``.

    Dry run is the default, and it is the *only* thing ``--dry-run`` and the default have in
    common: the dry run opens the database read-only so an accidental write fails loudly rather
    than quietly doing half a sweep. ``--apply`` is the deliberate act, and it is what a cron
    entry or a systemd timer should call.
    """
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(main.__doc__)
        return 0
    if "--policy" in argv:
        current = policy()
        for key, value in current.as_dict().items():
            print(f"{key}: {value}" + ("  (keep forever)" if value <= 0 else ""))
        return 0
    if "--dry-run" in argv and "--apply" in argv:
        print("--dry-run and --apply are contradictory; pick one", file=sys.stderr)
        return 2

    apply_now = "--apply" in argv and "--dry-run" not in argv
    as_json = "--json" in argv
    if "--vacuum" in argv:
        if not apply_now:
            print("--vacuum only makes sense with --apply", file=sys.stderr)
            return 2

    report = sweep(dry_run=not apply_now, actor=str(settings.retention_actor or "cli:retention"))
    if apply_now and "--vacuum" in argv:
        # Deliberate, exclusive, and never on the timer: VACUUM rewrites the whole file, so it
        # is the only operation here that can fail on a busy database, and it is the only way
        # to make deleted rows unreadable rather than merely unreferenced.
        try:
            connection = database.connect(isolation_level=None)
            try:
                connection.execute("VACUUM")
                report["vacuumed_at"] = _stamp(_now())
            finally:
                connection.close()
        except sqlite3.Error as exc:
            report["vacuum_error"] = f"{type(exc).__name__}: {exc}"
    _print_report(report, as_json=as_json)
    return 0 if report.get("failures_total", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
