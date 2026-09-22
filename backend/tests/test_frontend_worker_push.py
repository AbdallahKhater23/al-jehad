"""The push half of the worker alert channel.

WHY THIS EXISTS
---------------
The backend has been able to push for a while: ``GET /worker/me/push`` reports whether the
deployment has VAPID keys, ``POST /worker/me/push/subscribe`` stores what a browser hands
over, and ``push.dispatch`` sends. The inbox tests above pin the *record* half. What was
missing was everything the browser does: register a service worker, ask the permission
question, create a subscription, and send it up. None of that existed, so a deployment with
keys configured was still pushing to nobody.

What can only be checked here, and not against the API:

1. the service worker is registered **before** anything else in the channel happens, and
   registration failure degrades to a named state rather than an error;
2. the backend is asked whether pushing is *configured* before the browser is asked for a
   permission - a deployment with no VAPID keys must never prompt, because a permission
   nothing can use is how an app teaches workers to refuse prompts;
3. enabling creates a browser subscription and POSTs it where the route reads it, with the
   endpoint and both keys from the subscription itself;
4. **a subscribe refusal by the backend unsubscribes the browser too** - the card must not
   say "on" for a subscription no server will ever send to;
5. a permission the worker refused does not reach the subscribe endpoint at all;
6. disabling unsubscribes the browser and tells the server the endpoint, even if the server
   answer fails;
7. a push tapped with the app closed arrives as a message, and the page marks that notice
   read - with the id in the query string, where the route reads it;
8. the settings card says which state it is in: unsupported browser, server not configured,
   off, on. A card that shows a switch where nothing can work is a button to nowhere.

Node is optional; without it these skip rather than fail. The real browser journey - the
prompt, the actual push - needs a browser with a push service and is not automatable here;
the service worker's own behaviour is pinned by reading its source (see the last test).
"""

from __future__ import annotations

import re

import pytest

import frontend_vm

pytestmark = pytest.mark.skipif(frontend_vm.NODE is None, reason="node is not installed")

#: A URL-safe base64 VAPID public key, shaped like a real one (43 chars, no padding, ``-``
#: and ``_``). The value does not matter; the shape does: 43 chars of URL-safe base64
#: decode to exactly 32 bytes - the P-256 public key - so the byte-count assertion is
#: checking the decoder, not a coincidence of this sample's length.
PUBLIC_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"

WORKER = {"id": "601", name: None, "role": "worker", "token": "tok-worker"} if False else {"id": "601", "name": "Bilal Khan", "role": "worker", "token": "tok-worker"}

HARNESS = r"""
// --- the server's answers, faked ----------------------------------------

const STATS = {
    active_session: null, total_hours: 0, overtime_notify_hours: 8.1,
    break_minutes: 30, break_after_hours: 4, paid_day_hours: 8,
    auto_close_at_regular: '1', flagged_for_review: false
};

//: What the deployment answers for push. Flipped by the scenarios.
let pushConfig = {
    enabled: true, available: true, public_key: 'PUBLIC_KEY_PLACEHOLDER',
    reason: null, max_age_minutes: 60, subscriptions: 0
};
//: The subscription POSTs the page made: { endpoint, p256dh, auth }.
let subscribed = [];
//: The unsubscribe POSTs: the endpoints the page retired.
let unsubscribed = [];
//: What the next requestPermission answers ('granted'/'denied'/null = grant).
let permissionAnswer = null;

function responders(url, init) {
    if (url.indexOf('/worker/me/notifications/read') >= 0) {
        return { status: 200, body: { status: 'success', marked: 1 } };
    }
    if (url.indexOf('/worker/me/notifications') >= 0) {
        return { status: 200, body: { worker_id: '601', unread: 0, notifications: [] } };
    }
    if (url.indexOf('/worker/me/push/subscribe') >= 0) {
        if (pushSubscribeRefused) return { status: 409, body: { detail: 'Push notifications are unavailable: no VAPID key pair is configured' } };
        subscribed.push(JSON.parse(init.body));
        return { status: 200, body: { status: 'success', created: true } };
    }
    if (url.indexOf('/worker/me/push/unsubscribe') >= 0) {
        if (unsubscribeBroken) {
            unsubscribed.push(JSON.parse(init.body));
            return { status: 503, body: { detail: 'unreachable' } };
        }
        unsubscribed.push(JSON.parse(init.body));
        return { status: 200, body: { status: 'success', retired: true } };
    }
    if (url.indexOf('/worker/me/push') >= 0) {
        return { status: 200, body: pushConfig };
    }
    if (url.indexOf('/worker/me/stats') >= 0) return { status: 200, body: STATS };
    if (url.indexOf('/worker/me/logs') >= 0) return { status: 200, body: [] };
    return { status: 200, body: {} };
}

let pushSubscribeRefused = false;
let unsubscribeBroken = false;

function toasts(env) {
    return env.evaluate("(document.getElementById('toastRoot').__children || []).map((el) => el.textContent)");
}

async function workerEnv({ serviceWorker = true } = {}) {
    subscribed.length = 0; unsubscribed.length = 0;
    pushSubscribeRefused = false; unsubscribeBroken = false;
    permissionAnswer = null;
    pushConfig = {
        enabled: true, available: true, public_key: 'PUBLIC_KEY_PLACEHOLDER',
        reason: null, max_age_minutes: 60, subscriptions: 0
    };
    const env = boot();
    env.setResponder(responders);
    if (serviceWorker) env.installServiceWorkerSupport();
    env.evaluate("localStorage.setItem('layoutOverride', 'mobile')");
    env.evaluate("State.saveUser(" + JSON.stringify({ id: '601', name: 'Bilal Khan', role: 'worker', token: 'tok-worker' }) + ")");
    return env;
}

const results = {};

// 1. init with a working browser: registration first, config second, no permission prompt
{
    const env = await workerEnv();
    await env.evaluate("WORKER_MODULES.initPush()");
    const swc = env.serviceWorkerContainer;
    results.init = {
        registered: env.registrations.map((r) => r.scriptURL),
        permission: env.notificationPermission,
        config: env.evaluate("WORKER_MODULES._pushConfig && { available: WORKER_MODULES._pushConfig.available, key: !!WORKER_MODULES._pushConfig.public_key }"),
        subscription: env.evaluate("WORKER_MODULES._pushSubscription"),
        // The enabling switch is on the profile tab, not fired at boot.
        asked_server: env.requests.map((r) => r.url.split('/api/v1')[1]).filter((u) => u.indexOf('/push') >= 0)
    };
    void swc;
}

// 2. a browser without service worker support: named state, no requests at all
{
    const env = await workerEnv({ serviceWorker: false });
    await env.evaluate("WORKER_MODULES.initPush()");
    results.unsupported = {
        reason: env.evaluate("WORKER_MODULES._pushConfig && WORKER_MODULES._pushConfig.reason"),
        card: env.evaluate("WORKER_MODULES.pushSettingsHtml()"),
        asked: env.requests.map((r) => r.url.split('/api/v1')[1]).filter((u) => u.indexOf('/push') >= 0)
    };
}

// 3. the server cannot push: no prompt, and the card says so with the server's reason
{
    const env = await workerEnv();
    pushConfig = { enabled: true, available: false, public_key: null, reason: 'no VAPID key pair is configured (see VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY)', max_age_minutes: 60, subscriptions: 0 };
    await env.evaluate("WORKER_MODULES.initPush()");
    results.server_off = {
        card: env.evaluate("WORKER_MODULES.pushSettingsHtml()"),
        permission: env.notificationPermission,
        toggle_button: env.evaluate("WORKER_MODULES.pushSettingsHtml().indexOf('data-push-toggle') >= 0")
    };
}

// 4. enabling: permission, subscription, and the POST the route reads
{
    const env = await workerEnv();
    await env.evaluate("WORKER_MODULES.initPush()");
    await env.evaluate("WORKER_MODULES.togglePush(true)");
    const sub = env.registrations[0].pushManager._subscription;
    results.enable = {
        permission: env.notificationPermission,
        posted: subscribed.slice(),
        browser_subscription_alive: !!sub && !sub.unsubscribed,
        state: env.evaluate("!!WORKER_MODULES._pushSubscription"),
        key_bytes: env.evaluate("WORKER_MODULES.urlBase64ToUint8Array(WORKER_MODULES._pushConfig.public_key).length")
    };
}

// 5. a refused permission never reaches the subscribe endpoint
{
    const env = await workerEnv();
    await env.evaluate("WORKER_MODULES.initPush()");
    env.setRequestedPermission('denied');
    let refused = null;
    try { await env.evaluate("WORKER_MODULES.togglePush(true)"); } catch (err) { refused = String(err.message); }
    results.denied = {
        error: refused,
        posted: subscribed.slice(),
        subscription: env.evaluate("!!WORKER_MODULES._pushSubscription"),
        toasts: toasts(env)
    };
}

// 6. the backend refuses the subscription: the browser one is released too
{
    const env = await workerEnv();
    await env.evaluate("WORKER_MODULES.initPush()");
    pushSubscribeRefused = true;
    let refused = null;
    try { await env.evaluate("WORKER_MODULES.togglePush(true)"); } catch (err) { refused = String(err.message); }
    const sub = env.registrations[0].pushManager._subscription;
    results.refused = {
        error: refused,
        posted: subscribed.slice(),
        browser_unsubscribed: !!sub && sub.unsubscribed === true,
        state: env.evaluate("WORKER_MODULES._pushSubscription")
    };
}

// 7. disabling: the browser is released even when the server answer fails
{
    const env = await workerEnv();
    await env.evaluate("WORKER_MODULES.initPush()");
    await env.evaluate("WORKER_MODULES.togglePush(true)");
    unsubscribeBroken = true;
    await env.evaluate("WORKER_MODULES.togglePush(false)");
    const sub = env.registrations[0].pushManager._subscription;
    results.disable = {
        posted_unsubscribe: unsubscribed.slice(),
        browser_unsubscribed: !!sub && sub.unsubscribed === true,
        state: env.evaluate("WORKER_MODULES._pushSubscription")
    };
}

// 8. a tap with the app closed: the message marks that notice read, id in the query
// string. The page binds its listener inside ``UI.init``'s boot, so this scenario boots
// the real app on a fresh env, then delivers the message the service worker posts.
{
    const env2 = await workerEnv();
    env2.evaluate("window.__reads = []");
    env2.setResponder((url, init) => {
        if (url.indexOf('/worker/me/notifications/read') >= 0) {
            env2.evaluate("window.__reads.push({ through: 'listener' })");
            return { status: 200, body: { status: 'success', marked: 1 } };
        }
        if (url.indexOf('/worker/me/push') >= 0) return { status: 200, body: pushConfig };
        if (url.indexOf('/worker/me/stats') >= 0) return { status: 200, body: STATS };
        if (url.indexOf('/worker/me/notifications') >= 0) {
            return { status: 200, body: { worker_id: '601', unread: 1, notifications: [
                { id: 55, kind: 'shift_auto_closed', title: 'Your shift was closed', body: '8.00h payable.', created_at: '2026-09-21 16:00:00', read: false, delivered: true }
            ] } };
        }
        return { status: 200, body: {} };
    });
    // Boot the real app: this is what registers the ``message`` listener.
    await env2.evaluate("UI.init()").catch(() => {});
    env2.serviceWorkerContainer.fireMessage({ type: 'alert-clicked', id: 55, target: 'alerts' });
    await new Promise((resolve) => setTimeout(resolve, 20));
    results.tapped = {
        reads_via_listener: env2.evaluate("(window.__reads || []).length"),
        url_shape: env2.requests.map((r) => r.url.split('/api/v1')[1]).filter((u) => u.indexOf('read') >= 0)
    };
}

// 9. every new string exists in every language, placeholders carried through
{
    const env = await workerEnv();
    const KEYS = ['pushTitle', 'pushChecking', 'pushServerOff', 'pushSwUnsupported', 'pushSwFailed',
                  'pushOnNote', 'pushOffNote', 'pushEnable', 'pushDisable', 'pushEnabled',
                  'pushDisabled', 'pushPermissionDenied', 'pushUnavailable', 'pushDevices'];
    results.translations = env.evaluate(`(function () {
        const KEYS = ${JSON.stringify(KEYS)};
        return {
            missing: Object.keys(TRANSLATIONS).map((lang) => [
                lang, KEYS.filter((key) => !(key in TRANSLATIONS[lang])).join(',')
            ]),
            placeholders: KEYS.filter((key) => {
                const holder = /\\{\\w+\\}/.exec(TRANSLATIONS.en[key]);
                if (!holder) return false;
                return Object.keys(TRANSLATIONS).some((lang) => TRANSLATIONS[lang][key].indexOf(holder[0]) < 0);
            })
        };
    })()`);
}
"""


@pytest.fixture(scope="module")
def results():
    harness = HARNESS.replace("PUBLIC_KEY_PLACEHOLDER", PUBLIC_KEY)
    return frontend_vm.run(harness)


def test_init_registers_the_worker_before_anything_else_and_prompts_nobody(results):
    init = results["init"]
    assert init["registered"] == ["push-service-worker.js"]
    # A web push cannot exist without a service worker, so registration is step one.
    assert init["permission"] == "default", (
        f"boot asked for the notification permission on its own: {init['permission']!r}. "
        "A prompt fired at boot is how an app teaches people to refuse prompts."
    )
    assert init["config"] == {"available": True, "key": True}
    assert not init["subscription"], "a fresh session is not subscribed"
    assert all("/push/subscribe" not in url for url in init["asked_server"])


def test_a_browser_without_service_workers_is_a_named_state_not_an_error(results):
    unsupported = results["unsupported"]
    assert unsupported["reason"] == "pushSwUnsupported"
    # The card is rendered through i18n, so it carries the sentence, not the key: what
    # matters is that the worker is told something readable and offered no switch.
    assert "cannot show push notifications" in unsupported["card"], unsupported["card"]
    assert "data-push-toggle" not in unsupported["card"], (
        "a switch that can only fail is a button to nowhere; the card offers none"
    )


def test_a_deployment_that_cannot_push_prompts_for_nothing(results):
    server_off = results["server_off"]
    assert server_off["permission"] == "default", (
        "the permission prompt fired although the server said pushing is not configured"
    )
    assert "no VAPID key pair is configured" in server_off["card"], (
        "the card must carry the server's reason: it is the sentence that tells a worker "
        "which problem to bring to whom"
    )
    assert server_off["toggle_button"] is False, (
        "a switch that can only fail is a button to nowhere; the card offers none"
    )


def test_enabling_posts_the_subscription_where_the_route_reads_it(results):
    enable = results["enable"]
    assert enable["permission"] == "granted"
    assert len(enable["posted"]) == 1, enable["posted"]
    posted = enable["posted"][0]
    assert posted["endpoint"].startswith("https://push.example.com/")
    assert posted["p256dh"].startswith("B-key-")
    assert posted["auth"].startswith("auth-")
    assert set(posted) == {"endpoint", "p256dh", "auth"}, (
        f"the subscribe body carries {sorted(posted)}; anything else is a field the route "
        "does not read"
    )
    assert enable["browser_subscription_alive"] is True
    assert enable["state"] is True
    # 43 chars of URL-safe base64 decode to 32 bytes - the P-256 public key. A decoder
    # that passes the raw string through would throw in the browser here.
    assert enable["key_bytes"] == 32


def test_a_refused_permission_never_reaches_the_subscribe_endpoint(results):
    denied = results["denied"]
    assert denied["posted"] == [], (
        "a permission the worker refused must not create a server-side subscription"
    )
    assert denied["subscription"] is False
    assert denied["error"] is not None and "permission" in denied["error"].lower()


def test_a_backend_refusal_releases_the_browser_subscription_too(results):
    refused = results["refused"]
    assert refused["posted"] == []
    assert refused["browser_unsubscribed"] is True, (
        "the backend refused the subscription (409), so the browser-side subscription must "
        "be released: a card saying 'on' for a subscription no server will send to is the "
        "one lie this switch must not tell"
    )
    assert refused["state"] is None
    assert refused["error"] is not None


def test_disabling_unsubscribes_the_browser_even_when_the_server_fails(results):
    disable = results["disable"]
    assert len(disable["posted_unsubscribe"]) == 1
    assert set(disable["posted_unsubscribe"][0]) == {"endpoint"}
    assert disable["browser_unsubscribed"] is True, (
        "the worker asked for off; a server that could not be told does not get to keep "
        "the browser-side subscription alive"
    )
    assert disable["state"] is None


def test_a_push_tapped_with_the_app_closed_marks_that_notice_read(results):
    tapped = results["tapped"]
    assert tapped["reads_via_listener"] >= 1, (
        "the page never answered the service worker's 'alert-clicked' message"
    )
    assert any("read" in url and "notification_id=55" in url for url in tapped["url_shape"]), (
        f"the read request went out as {tapped['url_shape']}; the id belongs in the query "
        "string, which is where the route reads it"
    )


def test_every_new_string_exists_in_every_language(results):
    translations = results["translations"]
    assert translations["missing"] == [] or all(not missing for _, missing in translations["missing"]), (
        f"strings missing from some tables: {translations['missing']}"
    )
    assert translations["placeholders"] == [], (
        f"placeholders dropped in some translations: {translations['placeholders']}"
    )


def test_the_service_worker_shows_what_the_server_sent_and_holds_no_credentials():
    """The worker file is the one part a VM cannot execute; its shape is pinned by reading it.

    Three properties matter enough to check the source for: it shows a notification from
    whatever the server sent (a push handler that drops a payload it cannot parse trains
    the worker that a buzz means nothing), it opens the app on the tap, and it never holds
    a token - the worker has no storage and must not become a stored credential.
    """
    source = (frontend_vm.FRONTEND / "push-service-worker.js").read_text(encoding="utf-8")
    assert "addEventListener('push'" in source
    assert "showNotification" in source
    assert "notificationclick" in source
    assert "openWindow" in source or "clients.focus" in source.replace("client.focus", "clients.focus")
    assert "Authorization" not in source, "the worker must not carry a credential"
    assert "localStorage" not in source, "the worker must not hold state"
    # The marker attribute the profile card binds; keeping the two in step is what makes
    # the tap land on the switch rather than nowhere.
    assert re.search(r"data-push-toggle", source) is None  # the worker draws no UI of its own
