"""The six file trees are named in the environment, so a *child* process writes where the server does.

WHY THIS EXISTS
---------------
``harness`` keeps a test run out of the checkout by repointing six module attributes —
``main.LOCAL_REFS_DIR``, ``main.WORKER_PHOTOS_DIR``, ``punch_frames.FRAMES_DIR``,
``quick_links.PHOTOS_DIR``, ``registrations.PHOTOS_DIR`` and ``corpus.ROOT_DIR`` — and every
module reads its own at call time,
so one repoint (and
one database reset) carries the whole rotation. That works for code running *inside* the test
process, which is where every rotation test lives, and nowhere else.

A module attribute does not cross a process boundary; the environment does. So a child that
imported the application — a report, an operator's script, a second worker, the load-test tool —
resolved those paths to the directories inside the project and wrote there: biometric
templates, enrollment selfies, punch evidence and quick-link photos landing in the checkout of a
test run, with no row in the rotated database and nothing in the log. The rotation tests could
not see it, because the leak happened in a process they never inspected.

``DATABASE_PATH`` never had that problem — it is read from the environment by ``config``, so a
child inherits the answer. These now work the same way, and this suite pins the three
things that make it true:

1. **``config`` reads them**, one environment variable per tree, defaulting to the directory
   inside the project the application has always used (a deployment that sets none of them is
   unchanged).
2. **The application names its directories from the settings**, so a child that sets nothing but
   the variables gets the same answers the parent has - asserted in a child process,
   because in *this* process the harness has already overridden the attributes.
3. **The suite publishes them for children**, from the same ``harness.FILE_TREES`` table that
   drives the rotation: the parent's attributes and the child's environment are two views of one
   list, and a tree added to the application fails a test until both exist.

The write is asserted too, not just the path: one probe stores a punch frame through
``punch_frames.store_frame`` and the checkout's own ``punch_frames`` is compared before and
after. A path is a promise; a file is the fact.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import harness
import pytest

import config

#: ``(environment variable, settings field, directory inside the project)`` for each tree, in the
#: order the application defines them. The third column is what a deployment that sets *nothing*
#: must still get, so it is written out here rather than read back from the settings - a test
#: that asked ``config`` what the default is would agree with any change to it.
TREES: Final = (
    ("LOCAL_REFS_DIR", "local_refs_dir", "local_references"),
    ("WORKER_PHOTOS_DIR", "worker_photos_dir", "worker_photos"),
    ("PUNCH_FRAMES_DIR", "punch_frames_dir", "punch_frames"),
    ("QUICK_LINK_PHOTOS_DIR", "quick_link_photos_dir", "quick_link_photos"),
    ("REGISTRATION_PHOTOS_DIR", "registration_photos_dir", "registration_photos"),
    ("CALIBRATION_CORPUS_DIR", "calibration_corpus_dir", "calibration_corpus"),
)

CONFIG = Path(config.__file__)

#: What a child sees. Asks the application for each directory through its *own* accessor, so the
#: answer covers the whole chain (config -> settings -> module attribute -> accessor) rather than
#: the settings object alone.
#:
#: These probes used to install a fake ``deepface`` module first, so that ``import main`` would not
#: load TensorFlow. They no longer need to: the embedding engine is ONNX Runtime and the model is
#: loaded lazily, so importing the application costs no framework and the probe measures what a
#: real child process does rather than a stub.
READ_PROBE = (
    "import json;"
    "import main, punch_frames, quick_links, registrations, biometrics, corpus;"
    "refs, photos = biometrics.directories();"
    "print(json.dumps({"
    " 'LOCAL_REFS_DIR': refs,"
    " 'WORKER_PHOTOS_DIR': photos,"
    " 'PUNCH_FRAMES_DIR': punch_frames.frames_dir(),"
    " 'QUICK_LINK_PHOTOS_DIR': quick_links.photos_dir(),"
    " 'REGISTRATION_PHOTOS_DIR': registrations.photos_dir(),"
    " 'CALIBRATION_CORPUS_DIR': corpus.root_dir()}))"
)

#: A child that writes: one punch frame through the application's own writer.
WRITE_PROBE = (
    "import json;"
    "from PIL import Image;"
    "import punch_frames;"
    "name = punch_frames.store_frame(Image.new('RGB', (64, 48), (10, 20, 30)));"
    "print(json.dumps({'stored': name, 'directory': punch_frames.frames_dir()}))"
)


def _child_env(**overrides: str) -> dict[str, str]:
    """The environment a probe runs in: this process's, minus anything that would lie to it.

    ``ENV_FILE`` points at a file that does not exist for the same reason
    ``harness.import_app_in_subprocess`` does it — a developer's ``.env`` must not be able to
    answer a question this suite is asking about the *environment*.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(harness.BACKEND_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["ENV_FILE"] = str(harness.TMP_ROOT / "absent-for-this-probe.env")
    env.update(overrides)
    return env


def _run(script: str, env: dict[str, str], cwd: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"the probe exited {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr[-1500:]}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _checkout_tree(directory: str) -> set[str]:
    target = config.PROJECT_ROOT / directory
    if not target.exists():
        return set()
    return {entry.name for entry in target.iterdir()}


def test_every_tree_here_is_a_variable_the_configuration_reads():
    """The list above and the code have to agree, in both directions.

    ``config`` reading a variable this suite does not name means a tree whose publication
    nothing checks; this suite naming one ``config`` ignores means a test asserting a behaviour
    that does not exist. Both are silent, so both are asserted.
    """
    read = set(re.findall(r'_env_path\(\s*"([A-Z0-9_]+)"', CONFIG.read_text(encoding="utf-8")))
    named = {env_var for env_var, _, _ in TREES}
    assert named <= read, (
        f"{sorted(named - read)} are named here and are not read by config.py, so setting them "
        f"would change nothing and a child would still write into the checkout"
    )
    assert named == {env_var for *_ , env_var in harness.FILE_TREES}, (
        "this suite's list and harness.FILE_TREES name different variables; the table that "
        "publishes them for children and the table that tests them have to be the same list"
    )


@pytest.mark.parametrize("env_var,field,directory", TREES)
def test_each_directory_is_read_from_the_environment(monkeypatch, tmp_path, env_var, field, directory):
    """Set the variable, and the configuration resolves to it — absolute, not joined to anything."""
    target = tmp_path / directory
    monkeypatch.setenv(env_var, str(target))

    built = config.build_settings()

    assert getattr(built, field) == target.resolve(), (
        f"{env_var} is set to {target} and config built {getattr(built, field)} instead"
    )


@pytest.mark.parametrize("env_var,field,directory", TREES)
def test_an_unset_environment_still_means_the_projects_own_directory(
    monkeypatch, tmp_path, env_var, field, directory
):
    """A deployment that sets nothing behaves exactly as it did before these existed.

    The ``ENV_FILE`` redirection matters here: without it a developer's ``.env`` would answer,
    and the test would be about that file rather than about the default.
    """
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "absent.env"))

    built = config.build_settings()

    assert getattr(built, field) == (config.PROJECT_ROOT / directory).resolve(), (
        f"with {env_var} unset the application resolved {getattr(built, field)} rather than the "
        f"project's own {directory} directory"
    )
    assert built.describe()[field] == str(getattr(built, field)), (
        f"`python -m config` does not report {field}, so the first thing an operator checks when "
        f"a file is not where they expected points nowhere"
    )


def test_a_child_process_reads_the_directories_from_the_environment(tmp_path):
    """The child-process claim, in a child process: every tree, through the app's own accessors.

    The corpus resolves through ``root_dir()``, its own accessor, like the others - so a child
    that inherits the environment and nothing else lands in the tree the parent configured. Its
    ``captures/`` subdirectory is what a *capture* is written into, and that is what the corpus
    suite's own tests assert against a monkeypatched root.
    """
    targets = {env_var: tmp_path / directory for env_var, _, directory in TREES}
    env = _child_env(**{env_var: str(target) for env_var, target in targets.items()})

    seen = _run(READ_PROBE, env, tmp_path)

    for env_var, target in targets.items():
        got = Path(seen[env_var])
        assert Path(os.path.realpath(got)) == target, (
            f"a child with {env_var}={target} resolved {got}: a subprocess importing the "
            f"application would write where nobody is looking"
        )
        assert config.PROJECT_ROOT not in Path(os.path.realpath(got)).parents, (
            f"{env_var} sent the child into the checkout anyway: {got}"
        )


def test_a_child_process_writes_its_frame_where_the_environment_points(tmp_path):
    """The file, not the path: a punch stored by a child, and a checkout that gained nothing."""
    frames = tmp_path / "frames"
    checkout_before = _checkout_tree("punch_frames")
    env = _child_env(PUNCH_FRAMES_DIR=str(frames))

    result = _run(WRITE_PROBE, env, tmp_path)

    stored = Path(os.path.realpath(result["directory"])) / result["stored"]
    assert stored.exists(), f"the child reported storing {stored} and it is not there"
    assert stored.parent == frames, (
        f"the child wrote the frame to {stored.parent} instead of the directory the environment "
        f"named ({frames})"
    )
    assert _checkout_tree("punch_frames") == checkout_before, (
        "a child process added a frame to the checkout's own punch_frames directory: the "
        "environment is what it reads, and this is the leak that made it necessary"
    )


def test_the_suite_publishes_every_tree_for_children():
    """The harness's half: the parent's attributes and the child's environment, from one table.

    Without this the application would be *capable* of honouring the variables and the suite
    would still leak, because a probe inherits whatever the parent happens to have exported.
    """
    for _module_name, _attribute, directory, env_var in harness.FILE_TREES:
        published = os.environ.get(env_var)
        assert published == str(harness.CURRENT_DIR / directory), (
            f"{env_var} is published as {published!r} rather than {harness.CURRENT_DIR / directory}: "
            f"a child spawned by a test run would not be kept out of the checkout"
        )
        assert config.PROJECT_ROOT not in Path(published).parents, (
            f"{env_var} points into the checkout ({published}), so a child would write there"
        )


def test_a_child_spawned_by_the_suite_writes_into_the_generation():
    """End to end, with the environment the suite actually exports.

    The two halves are each asserted above; this is the one that matters, and it is the state
    the suite will be in for every future test that starts a subprocess: no overrides, simply
    what ``harness`` published, and every one of them lands in the generation that holds the
    database - which is the property the rotation promised and could not deliver across a
    process boundary.
    """
    seen = _run(READ_PROBE, _child_env(), harness.TMP_ROOT)
    generation = harness.current_generation()
    # Expectations come from the harness table, not from ``TREES``: the first column here is the
    # directory inside the *checkout* (``local_references``) and the harness's is the subdirectory
    # of a generation (``refs``), and it is the harness's that a child of this run must land in.
    expected = {
        env_var: generation / directory
        for _module, _attribute, directory, env_var in harness.FILE_TREES
    }
    assert set(expected) == {env_var for env_var, _, _ in TREES}

    for env_var, target in expected.items():
        resolved = Path(os.path.realpath(seen[env_var]))
        assert resolved == target, (
            f"a child of this test run resolved {env_var} to {resolved} rather than the current "
            f"generation's tree ({target})"
        )
