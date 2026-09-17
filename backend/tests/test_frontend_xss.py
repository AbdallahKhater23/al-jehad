"""The stored-XSS surface, driven through the real frontend files.

WHY THIS EXISTS
---------------
The backend now refuses markup at the boundary (``textguard``), which means a name, a site
or a note *written from today on* cannot be a payload. It does not mean the frontend may
stop escaping, for three reasons that are worth writing down where the tests live:

1. **Rows written before this change are still there.** A name saved last month is exactly
   the row an attacker would have planted, and the read path is the same for it.
2. **The two defences fail differently.** Validation is a rule about input; escaping is a
   rule about output. A view that interpolates a server value into ``innerHTML`` without
   escaping is a bug whether or not the value could get in through the front door - the CSV
   import path, a support ``UPDATE``, and the next endpoint somebody adds are all front
   doors.
3. **Notification bodies are built from these strings** server-side, so the mark a payload
   leaves can travel to a reader this application has not written yet.

So this suite renders the *hostile* values - an ``<img onerror>``, a ``<script>`` site, an
``svg/onload`` status - through the app's own rendering functions, in the Node VM the other
frontend suites use, and asserts what lands in the DOM: the tag must not appear, the escaped
form must, and the readable text must survive (an escape that deletes the name is not an
escape). Both layouts are covered, because the app paints a card list and a table from two
different template literals and only one of them used to escape.

The last tests read the source instead. They pin the number of inline event handlers and
assert no page carries an inline ``<script>`` block - the two reasons ``CSP_HTML`` still names
``script-src-attr 'unsafe-inline'``, and both numbers may only fall - and they keep the boot
panel's script probe free of the string-built ``Function`` call that a policy without
``'unsafe-eval'`` refuses, because a diagnostic that names a loaded script as missing is
worse than no diagnostic at all.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import frontend_vm
from harness import PROJECT_ROOT

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

FRONTEND = PROJECT_ROOT / "frontend"

#: The payloads, kept in one place so a scenario cannot quietly test a friendlier string
#: than the one it claims to. Each one is a different *shape* of payload: an attribute
#: handler, a script element, an attribute-only handler on a tag nobody filters, and a
#: case/entity variant that a naive ``<script`` check would miss.
HOSTILE_NAME = "<img src=x onerror=alert(1)>"
HOSTILE_SITE = "<script>alert(2)</script> Depot"
HOSTILE_STAMP = "2026-08-07 <b>16:02</b>"
HOSTILE_STATUS = "<svg/onload=alert(3)>Approved"

HARNESS = (
    r"""
const NAME = '%(name)s';
const SITE = '%(site)s';
const STAMP = '%(stamp)s';
const STATUS = '%(status)s';

function responder(url) {
    if (url.includes('/worker/me/logs')) {
        return { status: 200, body: [
            { id: 1, action: 'Clock In', timestamp: STAMP, site: SITE, hours: 8, status: STATUS },
            { id: 2, action: 'Clock Out', timestamp: '2026-08-07 17:00:00',
              site: 'Downtown Tower A', hours: 8, status: 'Approved' }
        ] };
    }
    if (url.includes('/worker/notes')) {
        return { status: 200, body: { open: 1, notes: [{
            id: 7, subject: NAME, category: 'other', status: 'open', priority: 'high',
            worker_unread: 1, created_at: '2026-08-07 09:00:00', last_reply_at: '2026-08-07 09:30:00',
            last_message: { body: SITE }
        }] } };
    }
    if (url.includes('/worker/me/stats')) {
        return { status: 200, body: { active: null, queued: 0, monthHours: 0 } };
    }
    return { status: 200, body: {} };
}

function workerEnv() {
    const env = boot();
    env.setResponder(responder);
    env.evaluate(`State.saveUser({ id: '601', name: ${JSON.stringify(NAME)}, role: 'worker', token: 'tok' })`);
    return env;
}

function escaped(html, raw) {
    // Two questions per payload, and both have to be asked: the raw tag must be gone, and
    // the escaped form must be what replaced it. ``raw_gone`` alone is satisfied by
    // deleting the value, which would silently lose a worker's own name off their screen.
    return {
        raw: html.indexOf(raw) >= 0,
        escaped_present: html.indexOf(raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')) >= 0,
        readable_tail: raw.indexOf('Depot') >= 0 ? html.indexOf('Depot') >= 0 : true
    };
}

const results = {};

    // 1. the worker's own clock-in history, painted as cards on a phone and as a table on a
    //    desktop. This is the one screen that shows a *site* the worker never chose, so a
    //    site name is the value a colleague's account - or a rogue admin - can plant.
    {
        const env = workerEnv();
        env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
        await env.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
        const mobile = env.evaluate("document.getElementById('historyTable').innerHTML");
        env.evaluate("localStorage.setItem('layoutOverride', 'desktop')");
        await env.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
        const desktop = env.evaluate("document.getElementById('historyTable').innerHTML");
        results.history = {
            mobile_site: escaped(mobile, SITE),
            desktop_site: escaped(desktop, SITE),
            mobile_stamp: escaped(mobile, STAMP),
            desktop_stamp: escaped(desktop, STAMP),
            mobile_status: escaped(mobile, STATUS),
            desktop_status: escaped(desktop, STATUS),
            // The row is still a row: the honest site, the hours and the action survive.
            rows_shown: (mobile.match(/Downtown Tower A/g) || []).length,
            hours_shown: mobile.indexOf('8.00') >= 0
        };
    }

    // 2. the signed-in worker's own name, in the header and on the profile screen. The name
    //    is set by an administrator or by a registration link, and every worker sees their
    //    own copy of it.
    {
        const env = workerEnv();
        results.header = {
            name: escaped(env.evaluate("UI.workerHeaderHtml()"), NAME)
        };
        env.evaluate("UI.renderWorkerProfile(document.getElementById('workerProfile'))");
        const profile = env.evaluate("document.getElementById('workerProfile').innerHTML");
        results.profile = { name: escaped(profile, NAME), id_shown: profile.indexOf('601') >= 0 };
    }

    // 3. the worker's notes list: the subject, the category label and the preview of the
    //    last message, all of which come off the server as free text.
    {
        const env = workerEnv();
        await env.evaluate("WORKER_MODULES.renderNotes(document.getElementById('notesHost'))");
        const html = env.evaluate("document.getElementById('notesHost').innerHTML");
        results.notes = {
            subject: escaped(html, NAME),
            preview: escaped(html, SITE),
            card_rendered: html.indexOf('data-note="7"') >= 0
        };
    }

    // 4. a failing request: the server's own sentence is escaped too. It is our text today,
    //    but "our text" stops being true the moment an endpoint interpolates a client value
    //    into it, and this is the branch that interpolates it into HTML.
    {
        const env = workerEnv();
        env.setResponder((url) => url.includes('/worker/me/logs')
            ? { status: 500, body: { detail: { message: NAME } } }
            : { status: 200, body: {} });
        await env.evaluate("WORKER_MODULES.renderHistory(document.getElementById('historyTable'))");
        const html = env.evaluate("document.getElementById('historyTable').innerHTML");
        results.failed_request = { message: escaped(html, NAME), said_error: html.indexOf('Error') >= 0 };
    }
"""
    % {
        "name": HOSTILE_NAME,
        "site": HOSTILE_SITE,
        "stamp": HOSTILE_STAMP,
        "status": HOSTILE_STATUS,
    }
)


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def _assert_escaped(block: dict, *, where: str) -> None:
    assert block["raw"] is False, f"{where}: the payload was interpolated into the DOM raw"
    assert block["escaped_present"] is True, (
        f"{where}: the value is neither raw nor escaped - it was dropped, and a dropped name "
        "is a blank line in a report"
    )


def test_a_hostile_site_name_stays_text_in_the_worker_history(results):
    """Both layouts: the card list and the table are two template literals, not one."""
    _assert_escaped(results["history"]["mobile_site"], where="history (mobile) site")
    _assert_escaped(results["history"]["desktop_site"], where="history (desktop) site")


def test_a_hostile_timestamp_and_status_stay_text_in_the_worker_history(results):
    """The two values beside the site come from the same row and were both raw.

    The status is asserted on the phone card only, because the desktop table has no status
    column (action, time, site, hours) - "missing" and "escaped" are different facts, and a
    test that could not tell them apart would one day pass on a screen that lost a column.
    The card is also the layout the workers actually use.
    """
    _assert_escaped(results["history"]["mobile_stamp"], where="history (mobile) timestamp")
    _assert_escaped(results["history"]["desktop_stamp"], where="history (desktop) timestamp")
    _assert_escaped(results["history"]["mobile_status"], where="history (mobile) status")
    assert results["history"]["desktop_status"]["raw"] is False


def test_escaping_a_site_does_not_erase_the_row(results):
    history = results["history"]
    assert history["rows_shown"] == 1, "the ordinary site on the second row must still be there"
    assert history["hours_shown"] is True, "and the row's figures with it"


def test_the_signed_in_workers_own_name_is_text(results):
    _assert_escaped(results["header"]["name"], where="worker header")
    _assert_escaped(results["profile"]["name"], where="worker profile")
    assert results["profile"]["id_shown"] is True


def test_a_hostile_note_subject_and_preview_stay_text(results):
    _assert_escaped(results["notes"]["subject"], where="notes list subject")
    _assert_escaped(results["notes"]["preview"], where="notes list preview")
    assert results["notes"]["card_rendered"] is True


def test_a_servers_own_error_message_is_escaped_too(results):
    """It is our sentence today; escaping it is what keeps that from being load-bearing."""
    _assert_escaped(results["failed_request"]["message"], where="history error branch")
    assert results["failed_request"]["said_error"] is True, "the reader still learns it failed"


# ---------------------------------------------------------------------------
# source-level guards: the two allowances the CSP still makes
# ---------------------------------------------------------------------------
#: Inline event handlers, pinned per file. The document policy has to allow
#: ``script-src-attr 'unsafe-inline'`` because the console builds its markup as strings and
#: puts the handler in an attribute; converting a call site to a delegated listener is what
#: removes that line from ``netguard.CSP_HTML``. The count is allowed to fall and never to
#: rise: adding an ``onclick=`` to a new button fails here, with this sentence as the reason.
INLINE_HANDLER_BUDGET = {
    "admin_modules.js": 67,
    "frontendjavascript.js": 19,
    "worker_modules.js": 10,
}

HANDLER_RE = re.compile(r"\son([a-z]+)\s*=")
INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)

#: Server values that must never appear unescaped in a template literal. Each was a real
#: interpolation in this codebase; the pattern is the exact shape that was fixed, so a revert
#: or a copy of it into a new view fails here rather than in a browser.
UNESCAPED_INTERPOLATIONS = (
    r"\$\{\s*log\.site\s*\}",
    r"\$\{\s*log\.timestamp\s*\}",
    r"\$\{\s*log\.status\s*\|\|\s*''\s*\}",
    r"\$\{\s*State\.user\.name\s*\}",
    r"\$\{\s*State\.user\.name\s*\|\|\s*'-'\s*\}",
    r"\$\{\s*State\.user\.id\s*\}",
    r"\$\{\s*State\.user\.role\s*\}",
    r"\$\{\s*err\.message\s*\}",
    # Not an injection anybody could reach today - the value is always ``data:image/png;base64,``
    # plus base64 - but the same field is escaped one screen further down, and a rule with an
    # exception nobody can name is how the next unescaped interpolation gets written.
    r"\$\{\s*res\.qr_png_data_uri\s*\}",
)


def test_no_new_inline_event_handlers():
    for name, budget in INLINE_HANDLER_BUDGET.items():
        body = (FRONTEND / name).read_text(encoding="utf-8")
        count = len(HANDLER_RE.findall(body))
        assert count <= budget, (
            f"{name} has {count} inline event handlers, up from {budget}. Bind the handler "
            "with addEventListener (or a delegated listener on the container) instead: each "
            "inline handler is why the document CSP still allows 'unsafe-inline' for "
            "attributes, and that allowance is what an injected <img onerror=...> needs."
        )


def test_the_pages_carry_no_inline_script_block():
    """Every script these pages run is a file, which is what makes ``script-src`` strict."""
    for page in sorted(FRONTEND.glob("*.html")):
        body = page.read_text(encoding="utf-8")
        found = INLINE_SCRIPT_RE.findall(body)
        assert not found, (
            f"{page.name} contains an inline script block ({found[:1]}). Move it into its own "
            "file and reference it with src=: an inline block cannot be told apart from an "
            "injected one, so the browser refuses both only if there are none."
        )


def test_the_extracted_scripts_are_valid_javascript():
    """The blocks that were inline are files now; a broken extraction is a blank page."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for name in ("boot.js", "tailwind_boot.js", "enroll.js", "quick.js"):
        path: Path = FRONTEND / name
        assert path.exists(), f"{name} is referenced by a page but missing"
        completed = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        assert completed.returncode == 0, f"{name} does not parse:\n{completed.stderr}"


def test_the_boot_panel_diagnoses_a_script_without_eval():
    """The one screen whose whole job is to say what broke must not be the thing that lies.

    ``frontend/boot.js`` reports which scripts failed to load by asking for each global by
    name. It used to build that question as a string for the ``Function`` constructor - which
    is exactly what a document policy without ``'unsafe-eval'`` refuses. So the probe threw,
    the surrounding ``catch`` read the throw as "not loaded", and the panel named *every*
    script as missing, on the only occasion it is ever shown.

    Two assertions, because there are two ways to get this wrong again. The first reads the
    source: neither the string-built call nor ``eval`` may come back. The second runs both
    forms under Node with code generation disabled - the nearest thing to the browser's
    policy - so the difference is demonstrated rather than asserted in a comment.
    """
    boot = (FRONTEND / "boot.js").read_text(encoding="utf-8")
    assert "new Function" not in boot, (
        "boot.js builds code from a string, which the document CSP refuses ('unsafe-eval' is "
        "not granted); probe the global with typeof instead"
    )
    assert not re.search(r"(?<![\w.$])eval\s*\(", boot), "boot.js calls eval"

    probe = (
        "const I18n = {};"
        "let stringBuilt;"
        "try { stringBuilt = new Function('return typeof I18n')() !== 'undefined'; }"
        "catch (err) { stringBuilt = err.name; }"
        "console.log(JSON.stringify({stringBuilt, direct: typeof I18n !== 'undefined'}));"
    )
    completed = subprocess.run(
        [frontend_vm.NODE, "--disallow-code-generation-from-strings", "-e", probe],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-300:]
    outcome = json.loads(completed.stdout)
    assert outcome["stringBuilt"] == "EvalError", outcome
    assert outcome["direct"] is True, outcome


def test_no_server_value_is_interpolated_unescaped():
    """Line by line, because the sink is what decides whether escaping is right.

    ``Toast`` paints with ``textContent`` - the DOM does the escaping, and an escaped value
    there would be *shown* as ``&lt;b&gt;`` - so a toast line is skipped rather than fixed.
    Everywhere else in these files the destination is ``innerHTML``, which is a parser.
    """
    for name in sorted(path.name for path in FRONTEND.glob("*.js")):
        for number, line in enumerate((FRONTEND / name).read_text(encoding="utf-8").splitlines(), 1):
            if "Toast." in line:
                continue
            for pattern in UNESCAPED_INTERPOLATIONS:
                match = re.search(pattern, line)
                assert match is None, (
                    f"{name}:{number} interpolates {match.group(0) if match else pattern} into "
                    "markup without escapeHtml: a value off the wire is text, and innerHTML is "
                    "a parser"
                )
