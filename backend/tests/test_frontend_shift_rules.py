"""The shift rules on screen, and the two numbers the worker is paid by.

WHY THIS EXISTS
---------------
The policy - 8 paid hours plus a 30-minute unpaid break, and a shift that ends itself
when the paid hours are up - only exists for the people who never read the API docs. Two
screens carry it:

* the **Admin** tab, which until now could not change a shift rule at all (the endpoint
  existed and no form called it), and
* the **worker's clock panel**, which ticks a live timer that used to count time on site
  as though it were time paid, and warn at a number the server had not sent.

What is asserted here is what those two screens say and what they send: the rules that
travel to ``POST /admin/shift_rules``, the refusal that comes back when an operator types
nonsense, and the panel's own arithmetic - a worker eight and a quarter hours into their
day has worked 7.75 paid hours and must not be told they are on overtime.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const RULES = {
    clock_in_window_start: '04:00',
    clock_in_window_end: '06:30',
    regular_hours: 8.0,
    overtime_notify_hours: 8.1,
    site_timezone: 'Africa/Cairo',
    break_minutes: 30,
    break_after_hours: 4,
    auto_close_at_regular: 1,
    // The verdict about the pair, exactly as the server computes it. The shipped 8.1/8.0
    // is the arrangement where the close stands down so the crossing can be reported.
    day_end: {
        regular_hours: 8.0,
        notify_hours: 8.1,
        close_at_paid_hours: null,
        close_defers: true,
        alert_reachable: true,
        day_ended_by: 'overtime_review',
        detail: 'the close stands down'
    }
};

/** A ``day_end`` block for one of the other two arrangements. */
function dayEnd(overrides) {
    return Object.assign({
        regular_hours: 8.0, notify_hours: 8.1, close_at_paid_hours: null, close_defers: false,
        alert_reachable: true, day_ended_by: 'auto_close', detail: 'the close ends the day'
    }, overrides || {});
}

const ADMIN = {
    id: '5000', name: 'Head Admin', role: 'head_admin', phone: '', email: 'head@example.test',
    status: 'active', face_enrolled: true, enrolled_at: null, password_set: true,
    password_changed_at: null, sessions_revoked: 0
};

const saveCalls = [];
let saveStatus = 200;
let saveReply = RULES;
// What ``GET /admin/shift_rules`` answers with, so a case can show the panel one of the
// other day-end verdicts without inventing a second responder.
let rulesReply = RULES;

function stats(overrides) {
    return Object.assign({
        worker_id: '600',
        total_hours: 12,
        regular_hours: 8,
        pending_hours: 0,
        overtime_hours: 0,
        overtime_notify_hours: 8.1,
        break_minutes: 30,
        break_after_hours: 4,
        paid_day_hours: 8.0,
        on_site_day_hours: 8.5,
        auto_close_at_regular: 1,
        flagged_for_review: false,
        active_session: null
    }, overrides || {});
}

function responders(url, init) {
    if (url.indexOf('/admin/shift_rules') >= 0) {
        const method = (init && init.method) || 'GET';
        if (method === 'POST') {
            saveCalls.push({ url: String(url), method: method, body: init.body, headers: init.headers });
            if (saveStatus !== 200) {
                return { status: saveStatus, body: { detail: 'break_minutes must be between 0 and 240.' } };
            }
            return { status: 200, body: { status: 'success', rules: saveReply } };
        }
        return { status: 200, body: rulesReply };
    }
    if (url.indexOf('/admin/active_sessions') >= 0) return { status: 200, body: [] };
    if (url.indexOf('/developer/notifications') >= 0) return { status: 200, body: { unread: 0, notifications: [] } };
    // The open shift travels nested under ``active_session`` - the shape
    // ``/worker/me/stats`` really answers with - so an override has to be nested too, or
    // the panel would be driven by a response the server never sends.
    if (url.indexOf('/worker/me/stats') >= 0) {
        return { status: 200, body: stats({ active_session: activeOverride }) };
    }
    return { status: 200, body: {} };
}

let activeOverride = null;

// --- reading what was rendered ------------------------------------------

function valueOf(markup, id) {
    const match = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(markup);
    return match ? match[1] : null;
}

function adminEnv(rules) {
    saveCalls.length = 0;
    saveStatus = 200;
    saveReply = RULES;
    rulesReply = rules || RULES;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: ADMIN.id, name: ADMIN.name, role: ADMIN.role, token: 'tok-5000'
    }) + ')');
    return env;
}

function workerEnv(active, config) {
    activeOverride = active || null;
    const env = boot();
    env.setResponder(responders);
    env.evaluate('State.saveUser(' + JSON.stringify({
        id: '600', name: 'Seed Lead', role: 'moallem', token: 'tok-600'
    }) + ')');
    if (config) env.evaluate('WORKER_MODULES._statsConfig = ' + JSON.stringify(config));
    return env;
}

/** A clock-in time that makes the shift `hoursAgo` long, in the server's format. */
function hoursAgo(hours) {
    const when = new Date(Date.now() - hours * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return when.getFullYear() + '-' + pad(when.getMonth() + 1) + '-' + pad(when.getDate()) + ' ' +
        pad(when.getHours()) + ':' + pad(when.getMinutes()) + ':' + pad(when.getSeconds());
}

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

const results = {};

// 1. the Admin tab carries the rules, with the day they add up to
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.rules_panel = {
        present: markup.indexOf('data-rules-panel') >= 0,
        has_form: markup.indexOf('id="shiftRulesForm"') >= 0,
        regular: valueOf(markup, 'rulesRegularHours'),
        break_minutes: valueOf(markup, 'rulesBreakMinutes'),
        break_after: valueOf(markup, 'rulesBreakAfterHours'),
        auto_close_checked: /id="rulesAutoClose"[^>]*checked/.test(markup),
        // 8 h paid + 30 min unpaid = 8.5 h on site, and the summary has to say the day
        // that produces rather than only the two inputs.
        // The class is styling and moves with the design; the attribute is the contract.
        summary_onsite: (/<p [^>]*data-rules-summary="([^"]*)"/.exec(markup) || [])[1],
        // This tab used to carry a second panel that created an administrator. It is gone:
        // the Credentials tab's New account form takes the id, the name, the contact details,
        // the password and the face in one step, and two places to create an account is one
        // too many. What must survive is the *pointer* - an administrator who comes here
        // looking for it has to be told where it went, and the note has to carry the id
        // ranges, which are the whole permission model.
        create_form: markup.indexOf('id="addAdminForm"') >= 0,
        points_at_credentials: markup.indexOf('data-admin-create-moved') >= 0
            && markup.indexOf(env.evaluate("I18n.__('adminCreateMoved')")) >= 0,
        labels: [
            env.evaluate("I18n.__('shiftRules')"),
            env.evaluate("I18n.__('shiftRulesBreak')"),
            env.evaluate("I18n.__('shiftRulesAutoClose')")
        ],
        requests: env.requests.filter((r) => r.url.indexOf('/admin/shift_rules') >= 0).length
    };
}

// 2. saving sends the four fields, and repaints from the answer
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    env.evaluate("document.getElementById('rulesRegularHours').value = '7.5'");
    env.evaluate("document.getElementById('rulesBreakMinutes').value = '45'");
    env.evaluate("document.getElementById('rulesBreakAfterHours').value = '5'");
    // The company clock-in window travels with the rest of the rules: it is what every site
    // without a window of its own is judged by, and this panel is the only place it is set.
    env.evaluate("document.getElementById('rulesWindowStart').value = '22:00'");
    env.evaluate("document.getElementById('rulesWindowEnd').value = '06:00'");
    env.evaluate("document.getElementById('rulesTimezone').value = 'Africa/Cairo'");
    saveReply = Object.assign({}, RULES, {
        regular_hours: 7.5, break_minutes: 45, break_after_hours: 5
    });
    await env.evaluate("UI_MODULES.saveShiftRules({ preventDefault: function () {} })");

    const call = saveCalls[saveCalls.length - 1];
    const sent = JSON.parse(call.body);
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.rules_saved = {
        calls: saveCalls.length,
        path: call.url.replace(/^.*\/api\/v1/, ''),
        method: call.method,
        sent: sent,
        authorized: call.headers['Authorization'],
        toast: toasts(env).slice(-1)[0],
        repainted_regular: valueOf(markup, 'rulesRegularHours'),
        repainted_summary: (/<p [^>]*data-rules-summary="([^"]*)"/.exec(markup) || [])[1]
    };
}

// 2b. unchecking the box is how an operator turns the automatic close off
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    env.evaluate("document.getElementById('rulesAutoClose').checked = false");
    await env.evaluate("UI_MODULES.saveShiftRules({ preventDefault: function () {} })");
    results.close_off = JSON.parse(saveCalls[saveCalls.length - 1].body);
}

// 2c. a refused save is reported as the reason, never as a success
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Admin')");
    saveStatus = 400;
    await env.evaluate("UI_MODULES.saveShiftRules({ preventDefault: function () {} })");
    results.save_refused = {
        calls: saveCalls.length,
        toast: toasts(env).slice(-1)[0],
        still_shows_the_rules: env.evaluate("document.getElementById('adminContent').innerHTML")
            .indexOf('data-rules-panel') >= 0
    };
}

// 2d. the panel says which rule ends the day - the deferral first, because it is the one
//     with a behavioural change behind it (the switch still reads as on)
function notice(markup, kind) {
    const match = new RegExp('data-rules-alert="' + kind + '"[^>]*>([^<]*)<').exec(markup);
    return match ? match[1].trim() : null;
}

{
    const env = adminEnv(RULES);
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.rules_warning_deferred = {
        deferred_notice: markup.indexOf('data-rules-alert="deferred"') >= 0,
        unreachable_notice: markup.indexOf('data-rules-alert="unreachable"') >= 0,
        notice_text: notice(markup, 'deferred')
    };
}

// 2e. the one genuinely unreachable case: the line sits on the paid day
{
    const rules = Object.assign({}, RULES, {
        overtime_notify_hours: 8.0,
        day_end: dayEnd({ notify_hours: 8.0, close_at_paid_hours: 8.0, alert_reachable: false })
    });
    const env = adminEnv(rules);
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.rules_warning_unreachable = {
        deferred_notice: markup.indexOf('data-rules-alert="deferred"') >= 0,
        unreachable_notice: markup.indexOf('data-rules-alert="unreachable"') >= 0,
        notice_text: notice(markup, 'unreachable')
    };
}

// 2f. and it is quiet when the two rules agree, so a warning does not become wallpaper
{
    const rules = Object.assign({}, RULES, {
        overtime_notify_hours: 7.5,
        day_end: dayEnd({ notify_hours: 7.5, close_at_paid_hours: 8.0 })
    });
    const env = adminEnv(rules);
    await env.evaluate("UI.renderAdminTab('Admin')");
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.rules_no_warning = {
        any_notice: markup.indexOf('data-rules-alert=') >= 0,
        panel_present: markup.indexOf('data-rules-panel') >= 0
    };
}

// 3. the Shifts tab shows the unpaid break beside the paid hours
{
    const env = adminEnv();
    await env.evaluate("UI.renderAdminTab('Shifts')");
    const markup = env.evaluate("document.getElementById('adminContent').innerHTML");
    results.shifts_view = {
        has_break_card: markup.indexOf('data-total="break_hours"') >= 0,
        break_header: markup.indexOf(env.evaluate("I18n.__('shiftsBreak')")) >= 0,
        // Cards come from the server's totals, which this fake answers with zeros, so the
        // assertion is that the column and the card exist and are named.
        labels_in_lang: env.evaluate("I18n.__('shiftsBreak')")
    };
}

// 4. the worker's panel states the two numbers the timer is judged by
{
    const active = { site_name: 'Downtown Tower A', clock_in_time: hoursAgo(2), late_flag: null };
    const env = workerEnv(active);
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerPanel'))");
    const markup = env.evaluate("document.getElementById('workerPanel').innerHTML");
    results.panel = {
        paid_day_label: markup.indexOf(env.evaluate("I18n.__('shiftPaidDay')")) >= 0,
        break_label: markup.indexOf(env.evaluate("I18n.__('shiftUnpaidBreak')")) >= 0,
        shows_8h: markup.indexOf('<b>8 h</b>') >= 0,
        shows_30m: markup.indexOf('<b>30m</b>') >= 0,
        timer_present: markup.indexOf('id="shiftElapsed"') >= 0,
        // 8.5 h on site for 8 h paid: the panel has to say which is which.
        policy: (/data-day-policy="([^"]*)"/.exec(markup) || [])[1]
    };
}

// 4b. the warning is about *paid* hours: 8.25 h on site is 7.75 h of work
{
    const env = workerEnv({ site_name: 'Downtown Tower A', clock_in_time: hoursAgo(8.25), late_flag: null });
    await env.evaluate("WORKER_MODULES.startElapsedTimer('" + hoursAgo(8.25) + "', " + JSON.stringify({
        overtimeHours: 8.1, breakMinutes: 30, breakAfterHours: 4, paidDayHours: 8, autoCloses: true
    }) + ")");
    results.mid_day = {
        elapsed: env.evaluate("document.getElementById('shiftElapsed').textContent"),
        note_hidden: env.evaluate("document.getElementById('shiftOvertimeNote').classList.contains('hidden')"),
        toasts: toasts(env)
    };
    env.evaluate("WORKER_MODULES.stopElapsedTimer()");
}

// 4c. at the limit the worker is told the day is done, not that they are on overtime
{
    const env = workerEnv({ site_name: 'Downtown Tower A', clock_in_time: hoursAgo(8.75), late_flag: null });
    await env.evaluate("WORKER_MODULES.startElapsedTimer('" + hoursAgo(8.75) + "', " + JSON.stringify({
        overtimeHours: 8.1, breakMinutes: 30, breakAfterHours: 4, paidDayHours: 8, autoCloses: true
    }) + ")");
    const note = env.evaluate("document.getElementById('shiftOvertimeNote').innerHTML");
    results.day_done = {
        hidden: env.evaluate("document.getElementById('shiftOvertimeNote').classList.contains('hidden')"),
        says_full_day: note.indexOf(env.evaluate("I18n.__('shiftEndsNow')")) >= 0,
        mentions_the_break: note.indexOf('30') >= 0 || note.indexOf('8.5') >= 0,
        toasts: toasts(env)
    };
    env.evaluate("WORKER_MODULES.stopElapsedTimer()");
}

// 4d. with the close switched off, the same shift is the overtime warning instead
{
    const env = workerEnv({ site_name: 'Downtown Tower A', clock_in_time: hoursAgo(9.2), late_flag: null });
    await env.evaluate("WORKER_MODULES.startElapsedTimer('" + hoursAgo(9.2) + "', " + JSON.stringify({
        overtimeHours: 8.1, breakMinutes: 30, breakAfterHours: 4, paidDayHours: 8, autoCloses: false
    }) + ")");
    const note = env.evaluate("document.getElementById('shiftOvertimeNote').innerHTML");
    results.overtime_warning = {
        hidden: env.evaluate("document.getElementById('shiftOvertimeNote').classList.contains('hidden')"),
        warns: note.indexOf('8.1') >= 0,
        stays_open: note.indexOf(env.evaluate("I18n.__('overtimeOpenShiftHint')")) >= 0,
        toasts: toasts(env)
    };
    env.evaluate("WORKER_MODULES.stopElapsedTimer()");
}
"""


@pytest.fixture(scope="module")
def results() -> dict:
    return frontend_vm.run(HARNESS)


def test_the_admin_tab_carries_the_shift_rules_and_the_day_they_add_up_to(results):
    panel = results["rules_panel"]
    assert panel["present"] is True and panel["has_form"] is True
    assert panel["regular"] == "8"
    assert panel["break_minutes"] == "30"
    assert panel["break_after"] == "4"
    assert panel["auto_close_checked"] is True
    assert panel["summary_onsite"] == "8.50", "8 paid hours plus the 30-minute break is an 8.5 h day"
    # The tab carries one job now. The create form moved to Credentials, where the same form
    # the console already had - id, name, contact, password and face in one step - is the only
    # way an account is made, and this tab says so rather than leaving a visitor guessing.
    assert panel["create_form"] is False, "the create-an-administrator form belongs to Credentials"
    assert panel["points_at_credentials"] is True, "and the tab has to say where it went"
    assert panel["labels"][1] == "Unpaid break (minutes)"
    assert panel["requests"] == 1, "one read of the rules, no writes"


def test_saving_the_rules_sends_them_and_repaints_from_the_answer(results):
    saved = results["rules_saved"]
    assert saved["calls"] == 1
    assert saved["path"].endswith("/admin/shift_rules")
    assert saved["method"] == "POST"
    assert saved["sent"] == {
        "regular_hours": 7.5,
        "break_minutes": 45,
        "break_after_hours": 5,
        "auto_close_at_regular": 1,
        "clock_in_window_start": "22:00",
        "clock_in_window_end": "06:00",
        "site_timezone": "Africa/Cairo",
    }
    assert saved["authorized"] == "Bearer tok-5000"
    assert "saved" in saved["toast"].lower()
    assert saved["repainted_regular"] == "7.5", "the form shows what was stored, not what was typed"
    assert saved["repainted_summary"] == "8.25", "7.5 paid + 45 unpaid is an 8.25 h day"


def test_unchecking_the_box_turns_the_automatic_close_off(results):
    assert results["close_off"]["auto_close_at_regular"] == 0
    assert results["close_off"]["break_minutes"] == 30, "the other fields still travel"


def test_the_panel_warns_when_the_automatic_close_has_stood_down(results):
    """The switch still reads as *on*, so the panel is the only place the operator is told."""
    warned = results["rules_warning_deferred"]
    assert warned["deferred_notice"] is True
    assert warned["unreachable_notice"] is False, "the crossing is observable; that is the point"
    assert "standing down" in warned["notice_text"], warned["notice_text"]
    assert "8.1" in warned["notice_text"] and "8" in warned["notice_text"]


def test_the_panel_warns_when_the_alert_cannot_fire_at_all(results):
    warned = results["rules_warning_unreachable"]
    assert warned["unreachable_notice"] is True
    assert warned["deferred_notice"] is False
    assert "strictly" in warned["notice_text"].lower(), warned["notice_text"]


def test_the_panel_is_quiet_when_the_two_rules_agree(results):
    quiet = results["rules_no_warning"]
    assert quiet["panel_present"] is True
    assert quiet["any_notice"] is False, "with the line below the day, both rules work and there is nothing to warn about"


def test_a_refused_save_is_the_servers_reason(results):
    refused = results["save_refused"]
    assert refused["calls"] == 1
    assert "between 0 and 240" in refused["toast"]
    assert refused["still_shows_the_rules"] is True, "a refused save leaves the form where it was"


def test_the_shifts_tab_names_the_unpaid_break(results):
    view = results["shifts_view"]
    assert view["has_break_card"] is True
    assert view["break_header"] is True
    assert view["labels_in_lang"] == "Unpaid break"


def test_the_workers_panel_says_which_hours_are_paid(results):
    panel = results["panel"]
    assert panel["paid_day_label"] is True and panel["break_label"] is True
    assert panel["shows_8h"] is True, "the paid day"
    assert panel["shows_30m"] is True, "the unpaid break, in minutes a worker can read"
    assert panel["timer_present"] is True
    assert panel["policy"] == "8"


def test_a_shift_eight_and_a_quarter_hours_long_is_not_overtime(results):
    """8.25 h on site is 7.75 h of paid work: telling the worker otherwise is a false alarm."""
    mid = results["mid_day"]
    assert mid["note_hidden"] is True
    assert mid["toasts"] == []
    assert mid["elapsed"].startswith("8:")


def test_at_the_limit_the_worker_is_told_the_day_is_done(results):
    done = results["day_done"]
    assert done["hidden"] is False
    assert done["says_full_day"] is True
    assert done["mentions_the_break"] is True
    assert done["toasts"], "the moment it happens, the worker is told"


def test_with_the_close_off_the_same_shift_is_the_overtime_warning(results):
    warning = results["overtime_warning"]
    assert warning["hidden"] is False
    assert warning["warns"] is True, "the threshold the server sent, not a hardcoded number"
    assert warning["stays_open"] is True, "with no automatic close, the shift counts until clock-out"
