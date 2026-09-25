"""The capacity harness's verdict, decided without a container.

WHY THIS EXISTS
---------------
``backend/tools/capacity_test.py`` costs a docker build, a memory-capped container, several
minutes of real face inference and a real deployment's worth of memory to produce one table
of gates. That is the right price for the *measurement*. It is the wrong price for the
*arithmetic*: which thresholds exist, which sample of a run each one reads, and whether a
number that crossed its ceiling actually fails the run are all pure functions of numbers a
run has already produced. A gate whose logic is only ever exercised by paying for a container
is a gate that quietly stops being true the first time somebody edits a comparison, and the
symptom is a green run that says nothing.

So this suite pins the verdict, not the traffic:

* the cgroup readers, against fixtures in the shape this kernel writes (v2) and the shape an
  older one writes (v1) - including the two places they disagree, which are the two places a
  reader that "worked locally" reports zeros on somebody else's host;
* the window arithmetic (deltas for counters, worst-case for high-water marks, a restart
  clamped to zero rather than reported as negative OOM kills);
* and :func:`capacity_test.evaluate_gates` against a **healthy synthetic report** and then
  against one mutation per gate, asserting the exact set of gates that fails. That table is
  the real contract: it is what says a run at 400 MiB of anonymous memory is a failure *and*
  that it is a failure about memory and not about latency.

The one piece of the harness that is genuinely exercised end to end here is the in-container
sampler (``--sample-stream``) - it is a subprocess with no docker and no HTTP in it, so the
line protocol the host half speaks to it is tested for real rather than stubbed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BACKEND_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import capacity_test as capacity  # noqa: E402 - after the path above

MIB = capacity.MIB


# --------------------------------------------------------------------------- #
# synthetic windows, levels and reports
# --------------------------------------------------------------------------- #
def make_window(
    label: str,
    *,
    seconds: float = 4.0,
    memory_mib: int = 300,
    anon_mib: int = 260,
    peak_mib: int | None = None,
    events: dict[str, int] | None = None,
    throttled_usec: int = 0,
    nr_throttled: int = 0,
) -> capacity.Window:
    """One window with only the fields a gate reads spelled out explicitly."""
    return capacity.Window(
        label=label,
        seconds=seconds,
        samples=16,
        memory_max=memory_mib * MIB,
        memory_last=memory_mib * MIB,
        anon_max=anon_mib * MIB,
        anon_last=anon_mib * MIB,
        file_last=40 * MIB,
        peak_bytes=(peak_mib if peak_mib is not None else memory_mib) * MIB,
        events={key: 0 for key in capacity.EVENT_KEYS} | (events or {}),
        usage_usec=int(seconds * 1_000_000),
        nr_periods=int(seconds * 100),
        nr_throttled=nr_throttled,
        throttled_usec=throttled_usec,
    )


def make_punch(status: int = 200, latency: float = 0.5, error_code: str = "") -> dict:
    """One punch result, in the shape ``punch()`` returns it."""
    return {"status": status, "latency": latency, "error_code": error_code, "detail": ""}


def make_level(
    concurrency: int,
    *,
    latency: float = 0.5,
    status: int = 200,
    error_code: str = "",
    anon_mib: int = 260,
    memory_mib: int | None = None,
    throttled_usec: int = 0,
    nr_throttled: int = 0,
    seconds: float = 4.0,
    results: list[dict] | None = None,
) -> capacity.LevelResult:
    """One concurrency level: ``concurrency`` identical punches inside one window."""
    punches = results if results is not None else [make_punch(status, latency, error_code) for _ in range(concurrency)]
    return capacity.LevelResult(
        concurrency=concurrency,
        results=punches,
        wall_seconds=seconds,
        window=make_window(
            f"concurrency {concurrency}",
            seconds=seconds,
            memory_mib=memory_mib if memory_mib is not None else anon_mib + 40,
            anon_mib=anon_mib,
            throttled_usec=throttled_usec,
            nr_throttled=nr_throttled,
        ),
    )


def make_report(
    levels: list[capacity.LevelResult] | None = None,
    *,
    baseline_anon_mib: int = 250,
    settled_anon_mib: int = 252,
    settled_events: dict[str, int] | None = None,
    limits: capacity.Limits | None = None,
    templates: str = "api",
) -> capacity.RunReport:
    """A full run: the five levels of the default sweep, and a quiet minute either side."""
    return capacity.RunReport(
        target="http://127.0.0.1:9",
        profile="512 MiB / 1 vCPU, swap off, one process",
        image="attendance-capacity:test",
        selfie="worker_photos/test.jpg",
        templates=templates,
        limits=limits or capacity.Limits(),
        baseline=make_window("baseline", seconds=10.0, memory_mib=290, anon_mib=baseline_anon_mib),
        settled=make_window(
            "settle", seconds=10.0, memory_mib=settled_anon_mib + 40, anon_mib=settled_anon_mib,
            events=settled_events,
        ),
        levels=levels if levels is not None else [make_level(c) for c in (2, 4, 6, 8, 10)],
    )


def failing(gates) -> set[str]:
    return {gate.name for gate in gates if not gate.ok}


# --------------------------------------------------------------------------- #
# the cgroup readers
# --------------------------------------------------------------------------- #
def test_stat_lines_keep_what_they_understand_and_ignore_the_rest():
    """``memory.stat`` grows fields between kernels; a reader that raised would be brittle."""
    text = "anon 1234\nfile 56\nweird_key 7 8\nnot a number 12\nempty_line_value\n"
    assert capacity.parse_stat_lines(text) == {"anon": 1234, "file": 56}


def test_a_v2_cgroup_is_read_the_way_this_kernel_writes_it(tmp_path: Path):
    (tmp_path / "memory.current").write_text("314572800\n")
    (tmp_path / "memory.peak").write_text("377487360\n")
    (tmp_path / "memory.stat").write_text("anon 262144000\nfile 52428800\nslab 1024\n")
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 9000000\nnr_periods 400\nnr_throttled 37\nthrottled_usec 250000\n")

    reading = capacity.read_cgroup(tmp_path)

    assert reading.memory_bytes == 314_572_800
    assert reading.peak_bytes == 377_487_360
    assert reading.anon_bytes == 262_144_000
    assert reading.file_bytes == 52_428_800
    assert reading.throttled_usec == 250_000
    assert reading.nr_throttled == 37
    assert reading.events["oom_kill"] == 0


def test_a_v1_cgroup_is_read_with_its_own_names(tmp_path: Path):
    """The v1 shape: files move under ``memory/`` and nanoseconds are spelled differently."""
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "memory.usage_in_bytes").write_text("314572800\n")
    (memory / "memory.max_usage_in_bytes").write_text("377487360\n")
    (memory / "memory.failcnt").write_text("3\n")
    (memory / "memory.stat").write_text("total_rss 262144000\ntotal_cache 52428800\n")
    (memory / "memory.oom_control").write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 1\n")
    (tmp_path / "cpu.stat").write_text("nr_periods 400\nnr_throttled 37\nthrottled_time 250000000\n")

    reading = capacity.read_cgroup(tmp_path)

    assert reading.anon_bytes == 262_144_000
    assert reading.file_bytes == 52_428_800
    # ``throttled_time`` is nanoseconds; the rest of this harness counts microseconds.
    assert reading.throttled_usec == 250_000
    assert reading.nr_throttled == 37
    # v1 spells "the limit was reached" ``failcnt``, and reports the kill separately.
    assert reading.events["max"] == 3
    assert reading.events["oom_kill"] == 1


def test_a_root_with_neither_hierarchy_is_refused_rather_than_read_as_zero(tmp_path: Path):
    """Reporting 0 MiB for a cgroup nobody read is how a capacity test passes by accident."""
    with pytest.raises(capacity.CapacityError) as refused:
        capacity.read_cgroup(tmp_path)
    assert "cgroup" in str(refused.value)


# --------------------------------------------------------------------------- #
# the window arithmetic
# --------------------------------------------------------------------------- #
def test_a_window_takes_deltas_for_counters_and_worst_cases_for_high_water_marks():
    readings = [
        capacity.CgroupReading(300 * MIB, 250 * MIB, 50 * MIB, 300 * MIB, {"oom_kill": 0, "max": 0}, 1_000, 10, 0, 0),
        capacity.CgroupReading(360 * MIB, 310 * MIB, 50 * MIB, 370 * MIB, {"oom_kill": 0, "max": 0}, 3_000, 20, 2, 5_000),
        capacity.CgroupReading(310 * MIB, 260 * MIB, 50 * MIB, 370 * MIB, {"oom_kill": 1, "max": 2}, 7_000, 40, 6, 90_000),
    ]

    window = capacity.summarise_window("concurrency 4", readings, seconds=2.0)

    assert window.memory_max == 360 * MIB
    assert window.anon_max == 310 * MIB
    assert window.memory_last == 310 * MIB
    assert window.peak_bytes == 370 * MIB
    assert window.events["oom_kill"] == 1
    assert window.events["max"] == 2
    assert window.usage_usec == 6_000
    assert window.nr_throttled == 6
    assert window.throttled_usec == 90_000
    assert window.throttle_ratio == pytest.approx(90_000 / 2_000_000)


def test_a_counter_that_moved_backwards_is_clamped_rather_than_reported_negative():
    """A replaced cgroup file must not read as negative OOM kills, nor hide the restart."""
    readings = [
        capacity.CgroupReading(100, 80, 20, 100, {"oom_kill": 4}, 900, 9, 1, 500),
        capacity.CgroupReading(100, 80, 20, 100, {"oom_kill": 0}, 100, 1, 0, 0),
    ]
    window = capacity.summarise_window("window", readings, seconds=1.0)
    assert window.events["oom_kill"] == 0
    assert window.usage_usec == 0
    assert window.throttled_usec == 0


def test_windows_combine_by_summing_counters_and_keeping_the_worst_maxima():
    quiet = make_window("quiet", seconds=10.0, anon_mib=250, memory_mib=290)
    busy = make_window("busy", seconds=4.0, anon_mib=330, memory_mib=380, events={"oom_kill": 1}, throttled_usec=2_000_000)

    whole = capacity.combine_windows("run", [quiet, busy])

    assert whole.seconds == 14.0
    assert whole.memory_max == 380 * MIB
    assert whole.anon_max == 330 * MIB
    assert whole.events["oom_kill"] == 1
    assert whole.throttled_usec == 2_000_000


def test_percentiles_are_nearest_rank():
    """The audience is a decision about the *tenth worker*, so its own latency is the answer."""
    values = [float(rank) for rank in range(1, 11)]
    assert capacity.percentile(values, 50) == 5.0
    assert capacity.percentile(values, 95) == 10.0
    assert capacity.percentile(values, 99) == 10.0
    assert capacity.percentile([0.4, 0.6], 95) == 0.6
    assert capacity.percentile([], 95) == 0.0


def test_only_a_verdict_or_a_mismatch_counts_as_having_reached_the_model():
    """The cheap refusals are the ones that would make the memory numbers look wonderful."""
    assert capacity.reached_the_model(make_punch(status=200))
    assert capacity.reached_the_model(make_punch(status=422, error_code="face_mismatch"))
    # Refused in front of the model: liveness, the geofence, a missing template, a session.
    assert not capacity.reached_the_model(make_punch(status=422, error_code="liveness_spoof"))
    assert not capacity.reached_the_model(make_punch(status=403, error_code=""))
    assert not capacity.reached_the_model(make_punch(status=400, error_code=""))
    assert not capacity.reached_the_model(make_punch(status=0, error_code=""))


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
def test_a_healthy_run_passes_every_gate():
    gates = capacity.evaluate_gates(make_report(), capacity.Limits())
    assert failing(gates) == set()
    assert len(gates) == 20


def test_the_gate_table_has_the_names_and_shape_the_report_prints():
    names = [gate.name for gate in capacity.evaluate_gates(make_report(), capacity.Limits())]
    assert names[:4] == ["memory ceiling", "anonymous set", "no OOM kill", "limit never reached"]
    assert "punches reach the model" in names
    assert names.count("p95 @ 10 concurrent") == 1
    assert names.count("p99 @ 10 concurrent") == 1
    assert names[-2:] == ["CPU not starved", "memory returns after the burst"]


#: One mutation per gate, and the exact set of gates each is expected to fail. The exactness is
#: the point: "the run failed" is not a contract anybody can act on, and a gate that fires for
#: a second reason is a gate whose name no longer means anything.
MUTATIONS: tuple[tuple[str, dict, set[str]], ...] = (
    (
        "over the memory ceiling",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, memory_mib=500, anon_mib=300)]},
        {"memory ceiling"},
    ),
    (
        "the anonymous set too large",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, memory_mib=440, anon_mib=400)]},
        {"anonymous set"},
    ),
    (
        "an OOM kill",
        {"settled_events": {"oom_kill": 1}},
        {"no OOM kill"},
    ),
    (
        "the limit reached without a kill",
        {"settled_events": {"max": 1}},
        {"limit never reached"},
    ),
    (
        "a punch refused before the model",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, results=[make_punch(status=403, error_code="")] + [make_punch() for _ in range(9)])]},
        {"punches reach the model"},
    ),
    (
        "a dead connection",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, results=[make_punch(status=0)] + [make_punch() for _ in range(9)])]},
        {"punches reach the model", "no transport errors"},
    ),
    (
        "a 500 from the punch",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, results=[make_punch(status=500)] + [make_punch() for _ in range(9)])]},
        {"punches reach the model", "no server errors"},
    ),
    (
        "the engine's queue answering 503",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, results=[make_punch(status=503)] + [make_punch() for _ in range(9)])]},
        {"punches reach the model", "no server errors", "queue absorbed the burst"},
    ),
    (
        "a level whose p95 is over budget",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8, latency=3.0), make_level(10)]},
        {"p95 @ 8 concurrent"},
    ),
    (
        "a level whose p99 is over budget",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8), make_level(10, latency=4.5)]},
        {"p95 @ 10 concurrent", "p99 @ 10 concurrent"},
    ),
    (
        "one core that cannot keep up",
        {"levels": [make_level(2), make_level(4), make_level(6), make_level(8, throttled_usec=int(0.8 * 4.0 * 1_000_000), nr_throttled=90), make_level(10)]},
        {"CPU not starved"},
    ),
    (
        "memory that never came back down",
        {"settled_anon_mib": 340},
        {"memory returns after the burst"},
    ),
)


@pytest.mark.parametrize("label,overrides,expected", MUTATIONS, ids=[mutation[0] for mutation in MUTATIONS])
def test_each_mutation_fails_exactly_the_gates_it_should(label, overrides, expected):
    gates = capacity.evaluate_gates(make_report(**overrides), capacity.Limits())
    assert failing(gates) == expected, f"{label}: {failing(gates)}"


def test_a_report_with_no_punches_cannot_pass():
    """Zero punches reaching the model is not 100% of them."""
    gates = capacity.evaluate_gates(make_report(levels=[]), capacity.Limits())
    assert "punches reach the model" in failing(gates)


def test_the_memory_ceiling_reads_the_kernel_peak_not_only_the_sampled_maximum():
    """A spike between two samples is still a spike: ``memory.peak`` is the cgroup's own."""
    level = make_level(2, memory_mib=300, anon_mib=260)
    spiked = replace(level, window=replace(level.window, peak_bytes=500 * MIB))
    report = make_report(levels=[spiked, make_level(4), make_level(6), make_level(8), make_level(10)])

    assert report.measured.memory_max < capacity.Limits().memory_bytes
    assert report.measured.peak_bytes > capacity.Limits().memory_bytes
    assert "memory ceiling" in failing(capacity.evaluate_gates(report, capacity.Limits()))


def test_widening_the_limits_rescues_the_same_numbers():
    """The whole reason the report is serialisable: argue about a gate with the same numbers."""
    report = make_report(levels=[make_level(c, latency=3.0) for c in (2, 4, 6, 8, 10)])
    assert "p95 @ 2 concurrent" in failing(capacity.evaluate_gates(report, capacity.Limits()))
    relaxed = capacity.Limits(p95_seconds=4.0, p99_seconds=6.0)
    assert failing(capacity.evaluate_gates(report, relaxed)) == set()


def test_throttling_is_gated_on_the_worst_level_not_on_the_average_run():
    """Ten idle seconds either side would otherwise hide the busiest level inside the mean."""
    report = make_report(
        levels=[make_level(2, throttled_usec=0), make_level(4, throttled_usec=0), make_level(6, throttled_usec=0),
                make_level(8, throttled_usec=int(0.9 * 4.0 * 1_000_000)), make_level(10, throttled_usec=0)]
    )
    measured = report.measured
    assert measured.throttle_ratio < capacity.Limits().throttle_ratio  # the average looks fine
    assert "CPU not starved" in failing(capacity.evaluate_gates(report, capacity.Limits()))


# --------------------------------------------------------------------------- #
# the report on disk, and the offline re-judgement
# --------------------------------------------------------------------------- #
def test_a_report_survives_a_json_round_trip(tmp_path: Path):
    limits = capacity.Limits(p95_seconds=3.5)
    original = make_report(limits=limits)
    path = tmp_path / "run.json"
    path.write_text(json.dumps(original.to_json()), encoding="utf-8")

    restored = capacity.RunReport.from_json(json.loads(path.read_text(encoding="utf-8")))

    assert restored.limits == limits
    assert [level.concurrency for level in restored.levels] == [2, 4, 6, 8, 10]
    assert asdict(restored.baseline) == asdict(original.baseline)
    assert asdict(restored.settled) == asdict(original.settled)
    assert [level.results for level in restored.levels] == [level.results for level in original.levels]
    assert evaluate_names(restored) == evaluate_names(original)


def evaluate_names(report: capacity.RunReport) -> set[str]:
    return failing(capacity.evaluate_gates(report, report.limits))


def test_the_offline_rejudgement_runs_the_same_gates_without_a_container(tmp_path: Path, capsys):
    """``--from-report`` is the escape hatch: no docker, no traffic, the same numbers."""
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(make_report(levels=[make_level(c, latency=3.0) for c in (2, 4, 6, 8, 10)]).to_json()),
        encoding="utf-8",
    )

    assert capacity.main(["--from-report", str(path)]) == 1
    assert "p95 @ 10 concurrent" in capsys.readouterr().out

    assert capacity.main(["--from-report", str(path), "--p95", "4.0", "--p99", "6.0"]) == 0
    assert "VERDICT: PASS" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the URLs the harness aims at
# --------------------------------------------------------------------------- #
def test_the_api_prefix_is_added_once():
    """The bug this file exists to prevent: ``/api/v1`` + ``/api/v1/status`` is a 404."""
    assert capacity.api_url("http://127.0.0.1:8000", capacity.STATUS_PATH) == "http://127.0.0.1:8000/api/v1/status"
    assert capacity.api_url("http://127.0.0.1:8000", capacity.PUNCH_PATH) == (
        "http://127.0.0.1:8000/api/v1/attendance/verify"
    )
    assert capacity.api_url("http://127.0.0.1:8000", capacity.ENROLL_PATH) == (
        "http://127.0.0.1:8000/api/v1/worker/me/enroll"
    )
    assert capacity.api_url("http://127.0.0.1:8000/", capacity.STATUS_PATH) == (
        "http://127.0.0.1:8000/api/v1/status"
    )
    assert "/api/v1/api/v1" not in capacity.api_url("http://x", capacity.STATUS_PATH)


def test_the_routes_the_harness_calls_are_the_routes_the_application_serves(client):
    """Asked of the running application, not of a string this file also owns.

    A renamed endpoint would otherwise surface as a 404 in the middle of a capacity run, on
    the one machine that has docker, hours after the change - reported as "the deployment
    never came healthy". "Not 404" is the whole assertion: these paths are authenticated (or
    form-validated), so the interesting failure is the route *not being there*, and every
    other status is the application answering.
    """
    assert client.get(capacity.api_url("", capacity.STATUS_PATH)).status_code == 200
    for method, path in (("post", capacity.PUNCH_PATH), ("post", capacity.ENROLL_PATH)):
        response = getattr(client, method)(capacity.api_url("", path))
        assert response.status_code != 404, f"{path} is not a route any more: {response.status_code}"


def test_a_missing_report_is_refused_in_a_sentence(tmp_path: Path):
    with pytest.raises(capacity.CapacityError) as refused:
        capacity.main(["--from-report", str(tmp_path / "nope.json")])
    assert "could not read" in str(refused.value)


# --------------------------------------------------------------------------- #
# the in-container half, end to end
# --------------------------------------------------------------------------- #
def _cgroup_fixture(root: Path) -> None:
    (root / "memory.current").write_text("314572800\n")
    (root / "memory.peak").write_text("377487360\n")
    (root / "memory.stat").write_text("anon 262144000\nfile 52428800\n")
    (root / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    (root / "cpu.stat").write_text("usage_usec 9000000\nnr_periods 400\nnr_throttled 37\nthrottled_usec 250000\n")


def test_the_sample_stream_answers_begin_end_quit(tmp_path: Path):
    """The line protocol the host half speaks over ``docker exec``, spoken for real."""
    root = tmp_path / "cgroup"
    root.mkdir()
    _cgroup_fixture(root)

    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "capacity_test.py"),
         "--sample-stream", "--interval", "0.05", "--cgroup-root", str(root)],
        input="begin concurrency 2\nend\nquit\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert events[0]["event"] == "begin"
    assert events[0]["label"] == "concurrency 2"
    window = next(payload["window"] for payload in events if payload["event"] == "window")
    assert window["label"] == "concurrency 2"
    assert window["memory_max"] == 314_572_800
    assert window["anon_max"] == 262_144_000
    assert window["peak_bytes"] == 377_487_360
    assert window["throttled_usec"] in (0, 250_000)  # one bracket, from the first reading on
    assert events[-1]["event"] == "bye"
    # ...and the payload is exactly a Window, which is what the host half rebuilds from it.
    rebuilt = capacity.Window(**window)
    assert rebuilt.events["oom_kill"] == 0
    assert rebuilt.seconds >= 0.0


def test_the_sample_stream_needs_no_first_reading_before_begin(tmp_path: Path):
    """``begin`` takes its own baseline reading; nothing may depend on the thread's timing."""
    root = tmp_path / "cgroup"
    root.mkdir()
    _cgroup_fixture(root)

    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "capacity_test.py"),
         "--sample-stream", "--interval", "30", "--cgroup-root", str(root)],
        input="end\nbegin second\nend\nquit\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    windows = [json.loads(line)["window"] for line in result.stdout.splitlines() if '"event": "window"' in line]
    assert len(windows) == 2
    assert all(window["samples"] >= 2 for window in windows)


def test_the_sample_stream_says_so_when_there_is_no_cgroup_at_all(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "capacity_test.py"), "--sample-stream", "--cgroup-root", str(tmp_path)],
        input="begin x\nquit\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode != 0
    assert "cgroup" in (result.stderr + result.stdout)
