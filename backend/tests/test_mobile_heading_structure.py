"""Every mobile screen's heading outline, held to one shape.

WHY THIS TEST EXISTS
--------------------
The screens were ported from the web a card at a time, and each card brought the web's own
``<h3 class="hand-section-title">``. On a desk page that is correct: the document has its own
h1 and h2s above every card. On the handset only one screen is mounted at a time, the header
shows the signed-in worker rather than a title, and the shell drew no h1 at all - so a
screen-reader user met "heading level 3: This month" floating with nothing above it, and a
heading that cannot be placed in the outline is a heading that cannot be jumped between.

The shape that fixes it, and the shape this test holds:

  * the shell draws each screen's own name (``Screen.title``) as the document's one **h1**,
    read and never seen, because the design has no title band;
  * every section title inside a screen is an **h2** - never a deeper level, because there is
    no h3 to sit under one;
  * the sign-in screen is not this shell and carries its own visible h1.

It is a source-level check on purpose: the alternative is a DOM harness the Node toolchain in
this repository does not have, and what regresses here is the markup itself - the next card
ported from the web is exactly how the orphan levels would come back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from harness import PROJECT_ROOT

SCREENS_DIR = PROJECT_ROOT / "mobile-client" / "src" / "ui" / "screens"
SHELL_TS = PROJECT_ROOT / "mobile-client" / "src" / "ui" / "shell.ts"

SCREEN_FILES = sorted(SCREENS_DIR.glob("*/*.ts"))
SCREEN_IDS = [path.parent.name for path in SCREEN_FILES]

#: An opening tag that carries the section-title class, with its level.
SECTION_TITLE_TAG = re.compile(r"<h([1-6])\s+class=\"[^\"]*hand-section-title", re.IGNORECASE)
#: Any heading at all, opening or closing.
ANY_HEADING = re.compile(r"</?h([1-6])[\s>]", re.IGNORECASE)


def test_the_screens_are_where_this_test_thinks_they_are():
    """A guard on the guard: a moved directory would otherwise make every case below vacuous."""
    assert SCREEN_IDS == ["alerts", "clock", "history", "login", "notes", "profile"], SCREEN_IDS
    assert SHELL_TS.exists()


@pytest.mark.parametrize("path", SCREEN_FILES, ids=SCREEN_IDS)
def test_no_screen_uses_a_heading_deeper_than_h2(path: Path):
    source = path.read_text(encoding="utf-8")
    levels = sorted({int(match) for match in ANY_HEADING.findall(source)})
    assert not [level for level in levels if level > 2], (
        f"{path.name} uses an h{max(levels)}: the shell provides the h1 and section titles "
        f"are h2, so a deeper level has nothing above it to hang from"
    )


@pytest.mark.parametrize("path", SCREEN_FILES, ids=SCREEN_IDS)
def test_a_section_title_is_always_an_h2(path: Path):
    """Screens without section titles (Alerts, sign-in) pass with no levels at all."""
    source = path.read_text(encoding="utf-8")
    levels = [int(match) for match in SECTION_TITLE_TAG.findall(source)]
    assert set(levels) <= {2}, f"{path.name} renders hand-section-title at {levels}"


@pytest.mark.parametrize("path", SCREEN_FILES, ids=SCREEN_IDS)
def test_every_screen_names_itself(path: Path):
    """The shell's h1 is drawn from ``Screen.title()``; a screen without one would render empty."""
    source = path.read_text(encoding="utf-8")
    assert re.search(r"title:\s*\(\s*\)\s*=>", source), f"{path.name} defines no Screen.title()"


def test_the_shell_draws_the_h1_for_whichever_screen_is_mounted():
    """One h1 per screen, from the screen's own name, before the screen's markup."""
    source = SHELL_TS.read_text(encoding="utf-8")
    assert re.search(r"<h1 class=\"hand-sr-only\"></h1>", source), "no screen h1 in the shell"
    assert re.search(r"heading\.textContent\s*=\s*title", source), (
        "the h1 must carry the screen's own title, not a second copy of it"
    )
    # Appended before the screen host, so the outline starts at the heading and heading
    # navigation reaches it first.
    assert re.search(r"viewHost\.appendChild\(heading\)[\s\S]{0,400}viewHost\.appendChild\(host\)", source), (
        "the h1 must precede the mounted screen in the main region"
    )
    # The fault state (a tab whose screen failed to register) is still a screen: it gets the
    # tab's own name rather than no heading at all.
    assert re.search(r"<h1 class=\"hand-sr-only\">\$\{esc\(TAB_META\[route\.tab\]\.label\)\}</h1>", source), (
        "the missing-screen state must still name its section"
    )


def test_the_sign_in_screen_keeps_its_own_visible_h1():
    """The login frame is not the shell, and its title is the one the design *does* draw."""
    source = (SCREENS_DIR / "login" / "login.ts").read_text(encoding="utf-8")
    headings = re.findall(r"<h([1-6])[\s>]", source)
    assert headings == ["1"], headings
    assert 'class="auth__title"' in source, "the sign-in h1 is the visible title"
