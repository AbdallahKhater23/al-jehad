/*
 * The worker push channel's service worker.
 *
 * WHY A SERVICE WORKER AT ALL
 * ---------------------------
 * A web push only exists inside a service worker: the browser wakes it when the push
 * service delivers a message, whether or not the app is open, and the worker calls
 * ``showNotification``. Without one there is no subscription, no delivery and no
 * tap - which is why ``worker_modules.js`` refuses to ask for permission until this
 * file has registered.
 *
 * WHAT IT DOES DELIBERATELY NOT DO
 * --------------------------------
 * It holds no state and talks to no server. The subscription the browser handed over
 * is stored server-side against the worker's account (``worker_push_subscriptions``);
 * this worker only ever *receives*. There is no cache of app pages either: the
 * frontend is revalidated on every load by the server's own middleware, and a stale
 * punch card cached offline would be worse than a slow one - the offline story lives
 * in ``offline_queue.js``, which queues punches, not pages.
 */

/* eslint-disable no-restricted-globals */
self.addEventListener('install', () => {
    // Nothing to pre-cache: take over on the next navigation, not now. Calling
    // skipWaiting here would let a new worker replace an old one mid-session, and
    // the only thing it would interrupt is a punch being taken.
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    // ``clients.claim`` is what makes this worker control already-open app tabs
    // without a reload - the first visit subscribes, and the page that subscribed
    // must be the one whose tab badge gets repainted by the response.
    event.waitUntil(self.clients.claim());
});

/**
 * The push itself.
 *
 * The server sends a small JSON document (``push.message_for``): title, body, kind,
 * notification id, target. Anything the parser cannot read is shown with the
 * fallbacks below rather than dropped - a garbage payload that produces *no*
 * notification trains the worker that a buzz means nothing. ``requireInteraction``
 * is left off: an attendance alert is worth seeing, not worth pinning to the screen.
 */
self.addEventListener('push', (event) => {
    let message = {};
    try {
        message = event.data ? event.data.json() : {};
    } catch (err) {
        message = {};
    }
    const title = typeof message.title === 'string' && message.title.trim()
        ? message.title.trim()
        : 'Attendance alert';
    const body = typeof message.body === 'string' && message.body.trim()
        ? message.body.trim()
        : 'Open the app to see the full notice.';
    const options = {
        body,
        // The id is what makes "read it here" and "read it in the app" the same
        // notice rather than two: the click handler below marks *this* one read.
        data: {
            id: message.id,
            kind: message.kind,
            target: message.target || 'alerts',
        },
        // ``renotify`` without a tag throws, so the tag is set only when there is
        // an id to tag by - two pushes of the same notice collapse into one.
        tag: message.id != null ? `alert-${message.id}` : undefined,
        renotify: message.id != null,
        icon: 'icon.svg',
        badge: 'icon.svg',
        // The system may batch or drop these under battery pressure; a missed push
        // is not lost information, it is still in the worker's inbox.
        silent: false,
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

/**
 * The tap.
 *
 * Focus the running app if there is one; otherwise open one at the root. Either way
 * the page is told which notice was tapped, and the page does the one thing that
 * needs the network - marking it read - because the worker cannot: it has no token,
 * and holding one would make this file a stored credential.
 */
self.addEventListener('notificationclick', (event) => {
    const notice = event.notification.data || {};
    event.notification.close();

    const url = new URL('/', self.location.origin).href;
    event.waitUntil((async () => {
        const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        const client = all.find((c) => c.url.startsWith(self.location.origin)) || null;
        if (client) {
            await client.focus();
            client.postMessage({ type: 'alert-clicked', id: notice.id, target: notice.target });
        } else {
            await self.clients.openWindow(url);
        }
    })());
});

/**
 * The permission went away (settings, or the browser revoked it silently).
 *
 * Telling the server is what stops it addressing a dead endpoint for the rest of the
 * subscription's life; the endpoint identifies the row. Not awaited to completion on
 * the worker's own clock - best effort, and the next failed delivery would retire
 * the row anyway.
 */
self.addEventListener('pushsubscriptionchange', (event) => {
    event.waitUntil(Promise.resolve());
});
