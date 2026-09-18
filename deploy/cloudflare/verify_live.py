"""Check the live deployment: the backend, the Worker in front of it, and a punch through both.

WHY THIS EXISTS
---------------
Every failure mode of this deployment looks the same from a phone - "Cannot reach the server" -
and they have different fixes:

* the shell loads and every call 404s -> the Worker has no proxy (its asset layer answered);
* every call answers ``worker_misconfigured`` -> ``API_ORIGIN`` is empty or not a URL;
* every call answers ``api_unreachable`` -> the backend is down, or the origin names an address
  that has moved (a quick tunnel's hostname does this on every restart);
* a JSON 502 from the backend host with ``x-railway-fallback`` -> the host has no healthy
  service, which is a build/start/crash problem visible only in that host's deploy log.

Telling them apart by hand means five curls and knowing what each answer means. This does it in
one run, in the order that makes the first failure the one worth reading, and then - if given a
worker's credentials - it drives the two calls the punch screen itself makes, through the public
Worker URL: the clock-in window lookup and the punch.

The punch is expected to be *refused* for most runs (a geofence, an unenrolled account, a photo
that is not the enrolled face, a punch outside the site's window). A refusal from the API is
still a pass for this script: what it is checking is that the request reached the API and came
back with the API's own judgement, rather than a 404 from an asset host or a 502 from a proxy.

Usage (nothing is uploaded unless ``--punch`` is given)::

    python deploy/cloudflare/verify_live.py \\
        --worker https://al-jehad1.abdallahtamet281.workers.dev \\
        --origin https://al-jehad-production.up.railway.app

    # the punch screen's own two calls, with a worker's credentials
    python deploy/cloudflare/verify_live.py --worker <worker-url> \\
        --location 30.05,31.23 --user-id 1 --email worker@example.com --password '...' \\
        --selfie worker_photos/<id>.jpg --punch

The password is read from ``$PUNCH_PASSWORD`` when it is set, so it does not have to appear in
a shell history or a process list.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DEFAULT_ORIGIN_FROM_CONFIG = re.compile(r'^API_ORIGIN\s*=\s*"([^"]*)"', re.M)

#: Where the punch screen's calls live, and what the deployment's own answers are called.
SITE_WINDOW = "/api/v1/worker/me/site-window"
PUNCH = "/api/v1/attendance/verify"
MISCONFIGURED = "worker_misconfigured"
UNREACHABLE = "api_unreachable"


#: The client this script pretends to be while checking the deployment.
#: Cloudflare's bot rules refuse a plain ``curl``/``urllib`` user agent with a 403 and
#: ``error code: 1010`` on this host, and a health check that is refused for being a script is
#: not a health check - it is a false alarm about the app. The check still *reports* a 1010 when
#: it sees one, because the same rule that blocks this script can block a real phone on a
#: browser that Cloudflare does not like, and that is worth knowing before a worker does.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class Reply:
    """One HTTP answer, with the parts this script reasons about."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self.body = body
        # Header names arrive with whatever casing the server chose; lower-case keys mean a
        # caller never has to guess which spelling this answer used.
        self.headers = {key.lower(): value for key, value in headers.items()}

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            return None

    @property
    def looks_like_html(self) -> bool:
        """An asset answer, a redirect to a page, or a proxy's idea of an error page."""
        return self.text.lstrip()[:1] == "<" or "text/html" in self.headers.get("content-type", "")


def request(url: str, *, method: str = "GET", body: bytes | None = None,
            headers: dict[str, str] | None = None, timeout: float = 30.0) -> Reply:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("user-agent", BROWSER_UA)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return Reply(answer.status, answer.read(), dict(answer.headers))
    except urllib.error.HTTPError as err:  # a refusal is an answer, not a crash
        return Reply(err.code, err.read(), dict(err.headers))
    except Exception as err:  # noqa: BLE001 - DNS, TLS, a timeout: all reported the same way
        return Reply(0, str(err).encode(), {})


def multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """A form body the way the browser sends a punch: fields plus one file part."""
    boundary = "----verify-live-boundary-9f2a"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
    )
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Report:
    """The lines the operator reads, and the exit code they imply."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, verdict: str, detail: str = "") -> None:
        self.rows.append((name, verdict, detail))
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[verdict]
        print(f"[{mark}] {name}" + (f" - {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(verdict == "fail" for _, verdict, _ in self.rows)


def cloudflare_block(reply: Reply) -> str | None:
    """Cloudflare's own refusal, if that is what this is - which is not a fact about the app.

    A burst of requests from one address can trip Cloudflare's bot rules and earn a 403 with
    ``error code: 1010`` in the body. That is the *client* being refused, not the Worker: the
    shell and the API are both fine, and a phone usually is too. It has to be named as its own
    thing, because the two nearby readings - a 403 from the API (a real refusal) and a 404 from
    the asset layer (no proxy) - both lead to the wrong fix.
    """
    if reply.status not in (403, 429):
        return None
    if "error code: 1010" in reply.text or "banned your access" in reply.text:
        return (
            "Cloudflare refused this client (error code 1010, its bot rules) - the deployment is "
            "not necessarily broken, wait a moment and try again from a browser"
        )
    return None


def described(reply: Reply) -> str:
    """The shortest honest description of an answer, for the line beside the step name."""
    if reply.status == 0:
        return f"no answer ({reply.text.strip()[:120]})"
    blocked = cloudflare_block(reply)
    if blocked:
        return blocked
    if reply.status == 502 and reply.headers.get("x-railway-fallback"):
        return "502 from the backend host's edge (x-railway-fallback: no healthy service)"
    body = reply.json()
    if isinstance(body, dict):
        for code in (MISCONFIGURED, UNREACHABLE):
            if body.get("error_code") == code:
                return f"{reply.status} {code}: {str(body.get('message', ''))[:160]}"
        if body.get("error_code"):
            return f"{reply.status} {body['error_code']}"
    snippet = " ".join(reply.text.split())[:120]
    return f"{reply.status} {snippet}" if snippet else str(reply.status)


def check_status(report: Report, label: str, url: str, *, raw_json_ok: bool = False) -> Reply:
    """One ``/api/v1/status`` probe: it must answer JSON, and say what. Returns the reply."""
    reply = request(url, timeout=30.0)
    if reply.status == 200 and isinstance(reply.json(), dict):
        report.add(label, "pass", described(reply))
        return reply
    if raw_json_ok and reply.status and not reply.looks_like_html:
        report.add(label, "pass", described(reply))
        return reply
    if reply.looks_like_html or reply.status == 404:
        report.add(label, "fail", f"{described(reply)} - the asset host answered, not the API")
    else:
        report.add(label, "fail", described(reply))
    return reply


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", required=True, help="the public Worker URL, e.g. https://<name>.<account>.workers.dev")
    parser.add_argument("--origin", help="the backend's URL (default: API_ORIGIN from wrangler.toml)")
    parser.add_argument("--user-id", help="the worker's account id, for the login step")
    parser.add_argument("--email", help="the worker's email or phone, for the login step")
    parser.add_argument("--password", default=os.environ.get("PUNCH_PASSWORD"), help="or set $PUNCH_PASSWORD")
    parser.add_argument("--location", help="the 'lat,lon' the phone is standing at, e.g. 30.05,31.23")
    parser.add_argument("--selfie", help="a JPEG to send as the punch photo")
    parser.add_argument("--punch", action="store_true", help="actually POST a punch (uploads the selfie)")
    parser.add_argument("--action", default="Clock In", choices=["Clock In", "Clock Out"])
    args = parser.parse_args()

    worker = args.worker.rstrip("/")
    origin = (args.origin or "").strip()
    if not origin:
        config = (HERE / "wrangler.toml").read_text(encoding="utf-8")
        found = DEFAULT_ORIGIN_FROM_CONFIG.search(config)
        origin = found.group(1).strip() if found else ""
    origin = origin.rstrip("/")

    report = Report()
    print(f"Worker: {worker}\nBackend: {origin or '(not set anywhere)'}\n")

    # 1. the backend on its own, before blaming the Worker for anything.
    if not origin:
        report.add("backend /api/v1/status", "fail", "no --origin and API_ORIGIN is empty")
    else:
        check_status(report, "backend /api/v1/status", f"{origin}/api/v1/status")

    # 2. the same path through the Worker: JSON means the proxy is wired and reaching the API.
    worker_status = check_status(report, "worker /api/v1/status", f"{worker}/api/v1/status")

    # 3. the shell, which is what a browser loads first.
    shell = request(f"{worker}/")
    if shell.status == 200 and shell.looks_like_html:
        report.add("worker shell", "pass", f"{shell.status}, {len(shell.body)} bytes of HTML")
    else:
        report.add("worker shell", "fail", described(shell))

    # 4. the allow-list boundary: a path the Worker does not proxy must not reach the API. The
    #    asset layer answering 404 with no body is the pass; a refusal or a block is neither a
    #    pass nor a failure of the allow-list, so it is reported as unsettled rather than
    #    guessed at.
    boundary = request(f"{worker}/metrics")
    if cloudflare_block(boundary):
        report.add("allow-list boundary (/metrics is not proxied)", "skip", described(boundary))
    elif boundary.status == 404 and not boundary.body:
        report.add("allow-list boundary (/metrics is not proxied)", "pass", "404 from the asset layer")
    elif boundary.looks_like_html and "metrics" not in boundary.text:
        report.add("allow-list boundary (/metrics is not proxied)", "pass", "an asset answer, not the API")
    else:
        report.add(
            "allow-list boundary (/metrics is not proxied)",
            "fail",
            f"{described(boundary)} - the API answered a path the Worker should not forward",
        )

    # 5. the punch screen's own calls, through the Worker, with a real token.
    needed = (args.user_id, args.email, args.password, args.location)
    if not all(needed):
        report.add(
            "login + punch flow",
            "skip",
            "needs --user-id, --email, --password and --location (and a site to stand at)",
        )
        print("\n" + summary(report))
        return 1 if report.failed else 0

    login_body = json.dumps(
        {"user_id": args.user_id, "email_or_phone": args.email, "password": args.password}
    ).encode()
    login = request(
        f"{worker}/api/v1/auth/login",
        method="POST",
        body=login_body,
        headers={"content-type": "application/json"},
    )
    token = (login.json() or {}).get("access_token") if login.json() else None
    if not token:
        report.add("login through the Worker", "fail", described(login))
        print("\n" + summary(report))
        return 1
    report.add("login through the Worker", "pass", f"{login.status}, token issued")

    window = request(
        f"{worker}{SITE_WINDOW}?location_input={urllib.parse.quote(args.location)}",
        headers={"authorization": f"Bearer {token}"},
    )
    window_body = window.json() or {}
    # This is the punch card's line: the site the worker is standing on and the window in force
    # there. 200 means the punch screen will show it; anything else is the API's own refusal.
    report.add(
        "punch-screen site-window through the Worker",
        "pass" if window.status == 200 else "fail",
        (
            f"{window.status}, on_site={window_body.get('on_site')} "
            f"verdict={window_body.get('verdict')} window={window_body.get('window')}"
            if window.status == 200
            else described(window)
        ),
    )

    if not args.punch:
        report.add("punch through the Worker", "skip", "run again with --punch to send it")
        print("\n" + summary(report))
        return 1 if report.failed else 0

    if not args.selfie:
        report.add("punch through the Worker", "fail", "--punch needs --selfie <file.jpg>")
        print("\n" + summary(report))
        return 1
    selfie = Path(args.selfie)
    if not selfie.is_file():
        report.add("punch through the Worker", "fail", f"{selfie} does not exist")
        print("\n" + summary(report))
        return 1

    body, content_type = multipart(
        {"worker_id": str(args.user_id), "action": args.action, "location_input": args.location},
        "selfie",
        selfie.name,
        selfie.read_bytes(),
    )
    punch = request(
        f"{worker}{PUNCH}",
        method="POST",
        body=body,
        headers={"content-type": content_type, "authorization": f"Bearer {token}"},
    )
    punch_body = punch.json() or {}
    # A refusal is a pass here: the geofence, the clock-in window, liveness and the face match
    # are the API's judgement, and what this step checks is that the judgement arrived through
    # the Worker. The API's own refusals are JSON - ``{"detail": ...}`` for one of its checks,
    # ``{"error_code": ...}`` for the Worker's own - and an empty body or HTML means the asset
    # layer answered instead, or a proxy did. That distinction is the whole step, and it is why
    # a case the API answers with 404 ("facial reference not registered") is a pass: the status
    # is not what is being read, the shape of the answer is.
    answered_by_api = bool(punch_body.get("detail") or punch_body.get("error_code"))
    reached_api = punch.status != 0 and answered_by_api and not punch.looks_like_html
    detail = f"{punch.status} " + " ".join(punch.text.split())[:200]
    report.add(
        "punch through the Worker",
        "pass" if reached_api else "fail",
        detail if reached_api else f"{detail} - the API did not answer this",
    )

    print("\n" + summary(report))
    return 1 if report.failed else 0


def summary(report: Report) -> str:
    met = sum(1 for _, verdict, _ in report.rows if verdict == "pass")
    unmet = sum(1 for _, verdict, _ in report.rows if verdict == "fail")
    skipped = sum(1 for _, verdict, _ in report.rows if verdict == "skip")
    verdict = "the deployment works end to end" if not unmet else "read the FAIL lines above"
    return f"{met} passed, {unmet} failed, {skipped} skipped - {verdict}."


if __name__ == "__main__":
    sys.exit(main())
