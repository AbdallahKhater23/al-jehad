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

The certificate is self-signed and stored in ``backend/certs/`` (git-ignored).
Each device has to accept the "not private" warning once; after tapping
"Advanced -> Proceed" the page counts as a secure context and GPS/camera work.
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
DATABASE_FILE = PROJECT_ROOT / "times.db"
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument("--http", action="store_true", help="serve plain HTTP instead of HTTPS")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--gen-cert-only", action="store_true", help="create the certificate then exit")
    parser.add_argument(
        "--tunnel", action="store_true",
        help="plain HTTP for a tunnel that provides the HTTPS (ngrok, cloudflared, ...)"
    )
    args = parser.parse_args()
    if args.tunnel:
        # The tunnel terminates TLS and forwards plain HTTP to us, so the browser
        # still gets a real trusted certificate. That is enough for GPS + camera,
        # and it avoids the self-signed warning entirely.
        args.http = True

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
    os.chdir(PROJECT_ROOT)
    for candidate in (BACKEND_DIR, PROJECT_ROOT):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))

    scheme = "http" if args.http else "https"
    ssl_kwargs = {}
    if not args.http:
        cert, key = ensure_certificate(ips)
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    print("\n" + "=" * 68)
    print("  Site Attendance server is starting")
    print("=" * 68)
    print(f"  Database: {DATABASE_FILE}")
    print(f"  Working dir: {PROJECT_ROOT}")
    print(f"  Laptop:  {scheme}://localhost:{args.port}")
    for ip in ips:
        if ip != "127.0.0.1":
            print(f"  Phone:   {scheme}://{ip}:{args.port}")
    print("-" * 68)
    if args.tunnel:
        print(f"  Tunnel mode: point your tunnel at http://localhost:{args.port}")
        print(f"      ngrok http {args.port}")
        print("  Then open the tunnel's https://... address on every device.")
        print("  First visit per browser: click 'Visit Site' on the ngrok warning page.")
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

    uvicorn.run(
        APP_IMPORT,
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Behind a tunnel/reverse proxy the real worker IP arrives in
        # X-Forwarded-For. slowapi rate-limits by client IP, so without this every
        # worker behind the same tunnel would share one 15/minute bucket.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
