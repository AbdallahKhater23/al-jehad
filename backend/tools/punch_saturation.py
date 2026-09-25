"""Saturate the punch endpoint on this machine and gate the process's peak RSS.

Why this exists
---------------
``backend/tools/capacity_test.py`` is the accurate instrument: the real image, in a
``--memory 512m --memory-swap 512m --cpus 1`` container, with the kernel's own counters read
from inside. It is also the *slow* one - a docker build, a container, several minutes of real
inference - which is the right price once, and the wrong price on every pull request. A budget
that is only checked when somebody remembers to run the expensive harness is a budget that is
enforced by inspection, and inspection is what missed this in the first place.

So this is the per-push half, and it deliberately measures less:

* it runs **the same application**, from this checkout, with the real YuNet and FaceNet
  models and the deployment's own memory profile (``FACE_INFERENCE_QUEUE=8``,
  ``STANDING_SWEEP_ENABLED=0``, engine capacity 2), as a **child process** on a loopback
  port, with a private data directory of its own;
* it drives ``POST /api/v1/attendance/verify`` to saturation - more concurrent punches than
  the engine can run, for several rounds, so frames are continuously in flight - and samples
  **the application's own process** while it does: resident set, the anonymous set (the part
  the kernel cannot evict), and the process's own high-water marks (``VmHWM`` /
  ``PeakWorkingSetSize``), which no sampling interval can miss;
* it fails the run when the peak crossed ``--ceiling-mib`` (default 400), or when the anon
  set crossed ``--anon-ceiling-mib``, or when the memory did not come back down afterwards,
  or when the load was answered by anything other than the pipeline and the designed 503.

What it does *not* measure, said plainly because the difference is the whole reason the other
harness exists: there is no cgroup cap here, so nothing kills the process when it crosses a
line - the gate is a prediction of the container's behaviour, not the container refusing. CPU
throttling, ``memory.events`` and the kernel's OOM accounting are all absent for the same
reason, and ``python tools/capacity_test.py`` remains the run that answers those. What this
does catch is the failure class that took the instance down: memory that grows with the
number of punches *waiting*, and memory that does not come back after a burst.

Asking the right process
------------------------
The pid this harness starts is not always the process that runs the application: on Windows a
virtualenv's ``python.exe`` is a **launcher** that re-executes the base interpreter as a child,
so the started process reads about five megabytes while the one holding the models - a
grandchild, a hundred times larger - is the one a ceiling is about. Gating on the launcher
would have made ``--ceiling-mib`` unfalsifiable: every run passing, and the number describing a
stub. The harness therefore resolves the application by size across its process tree once the
health check passes, prints which pid it chose, and ends the **tree** at shutdown - terminating
the launcher on its own leaves the interpreter behind with the database open.

The face, and the two modes
---------------------------
A punch that reaches the scoring step is the only punch whose memory is the deployment's: a
frame the detector refuses never runs the embedding, and a punch refused before the photo is
read (geofence, rate limit, session) never allocates it. ``--faces real`` therefore requires a
face-bearing selfie - ``--selfie``, ``PUNCH_SATURATION_SELFIE``, or the first ``.jpg`` under
``worker_photos/`` - and gates that every punch was decoded and detected.

That photo is necessarily outside the repository (this project does not commit faces), so
``--faces none`` (the default when no selfie can be found, and what CI runs) sends a frame
with no face in it. Every punch still exercises the upload policy, the spooled file, the
1280 px decode, the detector and the queue; the embedding is skipped, and the report says so
in its own words rather than in a footnote. Templates come from ``--templates synthetic`` by
default, which writes a provenance-correct template for each seeded account: the punches are
then refused *on the score*, after the full pipeline, which is what makes them measurable.

Usage
-----
    # what CI runs: no docker, no secrets, a face-free frame, gates at 400 MiB
    python backend/tools/punch_saturation.py

    # the honest one, with a real face: every punch is decoded, detected and scored
    python backend/tools/punch_saturation.py --selfie worker_photos/me.jpg --faces real

    # arming the ceiling from the pipeline, and keeping the data directory to look at
    python backend/tools/punch_saturation.py --ceiling-mib 380 --data-dir /tmp/punch-run

Exit codes
----------
    0  every gate passed
    1  a gate failed (the table names it) - or the run could not be made at all

Isolation
---------
The child gets a throwaway database and file trees, its own generated ``SECRET_KEY`` and its
own port. Nothing here reads or writes the live database, the live reference directory or a
running deployment.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

# The module next door *is* the vocabulary: one ``Limits``, one ``Gate``, one ``Window``, one
# punch driver, one seed. A second copy of any of them is how two harnesses come to disagree
# about what a pass means - the failure this tool exists to prevent, applied to itself.
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import capacity_test as harness  # noqa: E402 - the path above has to exist first
from capacity_test import (  # noqa: E402
    MIB,
    SEED_JSON_PREFIX,
    CapacityError,
    CgroupReading,
    Gate,
    LevelResult,
    Limits,
    RunReport,
    Sampler,
    SeedResult,
    Window,
    api_url,
    default_selfie,
    free_port,
    run_level,
    runtime_secret,
    summarise_window,
)

#: The deployment's memory profile, restated for a host process. The same list
#: ``capacity_test.start_container`` passes as ``--env``, so a run of either harness is a run
#: of the same deployment - including the rate limiters, which are per client IP and which a
#: burst from one address would otherwise spend its whole budget hitting.
DEPLOYMENT_ENV: dict[str, str] = {
    "FACE_INFERENCE_QUEUE": "8",
    "STANDING_SWEEP_ENABLED": "0",
    "ATTENDANCE_RATE_LIMIT": "1000000/minute",
    "ENROLLMENT_RATE_LIMIT": "1000000/minute",
}

#: The file trees the child owns. Every one of them points inside the throwaway data
#: directory, because the alternative is a harness that writes a worker's face beside the
#: source tree.
DATA_DIR_ENV: dict[str, str] = {
    "DATABASE_PATH": "times.db",
    "LOCAL_REFS_DIR": "local_references",
    "WORKER_PHOTOS_DIR": "worker_photos",
    "PUNCH_FRAMES_DIR": "punch_frames",
    "QUICK_LINK_PHOTOS_DIR": "quick_link_photos",
    "BACKUP_DIR": "backups",
}

#: ``(status, error_code)`` pairs that prove a punch's frame reached the model stack, i.e.
#: that its memory is the deployment's. A ``200`` is the approved/flagged punch; a ``422``
#: with ``face_mismatch`` is the same pipeline disagreeing about the score; a ``400`` with a
#: detector code is the same decode and the same detector with nothing to align. Everything
#: else (geofence, rate limit, liveness, no template) is a cheaper path, and a run built out
#: of those would report a wonderfully small number about a deployment that never ran.
PIPELINE_CODES = frozenset({"face_mismatch", "face_not_found", "multiple_faces"})

#: The engine's designed answer to a full queue: ``face_engine.busy_http_exception``.
SHED_STATUS = 503


class ProcessSampler(Sampler):
    """Peak RSS of **one process**, sampled on its own thread.

    The thread is the whole implementation, for the same reason ``LocalSampler`` has one: a
    saturated punch round is spent waiting on a server that is busy, so a harness that only
    read between rounds would miss the spike the round was made of. ``begin`` takes its
    reading synchronously so the bracket is exact, the thread fills in between, and ``end``
    reads again.

    The high-water mark is the number the gate is about, and it is not sampled at all: the
    kernel keeps it (``VmHWM`` on Linux, ``PeakWorkingSetSize`` on Windows), so a spike
    between two ticks still counts. The samples are there to show the shape - when the frame
    count peaked, and whether it came back down.
    """

    def __init__(self, pid: int, interval: float, reader: "ProcessReader") -> None:
        self.pid = pid
        self.interval = interval
        self.reader = reader
        self._lock = threading.Lock()
        self._readings: list[tuple[float, CgroupReading]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._label = ""
        self._start = 0

    def _record(self) -> CgroupReading:
        reading = self.reader(self.pid)
        with self._lock:
            self._readings.append((time.monotonic(), reading))
        return reading

    def start(self) -> None:
        self._record()
        self._thread = threading.Thread(target=self._loop, name="process-sampler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._record()
            except CapacityError:
                # A read that fails mid-run is not a reason to lose the bracket: the next tick
                # retries, and a process that is genuinely gone fails loudly at ``end``.
                continue

    def begin(self, label: str) -> None:
        with self._lock:
            self._label = label
            self._start = len(self._readings)
        self._record()

    def end(self) -> Window:
        self._record()
        with self._lock:
            window = list(self._readings[self._start :])
        seconds = window[-1][0] - window[0][0] if len(window) > 1 else 0.0
        return summarise_window(self._label, [reading for _, reading in window], seconds)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Reading one process
# --------------------------------------------------------------------------- #
ProcessReader = Callable[[int], CgroupReading]


def parse_proc_status(text: str) -> dict[str, int]:
    """``/proc/<pid>/status`` -> the five memory fields, in bytes.

    Pure, so the shape of a real file can be pinned by a test on any platform: this is the
    reader the Linux CI gate runs on, and a parser that silently reported zeros would make
    every ceiling pass. Fields the kernel does not carry are simply absent - ``RssAnon``
    only exists from 4.5, and its absence is what turns the anonymous-set gate off rather
    than into a zero.
    """
    wanted = ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem")
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if not separator or key not in wanted:
            continue
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024  # the kernel reports kB
    return values


def read_proc_reading(pid: int, *, proc: Path = Path("/proc")) -> CgroupReading:
    """One reading of a live process, from the kernel's own accounting files."""
    status = proc / str(pid) / "status"
    try:
        fields = parse_proc_status(status.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise CapacityError(f"could not read {status}: {exc}") from exc
    if not fields:
        raise CapacityError(f"{status} carried none of the memory fields this harness reads")
    anon = fields.get("RssAnon", 0) + fields.get("RssShmem", 0)
    file_backed = fields.get("RssFile", 0)
    resident = fields.get("VmRSS", anon + file_backed)
    return CgroupReading(
        memory_bytes=resident,
        anon_bytes=anon,
        file_bytes=file_backed,
        # The kernel's high-water mark, floored at what is resident now: a process whose
        # VmHWM is somehow lower than its current RSS would otherwise gate on the wrong one.
        peak_bytes=max(fields.get("VmHWM", resident), resident),
        events={},
        usage_usec=_cpu_usec(pid, proc=proc),
        nr_periods=0,
        nr_throttled=0,
        throttled_usec=0,
    )


def _cpu_usec(pid: int, *, proc: Path = Path("/proc")) -> int:
    """CPU time this process has used, in microseconds. Reported, never gated.

    Throttling is a cgroup property and belongs to the container harness; what survives to
    a host run is how much CPU the punch path spent, which is worth printing next to the
    memory it cost and is not a threshold anybody should be woken by.
    """
    try:
        fields = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
    except OSError:
        return 0
    if len(fields) < 13:
        return 0
    ticks = int(fields[11]) + int(fields[12])  # utime, stime
    return int(ticks * 1_000_000 / (os.sysconf("SC_CLK_TCK") or 100))


class _ProcessMemoryCounters(ctypes.Structure):
    """``PROCESS_MEMORY_COUNTERS`` from psapi, for the Windows half of the reader."""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def read_windows_reading(pid: int) -> CgroupReading:
    """One reading of a live process, from the Windows process counters.

    The working set, not the anonymous set: Windows does not expose the split the way
    ``/proc/<pid>/status`` does, so the anon gate is reported as *not measured* on this
    platform rather than quietly satisfied. It counts file-backed pages as resident, which
    makes it an upper bound - the same caveat the development ladder carries.
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise CapacityError(f"could not open process {pid} to read its memory")
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise CapacityError(f"GetProcessMemoryInfo failed for process {pid}")
        working = int(counters.WorkingSetSize)
        peak = max(int(counters.PeakWorkingSetSize), working)
        return CgroupReading(
            memory_bytes=working,
            anon_bytes=working,
            file_bytes=0,
            peak_bytes=peak,
            events={},
            usage_usec=0,
            nr_periods=0,
            nr_throttled=0,
            throttled_usec=0,
        )
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def process_reader() -> tuple[ProcessReader, str, bool]:
    """``(reader, platform name, whether the anonymous set is separable)``.

    Refused rather than approximated where neither source exists: a gate that cannot see the
    number it is about is worse than no gate, because it reports a pass.
    """
    if sys.platform == "win32":
        return read_windows_reading, "win32", False
    if Path("/proc").is_dir():
        return read_proc_reading, "proc", True
    raise CapacityError(
        f"no way to read another process's memory on {sys.platform}: this harness needs "
        "/proc (Linux) or the Windows process counters. The container harness "
        "(tools/capacity_test.py) measures a cgroup instead and needs docker."
    )


# --------------------------------------------------------------------------- #
# Which process is the application
# --------------------------------------------------------------------------- #
def descendants_from(parents: Mapping[int, int], root: int) -> list[int]:
    """``[root, every descendant]``, parents before children.

    Pure, because the shape it walks is the whole reason this exists: on Windows a virtualenv's
    ``python.exe`` is a launcher - a few megabytes of stub that re-executes the base
    interpreter as a child - so "the pid we started" and "the process running the
    application" are two processes with a hundredfold difference in size between them.

    A parent map is a snapshot, so a process that exited while it was being taken is simply
    absent, and losing one branch costs the walk that branch rather than the whole answer. The
    root is always first and always included: the caller is about to measure it and should get
    a reading error rather than an empty list.
    """
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    ordered: list[int] = [root]
    seen = {root}
    index = 0
    while index < len(ordered):
        for child in sorted(children.get(ordered[index], ())):
            if child not in seen:
                seen.add(child)
                ordered.append(child)
        index += 1
    return ordered


def windows_parent_map() -> dict[int, int]:
    """``pid -> ppid`` for every process on this machine, from one snapshot."""
    import ctypes.wintypes as wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class ProcessEntry32W(ctypes.Structure):  # noqa: N801 - the Win32 name
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise CapacityError("could not snapshot the process table to find the application's pid")
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        walking = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while walking:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            walking = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def posix_parent_map(*, proc: Path = Path("/proc")) -> dict[int, int]:
    """``pid -> ppid`` for every process, read from ``/proc/<pid>/stat``.

    One scan of ``/proc`` rather than ``/proc/<pid>/task/<tid>/children``: the children file
    depends on how the kernel was configured, and field 4 of each ``stat`` is there on every
    Linux this deployment runs on. Called only at startup and at shutdown, so the scan is
    never inside a measurement.
    """
    parents: dict[int, int] = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return parents
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The command name is parenthesised and may itself contain spaces and parentheses, so
        # the fields after it are found from the *last* ")" rather than by splitting the line.
        _, separator, rest = text.rpartition(")")
        fields = rest.split() if separator else []
        if len(fields) < 2:
            continue
        parents[int(entry.name)] = int(fields[1])
    return parents


def process_tree(pid: int) -> list[int]:
    """``[pid, every process below it]``, parents before children, on this platform."""
    parents = windows_parent_map() if sys.platform == "win32" else posix_parent_map()
    return descendants_from(parents, pid)


def resolve_app_pid(
    root_pid: int,
    reader: ProcessReader,
    *,
    tree: Callable[[int], Sequence[int]] = process_tree,
) -> tuple[int, CgroupReading]:
    """The process to measure: the largest at or below ``root_pid``, and one reading of it.

    Size, not position, because the promise being kept is a memory ceiling - the process holding
    the models is the one that can breach it, and on Windows that is the grandchild. Ties keep
    the first in the walk, which leaves the single-process case the answer it always was: the
    pid we started.
    """
    candidates = list(tree(root_pid))
    best_pid: int | None = None
    best: CgroupReading | None = None
    failures: list[str] = []
    for candidate in candidates:
        try:
            reading = reader(candidate)
        except CapacityError as exc:
            failures.append(f"{candidate} ({exc})")
            continue
        if best is None or (reading.peak_bytes, reading.memory_bytes) > (best.peak_bytes, best.memory_bytes):
            best_pid, best = candidate, reading
    if best is None or best_pid is None:
        raise CapacityError(
            f"none of the {len(candidates)} processes at or below pid {root_pid} could be read "
            f"({', '.join(failures[:3])}): there is nothing to measure"
        )
    return best_pid, best


def still_readable(pid: int, reader: ProcessReader) -> bool:
    """Whether a process is still there, asked the same way the gates ask for its memory."""
    try:
        reader(pid)
    except CapacityError:
        return False
    return True


def end_process(pid: int) -> None:
    """Ask one process to stop, ignoring one that has already gone."""
    try:
        os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess, which is what a stub needs
    except (OSError, ValueError):
        return


# --------------------------------------------------------------------------- #
# The child: the deployment, from this checkout
# --------------------------------------------------------------------------- #
@dataclass
class AppHandle:
    """The application under measurement: a child process, its port and its own data."""

    process: subprocess.Popen
    base_url: str
    data_dir: Path
    log_path: Path
    env: dict[str, str]
    #: The pid the gates measure. Usually the one started here; on Windows the virtualenv
    #: launcher's own child, which is the process that loads the models.
    measured_pid: int | None = None

    def log_tail(self, lines: int = 25) -> str:
        try:
            kept = [line for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        except OSError:
            return "    (no log)"
        return "\n".join(f"    {line}" for line in kept[-lines:])

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self, grace: float = 10.0) -> None:
        """End the application: every process it stands for, children first.

        The tree rather than the pid, because the pid started here may be a launcher:
        terminating it alone leaves the interpreter running with the database open, which is how
        a harness leaves a couple of hundred megabytes - and a bound port - behind after every
        run.
        """
        if self.process.poll() is not None:
            return
        try:
            tree = process_tree(self.process.pid)
        except CapacityError:  # pragma: no cover - a failed snapshot is not a reason to leak
            tree = [self.process.pid]
        for pid in reversed(tree):
            if pid != self.process.pid:
                end_process(pid)
        self.process.terminate()
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:  # pragma: no cover - only a wedged child
            self.process.kill()
            self.process.wait(timeout=grace)


def data_dir_for(args: argparse.Namespace) -> Path:
    """The child's private world: a throwaway tree unless the caller named one."""
    root = Path(args.data_dir).expanduser() if args.data_dir else Path(tempfile.mkdtemp(prefix="punch-saturation-"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def child_env(data_dir: Path) -> dict[str, str]:
    """The deployment's profile, the child's own file trees, and a key to boot with.

    ``runtime_secret`` is shared with the container harness rather than re-derived: the
    application refuses to boot without a signing key and derives no default, so both
    harnesses need the same answer for the same reason. Nothing signed here outlives the run.
    """
    env = dict(os.environ)
    env.update(DEPLOYMENT_ENV)
    env.update({name: str(data_dir / relative) for name, relative in DATA_DIR_ENV.items()})
    secret, _source = runtime_secret()
    env["SECRET_KEY"] = secret
    # One process, one port: the env var would otherwise be read by ``serve.default_port``
    # before the flag it is given on the command line.
    env.pop("PORT", None)
    # The child reads *no* ``.env``: it is configured by the environment above and nothing
    # else. A checkout's own file is a developer's deployment (a certificate, a live database
    # path, a tunnel address), and a memory gate that inherited it would measure that
    # deployment on this machine - which is the difference between a harness and a habit.
    env["ENV_FILE"] = str(data_dir / "no-env-file")
    return env


def start_app(args: argparse.Namespace, data_dir: Path) -> AppHandle:
    """Start ``serve.py`` as a child, on a loopback port, logging to the data directory."""
    port = args.port or free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = data_dir / "app.log"
    env = child_env(data_dir)
    # Plain HTTP on loopback, like the container harness publishes: the punches come from
    # this process, and a certificate in the loop would be a second variable in a memory
    # measurement.
    command = [sys.executable, "serve.py", "--host", "127.0.0.1", "--http", "--port", str(port)]
    log = log_path.open("wb")
    process = subprocess.Popen(command, cwd=str(BACKEND_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    handle = AppHandle(process=process, base_url=base_url, data_dir=data_dir, log_path=log_path, env=env)
    try:
        wait_until_serving(handle, args.startup_timeout)
    except BaseException:
        handle.stop()
        log.close()
        raise
    log.close()
    return handle


def wait_until_serving(handle: AppHandle, timeout: float) -> None:
    """Wait for the child to answer the deployment's own status route.

    The status route needs no session (the image's ``HEALTHCHECK`` uses it), so a 200 means
    migrations ran, the model files loaded and the lifespan came up. A child that *exited* is
    reported with its log rather than with a timeout, because "it never came healthy" and
    "it crashed on the first import" need different afternoons.
    """
    httpx = harness._load_httpx()
    deadline = time.monotonic() + timeout
    last = "no attempt yet"
    while time.monotonic() < deadline:
        if not handle.alive():
            raise CapacityError(
                f"the application exited before it served anything (exit {handle.process.returncode}):\n"
                f"{handle.log_tail()}"
            )
        try:
            response = httpx.get(api_url(handle.base_url, harness.STATUS_PATH), timeout=10.0)
            if response.status_code == 200:
                return
            last = f"{response.status_code} {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - not up yet is the expected case
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise CapacityError(
        f"the application never answered {api_url(handle.base_url, harness.STATUS_PATH)} within "
        f"{timeout:.0f}s ({last}):\n{handle.log_tail()}"
    )


def seed_roster(args: argparse.Namespace, handle: AppHandle) -> SeedResult:
    """Write the roster with the sibling tool's own seed, in the child's environment.

    Through ``capacity_test`` rather than beside it: the seed owns the biometric id format,
    the template's provenance and the id range it is allowed to clear, and a second copy of
    those three is how a harness starts measuring a deployment nobody has.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS_DIR / "capacity_test.py"),
            "--seed",
            "--users",
            str(args.users or args.clients + 1),
            "--templates",
            args.templates,
        ],
        cwd=str(BACKEND_DIR),
        env=handle.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600.0,
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith(SEED_JSON_PREFIX):
            return SeedResult(**json.loads(line[len(SEED_JSON_PREFIX) :]))
    raise CapacityError(
        "the seed printed no roster:\n"
        f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )


# --------------------------------------------------------------------------- #
# The face the punches send
# --------------------------------------------------------------------------- #
def faceless_frame(size: int = 640) -> bytes:
    """A frame with no face in it, for the run that has no face to send.

    Deliberately not a photograph of anybody: the point of this mode is that the deployment
    has no committed face, and a harness that shipped one would be exactly the thing this
    project does not do. What it does exercise is the whole path up to the detector - the
    upload policy, the spooled file, the 1280 px decode, the frame arrays, the queue and the
    refusals - which is the part a burst actually breaks.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (size, size), (176, 182, 188))
    draw = ImageDraw.Draw(image)
    for index in range(0, size, 40):  # structure, so the JPEG is not a solid block
        draw.line([(0, index), (size, index)], fill=(150, 156, 162), width=2)
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def resolve_selfie(args: argparse.Namespace) -> tuple[str, bytes, str]:
    """``(mode, jpeg, note)``: the face the punches send, and what that means for the gates.

    ``real`` is the run whose numbers are the deployment's: a face in the frame means the
    detector finds it, the alignment runs and the embedding is paid for. ``none`` is the run
    CI can make on a checkout with no face in it, and it says out loud which step it skipped
    so nobody reads its ceiling as the whole story.
    """
    if args.faces == "none":
        return "none", faceless_frame(), "no face in the frame: the embedding step is not exercised"
    candidate = args.selfie or os.environ.get("PUNCH_SATURATION_SELFIE") or default_selfie()
    if candidate and Path(candidate).is_file():
        return "real", Path(candidate).read_bytes(), f"a face from {candidate}"
    if args.faces == "real":
        raise CapacityError(
            "--faces real needs a face-bearing selfie, and none was found. Pass --selfie "
            "worker_photos/<a real face>.jpg, or set PUNCH_SATURATION_SELFIE. Without one the "
            "punches never reach the embedding, and their memory would not be the deployment's."
        )
    return "none", faceless_frame(), "no selfie available: the embedding step is not exercised"


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #
def reached_the_pipeline(result: Mapping[str, Any]) -> bool:
    """Whether this punch's frame was decoded and put in front of the model stack."""
    if int(result.get("status") or 0) == 200:
        return True
    return str(result.get("error_code") or "") in PIPELINE_CODES


def evaluate_gates(report: RunReport, limits: Limits, *, anon_measured: bool = True) -> list[Gate]:
    """The verdict: one :class:`Gate` per promise this profile makes.

    Pure and total - no clock, no network, no child process - because the thresholds are the
    part of a load test that is worth arguing about, and an argument needs to be settleable
    without paying for the traffic again (``tests/test_punch_saturation_gates.py`` pins every
    one of these by mutating a healthy synthetic run).
    """
    measured = report.measured
    results = report.all_results
    gates: list[Gate] = []

    def mib(value: int) -> str:
        return f"{value / MIB:.0f} MiB"

    # The sampler's maximum and the kernel's high-water mark, whichever is worse: the first
    # is "how close did it get while I watched" and the second is "how close did it get at
    # all", and gating on the first alone would make this gate a function of --interval.
    peak = max(measured.memory_max, measured.peak_bytes)
    gates.append(
        Gate(
            "peak rss",
            peak <= limits.memory_bytes,
            f"<= {limits.memory_mib} MiB",
            mib(peak),
            "the process's own high-water mark: resident set, page cache included",
        )
    )
    if anon_measured:
        gates.append(
            Gate(
                "anonymous set",
                measured.anon_max <= limits.anon_bytes,
                f"<= {limits.anon_mib} MiB",
                mib(measured.anon_max),
                "the part of the resident set the kernel cannot evict",
            )
        )

    answered = [result for result in results if int(result.get("status") or 0)]
    scored = [result for result in results if reached_the_pipeline(result)]
    fraction = len(scored) / len(results) if results else 0.0
    gates.append(
        Gate(
            "load reached the pipeline",
            fraction >= limits.min_scored_fraction,
            f">= {limits.min_scored_fraction * 100:.0f}%",
            f"{len(scored)}/{len(results)}",
            "only these punches could allocate a frame; the rest were refused by something in front",
        )
    )

    # The queue policy, from the client's side: a saturated engine is *meant* to answer 503,
    # and it is meant to say when to come back. A 503 without the header is a client that
    # retries immediately, which is how a saturated deployment is kept saturated.
    shed = [result for result in answered if int(result["status"]) == SHED_STATUS]
    bare = [result for result in shed if not str(result.get("retry_after") or "").strip()]

    # The designed 503 is not a server error. It is the engine refusing past its queue, which is
    # the state this harness spends its whole run creating: counting it here would fail every
    # run that saturated - exactly the runs the gate is for - and the shedding gate above is
    # where it belongs. A 500, or a 503 no engine wrote, still counts.
    server_errors = [
        result
        for result in results
        if int(result["status"]) >= 500 and int(result["status"]) != SHED_STATUS
    ]
    transport = [result for result in results if not int(result["status"])]
    gates.append(
        Gate(
            "no transport errors",
            not transport,
            "== 0",
            str(len(transport)),
            "; ".join(f"{r['status']} {str(r.get('detail'))[:60]}" for r in transport[:3])
            or "every punch got an answer",
        )
    )
    gates.append(
        Gate(
            "no server errors",
            not server_errors,
            f"== 0 ({len(shed)} x designed 503 excluded)",
            str(len(server_errors)),
            "; ".join(f"{r['status']} {str(r.get('detail'))[:60]}" for r in server_errors[:3])
            or "nothing answered with a 500",
        )
    )
    gates.append(
        Gate(
            "shedding is the designed one",
            not bare,
            "503 + Retry-After",
            f"{len(shed)} x 503, {len(bare)} without the header",
            "the engine refuses past its queue with a Retry-After; anything else is a bare 503",
        )
    )

    # A refusal in front of the pipeline is not a load result at all: it is a run that
    # measured a cheaper path than the one it claims to have measured.
    early = [
        result
        for result in answered
        if int(result["status"]) not in {200, SHED_STATUS} and not reached_the_pipeline(result)
    ]
    gates.append(
        Gate(
            "no refusals before the photo",
            not early,
            "== 0",
            str(len(early)),
            "; ".join(f"{r['status']} {r.get('error_code') or str(r.get('detail'))[:40]}" for r in early[:3])
            or "session, geofence, rate limit and liveness all stayed out of the way",
        )
    )

    # The jemalloc half of the memory work, as a gate: a burst must not become the new floor.
    # Read off the same sample the anonymous-set gate reads, so a platform without that
    # split compares what it does have.
    settled = report.settled.anon_last if anon_measured else report.settled.memory_last
    baseline = report.baseline.anon_last if anon_measured else report.baseline.memory_last
    growth = settled - baseline
    gates.append(
        Gate(
            "memory comes back",
            growth <= limits.recovery_bytes,
            f"<= +{limits.recovery_mib} MiB",
            f"{growth / MIB:+.0f} MiB after the burst",
            "idle vs settled: a spike that becomes the new floor is the next OOM's baseline",
        )
    )
    return gates


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def saturation_limits(args: argparse.Namespace) -> Limits:
    """The thresholds, assembled once so the table and the exit code cannot disagree."""
    return Limits(
        memory_mib=args.ceiling_mib,
        anon_mib=args.anon_ceiling_mib,
        # Latency and throttling are the container harness's to gate; a saturated run's
        # latencies are a queue statistic, and turning them into a threshold here would make
        # this gate fire for the state it deliberately creates.
        p95_seconds=float("inf"),
        p99_seconds=float("inf"),
        throttle_ratio=1.0,
        recovery_mib=args.recovery_mib,
        min_scored_fraction=args.min_pipeline,
    )


def drive(args: argparse.Namespace, sampler: Sampler, handle: AppHandle, roster: Sequence[Mapping[str, str]], jpeg: bytes, report: RunReport) -> None:
    """The load: ``--rounds`` rounds of ``--clients`` simultaneous punches.

    Rounds rather than one burst, because the failure this gate is about is cumulative:
    memory that grows with the number of *waiting* punches shows up as a floor that climbs
    round over round, and a single burst of the same total size would hide it. Each round
    keeps its own bracket, so the table reads as the shape of the run.
    """
    warm = asyncio_run(run_level(1, roster[-1:], jpeg, handle.base_url))[0]
    print(
        f"  warmup        : {warm['status']} in {warm['latency'] * 1000:.0f} ms "
        "(outside every bracket: it pays for the cold model, not the profile)"
    )
    for round_index in range(1, args.rounds + 1):
        sampler.begin(f"round {round_index}")
        started = time.perf_counter()
        results = asyncio_run(run_level(args.clients, roster, jpeg, handle.base_url))
        wall = time.perf_counter() - started
        window = sampler.end()
        report.levels.append(LevelResult(concurrency=args.clients, results=results, window=window, wall_seconds=wall))
        stats = report.levels[-1].stats
        statuses = report.levels[-1].statuses
        print(
            f"  round {round_index:>2}      : {len(results)} punches in {wall:>5.1f}s  "
            f"median {stats['median']:.2f}s  p95 {stats['p95']:.2f}s  "
            f"rss peak {window.peak_bytes / MIB:>4.0f} MiB  "
            f"anon {window.anon_max / MIB:>4.0f} MiB  "
            + " ".join(f"{key} x{value}" for key, value in sorted(statuses.items()))
        )


def asyncio_run(coroutine):
    """One event loop per round, closed before the next: a loop left running between
    brackets would keep client sockets and buffers alive inside the *driver*, where the
    child's numbers do not care but the harness's own footprint does."""
    import asyncio

    return asyncio.run(coroutine)


def render(report: RunReport, gates: Sequence[Gate], notes: Sequence[str], *, anon_measured: bool) -> str:
    """The run as text: what was measured, then what it means.

    The gate lines come from :meth:`Gate.line`, shared with the container harness, so a
    failure reads the same whichever instrument found it.
    """
    limits = report.limits
    measured = report.measured
    columns = ["", f"punch saturation - {report.profile}", f"  target        : {report.target}"]
    columns.append(f"  data          : {report.image or '-'}")
    columns.append(
        f"  idle baseline : anon {report.baseline.anon_last / MIB:.0f} MiB, "
        f"rss {report.baseline.memory_last / MIB:.0f} MiB over {report.baseline.seconds:.1f} s"
    )
    columns.append("")
    columns.append(f"  {'round':>5}  {'punches':>7}  {'statuses':<24}  {'wall':>7}  {'p95':>7}  {'rss max':>8}")
    columns.append("  " + "-" * 68)
    for index, level in enumerate(report.levels, start=1):
        statuses = " ".join(f"{key} x{value}" for key, value in sorted(level.statuses.items()))
        columns.append(
            f"  {index:>5}  {len(level.results):>7}  {statuses:<24}  {level.wall_seconds:>6.1f}s  "
            f"{level.stats['p95']:>6.2f}s  {level.window.memory_max / MIB:>6.0f} MiB"
        )
    columns.append("")
    columns.append(
        f"  whole run     : wall {measured.seconds:.1f} s, "
        f"rss max {measured.memory_max / MIB:.0f} MiB, kernel peak {measured.peak_bytes / MIB:.0f} MiB"
        + (f", anon max {measured.anon_max / MIB:.0f} MiB" if anon_measured else " (anon not separable here)")
    )
    growth = (report.settled.anon_last - report.baseline.anon_last) if anon_measured else (
        report.settled.memory_last - report.baseline.memory_last
    )
    columns.append(
        f"  recovery      : {'anon' if anon_measured else 'rss'} {growth / MIB:+.0f} MiB after "
        f"{report.settled.seconds:.1f} s of quiet"
    )
    if measured.usage_usec:
        columns.append(f"  cpu           : {measured.usage_usec / 1e6:.1f} s of process CPU over the run")
    for note in notes:
        columns.append(f"  note          : {note}")
    failures = [gate for gate in gates if not gate.ok]
    columns.append("")
    columns.append(f"  gates ({len(gates) - len(failures)}/{len(gates)} passed)")
    columns.append("  " + "-" * 66)
    columns.extend(gate.line() for gate in gates)
    columns.append("")
    columns.append(
        "  VERDICT: PASS - the process stayed under the ceiling"
        if not failures
        else "  VERDICT: FAIL - " + "; ".join(gate.name for gate in failures)
    )
    columns.append(
        f"  (limits: rss peak <= {limits.memory_mib} MiB, "
        f"anon <= {limits.anon_mib} MiB, recovery <= +{limits.recovery_mib} MiB, "
        f"pipeline >= {limits.min_scored_fraction * 100:.0f}%)"
    )
    return "\n".join(columns)


def run_local(args: argparse.Namespace) -> int:
    reader, platform_name, anon_measured = process_reader()
    data_dir = data_dir_for(args)
    limits = saturation_limits(args)
    mode, jpeg, face_note = resolve_selfie(args)

    print(f"  platform      : {platform_name}" + ("" if anon_measured else " (anonymous set not separable)"))
    print(f"  data          : {data_dir}")
    handle = start_app(args, data_dir)
    measured_pid, at_rest = resolve_app_pid(handle.process.pid, reader)
    handle.measured_pid = measured_pid
    print(f"  serving       : {handle.base_url} (pid {handle.process.pid}, log {handle.log_path.name})")
    print(
        f"  measured      : pid {measured_pid} at {at_rest.memory_bytes / MIB:.0f} MiB resident"
        + (
            f" - the application itself; pid {handle.process.pid} is the interpreter launcher in "
            "front of it, and a gate on that stub could never fail"
            if measured_pid != handle.process.pid
            else ""
        )
    )
    report = RunReport(
        target=handle.base_url,
        profile=f"host process, no cgroup cap; gates at {limits.memory_mib} MiB rss / {limits.anon_mib} MiB anon",
        image=str(data_dir),
        selfie=args.selfie or "",
        templates=args.templates,
        limits=limits,
    )
    notes = [
        f"faces: {mode} - {face_note}",
        f"templates: {args.templates}"
        + (" (punches are refused on the score, after the full pipeline)" if args.templates == "synthetic" else ""),
        "profile: " + ", ".join(f"{name}={value}" for name, value in sorted(DEPLOYMENT_ENV.items())),
        "no cgroup cap: crossing the ceiling is a prediction of the container's OOM, not the kill itself",
        "CPU throttling, memory.events and oom_kill belong to tools/capacity_test.py (needs docker)",
        f"measured process: pid {measured_pid}"
        + ("" if measured_pid == handle.process.pid else f", the child of launcher pid {handle.process.pid}"),
    ]
    try:
        seed = seed_roster(args, handle)
        print(f"  seeded        : {seed.seeded} accounts, {seed.cleared} cleared from a previous run")
        with ProcessSampler(measured_pid, args.interval, reader) as sampler:
            sampler.begin("baseline")
            time.sleep(args.baseline_seconds)
            report.baseline = sampler.end()
            drive(args, sampler, handle, seed.workers, jpeg, report)
            sampler.begin("settle")
            time.sleep(args.settle_seconds)
            report.settled = sampler.end()
        # Both halves: the launcher can outlive its child if the child is killed outright, and
        # a run that lost the process holding the models did not survive anything.
        survived = handle.alive() and still_readable(measured_pid, reader)
    finally:
        if not args.keep:
            handle.stop()

    report.notes = notes + [f"the application {'survived' if survived else 'DID NOT survive'} the run"]
    gates = evaluate_gates(report, limits, anon_measured=anon_measured)
    if not survived:
        gates.append(
            Gate(
                "process survived",
                False,
                f"pid {measured_pid} still serving",
                f"exit {handle.process.returncode}",
                "it died under load",
            )
        )
    else:
        gates.append(
            Gate(
                "process survived",
                True,
                f"pid {measured_pid} still serving",
                "yes",
                "the measured process was alive when the load ended",
            )
        )
    print(render(report, gates, report.notes, anon_measured=anon_measured))
    if args.report:
        payload = {
            "report": report.to_json(),
            "gates": [asdict(gate) for gate in gates],
            "measured_pid": measured_pid,
        }
        Path(args.report).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  report written to {args.report}")
    if args.keep:
        print(f"  kept          : {data_dir} (the child is still running on {handle.base_url})")
    return 0 if all(gate.ok for gate in gates) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Saturate the punch endpoint on this machine and gate the app process's peak RSS.",
        epilog="The container harness (tools/capacity_test.py) measures the cgroup itself: use it for the cap, throttling and OOM events.",
    )
    parser.add_argument("--clients", type=int, default=8, help="simultaneous punches per round (default 8: capacity 2 + queue 8 is saturated, nothing is shed)")
    parser.add_argument("--rounds", type=int, default=4, help="rounds of that load (default 4)")
    parser.add_argument("--users", type=int, default=None, help="accounts to seed (default: one per client, plus the warmup)")
    parser.add_argument("--interval", type=float, default=0.25, help="memory sampling interval, seconds (default 0.25)")
    parser.add_argument("--baseline-seconds", type=float, default=5.0, help="idle sampling before the load (default 5)")
    parser.add_argument("--settle-seconds", type=float, default=5.0, help="quiet sampling after the load (default 5)")
    parser.add_argument("--ceiling-mib", type=int, default=400, help="gate: peak rss ceiling in MiB (default 400, the 512 MiB instance's working line)")
    parser.add_argument("--anon-ceiling-mib", type=int, default=384, help="gate: anonymous-set ceiling in MiB (default 384, the same line capacity_test.py uses)")
    parser.add_argument("--recovery-mib", type=int, default=Limits.recovery_mib, help="gate: memory above the idle baseline allowed after the burst")
    parser.add_argument("--min-pipeline", type=float, default=0.5, help="gate: share of punches that must have reached the pipeline (default 0.5, for a deliberately oversubscribed run)")
    parser.add_argument("--faces", choices=("auto", "real", "none"), default="auto",
                        help="auto: a real face if one can be found, otherwise a face-free frame (default)")
    parser.add_argument("--selfie", default=None, help="a jpeg of a real face; every punch sends it")
    parser.add_argument("--templates", choices=("synthetic", "api"), default="synthetic",
                        help="synthetic: write a provenance-correct template (default, no enrollment); api: enroll through the deployment")
    parser.add_argument("--data-dir", default=None, help="where the child's database and file trees go (default: a fresh temp dir)")
    parser.add_argument("--port", type=int, default=None, help="port to serve on (default: a free one)")
    parser.add_argument("--startup-timeout", type=float, default=180.0, help="seconds to wait for the child to serve (default 180)")
    parser.add_argument("--report", default=None, help="write the run and its gates as JSON here")
    parser.add_argument("--keep", action="store_true", help="leave the child running and its data directory in place")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.clients < 1 or args.rounds < 1:
        raise CapacityError("--clients and --rounds have to be at least 1")
    return run_local(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CapacityError as error:
        print(f"\npunch saturation could not run: {error}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
