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

import names as names_module

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
    "register.name.hint",
    "register.password.placeholder",
    "register.password2.placeholder",
    "register.phone.placeholder",
    "register.email.placeholder",
    "register.work.placeholder",
    "register.role.label",
    "register.moallem.label",
    "register.moallem.none",
    "register.moallem.empty",
    "register.moallem.offline",
    "register.consent",
    "register.submit",
    "register.sending",
    "register.fallback",
    "register.done",
    "register.doneTitle",
    "register.doneId",
    "register.doneHint",
    "register.doneLogin",
    "register.nameRequired",
    "register.passwordShort",
    "register.passwordMismatch",
    "register.closed",
    "register.roleNotAvailable",
    "register.consentRequired",
    "register.queueFull",
    "register.conflict",
    "register.photoUnreadable",
    "register.failed",
    "register.uploadFailed",
    "register.needLink",
    "register.linkInvalid",
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
def test_the_page_asks_for_its_assets_one_level_up():
    """``/register/<token>`` is one segment deep, so its files are one level up.

    This page used to be a permanent address at the root, and its assets were therefore its
    siblings. It is served under the link's own token now, which puts the document's base at
    ``/register/``: a bare ``src="capture.js"`` resolves to ``/register/capture.js``, which is
    not a file, and the token route answers the request with the page itself as HTML - the
    browser refuses to execute it and the page sits on its placeholders looking like a hung
    connection. That is the failure the two link pages already had; this pins it here rather
    than rediscovering it.
    """
    sources = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page_body())
    assert sources == ["../api-config.js", "../capture.js", f"../{FLOW}"], (
        f"{PAGE} loads {sources}. Served under a token, its own files are one level up, and "
        "the shared capture module comes before the page's own flow"
    )
    for src in sources:
        assert src.startswith("../"), (
            f'{PAGE} asks for "{src}": a name that is not one level up resolves into the '
            "token's own segment, where the server answers with this page instead of a script"
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

    A missing one is a 422 the applicant cannot read; the photograph is the account's face, so
    it goes last and carries a filename rather than a form field.
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


def test_the_receipt_puts_the_id_the_server_answers_with_on_the_screen():
    """The id is the point of the new shape, and this is the only place it is ever said.

    A submission creates the account and answers with its id, so the page has to put that number
    in front of the person who has to keep it - in the largest type on the page, with the one
    thing left to do beside it (go and sign in), and with the caveat that will refuse them at a
    gate until an administrator approves them.

    What it must **not** do is invent one: nothing here sends an account id, and the only id ever
    shown is ``res.body.user_id``.
    """
    body = page_body()
    flow = flow_body()
    assert 'id="done-id"' in body and 'id="done"' in body, (
        "the page has nowhere to show the id it is handed"
    )
    assert 'id="btn-login"' in body and 'href="/"' in body, (
        "the receipt has no way to the sign-in screen"
    )
    for key in ("register.doneTitle", "register.doneId", "register.doneHint", "register.doneLogin"):
        assert f'data-t="{key}"' in body, f"the receipt never says {key}"
    assert "answer.user_id" in flow, "the receipt is not filled from the server's answer"
    assert 'form.append("user_id"' not in flow and "assigned_id" not in flow, (
        "the page sends an account id; the server is what assigns it"
    )


def test_the_page_reads_the_policy_before_it_asks_for_anything():
    """The roles, the password floor and the photo policy are the server's, not this page's.

    A page that offered a role the server refuses, or a password shorter than the policy,
    would be a form that fails after the photograph has been uploaded over a phone tether.
    """
    flow = flow_body()
    assert "fetch(ENDPOINT)" in flow, "the page never reads the policy"
    assert "min_password_length" in flow, "the password floor it enforces is not the server's"
    assert 'Capture.t("role." + roles[i])' in flow, (
        "the role names are not read from the server's list, so an offered role could be one "
        "the intake refuses"
    )
    assert "Capture.setPolicy(res.body.photo_policy)" in flow, (
        "the photo policy the browser refuses on is not the server's"
    )


def test_the_page_is_a_form_only_when_it_carries_the_link():
    """The token is read out of the page's own path, and a page without one is a sentence.

    This is the half of "no public form" the server cannot enforce: the route at the bare path
    is refused, but a page that still drew the form there would show an applicant a form that
    could never be submitted - and it would have to *ask* the server for its policy to draw it,
    which is a request nobody holding a cut-off link should be making. So the flow reads its own
    path first, and with no token it paints one sentence and stops.
    """
    flow = flow_body()
    assert "location.pathname" in flow, "the flow never reads the link's token out of its path"
    assert 'var ENDPOINT = API + "/register/" + encodeURIComponent(TOKEN)' in flow, (
        "the calls do not carry the token, so they would hit the bare path and be refused"
    )
    assert flow.count("fetch(ENDPOINT") == 2, (
        "both calls - the policy read and the submission - have to go to the link's own address"
    )
    # The moallem list is the third read from the same token, and it is derived from the same
    # constant rather than written out again - a second literal would be a second address to
    # keep in step with the route.
    assert 'var MOALLEMS = ENDPOINT + "/moallems"' in flow, (
        "the moallem list is fetched from somewhere other than this link's own address"
    )
    assert "fetch(MOALLEMS)" in flow, "nothing ever asks the server which moallems exist"
    assert 'sayKey("register.needLink", "err")' in flow, (
        "a page with no token does not say so, so it would look like a broken link"
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


# ---------------------------------------------------------------------------
# The moallem a worker names while applying
# ---------------------------------------------------------------------------
def test_a_worker_picks_a_moallem_and_no_other_role_is_asked_for_one():
    """The dropdown is the server's list, and it belongs to the worker role alone.

    Three decisions live here. The list is the *server's* - the same page cannot read accounts
    without a session, so an option that could be refused is never drawn. The field is shown for
    ``worker`` and put away for every other role, because a moallem is not assigned to another
    moallem and the server refuses that outright. And the choice is *optional*: the first option
    is always no moallem, so a site that has registered none can still take applications.
    """
    body = page_body()
    flow = flow_body()
    for element in ('id="moallem-field"', 'id="moallem"', 'id="moallem-note"'):
        assert element in body, f"the page has nowhere to put the moallem control ({element})"
    assert 'data-t="register.moallem.label"' in body, "the dropdown has no label in any language"
    assert '$("role").value !== "worker"' in flow, (
        "the field is not gated on the role, so a moallem would be offered a moallem"
    )
    assert '$("moallem").value = ""' in flow, (
        "a moallem chosen under the worker role survives a role change"
    )
    assert '$("role").addEventListener("change", syncMoallem)' in flow, (
        "nothing re-reads the role when the applicant changes it"
    )
    assert 'Capture.t("register.moallem.none")' in flow, (
        "the list has no \"no moallem\" option, so the assignment is not optional"
    )
    assert "names[Capture.lang]" in flow and "return entry.name || entry.id" in flow, (
        "a moallem is not named in the reader's language, and nothing stands in when the "
        "translation that matches is missing"
    )
    assert 'moallemNote = moallems.length ? "" : "register.moallem.empty"' in flow, (
        "an empty list is drawn as an empty dropdown with no explanation"
    )
    assert 'moallemNote = "register.moallem.offline"' in flow, (
        "a failed read is not told apart from a site that has registered no moallem"
    )


def test_the_form_carries_the_moallem_the_worker_chose():
    """``moallem_id`` goes with every submission; empty means nobody, which the server accepts."""
    flow = flow_body()
    assert 'form.append("moallem_id"' in flow, (
        "the choice never reaches the server, so every applicant is unassigned"
    )


def test_the_name_is_filed_under_the_language_it_was_written_in():
    """A name typed in Arabic must not be stored as the English one.

    The picker can be switched after the box is filled, and the box keeps its text - so the
    language is stamped as the applicant types rather than read at submit, which would file
    Arabic letters as English on a page somebody switched after typing.
    """
    flow = flow_body()
    assert 'form.append("name_" + nameLang' in flow, (
        "the name is not posted under a language key, so the server has only the plain field"
    )
    assert 'var nameLang = Capture.lang' in flow, "the name's language is never initialised"
    assert '$("full-name").addEventListener("input"' in flow, (
        "the language the name was written in is read at submit rather than as it is typed"
    )
    assert 'form.append("full_name"' in flow, (
        "the plain name field was dropped; older deployments of the server still read it"
    )


def test_the_name_languages_are_the_ones_the_server_stores():
    """``name_<code>`` has to be a key ``names.parse`` understands, or the name is dropped.

    Read off both sides rather than written twice: the page's picker and the server's storage
    are the same four codes, and a language added to one without the other is a name that never
    arrives.
    """
    offered = sorted(set(re.findall(r'code: "([a-z]{2})"', capture_body())))
    assert offered == sorted(names_module.LANGUAGES), (
        f"capture.js offers {offered} and the server stores {sorted(names_module.LANGUAGES)}; "
        "the page posts the name as name_<code>"
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
