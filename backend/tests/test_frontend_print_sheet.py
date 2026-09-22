"""One implementation of the paper, two readers.

WHY THIS EXISTS
---------------
Putting a report on paper is three rules that have nothing to do with what the report says:
the sheet goes into the page while everything else comes out of the paper, the document
title carries the name the print dialog offers to save under, and all of it is put back when
the dialog closes - on ``afterprint``, because Firefox and Safari render the sheet *after*
``print()`` returns, so emptying it early hands the reader a blank page.

Both readers print: an administrator's Shifts tab, and the self-hours screen that a worker,
a lead worker or a normal administrator reads on the handset. Those three rules were written
out in each of the two files - the same lines twice. Identical on the day they were written,
and a pair that diverges the first time somebody fixes the cleanup in one of them, which is
how a screen ends up printing a stale sheet behind its own or handing back a title it
borrowed. They live in ``PrintReport`` (``frontendjavascript.js``) now, and this suite is
what keeps them there:

1. the page prints from exactly one place, and that place is the helper;
2. nothing outside the helper puts a sheet in the page or takes the page out of the paper;
3. the helper clears up after itself, so the next print cannot inherit this one's page;
4. every print path - and only a print path - hands its HTML to the helper.

The *frame* of a report is shared for the same reason as the mechanics, and one step
further in: a sheet is a title, the lines saying what it covers, the table, what it adds up
to and the note about which figures count, and that document is the same one whoever is
holding it. What each screen supplies is content - its own cells, its own total, what an
empty period reads - which is exactly the split ``sheetHtml`` makes. So:

5. the frame is built once, and both readers compose through it.

What the paper *looks* like is checked where the dialogs are driven rather than here:
``test_frontend_shifts_filter.py`` for the console's Shifts tab and
``test_frontend_admin_handset.py`` for the self-hours screen. This suite checks that there
is only one of it.
"""

from __future__ import annotations

import pytest

from harness import PROJECT_ROOT

FRONTEND = PROJECT_ROOT / "frontend"

#: Where the shared helper lives. Named once, because half of this file is "and nowhere
#: else".
HELPER = "frontendjavascript.js"

#: The two files that print. A third would be a deliberate change here, not a quiet copy.
READERS = ("admin_modules.js", "worker_modules.js")

#: How many *sheets* each reader builds, and therefore how many times it asks the helper
#: for a period line. The console prints its whole period and one worker's month; the
#: self-hours screen prints the reader's own. Written out, so a third sheet is a deliberate
#: change here rather than a second way of describing a period slipping in unnoticed.
SHEETS_PER_READER = {"admin_modules.js": 2, "worker_modules.js": 1}


@pytest.fixture(scope="module")
def scripts() -> dict[str, str]:
    """Every first-party script, by name - the shipped files, not a copy of them."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND.glob("*.js"))
    }


def test_the_page_prints_from_exactly_one_place(scripts):
    """A second ``window.print()`` is a second sheet, a second cleanup and a second bug."""
    callers = sorted(name for name, text in scripts.items() if "window.print()" in text)
    assert callers == [HELPER], (
        "printing goes through PrintReport in "
        f"{HELPER}; it is also called from {callers}"
    )
    assert scripts[HELPER].count("window.print()") == 1, (
        "one call to the dialog, not one per sheet"
    )


def test_only_the_helper_moves_the_page_out_of_the_paper(scripts):
    """The class and the node are the whole mechanism, and they belong to one file.

    ``body.is-printing-report`` is the stylesheet's hook and ``.print-sheet`` is the one
    thing on paper - so whoever sets them decides what gets printed, and there had better
    be one of those.
    """
    for pattern in (
        "classList.add('is-printing-report')",
        "classList.remove('is-printing-report')",
        "className = 'print-sheet'",
    ):
        owners = sorted(name for name, text in scripts.items() if pattern in text)
        assert owners == [HELPER], f"{pattern!r} belongs to the helper alone; found in {owners}"


def test_the_helper_puts_the_page_back_as_it_found_it():
    """Clears first, listens once, unlistens, restores the title - in that order.

    Read as source rather than driven, because what is being pinned is the *shape* of the
    cleanup: an ``afterprint`` listener that never takes itself off piles up on ``window``
    for as long as the tab lives, each copy restoring a title that is no longer the tab's.
    The behaviour it produces is asserted in the two suites that drive the dialogs.
    """
    helper = (FRONTEND / HELPER).read_text(encoding="utf-8")
    body = helper[helper.index("const PrintReport"):helper.index("const Location")]

    # A sheet from a print whose dialog never closed must not be printed behind this one,
    # so the clear comes before the append.
    assert "this.clear()" in body and "appendChild" in body, body
    assert body.index("this.clear()") < body.index("appendChild"), (
        "the sheet already in the page has to go before the new one arrives"
    )

    assert body.count("window.addEventListener('afterprint'") == 1, (
        "one listener per print, registered once"
    )
    assert body.count("window.removeEventListener('afterprint'") == 1, (
        "and it takes itself off again"
    )
    listener = body[body.index("const afterPrint"):body.index("window.addEventListener('afterprint'")]
    assert "removeEventListener('afterprint'" in listener, (
        "the listener must be its own last act, or every print leaves one behind"
    )
    assert "document.title = screenTitle" in listener, (
        "the dialog borrows the tab's title for the file name; the tab gets it back"
    )


def test_both_readers_hand_their_sheet_to_the_helper(scripts):
    """The console's Shifts tab and the self-hours screen, printing the same way."""
    for name in READERS:
        assert "PrintReport.sheet(" in scripts[name], (
            f"{name} must build its rows its own way and print them through the helper"
        )
    mentioned = sorted(
        name for name, text in scripts.items() if "PrintReport" in text and name != HELPER
    )
    assert mentioned == sorted(READERS), (
        "the helper has exactly the two print paths it was written for: "
        f"found {mentioned}"
    )


def test_the_sheet_frame_is_built_in_one_place(scripts):
    """The document itself - title, meta, table, note - belongs to the helper.

    A reader that writes these class names is a reader that has its own idea of what a
    report looks like on paper, which is the drift this suite exists to prevent: the note
    at the foot is the rule both reports are read under, and the period line has to read
    the same on the console's sheet as on the worker's.
    """
    for pattern in (
        'class="print-sheet-title"',
        'class="print-sheet-meta"',
        'class="print-sheet-table"',
        'class="print-sheet-totals"',
        'class="print-sheet-note"',
        'class="print-sheet-empty"',
        # The company's own head, for the same reason: whose document it is comes from the
        # settings row and is drawn by the frame, so a reader cannot print a sheet that
        # reaches payroll anonymously - or one with a second idea of what the lockup is.
        'class="print-sheet-brand"',
        'class="print-sheet-mark"',
        'class="print-sheet-company"',
        'class="print-sheet-sub"',
        'class="print-sheet-tagline"',
    ):
        owners = sorted(name for name, text in scripts.items() if pattern in text)
        assert owners == [HELPER], f"{pattern} belongs to the helper alone; found in {owners}"

    # Both readers compose through it, and both take the period line from it rather than
    # formatting two dates themselves.
    for name in READERS:
        assert "PrintReport.sheetHtml(" in scripts[name], (
            f"{name} must supply its content and let the helper build the sheet"
        )
        assert "PrintReport.periodLine(" in scripts[name], (
            f"{name} must not describe the period in its own words"
        )

    # And neither of them draws a date range of its own: the arrow between the two dates is
    # the helper's. (The translation tables carry arrows too - they are text, not a second
    # period line - so this check is aimed at the two files that print.)
    for name in READERS:
        assert "\u2192" not in scripts[name], (
            f"{name} formats its own date range; the period line is the helper's"
        )

    helper = scripts[HELPER]
    assert helper.count("sheetHtml({") == 1, "one definition of what a report sheet is"
    assert helper.count("periodLine(period, extra)") == 1, "and one definition of its period line"
    assert helper.count("PrintReport.periodLine(") == 0, "the helper calls itself nothing"
    for name, sheets in SHEETS_PER_READER.items():
        assert scripts[name].count("PrintReport.periodLine(") == sheets, (
            f"{name} builds {sheets} sheet(s) and must take a period line from the helper "
            f"for each of them, rather than formatting one of its own"
        )


def test_the_stylesheet_is_what_takes_the_page_out_of_the_paper():
    """The class the helper sets has to be the class the print rules are scoped to.

    Both halves of this are load-bearing and they live in different files: during the dialog
    the sheet is the only thing left in the page, and outside it the sheet is hidden and a
    plain Ctrl+P on the screen prints the screen.
    """
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    assert ".print-sheet { display: none; }" in css, (
        "the sheet is a print artefact; it must not be a block on screen"
    )
    assert "body.is-printing-report > *:not(.print-sheet)" in css, (
        "everything that is not the sheet leaves the page while the dialog is open"
    )
