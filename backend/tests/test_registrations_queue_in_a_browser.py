"""The registrations queue, in a browser: the badge, the face, and the two answers.

WHY THIS EXISTS
---------------
``test_frontend_registrations_queue`` proves the markup, the request paths and the fetch
against a stub DOM - one half of each step. What it cannot prove is the half that only
exists when a browser runs the screen, and each of these is a way for the tab to look
finished and do nothing:

1. **the badge is on the tab before the tab is opened.** The count is read on sign-in and
   written onto the nav button - on the rail, on the phone strip, or on neither. A number
   that only appears because somebody opened the queue is not a badge. (The stub DOM's
   ``#adminContent`` has no ``querySelectorAll``, so a suite there can only watch the paint
   half; the read half is what this one catches.)
2. **the controls are live.** Both answers, and the button that fetches the photograph, are
   bound with delegated listeners on the pane - the document policy has no room for new
   inline handlers. A typo in the selector, or a listener attached to a pane that has already
   been replaced, is invisible to every VM suite in this repo.
3. **the face decodes.** It is fetched with the session credential and handed to the page as
   an object URL (an ``<img src>`` cannot carry a token), then revealed by removing a class: an
   object URL revoked too early, or a hidden class never removed, leaves a working-looking card
   with no face on it. The size the browser decodes is asserted against the size this test
   submitted.
4. **the account a decision was about is the account that exists.** The number is read off the
   screen and then looked up in the database, so "the console said 12" and "12 is a user row"
   have to agree - and a refusal's reason has to outlive the account it deleted.

WHAT IS ASSERTED
----------------
Two applications are submitted through the **real intake** (the public multipart endpoint,
which is what writes the account, files the face and holds it for approval), and one browser
session signs in as the administrator: the badge already reads two, one application's face is
fetched and decoded at the size that was stored, one application is refused with a reason and
one is approved with a note. Neither decision is written by the test.

Runs with the browser the machine has - see ``browser.py``. With none installed, it skips and
names ``python -m playwright install chromium``.
"""

from __future__ import annotations

import io
import json
import random

import pytest
from PIL import Image

import browser as browser_support
import harness
from config import settings

pytestmark = pytest.mark.regression

#: The size of the photograph this test submits, deliberately not square so a rescaled or
#: substituted image is told apart from the stored one.
PHOTO_SIZE = (200, 140)

#: The same policy every other password obeys, spelled out rather than guessed at.
STRONG_PASSWORD = "site-attendance-2026"

CONSOLE_LANDED = (
    "document.getElementById('adminContent') !== null"
    " && document.getElementById('adminContent').children.length > 0"
)

ADMIN_SIGN_IN = browser_support.Identity(
    role="administrator",
    user_id=harness.ADMIN,
    email=harness.EMAILS[harness.ADMIN],
    password=harness.PASSWORDS[harness.ADMIN],
    landed=CONSOLE_LANDED,
    why="the Registrations tab is drawn into #adminContent, which only exists once the console is up",
)


def _photo(seed: int = 0) -> bytes:
    """A valid JPEG whose *bytes* differ per seed.

    Seeded noise rather than a flat colour, and that is load-bearing rather than fussy: a
    uniformly flat image quantises a shade apart to the *same* coefficients, so two "different"
    flat colours come out byte-identical - and two applications that are the same photograph are
    two applications nothing else can tell apart, which would make "the first card" and "the
    second card" a question about the queue's order alone.
    """
    rng = random.Random(seed)
    image = Image.new("RGB", PHOTO_SIZE)
    image.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(PHOTO_SIZE[0] * PHOTO_SIZE[1])]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def two_applications(app_module, client, monkeypatch):
    """Two applications in the queue, submitted through the endpoint that takes them.

    Through the public intake rather than a hand-written pair of rows: the account, the id, the
    face reference and the ``pending_approval`` status are the intake's own work, and rows
    inserted by the test would prove only that the console can read rows the test invented.

    The suite's autouse reset has already run by the time this fixture body executes, so what
    is written here is what the browser finds.
    """
    monkeypatch.setattr(settings, "registration_enabled", True)
    # The link's own address, off the console's own read - what the browser would have been
    # sent, rather than a path assembled here: the token is the whole reachability of the form
    # now, so a fixture that guessed at one would be testing a URL nobody has.
    link = client.get(
        "/api/v1/admin/registrations/link", headers=harness.bearer(harness.HEAD_ADMIN)
    )
    assert link.status_code == 200, link.text[:400]
    public = "/api/v1/register/" + link.json()["url"].rsplit("/register/", 1)[1]
    ids = []
    for index, (name, role) in enumerate((("Nadia Saleh", "worker"), ("Omar Farouk", "moallem"))):
        submitted = client.post(
            public,
            data={
                "full_name": name,
                "password": STRONG_PASSWORD,
                "role": role,
                "consent": "true",
                "phone": "+965 555 010%d" % index,
                "work_details": "Six years on tower sites.",
            },
            files={"photo": ("selfie.jpg", _photo(index), "image/jpeg")},
        )
        assert submitted.status_code == 200, submitted.text[:400]
        ids.append(submitted.json()["user_id"])
    assert len(set(ids)) == 2, "the two submissions became one account"
    return ids


#: Work the queue the way an administrator does. ``%%A%%`` and ``%%B%%`` are replaced by the two
#: request ids the fixture submitted, in queue order.
#:
#: Every step is a poll rather than a sleep, and every step reports itself: a card that never
#: drew reads as ``first_card: false`` rather than as "the test was slow". ``complete`` and
#: ``naturalWidth > 0`` are both required for the photograph - ``complete`` alone is true for
#: an image that failed to decode.
#:
#: The badge is read **first**, before the tab is clicked: that is the question the badge exists
#: to answer.
WORK_THE_QUEUE = r"""
(async () => {
    const out = {};
    const waitFor = async (test, tries = 80) => {
        for (let i = 0; i < tries; i += 1) {
            const value = test();
            if (value) return value;
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return null;
    };
    const text = (node) => (node ? String(node.innerText || '').replace(/\s+/g, ' ').trim() : '');
    const badgeNow = () => document.querySelector('[data-admin-tab="Registrations"] [data-registrations-badge]');

    // The tab, and the count on it - before anybody opens the queue.
    const tab = await waitFor(() => document.querySelector('[data-admin-tab="Registrations"]'));
    out.tab = tab !== null;
    const badgeBefore = await waitFor(badgeNow);
    out.badge_before = badgeBefore ? text(badgeBefore) : null;
    out.badge_before_label = tab ? String(tab.getAttribute('aria-label') || '') : '';
    if (tab) tab.click();

    // The queue itself: the first application's card.
    const first = await waitFor(() => document.querySelector('[data-registration="%%A%%"]'));
    out.first_card = first !== null;
    out.cards = document.querySelectorAll('[data-registration]').length;
    out.count_line = text(document.querySelector('[data-registrations-count]'));

    // The photograph: hidden until it is asked for, then decoded.
    const imageId = 'registrationPhoto%%A%%';
    const before = document.getElementById(imageId);
    out.image_present = before !== null;
    out.hidden_before = before ? before.classList.contains('hidden') : null;
    const photoButton = first ? first.querySelector('[data-registration-photo]') : null;
    out.photo_button = photoButton !== null;
    if (photoButton) photoButton.click();
    out.rendered = await waitFor(() => {
        const img = document.getElementById(imageId);
        return img && !img.classList.contains('hidden') && img.complete && img.naturalWidth > 0;
    });
    const shot = document.getElementById(imageId);
    out.natural_width = shot ? shot.naturalWidth : 0;
    out.natural_height = shot ? shot.naturalHeight : 0;
    out.object_url = shot ? String(shot.src).indexOf('blob:') === 0 : false;
    out.photo_button_after = first ? first.querySelector('[data-registration-photo]') !== null : null;

    // Refuse the second one, with the reason the record keeps.
    const second = await waitFor(() => document.querySelector('[data-registration="%%B%%"]'));
    out.second_card = second !== null;
    const reason = document.getElementById('registrationNote-%%B%%');
    out.reason_field = reason !== null;
    if (reason) reason.value = 'No formwork experience';
    const refuse = second ? second.querySelector('[data-registration-reject]') : null;
    out.refuse_button = refuse !== null;
    if (refuse) refuse.click();
    const refused = await waitFor(() => document.querySelector('[data-registration-refused="%%B%%"]'));
    out.refused_receipt = text(refused);
    out.second_gone = (await waitFor(() => document.querySelector('[data-registration="%%B%%"]') === null)) !== null;

    // Approve the first one, with a note.
    const note = document.getElementById('registrationNote-%%A%%');
    out.note_field = note !== null;
    if (note) note.value = 'ID checked at the gate.';
    const card = document.querySelector('[data-registration="%%A%%"]');
    const approve = card ? card.querySelector('[data-registration-approve]') : null;
    out.approve_button = approve !== null;
    if (approve) approve.click();
    const minted = await waitFor(() => document.querySelector('[data-registration-minted]'));
    out.minted = minted ? String(minted.getAttribute('data-registration-minted') || '') : null;
    out.minted_text = text(minted);
    out.first_gone = (await waitFor(() => document.querySelector('[data-registration="%%A%%"]') === null)) !== null;
    // Nothing is waiting any more, so the numeral goes with the queue.
    out.badge_cleared = (await waitFor(() => badgeNow() === null)) !== null;
    out.toast = Array.from(document.querySelectorAll('.toast')).map(text);
    return out;
})()
"""


def test_the_console_works_the_registration_queue(
    browser, site, app_module, tmp_path, two_applications
):
    """One browser session: the badge, the face, a refusal with a reason, and a hire."""
    first, second = two_applications
    probe = {"queue": WORK_THE_QUEUE.replace("%%A%%", str(first)).replace("%%B%%", str(second))}
    seen = browser_support.sign_in(
        browser,
        site,
        ADMIN_SIGN_IN,
        browser_support.DESKTOP,
        until=CONSOLE_LANDED,
        probe=probe,
        shots=tmp_path,
    )
    assert seen.ran, (
        f"signing in as the administrator did not reach the console.\n{seen.describe()}\n"
        f"A screenshot of what the browser was showing is in {tmp_path}"
    )
    assert seen.refused_scripts == [], (
        f"the console asked for a script and was given something a browser will not "
        f"execute.\n{seen.describe()}"
    )
    assert seen.page_errors == [], f"the console threw while drawing itself.\n{seen.describe()}"

    queue = seen.probes["queue"]
    assert queue["tab"], (
        f"the Registrations tab is not in the console, so there is no queue to work.\n"
        f"{seen.describe()}"
    )
    # The count on the tab, read before the queue was opened: two applications are waiting.
    assert queue["badge_before"] == "2", (
        f"the tab's badge reads {queue['badge_before']!r} with two applications pending and "
        f"nobody having opened the queue - so the count never reached the nav, and an "
        f"administrator who is not already on that tab is never told there is work.\n"
        f"{seen.describe()}"
    )
    assert "2" in queue["badge_before_label"], (
        f"the badge is not announced either: {queue['badge_before_label']!r}\n{seen.describe()}"
    )
    assert queue["first_card"] and queue["second_card"], (
        f"the queue did not draw both applications (cards={queue['cards']}).\n{seen.describe()}"
    )
    assert "2 waiting on a decision" in queue["count_line"], queue["count_line"]

    # The photograph: hidden until it is asked for, then decoded at the stored size.
    assert queue["image_present"], f"the card ships no photograph element.\n{seen.describe()}"
    assert queue["hidden_before"] is True, (
        f"an applicant's face was visible before anybody asked for it.\n{seen.describe()}"
    )
    assert queue["photo_button"], (
        f"the card offers no way to see the photograph, so the delegated "
        f"[data-registration-photo] binding is not attached.\n{seen.describe()}"
    )
    assert queue["rendered"], (
        f"tapping Show the photo did not put the stored picture on the card "
        f"(width={queue['natural_width']}, height={queue['natural_height']}, "
        f"toast={queue['toast']}).\n{seen.describe()}"
    )
    assert (queue["natural_width"], queue["natural_height"]) == PHOTO_SIZE, (
        f"the card rendered a {queue['natural_width']}x{queue['natural_height']} image, not the "
        f"{PHOTO_SIZE[0]}x{PHOTO_SIZE[1]} photograph this test submitted - so the bytes on the "
        f"card are not the applicant's.\n{seen.describe()}"
    )
    assert queue["object_url"], (
        f"the image loads without the session credential, so the face is not being fetched the "
        f"way the console fetches it.\n{seen.describe()}"
    )
    assert queue["photo_button_after"] is False, (
        f"the button that fetched the photograph is still there.\n{seen.describe()}"
    )

    # The refusal: a reason was required, it was recorded, and the row left the queue.
    assert queue["second_card"] and queue["reason_field"], seen.describe()
    assert queue["refuse_button"], (
        f"the card has no Refuse control, so the delegated [data-registration-reject] binding "
        f"is not attached.\n{seen.describe()}"
    )
    assert queue["second_gone"], (
        f"the refused application is still on the queue screen.\n{seen.describe()}"
    )
    assert "refused" in queue["refused_receipt"].lower(), queue["refused_receipt"]
    # The refusal deleted the account and wiped its face, and the reason outlived both of them
    # in the audit trail - which is the only copy a deleted row could not be.
    with app_module.db() as conn:
        assert conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (second,)
        ).fetchone() is None, "the browser refused an account and it is still on the roster"
        audit = conn.execute(
            "SELECT after_json FROM audit_log WHERE action = 'registration_rejected_purged' "
            "AND entity_id = ?",
            (str(second),),
        ).fetchone()
    assert audit is not None, "the browser's refusal left no record behind"
    assert json.loads(audit["after_json"])["note"] == "No formwork experience", (
        f"the reason typed into the card is not the reason on the record: "
        f"{audit['after_json']!r}"
    )
    assert not harness.template_exists(second), (
        "the refused applicant's face is still on disk - a refusal destroys the reference"
    )

    # The approval: the id on the screen is the account that exists.
    assert queue["note_field"] and queue["approve_button"], (
        f"the card has no Approve control or no note field, so the delegated "
        f"[data-registration-approve] binding is not attached.\n{seen.describe()}"
    )
    assert queue["minted"], (
        f"approving did not put the minted id on the screen.\n{seen.describe()}"
    )
    assert queue["first_gone"], "the approved application is still on the queue screen"
    with app_module.db() as conn:
        created = conn.execute(
            "SELECT id, name, role, status FROM users WHERE id = ?", (queue["minted"],)
        ).fetchone()
    assert created is not None, (
        f"the console said account {queue['minted']} was created and no such account exists: "
        f"{queue['minted_text']!r}"
    )
    assert created["name"] == "Nadia Saleh" and created["role"] == "worker", dict(created)
    assert queue["badge_cleared"], (
        f"the tab still wears a count after both applications were decided.\n{seen.describe()}"
    )
