"""Web Push: the channel that reaches a worker whose app is closed.

WHAT THIS IS FOR
----------------
Every worker-facing fact in this application used to require the worker to be looking:
the clock panel raises its overtime note on a timer, and if the phone is in a pocket the
moment passes unannounced. The administrator gets a row in ``admin_notifications``; the
worker gets nothing. This module is the worker's half - a subscription per browser, and a
push per event - with the inbox (``worker_notifications``) as the record that survives a
phone that was off.

THREE STATES, SAID OUT LOUD
---------------------------
``transport_available()`` returns ``(ok, reason)``, and every caller branches on it:

* **ready** - ``PUSH_ENABLED``, a VAPID key pair and the optional ``pywebpush`` package;
  ``GET /worker/me/push`` tells the browser the public key and the app offers to subscribe;
* **not configured** - the honest default in this repository, because a VAPID key pair is a
  deployment secret that cannot ship here. ``GET /worker/me/push`` says which piece is
  missing, the app does not ask the worker for a notification permission it cannot use, and
  ``readiness`` raises it as an advisory check. The inbox still works;
* **switched off** - ``PUSH_ENABLED=0``, for a deployment that would rather nobody's phone
  rang.

THE NETWORK IS NEVER IN THE TRANSACTION
---------------------------------------
A push is an HTTPS request to somebody else's service. Writing one into the punch's write
transaction would hold a SQLite lock across a third party - and a worker standing at a gate
waiting for a phone to ring. So ``notify_worker`` writes the row inside the caller's
transaction, and ``dispatch_async`` runs the sends *after* it commits, on a daemon thread,
exactly the way the face engine's workers are kept out of the request path.

Nothing here retries for ever: a notification older than ``PUSH_MAX_AGE_MINUTES`` is not
worth waking a phone for (that is what the inbox is for), and a subscription the push
service has told us is gone (404/410) is revoked rather than pushed to again.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import notifications
from config import settings
from database import db

log = logging.getLogger("attendance.push")

TS = "%Y-%m-%d %H:%M:%S"

#: Where a tapped notification should land. The app is a single page, so the root is the
#: inbox; the hash is what the service worker uses to avoid opening a second tab.
DEFAULT_TARGET = "/#worker=notifications"


def _now() -> datetime:
    return datetime.now()


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime(TS)


# ---------------------------------------------------------------------------
# is the channel usable at all?
# ---------------------------------------------------------------------------
def _load_webpush():
    """The optional dependency, or ``None``. Imported late so a deployment without it pays
    nothing for the import and cannot fail at startup because of it."""
    try:
        from pywebpush import WebPushException  # noqa: PLC0415 - optional dependency
        from pywebpush import webpush  # noqa: PLC0415 - optional dependency

        return webpush, WebPushException
    except Exception:  # pragma: no cover - depends on the optional package
        return None


def transport_available() -> tuple[bool, str]:
    """``(usable, reason)``. The reason is shown to an operator and to the worker's app."""
    if not settings.push_enabled:
        return False, "push is switched off (PUSH_ENABLED=0)"
    if not settings.vapid_public_key or not settings.vapid_private_key:
        return False, "no VAPID key pair is configured (see VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY)"
    if _load_webpush() is None:
        return False, "the optional pywebpush package is not installed (pip install pywebpush)"
    return True, "ready"


def browser_config() -> dict:
    """What the worker's app needs, and the only place the public key is exposed.

    The private key is never returned from here - not to the browser, not in an error message.
    A deployment that is missing it is told so by the ``reason``, which names the *setting*.
    """
    usable, reason = transport_available()
    return {
        "enabled": bool(settings.push_enabled),
        "available": usable,
        "public_key": settings.vapid_public_key if usable else None,
        "reason": reason,
        "max_age_minutes": int(settings.push_max_age_minutes),
    }


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------
#: A push endpoint is a URL the browser got from its push service. It is stored so the
#: server can send to it, which makes it a capability for that device: validate the shape,
#: cap the length, and never echo it back to anybody but its own worker.
MAX_ENDPOINT_CHARS = 2000
MAX_KEY_CHARS = 200

# ---------------------------------------------------------------------------
# where a push may be sent
# ---------------------------------------------------------------------------
# Not ``netguard``: that module decides who may *call* this API, from which address, and this
# decides where the server may *go*. The two are deliberately separate - an IP that is fine as
# a client is not a push service, and a push service must not be reachable as an admin route -
# and neither rule is expressible in terms of the other.
#
#: Suffixes that can never be a push service, whatever the allowlist says.
#:
#: The allowlist is the control; this is the second lock, for the case where somebody adds an
#: entry by hand without reading it. No browser produces an endpoint on a private suffix, and
#: a name that resolves inside the network is the entire point of an SSRF, so these are
#: refused before the list is even consulted.
NON_ROUTABLE_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".home.arpa",
    ".lan",
    ".corp",
    ".onion",
)


def configured_hosts() -> tuple[str, ...]:
    """The allowlist in force, normalised: lower case, no surrounding dots."""
    hosts = getattr(settings, "push_endpoint_hosts", ()) or ()
    return tuple(str(host).strip().lower().strip(".") for host in hosts if str(host).strip("."))


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """Whether ``host`` is an allowed host or a **subdomain** of one.

    Matching on the label boundary - ``host == entry or host.endswith('.' + entry)`` - so
    ``notify.windows.com.evil.test`` does not match ``notify.windows.com``. A plain
    ``endswith("notify.windows.com")`` would accept it, and that is the classic way a suffix
    allowlist is defeated.
    """
    return any(host == entry or host.endswith("." + entry) for entry in allowed)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def validate_endpoint(endpoint: str, *, allowed: tuple[str, ...] | None = None) -> str:
    """Return ``endpoint`` unchanged, or raise ``ValueError`` saying why it is refused.

    THE RULE, AND WHY IT IS AN ALLOWLIST
    ------------------------------------
    A subscription endpoint is a URL **the server will later POST to**, supplied by a worker.
    That is the definition of a server-side request forgery primitive, and the failure is not
    a wrong page: the request leaves from inside the network, it carries no credential of the
    worker's, and it is retried on every notification that follows.

    So the endpoint must be ``https`` on port 443, carry no credentials, and name a host that
    a real push service hands out (``PUSH_ENDPOINT_HOSTS``). Nothing else is accepted. Note
    what a denylist of private ranges would still allow: ``https://collector.attacker.test/``
    is a public host, reachable, and confirms to its owner that the server fetched it. "Not
    internal" is therefore not the property wanted - "is a push service" is.

    Parsed with ``urlsplit`` rather than matched as a string, because the difference between
    those two *is* the attack: ``https://fcm.googleapis.com@evil.test/`` has the host
    ``evil.test``, and any check that looked for an allowed name somewhere in the string would
    accept it.
    """
    text = str(endpoint or "").strip()
    if not text:
        raise ValueError("A push subscription needs an endpoint.")
    if len(text) > MAX_ENDPOINT_CHARS:
        raise ValueError(f"The endpoint is longer than {MAX_ENDPOINT_CHARS} characters.")
    # Whitespace and control characters are refused before parsing: a newline inside a URL is
    # how a value smuggles a second line into whatever builds the request, and no endpoint
    # needs one.
    if any(character <= " " or character == "\x7f" for character in text):
        raise ValueError("A push endpoint cannot contain whitespace or control characters.")

    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("A push subscription endpoint must be an https URL.")
    if parts.username or parts.password:
        raise ValueError("A push endpoint cannot carry credentials.")
    if not parts.hostname:
        raise ValueError("A push subscription endpoint must name a host.")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("A push endpoint must use the default HTTPS port.") from None
    if port not in (None, 443):
        raise ValueError("A push endpoint must use the default HTTPS port.")

    validate_host(parts.hostname, allowed=allowed)
    return text


def validate_host(host: str, *, allowed: tuple[str, ...] | None = None) -> str:
    """Return the normalised host, or raise ``ValueError`` saying why it is refused.

    Split out from ``validate_endpoint`` so that the **allowlist itself** can be held to the
    same rules - see ``readiness._check_push_endpoint_allowlist``. An entry in
    ``PUSH_ENDPOINT_HOSTS`` that this refuses is an entry no subscription can ever use, and
    leaving it in place produces a worker who subscribes successfully and is never notified.

    Two rules bite here regardless of what the list says, and they are the second lock behind
    it: an **address** is never a push service however public, and a **private suffix** is
    never one however it is spelled. That way an allowlist entry added by hand without
    reading the comment beside it - or an ``.env`` that names an internal host - cannot turn
    this back into a request-forgery primitive.
    """
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("A push subscription endpoint must name a host.")
    if _is_ip_literal(host):
        raise ValueError("A push endpoint must name a push service, not an IP address.")
    if any(character in host for character in "/:@?#"):
        raise ValueError("A push endpoint host cannot carry a scheme, port, path or credentials.")
    if "." not in host or host.endswith(NON_ROUTABLE_SUFFIXES):
        raise ValueError("A push endpoint must name a public push service.")

    allowed_hosts = configured_hosts() if allowed is None else tuple(allowed)
    if not _host_allowed(host, allowed_hosts):
        raise ValueError(
            "That endpoint is not a push service this server will send to. Notifications may "
            "only be delivered through a known push service (see PUSH_ENDPOINT_HOSTS)."
        )
    return host


def _clean_keys(endpoint: str, p256dh: str, auth: str) -> tuple[str, str, str]:
    endpoint = str(endpoint or "").strip()
    p256dh = str(p256dh or "").strip()
    auth = str(auth or "").strip()
    # At the door, not at send time: a bad endpoint is refused where it is typed rather than
    # stored, sent to once, and discovered afterwards.
    endpoint = validate_endpoint(endpoint)
    for label, value, limit in (
        ("p256dh", p256dh, MAX_KEY_CHARS),
        ("auth", auth, MAX_KEY_CHARS),
    ):
        if len(value) > limit:
            raise ValueError(f"The {label} is longer than {limit} characters.")
    if not p256dh or not auth:
        raise ValueError("A push subscription needs both its p256dh and auth keys.")
    return endpoint, p256dh, auth


def subscribe(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Record (or refresh) one browser's subscription. Returns True when it was new.

    Keyed on the endpoint, not on ``(worker, endpoint)``: the same browser re-subscribing -
    which it does whenever the keys rotate - is the same subscription, and a second row would
    mean every notification rang twice on that phone.

    A subscription that was revoked (the push service said it was gone, or the worker turned
    notifications off in the app) is reactivated here rather than duplicated, because
    re-subscribing is exactly how a worker says "yes, this phone again".
    """
    endpoint, p256dh, auth = _clean_keys(endpoint, p256dh, auth)
    stamp = _stamp(now)
    row = conn.execute(
        "SELECT id, worker_id FROM worker_push_subscriptions WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO worker_push_subscriptions
                (worker_id, endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(worker_id), endpoint, p256dh, auth, user_agent, stamp),
        )
        return True

    # A browser handed to a different account (a shared site phone) follows the account that
    # is using it now: the endpoint is the device, and the device just told us who it is.
    conn.execute(
        "UPDATE worker_push_subscriptions SET worker_id = ?, p256dh = ?, auth = ?, "
        "user_agent = ?, created_at = ?, revoked_at = NULL, last_error = NULL WHERE id = ?",
        (str(worker_id), p256dh, auth, user_agent, stamp, row["id"]),
    )
    return str(row["worker_id"]) != str(worker_id)


def unsubscribe(
    conn: sqlite3.Connection, *, worker_id: str, endpoint: str, now: datetime | None = None
) -> bool:
    """Retire one subscription. Scoped by worker: nobody turns off somebody else's phone.

    Retired rather than deleted so "why did my alerts stop" has an answer on the row, and so
    a stale device id cannot be handed to another worker by accident.
    """
    cursor = conn.execute(
        "UPDATE worker_push_subscriptions SET revoked_at = ?, last_error = ? "
        "WHERE endpoint = ? AND worker_id = ? AND revoked_at IS NULL",
        (_stamp(now), "unsubscribed in the app", str(endpoint), str(worker_id)),
    )
    return cursor.rowcount > 0


def subscriptions(conn: sqlite3.Connection, worker_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT endpoint, p256dh, auth FROM worker_push_subscriptions "
        "WHERE worker_id = ? AND revoked_at IS NULL ORDER BY id",
        (str(worker_id),),
    ).fetchall()
    return [{"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}} for row in rows]


def _revoke(conn: sqlite3.Connection, endpoint: str, reason: str, now: datetime | None = None) -> None:
    conn.execute(
        "UPDATE worker_push_subscriptions SET revoked_at = ?, last_error = ? "
        "WHERE endpoint = ? AND revoked_at IS NULL",
        (_stamp(now), reason, endpoint),
    )


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------
def send_push(subscription: dict, message: dict) -> None:
    """Hand one message to the push service. Raises on failure; the caller classifies it.

    The single seam in this module. Tests replace it (see ``tests/test_worker_notifications``)
    because the alternative is asserting on a live HTTPS call to a browser vendor's push
    service, which cannot be done from a test suite at all - and the encryption this function
    is a thin wrapper around is pywebpush's, not ours.
    """
    loaded = _load_webpush()
    if loaded is None:  # pragma: no cover - guarded by transport_available at every caller
        raise RuntimeError("pywebpush is not installed")
    webpush, _ = loaded
    webpush(
        subscription_info=subscription,
        data=json.dumps(message),
        vapid_private_key=settings.vapid_private_key,
        vapid_claims={"sub": settings.vapid_subject},
    )


def _status_of(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def message_for(row: sqlite3.Row) -> dict:
    """What the phone is shown. Short, attributed, and never a payload an operator reads.

    The inbox row carries the full body; the notification carries enough to act on. A push
    notification is visible on a lock screen, so it must not be a place administrative detail
    leaks to whoever is holding the phone.
    """
    return {
        "title": row["title"],
        "body": row["body"],
        "kind": row["kind"],
        "id": row["id"],
        "target": DEFAULT_TARGET,
    }


def deliver(
    conn: sqlite3.Connection,
    *,
    notification: sqlite3.Row,
    now: datetime | None = None,
    send=send_push,
) -> dict:
    """Attempt one notification across every live subscription of its worker.

    Returns a summary rather than raising: a push failure is not an event that should take
    down the punch, the clock-out or the watcher pass that produced it.
    """
    result = {"sent": 0, "failed": 0, "revoked": 0, "refused": 0, "subscriptions": 0}
    worker_id = str(notification["worker_id"])
    subs = subscriptions(conn, worker_id)
    result["subscriptions"] = len(subs)
    for subscription in subs:
        try:
            # Re-applied here as well as at subscribe time. A row written before this check
            # existed - by an older build, or by an operator's script - must never be fetched,
            # and the send path is the last place that can say so. It is not revoked: the
            # operator may widen the allowlist, and a subscription is the worker's.
            validate_endpoint(subscription["endpoint"])
        except ValueError as exc:
            result["refused"] += 1
            conn.execute(
                "UPDATE worker_push_subscriptions SET last_error = ? WHERE endpoint = ?",
                (f"endpoint refused: {exc}"[:500], subscription["endpoint"]),
            )
            log.warning("refusing to push to %s: %s", subscription["endpoint"], exc)
            continue
        try:
            send(subscription, message_for(notification))
        except Exception as exc:  # noqa: BLE001 - any failure is a delivery failure
            status = _status_of(exc)
            if status in (404, 410):
                # The push service has forgotten this subscription: the browser was
                # uninstalled, the permission was revoked, or the endpoint expired. Pushing
                # again can only fail again, so it is retired here.
                _revoke(conn, subscription["endpoint"], f"push service answered {status}", now)
                result["revoked"] += 1
                continue
            result["failed"] += 1
            conn.execute(
                "UPDATE worker_push_subscriptions SET last_error = ? WHERE endpoint = ?",
                (f"{type(exc).__name__}: {exc}"[:500], subscription["endpoint"]),
            )
            log.warning("push delivery failed for worker %s: %s", worker_id, exc)
            continue
        result["sent"] += 1
        conn.execute(
            "UPDATE worker_push_subscriptions SET last_ok_at = ?, last_error = NULL "
            "WHERE endpoint = ?",
            (_stamp(now), subscription["endpoint"]),
        )
    return result


def dispatch(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    now: datetime | None = None,
    send=send_push,
) -> dict:
    """Send the undelivered, recent notifications and record what happened to each.

    Three filters, each doing a job:

    * ``delivered_at IS NULL`` - nothing is sent twice across the several worker processes
      that may run a dispatch;
    * ``delivery_attempts < push_attempt_limit`` - a service that keeps failing stops being
      asked. Without this a phone that will never accept another message is retried on every
      event for ever;
    * **age** - a notification older than ``push_max_age_minutes`` is not worth waking a phone
      for. It is not an error and it is not deleted: it is an inbox item, which is where a
      worker looks when they open the app. A phone that was off overnight must not be told it
      crossed the overtime line yesterday *as though it were now*.
    """
    summary = {
        "considered": 0,
        "delivered": 0,
        "failed": 0,
        "no_subscription": 0,
        "revoked": 0,
        # Counted separately from ``failed``: "the push service answered with an error" and
        # "this server declined to fetch that URL" are different events, and an operator
        # reading a dispatch log needs to be able to tell which one they are looking at.
        "refused": 0,
        "skipped": "",
    }
    usable, reason = transport_available()
    if not usable:
        summary["skipped"] = reason
        return summary

    moment = now or _now()
    oldest = _stamp(moment - timedelta(minutes=max(0, int(settings.push_max_age_minutes))))
    rows = conn.execute(
        "SELECT * FROM worker_notifications WHERE delivered_at IS NULL "
        "AND delivery_attempts < ? AND created_at >= ? ORDER BY created_at ASC, id ASC LIMIT ?",
        (int(settings.push_attempt_limit), oldest, max(1, int(limit))),
    ).fetchall()
    summary["considered"] = len(rows)
    for row in rows:
        outcome = deliver(conn, notification=row, now=moment, send=send)
        summary["revoked"] += outcome["revoked"]
        summary["refused"] += outcome["refused"]
        if outcome["subscriptions"] == 0:
            # Nobody to tell. Left undelivered on purpose: the worker may allow notifications
            # on this phone a minute from now, and while the notification is still inside the
            # age window there is still something worth delivering. Past it, nothing is.
            summary["no_subscription"] += 1
            continue
        if outcome["sent"]:
            conn.execute(
                "UPDATE worker_notifications SET delivered_at = ?, delivery_attempts = delivery_attempts + 1, "
                "last_error = NULL WHERE id = ?",
                (_stamp(moment), row["id"]),
            )
            summary["delivered"] += 1
            continue
        # When every subscription was refused, nothing was attempted: calling that a failure
        # would send an operator to the push service's logs for a request that never left this
        # server, so it is named for what it is.
        all_refused = bool(outcome["refused"]) and outcome["refused"] == outcome["subscriptions"]
        failure = (
            "endpoint refused: not a push service this server sends to"
            if all_refused
            else f"all {outcome['subscriptions']} subscription(s) failed"
        )
        conn.execute(
            "UPDATE worker_notifications SET delivery_attempts = delivery_attempts + 1, last_error = ? "
            "WHERE id = ?",
            (failure, row["id"]),
        )
        summary["failed"] += 1
    # Read on the connection this dispatch already holds, after the sends above: whatever was
    # left behind is now exactly what an operator needs to hear about, and it costs no second
    # connection. Dispatch is one of the two clocks this runs on (the watcher is the other),
    # so a channel that goes quiet between watcher passes is reported by the next punch.
    _record_stranded(conn, stranded_notices(conn, now=moment), now=moment)
    return summary


#: One dispatch at a time per process. Two watcher passes and a punch can land together, and
#: they would all select the same undelivered rows - the ``delivered_at`` stamp is the real
#: guard, but there is no reason to spend the network calls twice.
_dispatch_lock = threading.Lock()


def dispatch_async(*, limit: int = 20) -> bool:
    """Dispatch on a daemon thread. Returns whether one was started.

    Called *after* the transaction that wrote the notification has committed, and the reason
    is in this module's docstring: a push is a third-party network call, and the caller is
    usually holding a worker at a gate or a SQLite write lock.
    """
    usable, _ = transport_available()
    if not usable:
        return False
    if not _dispatch_lock.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            with db(write=True) as conn:
                summary = dispatch(conn, limit=limit)
            if summary["delivered"] or summary["failed"]:
                log.info(
                    "worker push: %s delivered, %s failed, %s revoked",
                    summary["delivered"], summary["failed"], summary["revoked"],
                )
        except Exception as exc:  # pragma: no cover - a background thread must not die loudly
            log.warning("worker push dispatch failed: %s", exc)
        finally:
            _dispatch_lock.release()

    threading.Thread(target=_run, name="worker-push", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# the quiet channel, said out loud
# ---------------------------------------------------------------------------
#: Operator alerts for the same backlog are not repeated more than once per hour. Both
#: extremes are wrong in a different way: a per-pass key would write a row on every watcher
#: tick ("the channel is dead" said sixty times an hour is an alarm nobody reads), while a
#: per-day key would say nothing for the first day of a dead channel - which is exactly the
#: day an operator needed to hear about it.
ALERT_BUCKET = "%Y-%m-%d %H"


def _window_minutes() -> int:
    return max(0, int(settings.push_max_age_minutes))


def _human_age(seconds: float) -> str:
    """The age of the oldest stranded notice as ``3h 20m``, for a sentence a human reads."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def stranded_notices(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict:
    """Read the backlog: notices that are undelivered and past the age window.

    A *reading*, not an event. Every other thing this application tells an operator is the
    consequence of something that happened - a punch, a scan, a refused login - and a push
    channel that stops working produces no such row. What it produces is the **absence** of
    ``delivered_at`` stamps, so the fact has to be measured rather than caught.

    The window is the same ``PUSH_MAX_AGE_MINUTES`` ``dispatch`` refuses to wake a phone for,
    and that is the definition of stranded rather than an arbitrary alarm threshold: past it
    the notice is no longer worth a push, so a row that is still undelivered will *never* be
    delivered. Counting anything newer would report a backlog that is simply the next send
    being a moment away.

    Two halves matter separately, because they need different people to fix them:
    ``no_device`` notices belong to workers with no live subscription - nothing was sent and
    nothing can be until they allow notifications in the app - while ``with_device`` notices
    went to a push service and did not arrive, which is the service's answer to give.

    **``channel_failures`` is the half that is a finding; ``waiting_for_device`` is context.**
    The two are complementary (they sum to ``notices``) and cut at a different line than
    ``with_device`` does on purpose. A notice is a *channel* failure when the channel was
    actually asked to deliver it - it was attempted (``delivery_attempts > 0``), or a live
    subscription exists right now - and it still did not arrive. A notice nobody was ever asked
    to deliver is not evidence about the channel at all: it is evidence about adoption, and
    reporting it as a broken channel is a finding an operator can neither reproduce nor fix.

    The distinction is not cosmetic on this deployment. With notifications enabled for the
    first time, every notice written before that moment is undelivered forever - there was no
    channel to deliver them - so a reading that counts them as failures reports a silent
    channel where none exists, and keeps reporting it for ``NOTIFICATION_RETENTION_DAYS``
    (180). ``attempted`` is what keeps the *revoked* case honest: a device that failed and was
    retired stops appearing in ``with_device``, but its attempts are on the row, so the notice
    stays a channel failure rather than sliding quietly into the adoption bucket.

    **A notice the worker has read is no longer stranded.** ``read_at`` is the worker's own
    answer to the notice, and an answered question is not an undelivered one: whatever the
    phone did, the person knows. Counting read rows kept this check red for exactly as long as
    the deployment keeps history - every notice written before push existed, and every one a
    worker dismissed without the phone ever ringing, counted for ``NOTIFICATION_RETENTION_DAYS``
    (180) - and no operator action could ever clear it. The inbox is the record either way;
    what this reading is *for* is a channel that is trying and cannot deliver, and a worker who
    has read the notice is not that.

    Strictly read-only, and only what an operator needs: counts, the oldest stamp, a kind
    breakdown. No endpoint, no ``p256dh``, no ``auth`` - an endpoint is a capability to send
    to somebody's phone, and an alert is a row that gets logged, mailed and pasted into
    issues.
    """
    moment = now or _now()
    window = _window_minutes()
    cutoff = _stamp(moment - timedelta(minutes=window))
    row = conn.execute(
        """
        SELECT COUNT(*) AS notices,
               COUNT(DISTINCT worker_id) AS workers,
               MIN(created_at) AS oldest,
               SUM(CASE WHEN delivery_attempts > 0 THEN 1 ELSE 0 END) AS attempted,
               SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM worker_push_subscriptions s
                        WHERE s.worker_id = worker_notifications.worker_id
                          AND s.revoked_at IS NULL
                    ) THEN 1 ELSE 0 END) AS with_device,
               SUM(CASE WHEN delivery_attempts > 0 OR EXISTS (
                        SELECT 1 FROM worker_push_subscriptions s
                        WHERE s.worker_id = worker_notifications.worker_id
                          AND s.revoked_at IS NULL
                    ) THEN 1 ELSE 0 END) AS channel_failures
          FROM worker_notifications
         WHERE delivered_at IS NULL AND read_at IS NULL AND created_at < ?
        """,
        (cutoff,),
    ).fetchone()
    kinds = conn.execute(
        "SELECT kind, COUNT(*) AS notices FROM worker_notifications "
        "WHERE delivered_at IS NULL AND read_at IS NULL AND created_at < ? "
        "GROUP BY kind ORDER BY notices DESC, kind ASC",
        (cutoff,),
    ).fetchall()
    notices = int(row["notices"] or 0)
    oldest = row["oldest"] or ""
    age = 0.0
    if oldest:
        try:
            age = max(0.0, (moment - datetime.strptime(oldest, TS)).total_seconds())
        except ValueError:  # pragma: no cover - a stamp this module did not write
            age = 0.0
    with_device = int(row["with_device"] or 0)
    channel_failures = int(row["channel_failures"] or 0)
    return {
        "window_minutes": window,
        "notices": notices,
        #: The finding: the channel was asked (attempted, or a live device) and did not deliver.
        "channel_failures": channel_failures,
        #: The context: nothing was ever asked to deliver these, so nothing failed.
        "waiting_for_device": notices - channel_failures,
        "workers": int(row["workers"] or 0),
        "oldest": oldest,
        "age_seconds": int(age),
        "age": _human_age(age) if notices else "",
        "attempted": int(row["attempted"] or 0),
        "with_device": with_device,
        "no_device": notices - with_device,
        "kinds": {str(entry["kind"]): int(entry["notices"]) for entry in kinds},
        "checked_at": _stamp(moment),
    }


def _stranded_report(reading: dict) -> str:
    """The whole reading in one sentence an operator can act on, not a count on its own."""
    notices = reading["notices"]
    sentence = (
        f"{notices} worker notification(s) for {reading['workers']} worker(s) passed the "
        f"{reading['window_minutes']}-minute push window without being delivered "
        f"(oldest {reading['age']} old)."
    )
    if reading["no_device"]:
        sentence += (
            f" {reading['no_device']} have no live device at all: nothing was sent, and nothing"
            " will be until that worker allows notifications in the app."
        )
    if reading["with_device"]:
        sentence += (
            f" {reading['with_device']} belong to a worker whose device is registered, so the"
            " push service refused or failed the send - the subscription's last_error says which."
        )
    if reading["attempted"]:
        sentence += (
            f" {reading['attempted']} were attempted; after {int(settings.push_attempt_limit)}"
            " attempts a notice is not retried."
        )
    by_kind = ", ".join(f"{kind} x{count}" for kind, count in reading["kinds"].items())
    if by_kind:
        sentence += f" By kind: {by_kind}."
    return sentence + (
        " Nothing is lost: every notice is still in the worker's own inbox, which is what"
        " their app shows. Readiness reports this as worker_notice_backlog."
    )


def _record_stranded(conn: sqlite3.Connection, reading: dict, *, now: datetime | None = None) -> bool:
    """Write the operator alert for a *failing channel*. Returns whether a new row was created.

    The trigger is ``channel_failures``, not ``notices``: an alert is a claim that something is
    broken, and a notice no channel was ever asked to deliver is not evidence of that. Firing on
    every undelivered notice also produces a row an hour, forever, for a state no alert can fix -
    notices written before notifications were enabled belong to workers with no device, and they
    stay undelivered no matter what the operator does. The adoption gap is reported where it can
    act on it instead: the readiness detail names it in full, the workers' own inboxes hold every
    notice, and each worker's app says whether their device is registered.

    The revocation case is why ``channel_failures`` is a union rather than "has a device now": a
    device that failed and was retired drops out of ``with_device``, but the attempts on the row
    keep the notice a failure - so a channel that went quiet does not slide into the adoption
    bucket and fall silent.
    """
    if not reading["channel_failures"]:
        return False
    moment = now or _now()
    return notifications.notify(
        conn,
        kind=notifications.KIND_WORKER_PUSH_UNDELIVERED,
        # Warning, not critical: attendance still works, every notice is still in the worker's
        # inbox, and what has been lost is the phone ringing. Critical stays reserved for the
        # states where the application cannot do its job at all.
        severity=notifications.SEVERITY_WARNING,
        title="Worker notifications went undelivered",
        body=_stranded_report(reading),
        payload={
            "window_minutes": reading["window_minutes"],
            "notices": reading["notices"],
            "workers": reading["workers"],
            "oldest": reading["oldest"],
            "age_seconds": reading["age_seconds"],
            "no_device": reading["no_device"],
            "with_device": reading["with_device"],
            "attempted": reading["attempted"],
            "kinds": reading["kinds"],
            "checked_at": reading["checked_at"],
        },
        dedupe_key=f"{notifications.KIND_WORKER_PUSH_UNDELIVERED}:{moment.strftime(ALERT_BUCKET)}",
    )


def alert_stranded_notices(*, now: datetime | None = None) -> dict:
    """Measure the backlog and, if there is one, tell the operator. Returns the reading.

    **Silent when this deployment does not intend to push.** With no key pair, or with
    ``PUSH_ENABLED=0``, an undelivered backlog is the expected state and not news - the inbox
    is the record, and the deployment has said out loud that no phone will ring. Alerting then
    would produce a permanent alarm about a decision somebody made deliberately, and would
    drown the reading that matters: a deployment which *is* trying to deliver and cannot.

    **And silent for notices nothing was asked to deliver.** A worker with no registered device
    leaves notices undelivered without any channel failure, and an alert about that is an hourly
    claim nobody can act on; the reading names it instead (``waiting_for_device``), and so does
    readiness. The alert fires on ``channel_failures`` - a notice that was attempted or belongs
    to a worker with a live device, and still did not arrive - which is the state this whole
    module exists to surface.

    Two connections, in that order, and never the read inside the write: the reading is
    strictly read-only, and most passes find nothing to say and must not take SQLite's write
    lock from a watcher thread to say it.
    """
    usable, reason = transport_available()
    if not usable:
        return {"checked": False, "reason": reason, "alerted": False, "notices": 0}
    moment = now or _now()
    with db() as conn:
        reading = stranded_notices(conn, now=moment)
    result = {"checked": True, "alerted": False, "reason": "nothing was left behind"}
    if not reading["notices"]:
        return {**reading, **result}
    if not reading["channel_failures"]:
        # Undelivered, but nothing was ever asked to deliver them: an adoption gap, which is
        # named in the reading and in readiness rather than alerted (see ``_record_stranded``).
        # Said out loud here so a caller reading ``alerted: False`` knows which of the two
        # silences it got - "nothing to report" and "nothing to *alert*" are different states.
        return {
            **reading,
            **result,
            "reason": "no channel failure: the backlog is waiting for a device",
        }
    with db(write=True) as conn:
        alerted = _record_stranded(conn, reading, now=moment)
    return {
        **reading,
        **result,
        "alerted": alerted,
        "reason": "the operator was told" if alerted else "already told this hour",
    }


# ---------------------------------------------------------------------------
# key generation (an operator's one-off)
# ---------------------------------------------------------------------------
def _generate_keys() -> str:
    """Print a VAPID key pair, using the same optional dependency the sender needs.

    Not implemented here by hand on purpose: a VAPID key pair is EC P-256, and hand-rolling
    the base64url encoding of the raw point is exactly the kind of "almost right" crypto that
    works against one push service and fails against the next. ``py-vapid`` ships with
    ``pywebpush`` and is the reference implementation, so the honest answer when it is absent
    is to say which command installs it.

    TWO THINGS THIS FUNCTION LEARNED THE HARD WAY, both worth keeping:

    * **the encoding is assembled from the libraries' own primitives, not from a helper that
      may not exist.** py-vapid 1.9 (what ``pip install pywebpush`` brings) dropped
      ``public_key_urlsafe_base64`` / ``private_key_urlsafe_base64``, which is what this
      function used to call - so the documented command printed a message about a missing
      attribute instead of a key pair, on a deployment whose whole push advisory then could not
      be satisfied. The two forms it needs are the ones the sender and the browser read:
      ``py_vapid.b64urlencode`` of the raw 32-byte scalar for the private half (which is what
      ``Vapid.from_string`` - the call ``pywebpush`` makes when handed this string - decodes),
      and the same encoding of ``encode_point()`` for the public half: the SEC1 uncompressed
      point a browser wants as ``applicationServerKey``.
    * **the pair is reloaded from the exact strings it prints, and refused if they do not
      round-trip.** That check is the whole safeguard the paragraph above is about: an encoding
      that is "almost right" loads back as a *different* key, and the only moment the
      difference is cheap to notice is here rather than at the first subscribe on a phone.
    """
    try:
        from py_vapid import Vapid, b64urlencode  # noqa: PLC0415 - optional dependency
    except Exception:
        return (
            "Generating a VAPID key pair needs the optional dependency:\n"
            "    pip install pywebpush\n"
            "then re-run: python -m push --generate-keys\n"
            "Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY from its output, and keep the private\n"
            "half out of the repository, the frontend and any log."
        )
    try:
        from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
            Encoding,
            PublicFormat,
        )

        key = Vapid()
        key.generate_keys()
        private = b64urlencode(key.private_key.private_numbers().private_value.to_bytes(32, "big"))
        # ``Encoding.X962`` + ``UncompressedPoint`` is cryptography's own name for the SEC1
        # uncompressed point (65 bytes, leading 0x04): the exact bytes a browser takes as
        # ``applicationServerKey``. Serialised by the library, never assembled by hand.
        point = key.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        public = b64urlencode(point)
    except Exception as exc:  # pragma: no cover - depends on the optional package's API
        return (
            f"Could not generate a key pair with this py-vapid version: {type(exc).__name__}: {exc}\n"
            "Any EC P-256 pair works if it is encoded the way pywebpush expects; py-vapid's\n"
            "README is the reference. What must not happen is a hand-rolled encoding that works\n"
            "against one push service and fails against the next."
        )

    problem = _pair_is_usable(public, private)
    if problem:
        return (
            f"Refusing to print a key pair that does not load back: {problem}\n"
            "The sender reads the private half through pywebpush's own ``Vapid.from_string`` and\n"
            "the browser reads the public half as ``applicationServerKey``; a pair that fails\n"
            "either of those subscribes nowhere, so it is not a deployment secret worth keeping."
        )
    return (
        f"VAPID_PUBLIC_KEY={public}\n"
        f"VAPID_PRIVATE_KEY={private}\n"
        "Keep the private half secret; the public half is served to the browser."
    )


def _pair_is_usable(public: str, private: str) -> str | None:
    """``None`` when these two strings are a working VAPID pair, otherwise why they are not.

    Deliberately checked through the same entry points the running system uses: ``from_string``
    for the private half (pywebpush's call) and the uncompressed-point form for the public half
    (the browser's format, 65 bytes beginning with the ``0x04`` that marks a point as
    uncompressed - the length the runbook tells an operator to eyeball before deploying).
    """
    from py_vapid import Vapid, b64urldecode, b64urlencode  # noqa: PLC0415 - optional dependency

    try:
        loaded = Vapid.from_string(private_key=private)
    except Exception as exc:  # pragma: no cover - only reachable with a broken library
        return f"pywebpush could not load the private half ({type(exc).__name__}: {exc})"
    from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
        Encoding,
        PublicFormat,
    )

    derived = b64urlencode(loaded.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
    if derived != public:
        return "the public half is not the pair of the private half"
    # py-vapid decodes *bytes* (its own ``from_string`` encodes first), so these go in as bytes.
    raw = b64urldecode(public.encode())
    if len(raw) != 65 or raw[0] != 0x04:
        return f"the public half is not an uncompressed P-256 point ({len(raw)} bytes)"
    if len(b64urldecode(private.encode())) != 32:
        return "the private half is not a 32-byte scalar"
    return None


if __name__ == "__main__":  # pragma: no cover - an operator's one-off
    import sys

    if "--generate-keys" in sys.argv:
        print(_generate_keys())
    else:
        print("usage: python -m push --generate-keys")
