"""Site categories in the console: the picker on the Shifts board, and the forms that feed them.

WHY THIS EXISTS
---------------
A category is a class of sites - a warehouse, a factory, a project - that carries the clock-in
window for every site inside it (``shift_windows``). The backend half is pinned in
``test_site_categories``. This is the console half, and it is the half an operator actually
touches:

* the Shifts board offers every category **present in the period**, in one picker, with the
  number of shifts behind it, because an option that filters to an empty table is a dead end and
  a count is the first thing a reader checks a filter against;
* the search box finds a shift by its site's category, so an operator can type the word they
  think in ("مخزن") rather than the name of a site they have to remember;
* a category travels in the link, as the period and the search do: a link that dropped it would
  hand a colleague a *wider* table than the one it was copied from, under the same dates;
* the Sites tab can put a site into a category and take it out again, and the save says which
  it is doing - an absent control and a cleared one are different answers;
* the categories themselves can be added, renamed, retuned and deleted, and each row says how
  many sites follow it, which is the number that decides whether an edit here is a correction
  or a retune of a whole class of buildings;
* the tab opens on the *sites*: adding one and maintaining a class are the rare jobs, so both
  are folds in the header band, shut until somebody asks for them - and the add form is the one
  fold that opens itself, on a deployment where there is no site to look at yet;
* a site is one row carrying the figures that decide something - the category, the window in
  force and where it came from, the radius - rather than a card of four labelled facts, because
  four facts a card is five sites a screen and forty a scroll nobody makes;
* a list long enough to need a search gets one, and it narrows the sites already in hand: this
  tab repaints from a string, so re-asking the server to filter would be a request per keystroke
  for a list that is already on screen;
* and deleting a site is answered by the delegated listener rather than by an ``onclick=`` with
  a site name in it, which was the last attribute of that kind on this tab.

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
// picker, the panel and the site form all have to answer "and what about the empty one?".
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
// picker and the search are built from, so it is on every row rather than looked up.
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

// The category choices, read out of the picker: every option, with the one the tab is
// actually filtered to marked. The list is the control now, so "what can I choose, and what
// am I looking at" is answered from the one place the reader sees.
function optionsOf(markup) {
    const block = /<select[^>]*data-shifts-category[^>]*>([\s\S]*?)<\/select>/.exec(markup);
    if (!block) return [];
    const pattern = /<option value="([^"]*)"([^>]*)>([^<]*)</g;
    const found = [];
    let match;
    while ((match = pattern.exec(block[1])) !== null) {
        found.push({ value: match[1], selected: match[2].indexOf('selected') >= 0, label: match[3] });
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
        options: optionsOf(markup),
        rows: rowsShown(env),
        // The picker belongs to the period picker's own row, not to the table below it: this
        // is where "what am I looking at" is answered on this tab.
        picker_in_the_preset_row: /data-preset="thisWeek"[\s\S]*?data-shifts-category/.test(markup),
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
        options: optionsOf(markupOf(env)),
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

// 8. the shape of the tab: the band, the two folds, then the sites - and both folds shut
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    const markup = markupOf(env);
    const at = (needle) => markup.indexOf(needle);
    results.folds = {
        // The order *is* the redesign: what an administrator opens this tab for comes first,
        // and the two forms that used to stand in front of it are behind folds below the band.
        order: [at('data-sites-bar'), at('id="sitesAddPanel"'), at('id="siteCategoriesPanel"'), at('id="sitesList"')],
        add_hidden: /id="sitesAddPanel"[^>]*\shidden/.test(markup),
        categories_hidden: /id="siteCategoriesPanel"[^>]*\shidden/.test(markup),
        add_collapsed: /data-sites-add[^>]*aria-expanded="false"/.test(markup),
        categories_collapsed: /data-sites-categories[^>]*aria-expanded="false"/.test(markup),
        search_hidden: markup.indexOf('id="sitesQuery"') < 0,
        // A site name is operator data, and this attribute was the last place one was written
        // into executable text anywhere in the console.
        delete_has_no_inline_handler: !/data-delete-site="[^"]*"[^>]*onclick=/.test(markup),
        reads_before: env.requests.filter((r) => r.url.indexOf('/admin/sites') >= 0).length
    };
    env.evaluate('UI_MODULES.toggleSitesAdd()');
    const opened = markupOf(env);
    results.folds.add_open = !/id="sitesAddPanel"[^>]*\shidden/.test(opened);
    results.folds.add_open_expanded = /data-sites-add[^>]*aria-expanded="true"/.test(opened);
    results.folds.form_still_there = opened.indexOf('id="addSiteForm"') >= 0;
    results.folds.fields_still_there = opened.indexOf('id="siteWindowStart"') >= 0;
    env.evaluate('UI_MODULES.toggleSitesCategories()');
    const categories = markupOf(env);
    results.folds.categories_open = !/id="siteCategoriesPanel"[^>]*\shidden/.test(categories);
    results.folds.categories_keeps_rows = (categories.match(/data-category-row="/g) || []).length;
    results.folds.reads_after = env.requests.filter((r) => r.url.indexOf('/admin/sites') >= 0).length;
}

// 9. a site is a row, and the row carries the figures that decide something
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    const markup = markupOf(env);
    const row = /<li class="sites-row"[^>]*data-site="Downtown Tower A"[\s\S]*?<\/li>/.exec(markup);
    results.rows = {
        row: !!row,
        categorised: /data-site-category="Downtown Tower A">([^<]*)</.exec(markup) ? RegExp.$1 : null,
        radius: row ? row[0].indexOf('65 m') >= 0 : false,
        // "from مخزن" is the answer to "who moved these hours", and it is a word on the row
        // rather than a fourth labelled fact.
        origin: row
            ? row[0].indexOf(env.evaluate("I18n.__('sitesWindowFromCategory').replace('{category}', 'مخزن')")) >= 0
            : false,
        // Neither fixture site sets a zone, so neither row may print one: the zone at an
        // inheriting site is the company's, and the company's is set once, on the Admin tab.
        zone_pills: (markup.match(/ops-badge is-zone/g) || []).length,
        editor_closed: markup.indexOf('id="editSiteForm"') < 0
    };
}

// 10. a long list gets a search, and narrowing it is a read of the list already in hand
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    // Sixteen sites - four times the fixture, and over SITES_SEARCH_AFTER, which is where a
    // search stops being a control that can only ever cost a look.
    env.evaluate("for (let round = 0; round < 3; round += 1) { UI_MODULES._sites = UI_MODULES._sites.concat(UI_MODULES._sites.map(function (site) { return Object.assign({}, site, { site_name: site.site_name + ' north' }); })); }");
    env.evaluate('UI_MODULES.paintSites()');
    const long = markupOf(env);
    const readsBefore = env.requests.filter((r) => r.url.indexOf('/admin/sites') >= 0).length;
    results.search = {
        has_box: long.indexOf('id="sitesQuery"') >= 0,
        rows: (long.match(/data-site="/g) || []).length,
        has_clear: long.indexOf('data-clear-sites-search') >= 0,
        reads_before: readsBefore
    };
    env.evaluate("document.getElementById('sitesQuery').value = 'مخزن'");
    await env.evaluate("document.getElementById('sitesFilter').onsubmit({ preventDefault: function () {} })");
    const narrowed = markupOf(env);
    results.search.narrowed_rows = (narrowed.match(/data-site="/g) || []).length;
    results.search.narrowed_count = (/data-sites-count[^>]*>([^<]*)</.exec(narrowed) || [null, null])[1];
    results.search.narrowed_count_expected = env.evaluate("I18n.__('sitesShowing').replace('{shown}', '8').replace('{total}', '16')");
    results.search.clear_button_now = narrowed.indexOf('data-clear-sites-search') >= 0;
    results.search.reads_after = env.requests.filter((r) => r.url.indexOf('/admin/sites') >= 0).length;
    // A query that matches nothing is a list to be unhidden rather than a dead end.
    await env.evaluate("UI_MODULES.setSitesQuery('ZZZ')");
    const none = markupOf(env);
    results.search.no_match = none.indexOf('data-sites-no-match') >= 0;
    results.search.no_match_names_query = none.indexOf('ZZZ') >= 0;
    results.search.list_gone = none.indexOf('id="sitesList"') < 0;
    // The way out is in the empty state, and it is answered by the *page*: the list it would
    // have been delegated from is exactly what is not on screen.
    env.evaluate("document.getElementById('sitesPage').onclick({ target: { closest: function (wanted) { return wanted === '[data-clear-sites-search]' ? {} : null; } } })");
    results.search.cleared_rows = (markupOf(env).match(/data-site="/g) || []).length;
}

// 11. a deployment with no sites at all opens the one form it has, by itself
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    env.evaluate('UI_MODULES._sites = []');
    env.evaluate('UI_MODULES.paintSites()');
    const empty = markupOf(env);
    results.empty = {
        empty_state: empty.indexOf('data-sites-empty') >= 0,
        add_open: !/id="sitesAddPanel"[^>]*\shidden/.test(empty),
        add_expanded: /data-sites-add[^>]*aria-expanded="true"/.test(empty),
        no_search: empty.indexOf('id="sitesQuery"') < 0,
        no_list: empty.indexOf('id="sitesList"') < 0
    };
    // ...and it shuts again like any other fold: the automatic state is a default, not a lock.
    env.evaluate('UI_MODULES.toggleSitesAdd()');
    results.empty.add_closed = /id="sitesAddPanel"[^>]*\shidden/.test(markupOf(env));
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


# ---------------------------------------------------------------------------
# the board
# ---------------------------------------------------------------------------
def test_the_board_offers_every_category_in_the_period(results):
    board = results["board"]
    labels = {entry["label"]: entry for entry in board["options"]}
    assert labels["مخزن (2)"]["value"] == "مخزن"
    assert labels["مصنع (1)"]["value"] == "مصنع"
    # The count is the shifts behind the entry, and the "all" entry counts every row on screen.
    assert any(entry["label"] == "All categories (4)" for entry in board["options"]), board["options"]
    assert len(board["options"]) == 3, board["options"]


def test_the_all_option_is_the_one_selected_when_the_tab_opens(results):
    options = results["board"]["options"]
    chosen = [entry for entry in options if entry["selected"]]
    assert len(chosen) == 1 and chosen[0]["value"] == "", options
    assert results["board"]["rows"] == 4, "and nothing is filtered away yet"


def test_the_picker_sits_in_the_period_row_rather_than_below_the_table(results):
    """The red square in the brief: the row that answers \"what am I looking at\"."""
    assert results["board"]["picker_in_the_preset_row"], (
        "the category picker must be in the preset row, not down beside the table"
    )
    assert results["board"]["has_copy_link"], "and the rest of that row is still there"


def test_choosing_a_category_narrows_the_rows_and_names_it_in_the_note(results):
    narrowed = results["one_category"]
    assert narrowed["rows"] == 2, "the two warehouse shifts, and neither of the others"
    selected = [entry for entry in narrowed["options"] if entry["selected"]]
    assert len(selected) == 1 and selected[0]["value"] == "مخزن", narrowed["options"]
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


# ---------------------------------------------------------------------------
# the shape of the tab: the sites first, and the two jobs behind folds
# ---------------------------------------------------------------------------
def test_the_tab_opens_on_the_sites_with_both_forms_shut(results):
    """The redesign, in one assertion: the tab opens on the list, not on eleven empty boxes."""
    folds = results["folds"]
    band, add, categories, sites = folds["order"]
    assert -1 not in folds["order"], "the band, both folds and the list all have to be there"
    assert band < add < categories < sites, (
        "the band comes first, then the two folds, then the sites: "
        f"got {folds['order']}"
    )
    assert folds["add_hidden"] is True, (
        "the add form shipped open, which is the thing this tab did wrong"
    )
    assert folds["categories_hidden"] is True
    assert folds["add_collapsed"] is True and folds["categories_collapsed"] is True, (
        "a shut fold has to say it is shut, not only look it"
    )
    assert folds["search_hidden"] is True, (
        "two sites need no search: the box appears over a list long enough to need one, not"
        " over every list"
    )


def test_opening_a_fold_is_a_repaint_and_not_a_request(results):
    folds = results["folds"]
    assert folds["add_open"] is True
    assert folds["add_open_expanded"] is True, (
        "and the button says so, to the eye and to a screen reader"
    )
    assert folds["form_still_there"] is True, "the fold shows the same form, field ids and all"
    assert folds["fields_still_there"] is True, "including the window boxes the save reads"
    assert folds["categories_open"] is True
    assert folds["categories_keeps_rows"] == 2, "and the category rows move with it"
    assert folds["reads_after"] == folds["reads_before"], (
        "opening a fold re-read the sites: it repaints from the list already in hand"
    )


def test_the_delete_button_is_no_longer_an_inline_handler(results):
    """It was the last ``onclick=`` on this tab, and the only one carrying a site name."""
    assert results["folds"]["delete_has_no_inline_handler"] is True, (
        "deleting a site must go through the delegated listener: handler text built from a"
        " site name is the sink the document CSP exists to close"
    )


# ---------------------------------------------------------------------------
# the row, and the search a long list gets
# ---------------------------------------------------------------------------
def test_a_site_is_one_row_with_the_figures_that_decide_something(results):
    rows = results["rows"]
    assert rows["row"] is True, "a site is a row, not a card"
    assert rows["categorised"] == "مخزن"
    assert rows["radius"] is True, "the radius is on the row"
    assert rows["origin"] is True, (
        "and the hours say where they came from: 'from مخزن' is the answer to why an arrival"
        " was judged the way it was"
    )
    assert rows["editor_closed"] is True, "the list opens with no editor in it"


def test_the_company_zone_is_not_repeated_on_every_row(results):
    """What is the same for every site belongs to the company window, not to each row."""
    assert results["rows"]["zone_pills"] == 0, (
        "neither fixture site sets a zone, so neither row may print one - the zone in force at"
        " an inheriting site is the company's, and it is set once, on the Admin tab"
    )


def test_a_long_list_gets_a_search_that_narrows_without_asking_the_server_again(results):
    search = results["search"]
    assert search["has_box"] is True, (
        "sixteen sites is past the point where a search pays for itself"
    )
    assert search["rows"] == 16, search
    assert search["narrowed_rows"] == 8, (
        "the warehouse is half the list, and the search matches a site's category as well as"
        f" its name: {search}"
    )
    assert search["narrowed_count"] == search["narrowed_count_expected"], (
        f"the count has to describe the view, not the list: {search['narrowed_count']}"
    )
    assert "8" in search["narrowed_count"] and "16" in search["narrowed_count"]
    assert search["has_clear"] is False, "nothing typed yet, so there is nothing to clear"
    assert search["clear_button_now"] is True, "and once something is, the way out is offered"
    assert search["reads_after"] == search["reads_before"], (
        "narrowing the search re-read the sites: it should filter the list already on screen"
    )


def test_a_search_that_finds_nothing_offers_the_way_out(results):
    search = results["search"]
    assert search["no_match"] is True
    assert search["no_match_names_query"] is True, "the sentence names what was typed"
    assert search["list_gone"] is True
    assert search["cleared_rows"] == 16, (
        "the empty state's own button restored the list - and it is answered by the page,"
        " because the list it could have been delegated from is what is not on screen"
    )


def test_an_empty_deployment_opens_the_add_form_by_itself(results):
    empty = results["empty"]
    assert empty["empty_state"] is True
    assert empty["add_open"] is True, (
        "with no sites at all the form is the only thing this tab can usefully offer, so it"
        " opens itself"
    )
    assert empty["add_expanded"] is True
    assert empty["no_search"] is True, "a search over nothing only costs a look"
    assert empty["no_list"] is True
    assert empty["add_closed"] is True, "and it shuts again like any other fold"
