"""The files a host builds from: the runtime manifest, the start command, and the port.

WHY THIS EXISTS
---------------
The repository had no runtime requirements file, and its own note in
``backend/requirements-dev.txt`` said so and called it a deployment risk. It was one: a host
that builds from this repository (Railway, Render, Fly) has nothing to install, so the
application either never starts or starts with whatever the builder guessed - and the symptom
is a 502 from the edge with no error anywhere the application can show you. That is what
``al-jehad-production.up.railway.app`` answered before these files existed.

Three things have to stay true together, and each one is silent when it breaks:

* **the manifest pins the runtime and nothing more.** A missing pin is an application that
  installs something other than what was tested; a test-only package in it is build time and
  disk on a host we pay for. Both directions are asserted, and so is the shape (``name==version``
  - a floating requirement is not a manifest);
* **the healthcheck path is a real route that answers 200 without a session.** Railway asks that
  path before it hands over traffic: a wrong one marks a perfectly healthy deploy unhealthy, and
  an authenticated one does the same, since the platform is not a user;
* **the start command binds the port the host gives it.** ``PORT`` is how Railway, Render, Fly
  and Heroku say which port to listen on, and ignoring it produces the same 502 with the
  application running and healthy at a port nobody forwards to.

The last check is the one that keeps ``.python-version`` honest: the manifest is a freeze of a
*particular* interpreter, and the pair has to move together.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys

import pytest

from harness import PROJECT_ROOT

REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
RAILWAY = PROJECT_ROOT / "railway.json"
PYTHON_VERSION = PROJECT_ROOT / ".python-version"
SERVE = PROJECT_ROOT / "backend" / "serve.py"

#: Imported by the application or by ``serve.py`` at runtime. Each one is a feature that
#: disappears - or a start that fails - if the host does not install it.
RUNTIME_PINS = (
    "fastapi",
    "uvicorn",
    "onnxruntime",
    "numpy",
    "scipy",
    "pydantic",
    "starlette",
    "slowapi",
    "PyJWT",
    "passlib",
    "python-multipart",
    "python-dotenv",
    "pillow",
    "requests",
)

#: Pinned by ``backend/requirements-dev.txt`` for the suite and the operator scripts. The venv
#: they were frozen from is the *test* environment, so they all appear in a naive ``pip freeze``
#: - and none of them belongs in what a host installs.
TEST_ONLY = ("pytest", "pluggy", "iniconfig", "httpx2", "httpcore2", "pyee", "playwright")

PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s=<>!~]+)$")


def _manifest() -> dict[str, str]:
    """``{name.lower(): version}`` for the runtime manifest, comments dropped."""
    pins: dict[str, str] = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        match = PIN.match(entry)
        assert match, f"{entry!r} is not a pinned requirement (name==version)"
        pins[match.group(1).lower()] = match.group(2)
    return pins


@pytest.fixture(scope="module")
def serve_module():
    """``backend/serve.py`` loaded by path, without running ``main()``."""
    spec = importlib.util.spec_from_file_location("serve_under_test", SERVE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_runtime_manifest_exists_at_the_root_and_is_fully_pinned():
    """The file a builder looks for, in the place it looks."""
    assert REQUIREMENTS.exists(), "a host building from this repo installs nothing without it"
    pins = _manifest()
    assert len(pins) > 40, f"the manifest looks truncated: {len(pins)} pins"


@pytest.mark.parametrize("name", RUNTIME_PINS)
def test_every_runtime_import_is_pinned(name):
    assert name.lower() in _manifest(), (
        f"{name} is imported at runtime and is not in requirements.txt: a host that builds "
        "from this repository would start without it"
    )


@pytest.mark.parametrize("name", TEST_ONLY)
def test_no_test_only_package_is_installed_on_a_host(name):
    """They are pinned in ``backend/requirements-dev.txt``, which is where they belong."""
    assert name.lower() not in _manifest(), (
        f"{name} is a test/tooling dependency and is installed on every deploy"
    )


def test_the_optional_extras_stay_optional():
    """qrcode and openpyxl degrade gracefully, so they are not hard requirements.

    ``onnxruntime`` used to be in this list and is deliberately not any more: it is the
    embedding engine (``face_onnx``), and a host without it cannot verify a single punch - which
    is a start that must fail loudly at build time, not a feature that quietly reports itself
    unavailable. Its pin is asserted by ``test_every_runtime_import_is_pinned`` instead.
    """
    pins = _manifest()
    for name in ("qrcode", "openpyxl"):
        assert name not in pins, (
            f"{name} is an optional extra (see backend/requirements-optional.txt): pinning it "
            "turns a feature that degrades into a start that fails"
        )


def test_the_python_version_matches_the_interpreter_the_manifest_was_frozen_from():
    """A freeze is known-good on one interpreter; the pair has to move together."""
    pinned = PYTHON_VERSION.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+", pinned), pinned
    live = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert pinned == live, (
        f"the manifest was frozen on Python {pinned} and this interpreter is {live}: "
        "regenerate requirements.txt and update .python-version together"
    )


def _start_command() -> str:
    """The command line the container actually runs.

    Railway appends ``deploy.startCommand`` to the image's ENTRYPOINT as arguments, so a
    startCommand that repeats the entrypoint's own command is a server that receives its
    whole command line again as positional arguments - which ``serve.py`` refuses, and a
    crash-looping container is a 502 from the edge. The command lives in the entrypoint
    alone; a startCommand is only allowed when it does NOT repeat it.
    """
    config = json.loads(RAILWAY.read_text(encoding="utf-8"))
    entrypoint = (PROJECT_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "backend/serve.py" in entrypoint, "the entrypoint carries the server command"
    start = config["deploy"].get("startCommand", "")
    if start:
        assert "backend/serve.py" not in start, (
            f"startCommand {start!r} repeats the entrypoint's command; Railway appends it to "
            "the ENTRYPOINT as arguments, so serve.py exits on the unrecognized arguments "
            "and the deploy 502s (this exact bug shipped once)"
        )
        return start
    return "python backend/serve.py --tunnel"


def test_the_railway_config_has_the_pieces_a_deploy_needs():
    """The build is stated, not guessed: DOCKERFILE with the Dockerfile named.

    Nixpacks infers a build from the repository's shape, which is how the two facts it
    cannot see from file names were lost: ``import cv2`` needs libGL at runtime (a green
    build that crash-loops on start), and the 87 MB ``facenet128.onnx`` the startup gate
    refuses to serve without has to survive the build context. The Dockerfile says both
    out loud, so the builder is pinned to it.
    """
    config = json.loads(RAILWAY.read_text(encoding="utf-8"))
    assert config["build"]["builder"] == "DOCKERFILE", config["build"]
    dockerfile = PROJECT_ROOT / config["build"].get("dockerfilePath", "Dockerfile")
    assert dockerfile.exists(), "railway.json names a Dockerfile the repository does not have"
    assert "backend/serve.py" in _start_command()
    assert "--tunnel" in _start_command(), (
        "the host terminates TLS: serving a self-signed certificate behind it is what makes "
        "GPS and the camera fail on a phone"
    )
    assert config["deploy"]["healthcheckTimeout"] >= 120, (
        "the first start runs migrations and imports TensorFlow; a short timeout marks a "
        "healthy deploy unhealthy"
    )


def test_the_dockerfile_carries_what_the_manifest_cannot_name():
    """The three facts a Docker build needs that requirements.txt cannot express.

    * opencv-python links against libGL at import time, so the image needs the system
      packages - a slim image without them builds green and crash-loops at start with
      ``libGL.so.1: cannot open shared object file``;
    * the embedding model must be in the build context: the startup gate refuses to
      serve without it, so a build that silently drops it is an app that never boots;
    * the state directories are owned by the image so a first boot without a volume
      works, and everything writable lives under one mountable root (/data).
    """
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "libgl1" in dockerfile, "cv2 imports libGL: without this the app crash-loops at start"
    assert "COPY backend/ backend/" in dockerfile, "the models ride in backend/models/"
    assert "DATABASE_PATH=/data" in dockerfile, "state has to land under one mountable root"

    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "backend/tests/" in dockerignore, "the test stack does not belong on a host we pay for"
    assert "backend/models/" not in dockerignore.split("!")[-1], (
        "a .dockerignore that excludes backend/models/ ships an app that cannot boot"
    )


def test_the_image_installs_the_optional_extras_this_deployment_advertises():
    """A deployment that cannot send is one whose push advisory never clears.

    Web Push needs two things that live in different places: a VAPID key pair, which is an
    environment setting an operator supplies in a minute, and ``pywebpush``, which is a
    package and therefore a *build* decision. The optional manifest holds the second one -
    it is optional precisely because the application boots without it (``push`` imports it
    inside a function and reports the absence as its own reason) - so an image that does not
    install that file answers "the optional pywebpush package is not installed" however
    correct ``VAPID_PUBLIC_KEY`` and ``VAPID_PRIVATE_KEY`` are, and the readiness gate raises
    ``worker_push_delivery`` on a channel somebody has just finished configuring.

    Both halves are asserted, because the failure is silent in both directions: an install
    line pointing at a file the build context does not carry fails the *build*, and an
    ignore line puts the file back on the wrong side of it while the install line still
    reads correctly.
    """
    optional = PROJECT_ROOT / "backend" / "requirements-optional.txt"
    assert "pywebpush" in optional.read_text(encoding="utf-8"), (
        "the optional manifest is where the push extra is named; transport_available() quotes "
        "pywebpush as the missing piece"
    )

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    installs = [line for line in dockerfile.splitlines() if "pip install" in line]
    assert any("requirements-optional.txt" in line for line in installs), (
        "the image must install the extras it advertises, or VAPID keys alone cannot make a "
        "worker's phone ring"
    )
    assert any("COPY" in line and "requirements-optional.txt" in line for line in dockerfile.splitlines()), (
        "pip install can only read a file the build context carried in"
    )

    ignored = [
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip().split(" ")[0] == "backend/requirements-optional.txt"
    ]
    assert not ignored, (
        f"{ignored} excludes the optional manifest from the build context while the Dockerfile "
        "installs it: the image would ship without the extras and the push advisory would stay up"
    )


def test_the_healthcheck_path_is_a_route_that_answers_without_a_session(client):
    """Railway is not a user: a path that needs a token marks every deploy unhealthy."""
    path = json.loads(RAILWAY.read_text(encoding="utf-8"))["deploy"]["healthcheckPath"]
    response = client.get(path)
    assert response.status_code == 200, f"{path} answered {response.status_code}"
    assert response.json().get("status"), response.text[:200]


# ---------------------------------------------------------------------------
# the port the host gives us
# ---------------------------------------------------------------------------
def test_the_exposed_port_and_the_pinned_listen_port_cannot_drift():
    """The edge routes to the EXPOSEd port; the server must be listening on that port.

    Railway injects a generated PORT (8080 on one deploy) while routing the public domain
    to the port the Dockerfile EXPOSEs (8000). An app that listens on whichever PORT the
    platform happens to inject is an app the edge can 502 while its own self-test reports
    every check green - which is precisely what happened. The entrypoint pins the listen
    port (APP_PORT, default 8000) to the EXPOSEd one, so the two numbers have to move
    together, and this assertion is what notices when they stop.
    """
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    exposed = re.search(r"^EXPOSE\s+(\d+)", dockerfile, re.MULTILINE)
    assert exposed, "the Dockerfile must EXPOSE the port the edge routes to"

    entrypoint = (PROJECT_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    pinned = re.search(r"APP_PORT=\"?\$\{APP_PORT:-(\d+)\}", entrypoint)
    assert pinned, "the entrypoint must pin the listen port (APP_PORT), not trust an injected PORT"
    assert exposed.group(1) == pinned.group(1), (
        f"the image EXPOSEs {exposed.group(1)} but the entrypoint listens on {pinned.group(1)}: "
        "the edge would route to a port with nothing behind it - a 502 with a healthy app"
    )


def test_the_port_flag_defaults_to_the_host_s_port_and_stays_8000_locally(serve_module, monkeypatch):
    """One command on the laptop and on the host, with the port written down once."""
    monkeypatch.delenv("PORT", raising=False)
    assert serve_module.default_port() == 8000

    monkeypatch.setenv("PORT", "7431")
    assert serve_module.default_port() == 7431


@pytest.mark.parametrize("value", ["", "   ", "abc", "0", "99999", "-5", "8080.5"])
def test_a_port_that_is_not_a_port_falls_back_instead_of_crashing(serve_module, monkeypatch, value):
    """A typo in a dashboard variable must not look like an application that crashes on start."""
    monkeypatch.setenv("PORT", value)
    assert serve_module.default_port() == 8000


def test_the_startup_banner_names_the_database_the_app_actually_opens(serve_module, monkeypatch, tmp_path):
    """``DATABASE_PATH`` is the volume, and the banner is how an operator knows where the data is.

    It used to print the checkout's ``times.db`` no matter what, so a correctly configured
    deploy on a mounted volume still reported a path inside the container - the one line that
    would be read while deciding whether the next deploy is about to take the punches with it.
    """
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert serve_module.resolved_database() == PROJECT_ROOT / "times.db"

    volume = tmp_path / "data" / "times.db"
    monkeypatch.setenv("DATABASE_PATH", str(volume))
    assert serve_module.resolved_database() == volume

    monkeypatch.setenv("DATABASE_PATH", "vol/times.db")  # relative, like a sloppy dashboard entry
    assert serve_module.resolved_database() == PROJECT_ROOT / "vol" / "times.db"


def test_a_volume_that_is_not_mounted_is_named_in_the_startup_message(serve_module, tmp_path):
    """SQLite will not create the directory, and its own error names neither setting nor cause.

    The directory is deliberately not created for the operator: a missing volume that is
    papered over looks exactly like a mounted one until the redeploy that takes every punch
    with it. What the server owes them instead is one line saying which path is missing.
    """
    mounted = tmp_path / "data"
    mounted.mkdir()
    assert serve_module.volume_warning(mounted / "times.db") is None

    message = serve_module.volume_warning(tmp_path / "not-mounted" / "times.db")
    assert message and "not-mounted" in message
    assert "DATABASE_PATH" in message, "the line has to name the setting that is wrong"


def test_the_start_command_is_accepted_and_means_plain_http(serve_module, monkeypatch):
    """Run the real parser over the configured command line, not a paraphrase of it.

    A bad flag here is a deploy that fails at start, and ``--tunnel`` is the flag that matters:
    it must turn the bundled certificate *off*, because a proxy has already done the handshake.
    The command line comes from the entrypoint (see ``_start_command`` for why it is not
    duplicated in railway.json's startCommand).
    """
    argv = _start_command().split()
    assert argv[0].startswith("python"), argv

    monkeypatch.setenv("PORT", "7431")
    args = serve_module.parse_args(argv[2:])  # the flags, i.e. ["--tunnel"]
    assert args.tunnel is True
    assert args.http is True, "the host terminates TLS; serving our certificate behind it breaks GPS"
    assert args.port == 7431, "the host's PORT is what the edge forwards to"
    assert args.reload is False, "reloading in production restarts the app under load"
