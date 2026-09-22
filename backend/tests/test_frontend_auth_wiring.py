"""The frontend's authentication wiring, exercised for real.

THE BUG THIS GUARDS
-------------------
``POST /auth/login`` returns the access token *beside* ``user``::

    {"status": "success", "user": {...}, "token": "...", "access_token": "..."}

The frontend stored only ``res.user``, so ``State.user.token`` was ``undefined``
and the shared request helper fell back to ``Bearer dummy``. The server answered
``401 {"detail": "Invalid token"}`` for every authenticated call - clock-in
included - which reached the worker as ``Attendance error: Invalid token`` with
nothing to act on and no hint that signing in again was the fix.

A backend test cannot catch that: the API was behaving correctly. So this suite
runs the real ``frontend/javascript`` files in a Node VM with a stubbed DOM and
asserts the three behaviours that were wrong or missing:

1. the token from the login response is what authenticates the next request;
2. a session restored without a token is discarded at boot instead of failing
   every tap;
3. a 401 on a request that carried a token signs the worker out with a message
   they can act on.

Node is optional; without it these skip rather than fail.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from harness import PROJECT_ROOT

NODE = shutil.which("node")
FRONTEND = PROJECT_ROOT / "frontend"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The files a worker's phone ends up with, in one shared scope (as separate tags
# would), against a stubbed DOM and a recording fetch. Deliberately no
# admin_modules.js: this suite is where the worker path is proven to run without it,
# now that index.html no longer ships it and UI fetches it only for an administrator.
HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const frontend = process.argv[2];
const scripts = ['i18n.js', 'i18n.ar.js', 'i18n.hi.js', 'i18n.ur.js',
                 'frontendjavascript.js', 'worker_modules.js'];

// A clock the tests can move by hand. The card's timer has to be *live*: the only
// way to observe a shift tipping past the threshold while the worker watches is to
// let the clock advance and tick the registered callbacks, without waiting 20 real
// minutes (and without a running interval keeping Node alive at the end).
function makeShiftedDate() {
    let offset = 0;
    class ShiftedDate extends Date {
        constructor(...args) {
            if (args.length === 0) super(Date.now() + offset);
            else super(...args);
        }
        static now() { return Date.now() + offset; }
    }
    ShiftedDate.advance = (ms) => { offset += ms; };
    return ShiftedDate;
}

function makeStorage() {
    const map = new Map();
    return {
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => { map.set(String(k), String(v)); },
        removeItem: (k) => { map.delete(k); },
        clear: () => map.clear(),
        dump: () => Object.fromEntries(map)
    };
}

function makeClassList() {
    const names = new Set();
    return {
        add(name) { names.add(name); },
        remove(name) { names.delete(name); },
        contains(name) { return names.has(name); },
        toggle(name, force) {
            const wanted = force === undefined ? !names.has(name) : !!force;
            if (wanted) names.add(name); else names.delete(name);
            return wanted;
        },
        all: () => Array.from(names)
    };
}

function makeElement(id) {
    return {
        id,
        className: '',
        innerHTML: '',
        textContent: '',
        value: id === 'userId' ? '600' : (id === 'email' ? 'seed600@example.test' : 'moallem-pass-123'),
        style: {},
        dataset: {},
        // Recording rather than inert: the clock card reveals its overtime note by
        // toggling the `hidden` class, and that has to be observable from the tests.
        classList: makeClassList(),
        addEventListener(type, handler) { (this.__handlers = this.__handlers || {})[type] = handler; },
        // Toast.render() appends into #toastRoot when it exists, so record what lands there.
        appendChild(child) { (this.__children = this.__children || []).push(child); },
        removeChild() {}, remove() {}, focus() {}, play() {}, click() {},
        setAttribute() {}, getAttribute() { return null; },
        querySelector() { return makeElement(id + '-child'); },
        firstElementChild: null
    };
}

function boot(options) {
    const opts = options || {};
    const elements = new Map();
    const requests = [];
    const toasts = [];
    const localStorage = makeStorage();
    const sessionStorage = makeStorage();
    if (opts.session) sessionStorage.setItem('user', JSON.stringify(opts.session));

    const document = {
        addEventListener() {},
        visibilityState: 'visible',
        documentElement: { classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {} },
        body: { style: {}, appendChild() {}, classList: { toggle() {} } },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        createElement(tag) { return makeElement(tag); },
        getElementById(id) { if (!elements.has(id)) elements.set(id, makeElement(id)); return elements.get(id); }
    };

    const window = {
        location: {
            protocol: 'http:', hostname: 'localhost', port: '8000',
            origin: 'http://localhost:8000', href: 'http://localhost:8000/', pathname: '/', search: ''
        },
        isSecureContext: true,
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        addEventListener() {},
        setTimeout
    };

    let responder = () => ({ status: 200, body: {} });
    // Intervals are recorded, never scheduled: the test decides when a second passes.
    const intervals = new Map();
    let nextIntervalId = 1;
    const ShiftedDate = makeShiftedDate();
    const context = vm.createContext({
        console, setTimeout, clearTimeout, TextEncoder, TextDecoder, FormData, Blob, URL,
        Date: ShiftedDate,
        setInterval: (fn) => { const id = nextIntervalId++; intervals.set(id, fn); return id; },
        clearInterval: (id) => { intervals.delete(id); },
        window, document, localStorage, sessionStorage,
        navigator: { onLine: true, userAgent: 'node', platform: 'linux', maxTouchPoints: 0 },
        alert(message) { toasts.push(String(message)); },
        confirm: () => true,
        fetch: async (url, init) => {
            const headers = (init && init.headers) || {};
            requests.push({ url: String(url), headers, method: (init && init.method) || 'GET' });
            const answer = responder(String(url), init);
            return {
                ok: answer.status < 400,
                status: answer.status,
                json: async () => answer.body
            };
        }
    });
    window.window = window;

    for (const name of scripts) {
        vm.runInContext(fs.readFileSync(path.join(frontend, name), 'utf8'), context, { filename: name });
    }

    return {
        elements,
        requests,
        toasts,
        evaluate(expression) { return vm.runInContext(expression, context); },
        setResponder(fn) { responder = fn; },
        advanceClock(ms) { ShiftedDate.advance(ms); },
        // One "second" for every timer the page has running.
        tickIntervals() { for (const fn of Array.from(intervals.values())) fn(); },
        submitLogin() {
            const handler = (elements.get('loginForm').__handlers || {}).submit;
            if (!handler) throw new Error('the login form handler was never registered');
            return handler({ preventDefault() {}, target: { querySelector: () => ({ disabled: false }) } });
        }
    };
}

const LOGIN_BODY = {
    status: 'success',
    user: { id: '600', name: 'Seed Lead Worker', role: 'moallem', email: 'seed600@example.test' },
    token: 'tok-abc-123',
    access_token: 'tok-abc-123',
    token_type: 'bearer',
    expires_in: 43200
};

// A local-time "YYYY-MM-DD HH:MM:SS" stamp, the shape the server stores and the
// shape worker_modules.js parses back with Date() (which reads it as local time).
function localStamp(secondsAgo) {
    const when = new Date(Date.now() - secondsAgo * 1000);
    const pad = (value) => String(value).padStart(2, '0');
    return when.getFullYear() + '-' + pad(when.getMonth() + 1) + '-' + pad(when.getDate()) +
        ' ' + pad(when.getHours()) + ':' + pad(when.getMinutes()) + ':' + pad(when.getSeconds());
}

// Boots the clock card with an open shift that started `secondsAgo` seconds ago.
async function bootOpenShift(secondsAgo, rules) {
    const env = boot();
    env.setResponder((url) => (url.includes('/worker/me/stats')
        ? {
            status: 200,
            body: Object.assign({
                worker_id: '600',
                total_hours: 3.5,
                overtime_notify_hours: 8.1,
                active_session: { site_name: 'Downtown Tower A', clock_in_time: localStamp(secondsAgo) }
            }, rules || {})
        }
        : defaultResponder(url)));
    env.evaluate("State.saveUser({ id: '600', name: 'W', role: 'worker', token: 'tok-abc-123' })");
    await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'))");
    return env;
}

function clockCardSnapshot(env) {
    const panel = env.evaluate("document.getElementById('workerDashboard').innerHTML");
    const note = env.evaluate("document.getElementById('shiftOvertimeNote')");
    return {
        has_elapsed_slot: panel.includes('id="shiftElapsed"'),
        has_note_slot: panel.includes('id="shiftOvertimeNote"'),
        timer_running: env.evaluate('WORKER_MODULES._elapsedTimer !== null'),
        shown_label: env.evaluate("document.getElementById('shiftElapsed').textContent"),
        note_text: note ? note.innerHTML : null,
        note_hidden: note ? note.classList.contains('hidden') : null,
        toasts: env.evaluate(
            "(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)"
        )
    };
}

// Advances the page's clock, delivers one tick, and reports what the card shows.
function tickAfter(env, seconds) {
    env.advanceClock(seconds * 1000);
    env.tickIntervals();
    return clockCardSnapshot(env);
}

function defaultResponder(url) {
    if (url.includes('/auth/login')) return { status: 200, body: LOGIN_BODY };
    if (url.includes('/admin/active_sessions')) return { status: 200, body: [] };
    if (url.includes('/worker/stats/')) return { status: 200, body: { total_hours: 0 } };
    if (url.includes('/admin/logs')) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

(async () => {
    const results = {};

    // 1. the login response's token must be what the next request carries
    {
        const env = boot();
        env.setResponder(defaultResponder);
        await env.evaluate('UI.init()');   // renders the login screen, registering its handler
        await env.submitLogin();
        const tokenAfterLogin = env.evaluate('State.token');
        await env.evaluate("API.request('/admin/logs')");
        const header = env.requests.filter((r) => r.url.includes('/admin/logs')).map((r) => r.headers.Authorization)[0];
        results.login = {
            token_after_login: tokenAfterLogin,
            session_persisted: !!env.evaluate("(JSON.parse(sessionStorage.getItem('user')) || {}).token"),
            authorization_header: header,
            stored_user_has_token: env.evaluate('!!(State.user && State.user.token)'),
            toasts: env.toasts
        };
    }

    // 2. a session restored without a token is unusable and must be discarded
    {
        const env = boot({ session: { id: '600', name: 'Seed Lead Worker', role: 'moallem' } });
        env.setResponder(defaultResponder);
        const before = env.evaluate('State.user && State.user.id');
        await env.evaluate('UI.init()');
        results.restored_session = {
            before: before,
            after: env.evaluate('State.user'),
            logged_out: env.evaluate('State.user === null'),
            toasts: env.evaluate(
                "(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)"
            ),
            alerts: env.toasts
        };
    }

    // 3. a 401 on a request that carried a token signs the worker out, with a reason
    {
        const env = boot();
        env.setResponder((url) => (url.includes('/admin/logs')
            ? { status: 401, body: { detail: 'Invalid token' } }
            : defaultResponder(url)));
        env.evaluate("State.saveUser({ id: '600', name: 'W', role: 'moallem', token: 'tok-abc-123' })");
        let message = null;
        try {
            await env.evaluate("API.request('/admin/logs')");
        } catch (err) {
            message = err.message;
        }
        results.expired = {
            message,
            signed_out: env.evaluate('State.user === null'),
            session_cleared: env.evaluate("sessionStorage.getItem('user') === null")
        };
    }

    // 4. even with no token at all, the server's wording must not reach the worker
    {
        const env = boot();
        env.setResponder((url) => (url.includes('/auth/me')
            ? { status: 401, body: { detail: 'Invalid token' } }
            : defaultResponder(url)));
        let message = null;
        try {
            await env.evaluate("API.request('/auth/me')");
        } catch (err) {
            message = err.message;
        }
        results.no_token_401 = { message };
    }

    // 5. a wrong password on the sign-in form must still say so
    {
        const env = boot();
        env.setResponder((url) => (url.includes('/auth/login')
            ? { status: 401, body: { detail: 'Invalid credentials or user ID' } }
            : defaultResponder(url)));
        await env.evaluate('UI.init()');
        await env.submitLogin();
        results.bad_password = {
            toasts: env.evaluate(
                "(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)"
            )
        };
    }

    // 6. the clock button must follow the server's view of the open shift, and must
    //    not depend on a route the worker is not allowed to read
    {
        const env = boot();
        env.setResponder((url) => (url.includes('/worker/me/stats')
            ? {
                status: 200,
                body: {
                    worker_id: '600',
                    total_hours: 3.5,
                    active_session: { site_name: 'Downtown Tower A', clock_in_time: '2026-09-12 05:12:30' }
                }
            }
            : defaultResponder(url)));
        env.evaluate("State.saveUser({ id: '600', name: 'W', role: 'worker', token: 'tok-abc-123' })");
        await env.evaluate("WORKER_MODULES.renderClockPanel(document.getElementById('workerDashboard'))");
        const panel = env.evaluate("document.getElementById('workerDashboard').innerHTML");
        results.clock_panel = {
            offers_clock_out: panel.includes("handleClock('Clock Out')"),
            offers_clock_in: panel.includes("handleClock('Clock In')"),
            shows_site: panel.includes('Downtown Tower A'),
            shows_hours: panel.includes('3.5'),
            read_admin_sessions: env.requests.some((r) => r.url.includes('/admin/active_sessions')),
            used_self_scoped_path: env.requests.some((r) => r.url.includes('/worker/me/stats'))
        };
    }

    // 7. an over-long open shift must be visible to the worker, live
    {
        const env = await bootOpenShift(9 * 3600 + 5 * 60);
        results.overtime_shift = clockCardSnapshot(env);
        env.evaluate('WORKER_MODULES.stopElapsedTimer()');
    }

    // 8. a shift that has not run long yet shows the timer but no warning
    {
        const env = await bootOpenShift(2 * 3600 + 61);
        results.short_shift = clockCardSnapshot(env);
        env.evaluate('WORKER_MODULES.stopElapsedTimer()');
    }

    // 9. the threshold must come from the server, not from a hardcoded 8.1
    {
        const env = await bootOpenShift(8 * 3600, { overtime_notify_hours: 7.5 });
        results.retuned_threshold = clockCardSnapshot(env);
        env.evaluate('WORKER_MODULES.stopElapsedTimer()');
    }

    // 9b. a server still sending a cutoff must not turn it into a close claim
    {
        const env = await bootOpenShift(9 * 3600, { hard_cutoff_hours: 11.0 });
        results.legacy_cutoff = clockCardSnapshot(env);
        env.evaluate('WORKER_MODULES.stopElapsedTimer()');
    }

    // 10. the live part: an open card must tip over on its own as the shift runs on.
    //     The threshold is in *paid* hours, so with the shipped 30-minute unpaid break
    //     the card tips at 8.6 h on site: 7:55 on site is 7:25 of work and is inside the
    //     line, 8:41 on site is 8:11 of work and is past it.
    {
        const env = await bootOpenShift(7 * 3600 + 55 * 60);   // 7:25 paid, inside
        const before = clockCardSnapshot(env);
        const wellBefore = tickAfter(env, 60);
        const after = tickAfter(env, 45 * 60);                  // 8:11 paid, past it
        env.evaluate('WORKER_MODULES.stopElapsedTimer()');
        const stopped = env.evaluate('WORKER_MODULES._elapsedTimer === null');
        results.crossing = { before, well_before: wellBefore, after, stopped };
    }

    process.stdout.write(JSON.stringify(results));
})().catch((err) => {
    console.error((err && err.stack) || String(err));
    process.exit(1);
});
"""


@pytest.fixture(scope="module")
def harness_script(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("frontend_auth") / "harness.js"
    path.write_text(HARNESS, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def results(harness_script) -> dict:
    completed = subprocess.run(
        [NODE, str(harness_script), str(FRONTEND)],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, f"frontend harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def test_login_stores_the_token_beside_the_user(results):
    """The regression: storing only res.user left every later call 'Bearer dummy'."""
    assert results["login"]["token_after_login"] == "tok-abc-123"
    assert results["login"]["stored_user_has_token"] is True
    assert results["login"]["session_persisted"] is True, "a reload must keep the token"


def test_authenticated_requests_carry_the_real_token(results):
    assert results["login"]["authorization_header"] == "Bearer tok-abc-123", (
        "a placeholder here is what the server answers with 401 Invalid token"
    )
    assert results["login"]["toasts"] == [], f"no error was expected: {results['login']['toasts']}"


def test_a_restored_session_without_a_token_is_discarded(results):
    assert results["restored_session"]["before"] == "600", "the harness must restore a session"
    assert results["restored_session"]["logged_out"] is True
    assert results["restored_session"]["after"] is None
    shown = results["restored_session"]["toasts"] + results["restored_session"]["alerts"]
    assert shown == ["Your session expired. Sign in again."], (
        "the worker must be told why they are back at the sign-in screen"
    )


def test_a_401_signs_the_worker_out_with_an_actionable_message(results):
    assert results["expired"]["message"] == "Your session expired. Sign in again."
    assert results["expired"]["signed_out"] is True
    assert results["expired"]["session_cleared"] is True


def test_a_401_without_a_token_is_never_shown_as_invalid_token(results):
    """The wording the worker saw must be unreachable, token or no token."""
    assert results["no_token_401"]["message"] == "Your session expired. Sign in again."
    assert "Invalid token" not in results["no_token_401"]["message"]


def test_a_wrong_password_still_reports_a_wrong_password(results):
    assert results["bad_password"]["toasts"] == ["Invalid credentials or user ID"]


def test_an_open_shift_makes_the_panel_offer_clock_out(results):
    """The bug that followed the token fix: "Already clocked in!" on every tap."""
    panel = results["clock_panel"]
    assert panel["offers_clock_out"] is True, "an open shift must offer Clock Out"
    assert panel["offers_clock_in"] is False, "Clock In is what produced Already clocked in!"
    assert panel["shows_site"] is True
    assert panel["shows_hours"] is True


def test_an_over_long_shift_shows_a_live_timer_and_the_approval_note(results):
    """The worker, not only an admin, must see that the shift has run past 8.1h paid."""
    card = results["overtime_shift"]
    assert card["has_elapsed_slot"] is True, "the elapsed time needs a node to tick into"
    assert card["has_note_slot"] is True, "the overtime note needs a node to be revealed in"
    assert card["timer_running"] is True, "the timer must tick, not freeze at render time"
    assert card["shown_label"].startswith("9:0"), f"expected ~9h on the clock: {card['shown_label']}"
    assert card["note_hidden"] is False, "past the line the note must be visible"
    # The title names the line the worker has reached and the basis it is counted on - the
    # note is the mirror of the administrator's alert, not a promise about a pay decision
    # (that depends on the line against the paid day; see ``shift_hours.overtime_assessment``).
    assert "8.1" in card["note_text"] and "paid" in card["note_text"], card["note_text"]
    assert any("8.1" in shown for shown in card["toasts"]), (
        "crossing the line is worth interrupting the worker for"
    )


def test_a_shift_inside_the_threshold_keeps_the_warning_hidden(results):
    card = results["short_shift"]
    assert card["timer_running"] is True
    assert card["shown_label"].startswith("2:0"), f"expected ~2h on the clock: {card['shown_label']}"
    assert card["note_hidden"] is True, "no overtime means no alarm"
    assert "overtime" not in (card["note_text"] or "")
    assert card["toasts"] == [], f"nothing to warn about: {card['toasts']}"


def test_the_overtime_warning_follows_the_servers_threshold(results):
    """An admin who retunes shift_rules must not leave the card warning about 8.1."""
    card = results["retuned_threshold"]
    assert card["note_hidden"] is False, "8h is past the retuned 7.5h threshold"
    assert "7.5" in card["note_text"]
    assert "8.1" not in card["note_text"], "the default threshold leaked back in"


def test_the_overtime_note_promises_no_automatic_close(results):
    """The panel must not state a rule the server does not have.

    ``/worker/me/stats`` has never sent ``hard_cutoff_hours``; the panel read it,
    fell back to a hardcoded 11, and told the worker "The shift closes
    automatically at 11h." - while ``enforce_11h_cutoff`` had been removed from
    the app on purpose (an invented close invents hours). A worker could plan
    around a clock-out that was never coming.
    """
    note = results["overtime_shift"]["note_text"]
    assert note is not None, "the snapshot must carry the rendered note"
    for name in ("overtime_shift", "legacy_cutoff"):
        rendered = results[name]["note_text"] or ""
        lowered = rendered.lower()
        assert "automatic" not in lowered and "closes" not in lowered, (
            f"{name} still promises a close nobody performs: {rendered!r}"
        )


def test_the_card_ticks_and_raises_the_warning_as_the_shift_crosses_the_line(results):
    """A worker watching the card sees it go over - no reload, no admin required."""
    crossing = results["crossing"]
    assert crossing["before"]["shown_label"].startswith("7:5"), crossing["before"]["shown_label"]
    assert crossing["before"]["note_hidden"] is True, "7h55m is still inside the 8.1h threshold"
    assert crossing["well_before"]["shown_label"] != crossing["before"]["shown_label"], (
        "the label must advance every tick, not freeze at render time"
    )
    assert crossing["well_before"]["note_hidden"] is True
    assert crossing["after"]["shown_label"].startswith("8:4"), crossing["after"]["shown_label"]
    assert crossing["after"]["note_hidden"] is False, (
        "past 8.1h of paid work (8:41 on site, not 8:10) the note must appear on its own"
    )
    assert "8.1" in crossing["after"]["note_text"]
    assert crossing["after"]["toasts"], "crossing the threshold is worth interrupting the worker for"
    assert not crossing["before"]["toasts"], "but there was nothing to say before it crossed"
    assert crossing["stopped"] is True, "the interval must be released, not left running"


def test_the_panel_uses_the_self_scoped_endpoint_only(results):
    panel = results["clock_panel"]
    assert panel["used_self_scoped_path"] is True
    assert panel["read_admin_sessions"] is False, (
        "a worker is not allowed to read /admin/active_sessions, so asking is a silent 403"
    )
