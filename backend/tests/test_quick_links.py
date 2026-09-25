"""Quick clock links: ``/admin/quick_links*`` and the public one-tap punch ``/q/<token>``.

WHY THIS EXISTS
---------------
A quick link is the one credential in this application that clocks somebody in *without*
proving who is holding the phone. That is the feature, not a bug: it exists so a worker can
punch from a bare link on a phone with no app session. What these tests pin is everything
that keeps it bounded, because every one of those bounds is a thing a later change could
quietly remove:

1. the token is stored as a hash, so a database leak hands nobody a working punch;
2. the link is tied to one worker, and the punch endpoint takes no ``worker_id`` at all, so
   a link can never clock in a person it was not issued for;
3. it expires, it can be revoked, it can be capped at N uses, and a deactivated account's
   link stops working with it;
4. the geofence still applies - a link forwarded to somebody at home records nothing;
5. a selfie is still required, and it is *detected*, not *matched*: the face count is the
   whole of the biometric stage, the photo is stored, and no distance is invented;
6. every use is a row with the action, the site, the coordinates, the IP and the photo, and
   the photo is only reachable by an authenticated administrator;
7. a punch is never attributed to the link when it did not happen - a refused punch leaves
   no use row, no attendance row, and no orphaned face on disk.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from harness import (
    ADMIN,
    HEAD_ADMIN,
    INSIDE_DOWNTOWN,
    MOALLEM,
    OUTSIDE_ALL_SITES,
    PASSWORDS,
    SEED_USERS,
    WORKER,
    assert_denied,
    bearer,
    db_rows,
    db_scalar,
    jpeg_bytes,
)

import harness
import quick_links
import shift_hours

#: The reference the harness writes for every seeded user, captured at import.
#:
#: The database is restored from a pristine snapshot before every test; the files are not,
#: and one test below deactivates an account, which deletes that account's face template on
#: purpose. Without this snapshot the damage would surface in a later suite as "Reference
#: embedding not found" - a failure two files away from its cause. The same guard the
#: user-management suite carries, for the same reason.
SEEDED_REFERENCE = harness.reference_path(WORKER).read_text()


@pytest.fixture(autouse=True)
def _restore_reference_files():
    yield
    for user_id in SEED_USERS:
        harness.seed_reference(user_id, SEEDED_REFERENCE)


@pytest.fixture(autouse=True)
def _no_standing_review_flag():
    """Clear the flagged row the harness leaves for worker 1.

    The seed data carries one ``pending_review`` row as a standing fixture for the approval
    suites, and a flagged account is refused a clock-out on the password path *and* on this
    one (pinned by its own test below). Every other test here would therefore be measuring
    that rule instead of the link, so the flag is cleared except where it is the subject.
    """
    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _quick_photo_dir(tmp_path_factory):
    """Keep punch selfies out of the repository.

    The module reads ``PHOTOS_DIR`` at call time, so repointing it here is enough - the same
    trick ``conftest`` uses for ``main.WORKER_PHOTOS_DIR``. Without it this suite would write
    real face photos into the checkout.
    """
    original = quick_links.PHOTOS_DIR
    directory = tmp_path_factory.mktemp("quick_link_photos")
    quick_links.PHOTOS_DIR = str(directory)
    yield directory
    quick_links.PHOTOS_DIR = original


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def issue(client, worker_id: str = WORKER, headers=None, **overrides):
    # ``headers if ... is not None`` rather than ``or``: an explicitly empty header set is
    # the anonymous caller, which is exactly the case the authorization tests send.
    return client.post(
        "/api/v1/admin/quick_links",
        headers=bearer(HEAD_ADMIN) if headers is None else headers,
        json={"worker_id": worker_id, **overrides},
    )


def punch(
    client,
    token,
    *,
    coordinates: str = INSIDE_DOWNTOWN,
    image: bytes | None = None,
    accuracy=None,
    confirm: bool = True,
):
    data = {"lat": coordinates.split(",")[0], "lon": coordinates.split(",")[1]}
    if accuracy is not None:
        data["accuracy"] = str(accuracy)
    # ``confirm`` is on by default in this file because nearly every punch here is taken
    # seconds after the shift opened - which is exactly the tap the early clock-out question
    # exists for. These tests are about the *link*: who may use it, how often, what it
    # records. The question itself is proved in ``test_early_checkout_and_rounding.py``, and
    # one test here turns the flag off to show the refusal rather than assume it.
    if confirm:
        data["confirm_early_checkout"] = "1"
    return client.post(
        f"/api/v1/q/{token}",
        data=data,
        files={"selfie": ("selfie.jpg", image if image is not None else jpeg_bytes(), "image/jpeg")},
    )


def active_session(worker_id: str = WORKER) -> str | None:
    return db_scalar("SELECT clock_in_time FROM active_sessions WHERE worker_id = ?", (worker_id,))


def clear_sessions(worker_id: str = WORKER) -> None:
    """Close the shift the seed data leaves open, so the next tap is a clock-in.

    The harness seeds worker 1 mid-shift on purpose (the clock-out tests need it), which
    means the first tap of a test would otherwise be a clock-out - and whether it is would
    depend on fixture order rather than on what the test is about.
    """
    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (worker_id,))
        conn.commit()
    finally:
        conn.close()


def seed_open_shift(worker_id: str = WORKER, hours_ago: float = 0.5) -> None:
    """Put a clock-in row on the worker's record ``hours_ago`` back, as the app would."""
    import sqlite3

    import harness

    started = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO active_sessions (worker_id, site_name, clock_in_time) VALUES (?,?,?)",
            (worker_id, "Downtown Tower A", started),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# issuing a link
# ---------------------------------------------------------------------------
def test_only_an_administrator_may_issue_a_link(client):
    """A worker holding a link cannot mint more of them."""
    for headers in ({}, bearer(WORKER), bearer(MOALLEM)):
        assert_denied(
            issue(client, headers=headers),
            endpoint="/admin/quick_links",
            detail="a clock link is a punch without a password",
        )


#: The two pages a link opens, and the script each one is made of. Both are served one
#: segment deep (``/q/<token>``, ``/enroll/<token>``) and both load their script with a
#: ``src`` that has to be read against that URL rather than against the file on disk.
LINK_PAGES = (("quick.html", "quick.js"), ("enroll.html", "enroll.js"))


def test_the_page_a_link_opens_reaches_a_script_the_browser_will_run(client):
    """The link worked; the page it opened did not, and nothing said so.

    ``quick.html`` and ``enroll.html`` are served at ``/q/<token>`` and ``/enroll/<token>``
    - one segment deep, where that segment is a token and not a directory. A bare
    ``src="quick.js"`` therefore resolves to ``/q/quick.js``, which the *token route* matches
    and answers with the page itself, as ``text/html``. Browsers refuse to execute a script
    whose response is HTML (strict MIME checking, and Chrome says so in the console), so
    neither file ever ran: the page kept the two placeholders it ships with - "Checking this
    link…" and "Checking your location…" - and it looked like a hung connection rather than
    a broken asset. The same trap caught the enrollment page, which nobody reported.

    Measured rather than asserted from the markup: the second half fetches the URL the
    browser will build from that ``src`` and insists it comes back as JavaScript, not as the
    page again. A page that reaches for its script in the wrong direction fails here instead
    of on a worker's phone at a gate; a "tidy-up" back to the bare file name fails here too.
    """
    for page, script in LINK_PAGES:
        body = (harness.PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        sources = re.findall(r'<script[^>]*\bsrc="([^"]+)"', body)
        assert sources, f"{page} loads no script at all"
        # Every script on the page, not just the first: the shared capture module was added
        # to both pages, and a trap that only watches the page's own file would let the new
        # one be named the wrong way.
        assert sources[-1] == f"../{script}", (
            f'{page} asks for its own flow as "{sources[-1]}". It is served at '
            f'/<prefix>/<token>, so anything but "../{script}" resolves into the token '
            "segment and comes back as this page"
        )
        assert "../capture.js" in sources, (
            f"{page} does not load the shared capture module. The camera, the photo policy "
            "and the location fix are one file for both link pages; a page that grows its "
            "own copy again is the drift this refactor removed"
        )

        for src in sources:
            name = src.rsplit("/", 1)[-1]
            assert src == f"../{name}", (
                f'{page} asks for "{src}". Both link pages are served at /<prefix>/<token> '
                f"- one segment deep, and that segment is a token - so the script has to "
                f'be asked for one level up, as "../{name}"'
            )
            # What the browser will request, and what it has to be given back.
            served = client.get(f"/{name}")
            assert served.status_code == 200, f"/{name} is not served: {served.status_code}"
            content_type = served.headers.get("content-type", "")
            assert "javascript" in content_type, (
                f"/{name} answered as {content_type!r}; a script served as anything else is "
                "a script the browser refuses to execute"
            )
            assert "<!DOCTYPE html>" not in served.text[:200], (
                f"/{name} answered with the page rather than the script"
            )


#: Every page the frontend ships, and the way it has to ask for the company's mark.
#: ``index.html`` is served from the site root, so a bare name is right; the two link pages
#: are served one segment deep under a token, so theirs climb a level. The difference is why
#: this is a table and not one shared expectation: a single expectation is what let the mark
#: go missing on the two pages a worker actually opens.
MARK_PAGES = (("index.html", "icon.svg"), ("quick.html", "../icon.svg"), ("enroll.html", "../icon.svg"))


def test_every_page_hands_the_browser_the_company_mark(client):
    """The tab and the home screen showed the browser's default globe on the pages that matter most.

    ``index.html`` has carried the mark since it was added. ``quick.html`` and
    ``enroll.html`` never did, and the reason is the trap the script test above describes:
    both are served at ``/<prefix>/<token>``, so a bare ``href="icon.svg"`` asks the *token
    route* for ``/q/icon.svg``, which matches and answers with the page itself. HTML behind
    ``rel="icon"`` is an icon the browser discards without a word, so the tab fell back to
    the default globe - and a worker who kept the punch button on their home screen got a
    grey page snapshot for a tile instead of the company's column. Nothing failed, nothing
    was logged, and no test could see it: the pages were not broken, they were unbranded.

    Both halves are pinned: the direction each page asks in, and what the browser is given
    back when it follows that href from the page's own URL.
    """
    for page, href in MARK_PAGES:
        body = (harness.PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        for rel, why in (
            ("icon", "the browser tab"),
            ("apple-touch-icon", "the home-screen tile"),
        ):
            found = re.findall(rf'<link[^>]*\brel="{rel}"[^>]*\bhref="([^"]+)"', body)
            assert found == [href], (
                f'{page} asks for {found or "no icon at all"} for {why}; expected "{href}". '
                f"The page is served from a URL of a different depth than its own file name, "
                f"and an href that does not account for that reaches the token route, which "
                f"answers with the page - and a page is not an icon"
            )

    # What the browser will follow that href to, and what it has to be handed back.
    served = client.get("/icon.svg")
    assert served.status_code == 200, f"/icon.svg is not served: {served.status_code}"
    content_type = served.headers.get("content-type", "")
    assert "svg" in content_type, (
        f"/icon.svg answered as {content_type!r}; an icon served as anything else is an icon "
        "the browser will not draw"
    )
    # Deliberately the whole body and not its opening: this file leads with a long comment
    # explaining the artwork, so the element is a few hundred characters in.
    assert "<svg" in served.text and "viewBox=" in served.text, (
        "/icon.svg did not come back as SVG markup"
    )
    assert "<!DOCTYPE html>" not in served.text[:200], (
        "/icon.svg answered with a page rather than the mark"
    )


def test_the_badge_is_the_same_column_as_the_mark():
    """One drawing, two files, kept equal by this test rather than by good intentions.

    ``icon.svg`` is the mark scaled into a badge, and it cannot *reference*
    ``logo-mark.svg`` - an ``<img>``-loaded SVG has no way to reach into another file - so
    the geometry is copied, and the file's own comment asks the next person to change a
    point in both. A copy is a promise to keep two places in step, and the tile a browser
    draws is a small enough surface that the drift would go unnoticed for months.

    The numbers are compared, not the files. The badge is allowed its own viewBox, its green
    ground and its shortened gradient; it is not allowed a different column.
    """

    def geometry(name: str) -> set[str]:
        body = (harness.PROJECT_ROOT / "frontend" / name).read_text(encoding="utf-8")
        shapes = re.findall(
            r'<path d="([^"]+)"'
            r'|<rect x="([^"]+)" y="([^"]+)" width="([^"]+)" height="([^"]+)"'
            r'|<circle cx="([^"]+)" cy="([^"]+)" r="([^"]+)"',
            body,
        )
        return {part for shape in shapes for part in shape if part}

    mark = geometry("logo-mark.svg")
    badge = geometry("icon.svg")
    assert mark, "logo-mark.svg has no geometry left to compare"
    assert badge == mark, (
        "the badge and the mark are no longer the same column. "
        f"Only in logo-mark.svg: {sorted(mark - badge)}. "
        f"Only in icon.svg: {sorted(badge - mark)}"
    )


#: The colour the console paints its own chrome with - the same #162F29 the brand block in
#: style.css hands the rail, the sign-in panel and the mark.
COMPANY_GREEN = "#162F29"

#: The slate these two pages shipped on before they had a brand: the blue-grey of a default
#: dark theme, chosen for the job and never for the company.
SLATE = "#0f172a"


def test_every_page_paints_the_phone_the_company_green():
    """The first thing the brand touches on a phone, and the last thing anybody notices is missing.

    ``theme-color`` is what an installed app paints its status bar with, and it is decided
    before the page has painted anything at all - so on the punch page it was a slate bar
    over a slate page, on a screen whose whole job is to belong to Al-Jehad. It is the same
    value on all three pages because it is the same app.
    """
    for page in ("index.html", "quick.html", "enroll.html"):
        body = (harness.PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        theme = re.findall(r'<meta name="theme-color" content="([^"]+)"', body)
        assert theme == [COMPANY_GREEN], (
            f"{page} tells the phone to paint its status bar {theme or 'nothing'}, and the "
            f"console paints {COMPANY_GREEN}. A status bar in a third colour over a page in "
            f"the company's is the seam this is here to keep shut"
        )


def test_the_link_pages_draw_the_mark_and_the_brand_palette_in_their_own_head():
    """Both link pages are standalone, so neither can inherit the console's styling.

    They load no stylesheet - deliberately, because a worker opens them from a link on site
    cellular and the page has to paint on the first frame - so the palette is written into
    each page's own ``<style>`` and the mark is an ``<img>`` one level up. That copying is
    the price of the two pages standing alone, and it is exactly the sort of thing that gets
    half-done: the mark was on the console's rail and the tab and nowhere a worker looked,
    and the pages kept the slate palette for the whole life of the feature.
    """
    for page, _ in LINK_PAGES:
        body = (harness.PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        assert f"--brand-green: {COMPANY_GREEN}" in body, (
            f"{page} does not define the brand palette it paints itself with. These pages "
            "load no stylesheet, so a colour that is not in this file is not on the page"
        )
        assert re.search(r'<img src="\.\./logo-mark\.svg"[^>]*>', body), (
            f"{page} does not draw the company mark. It has to be one level up, like every "
            "other asset on these pages"
        )
        assert SLATE not in body.lower(), (
            f"{page} is back on the slate palette ({SLATE}). It was replaced by the "
            f"company's green and the mark for a reason: these are the two pages a worker "
            "opens, and they were the only unbranded screens in the app"
        )


def test_the_lockup_the_artwork_sets_is_on_both_link_pages():
    """The company's four lines, in the artwork's order, and never translated.

    ``BRAND_DEFAULTS`` in frontendjavascript.js is where the console reads these four strings
    from, and the two link pages cannot read it: they load none of the console's scripts, want
    the lockup on the first frame, and so carry their own copy of it. This test is what keeps
    the copy honest - rename the company in one place and the other fails here, instead of
    shipping a page whose wordmark says something the console's does not.

    It also refuses to let them be translated. The pages translate anything carrying a
    ``data-t`` key, and ``capture.js`` has a table full of them; a wordmark, a legal suffix
    and a founding year are proper nouns, and a translated wordmark is a different logo.
    """
    source = (harness.PROJECT_ROOT / "frontend" / "frontendjavascript.js").read_text(encoding="utf-8")
    block = re.search(r"const BRAND_DEFAULTS = \{(.*?)\n\};", source, re.S)
    assert block, "BRAND_DEFAULTS has been renamed or moved; this test reads the lockup from it"
    shipped = [
        re.search(rf"\b{key}: '([^']*)'", block.group(1)).group(1)
        for key in ("name", "legal", "est", "tagline")
    ]

    for page, _ in LINK_PAGES:
        body = (harness.PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        offsets = [body.find(f">{line}<") for line in shipped]
        missing = [line for line, at in zip(shipped, offsets) if at < 0]
        assert missing == [], (
            f"{page} does not draw {missing}. The artwork sets {shipped}; a page that draws "
            "three of the four lines is a different lockup"
        )
        assert offsets == sorted(offsets), (
            f"{page} draws the lockup out of order: {shipped} appears at {offsets}. The order "
            "is part of the logo"
        )
        translated = re.findall(r'class="brand-(?:name|legal|est|tagline)"[^>]*\bdata-t', body)
        assert translated == [], (
            f"{page} hands the lockup to the translator ({translated}). It is the company's "
            "name, not a sentence"
        )


def test_both_link_pages_carry_every_element_the_shared_module_reaches_for():
    """One capture module, two pages, and it reaches into their markup by id.

    This is the failure that costs the most and shows the least: a page missing an element
    the module binds to throws while wiring its buttons, so the *whole* page stops - the
    link is never fetched, the state never painted, and it sits on "Checking this link…"
    looking like a slow connection. The enrollment page lost its file input exactly this
    way, when the shared translation pass replaced the text of the label the input was
    nested in.

    The id list is read off the module's own source rather than written out here, so an id
    added for one page cannot be forgotten on the other.
    """
    module = (harness.PROJECT_ROOT / "frontend" / "capture.js").read_text(encoding="utf-8")
    ids = sorted(set(re.findall(r'getElementById\(\"([\w-]+)\"\)', module)))
    # The location line is addressed through an option on ``locate``, so it is named here.
    ids = sorted(set(ids) | {"location-line"})
    assert len(ids) >= 6, f"capture.js addresses only {ids}"

    #: The one id only the punch page has, and deliberately: enrollment is not
    #: geo-verified, so there is no line to write a fix into and the module checks for the
    #: element instead of assuming it. Every other id is required on both pages.
    PUNCH_ONLY = {"location-line"}

    for page, _ in LINK_PAGES:
        body = (harness.PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        required = ids if page.startswith("quick") else [n for n in ids if n not in PUNCH_ONLY]
        missing = [name for name in required if f'id="{name}"' not in body]
        assert missing == [], (
            f"{page} is missing {missing}: capture.js addresses it by id, so the page would "
            "stop at its first binding instead of rendering"
        )
        # ``hidden`` is how the module shows and hides the camera, the preview and the
        # fallback label. It is each page's own stylesheet, so each page has to define it.
        assert ".hidden" in body, f"{page} uses the module's show/hide contract but never styles it"


def test_the_early_clock_out_question_is_wired_on_the_punch_page():
    """The question needs its elements bound and its sentences in all three tables.

    The link page is the one surface with no session and no app behind it, so a button the
    page never binds, or a sentence missing from one of the three tables, is a blank amber
    card at a gate - the silent failure this file already exists to catch. The app's own
    table has a parity test (``test_frontend_payload.py``); this page keeps its own, so it
    gets its own.
    """
    page = (harness.PROJECT_ROOT / "frontend" / "quick.html").read_text(encoding="utf-8")
    for element in ("early-confirm", "early-body", "btn-early-confirm", "btn-early-cancel"):
        assert f'id="{element}"' in page, f"quick.html has no #{element} for the flow to bind"

    module = (harness.PROJECT_ROOT / "frontend" / "capture.js").read_text(encoding="utf-8")
    # The number of tables is read off the page's own language list rather than written here,
    # so a language added to the picker is covered by this test without editing it - and a
    # sentence that reaches only some of the tables fails rather than passing quietly.
    languages = sorted(set(re.findall(r'code: "([a-z]{2})"', module)))
    assert len(languages) >= 3, f"capture.js offers only {languages}"
    for key in ("quick.earlyTitle", "quick.earlyBody", "quick.earlyConfirm", "quick.earlyCancel"):
        found = module.count(f'"{key}"')
        assert found == len(languages), (
            f"{key} is in {found} of the {len(languages)} language tables ({languages}) in capture.js"
        )

    # The link pages mirror themselves from their own list, the way the app does: Urdu is
    # right to left, and a page that renders it left to right reads as broken to the reader.
    assert re.search(r'var RTL = \["ar", "ur"\]', module), (
        "capture.js must name both right-to-left languages, and only those"
    )

    flow = (harness.PROJECT_ROOT / "frontend" / "quick.js").read_text(encoding="utf-8")
    assert "confirm_early_checkout" in flow, "the page never sends the flag the server asks for"
    assert "quick.earlyBody" in flow, "the page never writes the question it just defined"


def test_issue_returns_a_usable_link_once(client):
    response = issue(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["worker_id"] == WORKER
    assert body["token"] and body["token"] in body["url"]
    assert body["url"].endswith(f"/q/{body['token']}")

    # The token is shown once and only its hash is kept: the row on disk must not contain a
    # string that can be pasted into a URL.
    stored = db_scalar("SELECT token_hash FROM quick_links WHERE id = ?", (body["link_id"],))
    assert stored != body["token"]
    assert len(stored) == 64
    assert body["token"] not in stored


def test_issue_refuses_an_unknown_worker_and_an_administrator(client):
    assert issue(client, worker_id="999999").status_code == 404

    # An administrative account is not a punching account, and a link is a punch with no
    # credential behind it - so the two must never meet.
    refused = issue(client, worker_id=HEAD_ADMIN)
    assert refused.status_code == 400
    assert "administrator" in refused.json()["detail"].lower()


def test_issue_bounds_the_ttl_and_the_use_cap(client):
    assert issue(client, ttl_hours=0).status_code == 400
    assert issue(client, ttl_hours=24 * 365 + 1).status_code == 400
    assert issue(client, max_uses=-1).status_code == 400
    assert issue(client, max_uses=101).status_code == 400


def test_a_link_can_be_revoked_and_revocation_is_idempotent(client):
    link = issue(client).json()
    first = client.post(
        f"/api/v1/admin/quick_links/{link['link_id']}/revoke", headers=bearer(HEAD_ADMIN)
    )
    assert first.status_code == 200, first.text
    stamp = first.json()["revoked_at"]

    second = client.post(
        f"/api/v1/admin/quick_links/{link['link_id']}/revoke", headers=bearer(HEAD_ADMIN)
    )
    assert second.status_code == 200
    assert second.json()["revoked_at"] == stamp, "a second click must not rewrite the timestamp"

    refused = punch(client, link["token"])
    assert refused.status_code == 410
    assert refused.json()["detail"]["error_code"] == "link_revoked"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE source = 'quick_link'") == 0
    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0


def test_issuing_requires_authentication_but_a_link_is_public(client):
    """The asymmetry, stated: the console needs a token, the link needs nothing but itself."""
    assert_denied(
        client.get("/api/v1/admin/quick_links"), endpoint="/admin/quick_links", detail="listing links"
    )
    assert client.get("/api/v1/q/not-a-real-token").status_code == 404


# ---------------------------------------------------------------------------
# the public page's read
# ---------------------------------------------------------------------------
def test_the_info_endpoint_says_who_and_which_way(client):
    link = issue(client).json()
    info = client.get(f"/api/v1/q/{link['token']}")
    assert info.status_code == 200, info.text
    body = info.json()
    assert body["worker_id"] == WORKER
    assert body["worker_name"] == "Seed Worker"
    assert body["next_action"] == "Clock Out", "the harness seeds an open shift for worker 1"
    assert body["clocked_in"] is True

    # With no open shift the same link reads the other way, which is what lets one button be
    # both the clock-in and the clock-out.
    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM active_sessions WHERE worker_id = ?", (WORKER,))
        conn.commit()
    finally:
        conn.close()
    again = client.get(f"/api/v1/q/{link['token']}").json()
    assert again["clocked_in"] is False
    assert again["next_action"] == "Clock In"


def test_an_unknown_token_is_refused_without_leaking_anything(client):
    response = client.get("/api/v1/q/definitely-not-a-token")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "link_unknown"


# ---------------------------------------------------------------------------
# the punch
# ---------------------------------------------------------------------------
def test_a_tap_clocks_in_then_out_with_no_password(client):
    clear_sessions()
    link = issue(client).json()
    clock_in = punch(client, link["token"])
    assert clock_in.status_code == 200, clock_in.text
    assert clock_in.json()["action"] == "Clock In"
    assert clock_in.json()["site"] == "Downtown Tower A"
    assert clock_in.json()["face_count"] == 1
    assert active_session() is not None

    clock_out = punch(client, link["token"])
    assert clock_out.status_code == 200, clock_out.text
    body = clock_out.json()
    assert body["action"] == "Clock Out"
    assert active_session() is None
    assert body["hours"] >= 0.0

    # Both punches are on the worker's record, attributed to the link rather than to a face
    # match that never happened.
    rows = db_rows(
        "SELECT action, source, status, flag_reason FROM attendance_logs WHERE worker_id = ? "
        "ORDER BY id DESC LIMIT 2",
        (WORKER,),
    )
    assert [row[0] for row in rows] == ["Clock Out", "Clock In"]
    assert {row[1] for row in rows} == {"quick_link"}
    assert all("quick link" in (row[3] or "") for row in rows)
    assert all("not matched" in (row[3] or "") for row in rows)

    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 2


def test_a_short_tap_is_questioned_before_anything_is_recorded(client):
    """The link asks the same question the app does, and moves nothing until it is answered.

    A link punch taken seconds after the clock-in is the clearest case of a short shift
    there is - and the one an impatient double tap produces. The refusal has to leave the
    shift open, the link's uses untouched and no clock-out row, or cancelling would cost
    the worker a shift.
    """
    clear_sessions()
    link = issue(client).json()
    assert punch(client, link["token"]).status_code == 200, "the first tap is the clock-in"
    # The seed data leaves clock-out rows of its own behind, so what this pins is that
    # the refusal adds none - not that the table is empty.
    before = db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'",
        (WORKER,),
    )

    refused = punch(client, link["token"], confirm=False)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error_code"] == "confirm_early_checkout"
    assert detail["regular_hours"] == 8.0

    assert active_session() is not None, "a question must not close the shift"
    assert db_scalar(
        "SELECT COUNT(*) FROM attendance_logs WHERE worker_id = ? AND action = 'Clock Out'",
        (WORKER,),
    ) == before, "nothing may be recorded before the worker answers"
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 1, (
        "a refused tap is not a use of the link"
    )

    confirmed = punch(client, link["token"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["action"] == "Clock Out"
    assert active_session() is None


def test_the_worker_is_the_links_worker_and_not_the_callers_choice(client):
    """There is no ``worker_id`` field to send, and sending one changes nothing."""
    link = issue(client, worker_id=MOALLEM).json()
    clear_sessions()

    response = client.post(
        f"/api/v1/q/{link['token']}",
        data={"lat": "30.05", "lon": "31.23", "worker_id": WORKER},
        files={"selfie": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["worker_id"] == MOALLEM
    assert active_session(MOALLEM) is not None
    assert active_session(WORKER) is None, "a parameter the client sent must not redirect the punch"


def test_a_phone_outside_every_site_records_nothing(client):
    link = issue(client).json()
    before = db_scalar("SELECT COUNT(*) FROM attendance_logs")
    refused = punch(client, link["token"], coordinates=OUTSIDE_ALL_SITES)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "outside_geofence"
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs") == before
    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_mock_gps_coordinates_are_refused_as_invalid_input(client):
    """``(0,0)`` is the signature of a mock-location app, so it is refused as *bad input*."""
    link = issue(client).json()
    refused = punch(client, link["token"], coordinates="0,0")
    assert refused.status_code == 400
    assert "mock" in reflected(refused).lower()


def reflected(response) -> str:
    """The whole refusal, whatever shape FastAPI gave it, as one searchable string."""
    detail = response.json().get("detail")
    return detail if isinstance(detail, str) else str(detail)


def test_a_selfie_is_required_and_a_face_must_be_in_it(client, face):
    link = issue(client).json()

    missing = client.post(f"/api/v1/q/{link['token']}", data={"lat": "30.05", "lon": "31.23"})
    assert missing.status_code == 422, "the photo is not optional"

    face.FACE_MODE = "none"
    no_face = punch(client, link["token"])
    assert no_face.status_code == 400
    assert no_face.json()["detail"]["error_code"] == "no_face"

    face.FACE_MODE = "match"
    face.FACE_COUNT = 2
    two_faces = punch(client, link["token"])
    assert two_faces.status_code == 400
    assert two_faces.json()["detail"]["error_code"] == "multiple_faces"

    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_a_refused_punch_leaves_no_orphaned_photo(client, face):
    link = issue(client).json()
    face.FACE_MODE = "none"
    assert punch(client, link["token"]).status_code == 400
    assert list((_dir()).iterdir()) == [], "a face with no punch to explain it must not stay on disk"


def _dir():
    from pathlib import Path

    return Path(quick_links.PHOTOS_DIR)


def test_the_face_is_counted_not_matched(client, face):
    """A worker whose face reference does not match is still clocked in - by design.

    This is the honest trade the feature is built on: the link is the credential, the photo
    is evidence for a human, and a similarity score here would be a fabricated number. The
    test pins that no distance is invented (the row carries the same 1.0 the admin-override
    rows carry) and that the reference template is never consulted at all.
    """
    link = issue(client).json()
    face.FACE_MODE = "mismatch"  # the stub now returns a vector at distance ~2.00
    assert punch(client, link["token"]).status_code == 200
    assert db_scalar("SELECT score FROM attendance_logs WHERE source = 'quick_link'") == 1.0


def test_liveness_refuses_a_presentation_attack_when_it_is_enforcing(client, monkeypatch):
    """In ``enforce`` mode a spoof is refused, exactly as on the password path."""
    import liveness

    link = issue(client).json()

    class _Decision:
        allowed = False
        error_code = liveness.ERR_SPOOF

        class result:  # noqa: N801 - mirrors the real shape
            verdict = liveness.VERDICT_SPOOF
            detail = "stubbed spoof"

        def log_fields(self):
            return ("spoof", 0.01)

        def as_payload(self):
            return {"verdict": "spoof"}

    monkeypatch.setattr(quick_links.liveness, "inspect", lambda *a, **k: _Decision())
    refused = punch(client, link["token"])
    assert refused.status_code == 422
    assert refused.json()["detail"]["error_code"] == liveness.ERR_SPOOF
    assert db_scalar("SELECT COUNT(*) FROM attendance_logs WHERE source = 'quick_link'") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_a_flagged_account_cannot_close_its_shift_with_a_link(client):
    """The password path's review rule applies here too: a link is not a way round the queue.

    The shift stays open, which the auto-close policy bounds, and an administrator is one
    tap away from clearing the flag - the alternative (letting the link close a shift that is
    already under review) would make the review queue optional for exactly the workers it was
    raised about.
    """
    import sqlite3

    import harness

    link = issue(client).json()
    clear_sessions()
    assert punch(client, link["token"]).json()["action"] == "Clock In"

    flagged_id = 900777
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("DELETE FROM attendance_logs WHERE status = 'pending_review'")
        conn.execute(
            "INSERT INTO attendance_logs (id, worker_id, site_name, action, timestamp, hours, "
            "score, status) VALUES (?,?,?,?,?,?,?,?)",
            (
                flagged_id,
                WORKER,
                "Downtown Tower A",
                "Clock In",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                0.0,
                0.5,
                "pending_review",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO active_sessions (worker_id, site_name, clock_in_time) "
            "VALUES (?,?,?)",
            (WORKER, "Downtown Tower A", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()

    refused = punch(client, link["token"])
    assert refused.status_code == 403
    assert refused.json()["detail"]["error_code"] == "account_flagged"
    assert active_session() is not None, "the shift stays open for a human to close"
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM attendance_logs WHERE source = 'quick_link' AND action = 'Clock Out'"
        )
        == 0
    ), "no clock-out row was written for the refused tap"
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 1


def test_a_use_cap_is_enforced(client):
    link = issue(client, max_uses=1).json()
    assert punch(client, link["token"]).status_code == 200
    exhausted = punch(client, link["token"])
    assert exhausted.status_code == 410
    assert exhausted.json()["detail"]["error_code"] == "link_used_up"
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 1


def test_an_expired_link_does_not_work(client):
    link = issue(client, ttl_hours=1).json()
    import sqlite3

    import harness

    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute("UPDATE quick_links SET expires_at = ? WHERE id = ?", (past, link["link_id"]))
        conn.commit()
    finally:
        conn.close()

    refused = punch(client, link["token"])
    assert refused.status_code == 410
    assert refused.json()["detail"]["error_code"] == "link_expired"


def test_a_deactivated_workers_link_stops_with_the_account(client):
    link = issue(client).json()
    deactivated = client.post(
        "/api/v1/admin/users/status",
        headers=bearer(HEAD_ADMIN),
        json={"user_id": WORKER, "active": False},
    )
    assert deactivated.status_code == 200, deactivated.text

    refused = punch(client, link["token"])
    assert refused.status_code == 410
    assert refused.json()["detail"]["error_code"] == "link_account_inactive"
    assert client.get(f"/api/v1/q/{link['token']}").status_code == 410


# ---------------------------------------------------------------------------
# what the administrator gets to see
# ---------------------------------------------------------------------------
def test_the_console_can_see_every_link_and_what_it_did(client):
    link = issue(client, note="Gate 2 crew").json()
    clear_sessions()
    punch(client, link["token"])  # a clock-in
    punch(client, link["token"])  # and the clock-out that closes it

    listed = client.get("/api/v1/admin/quick_links", headers=bearer(HEAD_ADMIN))
    assert listed.status_code == 200, listed.text
    entry = next(item for item in listed.json() if item["id"] == link["link_id"])
    assert entry["worker_id"] == WORKER
    assert entry["worker_name"] == "Seed Worker"
    assert entry["uses"] == 2
    assert entry["usable"] is True
    assert entry["state"] == "active"
    assert entry["note"] == "Gate 2 crew"
    assert entry["last_used_at"]
    assert entry["remaining_uses"] is None, "no cap means no countdown"

    uses = client.get(
        f"/api/v1/admin/quick_links/{link['link_id']}/uses", headers=bearer(HEAD_ADMIN)
    )
    assert uses.status_code == 200, uses.text
    body = uses.json()
    assert body["worker_name"] == "Seed Worker"
    assert [use["action"] for use in body["uses"]] == ["Clock Out", "Clock In"]
    for use in body["uses"]:
        assert use["site_name"] == "Downtown Tower A"
        assert use["lat"] is not None and use["lon"] is not None
        assert use["face_count"] == 1
        assert use.get("photo_path") is None, "the stored path is server-side plumbing"
        assert use["photo_url"] == f"/api/v1/admin/quick_link_photo/{use['id']}"
        assert use["log_id"], "every use names the attendance row it wrote"


def test_the_punch_photo_is_only_reachable_by_an_administrator(client):
    link = issue(client).json()
    punch(client, link["token"])
    use_id = db_scalar("SELECT id FROM quick_link_uses ORDER BY id DESC LIMIT 1")

    for headers in ({}, bearer(WORKER), bearer(MOALLEM)):
        assert_denied(
            client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=headers),
            endpoint="/admin/quick_link_photo",
            detail="a worker's face photo",
        )

    served = client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=bearer(HEAD_ADMIN))
    assert served.status_code == 200, served.text
    assert served.headers["content-type"] == "image/jpeg"
    assert len(served.content) > 0


def test_a_photo_row_that_points_elsewhere_is_not_served(client):
    """The route serves files this app wrote, not files a row happens to name."""
    link = issue(client).json()
    punch(client, link["token"])
    use_id = db_scalar("SELECT id FROM quick_link_uses ORDER BY id DESC LIMIT 1")

    import sqlite3

    import harness

    conn = sqlite3.connect(harness.DB_PATH)
    try:
        conn.execute(
            "UPDATE quick_link_uses SET photo_path = ? WHERE id = ?", ("../../times.db", use_id)
        )
        conn.commit()
    finally:
        conn.close()

    refused = client.get(f"/api/v1/admin/quick_link_photo/{use_id}", headers=bearer(HEAD_ADMIN))
    assert refused.status_code == 404


def test_the_first_use_notifies_the_administrator_once(client):
    link = issue(client).json()
    punch(client, link["token"])
    punch(client, link["token"])
    notices = db_rows(
        "SELECT kind, body FROM admin_notifications WHERE kind = 'quick_link' ORDER BY id"
    )
    assert len(notices) == 1, "the alert is for 'the link reached a phone', not for every tap"
    assert "Seed Worker" in notices[0][1]


def test_a_refusal_never_writes_a_use_row(client, face):
    link = issue(client).json()
    face.FACE_MODE = "none"
    punch(client, link["token"])
    face.FACE_MODE = "match"
    punch(client, link["token"], coordinates=OUTSIDE_ALL_SITES)
    assert db_scalar("SELECT COUNT(*) FROM quick_link_uses") == 0
    assert db_scalar("SELECT uses FROM quick_links WHERE id = ?", (link["link_id"],)) == 0


def test_a_standard_admin_may_issue_and_revoke(client):
    """The quick-link surface is for the people who run the site, not just the head admin."""
    link = issue(client, headers=bearer(ADMIN)).json()
    assert client.get("/api/v1/admin/quick_links", headers=bearer(ADMIN)).status_code == 200
    assert (
        client.post(
            f"/api/v1/admin/quick_links/{link['link_id']}/revoke", headers=bearer(ADMIN)
        ).status_code
        == 200
    )


def test_a_quick_link_clock_out_is_routed_by_the_shared_resolver(client):
    """The link path reaches the same overtime verdict as the punch path.

    It used to resolve ``overtime_notify_hours`` for itself and compare the *recorded*
    hours against it, so the same shift could be held for approval from a link and approved
    on the phone. The margins are seconds rather than milliseconds because the clock-in is
    planted just before the request, and the request's own clock is a little later - ten
    seconds either side of the line is far outside that skew and still "on the same second"
    as far as any shift of a working day is concerned.
    """
    rules = client.get("/api/v1/admin/shift_rules", headers=bearer(HEAD_ADMIN)).json()
    line = shift_hours.overtime_rule(rules)
    break_seconds = int(round(shift_hours.break_hours(rules) * shift_hours.SECONDS_PER_HOUR))
    at_the_line = line["threshold_seconds"] + break_seconds
    link = issue(client).json()

    where = "worker_id = ? AND action = 'Clock Out' ORDER BY id DESC LIMIT 1"
    seed_open_shift(WORKER, hours_ago=(at_the_line - 10) / 3600)
    assert punch(client, link["token"]).status_code == 200
    assert db_scalar(f"SELECT status_code FROM attendance_logs WHERE {where}", (WORKER,)) == "approved", (
        "a shift short of the line is an ordinary day"
    )
    assert db_scalar(f"SELECT overtime_hours FROM attendance_logs WHERE {where}", (WORKER,)) is None

    seed_open_shift(WORKER, hours_ago=at_the_line / 3600)
    assert punch(client, link["token"], confirm=True).status_code == 200
    assert db_scalar(f"SELECT status_code FROM attendance_logs WHERE {where}", (WORKER,)) == "pending_overtime"
    stored = db_scalar(f"SELECT overtime_hours FROM attendance_logs WHERE {where}", (WORKER,))
    expected = shift_hours.overtime_assessment(at_the_line, rules)["overtime_hours"]
    assert stored == pytest.approx(expected, abs=1e-3), (
        "the link path and the resolver must hold back the same hours"
    )

    # ... and the reason on the row is the shared sentence, not a second wording.
    reason = db_scalar(f"SELECT flag_reason FROM attendance_logs WHERE {where}", (WORKER,)) or ""
    assert "overtime line" in reason and "paid day" in reason, reason


def test_the_audit_log_names_the_link_and_the_worker(client):
    link = issue(client).json()
    clear_sessions()
    punch(client, link["token"])
    actions = [
        row[0]
        for row in db_rows(
            "SELECT action FROM audit_log WHERE action LIKE 'quick_link%' ORDER BY id"
        )
    ]
    assert actions == ["quick_link_create", "quick_link_clock_in"]

    detail = db_scalar(
        "SELECT after_json FROM audit_log WHERE action = 'quick_link_create' ORDER BY id LIMIT 1"
    )
    assert str(WORKER) in detail
    assert "expires_at" in detail
