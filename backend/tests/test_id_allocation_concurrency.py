"""Allocating an account id: the read and the write have to be one transaction.

WHY THIS EXISTS
---------------
``security.lowest_free_id`` answers "which account id is free right now", and it is a **read**.
A read on its own is a snapshot: two callers that both see "43 is free" both insert 43, and one
of them dies on the primary key. The thing that makes read-then-insert safe is the *caller's*
transaction - ``database.immediate()``, which takes SQLite's single write lock at ``BEGIN``
rather than at the first ``INSERT``, so the second caller cannot even read until the first has
committed and therefore sees the row the first one wrote.

That distinction is invisible in a single-threaded test, and it is the entire requirement, so
this file drives it from threads - and includes the **control** that shows what a deferred
connection does instead, because "it passed with threads" is not evidence that the lock is what
made it pass.

WHAT IS PINNED
--------------
1. **No number is ever handed out twice** under real contention, and every number is inside the
   role's range.
2. **The lock is taken at ``BEGIN``, not at the first write** - the property the allocator's
   docstring claims, stated as two observations: a plain read still succeeds while the write
   lock is held (which is exactly why a ``SELECT`` before an ``INSERT`` protects nothing), and a
   second ``BEGIN IMMEDIATE`` is refused outright.
3. **A full range is reported, not guessed at**: ``IdSpaceExhausted``, naming the range and what
   an operator can do about it.
4. **A number that was freed is handed out again.** This is why the allocator scans from the
   floor instead of counting upward: the worker space is 499 wide and deletions are normal, so
   numbering strictly upward would burn it permanently while the roster never grew.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

import database
import harness  # noqa: F401 - importing it is what redirects the database and the file trees
from security import ROLE_ID_RANGES, IdSpaceExhausted, lowest_free_id, validate_user_id_for_role

WORKER_LOW, WORKER_HIGH = ROLE_ID_RANGES["worker"]


def _insert(conn: sqlite3.Connection, user_id: str, role: str = "worker") -> None:
    """The smallest row that occupies a number. The columns the allocator reads, and no more."""
    conn.execute(
        "INSERT INTO users (id, name, password_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, f"Concurrent {user_id}", "x", role),
    )


def _taken(role: str) -> set[int]:
    low, high = ROLE_ID_RANGES[role]
    with database.db() as conn:
        if high is None:
            rows = conn.execute(
                "SELECT id FROM users WHERE CAST(id AS INTEGER) >= ?", (low,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM users WHERE CAST(id AS INTEGER) BETWEEN ? AND ?", (low, high)
            ).fetchall()
    return {int(row[0]) for row in rows}


def test_concurrent_allocations_never_hand_out_one_number_twice():
    """The requirement, under real contention: eight threads, six rounds, one number each."""
    threads_count = 8
    rounds = 6
    barrier = threading.Barrier(threads_count)
    handed: list[str] = []
    failures: list[str] = []
    guard = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        for round_index in range(rounds):
            try:
                with database.immediate() as conn:
                    new_id = lowest_free_id(conn, "worker")
                    _insert(conn, new_id)
                with guard:
                    handed.append(new_id)
            except Exception as exc:  # noqa: BLE001 - the failure is the assertion
                with guard:
                    failures.append(f"thread {index} round {round_index}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=run, args=(index,)) for index in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert not failures, failures
    assert len(handed) == threads_count * rounds, (
        f"{len(handed)} allocations completed out of {threads_count * rounds}; a thread that "
        "never finished is as much a failure as one that collided"
    )
    assert len(set(handed)) == len(handed), (
        f"one account id was handed out to two threads: {sorted(handed)}"
    )
    for value in handed:
        # Raises if the allocator ever crossed into another role's block.
        validate_user_id_for_role(value, "worker")


def test_the_write_lock_is_taken_at_begin_and_not_at_the_first_write():
    """The mechanism, not the outcome - and the control that makes the distinction visible.

    A deferred connection takes nothing at ``BEGIN``, so a reader is free to read the exact
    snapshot a concurrent writer is about to invalidate; that is the hazard the allocator's
    read-then-insert would have if its caller used ``db(write=True)``. ``BEGIN IMMEDIATE``
    refuses to start at all while another writer holds the lock, which is what turns the two
    statements into one decision.
    """
    started = threading.Event()
    release = threading.Event()
    holder_errors: list[Exception] = []
    held: dict[str, str] = {}

    def holder() -> None:
        try:
            with database.immediate() as conn:
                held["id"] = lowest_free_id(conn, "worker")
                _insert(conn, held["id"])
                started.set()
                release.wait(timeout=120)
        except Exception as exc:  # noqa: BLE001
            holder_errors.append(exc)
            started.set()

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(timeout=60), "the holding transaction never took the lock"
    try:
        # The control: a plain read is *not* blocked, which is precisely why the allocator
        # cannot rely on a read to reserve anything.
        with database.db() as reader:
            assert reader.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 0

        # ...and the same work inside BEGIN IMMEDIATE cannot proceed at all. The 50 ms timeout
        # is the test asking for the refusal rather than waiting ten seconds for it.
        with pytest.raises(sqlite3.OperationalError) as refused:
            with database.immediate(timeout=0.05) as contender:
                lowest_free_id(contender, "worker")
        assert "locked" in str(refused.value).lower(), (
            f"a second writer was refused with {refused.value!r} rather than 'database is "
            "locked'; the lock is not being taken at BEGIN"
        )
    finally:
        release.set()
        thread.join(timeout=120)

    assert not holder_errors, holder_errors
    # The holding thread's row survived its own commit, which is what the second caller would
    # have seen had it been allowed to read.
    with database.db() as conn:
        assert conn.execute("SELECT id FROM users WHERE id = ?", (held["id"],)).fetchone() is not None


def test_a_full_range_is_named_rather_than_guessed_at():
    """Every number in the block is spoken for: refuse, and say what to do about it."""
    taken = _taken("worker")
    with database.immediate() as conn:
        for value in range(WORKER_LOW, WORKER_HIGH + 1):
            if value not in taken:
                _insert(conn, str(value))

    with pytest.raises(IdSpaceExhausted) as exhausted:
        with database.immediate() as conn:
            lowest_free_id(conn, "worker")

    message = str(exhausted.value)
    assert f"{WORKER_LOW}-{WORKER_HIGH}" in message, (
        f"the refusal has to name the range that is full, and it says {message!r}"
    )
    assert "retire" in message, (
        "the refusal has to name what an operator can do, or it is a dead end"
    )


def test_a_number_that_was_freed_is_handed_out_again():
    """Deletion is normal, so the number comes back - the reason the scan starts at the floor."""
    with database.immediate() as conn:
        freed = lowest_free_id(conn, "worker")
        _insert(conn, freed)
        following = lowest_free_id(conn, "worker")
        assert following == str(int(freed) + 1)
        conn.execute("DELETE FROM users WHERE id = ?", (freed,))
        assert lowest_free_id(conn, "worker") == freed


@pytest.mark.parametrize("role", ["worker", "moallem", "admin", "head_admin"])
def test_each_role_is_handed_a_number_inside_its_own_block(role):
    """The ranges do not overlap, so a moallem can never be handed a worker's number."""
    with database.immediate() as conn:
        allocated = lowest_free_id(conn, role)
    validate_user_id_for_role(allocated, role)


def test_a_role_with_no_range_is_refused_rather_than_guessed():
    """A role the ranges do not describe gets an error, not the worker block."""
    with database.immediate() as conn:
        with pytest.raises(ValueError):
            lowest_free_id(conn, "nobody")


# ---------------------------------------------------------------------------
# reservations: an invite holds an id long before a ``users`` row exists
# ---------------------------------------------------------------------------
def _stamp(hours: int = 0) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_invite(conn: sqlite3.Connection, worker_id: str, *, role: str = "worker", **columns) -> int:
    """A minimal live ``kind='register'`` invite holding ``worker_id``. Returns its id."""
    row = {
        "token_hash": secrets.token_hex(16),
        "worker_id": worker_id,
        "created_by": "1000",
        "created_at": _stamp(0),
        "expires_at": _stamp(24),
        "max_uses": 1,
        "kind": "register",
        "pending_name": "Reserved Worker",
        "pending_role": role,
    }
    row.update(columns)
    placeholders = ", ".join("?" for _ in row)
    cursor = conn.execute(
        f"INSERT INTO enrollment_invites ({', '.join(row)}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return int(cursor.lastrowid or 0)


def test_a_live_registration_invite_holds_its_id_out_of_the_free_pool():
    """The bug this guards: the invite's id lives only on the invite row, so a naive scan of
    ``users`` calls it free and hands it to the next walk-up applicant - and the link the
    administrator already sent arrives dead with ``id_taken``."""
    with database.immediate() as conn:
        reserved = lowest_free_id(conn, "worker")
        _seed_invite(conn, reserved)
        following = lowest_free_id(conn, "worker")
    assert following != reserved, "the allocator handed out an id a live invite is holding"
    assert int(following) > int(reserved), "the next free number is above the reserved one"


def test_revoking_a_registration_invite_returns_its_id_to_the_pool():
    """A reservation is a hold, not a loss: revoke the link and the number comes back."""
    with database.immediate() as conn:
        reserved = lowest_free_id(conn, "worker")
        invite_id = _seed_invite(conn, reserved)
        assert lowest_free_id(conn, "worker") != reserved
        conn.execute(
            "UPDATE enrollment_invites SET revoked_at = ? WHERE id = ?", (_stamp(0), invite_id)
        )
        assert lowest_free_id(conn, "worker") == reserved


def test_an_expired_registration_invite_releases_its_id():
    """Expiry is the same clock the public page reads, so the allocator agrees with it."""
    with database.immediate() as conn:
        reserved = lowest_free_id(conn, "worker")
        _seed_invite(conn, reserved, expires_at=_stamp(-1))
        assert lowest_free_id(conn, "worker") == reserved


def test_a_reservation_in_another_role_block_is_ignored():
    """A moallem invite must not remove a worker's number from the worker pool."""
    with database.immediate() as conn:
        worker_id = lowest_free_id(conn, "worker")
        moallem_id = lowest_free_id(conn, "moallem")
        _seed_invite(conn, moallem_id, role="moallem")
        assert lowest_free_id(conn, "worker") == worker_id
