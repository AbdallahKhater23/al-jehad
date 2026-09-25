"""The memory gate's thresholds, pinned without paying for a run.

``tools/punch_saturation.py`` is the per-push half of the memory work: it drives the punch
endpoint past its capacity, samples the application's own process, and fails the run when the
peak crossed ``--ceiling-mib``. Its verdict is a pure function of a report and a ``Limits``
(``saturation.evaluate_gates``), and that is deliberate - a threshold that can only be checked
by starting a server and running real inference is a threshold nobody checks.

So this suite holds both halves of the contract:

* **the numbers the gate is about** - every mutation below is a report that differs from a
  healthy one in exactly one way, and the failing gate names are asserted as a set, so a gate
  that stops firing, fires for something else, or silently widens is a failure here rather
  than a PASS in a pipeline;
* **the wiring that finds the number** - reading ``/proc/<pid>/status``, walking a process
  tree, choosing the application inside it (a Windows virtualenv's ``python.exe`` is a
  launcher whose five megabytes are not the deployment's), ending that tree, and refusing when
  there is nothing it can legitimately measure.

No server, no models, no docker, no network: the whole file is milliseconds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BACKEND_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import capacity_test as capacity  # noqa: E402 - after the path above
import punch_saturation as saturation  # noqa: E402

MIB = capacity.MIB

#: The five gates a healthy saturated run passes. Listed rather than counted, because the
#: interesting failure is a gate that quietly disappeared.
GATE_NAMES = frozenset(
    {
        "peak rss",
        "anonymous set",
        "load reached the pipeline",
        "no transport errors",
        "no server errors",
        "shedding is the designed one",
        "no refusals before the photo",
        "memory comes back",
    }
)


# --------------------------------------------------------------------------- #
# synthetic runs, in the shape the harness produces
# --------------------------------------------------------------------------- #
def cli(*argv: str) -> argparse.Namespace:
    """The real parser, so a default this file asserts is the default the tool runs with."""
    return saturation.build_parser().parse_args(list(argv))


def limits_from(*argv: str) -> capacity.Limits:
    return saturation.saturation_limits(cli(*argv))


def window(
    label: str,
    *,
    seconds: float = 4.0,
    memory_mib: int = 300,
    anon_mib: int = 260,
    peak_mib: int | None = None,
) -> capacity.Window:
    return capacity.Window(
        label=label,
        seconds=seconds,
        samples=16,
        memory_max=memory_mib * MIB,
        memory_last=memory_mib * MIB,
        anon_max=anon_mib * MIB,
        anon_last=anon_mib * MIB,
        file_last=40 * MIB,
        peak_bytes=(memory_mib if peak_mib is None else peak_mib) * MIB,
        events={key: 0 for key in capacity.EVENT_KEYS},
        usage_usec=int(seconds * 1_000_000),
        nr_periods=0,
        nr_throttled=0,
        throttled_usec=0,
    )


def punch(status: int = 200, error_code: str = "", retry_after: str = "") -> dict:
    """One punch result, in the shape ``capacity_test.punch`` returns it."""
    return {"status": status, "latency": 0.5, "error_code": error_code, "detail": "", "retry_after": retry_after}


def make_report(
    punches: list[dict] | None = None,
    *,
    baseline_anon_mib: int = 250,
    settled_anon_mib: int = 252,
    memory_mib: int = 300,
    anon_mib: int = 260,
    peak_mib: int | None = None,
    levels: int = 2,
) -> capacity.RunReport:
    """A saturated run: ``levels`` rounds of the same punches, and a quiet minute either side."""
    load = punches if punches is not None else [punch() for _ in range(8)]
    return capacity.RunReport(
        target="http://127.0.0.1:9",
        profile="host process, no cgroup cap",
        image="/tmp/punch-saturation",
        selfie="",
        templates="synthetic",
        limits=limits_from(),
        baseline=window("baseline", seconds=5.0, memory_mib=290, anon_mib=baseline_anon_mib),
        settled=window("settle", seconds=5.0, memory_mib=settled_anon_mib + 40, anon_mib=settled_anon_mib),
        levels=[
            capacity.LevelResult(
                concurrency=len(load),
                results=list(load),
                wall_seconds=4.0,
                window=window(f"round {index}", memory_mib=memory_mib, anon_mib=anon_mib, peak_mib=peak_mib),
            )
            for index in range(1, levels + 1)
        ],
    )


def failing(gates) -> set[str]:
    return {gate.name for gate in gates if not gate.ok}


# --------------------------------------------------------------------------- #
# the ceiling is a number the caller arms
# --------------------------------------------------------------------------- #
def test_the_ceiling_defaults_to_the_instance_profile_and_is_armable():
    """400 MiB against a 512 MiB box, and ``--ceiling-mib`` moves it."""
    assert limits_from().memory_mib == 400
    assert limits_from("--ceiling-mib", "320").memory_mib == 320
    assert limits_from("--anon-ceiling-mib", "280").anon_mib == 280
    assert limits_from("--recovery-mib", "16").recovery_mib == 16


def test_the_punch_harness_deliberately_does_not_gate_latency_or_throttling():
    """A saturated run's latencies are the state it created on purpose.

    Gating p95 here would fail every run by construction, which is how a gate gets switched
    off; the container harness (``capacity_test.py``) is where those two belong, and this
    asserts the punch half stays out of them.
    """
    limits = limits_from()
    assert limits.p95_seconds == float("inf")
    assert limits.p99_seconds == float("inf")
    assert limits.throttle_ratio == 1.0


def test_the_default_load_is_more_than_the_engine_can_run_at_once():
    """Saturation, not a smoke test: several punches inside one engine slot."""
    import config

    settings = config.build_settings()
    assert cli().clients > settings.face_inference_concurrency
    assert cli().rounds >= 2  # cumulative growth needs more than one burst to show


def test_the_profile_the_harness_runs_is_the_one_the_image_sets():
    """The env a run inherits is the deployment's, or the number describes another process."""
    dockerfile = (BACKEND_DIR.parent / "Dockerfile").read_text(encoding="utf-8")
    for name, value in saturation.DEPLOYMENT_ENV.items():
        if name.endswith("RATE_LIMIT"):  # a burst from one address needs its own budget
            continue
        assert f"{name}={value}" in dockerfile, f"{name}={value} is not what the image sets"


# --------------------------------------------------------------------------- #
# a healthy run, and one mutation per gate
# --------------------------------------------------------------------------- #
def test_a_healthy_run_passes_every_gate():
    gates = saturation.evaluate_gates(make_report(), limits_from())
    assert failing(gates) == set()
    assert {gate.name for gate in gates} == GATE_NAMES


def test_every_gate_line_carries_a_limit_an_observation_and_a_reason():
    for gate in saturation.evaluate_gates(make_report(), limits_from()):
        assert gate.limit and gate.observed and gate.detail, gate


def test_a_report_with_no_punches_cannot_pass():
    """An empty run is the one report that must never read as healthy."""
    gates = saturation.evaluate_gates(make_report(levels=0, punches=[]), limits_from())
    assert "load reached the pipeline" in failing(gates)


@pytest.mark.parametrize(
    "label, changes, expected",
    [
        ("rss over the ceiling", {"memory_mib": 430}, {"peak rss"}),
        ("anon over its ceiling while rss stays under", {"memory_mib": 395, "anon_mib": 390}, {"anonymous set"}),
        ("memory that becomes the new floor", {"settled_anon_mib": 330}, {"memory comes back"}),
        ("a punch refused in front of the photo", {"last_punch": punch(status=403)}, {"no refusals before the photo"}),
        (
            "a 500",
            {"last_punch": punch(status=500)},
            {"no server errors", "no refusals before the photo"},
        ),
    ],
)
def test_each_mutation_fails_exactly_the_gates_it_should(label, changes, expected):
    """One change away from healthy, and the failing gate names asserted as a set.

    A set rather than a count: a gate that stopped firing, fired for something else, or was
    quietly widened shows up here as a difference in *names*, not as a smaller number of
    failures that still looks like progress.
    """
    last = changes.pop("last_punch", None)
    punches = None if last is None else [punch() for _ in range(7)] + [last]
    gates = saturation.evaluate_gates(make_report(punches, **changes), limits_from())
    assert failing(gates) == expected, f"{label}: {sorted(failing(gates))}"


def test_the_kernel_high_water_mark_is_gated_not_only_the_sampled_maximum():
    """The spike between two samples is the spike this gate exists for.

    Every sampled reading says 300 MiB; the process's own ``HWM`` says it touched 430. A gate
    that only looked at the samples would be a function of ``--interval``.
    """
    report = make_report(memory_mib=300, peak_mib=430)
    assert report.measured.memory_max == 300 * MIB
    assert failing(saturation.evaluate_gates(report, limits_from())) == {"peak rss"}


def test_an_anonymous_set_over_its_own_ceiling_fails_on_its_own_line():
    """``anon`` is the part the kernel cannot evict: 390 MiB of it under a 400 MiB rss ceiling.

    The resident set passes and the line that actually predicts the OOM kill does not, which
    is the reason both numbers are gated.
    """
    gates = saturation.evaluate_gates(make_report(memory_mib=395, anon_mib=390), limits_from())
    assert failing(gates) == {"anonymous set"}


def test_a_platless_platform_falls_back_to_the_resident_set_for_recovery():
    """No ``RssAnon`` (Windows): the anon gate is *absent*, and recovery reads what exists."""
    gates = saturation.evaluate_gates(make_report(), limits_from(), anon_measured=False)
    assert failing(gates) == set()
    assert "anonymous set" not in {gate.name for gate in gates}


def test_growth_is_measured_between_idle_and_settled_not_between_start_and_peak():
    """A spike that comes back down is how this profile is supposed to work."""
    assert failing(saturation.evaluate_gates(make_report(memory_mib=390, anon_mib=380), limits_from())) == set()
    assert failing(saturation.evaluate_gates(make_report(settled_anon_mib=330), limits_from())) == {"memory comes back"}


def test_widening_the_ceiling_rescues_the_same_numbers():
    """The point of a configurable gate: the same report, re-judged, without re-running it."""
    report = make_report(memory_mib=430)
    assert failing(saturation.evaluate_gates(report, limits_from())) == {"peak rss"}
    assert failing(saturation.evaluate_gates(report, limits_from("--ceiling-mib", "460"))) == set()


# --------------------------------------------------------------------------- #
# the load itself: a cheap run is not a passing run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "punches, expected",
    [
        ("transport", {"no transport errors"}),
        ("server", {"no server errors", "no refusals before the photo"}),
        ("bare-503", {"shedding is the designed one"}),
        ("framed-503", set()),
        ("geofence", {"no refusals before the photo"}),
        ("nothing-scored", {"load reached the pipeline", "no refusals before the photo"}),
    ],
)
def test_the_load_must_have_been_answered_by_the_pipeline(punches, expected):
    """Each shape of answer says something different about what the run measured.

    A refused connection, a 500, a 503 with no ``Retry-After``, a punch stopped before its
    photo was read, and a run where nothing reached the model at all: the last two are the
    expensive ones, because they look like a fast, cheap, healthy deployment.
    """
    eight = [punch() for _ in range(7)]
    load = {
        "transport": eight + [punch(status=0)],
        "server": eight + [punch(status=500)],
        "bare-503": eight + [punch(status=503)],
        "framed-503": eight + [punch(status=503, retry_after="1")],
        "geofence": eight + [punch(status=403)],
        "nothing-scored": [punch(status=403) for _ in range(8)],
    }[punches]
    gates = saturation.evaluate_gates(make_report(load), limits_from())
    assert failing(gates) == expected


def test_shedding_is_not_a_server_error():
    """The 503 past the queue is the *designed* answer, and must not fail the 5xx gate.

    Counting it as a server error fails every run that saturated - the state this harness
    spends its run creating - so the gate would be switched off rather than fixed. It gets a
    line of its own instead, about the ``Retry-After``: the half a client actually needs.
    """
    half_shed = [punch() for _ in range(4)] + [punch(status=503, retry_after="1") for _ in range(4)]
    gates = saturation.evaluate_gates(make_report(half_shed), limits_from())
    assert failing(gates) == set()
    no_5xx = next(gate for gate in gates if gate.name == "no server errors")
    assert "503" in no_5xx.limit and no_5xx.observed == "0"


def test_pipeline_codes_are_the_three_the_pipeline_itself_writes():
    assert saturation.PIPELINE_CODES == {"face_mismatch", "face_not_found", "multiple_faces"}


@pytest.mark.parametrize(
    "result, reached",
    [
        (punch(), True),  # 200: scored, approved or flagged
        (punch(status=422, error_code="face_mismatch"), True),  # scored, and the score disagreed
        (punch(status=400, error_code="face_not_found"), True),  # decoded and detected, nothing to align
        (punch(status=400, error_code="multiple_faces"), True),
        (punch(status=422, error_code="liveness_failed"), False),  # refused before the compare
        (punch(status=403), False),  # refused before the photo was read
        (punch(status=503), False),  # shed: never entered the engine
        ({"status": "200", "error_code": ""}, True),  # statuses arrive as strings from JSON
    ],
)
def test_only_a_punch_that_could_have_allocated_a_frame_counts_as_pipeline(result, reached):
    assert saturation.reached_the_pipeline(result) is reached


# --------------------------------------------------------------------------- #
# reading one process: /proc, and the same report on a platform without RssAnon
# --------------------------------------------------------------------------- #
PROC_STATUS = """Name:\tpython
State:\tS (sleeping)
VmPeak:\t 1234567 kB
VmSize:\t 1200000 kB
VmHWM:\t  297000 kB
VmRSS:\t  291500 kB
RssAnon:\t 210000 kB
RssFile:\t  80000 kB
RssShmem:\t  1500 kB
Threads:\t12
"""


def test_the_proc_reader_takes_the_five_memory_fields_in_bytes():
    fields = saturation.parse_proc_status(PROC_STATUS)
    assert fields["VmRSS"] == 291_500 * 1024
    assert fields["VmHWM"] == 297_000 * 1024
    assert fields["RssAnon"] == 210_000 * 1024
    assert fields["RssFile"] == 80_000 * 1024
    assert fields["RssShmem"] == 1_500 * 1024


def test_the_anon_set_is_anon_plus_shmem_and_stays_absent_when_the_kernel_omits_it():
    """``RssAnon`` exists from Linux 4.5; its absence must turn a gate off, not pass it as 0."""
    assert saturation.parse_proc_status(PROC_STATUS).get("RssAnon")
    older = "\n".join(line for line in PROC_STATUS.splitlines() if not line.startswith(("RssAnon", "VmHWM")))
    assert "RssAnon" not in saturation.parse_proc_status(older)
    assert "VmHWM" not in saturation.parse_proc_status(older)


def test_a_live_process_reads_as_a_reading_and_not_as_zeroes():
    """Whichever reader this platform gets has to work on a process we can see."""
    reader, platform_name, anon_separable = saturation.process_reader()
    reading = reader(os.getpid())
    assert reading.memory_bytes > 0
    assert reading.peak_bytes >= reading.memory_bytes
    assert anon_separable is (platform_name == "proc")


def test_reading_without_the_five_fields_is_an_error_rather_than_a_healthy_zero(tmp_path: Path):
    """A parser that reported zeros would make every ceiling pass."""
    root = tmp_path / "proc"
    (root / "7").mkdir(parents=True)
    (root / "7" / "status").write_text("Name:\tpython\nThreads:\t4\n", encoding="utf-8")
    with pytest.raises(capacity.CapacityError):
        saturation.read_proc_reading(7, proc=root)
    with pytest.raises(capacity.CapacityError):
        saturation.read_proc_reading(8, proc=root)  # no such process at all


def test_a_real_proc_tree_walks_parents_before_children(tmp_path: Path):
    """The fake ``/proc`` here is the Linux half of the tree walk, tested on any platform."""
    root = tmp_path / "proc"
    for pid, ppid, comm in ((1, 0, "python"), (2, 1, "my app (x)"), (3, 2, "worker"), (4, 1, "sibling")):
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1 0", encoding="utf-8")
    parents = saturation.posix_parent_map(proc=root)
    assert parents == {1: 0, 2: 1, 3: 2, 4: 1}
    assert saturation.descendants_from(parents, 1) == [1, 2, 4, 3]
    assert saturation.descendants_from(parents, 3) == [3]  # a leaf is its own tree


def test_a_parent_map_without_the_root_still_yields_the_root():
    """A root that exited between the snapshot and the walk gets a reading error, not nothing."""
    assert saturation.descendants_from({}, 99) == [99]


# --------------------------------------------------------------------------- #
# which process is the application
# --------------------------------------------------------------------------- #
def reading(mib: int) -> capacity.CgroupReading:
    return capacity.CgroupReading(
        memory_bytes=mib * MIB,
        anon_bytes=mib * MIB,
        file_bytes=0,
        peak_bytes=mib * MIB,
        events={},
        usage_usec=0,
        nr_periods=0,
        nr_throttled=0,
        throttled_usec=0,
    )


def test_the_application_is_the_largest_process_under_the_pid_that_was_started():
    """A Windows venv's ``python.exe`` is a launcher: gating on its 5 MiB would never fail.

    So the harness measures the grandchild that holds the models - and on a platform where the
    tree is one process, that is the pid it started, unchanged.
    """
    sizes = {100: 5, 200: 216}
    reader = lambda pid: reading(sizes[pid])  # noqa: E731 - the seam resolve_app_pid takes
    pid, at_rest = saturation.resolve_app_pid(100, reader, tree=lambda _root: [100, 200])
    assert (pid, at_rest.memory_bytes) == (200, 216 * MIB)
    pid, _ = saturation.resolve_app_pid(100, reader, tree=lambda root: [root])
    assert pid == 100


def test_a_process_that_cannot_be_read_is_skipped_rather_than_guessed_at():
    def reader(pid: int) -> capacity.CgroupReading:
        if pid == 200:
            raise capacity.CapacityError("it exited between the snapshot and the read")
        return reading(5)

    assert saturation.resolve_app_pid(100, reader, tree=lambda _root: [100, 200])[0] == 100
    assert saturation.still_readable(200, reader) is False
    with pytest.raises(capacity.CapacityError) as refused:
        saturation.resolve_app_pid(100, reader, tree=lambda _root: [200])
    assert "nothing to measure" in str(refused.value)


def test_a_tie_keeps_the_pid_that_was_started():
    """Determinism, so two runs of the same harness describe the same process."""
    reader = lambda pid: reading(10)  # noqa: E731
    assert saturation.resolve_app_pid(100, reader, tree=lambda _root: [100, 200, 300])[0] == 100


class FakeProcess:
    """Just enough ``Popen`` for the shutdown path: a pid, a poll, and the two endings."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def test_shutdown_ends_the_children_before_the_launcher(monkeypatch):
    """Terminating the launcher alone orphans the interpreter, database and port included."""
    ended: list[int] = []
    monkeypatch.setattr(saturation, "end_process", ended.append)
    monkeypatch.setattr(saturation, "process_tree", lambda root: [root, 200, 300])
    handle = saturation.AppHandle(
        process=FakeProcess(100),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:9",
        data_dir=Path("/tmp"),
        log_path=Path("/tmp/app.log"),
        env={},
    )
    handle.stop()
    assert ended == [300, 200]  # deepest first, the launcher last
    assert handle.process.terminated and handle.process.waited


# --------------------------------------------------------------------------- #
# the face the punches send
# --------------------------------------------------------------------------- #
def test_a_face_free_frame_is_a_jpeg_the_decoder_can_open():
    from PIL import Image

    import io

    image = Image.open(io.BytesIO(saturation.faceless_frame()))
    assert image.format == "JPEG"
    assert image.size == (640, 640)


def test_faces_real_without_a_selfie_is_refused_rather_than_quietly_downgraded(monkeypatch):
    """The modes differ in what they exercise, so a downgrade has to be a failure, not a note."""
    monkeypatch.delenv("PUNCH_SATURATION_SELFIE", raising=False)
    monkeypatch.setattr(saturation, "default_selfie", lambda: "")
    with pytest.raises(capacity.CapacityError) as refused:
        saturation.resolve_selfie(cli("--faces", "real"))
    assert "embedding" in str(refused.value)

    mode, jpeg, note = saturation.resolve_selfie(cli())  # auto, and nothing to find
    assert mode == "none" and jpeg and "embedding step is not exercised" in note


def test_a_selfie_that_exists_is_used_and_the_mode_says_so(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PUNCH_SATURATION_SELFIE", raising=False)
    face = tmp_path / "me.jpg"
    face.write_bytes(saturation.faceless_frame())
    mode, jpeg, note = saturation.resolve_selfie(cli("--selfie", str(face)))
    assert (mode, len(jpeg)) == ("real", len(face.read_bytes()))
    assert str(face) in note


# --------------------------------------------------------------------------- #
# the child's world
# --------------------------------------------------------------------------- #
def test_the_child_gets_its_own_files_a_key_and_no_port(tmp_path: Path, monkeypatch):
    """Every file tree inside the throwaway directory, and nothing inherited from a laptop."""
    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setattr(saturation, "runtime_secret", lambda: ("test-secret", "generated"))
    env = saturation.child_env(tmp_path)
    for name, relative in saturation.DATA_DIR_ENV.items():
        assert env[name] == str(tmp_path / relative), name
    assert env["SECRET_KEY"] == "test-secret"
    assert "PORT" not in env
    assert float(env["FACE_INFERENCE_QUEUE"]) >= 1
    assert env["STANDING_SWEEP_ENABLED"] == "0"


def test_the_data_directory_is_throwaway_unless_one_is_named(tmp_path: Path):
    named = saturation.data_dir_for(cli("--data-dir", str(tmp_path / "run")))
    assert named == tmp_path / "run" and named.is_dir()
    made = saturation.data_dir_for(cli())
    assert made.is_dir() and made != named


def test_a_zero_client_or_round_is_refused_in_a_sentence():
    for argv in (["--clients", "0"], ["--rounds", "0"]):
        with pytest.raises(capacity.CapacityError):
            saturation.main(argv)


# --------------------------------------------------------------------------- #
# what the run prints, and what the exit code follows
# --------------------------------------------------------------------------- #
def test_the_verdict_line_and_the_exit_code_agree():
    """``main`` returns 1 when any gate failed, and the table says which - from one list."""
    healthy = saturation.evaluate_gates(make_report(), limits_from())
    assert all(gate.ok for gate in healthy)
    printed = saturation.render(make_report(), healthy, ["a note"], anon_measured=True)
    assert "VERDICT: PASS" in printed and "a note" in printed

    report = make_report(memory_mib=430)
    broken = saturation.evaluate_gates(report, limits_from())
    assert not all(gate.ok for gate in broken)
    printed = saturation.render(report, broken, [], anon_measured=True)
    assert "VERDICT: FAIL" in printed and "peak rss" in printed


def test_the_ceiling_is_named_in_the_report_the_artefact_carries():
    """A pipeline artefact has to say what it was judged against, not only the number."""
    printed = saturation.render(make_report(), saturation.evaluate_gates(make_report(), limits_from()), [], anon_measured=True)
    assert "400 MiB" in printed and "384 MiB" in printed
