"""What a cache is allowed to keep, and what it must never keep.

WHY THIS SUITE EXISTS
---------------------
Cache headers are the one part of a web server whose mistakes are invisible on the
machine that made them. A wrong directive does not raise, does not log, and does not
show up in a response body - it shows up three weeks later as a worker's phone running
a frontend that no longer exists on the server, or as somebody's clock-in time sitting
in a shared proxy. So the policy is pinned here rather than trusted to the two headers
somebody remembered to set.

The shape of the policy, and why it is not simply "cache everything":

1. **The document always revalidates.** It carries the ``?v=<content hash>`` tokens
   that name its assets, so a document served from a cache would point a browser at a
   build the server may have already replaced.
2. **An asset is immutable only when its token is its content hash.** A page always
   asks for the version it shipped with; a bookmark or a cached page asking for the
   *old* token falls back to revalidation rather than being handed an immutable stale
   copy. That fallback is what makes the aggressive ``max-age`` safe.
3. **An API answer is never stored.** Tokens, punch logs and personal data default to
   ``no-store``; a handler that knows better (the public, content-addressed branding
   logo) sets its own header and keeps it.
4. **Large text is compressed, small text is not.** The threshold exists so the
   framing does not cost more than it saves; the exclusion of images, fonts and the
   SSE stream is the library's, and this pins that the big scripts ride it.

The tests read the *served* headers, not the source, because the whole point is the
response the browser, the proxy and the CDN see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import harness

FRONTEND = Path(harness.PROJECT_ROOT) / "frontend"

#: The assets ``index.html`` asks for with a plain tag. Each one is a round trip a phone
#: at a gate used to make on *every* load; after this change it makes none of them on a
#: warm one. The list is named rather than discovered so a tag quietly dropping out of
#: the page is a failure here and not a silent regression in the field.
EAGER = (
    "api-config.js",
    "boot.js",
    "style.css",
    "i18n.js",
    "frontendjavascript.js",
    "offline_queue.js",
    "worker_modules.js",
    "icon.svg",
)

#: ``src="..."`` / ``href="..."`` in the served markup.
ATTRIBUTE = re.compile(r'(?:src|href)="([^"]+)"')


def _stamped(html: str) -> dict[str, str]:
    """``{asset name: full url}`` for every versioned attribute on a page."""
    found: dict[str, str] = {}
    for url in ATTRIBUTE.findall(html):
        name, _, query = url.partition("?")
        if query.startswith("v=") and name:
            found[name.rsplit("/", 1)[-1]] = url
    return found


def _token(url: str) -> str:
    return url.split("v=", 1)[1]


def test_the_document_revalidates_and_names_every_asset_with_its_hash(client):
    response = client.get("/")
    assert response.status_code == 200
    # The document itself is not cached: it is the thing that carries the tokens.
    assert response.headers["cache-control"] == "no-cache, must-revalidate"

    stamped = _stamped(response.text)
    for name in EAGER:
        assert name in stamped, f"index.html did not stamp {name} with a content hash"
        assert _token(stamped[name]), f"{name} was stamped with an empty token"
    # And the tag is still the bare filename plus a query, not an absolute URL: the page
    # has to keep working when the folder is hosted on its own.
    assert stamped["style.css"].startswith("style.css?v=")


@pytest.mark.parametrize("asset", ["style.css", "frontendjavascript.js", "worker_modules.js"])
def test_a_versioned_asset_is_immutable_and_the_unversioned_fallback_revalidates(client, asset):
    """The token is what buys the year; without it the safe answer is still the old one."""
    token = _token(_stamped(client.get("/").text)[asset])

    fresh = client.get(f"/{asset}?v={token}")
    assert fresh.status_code == 200
    control = fresh.headers["cache-control"]
    assert "max-age=31536000" in control, control
    assert "immutable" in control, control
    assert "stale-while-revalidate" in control, control

    # A bookmark, or a page from a cache, asks for the file with no token: it must be
    # revalidated, and it needs an ETag for that revalidation to be a 304 rather than a body.
    plain = client.get(f"/{asset}")
    assert plain.status_code == 200
    assert plain.headers["cache-control"] == "no-cache, must-revalidate"
    assert plain.headers.get("etag"), "the revalidation fallback has nothing to revalidate against"

    # A token from a build the server no longer serves must not be trusted as the current one.
    stale = client.get(f"/{asset}?v=000000000000")
    assert stale.headers["cache-control"] == "no-cache, must-revalidate"


def test_a_link_page_stamps_against_the_token_url_not_the_file(client, link_paths):
    """``/q/<token>`` is one segment deep, and the segment is a token.

    The page asks for its scripts as ``../quick.js`` so they resolve to the site root
    rather than into the token segment; the stamping has to walk the same path the browser
    will, or every asset on these pages silently stays unversioned.
    """
    page = client.get(link_paths["quick"])
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache, must-revalidate"
    stamped = _stamped(page.text)
    for name in ("api-config.js", "capture.js", "quick.js", "logo-mark.svg", "icon.svg"):
        assert name in stamped, f"the quick link page did not stamp {name}"

    # The URL the page names resolves to a real, versioned, executable asset.
    served = client.get(stamped["quick.js"])
    assert served.status_code == 200
    assert "javascript" in served.headers["content-type"]
    assert "immutable" in served.headers["cache-control"]


def test_api_answers_are_never_stored(client):
    """The default is ``no-store`` because the next route added will not remember to ask."""
    public = client.get("/api/v1/status")
    assert public.status_code == 200
    assert public.headers["cache-control"] == "no-store"

    # A read a worker owns, refused without a credential, is still not a document to keep.
    refused = client.get("/api/v1/worker/me/stats")
    assert refused.status_code in (401, 403), refused.status_code
    assert refused.headers["cache-control"] == "no-store"


def test_a_route_that_sets_its_own_policy_keeps_it(client):
    """The public branding mark is cached hard because its URL names its bytes."""
    response = client.get("/api/v1/branding/logo")
    if response.status_code == 404:
        pytest.skip("no company logo is configured in the fixture")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


def test_a_large_text_response_is_compressed_and_a_small_one_is_not(client):
    """The threshold, read as the browser reads it: not every answer is worth a frame."""
    source = (FRONTEND / "frontendjavascript.js").read_bytes()
    assert len(source) > 4096, "the fixture script is too small to exercise the threshold"

    compressed = client.get("/frontendjavascript.js", headers={"Accept-Encoding": "gzip"})
    assert compressed.status_code == 200
    assert compressed.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in compressed.headers.get("vary", "").lower()
    # httpx decodes it, so the bytes prove the pipeline is lossless.
    assert compressed.content == source

    identity = client.get("/frontendjavascript.js", headers={"Accept-Encoding": "identity"})
    assert identity.headers.get("content-encoding") is None
    assert identity.content == source

    small = client.get("/api/v1/status", headers={"Accept-Encoding": "gzip"})
    assert small.headers.get("content-encoding") is None, "a 22-byte JSON body is not worth a frame"


def test_the_document_answers_a_conditional_request_with_304(client):
    """``no-cache`` still revalidates; the ETag is what keeps that ask body-less."""
    first = client.get("/")
    etag = first.headers.get("etag")
    assert etag, "a revalidating document with no ETag re-downloads on every visit"
    again = client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache, must-revalidate"


def test_the_bare_register_page_still_stamps_its_assets(client):
    """``/register`` has no token, so it is the URL ``urljoin`` gets wrong.

    ``urljoin('/register', '../api-config.js')`` answers a *relative* ``'api-config.js'``
    rather than ``'/api-config.js'``: a base with no trailing slash is one segment deep, so
    ``..`` climbs past the root. A helper that trusted the result would leave every asset on
    this one page unversioned, silently.
    """
    page = client.get("/register")
    assert page.status_code == 200
    stamped = _stamped(page.text)
    for name in ("api-config.js", "capture.js", "register.js", "logo-mark.svg"):
        assert name in stamped, f"the bare register page did not stamp {name}"


def test_the_asset_token_follows_the_bytes(tmp_path):
    """A stale token is the one thing that would make an immutable cache wrong."""
    import main

    one = tmp_path / "one.js"
    one.write_bytes(b"first")
    two = tmp_path / "two.js"
    two.write_bytes(b"second")

    assert main.asset_version(str(one)) == main.asset_version(str(one))
    assert main.asset_version(str(one)) != main.asset_version(str(two))
    assert main.asset_version(str(tmp_path / "missing.js")) is None

    # A changed file is a changed token, which is the whole cache-busting contract.
    one.write_bytes(b"first, and then some")
    assert main.asset_version(str(one)) != main.asset_version(str(two))
