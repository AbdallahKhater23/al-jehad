"""Where a push notification is allowed to be sent.

A push subscription endpoint is a **URL a worker supplies and this server later POSTs to**.
That is a server-side request forgery primitive with a retry: the request leaves from inside
the network, carries no credential of the worker's, and is repeated on every notification
that follows. So the property that matters is not "the endpoint looks reasonable" but "the
endpoint names a push service", and the difference between those two is whether
``https://169.254.169.254/latest/meta-data/`` (the cloud metadata service, which hands out
instance credentials to anything that asks) is refused.

This file holds three layers, because a rule enforced in only one of them is not enforced:

* **the policy** (``push.validate_endpoint``) - what is refused, and the two ways an
  allowlist is normally defeated: a longer name that merely *ends with* an allowed one, and
  a URL whose userinfo hides the real host;
* **the door** (``POST /worker/me/push/subscribe``) - refused where it is typed, and
  provably not stored;
* **the send path** (``push.deliver`` / ``dispatch``) - a row that predates the check, or
  that an operator wrote by hand, must not be fetched either.

The last layer is asserted against ``harness.OUTBOUND``, which records every outbound HTTP
attempt the application makes and performs none of them. "The server did not send" is a
claim about the network, so it is checked at the network seam rather than inferred from a
return value.

    pytest backend/tests/test_push_endpoint_authorisation.py -q
"""

from __future__ import annotations

from datetime import datetime

import pytest
from config import PUSH_ENDPOINT_HOSTS, settings
from database import db
from harness import WORKER, bearer, db_scalar

import notifications
import push

#: A shape the browser really does produce, so the accepted cases are not hypotheticals.
FCM = "https://fcm.googleapis.com/fcm/send/abc123:APA91b"


def _subscribe_body(endpoint: str, *, p256dh: str = "BAbcdefghijklmnop", auth: str = "auth-key-value") -> dict:
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}


@pytest.fixture
def push_available(monkeypatch):
    """Make the transport look configured, so the subscribe endpoint reaches validation.

    Without this the endpoint answers 409 ("push is unavailable") *before* it looks at the
    payload - which is correct behaviour, but it would mean these tests never touched the
    check they exist for. ``pywebpush`` is the optional dependency and is not installed in
    this environment, so the late import is stubbed rather than installed.
    """

    def _fake_webpush(subscription_info, data, vapid_private_key, vapid_claims):  # pragma: no cover
        raise AssertionError("no test in this file may actually push")

    monkeypatch.setattr(settings, "vapid_public_key", "BPublicKeyForTests", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "private-key-for-tests", raising=False)
    monkeypatch.setattr(push, "_load_webpush", lambda: (_fake_webpush, RuntimeError), raising=True)
    usable, reason = push.transport_available()
    assert usable, f"the transport fixture did not take effect: {reason}"
    return push


def _plant_subscription(endpoint: str, *, worker_id: str = WORKER) -> None:
    """Write a subscription row **bypassing** ``push.subscribe``.

    This is the case the door cannot cover: a row written before this check existed, by an
    older build, or by an operator's script. It is why the send path validates as well.
    """
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO worker_push_subscriptions (worker_id, endpoint, p256dh, auth, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (worker_id, endpoint, "BAbcdefghijklmnop", "auth-key-value", "2026-01-01 00:00:00"),
        )


# ---------------------------------------------------------------------------
# The policy: what is accepted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("service", PUSH_ENDPOINT_HOSTS)
def test_every_shipped_push_service_is_accepted(service):
    """The allowlist must not be so strict that real workers cannot subscribe."""
    endpoint = f"https://{service}/push/abc123"
    assert push.validate_endpoint(endpoint) == endpoint


@pytest.mark.parametrize("service", PUSH_ENDPOINT_HOSTS)
def test_a_subdomain_of_a_push_service_is_accepted(service):
    """WNS hands out ``<region>.notify.windows.com``, so subdomains must match."""
    endpoint = f"https://eu-west.{service}/push/abc123"
    assert push.validate_endpoint(endpoint) == endpoint


def test_the_allowlist_is_configurable_for_a_self_hosted_service(monkeypatch):
    """A deployment running its own push service must be able to say so."""
    monkeypatch.setattr(settings, "push_endpoint_hosts", ("push.mine.test",), raising=False)
    endpoint = "https://push.mine.test/send/1"
    assert push.validate_endpoint(endpoint) == endpoint
    # And the override *replaces* the defaults, which is what makes it useful as a lock-down
    # as well as an extension.
    with pytest.raises(ValueError):
        push.validate_endpoint(FCM)


# ---------------------------------------------------------------------------
# The policy: private and internal addresses
# ---------------------------------------------------------------------------
PRIVATE_ENDPOINTS: list[tuple[str, str]] = [
    ("https://127.0.0.1/hook", "loopback"),
    ("https://localhost/hook", "the loopback name, no dot at all"),
    ("https://[::1]/hook", "loopback, IPv6"),
    ("https://169.254.169.254/latest/meta-data/iam/security-credentials/", "cloud metadata (link-local)"),
    ("https://10.0.0.5:8443/hook", "RFC 1918 private range"),
    ("https://192.168.1.10/hook", "the office LAN"),
    ("https://172.16.4.4/hook", "RFC 1918 private range"),
    ("https://0.0.0.0/hook", "the unspecified address"),
    ("https://[fe80::1]/hook", "link-local, IPv6"),
    ("https://metadata.google.internal/computeMetadata/v1/", "a cloud metadata hostname"),
    ("https://push.local/hook", "a mDNS name on the network"),
    ("https://nas.lan/hook", "a bare local name"),
    ("https://vault.corp/hook", "an internal suffix"),
    ("https://intranet/hook", "a single-label hostname, which is always internal"),
    ("https://fcm.googleapis.com.internal/hook", "an allowed service name under a private suffix"),
    ("https://2130706433/hook", "127.0.0.1 as a decimal integer"),
    ("https://0x7f000001/hook", "127.0.0.1 in hex"),
]


@pytest.mark.parametrize("endpoint,label", PRIVATE_ENDPOINTS, ids=[c[1] for c in PRIVATE_ENDPOINTS])
def test_private_and_internal_addresses_are_refused(endpoint, label):
    """The SSRF surface, refused. Each of these is something a worker can simply type."""
    with pytest.raises(ValueError):
        push.validate_endpoint(endpoint)


def test_a_public_ip_literal_is_still_refused():
    """Not "is it internal" but "is it a push service": an address is never one.

    This is the case a private-range denylist would let through, and it is the reason the
    control is an allowlist. A public address is reachable, so a denylist-based check would
    fetch it - which is the whole attack, minus the credential theft.
    """
    with pytest.raises(ValueError):
        push.validate_endpoint("https://93.184.216.34/hook")


# ---------------------------------------------------------------------------
# The policy: unknown services, and the two ways an allowlist is defeated
# ---------------------------------------------------------------------------
UNKNOWN_SERVICES: list[tuple[str, str]] = [
    ("https://collector.attacker.test/hook", "somebody's own public server"),
    ("https://evil.test/hook", "any domain at all"),
    ("https://fcm.googleapis.com.evil.test/hook", "a longer name that ENDS WITH an allowed one"),
    ("https://notify.windows.com.attacker.test/hook", "same trick, on the Windows service"),
    ("https://fcm.googleapis.com@evil.test/hook", "userinfo, so the real host is evil.test"),
    ("https://fcm.googleapis.com\\@evil.test/hook", "userinfo behind a backslash, which urlsplit also reads"),
    ("https://evil.test/?next=https://fcm.googleapis.com/", "the service name only in the query"),
    ("https://evil.test/#fcm.googleapis.com", "the service name only in the fragment"),
]


@pytest.mark.parametrize("endpoint,label", UNKNOWN_SERVICES, ids=[c[1] for c in UNKNOWN_SERVICES])
def test_unknown_push_services_are_refused(endpoint, label):
    with pytest.raises(ValueError):
        push.validate_endpoint(endpoint)


def test_a_longer_name_does_not_match_the_host_it_contains():
    """The label-boundary rule, asserted directly.

    A suffix allowlist implemented with ``endswith("notify.windows.com")`` accepts
    ``notify.windows.com.evil.test`` - an attacker registers that name for the price of a
    domain and the server POSTs to it. Matching on the dot boundary is the fix.
    """
    allowed = ("notify.windows.com",)
    assert push._host_allowed("notify.windows.com", allowed) is True
    assert push._host_allowed("eu-west.notify.windows.com", allowed) is True
    assert push._host_allowed("notify.windows.com.evil.test", allowed) is False
    # And the same trap in the trailing-dot form, which is what a fuzzer sends.
    assert push.validate_endpoint("https://eu-west.notify.windows.com./push/1") == (
        "https://eu-west.notify.windows.com./push/1"
    )


# ---------------------------------------------------------------------------
# The policy: shape
# ---------------------------------------------------------------------------
BAD_SHAPES: list[tuple[str, str]] = [
    ("", "empty"),
    ("http://fcm.googleapis.com/hook", "plain http, so the endpoint is readable in transit"),
    ("fcm.googleapis.com/hook", "no scheme"),
    ("https://fcm.googleapis.com:8443/hook", "a non-standard port, which is not a push service"),
    ("https://user:secret@fcm.googleapis.com/hook", "credentials embedded in the URL"),
    ("https://fcm.googleapis.com/\r\nX-Injected: 1", "a CRLF, which smuggles a second header"),
    ("https://fcm.googleapis.com/ hook", "a space"),
    ("https://fcm.googleapis.com/\x00", "a NUL"),
]


@pytest.mark.parametrize("endpoint,label", BAD_SHAPES, ids=[c[1] for c in BAD_SHAPES])
def test_malformed_endpoints_are_refused(endpoint, label):
    with pytest.raises(ValueError):
        push.validate_endpoint(endpoint)


def test_an_overlong_endpoint_is_refused():
    with pytest.raises(ValueError):
        push.validate_endpoint("https://fcm.googleapis.com/" + "a" * push.MAX_ENDPOINT_CHARS)


def test_a_widened_allowlist_still_cannot_name_an_address_or_a_private_suffix():
    """The allowlist is the control; this is the second lock behind it.

    Somebody will eventually add an entry without reading the comment beside it - or an
    ``.env`` will set ``PUSH_ENDPOINT_HOSTS`` to an internal host by mistake. An address and a
    private suffix are refused whatever the list says, so that mistake is not a hole.
    """
    with pytest.raises(ValueError):
        push.validate_endpoint("https://10.0.0.9/hook", allowed=("10.0.0.9",))
    with pytest.raises(ValueError):
        push.validate_endpoint("https://registry.internal/hook", allowed=("internal",))
    with pytest.raises(ValueError):
        push.validate_endpoint("https://127.0.0.1/hook", allowed=("127.0.0.1",))


# ---------------------------------------------------------------------------
# The door: the subscribe endpoint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint", [c[0] for c in PRIVATE_ENDPOINTS] + [c[0] for c in UNKNOWN_SERVICES])
def test_subscribe_refuses_and_stores_nothing(client, push_available, endpoint):
    """Refused where it is typed, and provably not written.

    The row matters as much as the status code: a 422 that still stored the endpoint would
    leave the send path holding a URL the server will later fetch.
    """
    response = client.post(
        "/api/v1/worker/me/push/subscribe",
        json=_subscribe_body(endpoint),
        headers=bearer(WORKER),
    )
    assert response.status_code == 422, f"{endpoint} was accepted: {response.status_code} {response.text[:200]}"
    assert response.json()["detail"], "the refusal carries no reason a worker could act on"
    assert db_scalar("SELECT COUNT(*) FROM worker_push_subscriptions") == 0, (
        "the refused endpoint was stored anyway, so the refusal protected nothing"
    )


def test_the_refusal_says_what_the_policy_is(client, push_available):
    """A refusal a worker cannot act on is a bug report waiting to happen.

    The message names the rule - notifications go through a known push service - rather than
    echoing the URL back, which would only tell the sender what they already knew.
    """
    response = client.post(
        "/api/v1/worker/me/push/subscribe",
        json=_subscribe_body("https://collector.attacker.test/hook"),
        headers=bearer(WORKER),
    )
    assert response.status_code == 422
    assert "push service" in response.json()["detail"]


def test_subscribe_accepts_a_real_push_service(client, push_available):
    """The control: a real endpoint still works, so the check is not a blanket deny."""
    response = client.post(
        "/api/v1/worker/me/push/subscribe",
        json=_subscribe_body(FCM),
        headers=bearer(WORKER),
    )
    assert response.status_code == 200, f"{response.status_code} {response.text[:200]}"
    assert response.json()["created"] is True
    assert db_scalar("SELECT COUNT(*) FROM worker_push_subscriptions WHERE worker_id = ?", (WORKER,)) == 1


def test_an_anonymous_caller_cannot_register_a_subscription(client, push_available):
    """The door is behind a session, so a subscription belongs to somebody."""
    response = client.post("/api/v1/worker/me/push/subscribe", json=_subscribe_body(FCM))
    assert response.status_code in (401, 403), response.status_code
    assert db_scalar("SELECT COUNT(*) FROM worker_push_subscriptions") == 0


# ---------------------------------------------------------------------------
# The send path: a row that predates the check
# ---------------------------------------------------------------------------
def test_delivery_refuses_a_planted_endpoint_and_never_fetches_it(client, push_available, outbound):
    """The layer that makes this a control rather than a validation.

    A row written before the check existed - or by an operator, or by a restored backup - is
    the realistic way a private endpoint ends up stored. ``deliver`` re-applies the rule at
    send time, and ``OUTBOUND`` proves the request was never made rather than trusting the
    return value to say so.
    """
    _plant_subscription("https://169.254.169.254/latest/meta-data/")
    sent: list[dict] = []
    outbound.calls.clear()

    # ``db`` rather than a raw connection: a bare ``with sqlite3.connect(...)`` commits but
    # never closes, which leaves the file locked for the next test's reset.
    with db(write=True) as conn:
        notifications.notify_worker(
            conn,
            worker_id=WORKER,
            kind="overtime_threshold",
            title="Overtime",
            body="You crossed the threshold.",
            dedupe_key="push-endpoint-refusal-test",
        )
        row = conn.execute(
            "SELECT * FROM worker_notifications WHERE worker_id = ? ORDER BY id DESC LIMIT 1", (WORKER,)
        ).fetchone()
        assert row is not None

        result = push.deliver(
            conn,
            notification=row,
            now=datetime.now(),
            send=lambda subscription, message: sent.append(subscription),
        )
        stored_error = conn.execute(
            "SELECT last_error FROM worker_push_subscriptions WHERE worker_id = ?", (WORKER,)
        ).fetchone()["last_error"]

    assert result["refused"] == 1, f"a planted private endpoint was not refused: {result}"
    assert result["sent"] == 0
    assert sent == [], "the planted endpoint was handed to the sender"
    assert outbound.urls() == [], f"the server tried to fetch a private address: {outbound.urls()}"
    assert "refused" in (stored_error or ""), "the row does not record why it is being skipped"


def test_dispatch_counts_a_refusal_apart_from_a_failure(client, push_available):
    """An operator reading a dispatch log must be able to tell the two apart.

    A refused endpoint means nothing left this server; a failed one means the push service
    answered with an error. Reporting both as "failed" sends an operator to the wrong logs -
    and, worse, implies the request was made.
    """
    _plant_subscription("https://10.0.0.9/hook")

    with db(write=True) as conn:
        notifications.notify_worker(
            conn,
            worker_id=WORKER,
            kind="overtime_threshold",
            title="Overtime",
            body="You crossed the threshold.",
            dedupe_key="push-dispatch-refusal-test",
        )
        summary = push.dispatch(conn, send=lambda subscription, message: None)

    assert summary["refused"] >= 1, f"the refusal was not counted: {summary}"
    assert summary["delivered"] == 0


# ---------------------------------------------------------------------------
# The allowlist itself
# ---------------------------------------------------------------------------
# A dead entry is the quietest failure in this file. The worker subscribes, the browser says
# it worked, and no notification ever arrives - indistinguishable from the vendor being down.
# So the list is held to the same rules as a submitted endpoint, at startup.
def _allowlist_check():
    import readiness

    return readiness._check_push_endpoint_allowlist({})


def test_the_shipped_allowlist_is_entirely_usable():
    check = _allowlist_check()
    assert check.ok, check.detail


def test_a_dead_allowlist_entry_is_reported_not_ignored(monkeypatch):
    """An entry the policy would refuse must be named, not left to fail silently."""
    monkeypatch.setattr(
        settings,
        "push_endpoint_hosts",
        ("fcm.googleapis.com", "169.254.169.254", "registry.internal"),
        raising=False,
    )
    check = _allowlist_check()
    assert not check.ok, "a metadata address sat in the allowlist and nothing said so"
    assert "169.254.169.254" in check.detail
    assert "registry.internal" in check.detail
    assert check.detail.count("never be used") == 1


def test_an_entry_with_a_scheme_in_it_is_reported(monkeypatch):
    """``PUSH_ENDPOINT_HOSTS=https://fcm.googleapis.com`` is a host list, not a URL list."""
    monkeypatch.setattr(settings, "push_endpoint_hosts", ("https://fcm.googleapis.com",), raising=False)
    check = _allowlist_check()
    assert not check.ok
    assert "scheme" in check.detail


def test_the_allowlist_check_is_registered_and_advisory():
    """A check nobody runs is a comment. Advisory, because push is optional."""
    import readiness

    registered = [getattr(check, "__name__", "") for check in readiness.CHECKS]
    assert "_check_push_endpoint_allowlist" in registered, (
        "the allowlist check is not in the startup registry, so nothing runs it"
    )
    assert _allowlist_check().tier == readiness.TIER_ADVISORY
