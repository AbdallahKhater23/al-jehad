"""The Live Ops board read by *site category* rather than by site.

WHY THIS EXISTS
---------------
A site is a place; a category is the *kind* of place - a warehouse, a depot, a project. An
operator asking "are my warehouses covered this morning" has, until now, had one way to answer
it: read every site name in the list and know which ones are warehouses. The board has carried
the categories with it all along - it already reads ``/admin/sites``, which publishes a
``category`` per site - so the view is a client-side grouping, not a second endpoint.

Two things this suite protects, both of which are quiet when wrong:

* **the switch is one board, not two.** Category view groups the same rows the Site view shows,
  through the same table and cards, so a figure - the count of people, the elapsed times, the
  forced-out control - cannot be right in one view and wrong in the other;
* **nothing is silently dropped.** A shift at a site with no category, or at a site that is not
  configured at all, is still a person on site: it lands in the uncategorised bucket, last,
  rather than vanishing from a board that claims to show who is clocked in.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answer, faked ------------------------------------------

const NOW = Date.now();

function ago(hours) {
    const at = new Date(NOW - hours * 3600 * 1000);
    const pad = (value) => String(value).padStart(2, '0');
    return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
        `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

const RULES = {
    regular_hours: 8.0, overtime_notify_hours: 8.1, break_minutes: 30.0,
    break_after_hours: 4.0, auto_close_at_regular: 1
};

// Two warehouses, one depot, one site that is configured with no category, and one that is
// not on the site list at all: every way a row can fail to have a category.
const SITES = [
    { site_name: 'Downtown Tower A', category: 'Warehouse' },
    { site_name: 'New Capital Zone B', category: 'Warehouse' },
    { site_name: 'Harbour Depot', category: 'Depot' },
    { site_name: 'Riverside Yard', category: null },
    { site_name: 'Unlisted Site' }
];

function sessionsNow() {
    return [
        { worker_id: 'w1', name: 'Youssef Adel', site_name: 'Downtown Tower A', clock_in_time: ago(10), role: 'worker', late_flag: 0 },
        { worker_id: 'w2', name: 'Mona Samir', site_name: 'New Capital Zone B', clock_in_time: ago(8), role: 'moallem', late_flag: 0 },
        { worker_id: 'w3', name: 'Amr Kamal', site_name: 'Harbour Depot', clock_in_time: ago(6), role: 'worker', late_flag: 0 },
        { worker_id: 'w4', name: 'Hana Fouad', site_name: 'Riverside Yard', clock_in_time: ago(2), role: 'worker', late_flag: 0 },
        // Not configured: the site list has no row for it, so no category can be resolved.
        { worker_id: 'w5', name: 'Tarek Nabil', site_name: 'Red Sea Camp', clock_in_time: ago(1), role: 'worker', late_flag: 0 }
    ];
}

const requests = [];

function responders(url, init) {
    requests.push({ url: url.split('/api/v1')[1] || url, method: (init && init.method) || 'GET' });
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: sessionsNow() };
    if (url.indexOf('/admin/shift_rules') >= 0) return { status: 200, body: RULES };
    if (url.indexOf('/admin/users') >= 0) return { status: 200, body: [] };
    if (url.indexOf('/admin/sites') >= 0) return { status: 200, body: SITES };
    return { status: 200, body: {} };
}

const ADMIN = { id: '5000', name: 'Seed Head Admin', role: 'head_admin', token: 'tok-5000' };

function boardEnv() {
    requests.length = 0;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(ADMIN) + ')');
    return env;
}

function ids(env) {
    return env.evaluate("UI_MODULES.liveOpsRows(UI_MODULES._liveOps).map((row) => row.session.worker_id)");
}

const results = {};

// 1. the board opens in Site view, with the switch offering both
{
    const env = boardEnv();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    results.default = {
        group: env.evaluate("UI_MODULES.liveOpsGroup()"),
        reads_sites: requests.some((r) => r.url === '/admin/sites'),
        toggle: env.evaluate("UI_MODULES.liveOpsGroupToggleHtml()"),
        picker: env.evaluate("UI_MODULES.liveOpsFilterSelectHtml(UI_MODULES._liveOps)"),
        stats_hint: env.evaluate("UI_MODULES.liveOpsStatsHtml(UI_MODULES._liveOps)"),
        status: env.evaluate("UI_MODULES.liveOpsStatusSentence(UI_MODULES._liveOps)"),
        has_inline_handler: env.evaluate("UI_MODULES.liveOpsGroupToggleHtml().indexOf('onclick=') >= 0")
    };
}

// 2. the switch: category view groups the same rows, under headings
{
    const env = boardEnv();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await env.evaluate("UI_MODULES.setLiveOpsGroup('category')");
    results.category = {
        group: env.evaluate("UI_MODULES.liveOpsGroup()"),
        toggle: env.evaluate("UI_MODULES.liveOpsGroupToggleHtml()"),
        picker: env.evaluate("UI_MODULES.liveOpsFilterSelectHtml(UI_MODULES._liveOps)"),
        groups: env.evaluate(
            "UI_MODULES.liveOpsGroups(UI_MODULES.liveOpsRows(UI_MODULES._liveOps))"
            + ".map((group) => group.label + ':' + group.count)"
        ),
        board: env.evaluate("UI_MODULES.liveOpsBoardHtml(UI_MODULES._liveOps)"),
        stats: env.evaluate("UI_MODULES.liveOpsStats(UI_MODULES._liveOps)"),
        stats_hint: env.evaluate("UI_MODULES.liveOpsStatsHtml(UI_MODULES._liveOps)"),
        status: env.evaluate("UI_MODULES.liveOpsStatusSentence(UI_MODULES._liveOps)"),
        // Every worker on site is still on the board, in some bucket or other.
        rows_still: ids(env)
    };
}

// 3. a category chip narrows the board to that category
{
    const env = boardEnv();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await env.evaluate("UI_MODULES.setLiveOpsGroup('category')");
    await env.evaluate("UI_MODULES.setLiveOpsSite('Depot')");
    results.filtered = {
        rows: ids(env),
        chip_filter: env.evaluate("UI_MODULES._liveOpsSite"),
        note: env.evaluate("UI_MODULES.liveOpsFilterNoteHtml(UI_MODULES._liveOps)")
    };
    // A search for the category's own word finds every row of it, even though no site is
    // named "warehouse" - which is the point of having the word on the row at all.
    await env.evaluate("UI_MODULES.setLiveOpsSite('')");
    env.evaluate("UI_MODULES.setLiveOpsQuery('warehouse')");
    results.searched = { rows: ids(env) };
}

// 4. switching back to Site view drops a filter that can no longer mean anything
{
    const env = boardEnv();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await env.evaluate("UI_MODULES.setLiveOpsGroup('category')");
    await env.evaluate("UI_MODULES.setLiveOpsSite('Depot')");
    await env.evaluate("UI_MODULES.setLiveOpsGroup('site')");
    results.back = {
        group: env.evaluate("UI_MODULES.liveOpsGroup()"),
        filter: env.evaluate("UI_MODULES._liveOpsSite"),
        rows: ids(env),
        picker: env.evaluate("UI_MODULES.liveOpsFilterSelectHtml(UI_MODULES._liveOps)")
    };
    // The state setter the chips call is the same one in both views: the attribute it
    // patches is the view's own.
    results.click = await env.evaluate(
        "UI_MODULES.onLiveOpsClick({ target: { closest: (sel) =>"
        + " sel === '[data-live-ops-group]' ? { dataset: { liveOpsGroup: 'category' } } : null } })"
    ).then(() => env.evaluate("UI_MODULES.liveOpsGroup()"));
}

// 5. a board that is all one category is the flat board: no heading over every row
{
    const env = boardEnv();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    await env.evaluate("UI_MODULES.setLiveOpsGroup('category')");
    env.evaluate(`(function () {
        UI_MODULES._liveOps = Object.assign({}, UI_MODULES._liveOps, {
            sessions: UI_MODULES._liveOps.sessions.filter((s) => s.site_name === 'Harbour Depot')
        });
        return true;
    })()`);
    results.lone = {
        groups: env.evaluate("UI_MODULES.liveOpsGroups(UI_MODULES.liveOpsRows(UI_MODULES._liveOps)).length"),
        board: env.evaluate("UI_MODULES.liveOpsBoardHtml(UI_MODULES._liveOps)")
    };
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_board_opens_on_sites_and_offers_the_category_view(results):
    default = results["default"]
    assert default["group"] == "site", "the board must open where it always has"
    assert default["reads_sites"], "the categories come from the site list the board already reads"
    assert not default["has_inline_handler"], (
        "the switch is bound by a data- hook: the inline allowance may only fall"
    )
    assert 'data-live-ops-group="site"' in default["toggle"]
    assert 'data-live-ops-group="category"' in default["toggle"]
    assert 'data-live-ops-group="site" aria-pressed="true"' in default["toggle"], (
        "the view the board is in has to say so"
    )
    assert '<option value="Downtown Tower A">Downtown Tower A (1)</option>' in default["picker"]


def test_the_category_view_groups_the_same_rows_and_names_every_bucket(results):
    category = results["category"]
    assert category["group"] == "category"
    assert 'data-live-ops-group="category" aria-pressed="true"' in category["toggle"]
    # Two warehouses, one depot, and the two rows no category could be resolved for - a site
    # configured with none, and a site that is not configured at all.
    assert category["groups"] == ["Depot:1", "Warehouse:2", "No category:2"], category["groups"]
    assert 'data-live-ops-group-section="Warehouse"' in category["board"]
    assert 'data-live-ops-group-section="Depot"' in category["board"]
    assert '<option value="Warehouse">Warehouse (2)</option>' in category["picker"]
    # Nothing is filtered, so the whole-board option is the one marked selected.
    assert '<option value="" selected>All categories (5)</option>' in category["picker"]
    # Nobody fell off the board on the way in.
    assert sorted(category["rows_still"]) == ["w1", "w2", "w3", "w4", "w5"], category["rows_still"]


def test_the_figures_are_counted_over_categories_in_this_view(results):
    category = results["category"]
    assert category["stats"]["sites"] == 2, (
        "two categories have somebody in them: the uncategorised rows are not a category"
    )
    assert category["stats"]["group"] == "category"
    assert "Categories with people" in category["stats_hint"], category["stats_hint"]
    assert "Sites with people" not in category["stats_hint"]
    assert "Categories with people" in category["status"], category["status"]


def test_a_category_chip_narrows_the_board_and_the_word_is_searchable(results):
    filtered = results["filtered"]
    assert filtered["rows"] == ["w3"], "the depot holds one of the five shifts"
    assert filtered["chip_filter"] == "Depot"
    assert "1" in filtered["note"] and "5" in filtered["note"], filtered["note"]
    assert results["searched"]["rows"] == ["w1", "w2"], (
        "a search for 'warehouse' finds both warehouses, whose site names never say the word"
    )


def test_switching_back_to_sites_restores_the_board_and_drops_the_filter(results):
    back = results["back"]
    assert back["group"] == "site"
    assert back["filter"] == "", "a category name is not a site: the filter cannot be carried over"
    assert sorted(back["rows"]) == ["w1", "w2", "w3", "w4", "w5"]
    assert '<option value="Downtown Tower A">Downtown Tower A (1)</option>' in back["picker"]
    assert results["click"] == "category", "the delegated handler routes the switch"


def test_a_lone_category_is_the_flat_board(results):
    lone = results["lone"]
    assert lone["groups"] == 1
    assert "data-live-ops-group-section" not in lone["board"], (
        "one heading over every row is a heading that tells the reader nothing"
    )
    assert 'data-session="w3"' in lone["board"]
