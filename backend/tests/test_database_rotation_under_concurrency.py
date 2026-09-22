"""Each test gets its own database file, so a reset never waits on a lock.

THE FAILURE THIS PINS
---------------------
Every suite here clones the live ``times.db`` into a throwaway copy and restores that copy
before each test. The original restore overwrote one fixed path *in place*, and to do it
safely it had to take the database's exclusive lock - fold the write-ahead log, flip
``journal_mode`` back to ``delete``, then write the pristine bytes. Anything still holding
that one file turned fixture setup into ``sqlite3.OperationalError: database is locked`` for a
test that had not started yet, and the something-holding-it is always present in this suite:

* a browser test's live uvicorn thread is still serving the *previous* test while the next
  test's fixture setup runs;
* a connection leaked by a test that failed mid-write is never closed;
* two pytest processes run against the same checkout at once.

The fix is a *rotation* (``harness._restore_pristine``): the database lives at a stable path,
``CURRENT_DIR / "times.db"``, where ``CURRENT_DIR`` is a directory junction (a symlink on
POSIX) that each reset repoints at a brand-new generation directory built from the pristine
snapshot. Building the new generation touches only files nothing has ever opened; repointing
the link touches only the link. No step of a reset ever needs a lock on a database file, so a
straggler can keep reading the generation it is on and write into a directory that is already
garbage, harmlessly.

WHY THIS FILE EXISTS
--------------------
The rotation was described in ``harness`` but nothing proved it. The three things that could
silently regress are exactly the three this file measures:

1. a reset still completing while another connection holds the write lock with a zero
   timeout, i.e. taking no lock at all;
2. each reset really handing out a *different* file, and sweeping old generations so the
   throwaway tree does not grow without bound;
3. several processes doing this at once, which only means anything because each process has
   its own private temp tree - the assertion is that they never share a file to collide on.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import harness


def _resolved_database() -> Path:
    """The generation file ``harness.DB_PATH`` actually reaches right now.

    Resolved through the junction/symlink on purpose: the stable path is the same string
    before and after a rotation, and the *file* is what changes. Verified to resolve
    directory junctions on Windows (``os.path.realpath``) and symlinks on POSIX.
    """
    return Path(os.path.realpath(str(harness.DB_PATH)))


def test_a_reset_never_waits_for_the_database_a_straggler_still_holds(app_module):
    """The exact historical failure: a holder with the write lock must not block a reset.

    The straggler is opened the way the *first* connection of a browser test's live uvicorn
    server was: through the stable path, which is a junction. The reset then runs with the
    write lock held and a zero busy timeout, so a reset that tried to take that lock - the old
    in-place restore, which folded the WAL and flipped the journal mode on the live file -
    fails immediately with "database is locked", the way it presented in the suite: as an
    error in fixture setup for a test that had not begun.

    What made this pass rather than only describe the rotation is that connections are opened
    by the *generation* path, not the junction (``database.resolve_path``,
    ``harness.current_db_path``). SQLite keys a WAL database's locks by the path it was
    opened with, and on Windows two connections through the same junction across a repoint
    contend over the old file's lock even though the junction now names a different file - so
    a reset that reopened the junction would still be the bug this file exists for.
    """
    holder = sqlite3.connect(str(harness.DB_PATH), timeout=0.0, isolation_level=None)
    try:
        # Take and keep the write lock, and touch a row so the lock is not theoretical.
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE active_sessions SET site_name = site_name WHERE 1")
        # The file the holder is on: what the stable path resolves to *before* the reset. It is
        # captured now because resolving it afterwards follows the repointed junction instead.
        held_before = _resolved_database()

        # This is the assertion: it must not raise, and there is no busy_timeout to save it.
        harness.reset_database(app_module)

        assert _resolved_database() != held_before, (
            "the reset did not hand out a fresh database file: the stable path still reaches "
            "the generation the straggler is holding"
        )
        # The application opens the same generation the reset installed, by its concrete
        # path - not through the junction that the straggler is still parked on.
        assert _resolved_database() == harness.current_db_path()
        # The straggler is untouched and can still use the generation it holds.
        assert holder.execute("SELECT COUNT(*) FROM active_sessions").fetchone() is not None
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_every_reset_hands_out_a_distinct_generation(app_module):
    """One file per reset - the property "each test gets its own database" reduces to.

    The generation directories are also swept, so a long run does not accumulate one clone of
    the database per test; ``keep`` is small but never one, because the previous generation is
    the one a still-running browser server is most likely to be reading.
    """
    seen: list[Path] = []
    for _ in range(4):
        harness.reset_database(app_module)
        generation = _resolved_database()
        assert generation.parent.parent == harness.GENERATIONS_DIR, (
            f"{generation} is not inside the generations directory {harness.GENERATIONS_DIR}"
        )
        seen.append(generation)

    assert len(set(seen)) == len(seen), (
        f"a reset reused a database file: {seen}. A straggler still reading the earlier one "
        "would be writing into the database the next test is asserting against"
    )

    remaining = sorted(harness.GENERATIONS_DIR.glob("gen-*"))
    assert len(remaining) <= 3, (
        f"{len(remaining)} generations survived four resets ({[p.name for p in remaining]}); "
        "the sweep is not bounding the throwaway tree"
    )


def test_the_application_writes_to_the_generation_the_reset_installed(app_module):
    """The rotation has to be invisible to the code under test at its stable path.

    ``settings.database_path`` and ``database.DB_PATH`` are resolved once, at import, to the
    stable path - not to a generation. A rotation that broke that resolution would leave the
    application reading the previous test's database, and the symptom would be a test passing
    against stale rows rather than a clean error.
    """
    harness.reset_database(app_module)
    first = _resolved_database()

    with app_module.db(write=True) as conn:
        conn.execute("UPDATE shift_rules SET regular_hours = 7.25 WHERE id = 1")
    assert app_module.get_shift_rules()["regular_hours"] == 7.25

    harness.reset_database(app_module)

    assert _resolved_database() != first
    assert app_module.get_shift_rules()["regular_hours"] == app_module.DEFAULT_SHIFT_RULES["regular_hours"], (
        "a write made before the reset was still visible after it, so the application is "
        "reading a generation the reset no longer points at"
    )


#: One process's workload: hold the write lock, then rotate, repeatedly. Run in a clean
#: interpreter because the property under test is about *processes*, and because the parent
#: process's harness has already pinned its own private temp tree.
_PARALLEL_WORKER = """
import sqlite3, sys

for _ in range({rounds}):
    holder = sqlite3.connect(str(harness.DB_PATH), timeout=0.0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE active_sessions SET site_name = site_name WHERE 1")
    harness._restore_pristine()
    holder.execute("ROLLBACK")
    holder.close()

print("ok", harness.DB_PATH.resolve())
"""


def test_parallel_processes_never_collide_on_the_database():
    """Several processes rotating at once, none sharing a file to collide on.

    The two-process failure that motivated the rotation happened because both processes
    reached the *same* file. Each process now builds its tree under its own
    ``tempfile.mkdtemp``, so a second process cannot hold anything the first needs. This runs
    the workload concurrently and fails if any process reports a lock error; ``timeout=0.0``
    inside the worker means a lock wait would show up as a failure rather than a stall.
    """
    rounds = 5
    processes = 4
    script = _PARALLEL_WORKER.format(rounds=rounds)
    # ``harness`` lives beside the tests, not on the application's path, so both have to be
    # importable in the child exactly as pytest arranges them in this one.
    tests_dir = str(harness.BACKEND_DIR / "tests")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [tests_dir, str(harness.BACKEND_DIR), env.get("PYTHONPATH", "")]
    )
    env["PYTHONIOENCODING"] = "utf-8"

    running = [
        subprocess.Popen(
            [sys.executable, "-c", f"import harness\n{script}"],
            cwd=str(harness.BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(processes)
    ]
    results = []
    for process in running:
        output, _ = process.communicate(timeout=300)
        results.append((process.returncode, output))

    for code, output in results:
        assert code == 0, f"a parallel reset process failed:\n{output}"
        assert "ok" in output, output
        assert "locked" not in output.lower(), (
            "a parallel process hit a database lock while rotating; the per-test databases "
            f"are not isolated after all:\n{output}"
        )

    # Every process reported its own stable path, which is the stable path of *its* tree -
    # the proof that no two processes were ever pointed at the same file.
    resolved = [
        line.split(" ", 1)[1].strip()
        for _code, output in results
        for line in output.splitlines()
        if line.startswith("ok ")
    ]
    assert len(resolved) == processes, f"a process did not report its database path: {results}"
    assert len(set(resolved)) == processes, (
        f"two processes rotated the same database file: {resolved}"
    )
