"""Choosing a site's clock-in window in the console, and what the form sends.

WHY THIS EXISTS
---------------
The API has been able to hold a window per site since the overnight-shift work: three nullable
columns on ``construction_sites``, a resolved ``window`` object in ``GET /admin/sites``, and a
punch path that reads them. What was missing was any way for an administrator to *set* one - the
feature existed and nothing called it, which for the person at the site is the same as it not
existing. This suite is the console half:

* the window that is **in force** is on the card, with where each half of it came from, because
  "why was this arrival flagged?" is answered by 21:30-05:30 appearing next to the site's name;
* the **add** form can set one, and a blank time box means "follow the company window" (sent as
  ``null`` - the API keeps an absent key and an explicitly cleared one apart);
* the **edit** panel loads the site's *configured* columns, never the resolved window. This is the
  trap worth a test of its own: prefilling with the company's hours would turn every site that
  merely inherits into one that overrides, so the company hours could never move again;
* "use the company window" empties the boxes so the save sends the three nulls that clear them;
* the **company window** itself is on the Admin tab, which is what a site without one inherits.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------
//
// The two shapes are the two cases that matter: a site with nothing of its own (every field
// resolved from the company rules) and a night site that set hours *and* a zone.

function windowInfo(overrides) {
    return Object.assign({
        site_name: 'Downtown Tower A',
        clock_in_window_start: '04:00',
        clock_in_window_end: '06:30',
        site_timezone: 'Africa/Cairo',
        window: '04:00-06:30',
        crosses_midnight: false,
        site_specific: false,
        source: {
            clock_in_window_start: 'global',
            clock_in_window_end: 'global',
            site_timezone: 'global'
        }
    }, overrides || {});
}

const SITES = [
    {
        site_name: 'Downtown Tower A', lat: 30.05, lon: 31.23, radius: 65,
        clock_in_window_start: null, clock_in_window_end: null, site_timezone: null,
        window: windowInfo({ source: { clock_in_window_start: 'site', clock_in_window_end: 'site', site_timezone: 'global' } })
    },
    {
        site_name: 'New Capital Zone B', lat: 29.98, lon: 31.75, radius: 100,
        clock_in_window_start: '22:00', clock_in_window_end: '06:00', site_timezone: 'Asia/Riyadh',
        window: windowInfo({
            site_name: 'New Capital Zone B',
            clock_in_window_start: '22:00', clock_in_window_end: '06:00', site_timezone: 'Asia/Riyadh',
            window: '22:00-06:00 (overnight)', crosses_midnight: true, site_specific: true,
            source: { clock_in_window_start: 'site', clock_in_window_end: 'site', site_timezone: 'site' }
        })
    }
];

const RULES = {
    clock_in_window_start: '04:00', clock_in_window_end: '06:30', site_timezone: 'Africa/Cairo',
    regular_hours: 8.0, overtime_notify_hours: 8.1, break_minutes: 30,
    break_after_hours: 4, auto_close_at_regular: 1
};

const calls = [];
let editStatus = 200;
let editDetail = 'Clock-in window must be a 24-hour time in HH:MM form (for example 04:00).';

function responders(url, init) {
    const method = (init && init.method) || 'GET';
    const record = (kind) => calls.push({ kind: kind, url: String(url), method: method, body: init.body });
    if (url.indexOf('/admin/sites/edit') >= 0) {
        record('edit');
        if (editStatus !== 200) return { status: editStatus, body: { detail: editDetail } };
        return { status: 200, body: { status: 'success', message: 'Site updated.' } };
    }
    if (url.indexOf('/admin/sites/add') >= 0) {
        record('add');
        return { status: 200, body: { status: 'success', message: 'Site added.' } };
    }
    if (url.indexOf('/admin/sites') >= 0) return { status: 200, body: SITES };
    if (url.indexOf('/admin/shift_rules') >= 0) {
        if (method === 'POST') {
            record('rules');
            return { status: 200, body: { status: 'success', rules: RULES } };
        }
        return { status: 200, body: RULES };
    }
    if (url.indexOf('/developer/notifications') >= 0) return { status: 200, body: { unread: 0, notifications: [] } };
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

const ADMIN = { id: '5000', name: 'Head Admin', role: 'head_admin', token: 'tok-5000' };

function adminEnv() {
    calls.length = 0;
    editStatus = 200;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify(ADMIN) + ')');
    return env;
}

function sitesMarkup(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

function valueOf(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

function factOf(markup, siteName) {
    const match = new RegExp('data-site-window="' + siteName + '">([^<]*)<').exec(markup);
    return match ? match[1] : null;
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function openEditor(env, siteName) {
    env.evaluate('UI_MODULES.openSiteEdit(' + JSON.stringify(siteName) + ')');
}

/**
 * Fill the open editor's fields.
 *
 * Set by hand rather than read back out of the rendered markup: this platform keeps
 * ``innerHTML`` as a string, so a ``value="22:00"`` attribute it has just rendered is not the
 * field's value the way it is in a browser. That the *markup* carries the site's configured
 * columns is asserted on its own (``editor_inheriting``), and this is what the save then sees.
 */
function fillEditor(env, values) {
    Object.keys(values).forEach(function (id) {
        env.evaluate("document.getElementById('" + id + "').value = " + JSON.stringify(values[id]));
    });
}

const results = {};

// 1. the Sites tab names the window in force, and where it came from
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    const markup = sitesMarkup(env);
    results.cards = {
        downtown_window: factOf(markup, 'Downtown Tower A'),
        downtown_company: markup.indexOf(env.evaluate("I18n.__('sitesWindowFromCompany')")) >= 0,
        zone_b_window: factOf(markup, 'New Capital Zone B'),
        zone_b_own: markup.indexOf(env.evaluate("I18n.__('sitesWindowFromSite')")) >= 0,
        zone_b_overnight: markup.indexOf(env.evaluate("I18n.__('sitesWindowOvernight')")) >= 0,
        has_add_window_fields: markup.indexOf('id="siteWindowStart"') >= 0
            && markup.indexOf('id="siteWindowEnd"') >= 0
            && markup.indexOf('id="siteWindowTimezone"') >= 0,
        has_edit_button: markup.indexOf('data-edit-site="New Capital Zone B"') >= 0,
        editor_closed: markup.indexOf('id="editSiteForm"') < 0,
        // The delegated listener is the point: a handler built from a site name is exactly
        // what the document CSP keeps 'unsafe-inline' for.
        edit_button_has_no_inline_handler: !/data-edit-site="[^"]*"[^>]*onclick=/.test(markup),
        requests: env.requests.filter((r) => r.url.indexOf('/admin/sites') >= 0).length
    };
}

// 2. a new site can be given a window, and a blank one means "follow the company"
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    env.evaluate("document.getElementById('siteName').value = 'Night Works'");
    env.evaluate("document.getElementById('location').value = '30.10,31.40'");
    env.evaluate("document.getElementById('radius').value = '80'");
    env.evaluate("document.getElementById('siteWindowStart').value = '21:30'");
    env.evaluate("document.getElementById('siteWindowEnd').value = '05:30'");
    env.evaluate("document.getElementById('siteWindowTimezone').value = 'Africa/Cairo'");
    await env.evaluate("document.getElementById('addSiteForm').onsubmit({ preventDefault: function () {} })");
    results.add_with_window = JSON.parse(calls[calls.length - 1].body);

    const blank = adminEnv();
    await blank.evaluate("UI.renderAdminTab('Sites')");
    blank.evaluate("document.getElementById('siteName').value = 'Day Works'");
    blank.evaluate("document.getElementById('location').value = '30.10,31.40'");
    blank.evaluate("document.getElementById('radius').value = '80'");
    await blank.evaluate("document.getElementById('addSiteForm').onsubmit({ preventDefault: function () {} })");
    results.add_without_window = JSON.parse(calls[calls.length - 1].body);
}

// 3. opening the editor loads the *configured* columns, not the window in force
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    openEditor(env, 'Downtown Tower A');
    const inheriting = sitesMarkup(env);
    results.editor_inheriting = {
        open: inheriting.indexOf('id="editSiteForm"') >= 0,
        start: valueOf(inheriting, 'editSiteWindowStart'),
        end: valueOf(inheriting, 'editSiteWindowEnd'),
        zone: valueOf(inheriting, 'editSiteTimezone'),
        radius: valueOf(inheriting, 'editSiteRadius'),
        location: valueOf(inheriting, 'editSiteLocation'),
        // the card behind it still says which window applies today
        // The resolved window is still on screen while the boxes are being changed - it is the
        // one line that answers "what applies here today", which is the question that brings
        // somebody to this form.
        notice: /id="editSiteWindowNotice"[^>]*>([^<]*)</.exec(inheriting)[1]
    };

    openEditor(env, 'New Capital Zone B');
    const overriding = sitesMarkup(env);
    results.editor_overriding = {
        start: valueOf(overriding, 'editSiteWindowStart'),
        end: valueOf(overriding, 'editSiteWindowEnd'),
        zone: valueOf(overriding, 'editSiteTimezone'),
        notice: /id="editSiteWindowNotice"[^>]*>([^<]*)</.exec(overriding)[1]
    };
}

// 4. saving the editor sends the window as it stands, blank boxes included
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    openEditor(env, 'New Capital Zone B');
    fillEditor(env, {
        editSiteLocation: '29.98,31.75', editSiteRadius: '100',
        editSiteWindowStart: '22:00', editSiteWindowEnd: '06:00', editSiteTimezone: 'Asia/Riyadh'
    });
    await env.evaluate("document.getElementById('editSiteForm').onsubmit({ preventDefault: function () {} })");
    results.save_keeps_window = {
        calls: calls.filter((c) => c.kind === 'edit').length,
        sent: JSON.parse(calls[calls.length - 1].body),
        toast: toasts(env).slice(-1)[0]
    };
}

// 4b. "use the company window" empties the three boxes, and the save says so
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    openEditor(env, 'New Capital Zone B');
    fillEditor(env, {
        editSiteLocation: '29.98,31.75', editSiteRadius: '100',
        editSiteWindowStart: '22:00', editSiteWindowEnd: '06:00', editSiteTimezone: 'Asia/Riyadh'
    });
    // Through the delegated listener, exactly as a click on the button arrives.
    env.evaluate("document.getElementById('sitesList').onclick({ target: { closest: function (wanted) { return wanted === '[data-company-window]' ? {} : null; } } })");
    const box = (id) => env.evaluate("document.getElementById('" + id + "').value");
    const emptied = {
        start: box('editSiteWindowStart'),
        end: box('editSiteWindowEnd'),
        zone: box('editSiteTimezone'),
        notice: env.evaluate("document.getElementById('editSiteWindowNotice').textContent")
    };
    await env.evaluate("document.getElementById('editSiteForm').onsubmit({ preventDefault: function () {} })");
    results.clear_window = {
        start_after_click: emptied.start,
        end_after_click: emptied.end,
        zone_after_click: emptied.zone,
        notice_after_click: emptied.notice,
        sent: JSON.parse(calls[calls.length - 1].body)
    };
}

// 4c. a refused window is reported as the reason, and the editor stays open
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Sites')");
    openEditor(env, 'New Capital Zone B');
    fillEditor(env, { editSiteLocation: '29.98,31.75', editSiteRadius: '100' });
    editStatus = 422;
    await env.evaluate("document.getElementById('editSiteForm').onsubmit({ preventDefault: function () {} })");
    const markup = sitesMarkup(env);
    results.refused = {
        toast: toasts(env).slice(-1)[0],
        editor_still_open: markup.indexOf('id="editSiteForm"') >= 0,
        still_in_edit_mode: env.evaluate('UI_MODULES._siteEdit'),
        no_success_toast: toasts(env).filter((text) => text.indexOf('saved') >= 0).length
    };
}

// 5. the company window is on the Admin tab, and travels with the rest of the rules
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.rules_panel = {
        start: valueOf(markup, 'rulesWindowStart'),
        end: valueOf(markup, 'rulesWindowEnd'),
        zone: valueOf(markup, 'rulesTimezone'),
        hint: markup.indexOf(env.evaluate("I18n.__('shiftRulesWindowHint')")) >= 0
    };

    env.evaluate("document.getElementById('rulesWindowStart').value = '22:00'");
    env.evaluate("document.getElementById('rulesWindowEnd').value = '06:00'");
    env.evaluate("document.getElementById('rulesTimezone').value = 'Africa/Cairo'");
    await env.evaluate("UI_MODULES.saveShiftRules({ preventDefault: function () {} })");
    results.rules_saved = JSON.parse(calls[calls.length - 1].body);

    const cleared = adminEnv();
    await cleared.evaluate("UI.renderAdminTab('Admin')");
    cleared.evaluate("document.getElementById('rulesWindowStart').value = ''");
    cleared.evaluate("document.getElementById('rulesWindowEnd').value = ''");
    await cleared.evaluate("UI_MODULES.saveShiftRules({ preventDefault: function () {} })");
    results.rules_cleared = JSON.parse(calls[calls.length - 1].body);
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_card_says_which_window_applies_and_where_it_came_from(results):
    cards = results["cards"]
    assert cards["downtown_window"] == "04:00-06:30", "the company window, shown on the site that inherits it"
    assert cards["downtown_company"] is True
    assert cards["zone_b_window"] == "22:00-06:00 (overnight)", cards["zone_b_window"]
    assert cards["zone_b_own"] is True, "a site with its own hours says so"
    assert cards["zone_b_overnight"] is True, "and says which shape of window it is"
    assert cards["has_add_window_fields"] is True
    assert cards["has_edit_button"] is True
    assert cards["editor_closed"] is True, "the list opens with no editor in it"
    assert cards["edit_button_has_no_inline_handler"] is True, (
        "the edit button must be handled by the delegated listener: handler text built from a "
        "site name is what the document CSP keeps 'unsafe-inline' for"
    )
    assert cards["requests"] == 1, "one read of the sites, no writes"


def test_a_new_site_can_be_given_an_overnight_window(results):
    sent = results["add_with_window"]
    assert sent["site_name"] == "Night Works"
    assert sent["location_input"] == "30.10,31.40"
    assert sent["radius"] == 80
    assert sent["clock_in_window_start"] == "21:30"
    assert sent["clock_in_window_end"] == "05:30"
    assert sent["site_timezone"] == "Africa/Cairo"


def test_a_blank_window_on_a_new_site_means_follow_the_company(results):
    """``null``, not ``""``: the API reads the empty string as "not configured" too, but null is
    what an absent *value* is, and the site has to keep following the company hours."""
    sent = results["add_without_window"]
    assert sent["clock_in_window_start"] is None
    assert sent["clock_in_window_end"] is None
    assert sent["site_timezone"] is None


def test_the_editor_loads_the_sites_own_columns_not_the_window_in_force(results):
    """The trap this suite exists for: prefilling would silently stop a site inheriting."""
    inheriting = results["editor_inheriting"]
    assert inheriting["open"] is True
    assert inheriting["start"] == "", "the company's 04:00 must not be typed into an inheriting site"
    assert inheriting["end"] == ""
    assert inheriting["zone"] == ""
    assert inheriting["radius"] == "65"
    assert inheriting["location"] == "30.05,31.23"
    assert "04:00-06:30" in inheriting["notice"], inheriting["notice"]

    overriding = results["editor_overriding"]
    assert overriding["start"] == "22:00"
    assert overriding["end"] == "06:00"
    assert overriding["zone"] == "Asia/Riyadh"
    assert "22:00-06:00" in overriding["notice"], overriding["notice"]


def test_saving_the_editor_sends_the_window_as_well_as_the_pin(results):
    saved = results["save_keeps_window"]
    assert saved["calls"] == 1
    sent = saved["sent"]
    assert sent["site_name"] == "New Capital Zone B"
    assert sent["location_input"] == "29.98,31.75"
    assert sent["radius"] == 100
    assert sent["clock_in_window_start"] == "22:00", "an edit must not drop the window it was showing"
    assert sent["clock_in_window_end"] == "06:00"
    assert sent["site_timezone"] == "Asia/Riyadh"
    assert saved["toast"] and "saved" in saved["toast"].lower()


def test_use_the_company_window_empties_the_boxes_and_saves_nulls(results):
    cleared = results["clear_window"]
    assert cleared["start_after_click"] == ""
    assert cleared["end_after_click"] == ""
    assert cleared["zone_after_click"] == ""
    assert "company window" in cleared["notice_after_click"].lower()
    sent = cleared["sent"]
    assert sent["clock_in_window_start"] is None, "a cleared window travels as an explicit null"
    assert sent["clock_in_window_end"] is None
    assert sent["site_timezone"] is None


def test_a_refused_window_is_shown_as_the_reason_and_leaves_the_form_open(results):
    refused = results["refused"]
    assert "HH:MM" in refused["toast"], refused["toast"]
    assert refused["editor_still_open"] is True
    assert refused["still_in_edit_mode"] == "New Capital Zone B"
    assert refused["no_success_toast"] == 0, "nothing was saved, so nothing may say it was"


def test_the_company_window_is_on_the_admin_tab(results):
    panel = results["rules_panel"]
    assert panel["start"] == "04:00"
    assert panel["end"] == "06:30"
    assert panel["zone"] == "Africa/Cairo"
    assert panel["hint"] is True


def test_saving_the_company_window_sends_it_with_the_rest_of_the_rules(results):
    saved = results["rules_saved"]
    assert saved["clock_in_window_start"] == "22:00"
    assert saved["clock_in_window_end"] == "06:00"
    assert saved["site_timezone"] == "Africa/Cairo"
    assert saved["regular_hours"] == 8.0, "the window travels with the numbers, not instead of them"


def test_clearing_the_company_window_sends_it_empty_not_left_out(results):
    """``""`` is the instruction; omitting the key would mean "leave it as it is"."""
    cleared = results["rules_cleared"]
    assert cleared["clock_in_window_start"] == ""
    assert cleared["clock_in_window_end"] == ""
    assert cleared["site_timezone"] == ""
