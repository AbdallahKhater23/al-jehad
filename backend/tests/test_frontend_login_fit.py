"""The two sizes an unauthenticated screen gets wrong on a phone.

WHY THIS EXISTS
---------------
Everything else in this suite drives the DOM, and the stub VM cannot lay anything out:
a card that is 689px tall inside a 600px viewport is invisible to every JavaScript
assertion in the repository. Two specific failures were found by measuring the real page
and are pinned here, because both are one CSS edit away from coming back and neither
produces an error anywhere:

1. **The login screen did not fit.** Stacked, the brand panel (mark, wordmark, legal line,
   year, all centred) cost 208px of the roughly 600px a phone browser hands back, so the
   card came to 689px and *Sign In* sat below the fold - the form had to be scrolled
   before it could be submitted to a worker standing at a gate. Side by side the same
   lockup is 82px and the whole screen fits.

2. **The primary rule was dead.** ``.login-submit { min-height: 48px; font-size: 15px }``
   was declared above ``.ui-btn`` and lost the cascade at equal specificity, so the two
   declarations never applied and the button shipped at the generic 42px. The fix is the
   two-class selector ``.login-form .login-submit``, which wins on specificity rather than
   on position - and this test is what stops a later tidy-up from "simplifying" it back.

It also holds the handset's clock button to a thumb rather than a poster: it was 168px
when it *was* the card, 128px after a first trim, and either size pushes the month's hours
and the GPS state off a 640px screen.

The stylesheet is read as text, the way ``test_frontend_live_ops`` reads it for its tokens.
No browser and no Node are needed.
"""

from __future__ import annotations

import re

from harness import PROJECT_ROOT

#: Comments are dropped before anything is parsed: a rule carrying an explanatory note
#: above it would otherwise be found under a "selector" that starts with the note.
STYLE = re.sub(
    r"/\*.*?\*/",
    "",
    (PROJECT_ROOT / "frontend" / "style.css").read_text(encoding="utf-8", errors="ignore"),
    flags=re.S,
)

#: The handset breakpoint. Above it the login screen is the two-column card, whose brand
#: panel is a column again - the strip is a phone decision, not a new look.
PHONE_QUERY = "max-width: 899px"


def _block_at(source: str, open_brace: int) -> str:
    """The text of the ``{ ... }`` block whose opening brace is at ``open_brace``."""
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1:index]
    raise AssertionError("unbalanced braces in style.css")


def _media_block(query: str, marker: str) -> str:
    """The ``@media`` block matching ``query`` that is the one mentioning ``marker``."""
    for match in re.finditer(r"@media[^{]*\{", STYLE):
        if query not in match.group(0):
            continue
        block = _block_at(STYLE, match.end() - 1)
        if marker in block:
            return block
    raise AssertionError(f"no @media ({query}) block containing {marker}")


def _rule(source: str, selector: str) -> str:
    """The declarations of ``selector``, or ``""`` if the rule is not there.

    Whole selectors, not substrings: ``.login-submit`` is inside
    ``.login-form .login-submit``, and a substring match would read the two-class rule's
    body as the bare rule's - which is exactly the distinction this file is about.
    """
    for header, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
        parts = [part.strip() for part in header.split(",")]
        if selector in parts:
            return declarations
    return ""


def _length(declarations: str, prop: str) -> int:
    match = re.search(rf"(?<![\w-]){prop}\s*:\s*(\d+)px", declarations)
    assert match, f"no {prop} in {declarations!r}"
    return int(match.group(1))


def test_the_phone_login_is_a_strip_so_the_form_fits_without_scrolling():
    phone = _media_block(PHONE_QUERY, ".login-card")
    brand = _rule(phone, ".login-brand")
    assert "flex-direction: row" in brand, (
        "the phone lockup is stacked again: at 208px it put Sign In below the fold"
    )
    # The mark and the wordmark are what the strip is made of; shrinking the mark to a
    # wordmark-sized glyph is what makes the row fit a 320px handset.
    assert _length(_rule(phone, ".login-mark"), "width") <= 44, "the mark is panel-sized"
    assert _length(_rule(phone, ".login-company"), "font-size") <= 22
    # Nothing is dropped to save the room: the strip still carries the year.
    assert ".login-est" in phone, "the year left the lockup rather than the panel shrinking"
    assert "display: none" not in _rule(phone, ".login-est")


def test_the_primary_button_wins_the_cascade_by_specificity_not_by_position():
    """``min-height``/``font-size`` here must not depend on being *below* ``.ui-btn``."""
    submit = _rule(STYLE, ".login-form .login-submit")
    assert submit, (
        "the login submit lost its two-class selector, so .ui-btn overrides it again: the "
        "declarations are only read if this rule is more specific, not if it is later"
    )
    assert _length(submit, "min-height") >= 44, "the primary action shrank below the tap floor"

    # A bare ``.login-submit`` rule that also sets a size is the bug coming back.
    bare = _rule(STYLE, ".login-submit")
    assert "min-height" not in bare, f"a position-dependent rule is back: {bare!r}"


def test_the_phone_login_keeps_the_submit_and_the_help_reachable():
    phone = _media_block(PHONE_QUERY, ".login-card")
    assert _length(_rule(phone, ".login-form .login-submit"), "min-height") >= 44, (
        "the phone primary action is below the 44px touch floor"
    )
    # The stage and the panel are the two paddings that decide whether the card fits.
    for selector in (".login-stage", ".login-form-panel"):
        assert selector in phone, f"{selector} is not tuned on a phone"


def test_no_phone_control_is_under_the_touch_floor():
    """32px is a mouse measurement. The audit found it all over the phone layouts."""
    block = _media_block(PHONE_QUERY, ".ops-chip")
    for selector in (".ui-chip", ".ops-chip", ".ui-btn-sm", ".ui-btn.is-icon"):
        assert selector in block, f"{selector} kept its 32px height on a phone"
        assert _length(_rule(block, selector), "min-height") >= 40
    # The console builds several controls out of Tailwind utilities rather than a
    # component class - the links screen's selects and its Revoke/Taps buttons, the notes
    # row's Open button - so a class-level floor never reaches them.
    for selector in (".admin button", ".admin select"):
        assert selector in block, f"{selector} has no phone floor"
        assert _length(_rule(block, selector), "min-height") >= 40


def test_the_clock_button_is_a_thumb_and_not_a_poster():
    hand = _rule(STYLE, ".hand-clock")
    assert hand, "the handset's primary action lost its rule"
    assert _length(hand, "min-height") <= 80, (
        "a 100px+ clock button pushes the month's hours and the GPS state off a 640px screen"
    )
    assert _length(hand, "min-height") >= 48, "the primary action is below the tap floor"
    compact = _rule(STYLE, ".hand-clock.compact")
    assert compact, "the desktop variant is now sized by .clock-button again"
    assert _length(compact, "min-height") <= 80
