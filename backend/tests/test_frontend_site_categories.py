"""Site categories in the console: the chips on the Shifts board, and the forms that feed them.

WHY THIS EXISTS
---------------
A category is a class of sites - a warehouse, a factory, a project - that carries the clock-in
window for every site inside it (``shift_windows``). The backend half is pinned in
``test_site_categories``. This is the console half, and it is the half an operator actually
touches:

* the Shifts board offers a chip per category **present in the period**, with the number of
  shifts behind it, because a chip that filters to an empty table is a dead end and a count is
  the first thing a reader checks a filter against;
* the search box finds a shift by its site's category, so an operator can type the word they
  think in ("مخزن") rather than the name of a site they have to remember;
* a category travels in the link, as the period and the search do: a link that dropped it would
  hand a colleague a *wider* table than the one it was copied from, under the same dates;
* the Sites tab can put a site into a category and take it out again, and the save says which
  it is doing - an absent control and a cleared one are different answers;
* the categories themselves can be added, renamed, retuned and deleted, and each row says how
  many sites follow it, which is the number that decides whether an edit here is a correction
  or a retune of a whole class of buildings.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------
//
// Two categories - one with hours and two sites in it, one empty and untuned - because the
// chips, the panel and the picker all have to answer "and what about the empty one?".
const CATEGORIES = [
    {
        category_id: 1, name: 'مخزن',
        clock_in_window_start: '07:00', clock_in_window_end: '15:00', site_timezone: null,
        site_count: 2, created_at: '2026-09-26 08:00:00', updated_at: '2026-09-26 08:00:00'
    },
    {
        category_id: 2, name: 'مصنع',
        clock_in_window_start: null, clock_in_window_end: null, site_timezone: null,
        site_count: 0, created_at: '2026-09-26 08:00:00', updated_at: '2026-09-26 08:00:00'
    }
];

function windowInfo(overrides) {
    return Object.assign({
        clock_in_window_start: '07:00', clock_in_window_end: '15:00', site_timezone: 'Asia/Kuwait',
        window: '07:00-15:00', crosses_midnight: false, site_specific: false,
        category: null, category_specific: false,
        source: { clock_in_window_start: 'global', clock_in_window_end: 'global', site_timezone: 'global' }
    }, overrides || {});
}

const SITES = [
    {
        site_name: 'Downtown Tower A', lat: 30.05, lon: 31.23, radius: 65,
        clock_in_window_start: null, clock_in_window_end: null, site_timezone: null,
        category_id: 1, category: 'مخزن',
        window: windowInfo({
            category: 'مخزن', category_specific: true,
            source: { clock_in_window_start: 'category', clock_in_window_end: 'category', site_timezone: 'global' }
        })
    },
    {
        site_name: 'New Capital Zone B', lat: 29.98, lon: 31.75, radius: 100,
        clock_in_window_start: null, clock_in_window_end: null, site_timezone: null,
        category_id: null, category: null,
        window: windowInfo({})
    }
];

// One shift, with everything the timesheet row carries. ``site_category`` is the field the
// chips and the search are built from, so it is on every row rather than looked up.
function shift(overrides) {
    return Object.assign({
        log_id: 900, date: '2026-09-07', timestamp: '2026-09-07 16:02:11',
        worker_id: '600', worker_name: 'Seed Lead', role: 'moallem',
        site_name: 'Downtown Tower A', site_category: 'مخزن',
        hours: 8, recorded_hours: 8.5, approved_hours: 8, awaiting_approval_hours: 0,
        break_hours: 0.5, status_code: 'approved', status: 'Approved by Admin',
        awaiting_approval: false, open_notes: 0,
        arrival_time: '2026-09-07 04:20:00', arrival_verdict: 'on_time', arrival_minutes: 0
    }, overrides || {});
}

// Two warehouse shifts, one factory shift and one at a site in no category: four rows, so
// "the chip narrowed the table" is a claim with a wrong answer as well as a right one.
const ROWS = [
    shift({ log_id: 901, worker_id: '600', worker_name: 'Seed Lead' }),
    shift({ log_id: 902, worker_id: '1', worker_name: 'Seed Worker', role: 'worker' }),
    shift({ log_id: 903, worker_id: '601', worker_name: 'Factory Hand', site_name: 'Factory North', site_category: 'مصنع' }),
    shift({ log_id: 904, worker_id: '602', site_name: 'New Capital Zone B', site_category: null })
];

const REPORT = {
    rows: ROWS,
    totals: {
        shifts: 4, workers: 4, hours: 32, approved_hours: 32, awaiting_approval_hours: 0,
        awaiting_approval: 0, break_hours: 2, workers_with_open_notes: 0, late_arrivals: 0
    },
    categories: [
        { name: 'مخزن', shifts: 2, hours: 16 },
        { name: 'مصنع', shifts: 1, hours: 8 }
    ],
    period: { start: '2026-09-01', end: '2026-09-26' }
};

const calls = [];

function responders(url, init) {
    const method = (init && init.method) || 'GET';
    const record = (kind) => calls.push({ kind: kind, url: String(url), method: method, body: init.body });
    // The categories are matched before the sites: '/admin/site_categories' contains neither
    // '/admin/sites/add' nor '/admin/sites/edit', but the order is the habit worth keeping.
    if (url.indexOf('/admin/site_categories/') >= 0) {
        record(url.indexOf('/delete') >= 0 ? 'category_delete' : 'category_save');
        return { status: 200, body: { status: 'success', message: 'Saved.', category_id: 3 } };
    }
    if (url.indexOf('/admin/site_categories') >= 0) return { status: 200, body: CATEGORIES };
    if (url.indexOf('/admin/sites/edit') >= 0) {
        record('site_edit');
        return { status: 200, body: { status: 'success', message: 'Site updated.' } };
    }
    if (url.indexOf('/admin/sites/add') >= 0) {
        record('site_add');
        return { status: 200, body: { status: 'success', message: 'Site added.' } };
    }
    if (url.indexOf('/admin/sites') >= 0) return { status: 200, body: SITES };
    if (url.indexOf('/admin/reports/shifts') >= 0) {
        record('report');
        return { status: 200, body: REPORT };
    }
    if (url.indexOf('/developer/notifications') >= 0) return { status: 200, body: { unread: 0, notifications: [] } };
    return { status: 200, body: {} };
}

const ADMIN = { id: '5000', name: 'Head Admin', role: 'head_admin', token: 'tok-5000' };

function adminEnv() {
    calls.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(ADMIN) + ')');
    return env;
}

function markupOf(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function rowsShown(env) {
    return (markupOf(env).match(/data-shift="/g) || []).length;
}

function chipsOf(markup) {
    const pattern = /data-category="([^"]*)"\s*data-active="(true|false)"[^>]*aria-pressed="(true|false)"[^>]*>([^<]*)</g;
    const found = [];
    let match;
    while ((match = pattern.exec(markup)) !== null) {
        found.push({ value: match[1], active: match[2] === 'true', pressed: match[3] === 'true', label: match[4] });
    }
    return found;
}

function filterNote(env) {
    const match = /data-filter-note[^>]*>([\s\S]*?)<\/p>/.exec(markupOf(env));
    return match ? match[1].replace(/\s+/g, ' ').trim() : null;
}

function cardFact(markup, siteName) {
    const match = new RegExp('data-site-category="' + siteName + '">([^<]*)<').exec(markup);
    return match ? match[1] : null;
}

const results = {};

// 1. the board the tab opens on
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Shifts')");
    const markup = markupOf(env);
    results.board = {
        chips: chipsOf(markup),
        rows: rowsShown(env),
        // The chip row belongs to the period picker's own row, not to the table below it: this
        // is where "what am I looking at" is answered on this tab.
        chips_in_the_preset_row: /data-preset="thisWeek"[\s\S]*?data-category="/.test(markup),
        has_copy_link: markup.indexOf('data-copy-link') >= 0
    };
}

// 2. one tap on a category narrows the rows, and does not re-ask the server
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Shifts')");
    const readsBefore = env.requests.filter((r) => r.url.indexOf('/admin/reports/shifts') >= 0).length;
    await env.evaluate("UI_MODULES.setShiftsCategory('مخزن')");
    results.one_category = {
        rows: rowsShown(env),
        chips: chipsOf(markupOf(env)),
        note: filterNote(env),
        reads_before: readsBefore,
        reads_after: env.requests.filter((r) => r.url.indexOf('/admin/reports/shifts') >= 0).length,
        total_hours_card: /data-total="hours"[^>]*>[\s\S]*?data-value="([^"]*)"/.exec(markupOf(env)) ? RegExp.$1 : null
    };
}

// 3. the search box finds a shift by the category of the site it was worked at
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Shifts')");
    env.evaluate("document.getElementById('shiftsQuery').value = 'مخزن'");
    await env.evaluate("UI_MODULES.applyShiftsSearch()");
    results.search_by_category = {
        rows: rowsShown(env),
        note: filterNote(env)
    };

    // ...and a category with a search on top of it narrows further, rather than replacing it.
    await env.evaluate("UI_MODULES.setShiftsCategory('مصنع')");
    env.evaluate("document.getElementById('shiftsQuery').value = 'Seed Lead'");
    await env.evaluate("UI_MODULES.applyShiftsSearch()");
    results.category_and_search = { rows: rowsShown(env), note: filterNote(env) };
}

// 4. the category travels in the link, and a link is honoured at boot
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Shifts')");
    await env.evaluate("UI_MODULES.setShiftsCategory('مخزن')");
    const url = env.evaluate("UI_MODULES.shiftsUrl({ start: '2026-09-01', end: '2026-09-26' }, '')");

    const linked = adminEnv();
    linked.evaluate("window.location.hash = '#shifts=2026-09-01..2026-09-26&c=' + encodeURIComponent('مخزن')");
    const adopted = linked.evaluate('UI_MODULES.adoptShiftsRangeFromUrl()');
    await linked.evaluate("UI.renderAdminTab('Shifts')");
    results.link = {
        url: url,
        adopted: adopted === true,
        category: linked.evaluate('UI_MODULES.shiftsCategory()'),
        rows: rowsShown(linked)
    };
}

// 5. the sites screen: the category on the card, in both forms, and in the notice
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    const markup = markupOf(env);
    env.evaluate('UI_MODULES.openSiteEdit("New Capital Zone B")');
    const editor = markupOf(env);
    results.sites = {
        categorised_card: cardFact(markup, 'Downtown Tower A'),
        uncategorised_card: cardFact(markup, 'New Capital Zone B'),
        from_category: markup.indexOf(env.evaluate("I18n.__('sitesWindowFromCategory').replace('{category}', 'مخزن')")) >= 0,
        add_has_picker: markup.indexOf('id="siteCategory"') >= 0,
        add_picker_options: (markup.match(/id="siteCategory"[\s\S]*?<\/select>/) || [''])[0].indexOf('>مخزن<') >= 0,
        edit_has_picker: editor.indexOf('id="editSiteCategory"') >= 0,
        edit_picker_shows_none: /<option value=""[^>]*selected/.test(editor),
        panel: /id="siteCategoriesPanel"/.test(markup),
        panel_rows: (markup.match(/data-category-row="/g) || []).length,
        panel_names_hours_and_reach: markup.indexOf('مخزن') >= 0
            && markup.indexOf('07:00-15:00') >= 0
            && markup.indexOf(env.evaluate("I18n.__('sitesCategorySiteCount').replace('{count}', '2')")) >= 0,
        panel_labels_the_empty_category: markup.indexOf(env.evaluate("I18n.__('sitesCategoryNoHours')")) >= 0,
        // Handler text is never built from a category name: the buttons carry ids, and the
        // panel is delegated - a name is operator data, and a name in an attribute is the sink
        // the document CSP exists to close.
        panel_has_no_inline_handler: !/data-category-(edit|delete)="[^"]*"[^>]*onclick=/.test(markup)
    };
}

// 6. what each save sends about the category
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    env.evaluate("document.getElementById('siteName').value = 'Warehouse West'");
    env.evaluate("document.getElementById('location').value = '30.10,31.40'");
    env.evaluate("document.getElementById('radius').value = '80'");
    env.evaluate("document.getElementById('siteCategory').value = '1'");
    await env.evaluate("document.getElementById('addSiteForm').onsubmit({ preventDefault: function () {} })");
    results.add_into_category = JSON.parse(calls[calls.length - 1].body);

    env.evaluate("document.getElementById('siteName').value = 'Yard East'");
    env.evaluate("document.getElementById('location').value = '30.11,31.41'");
    env.evaluate("document.getElementById('radius').value = '80'");
    env.evaluate("document.getElementById('siteCategory').value = ''");
    await env.evaluate("document.getElementById('addSiteForm').onsubmit({ preventDefault: function () {} })");
    results.add_without_category = JSON.parse(calls[calls.length - 1].body);

    env.evaluate('UI_MODULES.openSiteEdit("Downtown Tower A")');
    env.evaluate("document.getElementById('editSiteCategory').value = ''");
    await env.evaluate("document.getElementById('editSiteForm').onsubmit({ preventDefault: function () {} })");
    results.move_out_of_category = JSON.parse(calls[calls.length - 1].body);
}

// 7. the categories panel's own save and delete
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    env.evaluate("document.getElementById('siteCategoryName').value = 'مشاريع'");
    env.evaluate("document.getElementById('siteCategoryStart').value = '06:00'");
    env.evaluate("document.getElementById('siteCategoryEnd').value = '14:00'");
    await env.evaluate("document.getElementById('siteCategoryForm').onsubmit({ preventDefault: function () {} })");
    results.category_added = {
        kind: calls[calls.length - 1].kind,
        body: JSON.parse(calls[calls.length - 1].body)
    };

    // An edit sends the id, so a rename and a retune are the same request - and the hours a
    // blank box clears are sent as nulls rather than omitted.
    env.evaluate('UI_MODULES.openCategoryEdit(1)');
    const editing = markupOf(env);
    env.evaluate("document.getElementById('siteCategoryName').value = 'المخزن'");
    env.evaluate("document.getElementById('siteCategoryStart').value = ''");
    env.evaluate("document.getElementById('siteCategoryEnd').value = ''");
    await env.evaluate("document.getElementById('siteCategoryForm').onsubmit({ preventDefault: function () {} })");
    results.category_edited = {
        kind: calls[calls.length - 1].kind,
        name_field_prefilled: /id="siteCategoryName"[^>]*value="مخزن"/.test(editing),
        hours_prefilled: /id="siteCategoryStart"[^>]*value="07:00"/.test(editing),
        body: JSON.parse(calls[calls.length - 1].body)
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


# ---------------------------------------------------------------------------
# the board
# ---------------------------------------------------------------------------
def test_the_board_offers_a_chip_for_every_category_in_the_period(results):
    board = results["board"]
    labels = {chip["label"]: chip for chip in board["chips"]}
    assert labels["مخزن (2)"]["value"] == "مخزن"
    assert labels["مصنع (1)"]["value"] == "مصنع"
    # The count is the shifts behind the chip, and the "all" chip counts every row on screen.
    assert any(chip["label"] == "All categories (4)" for chip in board["chips"]), board["chips"]
    assert len(board["chips"]) == 3, board["chips"]


def test_the_all_chip_is_the_one_pressed_when_the_tab_opens(results):
    chips = results["board"]["chips"]
    pressed = [chip for chip in chips if chip["pressed"]]
    assert len(pressed) == 1 and pressed[0]["value"] == "", chips
    assert all(chip["active"] == chip["pressed"] for chip in chips), chips
    assert results["board"]["rows"] == 4, "and nothing is filtered away yet"


def test_the_chips_sit_in_the_period_row_rather_than_below_the_table(results):
    """The red square in the brief: the row that answers \"what am I looking at\"."""
    assert results["board"]["chips_in_the_preset_row"], (
        "the category chips must be in the preset row, not down beside the table"
    )
    assert results["board"]["has_copy_link"], "and the rest of that row is still there"


def test_choosing_a_category_narrows_the_rows_and_names_it_in_the_note(results):
    narrowed = results["one_category"]
    assert narrowed["rows"] == 2, "the two warehouse shifts, and neither of the others"
    pressed = [chip for chip in narrowed["chips"] if chip["pressed"]]
    assert len(pressed) == 1 and pressed[0]["value"] == "مخزن", narrowed["chips"]
    assert "مخزن" in (narrowed["note"] or ""), narrowed["note"]
    assert "2 / 4" in (narrowed["note"] or ""), (
        f"the note has to say the figures are over part of the period: {narrowed['note']}"
    )


def test_narrowing_is_a_repaint_and_not_another_request(results):
    """The figures on screen are the same ones the server sent: no round trip to filter."""
    narrowed = results["one_category"]
    assert narrowed["reads_after"] == narrowed["reads_before"], (
        "a category filter re-read the server: it should filter the report already on screen"
    )


def test_the_search_box_finds_a_shift_by_its_site_category(results):
    """An operator types the word they think in - the warehouse - not a site name."""
    found = results["search_by_category"]
    assert found["rows"] == 2, found
    assert "مخزن" in (found["note"] or ""), found["note"]


def test_a_category_and_a_search_narrow_together(results):
    both = results["category_and_search"]
    assert both["rows"] == 0, (
        "the factory's shift is not the one Seed Lead worked at a warehouse, so nothing matches"
    )
    note = both["note"] or ""
    assert "مصنع" in note and "Seed Lead" in note, note


# ---------------------------------------------------------------------------
# the link
# ---------------------------------------------------------------------------
def test_the_link_carries_the_category(results):
    assert "c=%D9%85%D8%AE%D8%B2%D9%86" in results["link"]["url"], results["link"]["url"]


def test_a_shared_link_opens_on_the_category_it_named(results):
    link = results["link"]
    assert link["adopted"] is True
    assert link["category"] == "مخزن"
    assert link["rows"] == 2, "and the table it opens on is the one the link described"


# ---------------------------------------------------------------------------
# the sites screen
# ---------------------------------------------------------------------------
def test_a_site_shows_the_category_it_belongs_to_and_the_one_it_does_not(results):
    sites = results["sites"]
    assert sites["categorised_card"] == "مخزن", sites
    assert sites["uncategorised_card"], sites
    assert sites["from_category"], (
        "the hours in force have to say they came from the warehouse, not from the company"
    )


def test_both_site_forms_offer_the_categories_and_none_of_them(results):
    sites = results["sites"]
    assert sites["add_has_picker"] and sites["edit_has_picker"], sites
    assert sites["add_picker_options"], "the picker is empty: it never received the categories"
    assert sites["edit_picker_shows_none"], (
        "a site in no category must open on \"no category\", not on the first one in the list"
    )


def test_the_save_says_which_category_the_site_is_in(results):
    assert results["add_into_category"]["category_id"] == 1
    assert results["add_without_category"]["category_id"] is None, (
        "an empty picker is \"no category\", which is what the column's NULL means"
    )
    assert results["move_out_of_category"]["category_id"] is None, (
        "and taking a site out of one is an answer the form has to send"
    )


def test_the_categories_panel_lists_them_with_their_reach(results):
    sites = results["sites"]
    assert sites["panel"], sites
    assert sites["panel_rows"] == 2, sites
    assert sites["panel_names_hours_and_reach"], (
        "each row has to name the hours and the number of sites that follow it"
    )
    assert sites["panel_labels_the_empty_category"], (
        "a category with no hours of its own has to say so rather than showing blank times"
    )
    assert sites["panel_has_no_inline_handler"], (
        "a category name is operator data: the panel's buttons carry ids and are delegated"
    )


def test_a_category_is_added_with_its_hours(results):
    added = results["category_added"]
    assert added["kind"] == "category_save", added
    assert added["body"]["name"] == "مشاريع"
    assert added["body"]["clock_in_window_start"] == "06:00"
    assert added["body"]["clock_in_window_end"] == "14:00"
    assert added["body"]["site_timezone"] is None, "a zone nobody typed stays inherited"


def test_editing_a_category_sends_the_id_and_clears_what_was_emptied(results):
    edited = results["category_edited"]
    assert edited["name_field_prefilled"] and edited["hours_prefilled"], (
        "the edit form loads the category's own values"
    )
    assert edited["body"]["category_id"] == 1
    assert edited["body"]["name"] == "المخزن"
    assert edited["body"]["clock_in_window_start"] is None, (
        "an emptied box is an explicit null - \"inherit again\" - not an omission"
    )
