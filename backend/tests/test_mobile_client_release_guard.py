"""The Android client's release guard: a debug API address must not reach a shipped APK.

WHY THIS TEST EXISTS
--------------------
``VITE_API_BASE_URL`` is inlined into the bundle at build time, and the values that make the
LAN and emulator workflows work - ``http://192.168.8.23:8000/api/v1``, ``http://10.0.2.2:8000``
- are exactly the values that cannot work in a shipped app: a release APK keeps Android's
secure defaults (the cleartext permission and the network-security config live under
``android/app/src/debug`` and are not merged into release), the WebView page is
``https://localhost``, and a private address resolves to nothing on a worker's phone.

The rule is enforced in two places, and both are exercised here:

  * ``src/build/api-base-policy.ts`` classifies a configured value, and ``vite.config.ts``
    throws on the release profile when it is http:// or a private-network
    host. The build half is tested with real ``vite build --mode release`` runs: the failure
    cases must exit non-zero *without writing a bundle*, and the success case must write a
    ``build-info.json`` recording the release profile.
  * the **fallback** is judged too. ``VITE_API_BASE_URL`` is what a deployment configures, and
    a checkout that configures nothing ships ``DEFAULT_API_BASE`` from ``src/core/config.ts``
    instead - so that literal is classified by the same policy, and the built bundle is read
    back to prove no emulator or LAN address survived into it.
  * ``android/app/build.gradle`` refuses to package a ``dist`` whose ``build-info.json`` is
    not a release build. Gradle itself is not run here (it needs the Android SDK and a
    toolchain this suite does not require); the wiring is asserted statically, and the file's
    absence is what the double-build workflow would ship.

Every build in this module writes to pytest's ``tmp_path``. ``mobile-client/dist`` is the tree
the debug APK is assembled from, and clobbering it here would break the emulator workflow the
guard is careful not to disturb.

Node, esbuild and ``node_modules`` are optional; without them the suite skips rather than
fails, exactly as ``test_mobile_client_signing.py`` does - a missing JavaScript toolchain says
nothing about whether the attendance system works.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from harness import PROJECT_ROOT

NODE = shutil.which("node")
MOBILE_DIR = PROJECT_ROOT / "mobile-client"
ESBUILD_JS = MOBILE_DIR / "node_modules" / "esbuild" / "bin" / "esbuild"
VITE_BIN = MOBILE_DIR / "node_modules" / "vite" / "bin" / "vite.js"
POLICY_TS = MOBILE_DIR / "src" / "build" / "api-base-policy.ts"
CONFIG_TS = MOBILE_DIR / "src" / "core" / "config.ts"
GRADLE_FILE = MOBILE_DIR / "android" / "app" / "build.gradle"

#: The address a build with no ``VITE_API_BASE_URL`` reaches, read out of the source rather than
#: repeated here: a literal copied into a test stops being a check the moment the two drift, and
#: for a checkout that carries no local ``.env.release`` this is the only base the app has.
DEFAULT_API_BASE_LITERAL = re.compile(r"""DEFAULT_API_BASE\s*=\s*['"]([^'"]+)['"]""")

#: What must never appear in a shipped bundle. The emulator's alias for the developer's machine
#: and the loopback address are the debug workflows' answer to "where is the server"; a private
#: range is the same failure spelled as a LAN address; and ``http://`` is the scheme a release
#: APK cannot use at all (the page is ``https://localhost``, and the cleartext switch lives under
#: ``android/app/src/debug``, which a release build does not merge).
FORBIDDEN_BUNDLE_LITERALS = ("10.0.2.2", "127.0.0.1", "192.168.", "http://")

pytestmark = pytest.mark.skipif(
    NODE is None
    or not ESBUILD_JS.exists()
    or not VITE_BIN.exists()
    or not POLICY_TS.exists()
    or not CONFIG_TS.exists(),
    reason="node + mobile-client/node_modules are required to run the client build",
)

#: The harness: judge a table of configured values, and format the refusal for one of them.
HARNESS_TS = """
import { assessApiBase, releaseGuardMessage } from './api-base-policy.ts';

const chunks: Buffer[] = [];
process.stdin.on('data', (c) => chunks.push(c as Buffer));
process.stdin.on('end', () => {
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const out: Record<string, unknown> = {
    assessments: payload.values.map((value: string | null) => assessApiBase(value)),
  };
  if (payload.message_for !== undefined) {
    out.message = releaseGuardMessage(assessApiBase(payload.message_for));
  }
  process.stdout.write(JSON.stringify(out));
});
"""


@pytest.fixture(scope="module")
def policy_harness(tmp_path_factory) -> Path:
    """Bundle the policy module once for the whole module."""
    workdir = tmp_path_factory.mktemp("mobile_release_guard")
    harness = workdir / "harness.ts"
    harness.write_text(HARNESS_TS, encoding="utf-8")
    (workdir / "api-base-policy.ts").write_text(POLICY_TS.read_text(encoding="utf-8"), encoding="utf-8")
    bundle = workdir / "harness.mjs"
    completed = subprocess.run(
        [NODE, str(ESBUILD_JS), str(harness), "--bundle", "--platform=node", "--format=esm",
         f"--outfile={bundle}", "--log-level=warning"],
        capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, f"esbuild failed:\n{completed.stderr}"
    return bundle


def assess(policy_harness: Path, values: list, message_for=None) -> dict:
    payload = {"values": values}
    if message_for is not None:
        payload["message_for"] = message_for
    completed = subprocess.run(
        [NODE, str(policy_harness)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, f"policy harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# the classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "kind", "problem_contains"),
    [
        # A public https deployment: the only shape a release build may bake in.
        pytest.param("https://attendance.example.com/api/v1", "secure", "", id="public-https"),
        pytest.param("HTTPS://ATTENDANCE.EXAMPLE.COM/api/v1", "secure", "", id="public-https-uppercase"),
        pytest.param("https://attendance.example.com:8443/api/v1", "secure", "", id="public-https-port"),
        pytest.param("attendance.example.com", "secure", "", id="missing-scheme-reads-as-https"),
        pytest.param("https://8.8.8.8/api/v1", "secure", "", id="public-ipv4"),
        # Nothing configured is not an error: the app can find its server at runtime.
        pytest.param("", "none", "", id="unset"),
        pytest.param("   ", "none", "", id="blank"),
        pytest.param(None, "none", "", id="null"),
        # Plaintext, whatever the host.
        pytest.param("http://attendance.example.com/api/v1", "insecure", "plaintext", id="http-public-host"),
        pytest.param("http://192.168.8.23:8000/api/v1", "insecure", "plaintext", id="lan-http"),
        pytest.param("ftp://files.example.com", "insecure", "ftp:", id="not-http-scheme"),
        pytest.param("https://exa mple.com/api/v1", "insecure", "not a usable URL", id="unparseable"),
        # The LAN and emulator addresses this project actually uses.
        pytest.param("https://192.168.8.23:8000/api/v1", "insecure", "192.168.0.0/16", id="lan-https"),
        pytest.param("192.168.8.23:8000/api/v1", "insecure", "192.168.0.0/16", id="lan-missing-scheme"),
        pytest.param("http://10.0.2.2:8000/api/v1", "insecure", "10.0.0.0/8", id="emulator-loopback"),
        pytest.param("https://10.255.255.254/api/v1", "insecure", "10.0.0.0/8", id="ten-boundary-high"),
        # 172.16.0.0/12 ends at 172.31.255.255 - the neighbours are public and must pass.
        pytest.param("https://172.16.0.1/api/v1", "insecure", "172.16.0.0/12", id="rfc1918-low"),
        pytest.param("https://172.31.255.254/api/v1", "insecure", "172.16.0.0/12", id="rfc1918-high"),
        pytest.param("https://172.15.255.255/api/v1", "secure", "", id="below-rfc1918"),
        pytest.param("https://172.32.0.0/api/v1", "secure", "", id="above-rfc1918"),
        pytest.param("https://169.254.10.10/api/v1", "insecure", "169.254.0.0/16", id="link-local"),
        pytest.param("https://127.0.0.1:8000/api/v1", "insecure", "127.0.0.0/8", id="loopback-literal"),
        pytest.param("https://0.0.0.0:8000/api/v1", "insecure", "0.0.0.0/8", id="unspecified"),
        # Carrier-grade NAT: where a Tailscale address lands, and just as unreachable off-net.
        pytest.param("https://100.64.0.7/api/v1", "insecure", "100.64.0.0/10", id="cgnat-low"),
        pytest.param("https://100.127.255.255/api/v1", "insecure", "100.64.0.0/10", id="cgnat-high"),
        pytest.param("https://100.128.0.0/api/v1", "secure", "", id="above-cgnat"),
        # Names that only resolve on a local network, and IPv6.
        pytest.param("https://localhost:8443/api/v1", "insecure", "localhost", id="localhost"),
        pytest.param("https://api.localhost/api/v1", "insecure", "localhost", id="localhost-subdomain"),
        pytest.param("https://build-server.local/api/v1", "insecure", ".local", id="mdns-local"),
        pytest.param("https://attendance.internal/api/v1", "insecure", ".internal", id="internal-tld"),
        pytest.param("https://[::1]:8000/api/v1", "insecure", "::1", id="ipv6-loopback"),
        pytest.param("https://[fd00::1]/api/v1", "insecure", "fc00::/7", id="ipv6-ula"),
        pytest.param("https://[fe80::1]/api/v1", "insecure", "fe80::/10", id="ipv6-link-local"),
        pytest.param("https://[::ffff:192.168.1.5]/api/v1", "insecure", "192.168.0.0/16", id="ipv4-mapped"),
    ],
)
def test_the_release_rule_classifies_a_configured_base(policy_harness, value, kind, problem_contains):
    answer = assess(policy_harness, [value])["assessments"][0]
    assert answer["kind"] == kind, answer
    if problem_contains:
        assert problem_contains in answer["problem"], answer
    else:
        assert answer["problem"] == "", answer


def test_the_refusal_names_the_value_and_the_way_out(policy_harness):
    """The message is the deliverable when a build fails: it has to say what to change."""
    answer = assess(policy_harness, [], message_for="http://192.168.8.23:8000/api/v1")
    message = answer["message"]
    assert "RELEASE" in message
    assert "http://192.168.8.23:8000/api/v1" in message
    assert "plaintext" in message
    assert "npm run build:release" in message
    assert "npm run build" in message


# ---------------------------------------------------------------------------
# the build itself
# ---------------------------------------------------------------------------
def run_vite(mode: str, base_url: str | None, out_dir: Path) -> subprocess.CompletedProcess:
    """One real build into ``out_dir``, so mobile-client/dist is never touched."""
    env = dict(os.environ)
    if base_url is None:
        env.pop("VITE_API_BASE_URL", None)
    else:
        env["VITE_API_BASE_URL"] = base_url
    return subprocess.run(
        [NODE, str(VITE_BIN), "build", "--mode", mode, "--outDir", str(out_dir), "--emptyOutDir"],
        cwd=str(MOBILE_DIR), env=env, capture_output=True, text=True, timeout=300,
    )


def read_build_info(out_dir: Path) -> dict:
    return json.loads((out_dir / "build-info.json").read_text(encoding="utf-8"))


def read_default_api_base() -> str:
    """``DEFAULT_API_BASE`` as it is written in ``src/core/config.ts``."""
    match = DEFAULT_API_BASE_LITERAL.search(CONFIG_TS.read_text(encoding="utf-8"))
    assert match, (
        "DEFAULT_API_BASE is no longer a plain string literal in src/core/config.ts; the "
        "fallback half of the release guard needs it to stay readable here"
    )
    return match.group(1)


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("http://192.168.8.23:8000/api/v1", id="lan-http"),
        pytest.param("https://192.168.8.23:8000/api/v1", id="lan-https"),
        pytest.param("http://10.0.2.2:8000/api/v1", id="emulator"),
    ],
)
def test_a_release_build_refuses_a_debug_address(tmp_path, base_url):
    """Non-zero exit, the offending value in the refusal, and no bundle on disk."""
    out_dir = tmp_path / "dist"
    completed = run_vite("release", base_url, out_dir)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "Refusing to build the RELEASE profile" in output, output
    assert base_url in output, output
    assert not (out_dir / "index.html").exists(), "a refused build must not leave a bundle"


def test_a_release_build_with_a_public_https_base_marks_the_dist(tmp_path):
    out_dir = tmp_path / "dist"
    completed = run_vite("release", "https://attendance.example.com/api/v1", out_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    info = read_build_info(out_dir)
    assert info["profile"] == "release"
    assert info["insecure_api_base"] is False
    assert info["api_base"] == "https://attendance.example.com/api/v1"
    assert info["api_base_host"] == "attendance.example.com"


def test_a_release_build_without_a_base_url_is_allowed_and_marked(tmp_path):
    """A release with no ``VITE_API_BASE_URL`` is legitimate: the app has its own fallback.

    No ``.env.release`` is committed, deliberately. The file would not be a secret - it holds one
    public https address - but its *basename* is the credential shape the build already refuses
    (``test_no_committed_credentials.py``), so the address a shipped build actually uses is the
    one compiled into ``src/core/config.ts``. That is why ``api_base`` may be empty here: the
    recorded value is the *configured* one, and with nothing configured there is nothing to
    record - the fallback is in the bundle either way, and
    ``test_the_in_bundle_fallback_is_a_shippable_address`` is the check for that half.
    """
    out_dir = tmp_path / "dist"
    completed = run_vite("release", None, out_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    info = read_build_info(out_dir)
    assert info["profile"] == "release"
    # The one property the guard promises about this build: it never records an http:// or
    # private-network base. Whether it records a base at all depends on whether this checkout
    # keeps a local ``.env.release``.
    assert info["insecure_api_base"] is False
    assert info["api_base"] == "" or info["api_base"].startswith("https://")
    if info["api_base"]:
        assert info["api_base_host"] != ""
        assert info["api_base"].endswith("/api/v1")


def test_the_in_bundle_fallback_is_a_shippable_address(policy_harness):
    """The base a *fresh checkout* ships with, judged by the release rule.

    ``VITE_API_BASE_URL`` is only half the story. With nothing configured - the state of every
    clone that carries no local ``.env.release`` - the app talks to ``DEFAULT_API_BASE``, and
    until this test nothing judged that value at all: the release guard read the env var, the
    bundle inlined the env var, and the literal that actually answers the request rode along
    unexamined. A LAN address there is the exact failure this module exists to prevent, reached
    by the one path the guard was not looking at.
    """
    fallback = read_default_api_base()
    answer = assess(policy_harness, [fallback])["assessments"][0]
    assert answer["kind"] == "secure", (
        f"DEFAULT_API_BASE is not a shippable address: {fallback!r} -> {answer['problem']}. "
        "With no VITE_API_BASE_URL baked in, this is the URL every installed app uses."
    )
    assert answer["raw"].endswith("/api/v1"), answer


def test_the_shipped_bundle_carries_no_debug_address(tmp_path):
    """End to end, on a build with nothing configured: what the release APK would actually run.

    The assets are read rather than the settings, because the question is not "was the guard
    happy" but "is there a LAN or plaintext address in the JavaScript a worker's phone will
    execute". ``DEFAULT_DEV_BASE`` - ``http://10.0.2.2:8000/api/v1`` - is written into the same
    module and is dropped only because ``import.meta.env.DEV`` is false in a build, which is a
    property of how Vite folds that branch rather than of any check here.
    """
    out_dir = tmp_path / "dist"
    completed = run_vite("release", None, out_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    scripts = sorted(out_dir.glob("assets/*.js"))
    assert scripts, "the release build produced no script to inspect"
    bundle = "\n".join(path.read_text(encoding="utf-8") for path in scripts)

    fallback_host = urlsplit(read_default_api_base()).hostname or ""
    assert fallback_host and fallback_host in bundle, (
        f"{fallback_host!r} is not in the shipped bundle, so an app with nothing configured "
        "would have nowhere to send a punch"
    )
    for literal in FORBIDDEN_BUNDLE_LITERALS:
        assert literal not in bundle, (
            f"{literal!r} is in the release bundle; an installed app can reach nothing through "
            "it"
        )


def test_the_debug_profile_keeps_the_lan_address(tmp_path):
    """The workflow the guard must not break - and the profile marker Gradle reads."""
    out_dir = tmp_path / "dist"
    completed = run_vite("production", "http://192.168.8.23:8000/api/v1", out_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    info = read_build_info(out_dir)
    assert info["profile"] == "debug"
    assert info["insecure_api_base"] is True
    assert info["api_base"] == "http://192.168.8.23:8000/api/v1"


# ---------------------------------------------------------------------------
# the other half: the release APK may not package a debug dist
# ---------------------------------------------------------------------------
def test_the_release_apk_verifies_the_web_build_profile():
    """Static wiring check: Gradle itself needs the Android SDK, the rule does not."""
    source = GRADLE_FILE.read_text(encoding="utf-8")
    assert "verifyReleaseWebBuild" in source
    assert "src/main/assets/public/build-info.json" in source
    assert "preReleaseBuild" in source
    # Both halves of the verdict, so a release APK can never be assembled from a dist that
    # was not produced by `npm run build:release`.
    assert "profile != 'release'" in source
    assert "insecure_api_base" in source
