"""The standing coverage report: re-measure the detectors when new gate frames land.

WHY THIS EXISTS
---------------
``tools/coverage_sweep.py`` answers one question on demand - *at which detector settings can this
deployment's real faces be found, and does SCRFD find more of them than YuNet* - and its answer is
only as current as the frames it was handed. That is the wrong shape for the decision it supports.
The detector comparison will change with the seasons, with a camera that gets moved, with a worker
who parks at the far end of the yard, and none of those announce themselves. A measurement taken
once, in the month somebody happened to sample a corpus, silently becomes a statement about that
month - and a migration is then argued from it months later.

So the sweep stands: it re-runs itself when there is something new to measure, writes down what it
found, and **flags the run where SCRFD overtakes YuNet** (and the one where it stops being ahead),
because a change *is* the finding here - four found-shares that moved by a frame are not.

WHAT "NEW FRAMES LANDED" MEANS
------------------------------
The trigger is the count of frames in the gate's own frame directory, which is the cheapest thing
that can be polled every few minutes without touching a detector or decoding an image. Two
consequences are deliberate:

* **a shrinking corpus does not count as new frames.** Retention erases punch frames on a schedule
  (``retention_punch_frame_days``), so the count legitimately falls; that re-baselines the trigger
  instead of blocking it, and the run says so.
* **the newest frames are the sample.** Frame *n* of a growing corpus is not the same evidence as
  its first *n* - the point of a standing report is what the camera is doing now. ``sample()``
  takes the newest frames up to a cap, which is also the whole cost control (see below).

WHY IT IS CHEAP ENOUGH TO STAND
-------------------------------
It runs on the same box that serves punches, so the sample is capped (200 frames by default, about
90 seconds of detector time across the four configurations), the cadence is daily, the first run
after boot is delayed past the model preload, and the work happens on a **thread in this process
rather than an asyncio task** - the same reasoning as the retention sweeper, because blocking CPU
work in the event loop would stall every punch at the gate while it measured them. A deployment
that would rather run it elsewhere sets ``STANDING_SWEEP_ENABLED=0`` and schedules
``python -m coverage_report --once`` from cron, which is the same code path.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import notifications
from config import settings

#: The four configurations, the folder source and the verdict all live in the tool the operator
#: runs by hand - imported rather than reimplemented, so the standing report and the command line
#: cannot answer the same question two different ways.
_BACKEND_DIR = str(Path(__file__).resolve().parent)
if _BACKEND_DIR not in sys.path:  # pragma: no cover - the app's own directory, always importable
    sys.path.insert(0, _BACKEND_DIR)
# Appended rather than prepended: the tool is a *tool*, so a name in ``backend/tools`` must never
# shadow an application module or a stdlib one for the rest of the process.
_TOOLS_DIR = str(Path(_BACKEND_DIR) / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)

import coverage_sweep  # noqa: E402  (the tools directory is on the path for exactly this import)

#: Where the snapshots and the state file live. The operator points this at the volume
#: (``STANDING_SWEEP_DIR=/data/coverage_reports``); the default sits beside the app, which is
#: ephemeral on a platform - the report is regenerated, so losing it costs one cadence.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# what changed
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CorpusFingerprint:
    """The cheapest honest description of a folder of frames: how many, and how new."""

    frames: int
    newest: str | None
    newest_name: str | None
    total_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "newest": self.newest,
            "newest_name": self.newest_name,
            "total_bytes": self.total_bytes,
        }


def fingerprint(folder: str | Path) -> CorpusFingerprint:
    """Count the frames in a directory, without opening one.

    One ``scandir``, no decode: this runs every tick, and a probe that costs a second per check
    would cost more than the measurement it is deciding to take. A missing folder is zero frames
    rather than an error - a deployment without gate frames yet is a state, not a fault, and the
    watcher says so at startup rather than on every tick.
    """
    root = Path(folder)
    count = 0
    total = 0
    newest: tuple[float, str] | None = None
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file() or not entry.name.lower().endswith(IMAGE_SUFFIXES):
                    continue
                stat = entry.stat()
                count += 1
                total += int(stat.st_size)
                stamp = (stat.st_mtime, entry.name)
                if newest is None or stamp > newest:
                    newest = stamp
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return CorpusFingerprint(frames=0, newest=None, newest_name=None, total_bytes=0)
    newest_text = (
        datetime.fromtimestamp(newest[0]).strftime("%Y-%m-%d %H:%M:%S") if newest else None
    )
    return CorpusFingerprint(
        frames=count,
        newest=newest_text,
        newest_name=newest[1] if newest else None,
        total_bytes=total,
    )


def sample(folder: str | Path, limit: int) -> list[str]:
    """The newest ``limit`` frames, oldest-first within the sample, or all of them when ``limit`` is 0.

    Newest-first selection, then reversed, so the sweep reads them in capture order and the report's
    per-frame entries stay comparable between runs. The cap is what keeps a standing job from
    becoming a load test on a live gate; it is a *sample* and the report says so, with the corpus
    size beside it.
    """
    root = Path(folder)
    try:
        with os.scandir(root) as entries:
            candidates = [
                (entry.stat().st_mtime, entry.name, entry.path)
                for entry in entries
                if entry.is_file() and entry.name.lower().endswith(IMAGE_SUFFIXES)
            ]
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen = candidates if limit <= 0 else candidates[: int(limit)]
    return [path for _stamp, _name, path in sorted(chosen, key=lambda item: (item[0], item[1]))]


@dataclass
class Decision:
    """Whether to measure now, and the sentence that says why not."""

    should_run: bool
    reason: str
    #: The corpus shrank since the last run, so the baseline is moved without measuring: retention
    #: deleted frames, which is not evidence about a detector.
    rebaseline: bool = False


def decide(
    *,
    last_frames: int | None,
    last_run_at: datetime | None,
    current: CorpusFingerprint,
    now: datetime,
    interval_seconds: int,
    min_new_frames: int,
) -> Decision:
    """Should this tick measure, given what the last run saw? Pure, so the rules are testable.

    The order of the checks is the order an operator would ask them in, and each "no" is a sentence
    they can act on:

    1. nothing to measure - the corpus is empty;
    2. the corpus shrank - retention did that, re-baseline and say so;
    3. too few new frames - the whole point is to measure *change*, and a handful of frames cannot
       move a found-share by more than noise;
    4. measured too recently - the cadence is the floor, whatever the frames do;
    5. otherwise, measure.
    """
    if current.frames <= 0:
        return Decision(False, "the frame folder holds nothing to measure")
    if last_frames is None:
        return Decision(True, "no previous run")
    if current.frames < last_frames:
        return Decision(
            False,
            f"the corpus shrank from {last_frames} to {current.frames} frame(s) (retention), "
            "re-baselining without measuring",
            rebaseline=True,
        )
    new_frames = current.frames - last_frames
    if new_frames < min_new_frames:
        return Decision(
            False, f"{new_frames} new frame(s) since the last run; {min_new_frames} are needed"
        )
    if last_run_at is not None and now - last_run_at < timedelta(seconds=interval_seconds):
        waited = (now - last_run_at).total_seconds()
        return Decision(
            False,
            f"{new_frames} new frame(s), but the last run was {int(waited / 60)} minute(s) ago "
            f"and the cadence is {int(interval_seconds / 60)}",
        )
    return Decision(True, f"{new_frames} new frame(s) since the last run")


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------
def _kind(name: str, config: dict[str, Any]) -> str:
    """The detector family a configuration ran, from the fingerprint, falling back to its name.

    The fingerprint is the authority (it is what the run recorded); the name is the same fact in the
    spelling ``scrfd@640``, which keeps the comparison working on a report written before the
    fingerprint carried the field.
    """
    kind = (config.get("spec") or {}).get("kind")
    return str(kind) if kind else name.split("@", 1)[0]


def _config_view(config: dict[str, Any]) -> dict[str, Any]:
    frames = int(config.get("frames") or 0)
    found = int(config.get("found") or 0)
    return {
        "found": found,
        "frames": frames,
        "share": (found / frames) if frames else 0.0,
        "ms_per_frame": round(float(config.get("total_detect_ms") or 0.0) / frames, 1) if frames else 0.0,
    }


def verdict(report: dict[str, Any], *, min_margin: int = 1) -> dict[str, Any]:
    """Compare SCRFD with the best YuNet configuration, in the terms the migration is argued in.

    The question is deliberately *not* "is SCRFD better in general" - it is "does the fourth
    configuration recover frames that the YuNet configurations lose, on this deployment's frames,
    often enough to be worth a second model". So the reference is the **best** YuNet configuration
    of that run rather than a fixed one (usually ``yunet@640``, occasionally the tiled pass), and the
    answer carries the margin in frames: a one-frame margin is a tie in everything but arithmetic,
    which is why ``min_margin`` exists and defaults to the smallest honest step.
    """
    configs = report.get("configs") or {}
    yunet = {
        name: _config_view(config)
        for name, config in configs.items()
        if _kind(name, config) == "yunet"
    }
    scrfd_name = next(
        (name for name, config in configs.items() if _kind(name, config) == "scrfd"), None
    )
    result: dict[str, Any] = {
        "rule": report.get("subject", coverage_sweep.SUBJECT_EXACTLY_ONE),
        "threshold": (report.get("gate") or {}).get("min_score"),
        "yunet": yunet,
        "best_yunet": None,
        "scrfd": None,
        "margin_frames": None,
        "scrfd_measured": scrfd_name is not None,
        "scrfd_ahead": None,
        "min_margin_frames": int(min_margin),
    }
    if not yunet or scrfd_name is None:
        # Not a verdict: the fourth configuration was never run (its model is absent), so saying
        # "YuNet wins" would be the paper-assumption this whole feature exists to replace.
        result["detail"] = (
            "the SCRFD configuration was not measured, so there is no comparison to report"
            if scrfd_name is None
            else "no YuNet configuration was measured"
        )
        return result

    # The *best* YuNet configuration, not a fixed one: the question is whether the second network
    # beats what this deployment already runs, and which of the three that is can change with the
    # cameras. The tie-break is alphabetical so the same run answers the same way twice.
    best_name, best = max(yunet.items(), key=lambda item: (item[1]["found"], item[0]))
    scrfd = configs[scrfd_name]
    scrfd_view = {"name": scrfd_name, **_config_view(scrfd), "misses_by_reason": scrfd.get("misses_by_reason") or {}}
    margin = scrfd["found"] - best["found"]
    result.update(
        {
            "best_yunet": {"name": best_name, **best},
            "scrfd": scrfd_view,
            "margin_frames": margin,
            "margin_share": (scrfd_view["share"] - best["share"]),
            "scrfd_ahead": margin >= int(min_margin),
        }
    )
    return result


def verdict_line(verdict_payload: dict[str, Any]) -> str:
    """The verdict as one sentence, for a log line, a notification body and a shell summary."""
    if not verdict_payload.get("scrfd_measured"):
        return verdict_payload.get("detail") or "SCRFD was not measured"
    best = verdict_payload["best_yunet"]
    scrfd = verdict_payload["scrfd"]
    margin = verdict_payload["margin_frames"]
    if verdict_payload["scrfd_ahead"]:
        lead = f"SCRFD is ahead by {margin} frame(s)"
    elif margin == 0:
        lead = "SCRFD matches the best YuNet configuration exactly"
    else:
        lead = f"SCRFD is behind by {abs(margin)} frame(s)"
    return (
        f"{lead} ({scrfd['name']} {scrfd['found']}/{scrfd['frames']} vs "
        f"{best['name']} {best['found']}/{best['frames']}, rule {verdict_payload['rule']}, "
        f"{scrfd['ms_per_frame']} ms/frame vs {best['ms_per_frame']})"
    )


# ---------------------------------------------------------------------------
# state and snapshots
# ---------------------------------------------------------------------------
def state_path(report_dir: str | Path) -> Path:
    return Path(report_dir) / "state.json"


def latest_path(report_dir: str | Path) -> Path:
    return Path(report_dir) / "latest.json"


def load_json(path: str | Path) -> dict[str, Any] | None:
    """Read a JSON file, or ``None``. A corrupt report is not a reason to stop measuring."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: a reader must never see half a report."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(temporary, target)


def load_state(report_dir: str | Path) -> dict[str, Any]:
    return load_json(state_path(report_dir)) or {}


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def prune_snapshots(report_dir: str | Path, keep: int) -> None:
    """Keep the newest ``keep`` run snapshots; the rest are history nobody read."""
    if keep <= 0:
        return
    runs = sorted(Path(report_dir).glob("run-*.json"))
    for stale in runs[:-keep]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover - a file someone else already removed
            pass


# ---------------------------------------------------------------------------
# one measurement
# ---------------------------------------------------------------------------
def measure(
    *,
    folder: str | Path,
    report_dir: str | Path,
    limit: int,
    min_score: float,
    subject: str,
    min_margin: int,
    keep: int,
    scrfd_model: str | None = None,
    now: datetime | None = None,
    progress=None,
) -> dict[str, Any]:
    """Run the four configurations over the newest frames and persist the result.

    Raises :class:`coverage_sweep.SweepError` when a configuration cannot be built - the caller
    decides whether that is a startup failure or a line in a log, and the message already names the
    file to replace.
    """
    moment = now or datetime.now()
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    spec_slug = moment.strftime("%Y%m%d-%H%M%S")
    all_frames = fingerprint(folder)
    chosen = sample(folder, limit)
    if not chosen:
        raise coverage_sweep.SweepError(
            f"no frames to measure in {folder}: the folder holds no images, so a report from it "
            "would be four configurations that measured nothing"
        )

    specs, skipped_configs, _models = coverage_sweep.resolve_specs(
        scrfd_model=scrfd_model, min_score=min_score
    )
    from corpus_ingest import DirectorySource

    source = DirectorySource(Path(folder), paths=chosen)
    report = coverage_sweep.sweep(
        source,
        specs,
        subject=subject,
        progress=progress,
        skipped_configs=skipped_configs,
        source_info={
            "kind": "gate_frames",
            "root": str(folder),
            "sample": len(chosen),
            "sample_limit": int(limit),
            "corpus_frames": all_frames.frames,
            "corpus_newest": all_frames.newest,
        },
    )
    outcome = verdict(report, min_margin=min_margin)
    payload = {
        "measured_at": stamp,
        "corpus": all_frames.as_dict(),
        "sample": {"frames": len(chosen), "limit": int(limit)},
        "models": report.get("models"),
        "verdict": outcome,
        "summary": verdict_line(outcome),
        "report": report,
    }
    write_json(latest_path(report_dir), payload)
    write_json(Path(report_dir) / f"run-{spec_slug}.json", payload)
    prune_snapshots(report_dir, keep)
    return payload


def _notify(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """Announce a *change*, not a state. Returns whether a notification was created.

    Only the crossing matters: SCRFD ahead when it was not, or not ahead when it was. A report that
    says the same thing every day is a report nobody opens, and the transition is the moment
    somebody can act on it (a camera was moved, the light changed, the corpus moved to a different
    gate). The dedupe key still carries the direction and the day, so a restart loop cannot turn one
    change into a stream.
    """
    outcome = current.get("verdict") or {}
    if not outcome.get("scrfd_measured"):
        return False
    # Unknown reads as "was not ahead": the first report on a deployment is the first time anybody
    # knows the answer, so SCRFD winning it is a finding worth a notice, and YuNet winning it is not
    # a "moved back" - there was no earlier state for it to move back from.
    was = bool((previous or {}).get("verdict", {}).get("scrfd_ahead"))
    now = bool(outcome.get("scrfd_ahead"))
    if was == now:
        return False
    summary = current.get("summary") or verdict_line(outcome)
    if now:
        title = "SCRFD now recovers more gate frames than YuNet"
        body = f"{summary}. Worth re-reading the migration runbook before the next change."
        severity = notifications.SEVERITY_WARNING
    else:
        title = "YuNet is ahead again on the gate's own frames"
        body = f"{summary}. The detector comparison has moved back."
        severity = notifications.SEVERITY_INFO
    day = str(current.get("measured_at") or "")[:10]
    try:
        from database import db

        # ``write=True`` or the insert is rolled back when the block exits: an uncommitted
        # notification looks delivered to the caller and is invisible to the console.
        with db(write=True) as conn:
            return notifications.notify(
                conn,
                kind=notifications.KIND_COVERAGE_REPORT,
                severity=severity,
                title=title,
                body=body,
                payload={"verdict": outcome, "measured_at": current.get("measured_at")},
                dedupe_key=f"coverage_report:{'ahead' if now else 'behind'}:{day}",
            )
    except sqlite3.Error as exc:  # pragma: no cover - a notification must never raise
        _log.warning("could not record the coverage report notification: %s", exc)
        return False


def tick(
    *,
    folder: str | Path | None = None,
    report_dir: str | Path | None = None,
    interval_seconds: int | None = None,
    min_new_frames: int | None = None,
    limit: int | None = None,
    min_score: float | None = None,
    subject: str | None = None,
    min_margin: int | None = None,
    keep: int | None = None,
    now: datetime | None = None,
    progress=None,
) -> dict[str, Any]:
    """One cadence: decide, maybe measure, notify on a change, remember. Safe to call by hand.

    Everything is injectable for the same reason the sweep's internals are: a test has to be able to
    ask "would this tick measure?" without a corpus, a model or a waiting period, and an operator has
    to be able to run exactly this function once (``python -m coverage_report --once``).
    """
    moment = now or datetime.now()
    corpus_dir = Path(folder if folder is not None else settings.standing_sweep_corpus_dir)
    target = Path(report_dir if report_dir is not None else settings.standing_sweep_dir)
    cadence = int(
        interval_seconds if interval_seconds is not None else settings.standing_sweep_interval_seconds
    )
    needed = int(
        min_new_frames if min_new_frames is not None else settings.standing_sweep_min_new_frames
    )
    sample_limit = int(limit if limit is not None else settings.standing_sweep_sample)
    threshold = float(min_score if min_score is not None else settings.standing_sweep_min_score)
    counting = subject or settings.standing_sweep_subject
    margin = int(min_margin if min_margin is not None else settings.standing_sweep_min_margin_frames)
    keep_runs = int(keep if keep is not None else settings.standing_sweep_keep)

    state = load_state(target)
    current = fingerprint(corpus_dir)
    decision = decide(
        last_frames=None if state.get("frames") is None else int(state["frames"]),
        last_run_at=parse_time((state.get("last_run") or {}).get("measured_at")),
        current=current,
        now=moment,
        interval_seconds=cadence,
        min_new_frames=needed,
    )
    summary: dict[str, Any] = {
        "at": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "ran": False,
        "reason": decision.reason,
        "corpus": current.as_dict(),
        "corpus_dir": str(corpus_dir),
    }
    if not decision.should_run:
        # The baseline only moves when the corpus shrank: any other tick leaves it where the last
        # run left it, so "new frames since the last run" keeps meaning that.
        if decision.rebaseline or state.get("frames") is None:
            state["frames"] = current.frames
            state["corpus"] = current.as_dict()
            state["updated_at"] = summary["at"]
            write_json(state_path(target), state)
        summary["verdict"] = (state.get("last_run") or {}).get("verdict")
        return summary

    payload = measure(
        folder=corpus_dir,
        report_dir=target,
        limit=sample_limit,
        min_score=threshold,
        subject=counting,
        min_margin=margin,
        keep=keep_runs,
        now=moment,
        progress=progress,
    )
    changed = _notify(state.get("last_run"), payload)
    state.update(
        {
            "frames": current.frames,
            "corpus": current.as_dict(),
            "updated_at": summary["at"],
            "runs": int(state.get("runs") or 0) + 1,
            "last_run": {
                "measured_at": payload["measured_at"],
                "sample": payload["sample"],
                "verdict": payload["verdict"],
                "summary": payload["summary"],
                "notified": changed,
            },
        }
    )
    write_json(state_path(target), state)
    summary.update(
        {
            "ran": True,
            "measured_at": payload["measured_at"],
            "sample": payload["sample"],
            "verdict": payload["verdict"],
            "summary": payload["summary"],
            "notified": changed,
        }
    )
    return summary


def summary(report_dir: str | Path | None = None) -> dict[str, Any]:
    """What the console reads: the standing shape of the job plus the last verdict, no frame list."""
    target = Path(report_dir if report_dir is not None else settings.standing_sweep_dir)
    state = load_state(target)
    latest = load_json(latest_path(target))
    last_run = state.get("last_run") or {}
    return {
        "enabled": bool(settings.standing_sweep_enabled),
        "corpus_dir": str(settings.standing_sweep_corpus_dir),
        "report_dir": str(target),
        "interval_hours": round(int(settings.standing_sweep_interval_seconds) / 3600, 2),
        "sample_limit": int(settings.standing_sweep_sample),
        "min_new_frames": int(settings.standing_sweep_min_new_frames),
        "min_margin_frames": int(settings.standing_sweep_min_margin_frames),
        "subject_rule": settings.standing_sweep_subject,
        "runs": int(state.get("runs") or 0),
        "corpus": state.get("corpus"),
        "last_run": last_run or None,
        "verdict": last_run.get("verdict") or (latest or {}).get("verdict"),
        "measured_at": last_run.get("measured_at") or (latest or {}).get("measured_at"),
    }


# ---------------------------------------------------------------------------
# the standing timer
# ---------------------------------------------------------------------------
_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _loop(interval: int, initial_delay: int) -> None:
    if initial_delay > 0 and _stop_event.wait(initial_delay):
        return
    while not _stop_event.is_set():
        try:
            result = tick()
            if result["ran"]:
                _log.info("standing coverage report: %s", result.get("summary") or result["reason"])
            else:
                _log.info("standing coverage report: not measured yet - %s", result["reason"])
        except coverage_sweep.SweepError as exc:
            # A configuration that will not build is a file to replace, not a crash: logged once per
            # tick (daily by default), and the next tick tries again in case somebody fixed it.
            _log.warning("standing coverage report cannot run: %s", exc)
        except Exception as exc:  # noqa: BLE001 - the timer must survive whatever a run throws
            _log.warning("standing coverage report failed: %s", exc, exc_info=True)
        if _stop_event.wait(interval):
            return


def start_watcher(
    *,
    interval: int | None = None,
    enabled: bool | None = None,
    initial_delay: int | None = None,
) -> bool:
    """Start the standing timer. Returns whether it is running.

    Skipped when ``STANDING_SWEEP_ENABLED=0``, which is what the test suite, a read-only replica and
    any deployment that would rather schedule the run itself want. Like the overtime watcher, the
    reason it is *not* running is said out loud at startup: a standing report that never fires is
    indistinguishable from a standing report that has nothing to say, and the difference is
    entirely in the settings and the corpus folder.
    """
    global _thread
    on = settings.standing_sweep_enabled if enabled is None else enabled
    if not on:
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event.clear()
    seconds = int(interval if interval is not None else settings.standing_sweep_interval_seconds)
    delay = int(
        initial_delay if initial_delay is not None else settings.standing_sweep_initial_delay_seconds
    )
    corpus = fingerprint(settings.standing_sweep_corpus_dir)
    if corpus.frames == 0:
        _log.warning(
            "standing coverage report started (every %ss) but %s holds no frames yet, so there is "
            "nothing to measure: it will begin once punches store frames there (sample %s frames)",
            seconds,
            settings.standing_sweep_corpus_dir,
            settings.standing_sweep_sample,
        )
    else:
        _log.info(
            "standing coverage report started (every %ss, %s frame(s) in %s, newest %s, sample %s, "
            "+%s new frames to trigger)",
            seconds,
            corpus.frames,
            settings.standing_sweep_corpus_dir,
            corpus.newest,
            settings.standing_sweep_sample,
            settings.standing_sweep_min_new_frames,
        )
    _thread = threading.Thread(
        target=_loop, args=(seconds, delay), name="coverage-report", daemon=True
    )
    _thread.start()
    return True


def watcher_running() -> bool:
    """Whether the standing timer is up - the question a readiness check and a test both ask."""
    return _thread is not None and _thread.is_alive()


def stop_watcher(*, timeout: float = 2.0) -> None:
    """Signal the timer to stop and wait briefly for it (used at shutdown)."""
    global _thread
    _stop_event.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _thread = None


def main(argv: list[str] | None = None) -> int:
    """``python -m coverage_report`` - one tick, or a single measurement, from a shell.

    Present because a standing report nobody can run by hand is a standing report nobody trusts: the
    same entry point the timer uses is the one an operator schedules from cron (with the watcher
    off) when they would rather not spend the gate's CPU on it.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Run the standing coverage report once.")
    parser.add_argument("--corpus", default=None, help="folder of gate frames (default: settings)")
    parser.add_argument("--report-dir", default=None, help="where snapshots and state live")
    parser.add_argument("--sample", type=int, default=None, help="frames to measure (0 = all)")
    parser.add_argument("--force", action="store_true",
                        help="measure even when the trigger says there is nothing new")
    parser.add_argument("--json", default=None, help="write the tick summary here")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    common: dict[str, Any] = {}
    if args.corpus:
        common["folder"] = args.corpus
    if args.report_dir:
        common["report_dir"] = args.report_dir
    if args.sample is not None:
        common["limit"] = args.sample

    try:
        if args.force:
            target = Path(args.report_dir or settings.standing_sweep_dir)
            payload = measure(
                folder=args.corpus or settings.standing_sweep_corpus_dir,
                report_dir=target,
                limit=int(args.sample if args.sample is not None else settings.standing_sweep_sample),
                min_score=float(settings.standing_sweep_min_score),
                subject=settings.standing_sweep_subject,
                min_margin=int(settings.standing_sweep_min_margin_frames),
                keep=int(settings.standing_sweep_keep),
                now=None,
                progress=lambda name, index, total: print(
                    f"\r  sweeping {index}/{total}: {name[:60]}", end="", flush=True
                ),
            )
            print("\r" + " " * 79 + "\r", end="")
            print(f"measured {payload['sample']['frames']} frame(s) at {payload['measured_at']}")
            print(f"  {payload['summary']}")
            result = {"ran": True, **payload}
        else:
            result = tick(**common)
            print(f"ran: {result['ran']} - {result['reason']}")
            if result.get("summary"):
                print(f"  {result['summary']}")
    except coverage_sweep.SweepError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        write_json(args.json, result)
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    sys.exit(main())
