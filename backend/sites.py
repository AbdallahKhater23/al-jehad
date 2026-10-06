"""Creating a site: a Google Maps link, a coordinate pair, and the fence that follows.

WHAT THIS MODULE IS FOR
-----------------------
The console's Sites screen has always been able to add a site, and the form has always asked
for the one thing nobody can produce on a phone: two numbers, in a box whose entire
instruction was the placeholder ``Lat,Lon``. This module is the other half of the workflow
the requirements ask for, and it has two parts.

**Resolving a Maps link.** An administrator who has found the building in Google Maps has a
URL in their clipboard, and that URL is a *better* input than a pair of typed numbers: it is
what the map itself says, it survives a copy-and-paste, and it needs no coordinate format
anybody has to remember. The coordinates are in the URL in one of a handful of shapes -
``@29.35,47.98,17z`` in the browser's own address bar, ``?q=29.35,47.98`` from a share
sheet, ``!3d29.35!4d47.98`` in the encoded data blob, and ``/search/29.35,47.98`` from the
mobile app - so they are read out of the URL directly. A *shortened* link
(``maps.app.goo.gl``, ``goo.gl/maps``) carries no coordinates at all: the coordinates only
exist after the redirect has been followed, which is why ``POST /api/v1/resolve-maps-link``
exists and why the page asks the client to parse first and the server second.

**Creating the site.** ``POST /api/v1/sites`` writes the row and puts the fence in the
process's cache in the same request (see ``geofence.store_site_fence``), so the punch that
follows is measured against the boundary that was just drawn rather than against the one
this worker loaded at startup.

WHAT A MAPS URL IS ALLOWED TO BE
--------------------------------
An outbound fetch of a URL somebody typed is the textbook server-side request forgery, and
this one is worse than most: the URL is *meant* to be followed somewhere else, so "it is a
maps link" cannot be decided by looking at where it ends up. Two rules, and both are
checked before any connection is opened:

* **the host is on a short list of Google Maps hosts** (``maps.app.goo.gl``, ``goo.gl``,
  ``maps.google.com``, ``www.google.com``, the country domains, and ``googleusercontent``
  links), matched as an exact host or a subdomain of one - so ``maps.google.com.evil.test``
  is refused, which is the shape a suffix check gets wrong;
* **the scheme is https or http**, so ``file:``, ``gopher:`` and ``data:`` cannot reach the
  transport at all.

The redirect *target* is deliberately not checked against that list. Google's share links
bounce through a consent host and land on ``www.google.com/maps/...``; requiring every hop
to be a maps host would refuse the real links this feature exists for, and the thing the
allowlist is protecting - "which hosts can this deployment be made to call" - is already
decided by the first request's host. What is bounded instead is the *cost*: a hop limit, a
timeout, and a response body that is read only when the final URL has no coordinates in it.

THE ONE PLACE A URL IS FETCHED, AND WHY IT IS ``requests``
---------------------------------------------------------
``backend/tests/test_outbound_network_guard.py`` replaces ``requests.post``/``requests.get``
so that no test can reach the network, and fails if any module names a client the guard does
not cover. The requirement suggests ``httpx.AsyncClient``; this deployment already ships
``requests`` as a runtime dependency and ``httpx`` only in the dev/test manifest, so the
fetch is made through ``requests`` - in a threadpool, so the event loop is not blocked -
and the guard therefore covers it by construction. The pair is recorded in that suite's
``KNOWN_EXITS`` with this reason.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

import audit
import clock
import geofence
import shift_windows
import textguard
from database import db
from security import CurrentUser, admin_only

router = APIRouter(tags=["sites"])

#: How long the resolver may spend on one link, and how many redirects it will follow.
#:
#: Three hops is what a real share link needs (``maps.app.goo.gl`` -> a consent host ->
#: ``www.google.com/maps/...``) with one to spare; ten is the browser's own limit and would
#: let a hostile chain hold a worker for the whole timeout. Five seconds is under the
#: console's own patience and far under the request timeout.
RESOLVE_TIMEOUT_SECONDS = 5.0
RESOLVE_MAX_REDIRECTS = 4
#: How much of a page is read looking for coordinates when the final URL has none. A Maps
#: page is megabytes; the coordinates are in the first few kilobytes, and a cap is what
#: keeps a resolver from downloading a video because somebody pasted the wrong link.
RESOLVE_MAX_BYTES = 256_000

#: The hosts a Maps link may name. Exact host, or a subdomain of one - see the module
#: docstring for why the *redirect target* is not held to this list.
MAPS_HOSTS: tuple[str, ...] = (
    "maps.app.goo.gl",
    "goo.gl",
    "maps.google.com",
    "www.google.com",
    "google.com",
    "maps.google.co.uk",
    "maps.google.ae",
    "maps.google.com.eg",
    "maps.google.com.sa",
    "maps.google.co.in",
    "googleusercontent.com",
)

#: ``@29.351234,47.984712,17z`` - the shape the browser's own address bar carries. The zoom
#: is optional and ignored; the two numbers are the answer.
_AT_PAIR = re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
#: ``!3d29.351234!4d47.984712`` - the encoded place blob inside a Maps URL.
_DATA_PAIR = re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)")
#: ``/search/29.351234,47.984712`` or ``/place/29.35,47.98`` - the mobile app's shape.
_PATH_PAIR = re.compile(r"/(?:search|place|dir|ll|loc:)/(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
#: ``?q=29.351234,47.984712`` / ``?query=`` / ``?ll=`` / ``?center=``, and the same with a
#: space after the comma, which is what a share sheet produces.
_QUERY_PAIR = re.compile(r"^(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)$")
#: A bare ``29.351234,47.984712`` pasted with no URL around it at all.
_BARE_PAIR = re.compile(r"^\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$")
#: A loose sweep for a coordinate pair anywhere in a *page body*, used only as a last
#: resort after a redirect. Anchored on the decimal point so it cannot read a phone number.
_LOOSE_PAIR = re.compile(r"(-?\d{1,3}\.\d{4,}),\s*(-?\d{1,3}\.\d{4,})")


# ---------------------------------------------------------------------------
# reading coordinates out of a URL
# ---------------------------------------------------------------------------
def _plausible(latitude: float, longitude: float) -> bool:
    """Whether a pair could be a real fix, by the one rule the whole app uses."""
    return geofence.within_wgs84(latitude, longitude)


def _strip_loc_prefix(value: str) -> str:
    """``loc:29.351234,47.984712`` -> ``29.351234,47.984712``.

    The mobile app's share sheet writes the pair behind that prefix, so a link copied from a
    phone carries a coordinate the anchored query rule would otherwise refuse. Stripped
    rather than matched with a second pattern: one shape with an optional prefix is one rule,
    and two patterns are how the browser's parser and this one drift apart.
    """
    return re.sub(r"^\s*loc\s*:\s*", "", str(value), flags=re.IGNORECASE).strip()


def coordinates_from_text(text: str) -> tuple[float, float] | None:
    """The first plausible ``(latitude, longitude)`` in ``text``, or ``None``.

    Pure, and therefore the whole parser is testable without a network, a socket or a
    monkeypatched transport. The order is deliberate: the shapes Google actually produces,
    most specific first, and only then a loose sweep - so a URL that carries both a ``@``
    pair and a query pair answers with the ``@`` pair, which is the one the map was centred
    on when the link was copied.
    """
    if not text:
        return None
    raw = str(text)

    def first(pattern: re.Pattern[str]) -> tuple[float, float] | None:
        for match in pattern.finditer(raw):
            try:
                latitude, longitude = float(match.group(1)), float(match.group(2))
            except (TypeError, ValueError):
                continue
            if _plausible(latitude, longitude):
                return latitude, longitude
        return None

    # ``?q=`` first: a share link's own answer, and the one a query-string check is for.
    try:
        query = parse_qs(urlsplit(raw).query)
    except ValueError:
        query = {}
    for key in ("q", "query", "ll", "center", "daddr", "destination", "sll"):
        for value in query.get(key, []):
            match = _QUERY_PAIR.match(_strip_loc_prefix(value))
            if match and _plausible(float(match.group(1)), float(match.group(2))):
                return float(match.group(1)), float(match.group(2))

    bare = _BARE_PAIR.match(raw)
    if bare and _plausible(float(bare.group(1)), float(bare.group(2))):
        return float(bare.group(1)), float(bare.group(2))

    for pattern in (_DATA_PAIR, _AT_PAIR, _PATH_PAIR, _LOOSE_PAIR):
        found = first(pattern)
        if found is not None:
            return found
    return None


def is_shortened(link: str) -> bool:
    """Whether ``link`` is a share link with no coordinates in it, so it has to be followed."""
    host = _host_of(link)
    return host in ("maps.app.goo.gl", "goo.gl")


def _host_of(link: str) -> str:
    try:
        return (urlsplit(str(link)).hostname or "").lower()
    except ValueError:
        return ""


def host_is_allowed(link: str) -> bool:
    """Whether this deployment may fetch ``link``. See the module docstring.

    Exact host or subdomain: ``www.google.com`` matches ``google.com``, and
    ``maps.google.com.evil.test`` matches nothing - which is the whole reason the comparison
    is done on labels rather than with ``endswith``.
    """
    host = _host_of(link)
    if not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in MAPS_HOSTS)


def resolve_maps_link(link: str) -> dict[str, Any]:
    """Turn a Maps URL into coordinates, following a shortened link if that is what it is.

    Raises ``HTTPException`` with a sentence an administrator can act on: 400 for a URL that
    is not a URL or names a host this deployment will not call, 422 for a link that was
    fetched and simply has no coordinates in it, 502 for a transport failure or a hop limit.
    """
    text = str(link or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Paste a Google Maps link or a lat,lng pair.")

    # The cheap path first, and the one that costs nothing: the coordinates are already in
    # the text. A full ``https://www.google.com/maps/@29.35,47.98,17z`` never leaves the
    # process, which is most pastes.
    direct = coordinates_from_text(text)
    if direct is not None:
        return {
            "latitude": direct[0],
            "longitude": direct[1],
            "resolved": False,
            "source": "link",
        }

    if not re.match(r"^https?://", text, re.IGNORECASE):
        # Not a URL and not a coordinate pair: a bare ``29.35,47.98`` was already handled
        # above, so this is a typo or a search phrase, and saying so beats a 500 from a
        # transport that was handed "Tower B".
        raise HTTPException(
            status_code=400,
            detail=(
                "That is not a Maps link and not a lat,lng pair. Paste the URL from Google "
                "Maps, or the two numbers from a long-press."
            ),
        )

    if not host_is_allowed(text):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only Google Maps links can be resolved: paste a maps.app.goo.gl, goo.gl or "
                "google.com/maps URL, or type the coordinates."
            ),
        )

    final_url, body = _follow(text)
    found = coordinates_from_text(final_url) or coordinates_from_text(body)
    if found is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "That link was followed but carries no coordinates. Open it in Google Maps, "
                "copy the address bar once the place is on screen, and paste that."
            ),
        )
    return {
        "latitude": found[0],
        "longitude": found[1],
        "resolved": True,
        "source": "redirect" if final_url != text else "page",
        "final_url": final_url,
    }


def _follow(link: str) -> tuple[str, str]:
    """Fetch ``link``, following redirects, and hand back ``(final url, readable body)``.

    ``requests`` rather than ``httpx`` - see the module docstring: it is the runtime
    dependency this deployment already has, and the pair the outbound guard replaces. The
    call is synchronous because ``requests`` is; the route runs it in a threadpool.
    """
    import requests

    try:
        response = requests.get(
            link,
            allow_redirects=True,
            timeout=RESOLVE_TIMEOUT_SECONDS,
            headers={
                # A browser-shaped agent: Google answers a share link with a consent page or
                # a 403 for an unfamiliar client, and the coordinates are in neither.
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AttendanceConsole/1.0; +https://maps.google.com)"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same answer
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach that link ({type(exc).__name__}). Check the connection and paste it again.",
        ) from exc

    status = int(getattr(response, "status_code", 0) or 0)
    final_url = str(getattr(response, "url", "") or link)
    if status >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"That link answered HTTP {status}. Open it in a browser and copy the address bar.",
        )

    # The redirect chain is bounded by ``requests`` itself; the check below is for a server
    # that kept handing back a relative ``Location`` - ``requests`` raises on too many hops,
    # and that arrives here as the generic transport failure above.
    hops = len(getattr(response, "history", []) or [])
    if hops > RESOLVE_MAX_REDIRECTS:
        raise HTTPException(
            status_code=502,
            detail="That link redirected too many times to follow.",
        )

    body = ""
    if coordinates_from_text(final_url) is None:
        # Only read the page when the URL itself did not answer: a link that already carries
        # ``@lat,lng`` has cost one HEAD-shaped request and no body at all.
        try:
            chunks: list[bytes] = []
            read = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                chunks.append(chunk)
                read += len(chunk)
                if read >= RESOLVE_MAX_BYTES:
                    break
            body = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a body that will not read is not a failure yet
            body = ""
        body = unquote(body)
    try:
        response.close()
    except Exception:  # noqa: BLE001 - best effort
        pass
    return final_url, body


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class MapsLinkRequest(BaseModel):
    """What the console posts: one URL, or the two numbers pasted in its place."""

    model_config = ConfigDict(extra="ignore")

    url: str

    @field_validator("url")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 4096:
            raise ValueError("is longer than a URL this deployment will read")
        return text


class SiteCreate(BaseModel):
    """A new site, as the creation page posts it.

    ``latitude``/``longitude`` are the modern shape the requirement names; ``location_input``
    is the console's older shape - a ``lat,lon`` pair or a Maps URL - and is accepted here so
    the two forms cannot disagree about what a site is. Exactly one of the two has to be
    usable, which ``_coordinates()`` decides.

    ``site_name`` goes through the same allowlist as ``/admin/sites/add``: it is the primary
    key of ``construction_sites`` and it is written into ``attendance_logs.site_name``, so a
    name carrying markup would be a stored payload in the attendance table too.
    """

    model_config = ConfigDict(extra="ignore")

    site_name: str
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    radius_meters: float | None = Field(default=None, gt=0.0, le=geofence.MAX_RADIUS_METERS)
    location_input: str | None = None
    #: The same three window fields ``/admin/sites/add`` takes, so the creation page can set
    #: a site's hours in the same request it sets its pin.
    #:
    #: ``shift_start_time`` / ``shift_end_time`` are accepted as the same field under the
    #: name the requirement uses for a 24-hour boundary. The columns are the ones the shift
    #: pipeline already reads (``clock_in_window_start`` / ``clock_in_window_end``), so the
    #: alias is a spelling of one stored value rather than a second place hours can live.
    clock_in_window_start: str | None = Field(
        default=None, validation_alias=AliasChoices("clock_in_window_start", "shift_start_time")
    )
    clock_in_window_end: str | None = Field(
        default=None, validation_alias=AliasChoices("clock_in_window_end", "shift_end_time")
    )
    site_timezone: str | None = None
    category_id: int | None = None

    @field_validator("site_name")
    @classmethod
    def _plain_site_name(cls, value: str) -> str:
        return textguard.identifier(
            value, field="Site name", max_length=textguard.MAX_SITE_NAME
        )

    @field_validator("clock_in_window_start", "clock_in_window_end")
    @classmethod
    def _validate_window_time(cls, value: str | None) -> str | None:
        """The site's own hours, by the same rule ``/admin/sites/add`` applies.

        The two routes write the same two columns, so they have to agree about what a time is.
        This one used to accept anything: ``25:00`` posted here was stored, and a stored window
        ``shift_windows`` cannot parse degrades to the company hours at the gate - a fence that
        makes nobody late, arriving through a door nobody was watching.

        Blank is "inherit", which is what a form's empty time box means, so it is read as
        ``None`` rather than refused.
        """
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        if not shift_windows.is_valid_hhmm(candidate):
            raise ValueError(
                "Site clock-in window must be a 24-hour time in HH:MM form (for example "
                f"04:00, 22:00, 00:00); '{value}' is not. "
                "Minutes are 00-59 and hours are 00-23."
            )
        return candidate

    @field_validator("site_timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        """Refuse a zone ``zoneinfo`` cannot resolve, exactly as the console route does.

        A typo like ``Africa/Cario`` would otherwise be stored and then degrade to the default
        zone at every punch, which moves that site's window by an hour or two and presents as
        "the wrong people are late" rather than as a typo.
        """
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        if not shift_windows.is_known_timezone(candidate):
            raise ValueError(
                f"'{value}' is not a timezone this server knows. Use an IANA name such as "
                "Asia/Kuwait or Asia/Riyadh."
            )
        return candidate

    def coordinates(self) -> tuple[float, float]:
        """The site's position, from the modern pair or from the pasted input.

        The modern pair wins when both are present, because it is what the map on the page
        wrote: an administrator who typed a Maps link and *then* dragged the pin meant the
        pin. A link that has to be followed is not followed here - the page resolves it
        first and posts the numbers, so a create request never makes an outbound call.
        """
        if self.latitude is not None and self.longitude is not None:
            latitude, longitude = float(self.latitude), float(self.longitude)
        else:
            text = str(self.location_input or "").strip()
            if not text:
                raise HTTPException(
                    status_code=400,
                    detail="A site needs coordinates: paste a Maps link, or type lat,lng.",
                )
            found = coordinates_from_text(text)
            if found is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid location format. Provide valid GPS coordinates or a Google "
                        "Maps URL."
                    ),
                )
            latitude, longitude = found
        if not geofence.within_wgs84(latitude, longitude):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Location (0,0) is the default reading of a mock-location provider and is "
                    "not a valid fix."
                ),
            )
        return latitude, longitude


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("/resolve-maps-link")
async def resolve_maps_link_route(
    payload: MapsLinkRequest,
    current: CurrentUser = Depends(admin_only),
):
    """Read the coordinates out of a Google Maps link. Administrator only.

    Guarded because it is an *outbound fetch of a URL the caller chooses*: the host allowlist
    above is what makes it safe, and a session is what makes it accountable. A worker's phone
    has no use for it - a punch sends the numbers it already has.
    """
    from fastapi.concurrency import run_in_threadpool

    answer = await run_in_threadpool(resolve_maps_link, payload.url)
    return answer


@router.post("/sites")
async def create_site(
    payload: SiteCreate,
    request: Request,
    current: CurrentUser = Depends(admin_only),
):
    """Create a site, and put its fence in force before this request returns.

    One row in ``construction_sites`` - the same table ``main.site_at`` reads - plus the
    process-local cache the punch endpoint uses (``app.state.sites_cache``), written through
    in the same call. The site is therefore live for the *next* punch rather than for the
    next restart, which is the difference between a fence an administrator just drew and a
    fence that will be in force tomorrow.
    """
    latitude, longitude = payload.coordinates()
    radius = payload.radius_meters if payload.radius_meters is not None else 100.0
    if radius <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Radius must be greater than 0 metres - a site with a radius of 0 can never "
                "be clocked into."
            ),
        )
    window = {
        "clock_in_window_start": payload.clock_in_window_start,
        "clock_in_window_end": payload.clock_in_window_end,
        "site_timezone": payload.site_timezone,
    }
    with db(write=True) as conn:
        if payload.category_id is not None:
            found = conn.execute(
                "SELECT category_id FROM site_categories WHERE category_id = ?",
                (int(payload.category_id),),
            ).fetchone()
            if found is None:
                raise HTTPException(status_code=404, detail="Site category not found.")
        try:
            cursor = conn.execute(
                "INSERT INTO construction_sites "
                "(site_name, lat, lon, radius, clock_in_window_start, clock_in_window_end, "
                "site_timezone, category_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.site_name,
                    latitude,
                    longitude,
                    radius,
                    window["clock_in_window_start"],
                    window["clock_in_window_end"],
                    window["site_timezone"],
                    payload.category_id,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Site name already exists.")
        site_id = int(cursor.lastrowid) if cursor.lastrowid is not None else 0
        _audit(
            conn,
            action="site_create",
            actor=current,
            site_name=payload.site_name,
            after={
                "lat": latitude,
                "lon": longitude,
                "radius": radius,
                **window,
                "category_id": payload.category_id,
            },
            request=request,
        )

    # The write-through, and the reason this endpoint exists rather than the console calling
    # ``/admin/sites/add``: the fence is in the cache the punch reads before the response is
    # written. A cache that could not be updated (an old row this build cannot read) still
    # leaves the site *created* - the database is the durable copy - so it is not fatal.
    try:
        geofence.store_site_fence(
            site_id=site_id,
            site_name=payload.site_name,
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius,
        )
    except Exception:  # pragma: no cover - best effort; the row is what is durable
        geofence.refresh_site_cache()

    return {
        "status": "success",
        "site": {
            "site_id": site_id,
            "site_name": payload.site_name,
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": radius,
            **window,
            "category_id": payload.category_id,
        },
        "message": (
            f"Site '{payload.site_name}' added at ({latitude}, {longitude}) with a "
            f"{radius} m fence."
        ),
        "cached_sites": len(geofence.SITE_CACHE.by_id),
    }


@router.get("/sites")
async def list_cached_sites(current: CurrentUser = Depends(admin_only)):
    """The site fences this process is holding, straight off the cache.

    Not a replacement for ``GET /admin/sites`` - that one reads the table and resolves every
    site's clock-in window through the three layers. This answers the one question the cache
    exists for: *which boundaries will the next punch be measured against*, and how many of
    them this worker has.
    """
    fences = sorted(geofence.SITE_CACHE.by_id.values(), key=lambda fence: fence.site_id)
    return {
        "generation": geofence.SITE_CACHE.generation,
        "count": len(fences),
        "sites": [fence.as_dict() for fence in fences],
    }


def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None,
    site_name: str,
    after: dict[str, Any],
    request: Request | None,
) -> None:
    """Append the creation to ``audit_log``. Never raises.

    The row is the shape ``/admin/sites/add`` writes - ``site_create`` against
    ``construction_sites``, with the geometry in ``after_json`` - so the two creation paths
    are indistinguishable to a reader of the audit log.

    There is no ``before``: a site that did not exist had no earlier value, and the caller
    that adds one is a browser console, so its user agent is recorded.
    """
    audit.record(
        conn,
        action=action,
        actor=actor,
        entity="construction_sites",
        entity_id=site_name,
        after=after,
        request=request,
        user_agent=True,
        created_at=clock.now().strftime(clock.TS_FORMAT),
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
#: The creation page, and the map policy it needs. Registered in ``geofence.MAP_PAGES``,
#: which is what ``main.frontend_page_response`` asks for the relaxed CSP - this page draws
#: the same map the fence editor does, so it needs the same three origins.
PAGE = "admin_add_site.html"


def page_policy() -> str:
    """The CSP this page is served with. Named here so the route does not reach for it."""
    import netguard

    return netguard.CSP_HTML_MAPS
