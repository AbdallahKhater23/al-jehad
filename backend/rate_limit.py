"""The one rate limiter every router shares.

``slowapi`` attaches counting state to a ``Limiter`` instance, and ``app.state.limiter``
must be that same instance for the exception handler to render a 429. Feature modules
therefore import the limiter from here instead of building their own - two limiters
would mean two independent buckets and a route whose 429 handler does not fire.

Named ``rate_limit`` and not ``limits`` on purpose: ``slowapi`` imports the third-party
``limits`` package internally, so a local ``limits.py`` shadows it and breaks the import
of ``slowapi`` itself.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
