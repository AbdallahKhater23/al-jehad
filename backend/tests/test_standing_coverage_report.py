"""The standing detector-coverage report: when it re-measures, and when it says the answer moved.

WHY THIS SUITE EXISTS
---------------------
``tools/coverage_sweep.py`` is a measurement anybody can take; this module is the *decision to
take it again*, and that decision is invisible in a way the rest of this application is not. A
standing report that quietly stops - because a timer was never started, because the corpus folder
is the wrong one, because the trigger asked for more new frames than the gate produces in a
season - looks exactly like a standing report that has nothing new to say. The four claims pinned
here are the ones a reader would otherwise have to take on trust:

1. **"New frames landed" is arithmetic over the folder, not a guess.** The trigger is a count, it
   needs a real *increase*, and a shrink (retention erasing frames on its schedule) re-baselines
   rather than firing or blocking forever.
2. **A measurement is persisted, and says what it measured.** The sample, the corpus it was drawn
   from, the models each family ran, and the per-frame evidence all land in the report files, so
   a verdict can be re-read rather than re-argued.
3. **The verdict is SCRFD against the *best* YuNet configuration, and a tie is a tie.** The
   comparison is the whole point of the job, so "SCRFD found one more frame than the best YuNet
   pass" has to be a distinct outcome from "SCRFD matched it" - and an unmeasured SCRFD
   configuration has to be reported as *not measured* rather than as a loss.
4. **Only a change is announced.** The notification is raised on the crossing - SCRFD starting to
   win, or ceasing to - because a report that says the same thing every day is one nobody opens,
   and the crossing is the moment somebody can act.
5. **It is a thread, and it stops.** Like the retention sweeper, and for the same reason: it runs
   detectors, which is blocking CPU work on the box every punch shares.

The sweep itself is not re-tested here (``test_coverage_sweep.py`` owns that); it is stubbed, so
these tests decide the *trigger, the verdict, the persistence and the announcement* without an
ONNX model or three minutes of detector time. The one thing that is deliberately not stubbed is
the resolution of the model files - ``measure`` goes through ``coverage_sweep.resolve_specs``,
and that sharing is asserted directly, because two places that each decided which weights the
four configurations run would eventually disagree.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coverage_report
import coverage_sweep
import harness
import notifications
from harness import ADMIN, WORKER, bearer

# ---------------------------------------------------------------------------
# helpers: a corpus of frames, and a sweep that returns what a test asks it to
# ---------------------------------------------------------------------------
FOUR_NAMES = ("yunet@320", "yunet@640", "yunet@640+2x2tiling", "scrfd@640")


def _write_frames(folder: Path, count: int, *, jpeg: bytes, start: datetime | None = None) -> None:
    """Write ``count`` tiny JPEGs into ``folder``, one second apart, oldest first.

    Distinct mtimes on purpose: "the newest frames" is the sample rule, and a corpus written in one
    filesystem tick would make the ordering (and therefore the test) depend on the clock's
    resolution rather than on the rule being pinned.
    """
    folder.mkdir(parents=True, exist_ok=True)
    moment = start or (datetime.now() - timedelta(days=1))
    for index in range(count):
        path = folder / f"frame-{index:04d}.jpg"
        path.write_bytes(jpeg)
        stamp = (moment + timedelta(seconds=index)).timestamp()
        import os

        os.utime(path, (stamp, stamp))


def _spec(kind: str, input_size: int, tiles: int = 1) -> dict:
    return {"kind": kind, "input_size": input_size, "tiles": tiles, "overlap": 0.2, "square": kind == "scrfd"}


def _outcome(frames: int, *, best_share: float = 0.8, scrfd_offset: int = 2) -> dict[str, int]:
    """Found-counts for a corpus of ``frames``, as the differences the verdict is actually about.

    Stated as a best-YuNet share plus an offset rather than four absolute counts on purpose: the
    standing report is measured on whatever the gate stored (tens of frames), so a test that hard-
    coded "SCRFD 33 vs YuNet 30" would be silently clamped into a tie by a six-frame corpus. The
    shape - one best YuNet pass, the others no better, SCRFD that many frames out in front - is what
    the verdict reads, and it survives any corpus size the trigger tests happen to use.
    """
    best = max(0, min(int(frames * best_share), frames))
    return {
        "yunet@320": best,
        "yunet@640": max(0, best - 1),
        "yunet@640+2x2tiling": best // 2,
        "scrfd@640": max(0, min(best + scrfd_offset, frames)),
    }


def _report(found: dict[str, int], frames: int, *, subject: str = "exactly_one", min_score: float = 0.6) -> dict:
    """A sweep report shaped like the tool's own, with found-counts the test chooses.

    Only the fields the verdict and the persistence read are filled in - the per-frame evidence is
    the tool's business, and re-stating its shape here would pin a copy instead of the contract.
    """
    configs = {}
    for name in FOUR_NAMES:
        hits = max(0, min(int(found.get(name, frames)), frames))
        configs[name] = {
            "spec": _spec("scrfd" if name.startswith("scrfd") else "yunet", 640 if "scrfd" in name else int(name.split("@")[1].split("+")[0])),
            "name": name,
            "frames": frames,
            "found": hits,
            "discarded": frames - hits,
            "misses_by_reason": {} if hits == frames else {"no_face": frames - hits},
            "multi_face_frames": 0,
            "bystander_frames": 0,
            "total_detect_ms": round(frames * (19.0 if "@320" in name else 64.0), 1),
            "outcomes": [],
        }
    return {
        "frames_read": frames,
        "unreadable": 0,
        "duplicate_frame_names": 0,
        "identities": [],
        "source": {"kind": "gate_frames"},
        "gate": {"min_score": min_score},
        "subject": subject,
        "models": {"yunet": ["/models/yunet.onnx"], "scrfd": ["/models/scrfd.onnx"]},
        "configs": configs,
        "flips": [],
        "skipped_configs": [],
    }


class StubbedSweep:
    """The sweep, replaced: records what it was asked to do and returns what the test chose.

    ``found`` is the one thing a test sets; everything else follows from it. ``samples`` records the
    frame paths the standing report actually handed over, which is how the sample cap is proven to
    reach the source rather than being computed and dropped.
    """

    def __init__(self) -> None:
        #: The best YuNet pass finds four fifths of the frames, and SCRFD finds ``scrfd_offset``
        #: more than that. Ahead by default, because "the fourth configuration wins" is the claim
        #: this whole job exists to test; a test that wants the other answer moves the offset.
        self.best_share: float = 0.8
        self.scrfd_offset: int = 2
        self.samples: list[list[str]] = []
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.skipped = False

    def specs(self) -> list:
        from corpus_ingest import DetectorSpec

        return [
            DetectorSpec(input_size=320, model_path="/models/yunet.onnx"),
            DetectorSpec(input_size=640, model_path="/models/yunet.onnx"),
            DetectorSpec(input_size=640, tiles=2, model_path="/models/yunet.onnx"),
            DetectorSpec(kind="scrfd", input_size=640, model_path="/models/scrfd.onnx"),
        ]

    def install(self, monkeypatch) -> None:
        def fake_resolve(*, detector_model=None, scrfd_model=None, skip_scrfd=False, min_score=0.6):
            self.calls.append({"resolve": {"min_score": min_score, "scrfd_model": scrfd_model}})
            return self.specs(), [], {"yunet": "/models/yunet.onnx", "scrfd": "/models/scrfd.onnx"}

        def fake_sweep(source, specs=None, *, gate=None, progress=None, skipped_configs=None, subject="exactly_one", source_info=None):
            if self.error is not None:
                raise self.error
            paths = [str(path) for path in (getattr(source, "paths", None) or [])]
            self.samples.append(paths)
            self.calls.append({"sweep": {"subject": subject, "frames": len(paths), "source_info": source_info}})
            found = _outcome(len(paths), best_share=self.best_share, scrfd_offset=self.scrfd_offset)
            report = _report(found, len(paths), subject=subject)
            if self.skipped:
                del report["configs"]["scrfd@640"]
            return report

        monkeypatch.setattr(coverage_sweep, "resolve_specs", fake_resolve)
        monkeypatch.setattr(coverage_sweep, "sweep", fake_sweep)


@pytest.fixture
def stubbed(monkeypatch) -> StubbedSweep:
    stub = StubbedSweep()
    stub.install(monkeypatch)
    return stub


@pytest.fixture
def standing(tmp_path, monkeypatch) -> dict:
    """A throwaway corpus, a throwaway report directory, and the settings pointing at both."""
    folder = tmp_path / "gate_frames"
    reports = tmp_path / "coverage_reports"
    live = coverage_report.settings
    monkeypatch.setattr(live, "standing_sweep_corpus_dir", folder)
    monkeypatch.setattr(live, "standing_sweep_dir", reports)
    monkeypatch.setattr(live, "standing_sweep_enabled", True)
    monkeypatch.setattr(live, "standing_sweep_interval_seconds", 24 * 3600)
    monkeypatch.setattr(live, "standing_sweep_min_new_frames", 5)
    monkeypatch.setattr(live, "standing_sweep_sample", 10)
    monkeypatch.setattr(live, "standing_sweep_min_margin_frames", 1)
    monkeypatch.setattr(live, "standing_sweep_keep", 3)
    monkeypatch.setattr(live, "standing_sweep_min_score", 0.6)
    monkeypatch.setattr(live, "standing_sweep_subject", "exactly_one")
    return {"folder": folder, "reports": reports}


def _alerts() -> list:
    return harness.db_rows(
        "SELECT kind, severity, title, body, dedupe_key FROM admin_notifications WHERE kind = ? ORDER BY id",
        (notifications.KIND_COVERAGE_REPORT,),
    )


# ---------------------------------------------------------------------------
# 1. the trigger: what "new frames landed" means
# ---------------------------------------------------------------------------
def test_the_fingerprint_counts_frames_and_names_the_newest(tmp_path, jpeg):
    _write_frames(tmp_path, 3, jpeg=jpeg)
    (tmp_path / "notes.txt").write_text("not a frame", encoding="utf-8")

    found = coverage_report.fingerprint(tmp_path)

    assert found.frames == 3, "a non-image in the folder is not a frame"
    assert found.newest_name == "frame-0002.jpg", "the newest frame is the one the count is about"
    assert found.total_bytes == 3 * len(jpeg)


def test_a_missing_or_empty_folder_fingerprints_as_nothing(tmp_path):
    assert coverage_report.fingerprint(tmp_path / "absent").frames == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert coverage_report.fingerprint(empty).frames == 0
    assert coverage_report.fingerprint(empty).newest is None


def test_the_sample_is_the_newest_frames_in_capture_order(tmp_path, jpeg):
    _write_frames(tmp_path, 6, jpeg=jpeg)

    chosen = coverage_report.sample(tmp_path, 3)

    assert [Path(path).name for path in chosen] == [
        "frame-0003.jpg",
        "frame-0004.jpg",
        "frame-0005.jpg",
    ], "the newest three, oldest-first inside the sample"


def test_a_zero_limit_means_every_frame(tmp_path, jpeg):
    _write_frames(tmp_path, 4, jpeg=jpeg)
    assert len(coverage_report.sample(tmp_path, 0)) == 4


def _decide(corpus_frames: int, *, last_frames=None, last_run=None, now=None, needed=5, interval=24 * 3600):
    moment = now or datetime(2026, 9, 24, 12, 0, 0)
    return coverage_report.decide(
        last_frames=last_frames,
        last_run_at=last_run,
        current=coverage_report.CorpusFingerprint(corpus_frames, None, None, 0),
        now=moment,
        interval_seconds=interval,
        min_new_frames=needed,
    )


def test_the_first_run_over_a_corpus_measures():
    decision = _decide(40)
    assert decision.should_run is True
    assert decision.rebaseline is False
    assert "no previous run" in decision.reason


def test_an_empty_corpus_is_never_measured():
    decision = _decide(0, last_frames=0)
    assert decision.should_run is False
    assert "nothing to measure" in decision.reason


def test_a_shrunken_corpus_rebaselines_instead_of_measuring():
    decision = _decide(30, last_frames=40, last_run=datetime(2026, 9, 23, 12, 0, 0))
    assert decision.should_run is False
    assert decision.rebaseline is True
    assert "shrank" in decision.reason


def test_too_few_new_frames_do_not_trigger_a_measurement():
    decision = _decide(42, last_frames=40, last_run=datetime(2026, 9, 23, 12, 0, 0))
    assert decision.should_run is False
    assert "2 new frame(s)" in decision.reason


def test_the_cadence_holds_even_when_the_corpus_grew_a_lot():
    decision = _decide(
        200,
        last_frames=40,
        last_run=datetime(2026, 9, 24, 6, 0, 0),
        now=datetime(2026, 9, 24, 12, 0, 0),
    )
    assert decision.should_run is False
    assert "cadence" in decision.reason


def test_enough_new_frames_past_the_cadence_measure():
    decision = _decide(60, last_frames=40, last_run=datetime(2026, 9, 23, 12, 0, 0))
    assert decision.should_run is True
    assert "20 new frame(s)" in decision.reason


# ---------------------------------------------------------------------------
# 2. the verdict: SCRFD against the best YuNet configuration
# ---------------------------------------------------------------------------
def test_the_verdict_compares_scrfd_with_the_best_yunet_configuration():
    report = _report(
        {"yunet@320": 40, "yunet@640": 37, "yunet@640+2x2tiling": 24, "scrfd@640": 44}, 48
    )

    outcome = coverage_report.verdict(report)

    assert outcome["best_yunet"]["name"] == "yunet@320", "the reference is the best YuNet pass, not a fixed one"
    assert outcome["scrfd"]["found"] == 44
    assert outcome["margin_frames"] == 4
    assert outcome["scrfd_ahead"] is True
    assert outcome["scrfd_measured"] is True


def test_a_tie_is_not_an_overtake():
    report = _report({"yunet@320": 38, "yunet@640": 37, "yunet@640+2x2tiling": 24, "scrfd@640": 38}, 48)
    outcome = coverage_report.verdict(report)
    assert outcome["margin_frames"] == 0
    assert outcome["scrfd_ahead"] is False
    assert "matches the best YuNet configuration exactly" in coverage_report.verdict_line(outcome)


def test_a_margin_below_the_configured_step_is_not_an_overtake():
    report = _report({"yunet@320": 40, "yunet@640": 37, "yunet@640+2x2tiling": 24, "scrfd@640": 41}, 48)
    assert coverage_report.verdict(report, min_margin=3)["scrfd_ahead"] is False
    assert coverage_report.verdict(report, min_margin=1)["scrfd_ahead"] is True


def test_an_unmeasured_scrfd_configuration_is_not_a_verdict():
    report = _report({"yunet@320": 38, "yunet@640": 37, "yunet@640+2x2tiling": 24, "scrfd@640": 0}, 48)
    del report["configs"]["scrfd@640"]

    outcome = coverage_report.verdict(report)

    assert outcome["scrfd_measured"] is False
    assert outcome["scrfd_ahead"] is None, "a comparison nobody ran must never read as a win or a loss"
    assert "not measured" in coverage_report.verdict_line(outcome)


def test_the_verdict_line_says_which_way_and_by_how_much():
    behind = coverage_report.verdict(_report({"yunet@320": 40, "yunet@640": 37, "yunet@640+2x2tiling": 24, "scrfd@640": 36}, 48))
    line = coverage_report.verdict_line(behind)
    assert "behind by 4 frame(s)" in line
    assert "scrfd@640 36/48" in line and "yunet@320 40/48" in line
    assert "exactly_one" in line, "the counting rule is part of the claim"


# ---------------------------------------------------------------------------
# 3. a measurement: what it writes, and what it remembers
# ---------------------------------------------------------------------------
def test_a_measurement_persists_the_verdict_with_its_evidence(standing, stubbed, jpeg):
    _write_frames(standing["folder"], 12, jpeg=jpeg)

    payload = coverage_report.measure(
        folder=standing["folder"],
        report_dir=standing["reports"],
        limit=10,
        min_score=0.6,
        subject="exactly_one",
        min_margin=1,
        keep=3,
    )

    assert len(stubbed.samples) == 1 and len(stubbed.samples[0]) == 10, "the sample cap reached the sweep"
    assert payload["sample"] == {"frames": 10, "limit": 10}
    assert payload["corpus"]["frames"] == 12, "the corpus size is recorded beside the sample"
    assert payload["verdict"]["scrfd_ahead"] is True
    assert payload["verdict"]["margin_frames"] == 2
    assert payload["models"]["scrfd"] == ["/models/scrfd.onnx"], "which weights ran travels in the report"

    latest = json.loads((standing["reports"] / "latest.json").read_text(encoding="utf-8"))
    assert latest["summary"] == payload["summary"]
    assert latest["report"]["frames_read"] == 10, "the per-frame evidence is kept, not just the verdict"
    assert list(standing["reports"].glob("run-*.json")), "each run keeps its own snapshot"


def test_the_resolution_of_the_models_is_shared_with_the_command_line(standing, stubbed, jpeg):
    """The report and the CLI must run the *same* weights, so the resolution is not re-implemented."""
    _write_frames(standing["folder"], 6, jpeg=jpeg)
    coverage_report.measure(
        folder=standing["folder"],
        report_dir=standing["reports"],
        limit=6,
        min_score=0.55,
        subject="any",
        min_margin=1,
        keep=1,
    )
    resolved = [call for call in stubbed.calls if "resolve" in call]
    swept = [call for call in stubbed.calls if "sweep" in call]
    assert resolved[0]["resolve"]["min_score"] == 0.55, "the gate the caller asked for is the gate that ran"
    assert swept[0]["sweep"]["subject"] == "any"
    assert swept[0]["sweep"]["source_info"]["kind"] == "gate_frames"


def test_older_snapshots_are_pruned_and_the_newest_kept(standing, stubbed, jpeg):
    _write_frames(standing["folder"], 6, jpeg=jpeg)
    for _ in range(4):
        coverage_report.measure(
            folder=standing["folder"],
            report_dir=standing["reports"],
            limit=4,
            min_score=0.6,
            subject="exactly_one",
            min_margin=1,
            keep=2,
        )
        time.sleep(1.1)  # the snapshot name carries a whole-second timestamp

    runs = sorted(standing["reports"].glob("run-*.json"))
    assert len(runs) == 2, f"keep=2, found {[r.name for r in runs]}"
    assert (standing["reports"] / "latest.json").exists()


def test_measuring_a_folder_with_no_frames_is_refused(standing, stubbed):
    with pytest.raises(coverage_sweep.SweepError) as raised:
        coverage_report.measure(
            folder=standing["folder"],
            report_dir=standing["reports"],
            limit=10,
            min_score=0.6,
            subject="exactly_one",
            min_margin=1,
            keep=1,
        )
    assert "no frames to measure" in str(raised.value)
    assert not (standing["reports"] / "latest.json").exists()


def test_a_tick_measures_then_remembers_the_corpus(standing, stubbed, jpeg, app_module):
    _write_frames(standing["folder"], 8, jpeg=jpeg)

    first = coverage_report.tick()
    assert first["ran"] is True and first["summary"]

    second = coverage_report.tick()
    assert second["ran"] is False, "nothing new landed, so nothing was measured"
    assert "0 new frame(s)" in second["reason"]

    state = json.loads((standing["reports"] / "state.json").read_text(encoding="utf-8"))
    assert state["frames"] == 8
    assert state["runs"] == 1
    assert state["last_run"]["verdict"]["scrfd_ahead"] is True


def test_a_tick_re_measures_once_enough_new_frames_land(standing, stubbed, jpeg, app_module):
    """New frames *and* a cadence that has elapsed: either alone is not enough, by design."""
    start = datetime(2026, 9, 20, 8, 0, 0)
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    coverage_report.tick(now=start)

    _write_frames(standing["folder"], 14, jpeg=jpeg)
    tomorrow = coverage_report.tick(now=start + timedelta(hours=30))
    assert tomorrow["ran"] is True, tomorrow["reason"]
    assert "6 new frame(s)" in tomorrow["reason"]
    assert len(stubbed.samples) == 2

    # Six more frames land an hour after that run: enough new evidence to measure, and the run
    # still waits. The cadence is the floor on how often the gate's own CPU is spent on this,
    # whatever the folder does.
    _write_frames(standing["folder"], 20, jpeg=jpeg)
    sooner = coverage_report.tick(now=start + timedelta(hours=31))
    assert sooner["ran"] is False
    assert "cadence" in sooner["reason"]


def test_a_tick_after_retention_erased_frames_re_baselines_without_measuring(standing, stubbed, jpeg, app_module):
    _write_frames(standing["folder"], 12, jpeg=jpeg)
    coverage_report.tick()

    for stale in sorted(standing["folder"].glob("frame-*.jpg"))[:6]:
        stale.unlink()
    shrunk = coverage_report.tick()

    assert shrunk["ran"] is False
    assert "shrank" in shrunk["reason"]
    state = json.loads((standing["reports"] / "state.json").read_text(encoding="utf-8"))
    assert state["frames"] == 6, "the baseline moves, so the next real frame counts as new"
    assert state["runs"] == 1


# ---------------------------------------------------------------------------
# 4. the announcement: only a change
# ---------------------------------------------------------------------------
def test_the_first_measurement_that_finds_scrfd_ahead_is_announced(standing, stubbed, jpeg, app_module):
    _write_frames(standing["folder"], 8, jpeg=jpeg)

    result = coverage_report.tick()

    assert result["notified"] is True
    alerts = _alerts()
    assert len(alerts) == 1
    kind, severity, title, body, dedupe = alerts[0]
    assert severity == notifications.SEVERITY_WARNING, "an overtake is a migration decision, not an FYI"
    assert "SCRFD" in title and "more gate frames" in title
    assert "ahead by 2 frame(s)" in body
    assert dedupe.startswith("coverage_report:ahead:")


def test_a_verdict_that_did_not_change_is_not_announced_again(standing, stubbed, jpeg, app_module):
    start = datetime(2026, 9, 20, 8, 0, 0)
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    coverage_report.tick(now=start)
    assert len(_alerts()) == 1

    _write_frames(standing["folder"], 14, jpeg=jpeg)
    stubbed.best_share = 0.6  # still ahead, now by more
    second = coverage_report.tick(now=start + timedelta(hours=30))

    assert second["ran"] is True
    assert second["notified"] is False
    assert len(_alerts()) == 1, "still ahead is not news; the crossing was the news"


def test_scrfd_ceasing_to_be_ahead_is_announced_too(standing, stubbed, jpeg, app_module):
    start = datetime(2026, 9, 20, 8, 0, 0)
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    coverage_report.tick(now=start)

    _write_frames(standing["folder"], 14, jpeg=jpeg)
    stubbed.scrfd_offset = -4  # the cameras moved, or the corpus did
    second = coverage_report.tick(now=start + timedelta(hours=30))

    assert second["notified"] is True
    alerts = _alerts()
    assert len(alerts) == 2
    assert alerts[1][1] == notifications.SEVERITY_INFO
    assert "ahead again" in alerts[1][2]
    assert alerts[1][4].startswith("coverage_report:behind:")


def test_a_verdict_that_was_never_ahead_is_silent_on_the_first_measurement(standing, stubbed, jpeg, app_module):
    """Nothing "moved back" the first time, so there is nothing to say."""
    stubbed.scrfd_offset = -4
    _write_frames(standing["folder"], 8, jpeg=jpeg)

    result = coverage_report.tick()

    assert result["ran"] is True
    assert result["notified"] is False
    assert _alerts() == []


def test_an_unmeasured_scrfd_configuration_is_never_announced(standing, stubbed, jpeg, app_module):
    """A comparison that was not run cannot have "overtaken" anything."""
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    stubbed.skipped = True  # the model file is absent: three configurations, and no comparison

    result = coverage_report.tick()

    assert result["ran"] is True
    assert result["verdict"]["scrfd_measured"] is False
    assert result["notified"] is False
    assert _alerts() == []


# ---------------------------------------------------------------------------
# 5. the surface and the timer
# ---------------------------------------------------------------------------
def test_the_summary_answers_the_console_from_the_written_report(standing, stubbed, jpeg, app_module):
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    coverage_report.tick()

    summary = coverage_report.summary()

    assert summary["enabled"] is True
    assert summary["runs"] == 1
    assert summary["sample_limit"] == 10
    assert summary["min_new_frames"] == 5
    assert summary["verdict"]["scrfd_ahead"] is True
    assert summary["measured_at"]
    assert "outcomes" not in json.dumps(summary), "the console payload is the verdict, not the frame list"


def test_the_summary_of_a_report_that_never_ran_says_so(standing, app_module):
    summary = coverage_report.summary()
    assert summary["runs"] == 0
    assert summary["verdict"] is None
    assert summary["measured_at"] is None


def test_the_endpoint_serves_the_verdict_to_an_admin(client, standing, stubbed, jpeg, app_module):
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    coverage_report.tick()

    response = client.get("/api/v1/admin/coverage_report", headers=bearer(ADMIN))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"]["scrfd_ahead"] is True
    assert body["corpus"]["frames"] == 8


def test_the_endpoint_is_closed_to_everyone_else(client, standing, app_module):
    assert client.get("/api/v1/admin/coverage_report", headers=bearer(WORKER)).status_code == 403
    assert client.get("/api/v1/admin/coverage_report").status_code == 401


def test_the_watcher_does_not_start_when_it_is_disabled(app_module):
    assert coverage_report.start_watcher(enabled=False) is False
    assert coverage_report.watcher_running() is False


def test_starting_the_watcher_twice_does_not_start_two_reports(app_module):
    try:
        assert coverage_report.start_watcher(interval=3600, initial_delay=3600, enabled=True) is True
        assert coverage_report.start_watcher(interval=3600, initial_delay=3600, enabled=True) is True
        assert sum(1 for thread in threading.enumerate() if thread.name == "coverage-report") == 1
        assert coverage_report.watcher_running() is True
    finally:
        coverage_report.stop_watcher(timeout=10)


def test_the_watcher_runs_a_tick_and_then_stops(standing, stubbed, jpeg, app_module, monkeypatch):
    """The timer calls the real ``tick``; only the detector pass underneath it is stubbed."""
    _write_frames(standing["folder"], 8, jpeg=jpeg)
    seen: list[dict] = []
    real_tick = coverage_report.tick

    def recording_tick(**kwargs):
        result = real_tick(**kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(coverage_report, "tick", recording_tick)
    try:
        assert coverage_report.start_watcher(interval=1, initial_delay=0, enabled=True) is True
        deadline = time.time() + 20
        while not seen and time.time() < deadline:
            time.sleep(0.1)
    finally:
        coverage_report.stop_watcher(timeout=10)

    assert seen, "the standing report never ran"
    assert seen[0]["ran"] is True
    assert coverage_report.watcher_running() is False


def test_stopping_a_watcher_that_never_started_is_harmless(app_module):
    coverage_report.stop_watcher(timeout=0)
    assert coverage_report.watcher_running() is False
