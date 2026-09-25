"""Capacity test the punch path inside a memory-capped container: does the box hold?

Why this exists
---------------
``backend/tools/load_test.py`` answers "how fast is a punch, and does the queue absorb a
burst" - it stubs the face models, so it measures HTTP + queue + SQLite and nothing about
memory. That is the wrong instrument for the question this deployment actually lost to: at
the 04:00 minibus the process allocated a decoded frame per queued job, RSS followed the
high-water mark of the busiest minute, and Railway's 512 MB / 1 vCPU instance with swap off
answered with an OOM kill. Latency was never the problem; the ceiling was.

So this tool measures the ceiling, and nothing else:

* it runs **the real image** (``Dockerfile``, jemalloc, real YuNet + FaceNet) in the
  **memory-capped** shape the instance has - ``--memory 512m --memory-swap 512m --cpus 1``,
  swap off, one process - and drives real punches at it from the host;
* it samples the container's own cgroup while it does: ``memory.current`` (what the kernel
  enforces), ``memory.stat/anon`` (the part of it that cannot be evicted), ``memory.peak``,
  ``memory.events`` (``high``/``max``/``oom``/``oom_kill``) and ``cpu.stat``
  (``nr_periods``/``nr_throttled``/``throttled_usec``);
* it walks ``2, 4, 6, 8, 10`` concurrent punches - one punch per seeded account, so no
  punch can be refused *before* the model by a session rule already being open;
* and it turns all of that into a **pass/fail gate table** with a non-zero exit code, so a
  regression in memory or a widening stall fails a pipeline instead of being noticed on a
  graph a week later.

Why the two numbers for memory
------------------------------
A cgroup's ``memory.current`` counts page cache. Reading an 87 MB ONNX model and 150 MB of
shared libraries puts that much on ``file``, and the kernel hands it straight back under
pressure - so a harness that gates on ``memory.current`` alone fails runs that were never
close to a problem, which is how a gate gets switched off and stops being a gate. The
identical trap on the other side is ``anon``: it is the part the kernel *cannot* evict, and
it is what actually decides whether the next burst is an OOM kill or another quiet minute.

Both are therefore gated, and the pair is the honest sentence: ``anon`` is how much memory
the process really holds, ``memory.current`` plus ``memory.events`` is what the kernel did
about it. On Linux the deployment's own numbers are the reference - the Windows working-set
ladder this was developed against (222 MB imported, 291 MB after a warm detect+align+embed,
flat at 298 MB over ten passes) counts image-backed pages as resident, so a Linux ``anon``
of the same build reads materially *lower*. The gates below are absolute ceilings, not
comparisons against that ladder.

The design, and why it is split in two processes
------------------------------------------------
The container serves; the host measures. A driver running inside the container would share
the 512 MB and the one vCPU it is trying to measure, and a client that competes for the core
inflates exactly the p95 the run is about. So:

* the **driver** (this process, on the host) builds and starts the container, seeds it,
  fires the punches, and prints the report. It needs ``httpx`` and ``docker``; it is the
  half that runs from a Windows checkout.
* the **sampler** (``--sample-stream``, the same file, ``docker exec``-ed into the
  container) reads the cgroup files and answers a tiny line protocol on stdin/stdout. It
  runs *inside* the target cgroup, so no host-side cgroup path has to be resolved - which is
  the only reason this works from a host where Docker runs in a VM and ``/proc/<pid>``
  belongs to Windows rather than to the container's kernel.

The protocol is deliberately not time-based. Docker Desktop's kernel clock and the host's
are different clocks, so brackets drawn with ``time.monotonic()`` on one side and compared
on the other would be meaningless. The driver instead says ``begin <label>`` / ``end`` and
the sampler takes the before/after readings itself, in the same clock, microseconds apart
from the traffic being attributed to them.

Seeding, and why the punches are *approved*
-------------------------------------------
The container boots on an empty ``/data`` and migrates itself, so nothing exists to punch
as. The seed (``--seed``, also run through ``docker exec``) inserts a geofence and N
accounts, and the host then **enrolls the selfie through the deployment's own
``/api/v1/worker/me/enroll``** - upload the photo, let the real liveness model and the real
embedding path make the template. That is not a shortcut around the application: it is the
application, and it means every punch matches its own template, is *approved*, and runs the
full path (frame stored, row written, notification fanned out) rather than a cheap refusal
that would make the memory numbers a lie. The seeded accounts carry the ``admin`` role only
because that is the role the self-enrollment endpoint admits (``main.SELF_ENROLL_ROLES``);
the punch itself takes any authenticated role.

``--templates synthetic`` skips the enrollment and writes a dimension/provenance-correct
template instead. That measures the pipeline and not the verdict - useful when the seed
selfie cannot pass the liveness gate the enrollment path rightly enforces - and the gate
table says which one ran, because an approved punch and a ``face_mismatch`` refusal are
both "the model ran" while neither is "the template is real".

Usage
-----
    # the whole thing: build, run capped, seed, sweep 2-10, gate (needs docker + a selfie)
    python backend/tools/capacity_test.py --selfie worker_photos/<a real face>.jpg

    # a different profile than the instance's
    python backend/tools/capacity_test.py --memory-mib 1024 --anon-ceiling-mib 768 --p95 3.0

    # an already-built image, levels of your choosing, machine-readable report
    python backend/tools/capacity_test.py --image attendance:latest --levels 2,4,8 --report run.json

    # re-judge a saved run with different limits - no docker, no network, no traffic
    python backend/tools/capacity_test.py --from-report run.json --p95 4.0

    # a deployment that is already running (Linux host, same cgroup): attach instead
    python backend/tools/capacity_test.py --target-url http://127.0.0.1:8000 \
        --cgroup-root /sys/fs/cgroup --credentials creds.json

Exit codes
----------
    0  every gate passed
    1  a gate failed (the table names it) - or the run could not be made at all
       (no docker, no selfie, the container never came healthy, enrollment refused)

Isolation
---------
Nothing here touches the live database, the live reference directory or a live deployment:
the container gets a private ``/data`` of its own, and the punches are filed inside it. The
host-side attachments (``--selfie``, ``--credentials``) are read, never written.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

MIB = 1024 * 1024

#: The cgroup v2 mount point; cgroup v1 lives at the same root with a different shape (see
#: :func:`read_cgroup`, which sniffs rather than being told).
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")

#: Where the image keeps this tool, for the ``docker exec`` invocations. WORKDIR is ``/app``
#: (see the Dockerfile) and ``COPY backend/ backend/`` travels, so this is the same file that
#: is running here - the sampler cannot drift from the driver.
TOOL_IN_IMAGE = "backend/tools/capacity_test.py"

#: The API prefix, spelled once and prepended to every route below. Each of the three paths
#: is *relative to it*: a constant that carried the prefix as well would compose into
#: ``/api/v1/api/v1/...``, which is a 404 at the health probe and therefore a run that never
#: starts - so there is exactly one place to get this right and one function that joins it.
API_PREFIX = "/api/v1"
#: The liveness route, ``GET``, unauthenticated (the Dockerfile's own HEALTHCHECK uses it).
STATUS_PATH = "/status"
#: The punch: one multipart POST per clock in or out.
PUNCH_PATH = "/attendance/verify"
#: Self-enrollment, ``POST``, role-gated by ``main.SELF_ENROLL_ROLES``. This is how the
#: harness gives every seeded account a *real* template (see ``enroll_workers``).
ENROLL_PATH = "/worker/me/enroll"


def api_url(base_url: str, path: str) -> str:
    """Join a base URL and one of the route constants above into a full URL."""
    return base_url.rstrip("/") + API_PREFIX + path

#: The coordinate the seeded geofence covers, and the one every punch reports. Matches the
#: convention the punch suites use (``harness.INSIDE_DOWNTOWN``) so a run of this tool and a
#: run of the suite are describing the same place.
SITE_NAME = "Capacity Harness Site"
SITE_LAT, SITE_LON, SITE_RADIUS_M = 30.05, 31.23, 100.0
INSIDE_SITE = f"{SITE_LAT},{SITE_LON}"

#: First id of the seeded workforce. High enough never to collide with a real account (the
#: console's ranges stop at 4999 and ``SEED_USERS`` uses small ids), so the seed can clear
#: exactly its own rows on a re-run and nothing else.
USER_ID_BASE = 91000

#: The prefix the in-container seed prints its JSON on. A marker rather than "the last line
#: of stdout" because importing the application on the way to writing a template can emit
#: warnings, and a harness that silently parses a warning as its roster fails in a way that
#: looks like a server problem.
SEED_JSON_PREFIX = "CAPACITY_SEED_JSON "

#: The event counters this harness compares between two readings. ``oom_kill`` is the kill;
#: ``max`` is the kernel having *reached* the limit, which is the same failure one allocator
#: call earlier - a run that trips it survived by luck, and gating on it is what makes the
#: gate about the ceiling rather than about the coin flip.
EVENT_KEYS = ("low", "high", "max", "oom", "oom_kill")

#: The keys ``memory.stat`` uses for the two halves that matter, v2 first and v1 second.
#: ``anon``/``total_rss`` is the un-evictable set; ``file``/``total_cache`` is page cache.
ANON_STAT_KEYS = ("anon", "total_rss")
FILE_STAT_KEYS = ("file", "total_cache")


class CapacityError(RuntimeError):
    """The run could not be made. Always a message for a person, never a traceback."""


# --------------------------------------------------------------------------- #
# The gates, as data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Limits:
    """Every threshold in one place, so the table and the exit code cannot disagree.

    Defaults are the 512 MB instance profile. ``memory_bytes`` is deliberately *under* the
    cap: a run that peaks within a percent of the limit has no headroom left for the next
    arrival, and a gate that only fires once the kernel has already decided is a gate that
    reports history.
    """

    #: ``memory.current`` ceiling (MiB of the container's cap, page cache included).
    memory_mib: int = 460
    #: ``memory.stat anon`` ceiling - the part that cannot be reclaimed.
    anon_mib: int = 384
    #: Per-level p95 and p99 ceilings, in seconds.
    p95_seconds: float = 2.5
    p99_seconds: float = 4.0
    #: Share of wall time the cgroup may spend throttled before the profile is too small.
    throttle_ratio: float = 0.60
    #: How far above the idle baseline RSS may sit *after* the last level. This is the
    #: jemalloc half of the fix: a spike must not become the new floor.
    recovery_mib: int = 64
    #: Share of punches that must prove they reached the model. 1.0 because the container run
    #: gives every punch its own account; attach mode, where accounts are cycled and a second
    #: punch from one account is refused by a session rule, has to lower it and say so.
    min_scored_fraction: float = 1.0

    @property
    def memory_bytes(self) -> int:
        return self.memory_mib * MIB

    @property
    def anon_bytes(self) -> int:
        return self.anon_mib * MIB

    @property
    def recovery_bytes(self) -> int:
        return self.recovery_mib * MIB


@dataclass(frozen=True)
class Gate:
    """One line of the verdict: what was asked, what was seen, and whether that passes."""

    name: str
    ok: bool
    limit: str
    observed: str
    detail: str = ""

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        text = f"  [{mark}] {self.name:<22} limit {self.limit:<14} observed {self.observed}"
        return f"{text}\n           {self.detail}" if self.detail else text


# --------------------------------------------------------------------------- #
# Reading a cgroup
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CgroupReading:
    """One sample of the target cgroup. Every field is absolute; deltas are the window's job."""

    memory_bytes: int
    anon_bytes: int
    file_bytes: int
    peak_bytes: int
    events: Mapping[str, int]
    usage_usec: int
    nr_periods: int
    nr_throttled: int
    throttled_usec: int


def parse_stat_lines(text: str) -> dict[str, int]:
    """``memory.stat`` / ``memory.events`` / ``cpu.stat``: ``key value`` per line.

    Tolerant on purpose. These files grow fields between kernel versions and carry
    hierarchies in the cgroup's own labels; a parser that raised on a line it did not
    recognise would make this harness version-sensitive for no gain, and a parser that
    guessed would invent numbers. Unknown keys are kept, unparseable lines are skipped.
    """
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return values


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _first_stat(stat: Mapping[str, int], keys: Sequence[str]) -> int:
    for key in keys:
        if key in stat:
            return int(stat[key])
    return 0


def read_cgroup(root: Path) -> CgroupReading:
    """The target cgroup's memory and CPU state, from a v2 or a v1 hierarchy.

    v2 is what every current kernel, Docker and Railway use; the v1 branch is here because
    the same tool is documented for a host that attaches to its own cgroup root, and a
    harness that reports zeros on the older shape is worse than one that says so. The two
    differ in three places that matter and are handled explicitly: the memory files move
    under ``memory/``, ``throttled_time`` is nanoseconds where v2 calls the same counter
    ``throttled_usec``, and the "hit the limit" counter is ``memory.failcnt`` rather than an
    ``events`` line.
    """
    v2_memory = root / "memory.current"
    if v2_memory.exists():
        memory_bytes = _read_int(v2_memory) or 0
        peak_bytes = _read_int(root / "memory.peak") or memory_bytes
        stat = parse_stat_lines(_read_text(root / "memory.stat") or "")
        events = parse_stat_lines(_read_text(root / "memory.events") or "")
        cpu = parse_stat_lines(_read_text(root / "cpu.stat") or "")
        return CgroupReading(
            memory_bytes=memory_bytes,
            anon_bytes=_first_stat(stat, ANON_STAT_KEYS),
            file_bytes=_first_stat(stat, FILE_STAT_KEYS),
            peak_bytes=peak_bytes,
            events=events,
            usage_usec=int(cpu.get("usage_usec", 0)),
            nr_periods=int(cpu.get("nr_periods", 0)),
            nr_throttled=int(cpu.get("nr_throttled", 0)),
            throttled_usec=int(cpu.get("throttled_usec", 0)),
        )

    v1 = root / "memory"
    usage = _read_int(v1 / "memory.usage_in_bytes")
    if usage is None:
        raise CapacityError(
            f"neither a cgroup v2 nor a cgroup v1 hierarchy was found under {root}: "
            "expected memory.current (v2) or memory/memory.usage_in_bytes (v1)"
        )
    stat = parse_stat_lines(_read_text(v1 / "memory.stat") or "")
    events = dict(parse_stat_lines(_read_text(v1 / "memory.oom_control") or ""))
    failcnt = _read_int(v1 / "memory.failcnt")
    if failcnt is not None:
        # v1's name for "the limit was reached", which v2 reports as events.max.
        events["max"] = failcnt
    cpu = parse_stat_lines(_read_text(root / "cpu.stat") or "")
    throttled_usec = int(cpu.get("throttled_usec", 0)) or int(cpu.get("throttled_time", 0)) // 1000
    usage_usec = int(cpu.get("usage_usec", 0))
    if not usage_usec:
        # v1 keeps cumulative CPU time in cpuacct, in nanoseconds.
        usage_ns = _read_int(root / "cpuacct" / "cpuacct.usage")
        usage_usec = (usage_ns or 0) // 1000
    return CgroupReading(
        memory_bytes=usage,
        anon_bytes=_first_stat(stat, ANON_STAT_KEYS),
        file_bytes=_first_stat(stat, FILE_STAT_KEYS),
        peak_bytes=_read_int(v1 / "memory.max_usage_in_bytes") or usage,
        events=events,
        usage_usec=usage_usec,
        nr_periods=int(cpu.get("nr_periods", 0)),
        nr_throttled=int(cpu.get("nr_throttled", 0)),
        throttled_usec=throttled_usec,
    )


# --------------------------------------------------------------------------- #
# A window: what a level cost
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    """One bracketed stretch of time (a level, the baseline, the settle), as deltas.

    ``memory_max``/``anon_max`` are sampled maxima and ``peak_bytes`` is the cgroup's own
    lifetime high-water mark, which no sampling interval can miss. Both are reported because
    they answer different questions: the maximum is "how close did it get while I watched",
    and the peak is "how close did it get at all".
    """

    label: str
    seconds: float
    samples: int
    memory_max: int
    memory_last: int
    anon_max: int
    anon_last: int
    file_last: int
    peak_bytes: int
    events: Mapping[str, int]
    usage_usec: int
    nr_periods: int
    nr_throttled: int
    throttled_usec: int

    @property
    def throttle_ratio(self) -> float:
        """Throttled microseconds per wall microsecond. 0.6 is "the core was not there"."""
        window_usec = max(self.seconds, 1e-9) * 1_000_000
        return self.throttled_usec / window_usec

    def mib(self, value: int) -> float:
        return value / MIB


def summarise_window(label: str, samples: Sequence[CgroupReading], seconds: float) -> Window:
    """Fold the readings taken inside one bracket into the window they describe.

    The first sample is the reading taken *at* ``begin`` and the last the one taken *at*
    ``end``, so the event and CPU deltas span exactly the traffic between them. The maxima
    run over the periodic samples in between as well, so a spike shorter than the sampling
    interval still lands in ``memory_max`` - and in ``peak_bytes`` whatever its length.
    """
    if not samples:
        raise CapacityError(f"no cgroup readings were taken for the {label!r} window")
    first, last = samples[0], samples[-1]

    def delta(key: str) -> int:
        # Clamped at zero: a counter that went *down* can only be a file that was replaced
        # under us (a container restart, a different cgroup). Reporting a negative "OOM
        # kills" would be nonsense, and reporting the drop as a delta would hide the restart.
        return max(0, int(last.events.get(key, 0)) - int(first.events.get(key, 0)))

    return Window(
        label=label,
        seconds=float(seconds),
        samples=len(samples),
        memory_max=max(reading.memory_bytes for reading in samples),
        memory_last=last.memory_bytes,
        anon_max=max(reading.anon_bytes for reading in samples),
        anon_last=last.anon_bytes,
        file_last=last.file_bytes,
        peak_bytes=max(reading.peak_bytes for reading in samples),
        events={key: delta(key) for key in EVENT_KEYS},
        usage_usec=max(0, last.usage_usec - first.usage_usec),
        nr_periods=max(0, last.nr_periods - first.nr_periods),
        nr_throttled=max(0, last.nr_throttled - first.nr_throttled),
        throttled_usec=max(0, last.throttled_usec - first.throttled_usec),
    )


def combine_windows(label: str, windows: Sequence[Window]) -> Window:
    """One window describing several: totals for the deltas, the worst for the maxima.

    Used for the whole-run gates, where the question is "did *any* moment of this run touch
    the wall" - so the maxima are maxima and the counters are sums.
    """
    if not windows:
        raise CapacityError(f"no windows to combine for {label!r}")
    return Window(
        label=label,
        seconds=sum(window.seconds for window in windows),
        samples=sum(window.samples for window in windows),
        memory_max=max(window.memory_max for window in windows),
        memory_last=windows[-1].memory_last,
        anon_max=max(window.anon_max for window in windows),
        anon_last=windows[-1].anon_last,
        file_last=windows[-1].file_last,
        peak_bytes=max(window.peak_bytes for window in windows),
        events={key: sum(int(w.events.get(key, 0)) for w in windows) for key in EVENT_KEYS},
        usage_usec=sum(window.usage_usec for window in windows),
        nr_periods=sum(window.nr_periods for window in windows),
        nr_throttled=sum(window.nr_throttled for window in windows),
        throttled_usec=sum(window.throttled_usec for window in windows),
    )


# --------------------------------------------------------------------------- #
# The sampler, local and over docker exec
# --------------------------------------------------------------------------- #
class Sampler:
    """``begin`` / ``end`` around a level, wherever the cgroup is being read from."""

    def start(self) -> None:
        """Bring the sampling up. A no-op where the readings are taken on demand."""

    def begin(self, label: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def end(self) -> Window:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "Sampler":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class LocalSampler(Sampler):
    """Reads a cgroup root from this process, on its own thread.

    The thread is the whole implementation: a punch level is spent *waiting* for the
    server, so a sampler that only read between levels would miss the spike the level was
    made of. ``begin`` takes its reading synchronously so the bracket is exact, then the
    thread fills in between and ``end`` reads again.
    """

    def __init__(self, root: Path, interval: float) -> None:
        self.root = root
        self.interval = interval
        self._lock = threading.Lock()
        self._readings: list[tuple[float, CgroupReading]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._label = ""
        self._start = 0

    def _record(self) -> CgroupReading:
        reading = read_cgroup(self.root)
        with self._lock:
            self._readings.append((time.monotonic(), reading))
        return reading

    def start(self) -> None:
        self._record()
        self._thread = threading.Thread(target=self._loop, name="cgroup-sampler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._record()
            except CapacityError:
                # A read failure between levels must not kill the thread mid-run: the next
                # tick retries, and a cgroup that is genuinely gone fails loudly at the
                # bracket instead of silently reporting zeros.
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


class RemoteSampler(Sampler):
    """The sampler ``docker exec``-ed into the container, spoken to over its stdin.

    Docker Desktop runs the container in a VM, so the container's cgroup is not reachable
    from this side of the boundary as files and ``time.monotonic()`` on the two sides is two
    different clocks. Both problems are answered the same way: the brackets are taken by the
    process that is *inside* the cgroup, and this class only asks for them.
    """

    def __init__(self, container: str, interval: float, cgroup_root: str = str(DEFAULT_CGROUP_ROOT)) -> None:
        self.container = container
        self.interval = interval
        self.cgroup_root = cgroup_root
        self._process: subprocess.Popen[str] | None = None
        self._stderr: list[str] = []

    def start(self) -> None:
        """Spawn the in-container sampler. Lazy: the first ``begin`` does it (see ``_ask``)."""

    def _spawn(self) -> subprocess.Popen[str]:
        command = [
            "docker", "exec", "-i", self.container,
            "python", TOOL_IN_IMAGE,
            "--sample-stream", "--interval", str(self.interval),
            "--cgroup-root", self.cgroup_root,
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:  # pragma: no cover - docker is checked earlier
            raise CapacityError("the `docker` command is not on PATH") from exc
        return self._process

    def _ask(self, line: str) -> Mapping[str, Any]:
        process = self._process or self._spawn()
        if process.stdin is None or process.stdout is None:
            raise CapacityError("the in-container sampler has no stdin/stdout")
        process.stdin.write(line + "\n")
        process.stdin.flush()
        while True:
            raw = process.stdout.readline()
            if not raw:
                self._drain_stderr(process)
                raise CapacityError(
                    "the in-container sampler exited or stopped answering "
                    f"(after {line!r}). stderr:\n" + "".join(self._stderr[-20:])
                )
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            return payload

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        with contextlib.suppress(Exception):
            if process.poll() is not None and process.stderr is not None:
                self._stderr.extend(process.stderr.read().splitlines())

    def begin(self, label: str) -> None:
        self._ask(f"begin {label}")

    def end(self) -> Window:
        payload = self._ask("end")
        if payload.get("event") != "window" or not isinstance(payload.get("window"), dict):
            raise CapacityError(f"the in-container sampler answered {payload!r} instead of a window")
        # The window is nested rather than spread beside the event tag, so the frame is
        # self-describing *and* the payload is exactly a ``Window``: the sampler cannot add a
        # field here without this line failing, which is the point.
        return Window(**payload["window"])

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        with contextlib.suppress(Exception):
            self._ask("quit")
        with contextlib.suppress(Exception):
            process.stdin.close() if process.stdin else None
        with contextlib.suppress(Exception):
            process.wait(timeout=10)
        with contextlib.suppress(Exception):
            process.kill()
        self._process = None


def stream_cgroup_samples(interval: float, cgroup_root: Path) -> int:
    """``--sample-stream``: the in-container half. Reads the cgroup, answers on stdout.

    Input is one command per line - ``begin <label>``, ``end``, ``quit`` - and every answer
    is a single JSON object on one line, so a reader can skip anything it does not
    understand (uvicorn warnings from a parent process sharing the pipe, for instance).

    The label is split off with ``maxsplit=1`` because a label is *phrasing*, not a token:
    the driver labels a level ``concurrency 4``, and a parser that split on whitespace
    everywhere would record it as a window called ``concurrency``.
    """
    sampler = LocalSampler(cgroup_root, interval)
    sampler.start()
    emit = lambda payload: (sys.stdout.write(json.dumps(payload) + "\n"), sys.stdout.flush())  # noqa: E731
    try:
        for line in sys.stdin:
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            command, label = parts[0], (parts[1].strip() if len(parts) > 1 else "")
            if command == "begin":
                sampler.begin(label or "window")
                emit({"event": "begin", "label": label or "window"})
            elif command == "end":
                emit({"event": "window", "window": asdict(sampler.end())})
            elif command in {"quit", "exit"}:
                emit({"event": "bye"})
                return 0
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C while attached
        return 0
    finally:
        sampler.close()
    return 0


# --------------------------------------------------------------------------- #
# Driving punches
# --------------------------------------------------------------------------- #
def _load_httpx():
    """The HTTP client, whichever name this checkout installed it under.

    Same reason and same shape as ``tools/load_test.py``: the development venv carries
    ``httpx2``, and a host that has the original should not have to install anything. The
    import is inside a function because this module's measurement half - the cgroup readers
    and the gates - is imported by the test suite, which should not need an HTTP client to
    check a threshold.
    """
    try:
        import httpx

        return httpx
    except ModuleNotFoundError:
        import httpx2 as httpx  # type: ignore[no-redef]

        return httpx


async def punch(client, base_url: str, worker: Mapping[str, str], jpeg: bytes, action: str) -> dict[str, Any]:
    """One timed punch. A transport failure is status 0, never an exception.

    ``error_code`` is lifted out of the body because it is what tells a real measurement
    from a cheap one: a punch refused *before* the model (no reference, liveness, geofence)
    costs nothing and would make the memory numbers look wonderful, and the gate table
    asserts that every punch reached the scoring step instead.

    ``retry_after`` is recorded for the one refusal this tool does *not* gate away: a
    saturated engine answers 503 + Retry-After (``face_engine.busy_http_exception``), and a
    run that deliberately drives the endpoint past its queue has to be able to tell that
    designed answer from a bare 503 - see ``tools/punch_saturation.py``.
    """
    started = time.perf_counter()
    try:
        response = await client.post(
            api_url(base_url, PUNCH_PATH),
            data={"worker_id": worker["user_id"], "action": action, "location_input": INSIDE_SITE},
            files={"selfie": ("selfie.jpg", jpeg, "image/jpeg")},
            headers={"Authorization": f"Bearer {worker['token']}"},
        )
        code, detail = "", ""
        try:
            body = response.json()
            payload = body.get("detail") if isinstance(body, dict) else None
            if isinstance(payload, dict):
                code = str(payload.get("error_code") or "")
                detail = str(payload.get("message") or "")[:160]
            elif payload:
                detail = str(payload)[:160]
            elif isinstance(body, dict):
                detail = str(body.get("status") or body.get("message") or "")[:160]
        except Exception:  # noqa: BLE001 - a non-JSON body is not a transport failure
            detail = str(response.text)[:160]
        return {
            "status": response.status_code,
            "latency": time.perf_counter() - started,
            "error_code": code,
            "retry_after": response.headers.get("retry-after", ""),
            "detail": detail,
        }
    except Exception as exc:  # noqa: BLE001 - a dead connection is a result too
        return {
            "status": 0,
            "latency": time.perf_counter() - started,
            "error_code": "",
            "retry_after": "",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
        }


async def run_level(
    level: int, roster: Sequence[Mapping[str, str]], jpeg: bytes, base_url: str, action: str = "Clock In"
) -> list[dict[str, Any]]:
    """``level`` punches submitted at the same instant - that simultaneity is the test.

    Connections are pooled wide enough that every punch is genuinely in flight at once: a
    client limit below the level would measure the client's own queueing and report it as
    the server's latency.
    """
    httpx = _load_httpx()
    workers = list(roster[:level])
    if len(workers) < level:  # pragma: no cover - the caller seeds enough for every level
        raise CapacityError(f"level {level} needs {level} accounts, the roster has {len(workers)}")
    limits = httpx.Limits(max_connections=max(level * 2, 16), max_keepalive_connections=level)
    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        return list(
            await asyncio.gather(*(punch(client, base_url, worker, jpeg, action) for worker in workers))
        )


def percentile(values: Iterable[float], pct: float) -> float:
    """Nearest-rank percentile: the value at or above ``pct``% of the samples.

    Nearest-rank rather than an interpolated quantile because the audience for this number
    is a decision ("will the tenth worker wait under 2.5 s"), and the tenth worker's own
    latency is a better answer than a point between two of them.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def latency_stats(results: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """min / median / p95 / p99 / max over a level's punches, in seconds."""
    latencies = [float(result["latency"]) for result in results]
    if not latencies:
        return {"min": 0.0, "median": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "min": min(latencies),
        "median": statistics.median(latencies),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": max(latencies),
    }


#: How a punch proves it was answered by the model rather than by a gate in front of it.
#: An allowlist of positive outcomes, not "anything without an error code": several of the
#: *cheap* refusals (a geofence rejection, "Already clocked in!") are plain sentences with no
#: error code at all, and a rule that let them through would report a wonderfully cheap
#: pipeline that never ran. Two shapes qualify:
#:
#: * ``200`` - the punch was scored and approved or flagged for review;
#: * ``422`` with ``face_mismatch`` - the punch was scored and the score disagreed. Written by
#:   ``main.verify_worker`` only after detection, alignment, the embedding and the cosine.
SCORED_STATUS = 200
SCORED_ERROR_CODE = "face_mismatch"


def reached_the_model(result: Mapping[str, Any]) -> bool:
    """Whether this punch's latency and memory are the pipeline's, not a gate's."""
    status = int(result.get("status") or 0)
    if status == SCORED_STATUS:
        return True
    return status == 422 and result.get("error_code") == SCORED_ERROR_CODE


def punches_that_reached_the_model(results: Sequence[Mapping[str, Any]]) -> int:
    """How many punches were answered by the model rather than by a gate in front of it."""
    return sum(1 for result in results if reached_the_model(result))


# --------------------------------------------------------------------------- #
# The report and the verdict
# --------------------------------------------------------------------------- #
@dataclass
class LevelResult:
    concurrency: int
    results: list[dict[str, Any]]
    window: Window
    wall_seconds: float

    @property
    def stats(self) -> dict[str, float]:
        return latency_stats(self.results)

    @property
    def statuses(self) -> dict[str, int]:
        counted: dict[str, int] = {}
        for result in self.results:
            key = str(result["status"]) if result["status"] else "conn-error"
            counted[key] = counted.get(key, 0) + 1
        return counted

    @property
    def scored(self) -> int:
        return punches_that_reached_the_model(self.results)


@dataclass
class RunReport:
    """Everything a verdict needs, in a form that survives a JSON round trip.

    Serializable on purpose: ``--report run.json`` writes one and ``--from-report`` re-judges
    it with different limits, which is how a gate gets argued about with the same numbers
    instead of with a re-run whose traffic, cache state and neighbours all differ.
    """

    target: str
    profile: str
    image: str = ""
    selfie: str = ""
    templates: str = "api"
    limits: Limits = field(default_factory=Limits)
    baseline: Window = None  # type: ignore[assignment]
    levels: list[LevelResult] = field(default_factory=list)
    settled: Window = None  # type: ignore[assignment]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "profile": self.profile,
            "image": self.image,
            "selfie": self.selfie,
            "templates": self.templates,
            "limits": asdict(self.limits),
            "baseline": asdict(self.baseline),
            "levels": [
                {
                    "concurrency": level.concurrency,
                    "wall_seconds": level.wall_seconds,
                    "window": asdict(level.window),
                    "results": level.results,
                }
                for level in self.levels
            ],
            "settled": asdict(self.settled),
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "RunReport":
        limits = Limits(**payload.get("limits", {}))
        return cls(
            target=payload["target"],
            profile=payload.get("profile", ""),
            image=payload.get("image", ""),
            selfie=payload.get("selfie", ""),
            templates=payload.get("templates", "api"),
            limits=limits,
            baseline=Window(**payload["baseline"]),
            settled=Window(**payload["settled"]),
            levels=[
                LevelResult(
                    concurrency=entry["concurrency"],
                    wall_seconds=entry["wall_seconds"],
                    window=Window(**entry["window"]),
                    results=list(entry["results"]),
                )
                for entry in payload.get("levels", [])
            ],
            notes=list(payload.get("notes", [])),
        )

    @property
    def measured(self) -> Window:
        """The whole run as one window: baseline through settle, maxima worst-case."""
        return combine_windows("run", [self.baseline, self.settled, *[level.window for level in self.levels]])

    @property
    def all_results(self) -> list[dict[str, Any]]:
        return [result for level in self.levels for result in level.results]


def evaluate_gates(report: RunReport, limits: Limits) -> list[Gate]:
    """The verdict: one :class:`Gate` per promise the profile makes.

    Pure and total - no clock, no network, no container - because a threshold that can only
    be checked by paying for a container is a threshold nobody checks. The suite pins this
    function against synthetic reports (``tests/test_capacity_gates.py``), so a gate that
    changed its mind would fail in a second rather than in a deploy.
    """
    measured = report.measured
    gates: list[Gate] = []

    def mib(value: int) -> str:
        return f"{value / MIB:.0f} MiB"

    # ``max`` of the two views, because they fail differently: ``memory_max`` is the highest
    # reading the sampler caught and ``peak_bytes`` is the cgroup's own lifetime high-water
    # mark, which no sampling interval can miss. Gating on the sampler's number alone would
    # make the strictness of this gate a function of ``--interval``.
    peak = max(measured.memory_max, measured.peak_bytes)
    gates.append(
        Gate(
            "memory ceiling",
            peak <= limits.memory_bytes,
            f"<= {limits.memory_mib} MiB",
            mib(peak),
            "memory.current, including reclaimable page cache: this is the kernel's view",
        )
    )
    gates.append(
        Gate(
            "anonymous set",
            measured.anon_max <= limits.anon_bytes,
            f"<= {limits.anon_mib} MiB",
            mib(measured.anon_max),
            "memory.stat anon: the memory the kernel cannot evict",
        )
    )
    gates.append(
        Gate(
            "no OOM kill",
            int(measured.events.get("oom_kill", 0)) == 0,
            "== 0",
            str(int(measured.events.get("oom_kill", 0))),
            "memory.events oom_kill across the whole run",
        )
    )
    gates.append(
        Gate(
            "limit never reached",
            int(measured.events.get("max", 0)) == 0,
            "== 0",
            str(int(measured.events.get("max", 0))),
            "memory.events max: the hit that came one allocation before a kill",
        )
    )

    total_punches = len(report.all_results)
    scored = punches_that_reached_the_model(report.all_results)
    gates.append(
        Gate(
            "punches reach the model",
            total_punches > 0 and scored / total_punches >= limits.min_scored_fraction,
            f">= {limits.min_scored_fraction * 100:.0f}%",
            f"{scored / total_punches * 100:.0f}%" if total_punches else "no punches",
            "refusals before the model (liveness, geofence, no template) measure nothing",
        )
    )

    server_errors = [result for result in report.all_results if int(result["status"]) >= 500]
    transport = [result for result in report.all_results if int(result["status"]) == 0]
    busy = [result for result in report.all_results if int(result["status"]) == 503]
    gates.append(
        Gate(
            "no transport errors",
            not transport,
            "== 0",
            str(len(transport)),
            "a dead connection is a result too, and this one is a failure",
        )
    )
    gates.append(
        Gate(
            "no server errors",
            not server_errors,
            "== 0",
            str(len(server_errors)),
            "; ".join(f"{r['status']} {r['detail'][:60]}" for r in server_errors[:3]),
        )
    )
    gates.append(
        Gate(
            "queue absorbed the burst",
            not busy,
            "== 0 x 503",
            f"{len(busy)} x 503",
            "the engine answers 503 + Retry-After past its queue; a punch must not meet it",
        )
    )

    for level in report.levels:
        stats = level.stats
        gates.append(
            Gate(
                f"p95 @ {level.concurrency} concurrent",
                stats["p95"] <= limits.p95_seconds,
                f"<= {limits.p95_seconds:.2f} s",
                f"{stats['p95']:.2f} s",
                f"median {stats['median']:.2f} s, max {stats['max']:.2f} s over {level.concurrency} punches",
            )
        )
        gates.append(
            Gate(
                f"p99 @ {level.concurrency} concurrent",
                stats["p99"] <= limits.p99_seconds,
                f"<= {limits.p99_seconds:.2f} s",
                f"{stats['p99']:.2f} s",
            )
        )

    # The worst level, and where it happened: a rate limiter, a lock or an under-provisioned
    # core all show up as throttling, and which level it started at is the first thing worth
    # knowing about it.
    worst = max(report.levels, key=lambda level: level.window.throttle_ratio, default=None)
    worst_ratio = worst.window.throttle_ratio if worst is not None else 0.0
    worst_detail = (
        f"worst at {worst.concurrency} concurrent: {worst.window.nr_throttled} throttled periods, "
        f"{worst.window.throttled_usec / 1000:.0f} ms of {worst.window.seconds:.1f} s wall"
        if worst is not None
        else "no levels were run"
    )
    gates.append(
        Gate(
            "CPU not starved",
            # The worst *level*, not the whole run: the run includes ten seconds of idle
            # baseline and ten of quiet afterwards, and averaging throttling across them
            # would let the busiest level of the day hide behind the emptiest minute of it.
            worst_ratio <= limits.throttle_ratio,
            f"<= {limits.throttle_ratio:.2f}",
            f"{worst_ratio:.2f}" if report.levels else "no levels",
            worst_detail,
        )
    )
    gates.append(
        Gate(
            "memory returns after the burst",
            report.settled.anon_last <= report.baseline.anon_last + limits.recovery_bytes,
            f"<= baseline + {limits.recovery_mib} MiB",
            mib(report.settled.anon_last),
            f"baseline {mib(report.baseline.anon_last)}: a spike must not become the new floor",
        )
    )
    return gates


def render(report: RunReport, gates: Sequence[Gate]) -> str:
    """The run, as text: what was measured, then what it means."""
    limits = report.limits
    lines: list[str] = []
    lines.append("")
    lines.append(f"capacity test - {report.profile}")
    lines.append(f"  target        : {report.target}")
    if report.image:
        lines.append(f"  image         : {report.image}")
    if report.selfie:
        lines.append(f"  selfie        : {report.selfie} (templates: {report.templates})")
    lines.append(
        f"  idle baseline : anon {report.baseline.anon_last / MIB:.0f} MiB, "
        f"current {report.baseline.memory_last / MIB:.0f} MiB over {report.baseline.seconds:.1f} s"
    )
    lines.append("")
    header = (
        f"  {'level':>5}  {'punches':>7}  {'statuses':<22}  {'median':>7}  {'p95':>7}  "
        f"{'p99':>7}  {'anon max':>9}  {'throttled':>9}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for level in report.levels:
        stats = level.stats
        statuses = " ".join(f"{key} x{value}" for key, value in sorted(level.statuses.items()))
        lines.append(
            f"  {level.concurrency:>5}  {len(level.results):>7}  {statuses:<22}  "
            f"{stats['median']:>6.2f}s  {stats['p95']:>6.2f}s  {stats['p99']:>6.2f}s  "
            f"{level.window.anon_max / MIB:>7.0f} MiB  {level.window.throttle_ratio * 100:>7.1f} %"
        )
    lines.append("")
    measured = report.measured
    lines.append(
        f"  whole run     : wall {measured.seconds:.1f} s, anon max {measured.anon_max / MIB:.0f} MiB, "
        f"current max {measured.memory_max / MIB:.0f} MiB, kernel peak {measured.peak_bytes / MIB:.0f} MiB"
    )
    lines.append(
        f"  recovery      : anon {report.settled.anon_last / MIB:.0f} MiB after the last level "
        f"({report.settled.seconds:.1f} s of quiet), vs {report.baseline.anon_last / MIB:.0f} MiB idle"
    )
    if report.notes:
        for note in report.notes:
            lines.append(f"  note          : {note}")
    failures = [gate for gate in gates if not gate.ok]
    lines.append("")
    lines.append(f"  gates ({len(gates) - len(failures)}/{len(gates)} passed)")
    lines.append("  " + "-" * 66)
    for gate in gates:
        lines.append(gate.line())
    lines.append("")
    lines.append(
        "  VERDICT: PASS - the profile holds"
        if not failures
        else "  VERDICT: FAIL - " + "; ".join(gate.name for gate in failures)
    )
    lines.append(
        f"  (limits: anon <= {limits.anon_mib} MiB, current <= {limits.memory_mib} MiB, "
        f"p95 <= {limits.p95_seconds:.2f} s, p99 <= {limits.p99_seconds:.2f} s, "
        f"throttled <= {limits.throttle_ratio * 100:.0f} %)"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Seeding, from inside the container
# --------------------------------------------------------------------------- #
@dataclass
class SeedResult:
    workers: list[dict[str, str]]
    site: dict[str, Any]
    seeded: int
    cleared: int
    templates: str


def seed_inside(args: argparse.Namespace) -> int:
    """``--seed``: accounts, a geofence and (optionally) templates, inside the container.

    Run through ``docker exec`` against a deployment that is already serving, so all this
    does is write rows and files the application then reads - no model, no queue, no port.
    Imports the application's own modules for the two things that must not be re-derived:
    the biometric id format (``biometrics.new_id``) and the template's provenance, which is
    the pair ``(pipeline, model)`` a punch checks before it will score anything.

    The accounts carry the ``admin`` role because ``/worker/me/enroll`` - the endpoint the
    host half uses to make real templates - admits only ``main.SELF_ENROLL_ROLES``. Nothing
    in the punch path treats the role differently: ``/attendance/verify`` takes any
    authenticated role.
    """
    sys.path.insert(0, str(BACKEND_DIR))
    import sqlite3

    import biometrics  # noqa: E402 - the path above has to exist first
    import config  # noqa: E402
    import database  # noqa: E402
    import migrations  # noqa: E402
    import security  # noqa: E402

    settings = config.settings
    database.configure(repair=True)
    connection = database.connect(isolation_level=None)
    try:
        # Idempotent by design: this is the same call the server makes at boot, so seeding a
        # running deployment neither re-migrates nor re-bootstraps anything.
        migrations.initialize(connection)

        base = args.user_id_base
        workers: list[dict[str, str]] = []
        cleared = 0

        # Clear this tool's own previous rows first, so a second run is a first run. Only the
        # id range this tool owns is touched - a harness that truncated the table would be a
        # harness nobody dares run twice.
        rows = connection.execute(
            # By index, not by name: whether the row factory is ``sqlite3.Row`` is
            # ``database``'s decision, and this reads two columns from a table whose schema
            # is not this file's.
            "SELECT id, biometric_id FROM users WHERE CAST(id AS INTEGER) BETWEEN ? AND ?",
            (base + 1, base + max(args.users, 0) + 64),
        ).fetchall()
        for row in rows:
            cleared += 1
            with contextlib.suppress(OSError):
                (Path(settings.local_refs_dir) / f"{row[1]}.json").unlink()
        connection.execute(
            "DELETE FROM users WHERE CAST(id AS INTEGER) BETWEEN ? AND ?",
            (base + 1, base + max(args.users, 0) + 64),
        )
        connection.execute(
            "DELETE FROM active_sessions WHERE CAST(worker_id AS INTEGER) BETWEEN ? AND ?",
            (base + 1, base + max(args.users, 0) + 64),
        )
        connection.execute(
            "DELETE FROM attendance_logs WHERE CAST(worker_id AS INTEGER) BETWEEN ? AND ?",
            (base + 1, base + max(args.users, 0) + 64),
        )

        # The geofence. Without one every punch is refused *before* the photo is read
        # ("Location Rejected"), which is a fast, memory-light refusal that would make this
        # whole exercise a measurement of nothing.
        connection.execute(
            "INSERT OR REPLACE INTO construction_sites (site_name, lat, lon, radius) VALUES (?, ?, ?, ?)",
            (SITE_NAME, float(args.site_lat), float(args.site_lon), float(args.site_radius_m)),
        )

        # One bcrypt hash reused for every account: the token is the credential every punch
        # here uses, the password is never verified, and hashing thirty times would add half a
        # minute of pure CPU to every run. Same choice ``tools/load_test.py`` makes.
        shared_hash = security.pwd_context.hash("capacity-test-only-password")
        reference_dir = Path(settings.local_refs_dir)
        reference_dir.mkdir(parents=True, exist_ok=True)

        template = None
        if args.templates == "synthetic":
            import face_detector  # noqa: E402
            import face_engine  # noqa: E402

            width = int(face_engine.FACE_DIMENSIONS)
            # Deterministic, unit-length, and shaped like an embedding rather than a one-hot
            # vector: the score it produces against a real face is ~1.0 either way, so the
            # punch is refused on the score - which is the point of this mode (see the module
            # docstring). Seeded so two runs are the same run.
            rng = random.Random(20260925)
            vector = [rng.gauss(0.0, 1.0) for _ in range(width)]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            template = {
                "model": face_engine.FACE_MODEL,
                "pipeline": face_detector.active_pipeline(),
                "dimensions": width,
                "embedding": [value / norm for value in vector],
            }

        for index in range(max(args.users, 0)):
            user_id = str(base + index + 1)
            biometric_id = biometrics.new_id()
            connection.execute(
                "INSERT OR REPLACE INTO users (id, name, email, phone, password_hash, role, biometric_id) "
                "VALUES (?, ?, ?, '', ?, 'admin', ?)",
                (user_id, f"Capacity Worker {index + 1}", f"capacity{index + 1}@example.test", shared_hash, biometric_id),
            )
            if template is not None:
                path = reference_dir / f"{biometric_id}.json"
                path.write_text(json.dumps(template), encoding="utf-8")
            token, _expires = security.create_access_token(user_id, "admin")
            workers.append({"user_id": user_id, "biometric_id": biometric_id, "token": token})
        connection.commit()
    finally:
        connection.close()

    result = SeedResult(
        workers=workers,
        site={
            "site_name": SITE_NAME,
            "lat": float(args.site_lat),
            "lon": float(args.site_lon),
            "radius": float(args.site_radius_m),
        },
        seeded=len(workers),
        cleared=cleared,
        templates=args.templates,
    )
    print(SEED_JSON_PREFIX + json.dumps(asdict(result)))
    return 0


# --------------------------------------------------------------------------- #
# Container orchestration, from the host
# --------------------------------------------------------------------------- #
def require_docker() -> None:
    if shutil.which("docker") is None:
        raise CapacityError(
            "the `docker` command is not on PATH. This harness runs the real image inside a "
            "memory-capped container; without docker there is no cgroup to measure. Install "
            "Docker, or attach to a running deployment with --target-url."
        )


def docker(args: Sequence[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    """One docker invocation, with the CLI's own text kept for the error message.

    Decoded as utf-8 with replacement rather than by the console's code page: ``docker
    build`` output on a Windows shell is not always the encoding the shell thinks it is, and
    a harness that dies of a UnicodeDecodeError instead of printing a build error is a
    harness that wastes an afternoon.
    """
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise CapacityError(f"`docker {' '.join(args)}` timed out after {timeout:.0f}s") from exc


def tail(text: str, lines: int = 12) -> str:
    kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
    return "\n".join(f"    {line}" for line in kept)


def free_port() -> int:
    """A loopback port nothing is listening on, so two runs can coexist in one checkout."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_image(tag: str) -> None:
    print(f"  building {tag} from {PROJECT_ROOT / 'Dockerfile'} (this is the deployment's own image)")
    result = docker(["build", "-t", tag, "."], timeout=3600.0)
    if result.returncode != 0:
        raise CapacityError(f"docker build failed:\n{tail(result.stdout)}\n{tail(result.stderr)}")
    print("  image built")


def runtime_secret() -> tuple[str, str]:
    """``(SECRET_KEY, where it came from)`` for the throwaway container.

    The application refuses to boot without one (``config._validate_secret``) and derives no
    default on purpose, so a harness that assumed the environment would carry it would fail
    on the first machine that does not - with a container that exits and a log nobody reads.
    A key handed in through the environment is used as given; otherwise a fresh one is made
    for this run, which is safe precisely because the container is throwaway: the tokens the
    seed mints are minted *inside* it, against the same key. Nothing signed here outlives the
    run, and nothing from the deployment's own key is written to a file.
    """
    inherited = (os.environ.get("SECRET_KEY") or "").strip()
    if inherited:
        return inherited, "taken from this environment"
    return secrets.token_urlsafe(48), "generated for this run (a secret is needed to boot)"


def start_container(args: argparse.Namespace, tag: str, port: int, secret: str) -> str:
    """Run the image in the shape the instance has, and leave it running.

    The flags are the profile, not decoration: ``--memory`` is the ceiling the whole exercise
    is about, ``--memory-swap`` equal to it is what turns "slower" into "killed" (Railway's
    instance has swap off, and a harness that allowed swap would measure a box nobody has),
    and ``--cpus 1`` is the single vCPU ``cover_report``'s standing sweep was removed for.
    """
    # Clear a leftover container of the same name first: ``--keep`` leaves one behind on
    # purpose, and a crashed run leaves one behind by accident, and in both cases ``docker
    # run`` would refuse to start the new one with a name conflict that reads like a docker
    # problem rather than a harness one.
    with contextlib.suppress(Exception):
        docker(["rm", "--force", args.container_name], timeout=120.0)

    memory = f"{args.memory_mib}m"
    limits = [
        "--memory", memory,
        "--memory-swap", memory,
        "--cpus", "1",
        "--publish", f"127.0.0.1:{port}:8000",
        # The rate limiters are per client IP and this harness is one IP punching thirty
        # times. Raised here rather than worked around, and said out loud in the report: a
        # run that measured the limiter would be a run about the limiter.
        "--env", "ATTENDANCE_RATE_LIMIT=1000000/minute",
        "--env", "ENROLLMENT_RATE_LIMIT=1000000/minute",
        # The deployment's own memory profile, restated so --image reusing a hand-built
        # image without the Dockerfile's ENV is measured as this deployment, not as a default.
        "--env", "FACE_INFERENCE_QUEUE=8",
        "--env", "STANDING_SWEEP_ENABLED=0",
        # Required to boot (see ``runtime_secret``) and inherited by every ``docker exec``,
        # so the seed signs the tokens the server will then verify.
        "--env", f"SECRET_KEY={secret}",
    ]
    if not args.keep:
        limits.append("--rm")
    command = ["run", "--detach", "--name", args.container_name, *limits, tag]
    result = docker(command, timeout=300.0)
    if result.returncode != 0:
        raise CapacityError(f"docker run failed:\n{tail(result.stdout)}\n{tail(result.stderr)}")
    return result.stdout.strip().splitlines()[-1][:12]


def stop_container(container: str) -> None:
    with contextlib.suppress(Exception):
        docker(["rm", "--force", container], timeout=120.0)


def wait_for_health(base_url: str, timeout: float, container: str) -> None:
    """Wait for the deployment to serve, and say what it said if it will not.

    The status route answers without a session (the Dockerfile's own HEALTHCHECK uses it), so
    a 200 here means migrations ran, the model files loaded and the lifespan came up. The
    application's logs are printed on failure because on a first boot they carry the
    bootstrap admin password, and a container that exits is far easier to diagnose with its
    last twelve lines than with "it never came healthy".
    """
    httpx = _load_httpx()
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(api_url(base_url, STATUS_PATH), timeout=10.0)
            if response.status_code == 200:
                return
            last = f"{response.status_code} {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - not up yet is the expected case
            last = f"{type(exc).__name__}: {exc}"
        if docker(["inspect", "--format", "{{.State.Status}}", container], timeout=30.0).stdout.strip() in {
            "exited",
            "dead",
        }:
            logs = docker(["logs", "--tail", "40", container], timeout=60.0)
            raise CapacityError(f"the container exited before serving ({last}):\n{tail(logs.stdout + logs.stderr)}")
        time.sleep(1.0)
    logs = docker(["logs", "--tail", "40", container], timeout=60.0)
    raise CapacityError(
        f"the container never answered {api_url(base_url, STATUS_PATH)} within {timeout:.0f}s "
        f"({last}):\n{tail(logs.stdout + logs.stderr)}"
    )


def seed_container(container: str, args: argparse.Namespace) -> SeedResult:
    """Insert the roster through ``docker exec``, and read its answer off the marker line."""
    result = docker(
        [
            "exec", container, "python", TOOL_IN_IMAGE, "--seed",
            "--users", str(args.users),
            "--user-id-base", str(args.user_id_base),
            "--site-lat", str(args.site_lat),
            "--site-lon", str(args.site_lon),
            "--site-radius-m", str(args.site_radius_m),
            "--templates", args.templates,
        ],
        timeout=600.0,
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith(SEED_JSON_PREFIX):
            payload = json.loads(line[len(SEED_JSON_PREFIX) :])
            return SeedResult(**payload)
    raise CapacityError(
        "the in-container seed printed no roster:\n" + tail(result.stdout) + "\n" + tail(result.stderr)
    )


def enroll_workers(base_url: str, workers: Sequence[Mapping[str, str]], jpeg: bytes, selfie: str) -> None:
    """Give every account a **real** template, through the deployment's own enroll endpoint.

    This is the step that makes the run honest. A hand-written template is a claim about the
    model's provenance that can drift from the model; this uploads the photo, lets the real
    enrollment path run the real liveness policy and the real embedding, and stores what the
    application itself would store. Every punch afterwards matches its own template, so the
    approved path - frame stored, attendance row, notification - is what gets measured
    instead of a refusal that returns before the model ever runs.

    One at a time, deliberately: thirty concurrent enrollments would contend with each other
    for the engine and turn a two-second setup into a capacity event of its own.
    """
    httpx = _load_httpx()
    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        for index, worker in enumerate(workers, start=1):
            response = client.post(
                api_url(base_url, ENROLL_PATH),
                files={"photo": ("reference.jpg", jpeg, "image/jpeg")},
                headers={"Authorization": f"Bearer {worker['token']}"},
            )
            if response.status_code != 200:
                raise CapacityError(
                    f"enrollment refused for account {index} ({response.status_code}): {response.text[:300]}\n"
                    f"    The deployment's own liveness policy judged {selfie!r}, and a template it will not "
                    "accept cannot be made real.\n"
                    "    Pass --selfie with a frame it accepts (a live capture, face filling the frame, even "
                    "light), or run with --templates synthetic to measure the pipeline without the verdict."
                )
            print(f"\r  enrolled      : {index}/{len(workers)}", end="", flush=True)
    print()


# --------------------------------------------------------------------------- #
# The two run modes
# --------------------------------------------------------------------------- #
def sweep(
    args: argparse.Namespace,
    sampler: Sampler,
    base_url: str,
    workers: Sequence[Mapping[str, str]],
    jpeg: bytes,
    report: RunReport,
) -> None:
    """The levels, each bracketed by the sampler, in order of concurrency.

    Each level takes its **own** accounts, off one cursor: an account that already clocked in
    is refused by the session rule, and while that refusal happens after the model (so its
    latency is honest) a level built from accounts that already punched is a level whose
    statuses are about the previous level. The warmup punch takes the last account, which the
    cursor never reaches, so no level inherits its session either.
    """
    pool = list(workers)
    if args.warmup and pool:
        # One punch before the first bracket: the first model call after enrollment pays for
        # a cold detector and a cold embedding session, and leaving that inside level 2 would
        # make the smallest level look like the slowest one - the exact shape of a false
        # regression report.
        warm = asyncio.run(run_level(1, pool[-1:], jpeg, base_url))[0]
        print(
            f"  warmup        : {warm['status']} in {warm['latency'] * 1000:.0f} ms "
            f"(outside every bracket: it pays for the cold caches, not the profile)"
        )
    cursor = 0
    for level in args.level_list:
        batch = pool[cursor : cursor + level]
        cursor += level
        sampler.begin(f"concurrency {level}")
        started = time.perf_counter()
        results = asyncio.run(run_level(level, batch, jpeg, base_url))
        wall = time.perf_counter() - started
        window = sampler.end()
        report.levels.append(LevelResult(concurrency=level, results=results, window=window, wall_seconds=wall))
        stats = latency_stats(results)
        print(
            f"  level {level:>2}      : median {stats['median']:.2f}s  p95 {stats['p95']:.2f}s  "
            f"p99 {stats['p99']:.2f}s  anon {window.anon_max / MIB:.0f} MiB  throttled {window.throttle_ratio * 100:.1f}%"
        )


def run_container(args: argparse.Namespace) -> int:
    require_docker()
    tag = args.image or args.tag
    if not args.image:
        build_image(tag)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    secret, secret_source = runtime_secret()
    print(f"  starting      : --memory {args.memory_mib}m --memory-swap {args.memory_mib}m --cpus 1 on {base_url}")
    container = start_container(args, tag, port, secret)
    print(f"  container     : {container}")

    report = RunReport(
        target=base_url,
        profile=f"{args.memory_mib} MiB / 1 vCPU, swap off, one process",
        image=tag,
        selfie=str(args.selfie),
        templates=args.templates,
        limits=args.limits,
    )
    notes = [
        "rate limiters raised for this run (one client IP, thirty punches)",
        f"queue depth 8, engine capacity {args.engine_concurrency}, wait 20 s (the deployment's profile)",
        f"SECRET_KEY {secret_source}",
    ]
    if args.templates == "synthetic":
        notes.append(
            "synthetic templates: the model runs, the verdict is a refusal by construction, and "
            "the seed process imports the model stack - which the kernel's peak includes"
        )
    try:
        wait_for_health(base_url, args.startup_timeout, container)
        jpeg = Path(args.selfie).read_bytes()
        seed = seed_container(container, args)
        print(f"  seeded        : {seed.seeded} accounts, {seed.cleared} cleared from a previous run")
        if args.templates == "api":
            enroll_workers(base_url, seed.workers, jpeg, str(args.selfie))

        with RemoteSampler(container, args.interval) as sampler:
            sampler.begin("baseline")
            time.sleep(args.baseline_seconds)
            report.baseline = sampler.end()
            sweep(args, sampler, base_url, seed.workers, jpeg, report)
            sampler.begin("settle")
            time.sleep(args.settle_seconds)
            report.settled = sampler.end()
    finally:
        stop_container(container)

    report.notes = notes
    gates = evaluate_gates(report, args.limits)
    print(render(report, gates))
    if args.report:
        Path(args.report).write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
        print(f"  report written to {args.report}")
    return 0 if all(gate.ok for gate in gates) else 1


def load_credentials(path: str) -> list[dict[str, str]]:
    """``--credentials``: a JSON list of ``{"user_id": ..., "token": ...}``.

    Attach mode cannot seed, so the accounts are the caller's - and because one account
    punching twice hits "Already clocked in!" (a refusal *after* the model, so the latency is
    still real, but the second punch cannot open a session) the honest input is one account
    per concurrent punch. Fewer are cycled, and the report says so.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapacityError(f"could not read --credentials {path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise CapacityError(f"--credentials {path} must be a non-empty JSON list")
    workers: list[dict[str, str]] = []
    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("user_id") or not entry.get("token"):
            raise CapacityError(f"--credentials {path} entries need user_id and token, got {entry!r}")
        workers.append({"user_id": str(entry["user_id"]), "token": str(entry["token"])})
    return workers


def run_attach(args: argparse.Namespace) -> int:
    """Measure a deployment that is already running, sampling its own cgroup root.

    The mode for the instance itself: run this *inside* the container (or a sibling in the
    same cgroup) with ``--cgroup-root /sys/fs/cgroup`` and it measures exactly what the
    container run measures, minus the container. It is also what a Railway one-off job
    would use, if the platform ever offers one with a shell.
    """
    if not args.credentials:
        raise CapacityError("attach mode needs --credentials (a JSON list of {user_id, token})")
    jpeg = Path(args.selfie).read_bytes() if args.selfie else None
    if jpeg is None:
        raise CapacityError("attach mode needs --selfie (the jpeg every punch sends)")
    workers = load_credentials(args.credentials)
    # Every level takes its own accounts (see ``sweep``), so a full sweep needs one per punch.
    # Attach mode cannot seed, so when the caller has fewer they are cycled - and the note
    # below is the reason the "punches reach the model" gate then needs --min-scored lowered:
    # a second punch from one account is refused by a session rule, which is a real answer
    # from the deployment but not a scored one.
    needed = sum(args.level_list) + (1 if args.warmup else 0)
    supplied = len(workers)
    report = RunReport(
        target=args.target_url,
        profile=f"attached, cgroup root {args.cgroup_root}",
        selfie=str(args.selfie),
        templates="pre-existing",
        limits=args.limits,
    )
    notes = [f"{supplied} credential(s) supplied, {needed} punches to fire"]
    if supplied < needed:
        notes.append(
            f"accounts cycled {math.ceil(needed / supplied)}x: a re-punch is refused by the "
            "session rule, so lower --min-scored (the latencies are still the pipeline's)"
        )
    while len(workers) < needed:
        workers = workers + workers[: needed - len(workers)]

    with LocalSampler(Path(args.cgroup_root), args.interval) as sampler:
        sampler.begin("baseline")
        time.sleep(args.baseline_seconds)
        report.baseline = sampler.end()
        sweep(args, sampler, args.target_url.rstrip("/"), workers, jpeg, report)
        sampler.begin("settle")
        time.sleep(args.settle_seconds)
        report.settled = sampler.end()

    report.notes = notes
    gates = evaluate_gates(report, args.limits)
    print(render(report, gates))
    if args.report:
        Path(args.report).write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
        print(f"  report written to {args.report}")
    return 0 if all(gate.ok for gate in gates) else 1


def rejudge(args: argparse.Namespace) -> int:
    """``--from-report``: the same numbers, different limits. No docker, no traffic."""
    try:
        payload = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapacityError(f"could not read --from-report {args.from_report}: {exc}") from exc
    report = RunReport.from_json(payload)
    report.limits = args.limits
    gates = evaluate_gates(report, args.limits)
    print(render(report, gates))
    return 0 if all(gate.ok for gate in gates) else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def default_selfie() -> str | None:
    """The same convention ``tools/load_test.py`` uses: a real face from ``worker_photos/``."""
    candidates = sorted(
        path for path in (PROJECT_ROOT / "worker_photos").glob("*.jpg") if path.is_file()
    )
    return str(candidates[0]) if candidates else None


def parse_levels(text: str) -> list[int]:
    levels: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            level = int(chunk)
        except ValueError:
            raise CapacityError(f"--levels wants integers separated by commas, got {text!r}") from None
        if level < 1:
            raise CapacityError(f"a concurrency level below 1 is not a level: {level}")
        levels.append(level)
    if not levels:
        raise CapacityError("--levels was empty")
    return sorted(set(levels))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capacity test the punch path in a memory-capped container: RSS, CPU throttling and p95/p99, with gates."
    )
    parser.add_argument("--levels", default="2,4,6,8,10", help="concurrent punches per level (default 2,4,6,8,10)")
    parser.add_argument("--interval", type=float, default=0.25, help="cgroup sampling interval in seconds (default 0.25)")
    parser.add_argument("--baseline-seconds", type=float, default=10.0, help="idle sampling before any traffic (default 10)")
    parser.add_argument("--settle-seconds", type=float, default=10.0, help="quiet sampling after the last level (default 10)")
    parser.add_argument("--warmup", action="store_true", default=True, help="one punch before the first level (default on)")
    parser.add_argument("--no-warmup", dest="warmup", action="store_false")
    parser.add_argument("--selfie", default=None, help="a jpeg of a real face; every punch sends it, and it is enrolled")
    parser.add_argument("--templates", choices=("api", "synthetic"), default="api",
                        help="api: enroll --selfie through the deployment (default); synthetic: write a template, expect refusals")
    parser.add_argument("--report", default=None, help="write the full run as JSON here")
    parser.add_argument("--from-report", default=None, help="re-judge a saved report instead of running anything")

    # limits
    parser.add_argument("--memory-mib", type=int, default=512, help="the container's cap, in MiB (default 512)")
    parser.add_argument("--memory-ceiling-mib", type=int, default=Limits.memory_mib, help="gate: memory.current ceiling")
    parser.add_argument("--anon-ceiling-mib", type=int, default=Limits.anon_mib, help="gate: memory.stat anon ceiling")
    parser.add_argument("--p95", type=float, default=Limits.p95_seconds, help="gate: per-level p95, seconds")
    parser.add_argument("--p99", type=float, default=Limits.p99_seconds, help="gate: per-level p99, seconds")
    parser.add_argument("--throttle-ratio", type=float, default=Limits.throttle_ratio, help="gate: throttled share of wall time")
    parser.add_argument("--recovery-mib", type=int, default=Limits.recovery_mib, help="gate: anon above baseline allowed after the burst")
    parser.add_argument("--min-scored", type=float, default=Limits.min_scored_fraction,
                        help="gate: share of punches that must prove they reached the model (default 1.0)")

    # container mode
    parser.add_argument("--image", default=None, help="use this image instead of building the Dockerfile")
    parser.add_argument("--tag", default="attendance-capacity:latest", help="tag for the image built from the Dockerfile")
    parser.add_argument("--container-name", default="attendance-capacity", help="docker container name to use")
    parser.add_argument("--keep", action="store_true", help="leave the container behind for inspection")
    parser.add_argument("--startup-timeout", type=float, default=180.0, help="seconds to wait for /api/v1/status (default 180)")
    parser.add_argument("--engine-concurrency", type=int, default=2, help="for the report only: the engine's pool size")

    # attach mode
    parser.add_argument("--target-url", default=None, help="measure this already-running deployment instead of a container")
    parser.add_argument("--credentials", default=None, help="attached: JSON list of {user_id, token}")
    parser.add_argument("--cgroup-root", default=str(DEFAULT_CGROUP_ROOT), help="attached: the cgroup root to sample")

    # seeding (also used by --seed inside the container)
    parser.add_argument("--users", type=int, default=None, help="accounts to seed (default: one per punch, plus the warmup)")
    parser.add_argument("--user-id-base", type=int, default=USER_ID_BASE, help="first seeded account id")
    parser.add_argument("--site-lat", type=float, default=SITE_LAT, help="seeded geofence latitude")
    parser.add_argument("--site-lon", type=float, default=SITE_LON, help="seeded geofence longitude")
    parser.add_argument("--site-radius-m", type=float, default=SITE_RADIUS_M, help="seeded geofence radius, metres")

    parser.add_argument("--seed", action="store_true", help="(inside the container) write the roster and exit")
    parser.add_argument("--sample-stream", action="store_true", help="(inside the container) sample the cgroup on stdin/stdout")
    return parser


def limits_from(args: argparse.Namespace) -> Limits:
    """The gate thresholds as a :class:`Limits`, assembled once for every mode that judges."""
    return Limits(
        memory_mib=args.memory_ceiling_mib,
        anon_mib=args.anon_ceiling_mib,
        p95_seconds=args.p95,
        p99_seconds=args.p99,
        throttle_ratio=args.throttle_ratio,
        recovery_mib=args.recovery_mib,
        min_scored_fraction=args.min_scored,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The two in-container modes own their whole process and read no limits.
    if args.sample_stream:
        return stream_cgroup_samples(args.interval, Path(args.cgroup_root))
    if args.seed:
        return seed_inside(args)

    # Everything that judges takes its thresholds from here, including ``--from-report`` -
    # which is the whole point of that mode, so its limits have to exist before the branch.
    args.limits = limits_from(args)
    if args.from_report:
        return rejudge(args)

    args.level_list = parse_levels(args.levels)
    # One account per punch: a second punch from an account that already clocked in is
    # refused by a session rule (after the model, so the latency would still be real, but the
    # burst would no longer be the thing being measured). Plus one more for the warmup.
    if args.users is None:
        args.users = sum(args.level_list) + (1 if args.warmup else 0)

    if args.target_url:
        if not args.selfie:
            raise CapacityError("attach mode needs --selfie")
        return run_attach(args)

    if not args.selfie:
        found = default_selfie()
        if found is None:
            raise CapacityError(
                "no --selfie given and no .jpg under worker_photos/. The punches have to send a "
                "real face: a frame with no face in it is refused by the detector before the "
                "embedding, and the memory numbers would describe a much cheaper path."
            )
        args.selfie = found
        print(f"  selfie        : {args.selfie} (first .jpg under worker_photos/)")
    if not Path(args.selfie).is_file():
        raise CapacityError(f"--selfie {args.selfie} does not exist")
    return run_container(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CapacityError as error:
        print(f"\ncapacity test could not run: {error}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
