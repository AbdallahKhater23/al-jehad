"""Run the Site Attendance app over HTTPS.

Why this exists
---------------
Chrome, Safari and Firefox only expose ``navigator.geolocation`` and
``navigator.mediaDevices.getUserMedia`` (the camera) on **secure contexts**.
A secure context is either ``https://`` or ``http://localhost``.  When the app
is opened from a phone at ``http://192.168.x.x:8000`` the browser refuses to
even show the permission prompt and reports ``error.code === 1``
(PERMISSION_DENIED) immediately, which the old UI surfaced as
"Permission denied (Code 1). Please check site settings in the address bar."

That message was a red herring: there is nothing to fix in site settings.
Serving over HTTPS fixes GPS *and* the camera on every device at once.

Usage
-----
    python serve.py                 # HTTPS on 0.0.0.0:8000 (use this one)
    python serve.py --http          # plain HTTP, localhost only (no GPS/camera off-device)
    python serve.py --gen-cert-only  # (re)create the certificate and exit
    python serve.py --port 8443
    python serve.py --tunnel        # plain HTTP for a tunnel/proxy that provides the HTTPS

The certificate is self-signed and stored in ``backend/certs/`` (git-ignored).
Each device has to accept the "not private" warning once; after tapping
"Advanced -> Proceed" the page counts as a secure context and GPS/camera work.

On a host that publishes the app for you (Railway, Render, Fly, Heroku - the ones
that inject ``PORT``) there is no certificate to make and no port to guess:

    python serve.py --tunnel

``--tunnel`` because the host terminates TLS, exactly like a tunnel does, and the
port comes from ``PORT`` when it is set - so one command works on the laptop and on
the host, with no port written down in two places.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
# main.py stores its SQLite file relative to the CURRENT DIRECTORY, so the app must
# be started from the same place as the server it replaces - otherwise you silently
# get a different, empty database. Run from the project root and import `backend.main`.
APP_IMPORT = "backend.main:app"
DEFAULT_DATABASE_FILE = PROJECT_ROOT / "times.db"
CERT_DIR = BACKEND_DIR / "certs"
CERT_FILE = CERT_DIR / "dev-cert.pem"
KEY_FILE = CERT_DIR / "dev-key.pem"
SAN_FILE = CERT_DIR / "san.txt"
OPENSSL_CONFIG = CERT_DIR / "openssl.cnf"

# Git for Windows ships OpenSSL outside the normal Windows PATH.
OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
    r"C:\Program Files (x86)\OpenSSL-Win32\bin\openssl.exe",
    "/usr/bin/openssl",
    "/usr/local/bin/openssl",
    "/opt/homebrew/bin/openssl",
)


def resolved_database() -> Path:
    """The SQLite file the application will actually open, for the startup banner.

    ``DATABASE_PATH`` is how a host points the app at a mounted volume, and ``config.py``
    resolves it (``~`` expanded, a relative path taken from the working directory, which this
    script has already set to the project root) before anything else reads the database. The
    banner used to name the checkout's ``times.db`` unconditionally, so a deploy that was
    correctly writing to ``/data/times.db`` still printed a path next to the code - and that
    one line is how an operator answers "where is the data", usually while deciding whether a
    redeploy is about to delete it.
    """
    raw = (os.environ.get("DATABASE_PATH") or "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_DATABASE_FILE
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def volume_warning(database: Path) -> str | None:
    """What to say when the directory ``DATABASE_PATH`` points into does not exist.

    SQLite will not create the directory, so the application dies in startup with
    ``unable to open database file`` - a message that names neither the setting nor the most
    likely cause. On a host the cause is almost always the same: the volume is not mounted at
    the path the variable names, so the file would land on the container's own filesystem and
    disappear with the next deploy.

    The directory is deliberately **not** created here. A missing volume that is papered over
    this way is indistinguishable from a mounted one until the redeploy that takes every punch
    with it, and losing a payroll record silently is worse than a server that refuses to start.
    """
    folder = database.parent
    if folder.exists():
        return None
    return (
        f"the directory {folder} does not exist, so the database cannot be created there.\n"
        "     On a host this usually means the volume is not mounted at that path: "
        "DATABASE_PATH must point inside the mount (e.g. /data/times.db)."
    )


def trusted_proxies_for_uvicorn() -> str:
    """The ``TRUSTED_PROXIES`` setting in the form uvicorn's ``--forwarded-allow-ips`` wants.

    Read from the same setting the application's own network gate uses (``netguard``), so
    there is one list to get right rather than two that can disagree - a disagreement here is
    invisible and expensive: uvicorn would not rewrite ``scope["client"]`` (so the audit log
    and the per-IP rate limiter would see the proxy for every worker), while the gate would
    refuse the administrator whose address it could no longer determine.

    Falls back to loopback and says so, because this runs before the app's own configuration
    validation: the server has to be able to start far enough to report a broken config.
    """
    try:
        from config import build_settings

        return ",".join(build_settings().trusted_proxies) or "127.0.0.1"
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop the boot message
        print(f"  ! could not read TRUSTED_PROXIES ({exc}); trusting loopback only")
        return "127.0.0.1"


def find_openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in OPENSSL_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def local_ipv4_addresses() -> list[str]:
    """Every IPv4 address a phone on the same network could reach us on."""
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass

    # Asking the OS which interface routes outbound gives the primary LAN IP.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            ips.add(probe.getsockname()[0])
    except OSError:
        pass

    return sorted(ip for ip in ips if not ip.startswith("169.254."))


def _port_already_in_use(port: int) -> bool:
    """True when another process already owns the port we want to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _san_config(ips: list[str]) -> str:
    alternatives = ["DNS.1 = localhost"]
    for index, ip in enumerate(ips, start=1):
        alternatives.append(f"IP.{index} = {ip}")
    return f"""[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = Site Attendance (Local Dev)
O = Site Attendance

[v3_req]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
{chr(10).join(alternatives)}
"""


def ensure_certificate(ips: list[str], force: bool = False) -> tuple[Path, Path]:
    """Create (or reuse) a self-signed certificate valid for the given IPs."""
    san = ",".join(ips)
    if not force and CERT_FILE.exists() and KEY_FILE.exists():
        if SAN_FILE.exists() and SAN_FILE.read_text().strip() == san:
            return CERT_FILE, KEY_FILE

    openssl = find_openssl()
    if not openssl:
        raise SystemExit(
            "Could not find the 'openssl' command.\n"
            "Install Git for Windows (which bundles it) or OpenSSL, then retry."
        )

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    OPENSSL_CONFIG.write_text(_san_config(ips), encoding="utf-8")

    env = dict(os.environ)
    # Stop Git Bash / MSYS from rewriting the openssl config paths.
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"

    print(f"[serve] Generating a self-signed certificate for: {', '.join(ips)}")
    result = subprocess.run(
        [
            openssl, "req", "-x509", "-nodes", "-newkey", "rsa:2048",
            "-days", "825",
            "-keyout", str(KEY_FILE),
            "-out", str(CERT_FILE),
            "-config", str(OPENSSL_CONFIG),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(f"openssl failed:\n{result.stderr.strip()}")

    SAN_FILE.write_text(san, encoding="utf-8")
    print(f"[serve] Certificate written to {CERT_FILE}")
    return CERT_FILE, KEY_FILE


def default_port() -> int:
    """The port to bind: ``PORT`` when the host injects one, else 8000.

    Railway, Render, Fly and Heroku publish whatever the process listens on, and they say
    which port that is in ``PORT``. Ignoring it is the classic "502 Application failed to
    respond": the app is running and healthy on 8000, at a port the edge never forwards to.

    A value that is not a port number is reported and ignored rather than fatal, for the same
    reason the punch path never raises over configuration: a typo in a dashboard's variable
    should not look like an application that crashes on start.
    """
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return 8000
    try:
        port = int(raw)
    except ValueError:
        print(f"[serve] PORT={raw!r} is not a port number; using 8000", file=sys.stderr)
        return 8000
    if not 1 <= port <= 65535:
        print(f"[serve] PORT={port} is out of range; using 8000", file=sys.stderr)
        return 8000
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line, and apply the one rule that couples two of the flags.

    Split out from :func:`main` so the flags a host is configured with can be checked without
    starting a server: ``railway.json`` names this command line, and a typo in it used to be
    discoverable only by watching a deploy fail.

    ``--tunnel`` is a promise that something in front of us terminates TLS, so it implies plain
    HTTP on this side. Serving the self-signed certificate to a proxy that already did the
    handshake is what makes GPS and the camera fail on a phone while the laptop looks fine.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument(
        "--port", type=int, default=default_port(),
        help="port (default: $PORT on a host that injects one, else 8000)",
    )
    parser.add_argument("--http", action="store_true", help="serve plain HTTP instead of HTTPS")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--gen-cert-only", action="store_true", help="create the certificate then exit")
    parser.add_argument(
        "--tunnel", action="store_true",
        help="plain HTTP for a tunnel that provides the HTTPS (ngrok, cloudflared, ...)"
    )
    args = parser.parse_args(argv)
    if args.tunnel:
        # The tunnel terminates TLS and forwards plain HTTP to us, so the browser
        # still gets a real trusted certificate. That is enough for GPS + camera,
        # and it avoids the self-signed warning entirely.
        args.http = True
    return args


def main() -> None:
    args = parse_args()

    if _port_already_in_use(args.port):
        print(
            f"[serve] WARNING: something is already listening on port {args.port}.\n"
            f"        It may be a wedged server holding the port (that shows up in the\n"
            f"        browser as a blank white page). Find and stop it first:\n"
            f"          netstat -ano | findstr :{args.port}" + "\n"
            f"          taskkill /F /PID <pid>\n"
            f"        Then run this script again.",
            file=sys.stderr,
        )

    ips = local_ipv4_addresses()
    if args.host not in ("0.0.0.0", "::"):
        ips = [args.host] if args.host not in ips else ips

    if args.gen_cert_only:
        ensure_certificate(ips)
        return

    # Import the app exactly like `uvicorn backend.main:app` from the project root,
    # so the existing times.db (and enrolled face references) are used as-is.
    #
    # BOTH directories go on sys.path. The project root is where the data lives, but
    # ``main.py`` and its siblings (``database``, ``enrollment``, ``security``, ...)
    # are importable as *top-level* modules, which only works if ``backend/`` is
    # searchable too. Inserting the root alone produced
    # "ModuleNotFoundError: No module named 'enrollment'" when the server was started
    # from anywhere other than ``backend/``.
    #
    # ``backend/`` goes in *last*, which puts it first - the order here used to be the
    # other way round, and since these are inserted at position 0 the root ended up ahead
    # of ``backend/``. Nothing collides except the one name that does: a checkout with a
    # ``main.py`` at the root as well (the pre-``backend/`` prototype that the rollback
    # branch keeps there) then answers ``import main`` with that file, and
    # ``biometrics.directories()`` fails at startup with "module 'main' has no attribute
    # LOCAL_REFS_DIR" - every module the app imports resolves to the *other* main. The
    # intended module is the one that also has ``database``, ``enrollment`` and
    # ``security`` beside it, so the backend directory wins.
    os.chdir(PROJECT_ROOT)
    # Removed and re-inserted rather than inserted-if-absent: running this script already put
    # ``backend/`` in ``sys.path`` (Python prepends the script's own directory), and that entry
    # is not always textually equal to ``BACKEND_DIR`` the way ``Path.resolve()`` writes it - so
    # the old check could skip the one directory that had to move, leaving the root in front.
    for candidate in (PROJECT_ROOT, BACKEND_DIR):
        while str(candidate) in sys.path:
            sys.path.remove(str(candidate))
        sys.path.insert(0, str(candidate))

    scheme = "http" if args.http else "https"
    ssl_kwargs = {}
    if not args.http:
        cert, key = ensure_certificate(ips)
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    print("\n" + "=" * 68)
    print("  Site Attendance server is starting")
    print("=" * 68)
    database_file = resolved_database()
    print(f"  Database: {database_file}")
    warning = volume_warning(database_file)
    if warning:
        print(f"  !! {warning}")
    print(f"  Working dir: {PROJECT_ROOT}")
    print(f"  Laptop:  {scheme}://localhost:{args.port}")
    for ip in ips:
        if ip != "127.0.0.1":
            print(f"  Phone:   {scheme}://{ip}:{args.port}")
    print("-" * 68)
    if args.tunnel:
        print(f"  Tunnel mode: point your tunnel at http://localhost:{args.port}")
        print(f"      cloudflared tunnel --url http://localhost:{args.port}")
        print(f"      ngrok http {args.port}")
        print("  Then open the tunnel's https://... address on every device.")
        print("  On a host (Railway, Render, ...) this flag is how the edge terminates TLS:")
        print("  nothing is listening on a public interface here, only on the port above.")
        print("  First visit per browser, ngrok only: click 'Visit Site' on the warning page.")
        print("  The tunnel supplies the trusted HTTPS, so GPS and the camera work")
        print("  with no certificate warning.")
    elif args.http:
        print("  !! Plain HTTP: phones CANNOT use GPS or the camera like this.")
        print("     Drop --http to run the HTTPS server instead.")
    else:
        print("  First visit on each device: tap 'Advanced' -> 'Proceed' to accept")
        print("  the self-signed certificate. Then allow Location + Camera when asked.")
    print("=" * 68 + "\n")

    import uvicorn

    # Behind a tunnel or a reverse proxy the real client address arrives in
    # X-Forwarded-For, and uvicorn will only believe it from a peer on this list.
    #
    # This used to be hardcoded to loopback, which is right for the bundled TLS server
    # and for a tunnel running on the same host - and wrong, silently, for a proxy on
    # another machine: every worker behind it shared one slowapi bucket (a 15/minute
    # limit for the whole site), and every audit row recorded the proxy's address as
    # the actor's. It now reads the same ``TRUSTED_PROXIES`` setting the admin network
    # gate uses (``netguard``), so there is one list to get right rather than two that
    # can disagree.
    forwarded_allow_ips = trusted_proxies_for_uvicorn()
    print(f"  Trusted proxies for X-Forwarded-For: {forwarded_allow_ips}")

    uvicorn.run(
        APP_IMPORT,
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
