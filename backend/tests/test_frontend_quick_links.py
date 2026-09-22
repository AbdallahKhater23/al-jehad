"""The Links tab and the forced clock-in, from the console.

``test_quick_links.py`` covers what the server does with a link. What can only be checked
here is what the console offers and what it sends:

1. the create form lists the accounts a link may go to - an active worker or lead worker, and
   *not* an administrator or a deactivated account, because the server refuses both and a
   button that always fails is a trap;
2. the URL is shown once, with the warning that says so, and copied from the field on screen
   rather than from a stored copy that does not exist;
3. every link's state is on screen - working, revoked, expired, used up, or "the account
   behind it was switched off" - and the revoke button disappears once it is dead;
4. the punch list carries what an administrator reviews: action, site, paid hours, face count,
   address and the selfie button - and the selfie is fetched with the session's token rather
   than being an ``<img src=...>`` that would work for anybody who has the URL;
5. Live Ops can force a worker *onto* a site, and only offers workers who are not already on
   shift, since a worker who is missing from the session list is exactly the case it exists
   for.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const HEAD = { id: '5000', name: 'Head Admin', role: 'head_admin' };

function roster() {
    return [
        { id: '1', name: 'Seed Worker', role: 'worker', status: 'active', phone: '', email: '' },
        { id: '600', name: 'Ana Torrez', role: 'moallem', status: 'active', phone: '', email: '' },
        // Deactivated: a link for this account is refused, so it must not be offered.
        { id: '2', name: 'Gone Worker', role: 'worker', status: 'inactive', phone: '', email: '' },
        // On shift already, so Live Ops must not offer to force them in.
        { id: '3', name: 'On Shift', role: 'worker', status: 'active', phone: '', email: '' },
        { id: '1000', name: 'Ops Admin', role: 'admin', status: 'active', phone: '', email: '' }
    ];
}

function links() {
    return [
        {
            id: 12, worker_id: '1', worker_name: 'Seed Worker', worker_role: 'worker',
            worker_status: 'active', worker_active: true, created_by: '5000',
            created_at: '2026-09-01 08:00:00', expires_at: '2026-10-01 08:00:00',
            max_uses: 0, uses: 2, revoked_at: null, last_used_at: '2026-09-15 06:05:00',
            last_used_ip: '10.0.0.9', note: 'Gate 2 crew', state: 'active', usable: true,
            remaining_uses: null, clocked_in: true, clock_in_time: '2026-09-15 06:05:00',
            open_shift_site: 'Downtown Tower A', worker_status_label: null
        },
        {
            // The link is live but the account behind it was switched off. Not the same
            // thing as a revoked credential, and the row has to say which it is.
            id: 11, worker_id: '600', worker_name: 'Ana Torrez', worker_role: 'moallem',
            worker_status: 'inactive', worker_active: false, created_by: '5000',
            created_at: '2026-08-01 08:00:00', expires_at: '2026-10-31 08:00:00',
            max_uses: 4, uses: 1, revoked_at: null, last_used_at: null, last_used_ip: null,
            note: null, state: 'active', usable: false, remaining_uses: 3, clocked_in: false,
            clock_in_time: null, open_shift_site: null
        },
        {
            id: 9, worker_id: '1', worker_name: 'Seed Worker', worker_role: 'worker',
            worker_status: 'active', worker_active: true, created_by: '5000',
            created_at: '2026-06-01 08:00:00', expires_at: '2026-07-01 08:00:00',
            max_uses: 0, uses: 0, revoked_at: null, last_used_at: null, last_used_ip: null,
            note: null, state: 'expired', usable: false, remaining_uses: null,
            clocked_in: false, clock_in_time: null, open_shift_site: null
        },
        {
            id: 10, worker_id: '1', worker_name: 'Seed Worker', worker_role: 'worker',
            worker_status: 'active', worker_active: true, created_by: '1000',
            created_at: '2026-07-01 08:00:00', expires_at: '2026-08-01 08:00:00',
            max_uses: 0, uses: 3, revoked_at: '2026-07-10 09:00:00', last_used_at: null,
            last_used_ip: null, note: null, state: 'revoked', usable: false,
            remaining_uses: null, clocked_in: false, clock_in_time: null, open_shift_site: null
        }
    ];
}

const USES = {
    link_id: 12,
    worker_id: '1',
    worker_name: 'Seed Worker',
    uses: [
        {
            id: 5, worker_id: '1', action: 'Clock Out', site_name: 'Downtown Tower A',
            lat: 30.05, lon: 31.23, accuracy: 12.5, log_id: 900, ip: '10.0.0.9',
            user_agent: 'node', face_count: 1, created_at: '2026-09-15 16:10:00',
            hours: 8.0, break_hours: 0.5, log_status: 'Approved',
            flag_reason: 'quick link #12 (clock out) - selfie recorded, not matched',
            timestamp: '2026-09-15 16:10:00', photo_url: '/api/v1/admin/quick_link_photo/5'
        },
        {
            id: 4, worker_id: '1', action: 'Clock In', site_name: 'Downtown Tower A',
            lat: 30.05, lon: 31.23, accuracy: 9.0, log_id: 899, ip: '10.0.0.9',
            user_agent: 'node', face_count: 1, created_at: '2026-09-15 06:05:00',
            hours: 0, break_hours: null, log_status: 'Approved',
            flag_reason: 'quick link #12 (clock in) - selfie recorded, not matched',
            timestamp: '2026-09-15 06:05:00', photo_url: '/api/v1/admin/quick_link_photo/4'
        }
    ]
};

const created = [];
const revoked = [];
const forced = [];
let usersOk = true;
let photoStatus = 200;

function responders(url, init) {
    const method = (init && init.method) || 'GET';
    if (url.indexOf('/admin/quick_links') >= 0 && url.indexOf('/revoke') >= 0) {
        revoked.push({ url: url, method: method, headers: init.headers });
        return { status: 200, body: { status: 'success', link_id: 12, message: 'Clock link revoked.' } };
    }
    if (url.indexOf('/admin/quick_links') >= 0 && url.indexOf('/uses') >= 0) {
        return { status: 200, body: USES };
    }
    if (url.indexOf('/admin/quick_links') >= 0 && method === 'POST') {
        created.push({ body: JSON.parse(init.body), headers: init.headers });
        return {
            status: 200,
            body: {
                status: 'success', link_id: 13, worker_id: '1', worker_name: 'Seed Worker',
                url: 'http://localhost:8000/q/one-tap-token', token: 'one-tap-token',
                expires_at: '2026-10-15 08:00:00', max_uses: 0,
                qr_png_data_uri: 'data:image/png;base64,AAA'
            }
        };
    }
    if (url.indexOf('/admin/quick_links') >= 0) return { status: 200, body: links() };
    if (url.indexOf('/admin/quick_link_photo/') >= 0) {
        return { status: photoStatus, body: {}, contentType: 'image/jpeg' };
    }
    if (url.indexOf('/admin/force_clock_in') >= 0) {
        forced.push({ body: JSON.parse(init.body), headers: init.headers });
        return { status: 200, body: { status: 'success', message: "Worker 3 successfully Force Clocked In." } };
    }
    if (url.indexOf('/admin/active_sessions') >= 0) {
        return {
            status: 200,
            body: [{ worker_id: '3', name: 'On Shift', site_name: 'Downtown Tower A', clock_in_time: '2026-09-15 06:00:00' }]
        };
    }
    if (url.indexOf('/admin/sites') >= 0) {
        return { status: 200, body: [{ site_name: 'Downtown Tower A', radius: 65 }, { site_name: 'New Capital Zone B', radius: 100 }] };
    }
    if (url.indexOf('/admin/users') >= 0) {
        return usersOk ? { status: 200, body: roster() } : { status: 500, body: { detail: 'roster unavailable' } };
    }
    return { status: 200, body: {} };
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

function render(env) {
    return env.evaluate("document.getElementById('adminContent').innerHTML");
}

/** Each link as the admin sees it: the id, the published state, and its buttons. */
function rowsOf(markup) {
    const rows = markup.match(/<tr data-link="[^"]*"[\s\S]*?<\/tr>/g) || [];
    return rows.map((row) => ({
        id: (/data-link="([^"]*)"/.exec(row) || [])[1],
        state: (/data-link-state="([^"]*)"/.exec(row) || [])[1],
        uses: (/data-link-uses="([^"]*)"/.exec(row) || [])[1],
        revoke: row.indexOf('data-revoke-link=') >= 0,
        opens_uses: row.indexOf('data-link-uses=') >= 0,
        text: row.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
    }));
}

function optionsOf(markup, id) {
    const group = new RegExp('<select id="' + id + '"[\\s\\S]*?</select>').exec(markup);
    if (!group) return null;
    return (group[0].match(/<option value="[^"]*"/g) || []).map((o) => /"([^"]*)"/.exec(o)[1]);
}

function valueOf(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

async function linksEnv(options) {
    created.length = 0;
    revoked.length = 0;
    forced.length = 0;
    // Set here rather than by the scenario: the tab is rendered inside this function, so a
    // flag flipped before the call would be flipped back before it mattered.
    usersOk = !(options && options.roster === false);
    photoStatus = 200;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({ ...HEAD, token: 'tok-5000' }) + ')');
    await env.evaluate("UI.renderAdminTab('Links')");
    return env;
}

const results = {};

// 1. the tab exists, and is labelled in every language
{
    const env = await linksEnv();
    results.tabs = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, tab.key])");
    results.labels = env.evaluate("ADMIN_TABS.map((tab) => [tab.id, I18n.__(tab.key)])");
    results.translations = env.evaluate(
        "Object.keys(TRANSLATIONS).map((lang) => [lang, Object.keys(TRANSLATIONS[lang]).filter((k) => /^(links|quickLinks|forceIn)/.test(k)).length, " +
        "Object.keys(TRANSLATIONS.en).filter((k) => /^(links|quickLinks|forceIn)/.test(k) && !(k in TRANSLATIONS[lang])).join(',')])"
    );
}

// 2. what the tab asks for, and who it offers
{
    const env = await linksEnv();
    const markup = render(env);
    results.form = {
        asked_for: env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')).sort(),
        workers: optionsOf(markup, 'linkWorker')
    };
}

// 3. a roster the tab could not read still leaves a usable form
{
    const env = await linksEnv({ roster: false });
    const markup = render(env);
    results.no_roster = {
        // A failed roster must leave a typed-id input, not a broken select. Asserted on the
        // element, not its class: the class is styling and has to be free to change.
        free_text: markup.indexOf('<input id="linkWorker"') >= 0 && markup.indexOf('<select id="linkWorker"') < 0,
        note_shown: markup.indexOf(env.evaluate("I18n.__('linksRosterUnavailable')")) >= 0,
        // The list is the reason the screen exists, so a failed roster must not take it.
        list_still_there: markup.indexOf('data-link="12"') >= 0
    };
}

// 4. the links, as rows
{
    const env = await linksEnv();
    const rows = rowsOf(render(env));
    results.rows = { all: rows, by_id: rows.reduce((acc, row) => { acc[row.id] = row; return acc; }, {}) };

    // The phone layout says the same thing.
    const cards = env.evaluate("UI_MODULES.linksHtml(UI_MODULES._links)");
    results.cards = {
        account_inactive_named: cards.indexOf('data-link-state="account_inactive"') >= 0,
        expired_named: cards.indexOf('data-link-state="expired"') >= 0,
        dead_have_no_revoke: cards.indexOf('data-revoke-link="10"') < 0 && cards.indexOf('data-revoke-link="9"') < 0,
        live_have_revoke: cards.indexOf('data-revoke-link="12"') >= 0 && cards.indexOf('data-revoke-link="11"') >= 0
    };
}

// 5. issuing one: what is sent, and what the admin gets back
{
    const env = await linksEnv();
    env.evaluate("document.getElementById('linkWorker').value = '600'");
    env.evaluate("document.getElementById('linkTtl').value = '24'");
    env.evaluate("document.getElementById('linkMaxUses').value = '4'");
    env.evaluate("document.getElementById('linkNote').value = 'Gate 2 crew'");
    await env.evaluate("UI_MODULES.createLink()");
    const markup = render(env);
    results.created = {
        calls: created.length,
        body: created[0] ? created[0].body : null,
        authorized: created[0] ? created[0].headers.Authorization : null,
        toast: toasts(env).slice(-1)[0],
        url_on_screen: valueOf(markup, 'linkUrl'),
        shown_once_warning: markup.indexOf(env.evaluate("I18n.__('linksShownOnce')")) >= 0,
        qr_shown: markup.indexOf('data:image/png;base64,AAA') >= 0,
        panel_survives_the_repaint: markup.indexOf('data-new-link="13"') >= 0,
        list_refreshed: env.requests.filter((r) => /\/admin\/quick_links$/.test(r.url)).length
    };
}

// 5b. copying it
{
    const env = await linksEnv();
    env.evaluate("document.getElementById('linkWorker').value = '1'");
    await env.evaluate("UI_MODULES.createLink()");
    await env.evaluate("UI_MODULES.copyLinkUrl()");
    results.copied = env.copiedUrls.slice(-1)[0];
}

// 5c. an empty choice never leaves the browser
{
    const env = await linksEnv();
    env.evaluate("document.getElementById('linkWorker').value = ''");
    await env.evaluate("UI_MODULES.createLink()");
    results.created_without_worker = { calls: created.length, toast: toasts(env).slice(-1)[0] };
}

// 6. revoking
{
    const env = await linksEnv();
    await env.evaluate("UI_MODULES.revokeLink(12)");
    results.revoked = {
        calls: revoked.length,
        url: revoked[0] ? revoked[0].url.replace(/^https?:\/\/[^/]*\/api\/v1/, '') : null,
        method: revoked[0] ? revoked[0].method : null,
        authorized: revoked[0] ? revoked[0].headers.Authorization : null,
        toast: toasts(env).slice(-1)[0]
    };
}

// 7. the punches under a link
{
    const env = await linksEnv();
    await env.evaluate("UI_MODULES.openLinkUses(12)");
    const markup = env.evaluate("document.getElementById('linkUsesPanel').innerHTML");
    results.uses = {
        fetched: env.requests.filter((r) => /\/admin\/quick_links\/12\/uses$/.test(r.url)).length,
        rows: (markup.match(/data-use="[^"]*"/g) || []).length,
        hours_shown: markup.indexOf(env.evaluate("I18n.__('linksPaidHours')")) >= 0 && markup.indexOf('<b>8</b>') >= 0,
        face_count: markup.indexOf(env.evaluate("I18n.__('linksFaceCount')")) >= 0,
        flag_reason: markup.indexOf('selfie recorded, not matched') >= 0,
        photo_buttons: (markup.match(/data-show-photo="[^"]*"/g) || []).length,
        // No <img src> to the photo endpoint: it cannot carry a token, so a URL that worked
        // in a src would be a URL that worked for anybody.
        no_bare_photo_url: !/<img[^>]*src="[^"]*quick_link_photo/.test(markup),
        closed: env.evaluate("(UI_MODULES.closeLinkUses(), document.getElementById('linkUsesPanel').innerHTML.length)")
    };
}

// 8. the selfie, fetched with the session's token
{
    const env = await linksEnv();
    await env.evaluate("UI_MODULES.openLinkUses(12)");
    await env.evaluate("UI_MODULES.showLinkPhoto(5)");
    const photoRequest = env.requests.filter((r) => /quick_link_photo\/5$/.test(r.url))[0];
    results.photo = {
        called: !!photoRequest,
        url: photoRequest ? photoRequest.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '') : null,
        authorized: photoRequest ? photoRequest.headers.Authorization : null,
        src: env.evaluate("document.getElementById('linkPhoto5').src"),
        visible: env.evaluate("!document.getElementById('linkPhoto5').classList.contains('hidden')")
    };
}

// 8b. a photo that cannot be fetched is reported, and nothing is shown
{
    const env = await linksEnv();
    photoStatus = 404;
    await env.evaluate("UI_MODULES.openLinkUses(12)");
    await env.evaluate("UI_MODULES.showLinkPhoto(5)");
    results.photo_failed = {
        toast: toasts(env).slice(-1)[0],
        src: env.evaluate("document.getElementById('linkPhoto5').src") || null
    };
}

// 9. forcing a worker onto a site, from Live Ops
{
    const env = await linksEnv();
    await env.evaluate("UI.renderAdminTab('Live Ops')");
    const markup = render(env);
    results.force_in = {
        workers: optionsOf(markup, 'forceInWorker'),
        sites: optionsOf(markup, 'forceInSite'),
        button: markup.indexOf('data-force-in="true"') >= 0,
        asked_for: env.requests.map((r) => r.url.replace(/^https?:\/\/[^/]*\/api\/v1/, '')).sort()
    };
    env.evaluate("document.getElementById('forceInWorker').value = '1'");
    env.evaluate("document.getElementById('forceInSite').value = 'New Capital Zone B'");
    await env.evaluate("UI.forceIn()");
    results.force_in.sent = forced[0] ? forced[0].body : null;
    results.force_in.authorized = forced[0] ? forced[0].headers.Authorization : null;
    results.force_in.toast = toasts(env).slice(-1)[0];
}

// 9b. nobody to force in says so rather than offering an empty button
{
    const env = await linksEnv();
    results.force_in_empty = env.evaluate(
        "UI.forceInPanelHtml([{worker_id:'1'},{worker_id:'2'},{worker_id:'3'},{worker_id:'600'}], UI_MODULES._linksRoster || [], [{site_name:'Downtown Tower A'}])"
    );
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_console_has_a_links_tab_in_every_language(results):
    assert ["Links", "quickLinks"] in results["tabs"]
    labels = dict(results["labels"])
    assert labels["Links"] == "Links"
    missing = {lang: absent for lang, _, absent in results["translations"] if absent}
    assert missing == {}, f"untranslated quick-link strings: {missing}"
    for _lang, count, _absent in results["translations"]:
        assert count == results["translations"][0][1]


def test_the_create_form_offers_only_accounts_a_link_may_go_to(results):
    form = results["form"]
    assert "/admin/quick_links" in form["asked_for"]
    assert "/admin/users" in form["asked_for"], "the roster is what the form is built from"
    assert form["workers"] == ["1", "600", "3"], (
        "an administrator and a deactivated account are refused by the server, "
        "so they are not offered here"
    )


def test_a_roster_failure_still_leaves_a_usable_form(results):
    no_roster = results["no_roster"]
    assert no_roster["free_text"] is True, "an id can still be typed"
    assert no_roster["note_shown"] is True, "and the form says why there is no list"
    assert no_roster["list_still_there"] is True, "the links are the tab's subject, not the roster"


def test_every_link_shows_its_state_and_only_live_ones_can_be_revoked(results):
    rows = results["rows"]["by_id"]
    assert set(rows) == {"12", "11", "10", "9"}
    assert rows["12"]["state"] == "active"
    assert rows["11"]["state"] == "account_inactive"
    assert rows["10"]["state"] == "revoked"
    assert rows["9"]["state"] == "expired"
    assert rows["12"]["revoke"] is True
    assert rows["11"]["revoke"] is True, "the link is still live, even if the account is not"
    assert rows["10"]["revoke"] is False
    assert rows["9"]["revoke"] is False, "an expired link has nothing left to revoke"
    for row in rows.values():
        assert row["opens_uses"] is True, "the punches stay readable after the link dies"


def test_the_phone_layout_says_the_same_thing(results):
    cards = results["cards"]
    assert cards["account_inactive_named"] is True, (
        "a link whose account was switched off is not a revoked link"
    )
    assert cards["expired_named"] is True
    assert cards["dead_have_no_revoke"] is True
    assert cards["live_have_revoke"] is True


def test_issuing_a_link_sends_the_choice_and_shows_the_url_once(results):
    created = results["created"]
    assert created["calls"] == 1
    assert created["body"] == {"worker_id": "600", "ttl_hours": 24, "max_uses": 4, "note": "Gate 2 crew"}
    assert created["authorized"] == "Bearer tok-5000"
    assert created["url_on_screen"] == "http://localhost:8000/q/one-tap-token"
    assert created["shown_once_warning"] is True
    assert created["qr_shown"] is True
    assert created["panel_survives_the_repaint"] is True, (
        "the single copy of the link must survive the list repainting underneath it"
    )
    assert created["list_refreshed"] >= 2, "the new link appears in the list"


def test_the_link_is_copied_from_the_field_on_screen(results):
    assert results["copied"] == "http://localhost:8000/q/one-tap-token"


def test_a_form_with_no_worker_never_reaches_the_server(results):
    assert results["created_without_worker"]["calls"] == 0
    assert "worker" in results["created_without_worker"]["toast"].lower()


def test_revoking_sends_the_link_and_reports_the_result(results):
    revoked = results["revoked"]
    assert revoked["calls"] == 1
    assert revoked["url"] == "/admin/quick_links/12/revoke"
    assert revoked["method"] == "POST"
    assert revoked["authorized"] == "Bearer tok-5000"
    assert "revoked" in revoked["toast"].lower()


def test_the_punch_list_is_what_an_admin_reviews(results):
    uses = results["uses"]
    assert uses["fetched"] == 1
    assert uses["rows"] == 2
    assert uses["hours_shown"] is True, "the paid hours of the shift it closed"
    assert uses["face_count"] is True
    assert uses["flag_reason"] is True, (
        "the row says the selfie was recorded and not matched - the feature's whole trade"
    )
    assert uses["photo_buttons"] == 2
    assert uses["no_bare_photo_url"] is True, (
        "an <img src> cannot carry a token, so a src that worked would work for anybody"
    )
    assert uses["closed"] == 0


def test_the_selfie_is_fetched_with_the_session_token(results):
    photo = results["photo"]
    assert photo["called"] is True
    assert photo["url"] == "/admin/quick_link_photo/5"
    assert photo["authorized"] == "Bearer tok-5000"
    assert photo["src"] == "blob:stub", "the bytes become an object URL only this page can use"
    assert photo["visible"] is True


def test_a_selfie_that_cannot_be_fetched_is_reported(results):
    assert "could not be loaded" in results["photo_failed"]["toast"]
    assert results["photo_failed"]["src"] is None, "nothing is shown that is not there"


def test_live_ops_can_force_a_worker_onto_a_site(results):
    force_in = results["force_in"]
    assert force_in["button"] is True
    assert force_in["workers"] == ["1", "600"], (
        "the worker already on shift is not offered - they are on the session list, which is "
        "the one case this panel exists for"
    )
    # ``admin_id`` rides along as it does on every other console call and is ignored: the
    # server reads identity from the token, never from the body. What matters is the pair.
    assert force_in["sent"]["worker_id"] == "1"
    assert force_in["sent"]["site_name"] == "New Capital Zone B"
    assert force_in["authorized"] == "Bearer tok-5000"
    assert "Force Clocked In" in force_in["toast"]
    for path in ("/admin/active_sessions", "/admin/users", "/admin/sites"):
        assert path in force_in["asked_for"]


def test_the_force_in_panel_says_when_there_is_nobody_to_force(results):
    markup = results["force_in_empty"]
    assert 'data-force-in-note' in markup
    assert 'disabled' in markup, "an empty select with a live button is a trap"
