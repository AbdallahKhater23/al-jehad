"""Minting an account id at approval: the read and the write have to be one transaction.

WHY THIS EXISTS
---------------
``registrations._next_workforce_id`` answers "what number does this account get", and it is a
**read**. A read on its own is a snapshot: two approvals that both see "the highest is 43" both
insert 44, and one of them dies on the primary key. The thing that makes read-then-insert safe is
the *caller's* transaction - ``database.immediate()``, which takes SQLite's single write lock at
``BEGIN`` rather than at the first ``INSERT``, so the second caller cannot even read until the
first has committed and therefore sees the row the first one wrote.

That distinction is invisible in a single-threaded test, and it is the entire requirement, so this
file drives it from threads - and includes the **control** that shows what a deferred connection
does instead, because "it passed with threads" is not evidence that the lock is what made it pass.

WHAT IS PINNED
--------------
1. **No number is ever handed out twice** under real contention, and every number is inside the
   workforce band.
2. **The lock is taken at ``BEGIN``, not at the first write** - the property the allocator's
   docstring claims, stated as two observations: a plain read still succeeds while the write lock
   is held (which is exactly why a ``SELECT`` before an ``INSERT`` protects nothing), and a
   second ``BEGIN IMMEDIATE`` is refused outright.
3. **A full band is reported, not guessed at**: ``409 id_space_exhausted``, naming the band that
   is full and what an operator can do about it.
4. **The number is the lowest free one in the band, and the band ends below the administrative
   tiers.** Reuse is deliberate: a refusal deletes the account *and* wipes the face reference
   under its id (``registrations.reject_registration``), which is what makes handing that number
   to the next applicant safe rather than a new face inheriting an old one's template. An id at
   ``ADMIN_TIER_ID_FLOOR`` is neither counted when the next number is chosen nor ever minted by a
   submission. The per-role bands that used to split this id space are gone, so the two edges
   that matter now are the bottom of the workforce band and the administrative floor above it.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi import HTTPException

import database
import harness  # noqa: F401 - importing it is what redirects the database and the file trees
import registrations

BAND_CEILING = registrations.WORKFORCE_ID_CEILING
ADMIN_FLOOR = registrations.ADMIN_TIER_ID_FLOOR


def _allocate(conn: sqlite3.Connection) -> str:
    return registrations._next_workforce_id(conn)


def _insert(conn: sqlite3.Connection, user_id: str, role: str = "worker") -> None:
    """The smallest row that occupies a number. The columns the allocator reads, and no more."""
    conn.execute(
        "INSERT INTO users (id, name, password_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, f"Concurrent {user_id}", "x", role),
    )


def _ensure(conn: sqlite3.Connection, user_id: str, role: str) -> bool:
    """Insert the row unless it is already there. ``True`` when this call created it.

    Used for the two rows a test needs to *exist* rather than to own: nothing here may delete a
    row the shared fixture seeded, because the fixture's administrator is what the rest of the
    process reads.
    """
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None:
        return False
    _insert(conn, user_id, role)
    return True


def _free_band_numbers() -> list[int]:
    """Every number in the band nobody holds, so a test can fill it exactly.

    Read as *the numbers in use*, not as the highest one: the allocator hands out the lowest free
    number, so filling the band means occupying every gap - a fixture that only extended past the
    top would leave the allocator somewhere to go.
    """
    with database.db() as conn:
        taken = {
            int(row[0])
            for row in conn.execute(
                "SELECT CAST(id AS INTEGER) FROM users WHERE CAST(id AS INTEGER) BETWEEN 1 AND ?",
                (BAND_CEILING,),
            )
            if row[0] is not None
        }
    return [value for value in range(1, BAND_CEILING + 1) if value not in taken]


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
                    new_id = _allocate(conn)
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
        # Every number an approval minted has to be one an approval was allowed to mint.
        assert 1 <= int(value) <= BAND_CEILING, (
            f"{value} is outside the workforce band (1-{BAND_CEILING}), so an approval reached "
            "into the administrative tiers"
        )


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
                held["id"] = _allocate(conn)
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
                _allocate(contender)
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


def test_a_full_band_is_named_rather_than_guessed_at():
    """Every number below the administrative floor is spoken for: refuse, and say what to do."""
    filled = _free_band_numbers()
    with database.immediate() as conn:
        for value in filled:
            _insert(conn, str(value))
    try:
        with pytest.raises(HTTPException) as exhausted:
            with database.immediate() as conn:
                _allocate(conn)

        assert exhausted.value.status_code == 409
        detail = exhausted.value.detail
        assert detail["error_code"] == "id_space_exhausted"
        message = detail["message"]
        assert str(BAND_CEILING) in message and str(ADMIN_FLOOR) in message, (
            f"the refusal has to name the band that is full and the floor above it, and it says "
            f"{message!r}"
        )
        assert "Retire" in message, (
            "the refusal has to name what an operator can do, or it is a dead end"
        )
    finally:
        # Given back in the same test that spent them: a band left full would refuse every later
        # test in this process.
        with database.immediate() as conn:
            for value in filled:
                conn.execute("DELETE FROM users WHERE id = ?", (str(value),))


def test_the_lowest_free_number_is_handed_out_and_a_freed_one_comes_back():
    """Numbers fill the gaps, and a gap left by a deleted account is filled first.

    Counting upward from the highest id in use was the old rule, and it left every number a
    refused application gave back permanently unusable. What makes reuse safe is not this
    arithmetic: a refusal deletes the account and wipes the face reference under its id before
    the number is free, so the next holder of that number cannot be scored against the person
    before them (``registrations.reject_registration``).
    """
    with database.immediate() as conn:
        first = _allocate(conn)
        assert 1 <= int(first) <= BAND_CEILING
        _insert(conn, first)
        second = _allocate(conn)
        assert int(second) == int(first) + 1, (
            f"the next number after {first} was {second}; a contiguous band is not being walked"
        )
        _insert(conn, second)
        # Give the *lower* one back: a scan for the lowest free number fills it in again, which
        # is exactly what the old allocator refused to do.
        conn.execute("DELETE FROM users WHERE id = ?", (first,))
        third = _allocate(conn)
    assert third == first, (
        f"the gap at {first} left by a deleted account was skipped and {third} was handed out"
    )


def test_an_administrators_id_is_neither_counted_nor_minted():
    """The scan is banded: an admin at the floor does not push the next number above it."""
    with database.immediate() as conn:
        created = _ensure(conn, str(ADMIN_FLOOR), "admin")
        try:
            allocated = _allocate(conn)
            assert int(allocated) < ADMIN_FLOOR, (
                f"an approval was handed {allocated}, at or above {ADMIN_FLOOR}: it can mint an "
                "account in an administrative tier"
            )
        finally:
            if created:
                conn.execute("DELETE FROM users WHERE id = ?", (str(ADMIN_FLOOR),))


def test_an_id_that_is_not_a_number_occupies_no_number():
    """``users.id`` is TEXT and the band scan casts, so a non-numeric id is not a number in it."""
    with database.immediate() as conn:
        before = _allocate(conn)
        created = _ensure(conn, "worker-legacy", "worker")
        try:
            after = _allocate(conn)
        finally:
            if created:
                conn.execute("DELETE FROM users WHERE id = ?", ("worker-legacy",))
    assert after == before, (
        f"the allocator read the non-numeric id 'worker-legacy' as a number in the band and moved "
        f"the next number from {before} to {after}"
    )
