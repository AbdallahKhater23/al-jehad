"""The evidence on a pending review, in a browser: the frame really is on the card.

WHY THIS EXISTS
---------------
A review card is a claim about a worker's face - matched at 0.42, refused, a liveness verdict -
and the frame is the evidence the claim is about. The console deliberately does **not** put the
picture in an ``<img src>`` (see ``approvalsEvidenceHtml``): the frame is a worker's face served
to administrators only, so the bytes are fetched with the session credential, turned into an
object URL and *then* written onto an ``<img>`` the card ships hidden. That is three separate
things that can each leave the card looking right and empty:

1. the row never carried a ``frame_url`` (``_pending_review_row`` popped ``punch_frame`` and
   found none), so there is no control to tap at all;
2. the control is drawn and dead - the delegated ``[data-show-frame]`` listener never attached,
   which is exactly what a repaint between paint and tap would do; and
3. the fetch succeeds but the image does not render: the object URL is revoked too early, the
   ``hidden`` class is never removed, or the response was an error page the blob still wrapped.

``frontend_vm`` proves the markup, the URL and the fetch - one half of each step, with a stub
DOM that has no ``<img>`` and no decoder. None of the three failures above is visible to it.
This is the only place a browser decodes the bytes and says how big the picture is.

WHAT IS ASSERTED
----------------
Against the live server and the seeded database, one flagged punch is given a frame written by
``punch_frames.store_frame`` - the application's own writer, so the file is exactly what a real
punch would store - and the console is driven to it: the Approvals tab, the card for that log
id, the button that fetches the frame, and then the image itself, which has to be visible and
have decoded to the stored dimensions. The dimensions are the part that cannot be faked by a
placeholder: they are asserted to equal the frame written for this test, so a card rendering
*some other* picture would fail too.

Runs with the browser the machine has - see ``browser.py``. With none installed, it skips and
names ``python -m playwright install chromium``.
"""

from __future__ import annotations

import pytest

import browser as browser_support
import harness

pytestmark = pytest.mark.regression


#: The size of the frame this test writes, deliberately not square so the width and the height
#: are told apart in the assertion, and small enough that ``store_frame``'s thumbnail leaves it
#: exactly as written - so ``naturalWidth``/``naturalHeight`` are the stored frame's own, not a
#: rescaled guess.
EVIDENCE_SIZE = (180, 120)

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
    why="the Approvals tab is drawn into #adminContent, which only exists once the console is up",
)


@pytest.fixture
def a_pending_review_with_its_frame(app_module, monkeypatch, tmp_path):
    """The seeded flagged punch, given evidence, written the way a punch writes one.

    The frame goes through ``punch_frames.store_frame`` rather than a hand-written file, so
    what the route serves is what a real punch would have left on disk - same directory
    convention, same re-encode. ``FRAMES_DIR`` is read at call time (``frames_dir()``), so
    repointing the module attribute is enough to keep the face out of the repository, the same
    trick ``test_retention`` uses.

    The row update runs after the suite's autouse reset (autouse fixtures execute first within
    their scope), so the ``punch_frame`` column it sets survives into the test body.
    """
    import punch_frames
    from PIL import Image

    directory = tmp_path / "punch_frames"
    directory.mkdir()
    monkeypatch.setattr(punch_frames, "FRAMES_DIR", str(directory))

    name = punch_frames.store_frame(Image.new("RGB", EVIDENCE_SIZE, (37, 52, 78)))
    with app_module.db(write=True) as conn:
        conn.execute(
            "UPDATE attendance_logs SET punch_frame = ? WHERE id = ?",
            (name, harness.SEEDED_PENDING_LOG_ID),
        )
    return name


#: Open the Approvals tab and make the evidence image render. ``SEEDED_ID`` is substituted by
#: the test with the log id the fixture wrote the frame for.
#:
#: The waits are ``waitFor`` polls rather than sleeps, and every step reports itself: a card
#: that never drew reads as ``card: false``, not as "the test was slow". ``complete`` and
#: ``naturalWidth > 0`` are both required - ``complete`` alone is true for an image that
#: failed to decode.
OPEN_THE_EVIDENCE = r"""
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

    // The Approvals tab, tapped the way an administrator taps it.
    const tab = await waitFor(() => document.querySelector('[data-admin-tab="Approvals"]'));
    out.tab = tab !== null;
    if (tab) tab.click();

    // The card for the flagged shift, and the control that fetches its frame.
    const id = 'SEEDED_ID';
    const card = await waitFor(() => document.querySelector('[data-review="' + id + '"]'));
    out.card = card !== null;
    const button = card ? card.querySelector('[data-show-frame]') : null;
    out.button = button !== null;
    const imageId = 'reviewFrame' + id;
    const before = document.getElementById(imageId);
    out.image_present = before !== null;
    out.hidden_before = before ? before.classList.contains('hidden') : null;

    if (button) button.click();

    out.rendered = await waitFor(() => {
        const img = document.getElementById(imageId);
        return img && !img.classList.contains('hidden') && img.complete && img.naturalWidth > 0;
    });
    const after = document.getElementById(imageId);
    out.hidden_after = after ? after.classList.contains('hidden') : null;
    out.natural_width = after ? after.naturalWidth : 0;
    out.natural_height = after ? after.naturalHeight : 0;
    // The bytes were fetched with the session credential and handed to the page as an object
    // URL; a plain ``src`` would be the unauthenticated route the console avoids on purpose.
    out.object_url = after ? String(after.src).indexOf('blob:') === 0 : false;
    // Success consumes the control - a button still offered after the picture arrived would
    // mean the fetch never happened and something else revealed the image.
    out.button_after = card ? card.querySelector('[data-show-frame]') !== null : null;
    out.toast = Array.from(document.querySelectorAll('.toast')).map((el) => el.innerText);
    return out;
})()
""".replace("SEEDED_ID", str(harness.SEEDED_PENDING_LOG_ID))


def test_a_pending_reviews_stored_frame_renders_on_its_card(
    browser, site, tmp_path, a_pending_review_with_its_frame
):
    """The evidence image decodes to the stored frame, on the card a reviewer decides from.

    The frame is written by the application's own store, so this is the whole chain in the one
    place it can be seen end to end: the row carries a frame, the card offers the control, the
    tap fetches the bytes with the admin's token, and the browser decodes them into a visible
    image of the size that was stored.
    """
    seen = browser_support.sign_in(
        browser,
        site,
        ADMIN_SIGN_IN,
        browser_support.DESKTOP,
        until=CONSOLE_LANDED,
        probe={"evidence": OPEN_THE_EVIDENCE},
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

    evidence = seen.probes["evidence"]
    assert evidence["tab"], (
        f"the Approvals tab is not in the console, so there is no screen to review on.\n"
        f"{seen.describe()}"
    )
    assert evidence["card"], (
        f"the seeded flagged punch ({harness.SEEDED_PENDING_LOG_ID}) drew no review card, "
        f"so the frame has nowhere to be shown.\n{seen.describe()}"
    )
    assert evidence["button"], (
        f"the review card offers no way to see its evidence: the row carried no frame_url, "
        f"even though a frame is on disk for it.\n{seen.describe()}"
    )
    assert evidence["image_present"], (
        f"the card has no evidence image element at all.\n{seen.describe()}"
    )
    assert evidence["hidden_before"] is True, (
        f"the evidence image was visible before anything was fetched - it is a worker's face "
        f"and must start hidden.\n{seen.describe()}"
    )

    assert evidence["rendered"], (
        f"tapping Show frame did not put the stored picture on the card (hidden_after="
        f"{evidence['hidden_after']!r}, width={evidence['natural_width']}, "
        f"height={evidence['natural_height']}, toast={evidence['toast']}).\n{seen.describe()}"
    )
    assert evidence["hidden_after"] is False, (
        f"the image was left in the card's hidden state.\n{seen.describe()}"
    )
    assert (evidence["natural_width"], evidence["natural_height"]) == EVIDENCE_SIZE, (
        f"the card rendered a {evidence['natural_width']}x{evidence['natural_height']} image, "
        f"not the {EVIDENCE_SIZE[0]}x{EVIDENCE_SIZE[1]} frame stored for this punch - so the "
        f"bytes on the card are not the evidence they claim to be.\n{seen.describe()}"
    )
    assert evidence["object_url"], (
        f"the image loads without the session credential, so the evidence is not being "
        f"fetched the way the console fetches it.\n{seen.describe()}"
    )
    assert evidence["button_after"] is False, (
        f"the Show frame button is still there after the picture arrived.\n{seen.describe()}"
    )
