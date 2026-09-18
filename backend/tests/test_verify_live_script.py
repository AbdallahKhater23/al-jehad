"""The deployment checker's own logic: the parts that decide PASS from FAIL.

WHY THIS EXISTS
---------------
``deploy/cloudflare/verify_live.py`` is run by hand against a live deployment, so nothing else
here executes it - and its verdicts are what an operator acts on at the moment they are least
able to second-guess them. Three of those verdicts are easy to get subtly wrong, and each one
was wrong in the first draft of the script:

* **whose answer is this?** The API's refusals are JSON (``{"detail": ...}``) and the Worker's
  own are JSON with an ``error_code``, while an asset layer, a proxy or a host's edge answer
  with HTML, an empty body, or a JSON 502 that carries ``x-railway-fallback``. Reading a status
  code alone cannot tell them apart: the punch is refused with **404** when the account has no
  enrolled face - a real judgement - while a 404 from the asset layer means no proxy at all;
* **which refusal is the app's and which is Cloudflare's?** A burst of requests can earn a 403
  with ``error code: 1010`` ("banned your access") from Cloudflare's bot rules. That is the
  *client* being refused, not the deployment being broken, and it must never be reported as a
  failed check - the fix it would send somebody looking for does not exist;
* **header casing.** The first draft looked for ``server: cloudflare`` and never found it,
  because answers spell it ``Server``. Names are folded once, where the answer is built.

The multipart body is asserted too, since it is built by hand: a punch is only forwarded if the
boundary and the ``Content-Disposition`` blocks are exactly right, and a malformed one would
read as "the Worker drops the punch's photo".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from harness import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "deploy" / "cloudflare" / "verify_live.py"


@pytest.fixture(scope="module")
def verify():
    """The script loaded by path: it is operator tooling, not an importable package."""
    spec = importlib.util.spec_from_file_location("verify_live_under_test", SCRIPT)
    assert spec and spec.loader, f"{SCRIPT} is missing"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_an_answer_s_headers_are_readable_in_any_casing(verify):
    """An HTTP server may spell it ``Server``; the script must not have to guess."""
    reply = verify.Reply(403, b"", {"Server": "cloudflare", "Content-Type": "text/html"})
    assert reply.headers["server"] == "cloudflare"
    assert reply.headers["content-type"] == "text/html"


def test_cloudflare_s_own_refusal_is_not_reported_as_a_broken_deployment(verify):
    """Error 1010 is the client being banned, which is a different thing from a dead app."""
    blocked = verify.Reply(
        403,
        b"<html><body>error code: 1010</body></html>",
        {"Server": "cloudflare", "Content-Type": "text/html"},
    )
    message = verify.cloudflare_block(blocked)
    assert message, "a 1010 must be recognised"
    assert "1010" in message and "bot rules" in message
    assert "Cloudflare" in verify.described(blocked), verify.described(blocked)

    ordinary = verify.Reply(403, b'{"detail":"Not authenticated"}', {"content-type": "application/json"})
    assert verify.cloudflare_block(ordinary) is None, "an API refusal is not a Cloudflare block"
    assert "Not authenticated" in verify.described(ordinary)


def test_the_backend_host_s_fallback_502_is_named_as_the_host_s_problem(verify):
    """Railway's edge answers this when nothing healthy is behind the service."""
    reply = verify.Reply(
        502,
        json.dumps({"status": "error", "code": 502, "message": "Application failed to respond"}).encode(),
        {"Content-Type": "application/json", "x-railway-fallback": "true"},
    )
    described = verify.described(reply)
    assert "x-railway-fallback" in described
    assert "no healthy service" in described


def test_a_json_refusal_is_an_answer_from_the_api_and_html_is_not(verify):
    """The distinction the punch step turns on: the API judged it, or nobody did."""
    api_judgement = verify.Reply(
        404,
        b'{"detail":"Facial reference not registered. Please contact your administrator to enroll."}',
        {"content-type": "application/json"},
    )
    assert api_judgement.json().get("detail"), "the API's refusal is JSON with a detail"
    assert not api_judgement.looks_like_html

    asset_answer = verify.Reply(404, b"", {})
    assert asset_answer.json() is None and not asset_answer.looks_like_html
    assert not (asset_answer.json() or {}).get("detail"), "an empty 404 is nobody's judgement"

    block_page = verify.Reply(200, b"<!doctype html><html></html>", {"content-type": "text/html"})
    assert block_page.looks_like_html


def test_the_punch_body_is_a_form_the_api_can_parse(verify):
    """A punch is only forwarded if this body is exactly right, so its shape is asserted."""
    body, content_type = verify.multipart(
        {"worker_id": "1", "action": "Clock In", "location_input": "30.05,31.23"},
        "selfie",
        "selfie.jpg",
        b"\xff\xd8\xff\xe0jpeg-bytes",
    )
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()

    for field, value in (("worker_id", "1"), ("action", "Clock In"), ("location_input", "30.05,31.23")):
        assert f'name="{field}"\r\n\r\n{value}\r\n'.encode() in body, field
    assert b'name="selfie"; filename="selfie.jpg"' in body
    assert b"Content-Type: image/jpeg" in body
    assert b"jpeg-bytes" in body
    assert body.startswith(b"--" + boundary) and body.endswith(b"--" + boundary + b"--\r\n")


def test_the_summary_and_the_exit_code_agree(verify, capsys):
    report = verify.Report()
    report.add("a step", "pass", "fine")
    report.add("another", "skip", "no credentials")
    assert report.failed is False
    assert "1 passed, 0 failed, 1 skipped" in verify.summary(report)

    report.add("a real failure", "fail", "502")
    assert report.failed is True
    assert "read the FAIL lines above" in verify.summary(report)
    assert "FAIL" in capsys.readouterr().out
