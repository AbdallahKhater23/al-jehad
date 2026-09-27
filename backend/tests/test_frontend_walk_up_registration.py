"""The page the walk-up registration link opens.

WHY THIS EXISTS
---------------
``test_walk_up_registration`` covers the intake end to end on the server: the switch, the
validation order, the pending row, the queue cap, and the approval that is the only thing
which creates an account. What it cannot see is whether anybody can *reach* any of it. The
server half shipped without a page, so the queue could only be filled by hand - a
``curl`` - and an administrator's decision had nothing to decide.

This suite is about the page, and it is deliberately split the way the other two link pages
are:

* **source** - what the page and its flow must contain, checked without a browser: the
  assets it asks for, the ids and keys the shared module reaches for, the lockup, and every
  sentence present in all four language tables (a string that reaches three of them is a
  blank line for the reader of the fourth);
* **a browser** - the page boots, its own script runs, and the server's answer is painted.
  That is ``browser.PAGES``, so it runs in ``test_pages_boot_in_a_browser`` with the other
  three pages rather than here.

The two halves are here rather than scattered because the failure this page is most likely
to have is the one the link pages already had: a page whose script never ran, which looks
exactly like a slow connection.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = PROJECT_ROOT / "frontend"

PAGE = "register.html"
FLOW = "register.js"

#: The keys this page added to ``capture.js``, in the order they are written there. Named
#: rather than counted: a test that read them off the page would pass for a page whose
#: sentences had been deleted along with the tables.
REGISTER_KEYS = (
    "register.title",
    "register.headTitle",
    "register.sub",
    "register.intro",
    "register.name.placeholder",
    "register.password.placeholder",
    "register.password2.placeholder",
    "register.phone.placeholder",
    "register.email.placeholder",
    "register.work.placeholder",
    "register.role.label",
    "register.consent",
    "register.submit",
    "register.sending",
    "register.fallback",
    "register.done",
    "register.nameRequired",
    "register.passwordShort",
    "register.passwordMismatch",
    "register.closed",
    "register.roleNotAvailable",
    "register.consentRequired",
    "register.queueFull",
    "register.duplicate",
    "register.failed",
    "register.uploadFailed",
)

#: The roles the server publishes, and the words the page shows for them. The names are
#: words, so they are translated; the codes are what the form posts.
ROLE_KEYS = ("role.worker", "role.moallem", "role.off_office")


def page_body() -> str:
    return (FRONTEND / PAGE).read_text(encoding="utf-8")


def flow_body() -> str:
    return (FRONTEND / FLOW).read_text(encoding="utf-8")


def capture_body() -> str:
    return (FRONTEND / "capture.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The assets: this page is served at the root, and the two link pages are not
# ---------------------------------------------------------------------------
def test_the_page_asks_for_its_assets_as_siblings():
    """``/register`` is at the root; ``/enroll/<token>`` is one segment deep.

    The two link pages had to learn this the expensive way - a bare ``src="enroll.js"``
    resolved to ``/enroll/enroll.js``, the token route answered with the page itself as
    HTML, and the browser refused to execute it, so the page sat on its placeholders
    looking like a hung connection. This page has the opposite convention, and getting it
    wrong is the same class of bug in the other direction: ``../capture.js`` from
    ``/register`` resolves *up* from the root, and there is nothing above the root to
    serve it.
    """
    sources = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page_body())
    assert sources == ["api-config.js", "capture.js", FLOW], (
        f"{PAGE} loads {sources}. Served at the root, its own files are its siblings: no "
        f"``../``, and the shared capture module before the page's own flow"
    )
    for src in sources:
        assert not src.startswith(("../", "/")), (
            f'{PAGE} asks for "{src}". This page is not served under a token, so a relative '
            "path is resolved from the root and a leading ``../`` walks off it"
        )


def test_the_page_carries_no_inline_script_and_no_inline_handler():
    """Same rule as the other pages: every script is a file, and every control is bound.

    An inline ``<script>`` cannot be told apart from an injected one, and the console's
    inline-handler budget is pinned per file and may only fall - so this page binds its
    buttons the way the other two do, by id, and adds nothing to that allowance.
    """
    body = page_body()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", body, re.IGNORECASE), (
        "the page carries an inline script block"
    )
    handlers = re.findall(r"\son[a-z]+\s*=", body)
    assert handlers == [], f"the page carries inline event handlers: {handlers}"


def test_the_page_defines_the_show_hide_contract_it_uses():
    """``capture.js`` shows and hides by the ``hidden`` class, which is each page's own CSS."""
    assert ".hidden" in page_body(), (
        "the shared module hides the camera, the preview and the fallback label with the "
        "``hidden`` class, and this page has to define it"
    )


def test_the_page_draws_the_lockup_the_console_draws():
    """Four lines, in the artwork's order, and not handed to the translator.

    A wordmark, a legal suffix and a founding year are proper nouns: a translated wordmark is
    a different logo. Read off ``BRAND_DEFAULTS`` rather than written here, so renaming the
    company in the console fails here instead of shipping a page whose mark disagrees.
    """
    source = (FRONTEND / "frontendjavascript.js").read_text(encoding="utf-8")
    block = re.search(r"const BRAND_DEFAULTS = \{(.*?)\n\};", source, re.S)
    assert block, "BRAND_DEFAULTS has been renamed or moved; this test reads the lockup from it"
    shipped = [
        re.search(rf"\b{key}: '([^']*)'", block.group(1)).group(1)
        for key in ("name", "legal", "est", "tagline")
    ]
    body = page_body()
    offsets = [body.find(f">{line}<") for line in shipped]
    assert -1 not in offsets, (
        f"{PAGE} does not draw all of {shipped}. The artwork sets four lines; a page that "
        "draws three of them is a different lockup"
    )
    assert offsets == sorted(offsets), f"{PAGE} draws the lockup out of order: {offsets}"
    assert not re.findall(r'class="brand-(?:name|legal|est|tagline)"[^>]*\bdata-t', body), (
        "the lockup is handed to the translator"
    )


def test_the_page_has_every_element_the_shared_module_reaches_for():
    """A missing element throws while the page wires its buttons, so the whole page stops.

    The id list is read off ``capture.js``'s own source, so an id added there cannot be
    forgotten here - which is exactly how the enrollment page lost its file input once.
    """
    module = capture_body()
    ids = sorted(set(re.findall(r'getElementById\("([\w-]+)"\)', module)))
    assert len(ids) >= 6, f"capture.js addresses only {ids}"
    #: The punch page's own line, and the only id the module itself treats as optional.
    PUNCH_ONLY = {"location-line"}
    body = page_body()
    missing = [name for name in ids if name not in PUNCH_ONLY and f'id="{name}"' not in body]
    assert missing == [], (
        f"{PAGE} is missing {missing}: capture.js addresses it by id, so the page would stop "
        "at its first binding instead of rendering"
    )


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------
def test_the_form_posts_the_fields_the_intake_takes():
    """Every field the endpoint declares, and nothing invented.

    A missing one is a 422 the applicant cannot read; the photograph is the request, so it
    goes last and carries a filename rather than a form field.
    """
    flow = flow_body()
    expected = (
        "full_name",
        "password",
        "role",
        "phone",
        "email",
        "work_details",
        "consent",
    )
    for field in expected:
        assert f'form.append("{field}"' in flow, (
            f"the form never sends {field!r}, which ``POST /register`` takes"
        )
    assert 'form.append("photo"' in flow, "the photograph is the request"


def test_the_page_never_promises_an_account_id():
    """The id is minted at approval, so there is no field for it and no control for it.

    This is the one thing the applicant must not be told: a number said at submission is a
    promise the next approval can take back, which is why ``registrations`` allocates it
    inside the transaction that creates the account.
    """
    body = page_body()
    flow = flow_body()
    assert 'id="id"' not in body and 'id="account-id"' not in body
    assert "assigned_id" not in flow and 'form.append("user_id"' not in flow, (
        "the page sends or shows an account id; the server picks it at approval"
    )


def test_the_page_reads_the_policy_before_it_asks_for_anything():
    """The roles, the password floor and the photo policy are the server's, not this page's.

    A page that offered a role the server refuses, or a password shorter than the policy,
    would be a form that fails after the photograph has been uploaded over a phone tether.
    """
    flow = flow_body()
    assert 'fetch(API + "/register")' in flow, "the page never reads the policy"
    assert "min_password_length" in flow, "the password floor it enforces is not the server's"
    assert 'Capture.t("role." + roles[i])' in flow, (
        "the role names are not read from the server's list, so an offered role could be one "
        "the intake refuses"
    )
    assert "Capture.setPolicy(res.body.photo_policy)" in flow, (
        "the photo policy the browser refuses on is not the server's"
    )


def test_a_closed_intake_says_so_instead_of_looking_broken():
    """Off is the default and the link is permanent, so the page has to be able to say it."""
    flow = flow_body()
    assert 'sayKey("register.closed", "err")' in flow
    assert "res.body.enabled" in flow, "the page never reads the switch it has to obey"
    # And the switch's own refusal, for a submission that raced the switch being turned off.
    assert "registration_closed" in capture_body(), (
        "the intake's ``registration_closed`` code is not mapped to a sentence, so a refusal "
        "would reach the applicant in English prose"
    )


@pytest.mark.parametrize("key", REGISTER_KEYS + ROLE_KEYS)
def test_every_sentence_is_in_all_four_language_tables(key: str):
    """A string in three of the four is a blank line for the reader of the fourth.

    The count of tables is read off the module's own language list rather than written here,
    so a language added to the picker is covered without editing this test.
    """
    module = capture_body()
    languages = sorted(set(re.findall(r'code: "([a-z]{2})"', module)))
    assert len(languages) >= 4, f"capture.js offers only {languages}"
    # Counted as a *table entry* (``"key":``), not as the bare string: the refusal keys are
    # also values in the error-code map, which is not a translation.
    found = module.count(f'"{key}":')
    assert found == len(languages), (
        f"{key} is in {found} of the {len(languages)} language tables ({languages}) in "
        "capture.js. The applicant reads this page in whichever of them they chose"
    )
