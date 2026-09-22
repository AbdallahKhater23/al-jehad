"""The two signed-in screens, in a browser: a worker gets a handset, an admin a console.

WHY THIS EXISTS
---------------
The console's module was moved out of ``index.html`` so a worker's phone stops downloading
the back office, and the console fetches it on demand instead
(``App.loadConsoleModule``). Two things can go wrong with that move and neither is visible
to a test that only reads the server or runs the frontend in a VM:

1. the deferral is undone - somebody puts ``admin_modules.js`` back in ``index.html`` and
   every phone at a gate pays for it again, while every screen still looks correct; and
2. the console signs an administrator in and then never paints, because the on-demand fetch
   failed, was refused by strict MIME checking, or raced the first frame.

The same is true of the role split itself. ``App.renderApp`` sends a worker to
``renderWorkerPortal`` and an administrator to ``renderAdminConsole``, and each branch then
picks a phone or a desktop layout from ``Device.isMobile``. A branch that renders the *other*
role's screen is a real failure - an administrator's console shown to a worker is both a
usability bug and an authorization one - and it is only observable in a browser.

WHAT IS ASSERTED
----------------
Each role, signed in through the real form against the live server and the seeded database,
at the width the width's own layout belongs to:

* the role's own layout is on the page, and the *other* width's layout for that role is not
  (so "phone" is not the desktop screen squeezed into 390px);
* a worker's session never has the console's content host, and never fetches the console's
  module - while an administrator's session does both;
* the console module is fetched *after* sign-in, not shipped in the initial payload: an
  anonymous visit to the shell is watched for the same request and must not make it.

Runs with the browser the machine has - see ``browser.py``. With none installed, it skips
and names ``python -m playwright install chromium``.
"""

from __future__ import annotations

import pytest

import browser as browser_support
import harness
import notifications
from harness import bearer

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Who signs in, and the screen they are owed
# ---------------------------------------------------------------------------
#: Credentials come from the seeded roster, not from literals: the harness reseeds these
#: accounts before every test, so a password here that drifted from ``SEED_USERS`` would be
#: a test that fails for a reason unrelated to the application.
WORKER_LANDED = (
    "document.querySelector('.hand-app') !== null"
    " && document.querySelector('.hand-credit') !== null"
)
CONSOLE_LANDED = (
    "document.getElementById('adminContent') !== null"
    " && document.getElementById('adminContent').children.length > 0"
)

WORKER_SIGN_IN = browser_support.Identity(
    role="worker",
    user_id=harness.WORKER,
    email=harness.EMAILS[harness.WORKER],
    password=harness.PASSWORDS[harness.WORKER],
    landed=WORKER_LANDED,
    why="the handset and its credit line are the worker's own DOM, drawn only once a session exists",
)
ADMIN_SIGN_IN = browser_support.Identity(
    role="administrator",
    user_id=harness.ADMIN,
    email=harness.EMAILS[harness.ADMIN],
    password=harness.PASSWORDS[harness.ADMIN],
    landed=CONSOLE_LANDED,
    why=(
        "the console writes a tab into #adminContent only after admin_modules.js has been "
        "fetched and parsed, so a painted console is proof the deferred module arrived"
    ),
)
MOALLEM_SIGN_IN = browser_support.Identity(
    role="lead worker",
    user_id=harness.MOALLEM,
    email=harness.EMAILS[harness.MOALLEM],
    password=harness.PASSWORDS[harness.MOALLEM],
    landed=WORKER_LANDED,
    why=(
        "a lead worker is a worker first: the handset is the only screen their account can "
        "use, because every /admin/* endpoint the console calls refuses the role"
    ),
)
IDENTITIES = {
    "worker": WORKER_SIGN_IN,
    "administrator": ADMIN_SIGN_IN,
    "lead worker": MOALLEM_SIGN_IN,
}

#: role -> width -> (the layout that width draws, the layout it must not).
#:
#: The desktop worker is a two-column split where the phone worker is a stack with a tab
#: bar; the desktop console has a rail where the phone console has a head with a tab strip.
#: Each pair is asserted both ways, because "the right one is there" and "the wrong one is
#: not" are different bugs - a repaint that leaves the old layout in the DOM passes the
#: first and fails the second.
LAYOUTS = {
    ("worker", "phone"): (".hand-tabs", ".hand-desk"),
    ("worker", "desktop"): (".hand-desk", ".hand-tabs"),
    ("administrator", "phone"): (".admin-mobile-head", ".admin-rail"),
    ("administrator", "desktop"): (".admin-rail", ".admin-mobile-head"),
}


#: The whole worker export flow as one expression.
#:
#: One expression because a probe is the only moment this suite has the tab: it is evaluated
#: once the screen is up and its answer is the only thing that survives the context closing.
#: ``SEEDED_MONTH`` is substituted by the test with the month the fixtures actually worked,
#: so the flow walks the month picker the way a worker would rather than hard-coding a date.
TAKE_THE_TIMESHEET_AWAY = r"""
(async () => {
    const out = {};
    // The real tab, tapped the way a worker taps it.
    document.querySelector('[data-worker-tab="history"]').click();
    const waitFor = async (test, tries = 60) => {
        for (let i = 0; i < tries; i += 1) {
            const value = test();
            if (value) return value;
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return null;
    };

    const card = await waitFor(() => document.querySelector('[data-my-hours]'));
    out.card = card !== null;
    out.buttons = Array.from(document.querySelectorAll('[data-download-hours]'))
        .map((button) => button.getAttribute('data-download-hours'));

    // The month the fixtures worked. The picker refetches, and the summary is repainted
    // from the answer - so waiting for the seeded site name is waiting for the fetch.
    const picker = document.getElementById('myHoursMonth');
    out.picker_present = picker !== null;
    picker.value = 'SEEDED_MONTH';
    picker.dispatchEvent(new Event('change', { bubbles: true }));
    out.seeded_rows = (await waitFor(() =>
        (document.querySelector('[data-my-hours]') || {}).innerText || ''
    )).indexOf('Downtown Tower A') >= 0;
    out.picker_value = picker.value;

    // The CSV: read back out of the blob the page handed the anchor, which is the file.
    const realCreate = URL.createObjectURL;
    // The anchor is removed from the page the instant it is clicked, so its own name is
    // read from inside the click rather than hunted for afterwards.
    const realAnchorClick = HTMLAnchorElement.prototype.click;
    let saved = null;
    URL.createObjectURL = function (blob) { saved = blob; return realCreate.call(URL, blob); };
    HTMLAnchorElement.prototype.click = function () { out.csv_name = this.getAttribute('download'); };
    document.querySelector('[data-download-hours="csv"]').click();
    await new Promise((resolve) => setTimeout(resolve, 200));
    out.csv = saved ? await saved.text() : '';
    URL.createObjectURL = realCreate;
    HTMLAnchorElement.prototype.click = realAnchorClick;

    // The PDF: the printer is replaced so the suite can read the sheet at the moment the
    // dialog opens - the only instant at which the page *is* the paper. A real
    // ``window.print()`` in a headless browser opens nothing and reports nothing.
    out.printed = 0;
    window.print = function () {
        out.printed += 1;
        out.printing = document.body.classList.contains('is-printing-report');
        out.title = document.title;
        const sheets = Array.from(document.body.children)
            .filter((node) => node.className === 'print-sheet');
        out.sheet = sheets.length ? sheets[0].innerText : '';
    };
    document.querySelector('[data-download-hours="pdf"]').click();
    await new Promise((resolve) => setTimeout(resolve, 100));
    return out;
})()
"""


#: One worker's month, printed from that worker's shift row.
#:
#: ``frontend_vm`` proves everything about this sheet except the one thing that makes it a
#: feature: the action is bound to a click. The rows are a string painted into the tab, so a
#: listener that was never attached - or attached to a node the next repaint threw away -
#: leaves a control that looks right and does nothing.
#:
#: It also pins the two halves of "this person's month" against the live server, which is the
#: only place they can be told apart from "the period on screen": the tab is narrowed to one
#: seeded day, and the request and the sheet both have to cover the whole month that day is
#: in. ``SEEDED_DAY`` is substituted by the test with a date the fixtures worked.
PRINT_ONE_WORKER_S_MONTH = r"""
(async () => {
    const out = {};
    const waitFor = async (test, tries = 60) => {
        for (let i = 0; i < tries; i += 1) {
            const value = test();
            if (value) return value;
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return null;
    };

    // Every read of the report endpoint, recorded from before the first click: *which* window
    // the button asks for is half of what this test is about.
    out.requests = [];
    const realFetch = window.fetch;
    window.fetch = function (url) {
        out.requests.push(String(url));
        return realFetch.apply(this, arguments);
    };

    // The real tab, tapped the way an administrator taps it, then narrowed to a single
    // seeded day - a period that is deliberately not a month.
    document.querySelector('[data-admin-tab="Shifts"]').click();
    out.tab = (await waitFor(() => document.getElementById('shiftsStart'))) !== null;
    document.getElementById('shiftsStart').value = 'SEEDED_DAY';
    document.getElementById('shiftsEnd').value = 'SEEDED_DAY';
    document.querySelector('#shiftsFilter button[type="submit"]').click();
    const rows = await waitFor(() => {
        const found = Array.from(
            document.querySelectorAll('[data-shifts-table="true"] tr[data-worker]'));
        return found.length ? found : null;
    });
    out.rows_on_screen = rows ? rows.length : 0;
    out.row_worker = rows ? rows[0].getAttribute('data-worker') : null;
    out.row_date = rows ? rows[0].getAttribute('data-date') : null;

    // The paper, read at the one instant the page *is* the paper.
    out.printed = 0;
    window.print = function () {
        out.printed += 1;
        out.printing = document.body.classList.contains('is-printing-report');
        out.title = document.title;
        const sheets = Array.from(document.body.children)
            .filter((node) => node.className === 'print-sheet');
        out.sheet = sheets.length ? sheets[0].innerText : '';
    };
    const button = await waitFor(() => document.querySelector('[data-print-worker]'));
    out.button = button !== null;
    out.button_label = button ? (button.getAttribute('aria-label') || '') : '';
    if (button) button.click();
    await waitFor(() => out.printed > 0);
    out.month_requests = out.requests.filter((url) =>
        url.indexOf('/admin/reports/shifts') >= 0 && url.indexOf('worker_id=') >= 0);

    window.dispatchEvent(new Event('afterprint'));
    out.after = {
        printing: document.body.classList.contains('is-printing-report'),
        title: document.title,
        sheets: Array.from(document.body.children)
            .filter((node) => node.className === 'print-sheet').length
    };
    window.fetch = realFetch;
    return out;
})()
"""


#: The enroll control, tapped in a real browser.
#:
#: One expression, because a probe is the only moment this suite has the tab. The capture
#: step is replaced before the tap - this browser has no camera to give, and a fake device
#: would test the stream rather than the thing under test, which is whether the button the
#: roster draws is *bound* at all. ``frontend_vm`` cannot answer that: it has no HTML parser
#: and no click dispatch, so a binding that never happened looks exactly like one that did.
#: A control that is drawn and dead is the failure this catches, on the one screen where an
#: administrator can put their own face on the clock.
ENROLL_MY_OWN_FACE = r"""
(async () => {
    const out = {};
    const waitFor = async (test, tries = 60) => {
        for (let i = 0; i < tries; i += 1) {
            const value = test();
            if (value) return value;
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return null;
    };

    // The Credentials tab, tapped the way an administrator taps it.
    const tab = await waitFor(() => document.querySelector('[data-admin-tab="Credentials"]'));
    out.tab = tab !== null;
    if (tab) tab.click();
    out.roster = (await waitFor(() => document.querySelector('[data-credentials-table]'))) !== null;

    window.__enrollStarts = 0;
    Camera.start = async () => { window.__enrollStarts += 1; return { getTracks: () => [] }; };

    // Exactly one row offers it, and it is the signed-in administrator's own.
    const all = document.querySelectorAll('[data-enroll-self]');
    out.buttons = all.length;
    const button = all[0] || null;
    // The nine-column table draws this action as an icon with an accessible name, the way it
    // draws the other row actions, so the words are on ``aria-label`` rather than in the
    // button's text - which is why "is it labelled" is asked of the name, not the markup.
    out.label = button ? (button.getAttribute('aria-label') || (button.textContent || '').trim()) : '';
    out.is_replace = !!out.label && (
        out.label.indexOf(I18n.__('credentialsFaceReplace')) >= 0
        || out.label.indexOf(I18n.__('credentialsFaceEnroll')) >= 0
    );
    const row = button && button.closest('tr');
    out.row = row ? row.getAttribute('data-user') : null;
    out.mine = row ? row.getAttribute('data-user') === String(State.user.id) : false;
    out.face_cell = row ? row.querySelector('[data-face]').getAttribute('data-face') : null;

    if (button) button.click();
    const open = await waitFor(() => document.getElementById('enrollOverlay'));
    out.overlay = open !== null;
    out.camera_starts = window.__enrollStarts;
    out.says = open && open.querySelector('.camera-action')
        ? open.querySelector('.camera-action').textContent.trim() : '';
    out.title_is_its_own = open && open.querySelector('.camera-action')
        ? open.querySelector('.camera-action').textContent.trim() === I18n.__('credentialsFaceEnrollTitle') : false;
    out.shutter = !!(open && open.querySelector('#enrollShutter'));
    // Hand the screen back as it was found: the camera belongs to this expression, and no
    // later assertion should inherit an overlay or a muted page.
    UI_MODULES.closeEnrollCamera();
    out.closed = document.getElementById('enrollOverlay') === null;
    return out;
})()
"""


#: The reference-photo picker on the account edit panel, driven in a real browser.
#:
#: The same argument as the enroll control above, and the same failure it exists for. The
#: roster is repainted from a string on every save, every search and every tab change, and
#: this picker is bound by a ``data-`` hook that each repaint has to re-attach - so a control
#: that is drawn and *never bound* looks exactly like a working one. ``frontend_vm`` cannot
#: see the difference: it calls ``pickEditPhoto`` itself, which is the second half of the
#: flow. Here the first half is the browser's own - a real ``File`` written into the real
#: input through a ``DataTransfer`` (the only way a script may write a file list), the real
#: ``change`` event, and the resulting held state read back out.
#:
#: The row this drives is **somebody else's**, deliberately: writing your own face is what
#: the roster's own capture button does, and the point of this panel is that any worker's
#: photo can be replaced from the account an administrator is already editing.
EDIT_REFERENCE_PHOTO = r"""
(async () => {
    const out = {};
    const waitFor = async (test, tries = 60) => {
        for (let i = 0; i < tries; i += 1) {
            const value = test();
            if (value) return value;
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return null;
    };

    // The Credentials tab, tapped the way an administrator taps it.
    const tab = await waitFor(() => document.querySelector('[data-admin-tab="Credentials"]'));
    out.tab = tab !== null;
    if (tab) tab.click();
    out.roster = (await waitFor(() => document.querySelector('[data-credentials-table]'))) !== null;

    const row = await waitFor(() => Array.from(document.querySelectorAll('tr[data-user]'))
        .find((candidate) => candidate.querySelector('[data-edit-user]')) || null);
    out.row = row ? row.getAttribute('data-user') : null;
    out.own_row = row ? row.getAttribute('data-user') === String(State.user.id) : null;
    if (row) row.querySelector('[data-edit-user]').click();

    const picker = await waitFor(() => document.getElementById('userEditPhoto'));
    out.picker = picker !== null;
    out.picker_for = picker ? picker.getAttribute('data-edit-photo') : null;
    out.panel_for = (function () {
        const panel = document.querySelector('[data-user-edit]');
        return panel ? panel.getAttribute('data-user-edit') : null;
    })();
    const face = document.querySelector('[data-edit-face]');
    out.face_state = face ? face.getAttribute('data-edit-face') : null;

    // The listener has to be the module's own: the event must arrive as a call on the object
    // that owns the flow, naming the control that fired it.
    window.__picked = 0;
    window.__pickedId = null;
    const real = UI_MODULES.pickEditPhoto;
    UI_MODULES.pickEditPhoto = function (input) {
        window.__picked += 1;
        window.__pickedId = input && input.id;
        return real.apply(this, arguments);
    };

    const transfer = new DataTransfer();
    transfer.items.add(new File(
        [new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], 'ana.jpg', { type: 'image/jpeg' }
    ));
    picker.files = transfer.files;
    picker.dispatchEvent(new Event('change', { bubbles: true }));

    out.chosen = (await waitFor(() => document.querySelector('[data-edit-photo-chosen]'))) !== null;
    out.handler_calls = window.__picked;
    out.handler_target = window.__pickedId;
    out.held = UI_MODULES._credentialsEditPhoto ? UI_MODULES._credentialsEditPhoto.name : null;
    out.clear_offered = document.querySelector('[data-clear-edit-photo]') !== null;

    // Hand the screen back as it was found: this panel belongs to this expression, and no
    // later assertion should inherit an open form.
    UI_MODULES.pickEditPhoto = real;
    await UI_MODULES.closeUserEdit();
    out.panel_closed = document.querySelector('[data-user-edit]') === null;
    return out;
})()
"""


#: The column chooser on My hours, driven in a real browser.
#:
#: The same argument as the two controls above, and one more of its own. These boxes are
#: re-drawn from a string on every repaint (a month change, a tab switch), so a listener that
#: was never re-attached leaves eight checkboxes that look exactly like working ones - and
#: ``frontend_vm`` cannot tell the difference: it has no HTML parser, so it calls
#: ``toggleMyHoursColumn`` itself, which is the second half of the flow. Here the first half
#: is the browser's own: a real click on a real box, the real ``change`` event, the module
#: called with the column the box stands for.
#:
#: Two things are worth a browser beyond the binding. The refusal - unticking the last column
#: - has to put the *box* back, which is a DOM state no VM test can see. And the point of the
#: whole feature is a file, so both files are read back: the CSV out of the blob the page
#: handed the anchor, the sheet off the page at the moment the dialog opens.
CHOOSE_THE_COLUMNS = r"""
(async () => {
    const out = {};
    const waitFor = async (test, tries = 60) => {
        for (let i = 0; i < tries; i += 1) {
            const value = test();
            if (value) return value;
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return null;
    };

    // The real tab, tapped the way a worker taps it, then the worker's own month: the
    // summary is what carries the chooser, and a file worth reading needs rows on it.
    document.querySelector('[data-worker-tab="history"]').click();
    const card = await waitFor(() => document.querySelector('[data-report-columns]'));
    out.chooser = card !== null;
    const picker = document.getElementById('myHoursMonth');
    picker.value = 'SEEDED_MONTH';
    picker.dispatchEvent(new Event('change', { bubbles: true }));
    out.seeded_rows = (await waitFor(() =>
        (document.querySelector('[data-my-hours]') || {}).innerText || ''
    )).indexOf('Downtown Tower A') >= 0;

    // Open it: a control inside a closed disclosure can still be clicked by a script, and
    // clicking one is not what a worker does.
    const chooser = document.querySelector('[data-report-columns]');
    chooser.open = true;
    const boxes = Array.from(document.querySelectorAll('[data-report-column]'));
    const box = (id) => boxes.find((candidate) => candidate.getAttribute('data-report-column') === id);
    out.boxes = boxes.map((candidate) => candidate.getAttribute('data-report-column'));
    out.ticked_at_start = boxes
        .filter((candidate) => candidate.checked)
        .map((candidate) => candidate.getAttribute('data-report-column'));
    const chosen = () => WORKER_MODULES.myHoursColumns(WORKER_MODULES._myHoursReport).join(',');

    // The listener has to be the module's own: the event must arrive as a call on the object
    // that owns the flow, naming the column the box stands for.
    window.__toggled = [];
    const realToggle = WORKER_MODULES.toggleMyHoursColumn;
    WORKER_MODULES.toggleMyHoursColumn = function (id, on) {
        window.__toggled.push(id + ':' + on);
        return realToggle.apply(this, arguments);
    };

    box('break').click();
    out.after_untick = chosen();
    out.untick_calls = window.__toggled.join(' ');

    window.__saved = 0;
    const realSave = WORKER_MODULES.saveMyHoursColumns;
    WORKER_MODULES.saveMyHoursColumns = function () {
        window.__saved += 1;
        return realSave.apply(this, arguments);
    };
    document.querySelector('[data-save-columns]').click();
    await waitFor(() => window.__saved > 0);
    await new Promise((resolve) => setTimeout(resolve, 300));
    out.save_calls = window.__saved;
    out.after_save = chosen();
    out.stored = (WORKER_MODULES._myHoursReport.columns || []).join(',');
    out.toast = Array.from(document.querySelectorAll('.toast')).map((el) => el.textContent);

    // The paper, read at the one instant the page *is* the paper.
    out.printed = 0;
    window.print = function () {
        out.printed += 1;
        const sheets = Array.from(document.body.children)
            .filter((node) => node.className === 'print-sheet');
        out.sheet = sheets.length ? sheets[0].innerText : '';
    };
    document.querySelector('[data-download-hours="pdf"]').click();
    await new Promise((resolve) => setTimeout(resolve, 100));
    window.dispatchEvent(new Event('afterprint'));

    // And the spreadsheet, read back out of the blob the page handed the anchor.
    const realCreate = URL.createObjectURL;
    const realAnchorClick = HTMLAnchorElement.prototype.click;
    let saved = null;
    URL.createObjectURL = function (blob) { saved = blob; return realCreate.call(URL, blob); };
    HTMLAnchorElement.prototype.click = function () { out.csv_name = this.getAttribute('download'); };
    document.querySelector('[data-download-hours="csv"]').click();
    await new Promise((resolve) => setTimeout(resolve, 200));
    out.csv = saved ? await saved.text() : '';
    URL.createObjectURL = realCreate;
    HTMLAnchorElement.prototype.click = realAnchorClick;

    // The one tick that cannot come off. Everything but Date, then Date as well.
    chosen().split(',').filter((id) => id !== 'date').forEach((id) => box(id).click());
    out.down_to = chosen();
    box('date').click();
    out.floor = chosen();
    out.floor_box_checked = box('date').checked;
    out.floor_toast = Array.from(document.querySelectorAll('.toast')).map((el) => el.textContent);

    WORKER_MODULES.toggleMyHoursColumn = realToggle;
    WORKER_MODULES.saveMyHoursColumns = realSave;
    return out;
})()
"""


def _sign_in(browser, site, role: str, width, tmp_path, *, until: str | None = None, probe=None, after=None):
    """Sign in and return the observation, with the usual assertions applied once."""
    seen = browser_support.sign_in(
        browser, site, IDENTITIES[role], width, until=until, probe=probe, shots=tmp_path, after=after
    )
    assert seen.ran, (
        f"signing in as the {role} did not reach that role's screen.\n{seen.describe()}\n"
        f"A screenshot of what the browser was showing is in {tmp_path}"
    )
    assert seen.refused_scripts == [], (
        f"the {role} session asked for a script and was given something a browser will not "
        f"execute.\n{seen.describe()}"
    )
    assert seen.page_errors == [], f"the {role} session threw while drawing itself.\n{seen.describe()}"
    return seen


@pytest.mark.parametrize(
    "role,width_name",
    sorted(LAYOUTS),
    ids=lambda value: value,
)
def test_each_role_renders_its_own_layout(browser, site, role, width_name, tmp_path):
    """A worker gets the handset, an administrator the console, and each at its own width."""
    own, other = LAYOUTS[(role, width_name)]
    width = browser_support.PHONE if width_name == "phone" else browser_support.DESKTOP
    seen = _sign_in(
        browser,
        site,
        role,
        width,
        tmp_path,
        until=f"document.querySelector({own!r}) !== null",
        probe={
            "own": f"document.querySelector({own!r}) !== null",
            "other": f"document.querySelector({other!r}) !== null",
            "console_host": "document.getElementById('adminContent') !== null",
        },
    )

    assert seen.probes["own"], (
        f"the {role} at {width_name} width did not draw {own}, its own layout.\n{seen.describe()}"
    )
    assert not seen.probes["other"], (
        f"the {role} at {width_name} width still has {other} in the page - the other width's "
        f"layout.\n{seen.describe()}"
    )
    # The width reached the application's own switch, so the layout was chosen rather than
    # inherited from whatever the previous screen happened to leave behind.
    expected_mode = "mobile" if width_name == "phone" else "desktop"
    assert seen.layout_mode == expected_mode, (
        f"the app's own layout switch read {seen.layout_mode!r} for the {role} at {width_name} "
        f"width.\n{seen.describe()}"
    )
    # A worker has no console: not the content host, wherever the module is.
    assert seen.probes["console_host"] is (role == "administrator"), (
        f"the {role} session's #adminContent presence was {seen.probes['console_host']!r}.\n"
        f"{seen.describe()}"
    )


def test_the_phone_console_band_is_just_the_tab_strip(browser, site, tmp_path):
    """The sticky band is one strip tall, and the notch moves it rather than covering it.

    Two separate regressions are pinned here. The first is that the band had grown to carry
    the tab's title, the brand mark and four action buttons - about 149px of a phone
    viewport, held there while an operator reads a table. The second is subtler: the inset
    must be **padding on the sticky element**, so the band's background fills the notch
    while its content sits below it. As a margin it would scroll the strip up under the
    status bar and paint the tabs across the clock.
    """
    seen = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.PHONE,
        tmp_path,
        until="document.querySelector('.admin-mobile-head') !== null",
        probe={"band": THE_PHONE_BAND},
    )
    band = seen.probes["band"]
    assert band.get("found"), (
        f"the phone console drew no .admin-mobile-head/.admin-tabs to measure.\n{seen.describe()}"
    )

    # --- the inset moves the content, and the band covers it ---
    assert band["band_grew_by"] == 47, (
        f"a 47px top safe-area inset changed the band's height by {band['band_grew_by']}px, "
        "so the band is not reading --safe-top and its content sits under the status "
        f"bar.\n{seen.describe()}"
    )
    assert band["content_moved_by"] == 47, (
        f"the inset moved the band by {band['band_grew_by']}px but its content by "
        f"{band['content_moved_by']}px - the tabs are not being pushed clear of the "
        f"notch.\n{seen.describe()}"
    )
    assert band["band_still_starts_at_the_top"], (
        f"the band no longer starts at the top of the viewport: it would leave a gap of "
        f"page background above a sticky element.\n{seen.describe()}"
    )
    assert band["band_is_sticky"], "the band is not sticky any more; the strip would scroll away"

    # --- the band is the strip and nothing else ---
    assert band["band_has_strip"], f"the band has no tab strip.\n{seen.describe()}"
    assert not band["band_has_title"], (
        "the tab's title is still inside the sticky band, so the band is still spending "
        f"viewport height on something that does not need to survive scrolling.\n{seen.describe()}"
    )
    assert not band["band_has_actions"], (
        f"the console's action buttons are still pinned in the band.\n{seen.describe()}"
    )

    # --- ...and they are still on the page, in the frame ---
    assert band["bar_is_in_the_frame"], (
        f"the title/actions bar is not in .admin-frame.\n{seen.describe()}"
    )
    assert band["bar_has_title"] and band["title_present"], (
        f"the tab title was removed rather than moved.\n{seen.describe()}"
    )
    assert band["bar_has_actions"], (
        f"the console's action buttons were removed rather than moved.\n{seen.describe()}"
    )
    assert band["subtitle_present"], (
        "the tab's hint line is gone; the band change should not have touched it.\n"
        f"{seen.describe()}"
    )

    # --- the height is the strip's, not a fraction of the viewport ---
    assert band["band_is_just_the_strip"], (
        f"the band is {band['band_height']}px around a {band['strip_height']}px strip, so "
        f"something else is still inside it.\n{seen.describe()}"
    )
    assert band["band_height"] < 100, (
        f"the phone band is {band['band_height']}px, down from 149px before this change "
        f"but still not just the strip.\n{seen.describe()}"
    )

    # --- the other end: the home indicator ---
    assert band["frame_bottom_inset"] == 34, (
        f"a 34px bottom safe-area inset moved the pane's bottom padding by "
        f"{band['frame_bottom_inset']}px, so the last row of a table can still be painted "
        f"over by the home indicator.\n{seen.describe()}"
    )

    # ``env(safe-area-inset-*)`` is only non-zero on a device when the viewport is allowed
    # to reach the edges; without this the insets above are always 0 on a real phone, which
    # is precisely the failure a browser test on a notchless emulator would not notice.
    index = (harness.PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in index, (
        "index.html no longer opts into the full-bleed viewport, so every safe-area inset "
        "in the stylesheet resolves to 0 on a real phone"
    )


#: The phone console's sticky band, measured in a real browser.
#:
#: The band used to carry the tab's title, the brand mark and the four action buttons as
#: well as the tab strip - about 149px on an iPhone, a sixth of the viewport, held there
#: permanently on a screen whose whole job is showing an operator a table. It is now the
#: strip and nothing else, and the title and the actions scroll away in the frame.
#:
#: **The notch cannot be emulated.** Playwright's device profiles report no
#: ``safe-area-inset-*``, so measuring the header on an emulated iPhone proves nothing
#: about an iPhone. Instead this probe *puts* an inset there by setting the custom property
#: the stylesheet reads, and asserts the geometry moves by exactly that much. That is the
#: only part the CSS is responsible for: ``viewport-fit=cover`` in ``index.html`` is what
#: makes the real inset non-zero on a device, and the test asserts that meta tag separately.
#:
#: Two things are being pinned, and they are different: that the band no longer spends the
#: viewport on the title and the actions, and that the inset moves the *content* while the
#: band's background still covers it - a margin would scroll the strip up under the clock
#: and paint the tabs across it.
THE_PHONE_BAND = r"""
(() => {
    const out = {};
    const root = document.documentElement;
    const head = document.querySelector('.admin-mobile-head');
    const strip = document.querySelector('.admin-tabs');
    const bar = document.querySelector('.admin-mobile-bar');
    const frame = document.querySelector('.admin-frame');
    if (!head || !strip) return { found: false };
    out.found = true;

    const geometry = () => {
        const h = head.getBoundingClientRect();
        const s = strip.getBoundingClientRect();
        return { head: h.height, head_top: h.top, strip_top: s.top, strip: s.height };
    };
    const before = geometry();

    // The console's own notch. 47px is an iPhone 14 Pro's top inset; the value only has to
    // be non-zero and unambiguous, because the assertion is that the geometry moved by it.
    root.style.setProperty('--safe-top', '47px');
    const notched = geometry();
    root.style.removeProperty('--safe-top');

    out.band_grew_by = Math.round(notched.head - before.head);
    out.content_moved_by = Math.round(notched.strip_top - before.strip_top);
    out.band_still_starts_at_the_top = Math.round(before.head_top) === 0;
    out.band_is_sticky = getComputedStyle(head).position === 'sticky';

    // The band is the strip and nothing else.
    out.band_has_strip = head.querySelector('.admin-tabs') !== null;
    out.band_has_title = head.querySelector('#adminTitle') !== null;
    out.band_has_actions = head.querySelector('.admin-actions') !== null;

    // ...and the title and the actions are still on the page, in the frame rather than
    // pinned - the point of the change is that they move, not that they are gone.
    out.title_present = document.getElementById('adminTitle') !== null;
    out.subtitle_present = document.getElementById('adminSubtitle') !== null;
    out.bar_has_title = !!(bar && bar.querySelector('#adminTitle'));
    out.bar_has_actions = !!(bar && bar.querySelector('.admin-actions'));
    out.bar_is_in_the_frame = !!(bar && bar.closest('.admin-frame') !== null);

    // The band is now strip-sized. 149 is what it measured on a phone before the change.
    out.band_height = Math.round(before.head);
    out.strip_height = Math.round(before.strip);
    out.band_is_just_the_strip = Math.abs(before.head - (before.strip + 1)) <= 3;

    // The other end: the home indicator cannot paint over the bottom of the pane.
    const padBefore = parseFloat(getComputedStyle(frame).paddingBottom);
    root.style.setProperty('--safe-bottom', '34px');
    const padAfter = parseFloat(getComputedStyle(frame).paddingBottom);
    root.style.removeProperty('--safe-bottom');
    out.frame_bottom_inset = Math.round(padAfter - padBefore);

    return out;
})()
"""


#: Tap the last tab of the phone's strip and report where the strip left it.
#:
#: The strip is flattened from the rail's own groups, so it is eight tabs - about 820px of
#: them - inside a window of about 290-360px. That makes it a scrollable list that nothing
#: ever scrolled: the tap changed the pane underneath while the band above it went on
#: showing the first two and a half tabs, so the header named a screen that was no longer
#: on screen. Measured before the fix, the lit-up tab sat at x=729 inside a 358px strip.
STRIP_AFTER_THE_LAST_TAB = r"""
(() => {
    const buttons = Array.from(document.querySelectorAll('.admin-tabs button'));
    if (!buttons.length) return { strip: false };
    buttons[buttons.length - 1].click();
    const strip = document.querySelector('.admin-tabs');
    const lit = strip.querySelector('[aria-current="page"]');
    const frame = strip.getBoundingClientRect();
    const box = lit.getBoundingClientRect();
    return {
        strip: true,
        tab: lit.innerText.trim(),
        taps: buttons.length,
        scrolls: strip.scrollWidth > strip.clientWidth + 1,
        scrollLeft: Math.round(strip.scrollLeft),
        left: Math.round(box.left - frame.left),
        right: Math.round(box.right - frame.left),
        width: Math.round(frame.width),
        inside: box.left >= frame.left - 1 && box.right <= frame.right + 1,
    };
})()
"""


def test_the_phone_tab_strip_shows_the_tab_that_is_lit_up(browser, site, tmp_path):
    """The phone's console navigation: the tab you are on is the tab you can see.

    A tab that is off screen is not only a missing label - the tab strip is the only place
    the phone's console says which screen it is showing (the title above it is the same
    words), so a pane the strip cannot name is a screen the operator has to guess at.

    The vacuity guard matters as much as the assertion: if the strip ever stopped
    scrolling - three tabs rather than eight, or a device wide enough to hold them all -
    this test would pass while proving nothing, and it would go on passing if the reveal
    were deleted. ``scrolls`` is what makes the rest of it meaningful.
    """
    tapped = {}

    def tap_the_last_tab(tab):
        tapped["result"] = tab.evaluate(f"({STRIP_AFTER_THE_LAST_TAB})")

    seen = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.PHONE,
        tmp_path,
        after=tap_the_last_tab,
    )

    data = tapped["result"]
    assert data["strip"], f"the phone's console has no tab strip to scroll.\n{seen.describe()}"
    assert data["scrolls"], (
        f"the tab strip fits in its window ({data['width']}px for {data['taps']} tabs), so "
        f"there is nothing to scroll and this test proves nothing.\n{seen.describe()}"
    )
    assert data["inside"], (
        f"after tapping {data['tab']!r} - the last of {data['taps']} tabs - the lit-up tab "
        f"sits at x={data['left']}..{data['right']} in a {data['width']}px strip (scrollLeft "
        f"{data['scrollLeft']}), where it cannot be seen. The band goes on showing whichever "
        f"tab was in view before.\n{seen.describe()}"
    )


def test_a_lead_worker_lands_on_the_handset_and_never_on_the_console(browser, site, tmp_path):
    """The role's own screen, and the one the server would have refused request by request.

    ``admin_only`` names ``admin`` and ``head_admin``, so a console drawn for a ``moallem``
    is a page of 403s - and it was the page they were sent to. The handset is where a lead
    worker clocks in and reads their own hours, and the deferred console module is what
    would have filled the other screen, so its absence is the other half of the proof.
    """
    seen = _sign_in(
        browser,
        site,
        "lead worker",
        browser_support.PHONE,
        tmp_path,
        probe={
            "console_host": "document.getElementById('adminContent') !== null",
            "console_module": "typeof UI_MODULES !== 'undefined'",
            # Their own timesheet, on the tab a worker taps for it - the handset opens on the
            # clock, so this is the same walk the export test does. What it waits for is the
            # tab *answering*, not the summary card: this account has never punched, so the
            # honest screen for it is the one that says so, and the card is not owed to
            # somebody who has no hours to show.
            "history_answered": """(async () => {
                document.querySelector('[data-worker-tab=\"history\"]').click();
                const pane = () => (document.getElementById('historyTable') || {}).innerText || '';
                for (let i = 0; i < 40; i += 1) {
                    if (pane() && !pane().includes('Loading')) return true;
                    await new Promise((resolve) => setTimeout(resolve, 100));
                }
                return false;
            })()""",
        },
    )

    assert seen.probes["console_host"] is False, (
        f"a lead worker was shown the console, which answers 403 to every request it makes "
        f"for that role.\n{seen.describe()}"
    )
    assert seen.probes["console_module"] is False, (
        f"a lead worker's phone fetched the console's module.\n{seen.describe()}"
    )
    assert seen.probes["history_answered"] is True, (
        f"the lead worker's own History tab never answered - the role can clock in and read "
        f"its own hours, and this is the screen where that happens.\n{seen.describe()}"
    )


def test_a_worker_can_take_their_own_timesheet_away_as_a_file_or_a_sheet(browser, site, tmp_path):
    """The two buttons have to be *bound*, which is the one thing a stubbed DOM cannot see.

    ``frontend_vm`` has no HTML parser and no click dispatch, so it calls
    ``WORKER_MODULES.downloadMyHoursCsv()`` directly and can only prove the file's contents.
    Whether the button the worker taps is attached to it is decided by a selector, a
    binding and a real DOM - and a repaint that dropped the binding would leave two buttons
    that look right and do nothing, on the one screen a worker opens to find out whether
    they have been paid for last month.

    Both halves are driven against the running server: the month is set to the seeded
    March 2026 shifts, the CSV is read back out of the blob the page handed the anchor, and
    the PDF is read off the page at the moment the print dialog opens.
    """
    seeded_month = harness.SEEDED_HISTORY_DAYS[0][:7]
    seen = _sign_in(
        browser,
        site,
        "worker",
        browser_support.PHONE,
        tmp_path,
        until=(
            "document.querySelector('[data-worker-tab=\"history\"]') !== null"
            " && document.querySelector('[data-my-hours]') !== null"
        ),
        probe={"take_away": TAKE_THE_TIMESHEET_AWAY.replace("SEEDED_MONTH", seeded_month)},
    )

    taken = seen.probes["take_away"]
    assert taken["buttons"] == ["csv", "pdf"], (
        f"My hours offers {taken['buttons']}, not the two files.\n{seen.describe()}"
    )
    assert taken["printed"] == 1, f"the PDF button is not bound to the dialog.\n{seen.describe()}"
    assert taken["printing"] is True, "the page has to be out of the paper while it prints"
    assert taken["title"].startswith("my_hours_"), taken["title"]
    for expected in ("My hours", harness.SEEDED_HISTORY_DAYS[0], "Downtown Tower A", "00 h"):
        assert expected in taken["sheet"], f"the sheet is missing {expected!r}: {taken['sheet'][:400]}"

    lines = taken["csv"].splitlines()
    assert lines[0] == "Date,Site,Arrival,Break hours,Hours,Approved hours,Status", lines[0]
    assert len(lines) == 3, f"the two seeded shifts, and the header: {lines}"
    assert lines[1].startswith(f"{harness.SEEDED_HISTORY_DAYS[1]},Downtown Tower A,"), lines[1]
    assert taken["csv_name"].startswith("my_hours_") and taken["csv_name"].endswith(".csv"), (
        taken["csv_name"]
    )


def test_an_administrator_prints_one_workers_month_from_the_shifts_tab(browser, site, tmp_path):
    """A manager's sheet for one person, from the row that person's shift is on.

    The console's period sheet is built from the rows already on screen, and ``frontend_vm``
    proves that whole path. This is the other sheet: the action on a shift row, which asks
    the server for one worker's month. Two things are only visible here - the click is bound
    at paint time, and the request leaves the tab's own period behind - so the tab is put on
    a single seeded day and the sheet must still be that day's *month*.
    """
    day = harness.SEEDED_HISTORY_DAYS[0]
    month = day[:7]
    seen = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.DESKTOP,
        tmp_path,
        until=CONSOLE_LANDED,
        probe={"month": PRINT_ONE_WORKER_S_MONTH.replace("SEEDED_DAY", day)},
    )

    printed = seen.probes["month"]
    assert printed["tab"], (
        f"the Shifts tab never drew its period picker.\n{seen.describe()}"
    )
    assert printed["rows_on_screen"] == 1, (
        f"the narrowed period should hold one seeded shift, not {printed['rows_on_screen']}.\n"
        f"{seen.describe()}"
    )
    assert printed["row_date"] == day, printed["row_date"]
    assert printed["row_worker"] == harness.WORKER, printed["row_worker"]
    assert printed["button"] is True, (
        f"no shift row offered the month action.\n{seen.describe()}"
    )
    assert harness.SEED_USERS[harness.WORKER][0] in printed["button_label"], (
        f"the action has to say whose month it prints, and reads {printed['button_label']!r}"
    )
    assert printed["printed"] == 1, (
        f"the row action is not bound to the print dialog.\n{seen.describe()}"
    )
    assert printed["printing"] is True, "the console has to be out of the paper while it prints"

    assert len(printed["month_requests"]) == 1, (
        f"one request, for one worker's month. Got {printed['month_requests']}"
    )
    asked = printed["month_requests"][0]
    assert f"worker_id={harness.WORKER}" in asked, (
        f"the sheet is not for the worker on the row that was clicked: {asked}"
    )
    assert f"start={month}-01" in asked and f"end={month}-31" in asked, (
        f"the request is the tab's own day, not that worker's month: {asked}"
    )

    assert printed["title"] == f"timesheet_seed-worker_{month}-01_{month}-31", (
        f"the dialog offers {printed['title']!r}"
    )
    for expected in (
        harness.SEED_USERS[harness.WORKER][0],  # whose sheet it is, once, above the table
        harness.SEEDED_HISTORY_DAYS[0],         # both seeded days, though one was on screen
        harness.SEEDED_HISTORY_DAYS[1],
        harness.DOWNTOWN,
    ):
        assert expected in printed["sheet"], (
            f"the sheet is missing {expected!r}: {printed['sheet'][:400]}"
        )
    assert printed["after"]["printing"] is False, "the page was left out of its own print rules"
    assert printed["after"]["sheets"] == 0, "the sheet was left in the page"
    assert printed["after"]["title"] == "Al-Jehad - Site Attendance", printed["after"]["title"]


def test_an_administrator_reaches_their_own_capture_from_the_console(browser, site, tmp_path):
    """The console half of self-enrollment, in the only place the binding is observable.

    The backend suite proves the endpoint writes a template the punch will then match (see
    ``test_admin_self_enrollment``), and ``frontend_vm`` proves the request carries a photo
    and no account id. What neither can show is that the button on the administrator's own
    row is *connected*: the roster is repainted from a string on every search, every save
    and every tab change, and a repaint that dropped the listener would leave a control
    that looks right and does nothing - which is exactly the bug that shipped, in the other
    direction, when the console had no control here at all.
    """
    seen = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.DESKTOP,
        tmp_path,
        until=CONSOLE_LANDED,
        probe={"enroll": ENROLL_MY_OWN_FACE},
    )

    enroll = seen.probes["enroll"]
    assert enroll["tab"] and enroll["roster"], (
        f"the Credentials tab never drew its roster, so this proves nothing.\n{seen.describe()}"
    )
    assert enroll["buttons"] == 1, (
        f"exactly one row may offer the capture - the reader's own. Found {enroll['buttons']} "
        f"on the roster.\n{seen.describe()}"
    )
    assert enroll["mine"] is True, (
        f"the enroll button is on row {enroll['row']}, not the signed-in "
        f"administrator's own.\n{seen.describe()}"
    )
    assert enroll["row"] == harness.ADMIN, enroll["row"]
    assert enroll["label"], "the button has no words on it"
    assert enroll["is_replace"] is True, (
        f"a translated label was expected; the button reads {enroll['label']!r}"
    )
    assert enroll["face_cell"] in ("enrolled", "missing"), enroll["face_cell"]

    assert enroll["overlay"] is True, (
        f"the button is not bound to the capture: nothing opened on the tap.\n{seen.describe()}"
    )
    assert enroll["camera_starts"] == 1, (
        f"the tap did not reach the camera ({enroll['camera_starts']} streams requested).\n"
        f"{seen.describe()}"
    )
    assert enroll["shutter"] is True, "the capture overlay has no shutter"
    assert enroll["title_is_its_own"] is True, (
        f"the overlay says {enroll['says']!r}, which is not this screen's own title"
    )
    assert enroll["closed"] is True, "the capture camera was left in the page"


def test_an_administrator_replaces_a_workers_photo_from_the_account_editor(browser, site, tmp_path):
    """Any worker's reference photo, from the panel that edits their account.

    The backend suite proves the endpoint writes what a punch is then matched against, and
    the sweep of ``/admin/enroll`` proves who may write whose (``test_admin_user_management``);
    ``frontend_vm`` proves the two requests a save sends. What none of them can show is that
    the picker on this panel is *connected* - and that is the failure worth a browser: the
    panel is rebuilt from a string on every repaint, so a listener that was never attached
    would leave a control that looks exactly like a working one.
    """
    seen = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.DESKTOP,
        tmp_path,
        until=CONSOLE_LANDED,
        probe={"photo": EDIT_REFERENCE_PHOTO},
    )

    photo = seen.probes["photo"]
    assert photo["tab"] and photo["roster"], (
        f"the Credentials tab never drew its roster, so this proves nothing.\n{seen.describe()}"
    )
    assert photo["row"] is not None, (
        f"no row offered Edit, so the panel this is about never opened.\n{seen.describe()}"
    )
    assert photo["own_row"] is False, (
        "this has to be somebody else's account: on your own row it would be the same job the "
        "roster's own capture button already does"
    )
    assert photo["panel_for"] == photo["row"], (
        f"the panel opened for {photo['panel_for']!r} while the row was {photo['row']!r}"
    )
    assert photo["picker"] is True, "the edit panel draws no photo picker at all"
    assert photo["picker_for"] == photo["row"], (
        f"the picker names {photo['picker_for']!r}, which is not the account being edited"
    )
    assert photo["face_state"] in ("enrolled", "missing"), (
        f"the panel does not say which state the photo is in ({photo['face_state']!r})"
    )

    assert photo["handler_calls"] == 1, (
        f"the change event never reached the module: the picker is drawn and dead.\n"
        f"{seen.describe()}"
    )
    assert photo["handler_target"] == "userEditPhoto", photo["handler_target"]
    assert photo["held"] == "ana.jpg", (
        f"the chosen file was not held for the save to send ({photo['held']!r})"
    )
    assert photo["chosen"] is True, "the panel does not show which photo was chosen"
    assert photo["clear_offered"] is True, "and there is no way to take it back off"
    assert photo["panel_closed"] is True, "the edit panel was left open in the page"


def test_a_worker_chooses_the_columns_in_their_own_files(browser, site, tmp_path):
    """The chooser has to be *bound*, and the choice has to reach both files.

    ``frontend_vm`` proves the vocabulary, the refusals and the request; what it cannot prove
    is that the boxes the card draws are connected to them, and that is the failure worth a
    browser - the summary is rebuilt from a string on every repaint, so a binding that was
    never re-attached would leave a control that looks exactly like a working one.

    The same walk also pins the account's own copy of the choice: Save is a real POST to the
    running server, and what the screen keeps afterwards is the server's answer rather than
    the list it sent.
    """
    seeded_month = harness.SEEDED_HISTORY_DAYS[0][:7]
    seen = _sign_in(
        browser,
        site,
        "worker",
        browser_support.PHONE,
        tmp_path,
        until=(
            "document.querySelector('[data-worker-tab=\"history\"]') !== null"
            " && document.querySelector('[data-my-hours]') !== null"
        ),
        probe={"columns": CHOOSE_THE_COLUMNS.replace("SEEDED_MONTH", seeded_month)},
    )

    columns = seen.probes["columns"]
    assert columns["chooser"] is True, (
        f"My hours draws no column chooser at all.\n{seen.describe()}"
    )
    assert columns["seeded_rows"] is True, (
        f"the seeded month never loaded, so this proves nothing about the files.\n{seen.describe()}"
    )
    assert columns["boxes"] == [
        "date", "site", "arrival", "break", "hours", "recorded", "approved", "status"
    ], columns["boxes"]
    assert columns["ticked_at_start"] == [
        "date", "site", "arrival", "break", "hours", "approved", "status"
    ], f"an account that has never chosen opens on the file it always got: {columns['ticked_at_start']}"

    # The click reached the module, as that module: the untick is the browser's, the effect
    # is the module's, and neither half alone is the thing under test.
    assert columns["untick_calls"] == "break:false", (
        f"clicking the box called the module with {columns['untick_calls']!r}.\n{seen.describe()}"
    )
    assert columns["after_untick"] == "date,site,arrival,hours,approved,status", columns["after_untick"]

    # Save: one tap, one request, and the server's own list kept afterwards.
    assert columns["save_calls"] == 1, f"the Save button is not bound.\n{seen.describe()}"
    assert columns["stored"] == "date,site,arrival,hours,approved,status", (
        f"the answer was not kept ({columns['stored']!r})\n{seen.describe()}"
    )
    assert columns["after_save"] == "date,site,arrival,hours,approved,status", columns["after_save"]
    assert any("saved" in line.lower() for line in columns["toast"]), columns["toast"]

    # Both files carry the choice, in the shape the worker set.
    assert columns["printed"] == 1, f"the PDF button is not bound.\n{seen.describe()}"
    assert "Break" not in columns["sheet"], (
        f"the sheet still carries the column that was unticked: {columns['sheet'][:300]}"
    )
    # The sheet's own innerText: cells run together without separators, so each heading is
    # looked for by name, which is what a reader does with the paper anyway.
    for heading in ("Date", "Site", "Arrival", "Hours", "Approved", "Status"):
        assert heading in columns["sheet"], (
            f"the sheet lost the {heading!r} column: {columns['sheet'][:400]}"
        )

    csv_lines = columns["csv"].splitlines()
    assert csv_lines[0] == "Date,Site,Arrival,Hours,Approved hours,Status", csv_lines[0]
    assert len(csv_lines) == 3, f"the two seeded shifts, and the header: {csv_lines}"
    assert columns["csv_name"].startswith("my_hours_"), columns["csv_name"]

    # And the one tick that cannot come off: the box is put back and the screen says why.
    assert columns["down_to"] == "date", columns["down_to"]
    assert columns["floor"] == "date", columns["floor"]
    assert columns["floor_box_checked"] is True, (
        f"the refused untick left the box unchecked, so the file and the control now "
        f"disagree.\n{seen.describe()}"
    )
    assert any("least one column" in line for line in columns["floor_toast"]), columns["floor_toast"]


#: The notice's whole journey, driven the way a thumb drives it: what the clock panel says,
#: the band that leads into the inbox, the notice, and the tap that reads it.
#:
#: Every step waits for the effect rather than sleeping, and each wait *reports itself* - a
#: badge that never arrives has to read as "no badge", not as "the page was still loading".
THE_INBOX_JOURNEY = r"""
(async () => {
    const out = {};
    const wait = async (test, ms = 6000) => {
        const started = Date.now();
        while (Date.now() - started < ms) {
            if (test()) return true;
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        return false;
    };
    const text = (el) => (el ? el.innerText : null);
    const badge = () => document.querySelector('.hand-tabs [data-alert-count]');

    // 1. The clock panel, before anything is tapped: the count on the tab and the band
    //    above the button. The band is *above* the button on purpose - see the suite that
    //    pins the markup order - so what is checked here is that it is on this screen at all.
    out.badge_arrived = await wait(() => badge() !== null);
    out.badge = badge() ? badge().innerText.trim() : null;
    out.band_ready = await wait(() => document.querySelector('[data-alerts-banner]') !== null);
    out.band = text(document.querySelector('[data-alerts-banner]'));
    // The punch card's own control, and the action it is wired to - not its wording, which
    // depends on whether this worker happens to be on shift.
    const clockButton = document.querySelector('.hand-clock');
    out.button = text(clockButton);
    out.button_action = clockButton
        ? (/handleClock\('([^']+)'\)/.exec(clockButton.getAttribute('onclick') || '') || [])[1] : null;

    // 2. The band is the way in, and it lands on the inbox with the notice in it.
    const band = document.querySelector('[data-alerts-banner]');
    if (band) band.click();
    out.inbox_opened = await wait(() => document.querySelector('#workerAlerts [data-alert]') !== null);
    out.open_tab = text(document.querySelector('.hand-tabs [aria-current="page"]'));
    out.inbox = text(document.querySelector('#workerAlerts'));
    out.badge_on_open = badge() ? badge().innerText.trim() : null;

    // 3. An unread notice is a control and says what tapping it does.
    const row = document.querySelector('#workerAlerts button[data-alert]');
    out.row_is_a_button = row !== null;
    out.row_label = row ? text(row.querySelector('.hand-alert-more')) : null;
    out.row_chip = row ? text(row.querySelector('.ui-badge')) : null;
    if (row) row.click();

    // 4. Reading it clears both badges and turns the row back into a plain card.
    out.cleared = await wait(() => badge() === null);
    out.row_after = (document.querySelector('#workerAlerts [data-alert]') || {}).tagName || null;
    out.chip_after = document.querySelector('#workerAlerts .ui-badge.is-danger') !== null;
    out.toast = Array.from(document.querySelectorAll('.toast')).map((el) => el.innerText);
    return out;
})()
"""


@pytest.fixture
def a_notice_the_backend_wrote(app_module):
    """One notice, written by the function the watcher writes it with.

    Not a row inserted by hand: ``notifications.notify_worker`` is the call
    ``overtime.scan_auto_close`` makes inside the transaction that closes a shift, with the
    kind and the sentence that goes with it - so what the handset is asked to show is what
    the backend really produces. (The watcher's end of that journey, including the figures in
    the sentence, is pinned by ``test_day_end_precedence.py``.)
    """
    with app_module.db(write=True) as conn:
        written = notifications.notify_worker(
            conn,
            worker_id=harness.WORKER,
            kind=notifications.KIND_WORKER_SHIFT_AUTO_CLOSED,
            title="Your shift was closed at the paid day",
            body="8.50h on site - 0.50h unpaid break = 8.00h payable.",
            dedupe_key="browser-alerts-inbox",
        )
    assert written, "the notice was not written, so the handset has nothing to show"


def test_a_worker_reads_the_notice_the_backend_wrote(browser, site, client, tmp_path, a_notice_the_backend_wrote):
    """The record the backend keeps, on the phone of the person it is about.

    The whole point of the notice is that it reaches somebody: an auto-close writes one
    precisely so the worker is not left to discover the closed day when the *next* clock-out
    refuses them. Until this screen existed the record had no reader at all, so this walks the
    journey in a browser - the badge on the tab, the band above the button, the notice, the
    tap - and then asks the server whether the tap actually landed. A stub DOM can prove the
    request; only this can prove the row came back read.
    """
    journey = {}

    def walk(tab):
        journey.update(tab.evaluate(f"({THE_INBOX_JOURNEY})"))

    seen = _sign_in(
        browser,
        site,
        "worker",
        browser_support.PHONE,
        tmp_path,
        until="document.querySelector('.hand-tabs') !== null",
        after=walk,
    )

    assert journey.get("badge_arrived"), (
        f"nothing ever badged the alerts tab, so the notice the server is holding for this "
        f"worker is invisible.\n{seen.describe()}"
    )
    assert journey["badge"] == "1", journey
    assert journey.get("band_ready"), (
        f"the clock panel drew no band above the clock button.\n{seen.describe()}"
    )
    assert "1 new alert" in journey["band"], journey["band"]
    assert "Your shift was closed at the paid day" in journey["band"], (
        f"the band says how many without saying what:\n{journey['band']}"
    )
    assert "See all" in journey["band"], journey["band"]
    assert journey["button_action"] in ("Clock In", "Clock Out"), (
        f"the band replaced the punch card or left it unwired: {journey['button']!r} calls "
        f"handleClock({journey['button_action']!r})"
    )

    assert journey["inbox_opened"], (
        f"tapping the band did not open an inbox with the notice in it.\n{seen.describe()}"
    )
    assert journey["open_tab"] and "Alert" in journey["open_tab"], journey["open_tab"]
    assert "8.00h payable" in journey["inbox"], (
        f"the inbox does not carry the server's own sentence:\n{journey['inbox']}"
    )
    assert journey["badge_on_open"] == "1", journey

    assert journey["row_is_a_button"], (
        f"an unread notice is not a control, so there is no way to read it.\n{seen.describe()}"
    )
    assert journey["row_label"] and "Mark as read" in journey["row_label"], journey
    assert journey["row_chip"] and "New" in journey["row_chip"], journey

    assert journey["cleared"], (
        f"the notice was tapped and the badge is still there.\n{seen.describe()}"
    )
    assert journey["row_after"] == "DIV", (
        f"a read notice is still offered as something to tap: {journey['row_after']}"
    )
    assert journey["chip_after"] is False, journey
    assert any("marked as read" in line.lower() for line in journey["toast"]), journey["toast"]

    # And the tap reached the database, through the route that reads the id from the query
    # string: a body it does not parse would have been answered like "no id" and marked the
    # whole inbox - which, with one notice here, this would not tell apart. It is pinned by
    # name in ``test_frontend_worker_alerts.py``.
    inbox = client.get("/api/v1/worker/me/notifications", headers=bearer(harness.WORKER)).json()
    assert inbox["unread"] == 0, (
        f"the browser tapped the notice and the server still counts it unread: {inbox['notifications']}"
    )
    assert [row["read"] for row in inbox["notifications"]] == [True], inbox["notifications"]


def test_the_console_module_is_fetched_on_demand_and_only_by_the_console(browser, site, tmp_path):
    """The console's module is deferred, fetched after sign-in, and never a worker's cost.

    Three observations of one fact. The anonymous visit pins that the module is *not* in the
    initial payload - which is what makes the next two meaningful rather than "it happens to
    be cached by now". The administrator's fetch pins that deferring it did not strand the
    console without its code. The worker's pin is the point of the whole change.
    """
    shell = next(p for p in browser_support.PAGES if p.name == "app shell")
    anonymous = browser_support.boot(browser, site, shell, browser_support.PHONE, shots=tmp_path)
    assert anonymous.ran, f"the shell did not boot, so this proves nothing:\n{anonymous.describe()}"
    assert not anonymous.fetched(browser_support.CONSOLE_MODULE), (
        f"the console's module was requested before anybody signed in, so it is in the "
        f"initial payload again and every worker's phone pays for it.\n{anonymous.describe()}"
    )

    admin = _sign_in(
        browser,
        site,
        "administrator",
        browser_support.DESKTOP,
        tmp_path,
        probe={"module": "typeof UI_MODULES !== 'undefined'"},
    )
    assert admin.fetched(browser_support.CONSOLE_MODULE), (
        f"the administrator's console painted without the browser ever requesting "
        f"{browser_support.CONSOLE_MODULE}: either it is in index.html after all, or the "
        f"console is drawing from something else.\n{admin.describe()}"
    )
    assert admin.probes["module"], (
        f"the console fetched {browser_support.CONSOLE_MODULE} but UI_MODULES is still "
        f"undefined - the module loaded and did not define itself.\n{admin.describe()}"
    )

    worker = _sign_in(
        browser,
        site,
        "worker",
        browser_support.PHONE,
        tmp_path,
        probe={"module": "typeof UI_MODULES !== 'undefined'"},
    )
    assert not worker.fetched(browser_support.CONSOLE_MODULE), (
        f"a worker's phone downloaded {browser_support.CONSOLE_MODULE}, which no worker "
        f"screen can use.\n{worker.describe()}"
    )
    assert not worker.probes["module"], (
        f"UI_MODULES exists in a worker's session.\n{worker.describe()}"
    )
