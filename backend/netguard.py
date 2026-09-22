"""Network policy: who may call this API from where, and what their browser may do with it.

WHY THIS EXISTS
---------------
Three gaps, and each one is a different kind of mistake:

1. **CORS was a single static list.** ``CORSMiddleware`` with one ``allow_origins`` served
   both the worker app and the administrator's console, so any origin allowed to fetch a
   punch was equally free to call ``/api/v1/admin/users``. The two audiences are not the
   same trust level: a worker origin is a phone that has been handed a session, an admin
   origin is a managed console. So origins carry a *class* (``worker`` or ``admin``) and
   the class decides which paths the browser is allowed to reach.
2. **The admin surface had no network gate at all.** Anyone who could reach the port could
   attempt admin authentication. An allowlist is a control that does not depend on the
   quality of a password: on a payroll system, the admin API is worth hiding from the
   public internet even when every route already demands a token.
3. **No security headers.** The frontend is served by this same application, so the
   browsers running it deserve the same baseline a web server would send: no MIME
   sniffing, no framing, a CSP, and HSTS wherever TLS is actually terminated.

THREE POLICIES, ONE PASS
------------------------
All of it is one ASGI middleware (``NetworkGuardMiddleware``), because the decisions share
the same inputs - the request's method, path, ``Origin`` and the peer address - and
splitting them across three middlewares would mean parsing ``X-Forwarded-For`` three times
and getting the order wrong once.

**CORS is enforced by the browser, not by this middleware.** A request that carries an
``Origin`` the policy does not name is still served - the middleware just withholds the
headers that would let a browser read the answer, which is what the CORS specification says
to do. Refusing it outright would break every non-browser client that happens to send an
``Origin`` (curl scripts, the offline sync tooling, a native app) while adding nothing:
what protects the admin API is that this application authenticates with a **bearer token in
a header, never a cookie**, so a page on another origin has no credential the browser would
attach for it. The one case that *is* refused is a preflight, because a preflight is a
question and a question deserves a readable answer.

**The proxy rule is the part worth reading twice.** ``X-Forwarded-For`` is a request header:
any client can send it. It is therefore honoured *only* when the immediate peer is a
trusted proxy (``TRUSTED_PROXIES``, loopback by default), and then the chain is walked from
the right, skipping trusted proxies, until an address that is not a proxy is found - that
is the client, as far as anyone can honestly say. Two failure modes are treated as
configurations to fix rather than as requests to serve:

* ``X-Forwarded-For`` arriving from a peer that is **not** a trusted proxy. Either a proxy
  was put in front of the app without being declared (so the real client IP is unknown and
  the allowlist would be checking the proxy, not the admin), or somebody is spoofing the
  header on purpose. Both are refused when the admin gate is on, counted as
  ``proxy_not_trusted``, and reported by ``/api/v1/readiness`` - a deployment that is one
  environment variable away from checking the wrong address should not have to guess.
* An allowlist or proxy list that does not parse. The gate then cannot match anything, so
  it refuses instead of waving traffic through, and the startup gate reports the typo
  before the port opens (see ``readiness._check_network_policy``).

WHAT THIS IS NOT
----------------
* **Not an authorization check.** A client inside the allowlist still has to authenticate
  on every admin route. The gate narrows *where* a request may come from; it grants nothing.
* **Not a WAF.** It does not look at bodies, parameters or payloads.
* **Not a substitute for the escaping and the input validation on either side of it.** A
  policy decides what a browser may *load*; it says nothing about a name that is rendered
  as markup by a view that forgot to escape it (``frontend/*.js`` escapes every value it
  interpolates) or about what was allowed into the database in the first place
  (``textguard``). It is the third lock on the same door, and the only one that also stops
  a script that arrived through a route none of them anticipated.
* **Not a fully strict policy yet, and the gap is written down rather than implied.**
  ``CSP_HTML`` no longer allows inline ``<script>`` *elements* - those blocks were moved
  into ``frontend/boot.js``, ``enroll.js`` and ``quick.js`` - so the browser refuses an
  injected script tag, and it names no third-party origin: the utility classes the frontend
  used to have compiled in the browser are components in ``frontend/style.css`` now, so
  ``script-src`` is ``'self'`` alone. One allowance remains, for a reason that is visible in
  the policy string itself: ``script-src-attr 'unsafe-inline'`` (the console builds its
  markup as strings and puts the handler in an ``onclick=`` attribute; making that a
  delegated listener is the refactor that removes it). ``/api/v1/readiness`` reports it, and
  ``tests/test_network_hardening.py`` pins the count of inline handlers so it can only go
  down.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import telemetry

log = logging.getLogger("attendance.netguard")

#: Loopback only. That is what ``serve.py`` already passes to uvicorn as
#: ``forwarded_allow_ips``, so a document served through the local TLS server or a local
#: tunnel sees the client address and nothing else does. A reverse proxy on another host
#: has to be declared here - which is the point: it is a list, not a wildcard.
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")

#: Path prefixes owned by administrators. Kept as prefixes rather than route names because
#: this is a network-level gate: it must work for a path that does not exist yet.
DEFAULT_ADMIN_PATHS = ("/admin", "/api/v1/admin")

DEFAULT_ALLOWED_METHODS = ("GET", "POST", "OPTIONS")
#: ``ngrok-skip-browser-warning`` is not decoration: the frontend sends it on tunnel hosts
#: (``frontendjavascript.js``, ``offline_queue.js``) to keep a tunnel's warning page from
#: answering a fetch, so a cross-origin console that sends it must not be preflight-refused.
DEFAULT_ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    "Accept",
    "Origin",
    "X-Requested-With",
    "Cache-Control",
    "ngrok-skip-browser-warning",
)

#: A JSON body needs no resources, so its document policy denies everything. This one is
#: sent on every ``application/json`` response, including errors.
CSP_API = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; object-src 'none'"

#: The document policy for the pages this application serves (``index.html`` and the two
#: standalone capture pages).
#:
#: ``script-src`` names no ``'unsafe-inline'``: every script these pages run is a file under
#: ``frontend/``, which is what makes an injected ``<script>`` block - the payload a stored
#: XSS actually needs - a refusal rather than an execution.
#:
#: ``script-src`` names one source, ``'self'``: the frontend has no build step and no
#: third-party script any more. It used to name ``cdn.tailwindcss.com``, the Play CDN that
#: compiled the utility classes in the browser on every load; those classes are components
#: in ``style.css`` now, so the origin - and the download - is gone from the page and from
#: this policy.
#:
#: The one allowance that remains is named separately so nobody has to guess which one is
#: in force from the word "inline":
#:
#: * **``script-src-attr 'unsafe-inline'``**. The document contains no inline ``<script>``
#:   *element*, but the console builds its tables and buttons as HTML strings and puts the
#:   handler in an ``onclick=`` attribute, which CSP treats as inline script. The directive
#:   is scoped to attributes on purpose: it cannot enable an inline element, and the path
#:   that would - a delegated listener bound once on the document - is the refactor that
#:   removes the line (see the count pinned in ``tests/test_network_hardening.py``).
CSP_HTML = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "frame-src 'none'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "script-src-attr 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "manifest-src 'self'"
)

CLASS_ADMIN = "admin"
CLASS_WORKER = "worker"

#: Why a request was refused. These strings are also metric label values, so they are
#: fixed vocabulary rather than free text.
REASON_IP = "ip_not_allowed"
REASON_PROXY = "proxy_not_trusted"
REASON_ORIGIN = "origin_not_allowed"

#: Refusals are logged at most once per key per this many seconds, because a public port
#: attracts scanners and a log line per probe is its own denial of service. The counter is
#: the durable signal; this is for the human reading the log.
_LOG_THROTTLE_SECONDS = 60.0
_LOG_THROTTLE_MAX_KEYS = 512


class NetworkPolicyError(ValueError):
    """A network policy that cannot be built. Reported by the startup gate, never ignored."""


# ---------------------------------------------------------------------------
# origins
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OriginRule:
    """One configured origin, compiled once.

    Three forms, and the differences matter when reading an allowlist:

    * ``*`` - any origin. Only ever a worker rule: an admin path is never open to every
      origin, because "which console may drive the payroll API" is not a question whose
      answer is "anybody's page";
    * ``https://app.example.com`` - exactly that origin;
    * ``https://*.example.com`` - the subdomains, and **not** the bare domain. Wildcards
      matching "one label or more" is the CORS-safe reading of a pattern that a
      certificate wildcard treats as one label; an operator who wants both lists both.
    """

    raw: str
    pattern: re.Pattern[str] | None
    allow_any: bool = False

    def matches(self, origin: str) -> bool:
        if self.allow_any:
            return True
        return bool(self.pattern and self.pattern.match(origin))


def _normalise_origin(origin: str) -> str:
    """Lower-case scheme and host, no trailing slash, default ports removed.

    Browsers send origins in this form, but a configuration file is written by a person:
    ``https://Console.Example.com:443/`` and ``https://console.example.com`` are the same
    origin and a policy that only matched one of them would fail the day after a copy-paste.
    """
    text = str(origin or "").strip().strip('"').strip("'").rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        return text.lower()
    scheme, _, rest = text.partition("://")
    scheme = scheme.lower()
    host, _, port = rest.partition(":")
    host = host.lower()
    port = port.split("/")[0]
    if (scheme == "https" and port == "443") or (scheme == "http" and port == "80"):
        port = ""
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def compile_origin_rule(raw: str, *, admin: bool = False) -> OriginRule:
    """Compile one configured origin (or raise ``NetworkPolicyError``)."""
    text = str(raw or "").strip()
    if not text:
        raise NetworkPolicyError("empty origin in the CORS configuration")
    if text in {"*", "*://*"}:
        if admin:
            raise NetworkPolicyError(
                "an admin origin list cannot contain '*' - name the console origin(s), "
                "or leave CORS_ADMIN_ORIGINS empty so no cross-origin console is allowed"
            )
        return OriginRule(raw=text, pattern=None, allow_any=True)

    normalised = _normalise_origin(text)
    if "*" not in normalised:
        return OriginRule(raw=text, pattern=re.compile(rf"^{re.escape(normalised)}$"))

    scheme, sep, rest = normalised.partition("://")
    if not sep:
        raise NetworkPolicyError(f"{text!r} is not an origin (expected e.g. https://app.example.com)")
    # Only the host part may carry a wildcard, and it matches one or more labels.
    if "*" not in rest.replace("*.", "", 1):
        pattern = re.compile(rf"^{re.escape(scheme)}://[a-z0-9.-]*{re.escape(rest.replace('*', ''))}$")
        return OriginRule(raw=text, pattern=pattern)
    raise NetworkPolicyError(
        f"{text!r} has a wildcard outside the host (only 'https://*.example.com' style patterns are supported)"
    )


# ---------------------------------------------------------------------------
# networks
# ---------------------------------------------------------------------------
def parse_networks(entries: Iterable[str], *, setting: str) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse an allowlist of CIDRs or bare addresses. Raises on anything else.

    Raises rather than skipping, for a deliberate reason: silently dropping an entry from a
    security allowlist means the gate checks less than the operator believes it does, and
    ``10.0.0.0/33`` looks enough like a network that nobody would notice.
    """
    parsed: list[ipaddress._BaseNetwork] = []
    for entry in entries or ():
        text = str(entry or "").strip()
        if not text:
            continue
        if text == "*":
            # Spelled out rather than refused: a tunnel or a shared egress really does need
            # it. It is reported by readiness as what it is - every peer may forge
            # X-Forwarded-For, so the allowlist below it is only as strong as the network.
            parsed.append(ipaddress.ip_network(
                "0.0.0.0/0" if ":" not in text else "::/0"
            ))
            continue
        try:
            parsed.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise NetworkPolicyError(f"{setting}: {text!r} is not an address or CIDR ({exc})") from exc
    return tuple(parsed)


def _address(value: str | None) -> ipaddress._BaseAddress | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _in_networks(address: ipaddress._BaseAddress | None, networks: Sequence[ipaddress._BaseNetwork]) -> bool:
    if address is None:
        return False
    for network in networks:
        # An IPv4-mapped IPv6 address (``::ffff:10.0.0.5``, how a dual-stack listener
        # reports a v4 client) must be compared as the v4 address, or an allowlist of v4
        # ranges would never match anything behind such a listener.
        candidate = address
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            candidate = mapped
        if candidate.version == network.version and candidate in network:
            return True
    return False


@dataclass(frozen=True)
class ClientResolution:
    """Who the request is from, and whether that answer can be trusted."""

    ip: str | None
    peer: str | None
    #: ``None`` when the address is usable. Otherwise ``REASON_PROXY``, meaning the header
    #: chain came from somewhere untrusted and the real client cannot be determined.
    problem: str | None = None
    #: The ``X-Forwarded-For`` chain as it was read (for the log line and the tests).
    chain: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------
@dataclass
class NetworkPolicy:
    worker_origins: tuple[OriginRule, ...] = ()
    admin_origins: tuple[OriginRule, ...] = ()
    trusted_proxies: tuple[ipaddress._BaseNetwork, ...] = ()
    admin_networks: tuple[ipaddress._BaseNetwork, ...] = ()
    admin_paths: tuple[str, ...] = DEFAULT_ADMIN_PATHS
    allowed_methods: tuple[str, ...] = DEFAULT_ALLOWED_METHODS
    allowed_headers: tuple[str, ...] = DEFAULT_ALLOWED_HEADERS
    allow_credentials: bool = True
    max_age_seconds: int = 600
    security_headers: bool = True
    hsts_max_age_seconds: int = 15552000
    csp_api: str = CSP_API
    csp_html: str = CSP_HTML
    #: Non-fatal configuration remarks, reported by readiness rather than swallowed.
    remarks: tuple[str, ...] = ()
    #: Fatal configuration problems (unparsable networks, an admin wildcard). Non-empty
    #: means the gate refuses everything and the startup gate fails.
    problems: tuple[str, ...] = ()

    # -- origins ------------------------------------------------------------
    def origin_class(self, origin: str | None) -> str | None:
        """``"admin"``, ``"worker"`` or ``None``. Admin wins when an origin is in both."""
        if not origin:
            return None
        normalised = _normalise_origin(origin)
        for rule in self.admin_origins:
            if rule.matches(normalised):
                return CLASS_ADMIN
        for rule in self.worker_origins:
            if rule.matches(normalised):
                return CLASS_WORKER
        return None

    def is_admin_path(self, path: str) -> bool:
        candidate = str(path or "/")
        return any(
            candidate == prefix or candidate.startswith(prefix.rstrip("/") + "/")
            for prefix in self.admin_paths
        )

    def allows_origin(self, origin: str | None, path: str) -> bool:
        """May this origin read a response from this path?

        A worker origin may not reach an admin path: that is the whole point of having two
        lists. Admin origins reach both, because the console is also a client of the
        worker-facing API (it lists sites, checks readiness, and reads the same dashboard).
        """
        kind = self.origin_class(origin)
        if kind is None:
            return False
        if kind == CLASS_ADMIN:
            return True
        return not self.is_admin_path(path)

    # -- client address -----------------------------------------------------
    def resolve_client(self, peer: str | None, forwarded_for: str | None) -> ClientResolution:
        """The client address, honouring ``X-Forwarded-For`` only from a trusted proxy."""
        chain = tuple(part.strip() for part in str(forwarded_for or "").split(",") if part.strip())
        peer_address = _address(peer)

        if not chain:
            return ClientResolution(ip=str(peer_address) if peer_address else None, peer=peer)

        if not _in_networks(peer_address, self.trusted_proxies):
            # The header is client-controlled here. Never read it, and say so.
            return ClientResolution(ip=str(peer_address) if peer_address else None, peer=peer,
                                    problem=REASON_PROXY, chain=chain)

        parsed: list[ipaddress._BaseAddress] = []
        for hop in chain:
            address = _address(hop)
            if address is None:
                return ClientResolution(ip=None, peer=peer, problem=REASON_PROXY, chain=chain)
            parsed.append(address)

        # Right to left: the last hop was written by the trusted proxy we are talking to, so
        # the first entry that is not itself a trusted proxy is the client. If every hop is a
        # proxy (an internal console behind one), the leftmost is as far back as the chain goes.
        for address in reversed(parsed):
            if not _in_networks(address, self.trusted_proxies):
                return ClientResolution(ip=str(address), peer=peer, chain=chain)
        return ClientResolution(ip=str(parsed[0]), peer=peer, chain=chain)

    def client_allowed(self, resolution: ClientResolution) -> bool:
        """Is this client inside ``ADMIN_IP_ALLOWLIST``? (An empty list means the gate is off.)"""
        if not self.admin_networks:
            return True
        if resolution.problem:
            return False
        return _in_networks(_address(resolution.ip), self.admin_networks)

    @property
    def admin_gate_enabled(self) -> bool:
        """The gate is on when an allowlist is configured.

        Deliberately *not* a separate boolean: two switches that can disagree is how a
        deployment ends up believing it has an allowlist while running without one. An
        empty ``ADMIN_IP_ALLOWLIST`` means "not configured, no gate", and the readiness
        check says so out loud.
        """
        return bool(self.admin_networks)

    # -- descriptions -------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """For ``/api/v1/readiness`` (admin-only) and the startup log."""
        return {
            "worker_origins": [rule.raw for rule in self.worker_origins],
            "admin_origins": [rule.raw for rule in self.admin_origins],
            "cors_allow_credentials": self.allow_credentials,
            "admin_ip_allowlist": [str(network) for network in self.admin_networks],
            "admin_paths": list(self.admin_paths),
            "admin_gate_enabled": self.admin_gate_enabled,
            "trusted_proxies": [str(network) for network in self.trusted_proxies],
            "security_headers": self.security_headers,
            "hsts_max_age_seconds": self.hsts_max_age_seconds,
            #: Named precisely, because "does the policy allow inline script?" has two
            #: different answers here: the elements are refused, the event-handler
            #: *attributes* are not. A dashboard reading one boolean would report the
            #: wrong one of them.
            "csp_html_inline_script_elements": _allows_inline_script_element(self.csp_html),
            "csp_html_inline_event_attributes": "script-src-attr 'unsafe-inline'" in self.csp_html,
            "csp_html_script_sources": _directive_sources(self.csp_html, "script-src"),
            "problems": list(self.problems),
            "remarks": list(self.remarks),
        }


def _directive_sources(policy: str, directive: str) -> list[str]:
    """The source list of one CSP directive, or ``[]`` when the policy omits it.

    Written out rather than regexed inline because the difference this module reports on -
    ``script-src`` versus ``script-src-attr`` - is exactly the difference a loose pattern
    gets wrong: ``script-src[^;]*'unsafe-inline'`` happily matches the *attribute* directive,
    and a readiness check that says "inline scripts are allowed" when they are not is worse
    than no check at all.
    """
    for clause in str(policy).split(";"):
        parts = clause.split()
        if parts and parts[0].lower() == directive:
            return parts[1:]
    return []


def _allows_inline_script_element(policy: str) -> bool:
    """Whether an inline ``<script>`` **element** would be allowed to run.

    ``script-src-elem`` wins over ``script-src`` when both are present, which is the CSP
    rule and the reason a policy cannot be judged by looking at one of them.
    """
    sources = _directive_sources(policy, "script-src-elem") or _directive_sources(
        policy, "script-src"
    )
    return "'unsafe-inline'" in sources or "'strict-dynamic'" in sources


def build_policy(
    *,
    worker_origins: Iterable[str] = (),
    admin_origins: Iterable[str] = (),
    trusted_proxies: Iterable[str] = DEFAULT_TRUSTED_PROXIES,
    admin_networks: Iterable[str] = (),
    admin_paths: Iterable[str] = DEFAULT_ADMIN_PATHS,
    allowed_methods: Iterable[str] = DEFAULT_ALLOWED_METHODS,
    allowed_headers: Iterable[str] = DEFAULT_ALLOWED_HEADERS,
    allow_credentials: bool = True,
    max_age_seconds: int = 600,
    security_headers: bool = True,
    hsts_max_age_seconds: int = 15552000,
    csp_api: str | None = None,
    csp_html: str | None = None,
) -> NetworkPolicy:
    """Build the policy, collecting problems instead of raising on the first one.

    Every problem is reported together: an operator fixing ``ADMIN_IP_ALLOWLIST`` should not
    have to restart to discover the next typo in ``TRUSTED_PROXIES``. A policy with any
    problem has ``admin_gate_enabled`` true and matches nothing, i.e. it fails closed, and
    the startup gate refuses to serve until it is fixed (see ``readiness``).
    """
    problems: list[str] = []
    remarks: list[str] = []

    def rules(entries: Iterable[str], *, admin: bool, setting: str) -> tuple[OriginRule, ...]:
        compiled = []
        for entry in entries or ():
            try:
                compiled.append(compile_origin_rule(entry, admin=admin))
            except NetworkPolicyError as exc:
                problems.append(f"{setting}: {exc}")
        return tuple(compiled)

    worker_rules = rules(worker_origins, admin=False, setting="CORS_WORKER_ORIGINS")
    admin_rules = rules(admin_origins, admin=True, setting="CORS_ADMIN_ORIGINS")

    if any(rule.allow_any for rule in worker_rules) and allow_credentials:
        # ``Access-Control-Allow-Origin: *`` and credentials are mutually exclusive by spec,
        # and silently sending one or the other is how a deployment ends up with a browser
        # blocking every call. Say it, and drop credentials rather than the wildcard, since
        # the wildcard is a deliberate choice and credentials are the default.
        remarks.append(
            "CORS_WORKER_ORIGINS contains '*', so Access-Control-Allow-Credentials is not "
            "sent (the browser forbids the combination); this app authenticates with a "
            "bearer token rather than a cookie, so that is usually fine"
        )
        allow_credentials = False

    try:
        proxies = parse_networks(trusted_proxies, setting="TRUSTED_PROXIES")
    except NetworkPolicyError as exc:
        problems.append(str(exc))
        proxies = ()

    try:
        admin_nets = parse_networks(admin_networks, setting="ADMIN_IP_ALLOWLIST")
    except NetworkPolicyError as exc:
        problems.append(str(exc))
        # Fail closed: keep a gate that matches nothing rather than one that matches all.
        admin_nets = (ipaddress.ip_network("255.255.255.255/32"),)

    if any(network.prefixlen == 0 for network in proxies):
        remarks.append(
            "TRUSTED_PROXIES trusts every peer, so any client may forge X-Forwarded-For; "
            "an allowlist on top of that is only as strong as the network in front of it"
        )
    if not admin_nets:
        remarks.append(
            "ADMIN_IP_ALLOWLIST is empty: administrator routes are reachable from any "
            "address that can open a connection, protected only by their token"
        )
    if not worker_rules and not admin_rules:
        remarks.append(
            "no CORS origins are configured: no cross-origin client may read this API "
            "(same-origin pages, including the bundled frontend, are unaffected)"
        )
    effective_html = str(csp_html or CSP_HTML)
    if _allows_inline_script_element(effective_html):
        remarks.append(
            "the document CSP allows 'unsafe-inline' for script *elements*, so an injected "
            "<script> block would run: the bundled pages should have no inline script left, "
            "and a custom CSP_HTML should say so"
        )
    if "script-src-attr 'unsafe-inline'" in effective_html:
        remarks.append(
            "the document CSP allows 'unsafe-inline' for event-handler *attributes* only: the "
            "console puts its handlers in onclick= attributes, so an injected <img onerror=...> "
            "would still run - the refactor that closes this is one delegated listener"
        )
    if "cdn.tailwindcss.com" in effective_html:
        remarks.append(
            "the document CSP loads cdn.tailwindcss.com: the bundled frontend no longer "
            "needs it - every class it uses is a rule in frontend/style.css - so this is "
            "either a stale setting or a page that reintroduced the browser-side compiler"
        )

    paths = tuple(str(path).rstrip("/") or "/" for path in (admin_paths or DEFAULT_ADMIN_PATHS) if str(path).strip())

    return NetworkPolicy(
        worker_origins=worker_rules,
        admin_origins=admin_rules,
        trusted_proxies=proxies,
        admin_networks=admin_nets,
        admin_paths=paths or DEFAULT_ADMIN_PATHS,
        allowed_methods=tuple(str(m).upper() for m in allowed_methods),
        allowed_headers=tuple(str(h) for h in allowed_headers),
        allow_credentials=bool(allow_credentials),
        max_age_seconds=int(max_age_seconds),
        security_headers=bool(security_headers),
        hsts_max_age_seconds=int(hsts_max_age_seconds),
        csp_api=str(csp_api or CSP_API),
        csp_html=str(csp_html or CSP_HTML),
        remarks=tuple(remarks),
        problems=tuple(problems),
    )


def policy_from_settings(settings) -> NetworkPolicy:
    """Build the policy from ``config.settings``. One place, so the names cannot drift."""
    worker = list(getattr(settings, "cors_worker_origins", ()) or ())
    # The pre-hardening name. Merged rather than renamed out from under a deployment: an
    # existing .env with ALLOWED_ORIGINS= keeps working, and the entries are worker-level,
    # which is exactly what they were allowed to do before.
    for legacy in getattr(settings, "allowed_origins", ()) or ():
        if legacy not in worker:
            worker.append(legacy)
    return build_policy(
        worker_origins=worker,
        admin_origins=getattr(settings, "cors_admin_origins", ()) or (),
        trusted_proxies=getattr(settings, "trusted_proxies", ()) or DEFAULT_TRUSTED_PROXIES,
        admin_networks=getattr(settings, "admin_ip_allowlist", ()) or (),
        admin_paths=getattr(settings, "admin_allowlist_paths", ()) or DEFAULT_ADMIN_PATHS,
        allowed_methods=getattr(settings, "cors_allowed_methods", ()) or DEFAULT_ALLOWED_METHODS,
        allowed_headers=getattr(settings, "cors_allowed_headers", ()) or DEFAULT_ALLOWED_HEADERS,
        allow_credentials=bool(getattr(settings, "cors_allow_credentials", True)),
        max_age_seconds=int(getattr(settings, "cors_max_age_seconds", 600) or 0),
        security_headers=bool(getattr(settings, "security_headers_enabled", True)),
        hsts_max_age_seconds=int(getattr(settings, "hsts_max_age_seconds", 15552000) or 0),
        csp_api=getattr(settings, "csp_api", None),
        csp_html=getattr(settings, "csp_html", None),
    )


#: The policy in force. Resolved per request through ``policy()`` so that a reload - or a
#: test that wants to exercise a different configuration without rebuilding the app - can
#: replace it by assigning ``_policy``.
_policy: NetworkPolicy | None = None


def policy() -> NetworkPolicy:
    global _policy
    if _policy is None:
        import config

        _policy = policy_from_settings(config.settings)
    return _policy


def reload_policy() -> NetworkPolicy:
    """Rebuild from the current settings (``/api/v1/readiness`` and tests use this)."""
    global _policy

    import config

    _policy = policy_from_settings(config.settings)
    return _policy


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------
def security_headers(
    policy: NetworkPolicy,
    *,
    scheme: str,
    forwarded_proto: str | None,
    peer_trusted: bool,
    is_document: bool,
) -> list[tuple[bytes, bytes]]:
    """The header set for one response.

    ``is_document`` splits the CSP in two: a page gets a policy that names the resources it
    uses, and an API response gets one that denies everything (a JSON body has no business
    loading a script or being framed).

    HSTS is sent only when the connection really is TLS from our point of view: the request
    scheme is ``https``, or a *trusted* proxy says it terminated TLS
    (``X-Forwarded-Proto: https``). Pinning a host to HTTPS from a plain-HTTP deployment
    would lock out exactly the worker whose phone is talking to a site server over HTTP,
    and the header is ignored by browsers on HTTP anyway - so sending it blind is all risk.
    """
    headers: list[tuple[bytes, bytes]] = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        #: A document gets ``strict-origin-when-cross-origin`` - the browser default that
        #: still sends the origin on a cross-origin navigation and nothing at all on a
        #: downgrade to HTTP, which is what a page at a site needs when it is opened from a
        #: tunnel or a LAN address. A JSON response gets ``no-referrer``, because a JSON
        #: body is not a navigation source and cannot be one: the stricter value is free
        #: there, and it stops an error payload from ever being quoted as a referrer.
        (
            b"referrer-policy",
            b"strict-origin-when-cross-origin" if is_document else b"no-referrer",
        ),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (
            b"permissions-policy",
            # Camera and location are the two the app legitimately asks for; everything
            # listed as denied is a capability this application never uses, closed off
            # before somebody adds a third-party widget that does.
            b"camera=(self), geolocation=(self), microphone=(), display-capture=(), "
            b"payment=(), usb=(), serial=()",
        ),
        (b"content-security-policy", (policy.csp_html if is_document else policy.csp_api).encode()),
    ]
    if policy.security_headers and policy.hsts_max_age_seconds > 0:
        https = str(scheme).lower() == "https"
        if not https and peer_trusted and forwarded_proto:
            https = "https" in str(forwarded_proto).lower()
        if https:
            headers.append(
                (b"strict-transport-security",
                 f"max-age={policy.hsts_max_age_seconds}; includeSubDomains".encode())
            )
    return headers


#: Statuses that carry no body at all. A ``304`` is the one that matters: the frontend is
#: served ``Cache-Control: no-cache, must-revalidate`` on purpose (see ``revalidate_frontend_assets``
#: in ``main.py``), so from the second load of a page onwards the browser revalidates and the
#: server answers ``304 Not Modified``.
_BODILESS_STATUSES = frozenset({204, 205, 304})


def _accepts_html(request_headers: list | None) -> bool:
    """Whether the *request* asked for a page, which is all a bodyless response leaves."""
    accept = _header(request_headers or [], b"accept") or b""
    return b"text/html" in bytes(accept).lower()


def _is_document(
    content_type: bytes | str | None,
    *,
    status: int = 200,
    request_headers: list | None = None,
) -> bool:
    """Whether this response is a page, for the split between the two CSPs.

    The content-type is the evidence, except for a status that carries no body: the browser
    reuses the *cached* entity and replaces the stored headers with the ones sent alongside
    the ``304``, so a policy picked from a content-type the 304 does not have silently
    downgrades a revalidated page to ``CSP_API`` - ``default-src 'none'`` - and every script
    on it is refused from then on. The page then sits on its own "Checking…" placeholder and
    reads as a slow connection, which is the failure this repository treats as its worst.

    For those statuses the request is the only evidence left, so ``Accept`` decides: a
    navigation names ``text/html``, and a ``fetch`` from a page does not.
    """
    if status in _BODILESS_STATUSES:
        return _accepts_html(request_headers)
    return "text/html" in (
        content_type.decode("latin-1").lower() if isinstance(content_type, bytes) else str(content_type or "").lower()
    )


def _header(message_headers: list, name: bytes) -> bytes | None:
    wanted = name.lower()
    for key, value in message_headers or ():
        if bytes(key).lower() == wanted:
            return bytes(value)
    return None


def _merge_headers(existing: list, additions: list[tuple[bytes, bytes]]) -> list:
    """Add headers the response does not already carry, and merge ``Vary``.

    A route that sets its own ``Content-Security-Policy`` (a download, a report, the
    metrics payload) wins: this middleware adds a baseline, it does not overrule the
    handler that knows what the body is. ``Vary`` is the exception, because two values of
    ``Vary`` are not a conflict but a union - and dropping an existing ``Vary: Accept``
    would be a caching bug we introduced while fixing one.
    """
    result = list(existing or ())
    present = {bytes(key).lower() for key, _ in result}
    for name, value in additions:
        if name.lower() == b"vary":
            current = _header(result, b"vary")
            if current is None:
                result.append((name, value))
            elif value.lower() not in current.lower():
                result = [(key, val) for key, val in result if bytes(key).lower() != b"vary"]
                result.append((b"vary", b", ".join([current, value])))
            continue
        if name.lower() in present:
            continue
        result.append((name, value))
        present.add(name.lower())
    return result


# ---------------------------------------------------------------------------
# the middleware
# ---------------------------------------------------------------------------
@dataclass
class _Throttle:
    """Bounded log throttle: one line per key per window, and a hard cap on keys."""

    window: float = _LOG_THROTTLE_SECONDS
    max_keys: int = _LOG_THROTTLE_MAX_KEYS
    seen: dict[str, float] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        previous = self.seen.get(key)
        if previous is not None and now - previous < self.window:
            return False
        if len(self.seen) >= self.max_keys:
            # Bounded memory beats complete bookkeeping here: a flood is exactly when the
            # process must not grow a dict whose keys an attacker chooses.
            self.seen.clear()
        self.seen[key] = now
        return True


class NetworkGuardMiddleware:
    """Origin policy, admin network gate, and response headers, in one ASGI pass.

    Pure ASGI rather than ``BaseHTTPMiddleware``: this wrapper inspects and rewrites
    ``http.response.start``, and it must see the response exactly once whether it is a JSON
    body, a streamed report or a ``StaticFiles`` file. It also avoids the extra task that
    ``BaseHTTPMiddleware`` allocates per request - the punch path already pays for a pool
    submission and a database write, and a network policy is not worth another one.
    """

    def __init__(self, app, policy_obj: NetworkPolicy | None = None):
        self.app = app
        self._explicit = policy_obj

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        active = self._explicit or policy()
        path = str(scope.get("path") or "/")
        method = str(scope.get("method") or "GET").upper()
        request_headers = scope.get("headers") or []
        origin = _header(request_headers, b"origin")
        origin_text = origin.decode("latin-1") if origin else None

        # Noticed on *every* path, not just admin ones: a proxy that was never declared is a
        # deployment bug whether or not an administrator happens to be calling, and the
        # allowlist is checking the wrong address until somebody fixes it. Readiness reports
        # it; nothing is refused here, because on a worker route the peer address is the
        # honest answer anyway.
        if _header(request_headers, b"x-forwarded-for") and not _peer_trusted(active, scope):
            record_untrusted_forward((scope.get("client") or (None, None))[0])

        if active.admin_gate_enabled and active.is_admin_path(path):
            refusal = self._admin_refusal(active, scope, path, request_headers)
            if refusal is not None:
                await self._refuse(active, scope, send, refusal, origin_text, path)
                return

        if method == "OPTIONS" and origin_text is not None and _header(request_headers, b"access-control-request-method"):
            await self._preflight(active, scope, send, origin_text, path)
            return

        cors = self._cors_pairs(active, origin_text, path) if origin_text else []
        if origin_text:
            cors.append((b"vary", b"Origin"))

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                additions = list(cors)
                if active.security_headers:
                    additions += security_headers(
                        active,
                        scheme=str(scope.get("scheme") or "http"),
                        forwarded_proto=_decoded(_header(request_headers, b"x-forwarded-proto")),
                        peer_trusted=_peer_trusted(active, scope),
                        is_document=_is_document(
                            _header(message.get("headers") or [], b"content-type"),
                            status=int(message.get("status") or 200),
                            request_headers=request_headers,
                        ),
                    )
                message["headers"] = _merge_headers(message.get("headers") or [], additions)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    # -- decisions ----------------------------------------------------------
    def _admin_refusal(self, active: NetworkPolicy, scope, path: str, headers: list) -> str | None:
        peer = (scope.get("client") or (None, None))[0]
        forwarded = _decoded(_header(headers, b"x-forwarded-for"))
        resolution = active.resolve_client(peer, forwarded)
        if resolution.problem:
            return resolution.problem
        if active.client_allowed(resolution):
            return None
        return REASON_IP

    def _cors_pairs(self, active: NetworkPolicy, origin: str | None, path: str) -> list[tuple[bytes, bytes]]:
        """The CORS headers a *real* (non-preflight) response carries."""
        kind = active.origin_class(origin)
        if kind is None or not active.allows_origin(origin, path):
            return []
        rule_any = kind == CLASS_WORKER and any(rule.allow_any for rule in active.worker_origins)
        if rule_any:
            # Wildcard and credentials are mutually exclusive; ``build_policy`` has already
            # dropped credentials for this configuration.
            return [(b"access-control-allow-origin", b"*")]
        pairs = [
            (b"access-control-allow-origin", str(origin).encode()),
            (b"access-control-expose-headers", b"Content-Disposition"),
        ]
        if active.allow_credentials:
            pairs.append((b"access-control-allow-credentials", b"true"))
        return pairs

    async def _preflight(self, active: NetworkPolicy, scope, send, origin: str, path: str) -> None:
        """Answer a CORS preflight.

        Refused preflights are answered here rather than passed to the app: the app has no
        ``OPTIONS`` handlers, so letting it answer would produce a 405 with no CORS headers,
        which the browser reports as an opaque network error - the opposite of a diagnosable
        refusal.
        """
        allowed = active.allows_origin(origin, path)
        headers = scope.get("headers") or []
        request_headers = _decoded(_header(headers, b"access-control-request-headers")) or ""
        wanted = [item.strip().lower() for item in request_headers.split(",") if item.strip()]
        permitted = [item for item in active.allowed_headers if item.lower() in wanted]
        unpermitted = [item for item in wanted if item.lower() not in {h.lower() for h in active.allowed_headers}]

        if not allowed or unpermitted:
            # A preflight asking for a header the policy does not name is a refusal, not a
            # silent allow: the browser asked a question, so the answer has to be readable.
            telemetry.count_netguard_refusal(reason=REASON_ORIGIN)
            body = json.dumps(
                {
                    "error_code": "cors_refused",
                    "message": (
                        "This origin may not call that path cross-origin."
                        if not allowed
                        else "That request header is not allowed: " + ", ".join(unpermitted)
                    ),
                    "origin": origin,
                }
            ).encode()
            await self._respond(active, scope, send, 403, body, extra=[(b"vary", b"Origin")])
            if _THROTTLE.allow(f"preflight:{origin}:{path}"):
                log.warning("CORS preflight refused for %s -> %s", origin, path)
            return

        pairs = [
            (b"access-control-allow-origin", b"*" if _origin_is_wildcard(active, origin) else origin.encode()),
            (b"access-control-allow-methods", ", ".join(active.allowed_methods).encode()),
            (b"access-control-allow-headers", ", ".join(permitted or active.allowed_headers).encode()),
            (b"access-control-max-age", str(active.max_age_seconds).encode()),
            (b"vary", b"Origin"),
        ]
        if active.allow_credentials and not _origin_is_wildcard(active, origin):
            pairs.append((b"access-control-allow-credentials", b"true"))
        await self._respond(active, scope, send, 200, b"", extra=pairs)

    async def _refuse(self, active: NetworkPolicy, scope, send, reason: str, origin: str | None, path: str) -> None:
        telemetry.count_netguard_refusal(reason=reason)
        peer = (scope.get("client") or (None, None))[0]
        forwarded = _decoded(_header(scope.get("headers") or [], b"x-forwarded-for"))
        resolution = active.resolve_client(peer, forwarded)
        if _THROTTLE.allow(f"{reason}:{resolution.ip or peer}"):
            log.warning(
                "network policy refused %s %s: %s (client=%s peer=%s chain=%s)",
                scope.get("method"), path, reason, resolution.ip, peer, list(resolution.chain),
            )
        # The message has to be actionable without being informative to a scanner: it says
        # what to do (come from an allowed network, or ask the operator) and never which
        # network is allowed, which would hand over the allowlist one guess at a time.
        messages = {
            REASON_IP: "This administrator endpoint is not reachable from your network address.",
            REASON_PROXY: (
                "This request arrived with X-Forwarded-For from a host that is not a trusted "
                "proxy, so the real client address cannot be determined. The operator must add "
                "the proxy to TRUSTED_PROXIES."
            ),
        }
        body = json.dumps({"error_code": reason, "message": messages.get(reason, "Refused by network policy.")}).encode()
        extra = self._cors_pairs(active, origin, path) if origin else []
        await self._respond(active, scope, send, 403, body, extra=extra)

    async def _respond(self, active: NetworkPolicy, scope, send, status: int, body: bytes, *, extra: list) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ]
        if active.security_headers:
            headers += security_headers(
                active,
                scheme=str(scope.get("scheme") or "http"),
                forwarded_proto=_decoded(_header(scope.get("headers") or [], b"x-forwarded-proto")),
                peer_trusted=_peer_trusted(active, scope),
                is_document=False,
            )
        headers = _merge_headers(headers, extra)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _decoded(value: bytes | None) -> str | None:
    return value.decode("latin-1") if value else None


def _peer_trusted(active: NetworkPolicy, scope) -> bool:
    peer = (scope.get("client") or (None, None))[0]
    return _in_networks(_address(peer), active.trusted_proxies)


def _origin_is_wildcard(active: NetworkPolicy, origin: str) -> bool:
    return active.origin_class(origin) == CLASS_WORKER and any(rule.allow_any for rule in active.worker_origins)


#: The last time an ``X-Forwarded-For`` arrived from a peer that is not a trusted proxy.
#: Bounded by construction: three scalars, never a key per client.
_forward_misuse: dict[str, Any] = {"count": 0, "last_peer": None, "last_at": None}


def record_untrusted_forward(peer: str | None) -> None:
    """Note that a forwarded header came from somewhere we do not trust."""
    _forward_misuse["count"] = int(_forward_misuse["count"]) + 1
    _forward_misuse["last_peer"] = peer
    _forward_misuse["last_at"] = time.time()


def forward_misuse() -> dict[str, Any]:
    """A copy, so a reader cannot mutate the record it is reporting on."""
    return dict(_forward_misuse)


_THROTTLE = _Throttle()


def install(app) -> NetworkPolicy:
    """Add the middleware to ``app``. Returns the policy in force.

    No policy is passed to the constructor on purpose: the middleware resolves
    ``policy()`` per request, so a reload (or a test exercising a different configuration)
    does not need the application object to be rebuilt.
    """
    app.add_middleware(NetworkGuardMiddleware)
    return policy()
