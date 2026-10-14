"""The one rate limiter every router shares.

``slowapi`` attaches counting state to a ``Limiter`` instance, and ``app.state.limiter``
must be that same instance for the exception handler to render a 429. Feature modules
therefore import the limiter from here instead of building their own - two limiters
would mean two independent buckets and a route whose 429 handler does not fire.

Named ``rate_limit`` and not ``limits`` on purpose: ``slowapi`` imports the third-party
``limits`` package internally, so a local ``limits.py`` shadows it and breaks the import
of ``slowapi`` itself.

WHY THE KEY IS NOT ``get_remote_address``
----------------------------------------
``get_remote_address`` is ``request.client.host``, and behind a TLS-terminating edge that is
the *edge's* address - one bucket for the whole deployment. The measured consequence is the
punch endpoint's 15/minute answering a site instead of a phone: the first fifteen punches
anywhere in the country inside a minute, and every worker after them refused by a limit none of
them had reached, looking exactly like a backend outage.

So the key is the client address the network policy can *justify*: the peer uvicorn has already
resolved (``serve.py`` feeds ``--forwarded-allow-ips`` the same ``TRUSTED_PROXIES`` list), or
the rightmost ``X-Forwarded-For`` entry written by a proxy that really is trusted. A header
from a peer that is not a trusted proxy never becomes a key, or the limit is defeated by typing
one - which is the bug a naive "just read X-Forwarded-For" fix ships.
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter

import netguard


def client_key(request: Request) -> str:
    """The address a rate limit is counted against. Never an unverified header.

    Two layers can resolve the client, and this reads whichever one has the answer:

    * **uvicorn**, when ``--forwarded-allow-ips`` matched the peer, has already rewritten
      ``scope["client"]`` from ``X-Forwarded-For`` - so ``request.client.host`` *is* the real
      client and the header is a leftover. ``resolve_client`` reports that leftover as a
      problem (a forwarded chain arriving from a peer that is not itself a declared proxy),
      and the resolved peer wins, which is the correct answer in this direction too.
    * **netguard**, when the peer *is* a declared proxy and nothing rewrote it, resolves the
      chain itself: the rightmost entry that is not a trusted hop.

    A request with neither (a direct connection) keys on its own address, exactly as before.
    """
    peer = request.client.host if request.client else None
    resolution = netguard.policy().resolve_client(peer, request.headers.get("x-forwarded-for"))
    if resolution.problem:
        return str(peer or "unknown")
    return str(resolution.ip or peer or "unknown")


limiter = Limiter(key_func=client_key)
