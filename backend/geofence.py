"""The site geofence: one in-memory fence, one Haversine check, and the punch that uses it.

WHAT THIS MODULE IS FOR
-----------------------
A worker's phone sends a GPS fix and the server has to answer one question - *is this fix
inside the fence* - before it spends anything on the answer. That ordering is the whole
design:

* the fence is read **once**, at startup, into a process-local cache
  (``app.state.geofence_cache``); the punch path reads the cache and touches no database,
  no disk and no network before it has decided. A fix outside the radius is refused in
  microseconds, before a face is matched, before a frame is written and before a webhook
  is called - which is what makes a refusal cost nothing at the gate and keeps the
  expensive work for the punches that can actually be recorded;
* an administrator's edit writes SQLite **and** the cache in the same request, so the new
  fence is in force for the next punch rather than after a restart. The row is the durable
  copy and the cache is the one the punch reads, and both are written under one lock so
  they cannot disagree inside a worker;
* the *schema of attendance history is untouched*. This module owns two tables of its own
  (``geofence_settings``, ``attendance_punches``) and writes nothing into
  ``attendance_logs``, ``active_sessions`` or any other table the existing pipeline reads.

THE LIMIT WORTH STATING: ONE CACHE PER PROCESS
----------------------------------------------
A deployment running several ASGI workers has one cache per worker, and an edit refreshes
the worker that served the request. The others keep the fence they loaded at startup until
they restart. That is a real limitation and it is the price of the zero-latency read the
punch path is built on; the alternatives are a database read per punch (which is the cost
this design exists to remove) or a shared cache server (a dependency this deployment does
not have). ``CACHE.generation`` and the ``updated_at`` stamp travel with every fence so a
reader can see which one it is holding, and the deployment this was built for serves with
``--reload``/one worker. A multi-worker deployment should be given a shorter restart cadence
or a broadcast (a Postgres ``LISTEN``, a Redis pub/sub) - not a punch-path query.

THE HAVERSINE FORMULA, AND WHY IT IS CLAMPED
--------------------------------------------
``a`` is a float expression whose two terms are each in [0, 1], so a rounding error at the
edges can produce ``a`` slightly above 1 - an antipodal pair, or two fixes that differ by
less than a metre. ``sqrt(1 - a)`` then takes the square root of a *negative* number and
raises ``ValueError: math domain error``, which at a gate is a 500 for a worker standing in
the right place. Both arguments are therefore clamped into [0, 1] before ``atan2``, which
is exact for the two cases that matter: ``a >= 1`` is ``pi`` radians (half the planet) and
``a <= 0`` is zero.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

import audit
import clock
import notifications
from database import db
from security import CurrentUser, admin_only, any_authenticated

router = APIRouter(tags=["geofence"])

#: The mean Earth radius, in metres. The value the formula above and every distance this
#: module reports are computed with, named once so a test can hold the two together.
EARTH_RADIUS_METERS = 6_371_000.0

#: The fence a deployment gets before anybody has configured one.
#:
#: A cold boot must not leave the API in an uninitialised state: with no row in
#: ``geofence_settings`` every punch would be judged against ``None`` and the first worker
#: through the gate would be the one who found out. The coordinates are the deployment's
#: first site (``tests/harness.SITES`` seeds the same pair), and the radius is the console's
#: default. It is deliberately *not* a fence around the whole planet: a default that admits
#: every fix is a geofence that silently verifies nothing.
DEFAULT_LATITUDE = 30.05
DEFAULT_LONGITUDE = 31.23
DEFAULT_RADIUS_METERS = 100.0

#: A fix whose own uncertainty is worse than this is refused as *unusable input* (400), not
#: as "outside the fence" (403). The distinction matters to the person holding the phone:
#: the second one says "you are not at work" about a reading that cannot support the claim
#: either way. 50 m is roughly a phone in a building with no sky view.
MAX_ACCURACY_METERS = 50.0

#: A fence larger than this is not a site - it is a district - and a stored value like that
#: is a typo (a latitude pasted into the radius box). Bounded rather than clamped silently:
#: an administrator who meant 2000 m gets a refusal that names the number.
MAX_RADIUS_METERS = 10_000.0

#: The console's slider range, exported so the page and the API agree about what "the
#: normal range" is. The API accepts wider (see above); the UI does not offer it.
SLIDER_MIN_RADIUS_METERS = 20.0
SLIDER_MAX_RADIUS_METERS = 500.0
SLIDER_STEP_METERS = 5.0


# ---------------------------------------------------------------------------
# distance
# ---------------------------------------------------------------------------
def _clamp_unit(value: float) -> float:
    """``value`` held inside [0, 1] - the guard the Haversine formula needs. See the module."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 fixes, in metres.

    Pure Python on purpose: this runs before anything else on the punch path, and the
    ``math`` calls cost a few hundred nanoseconds - an order of magnitude less than the
    float parsing that produced the arguments. See the module docstring for why both
    ``sqrt`` arguments are clamped.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi * 0.5) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda * 0.5) ** 2
    )
    a = _clamp_unit(a)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(_clamp_unit(1.0 - a)))
    return EARTH_RADIUS_METERS * c


def within_wgs84(latitude: float, longitude: float) -> bool:
    """Whether a pair is a real WGS84 position: finite, in range, and not the mock reading.

    ``(0, 0)`` is the canonical output of a mock-location provider (see
    ``main.validate_plausible_coordinates``), and a fence at Null Island is not a site, so
    the pair is refused as input rather than accepted as a position.
    """
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return False
    if abs(latitude) > 90.0 or abs(longitude) > 180.0:
        return False
    return not (abs(latitude) < 1e-6 and abs(longitude) < 1e-6)


# ---------------------------------------------------------------------------
# the fence
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Geofence:
    """One fence, as the punch path sees it. Immutable, so a reader cannot see a half-edit."""

    latitude: float
    longitude: float
    radius_meters: float
    updated_at: datetime | None = None
    #: How many edits this process has seen, and where the value came from (``database``,
    #: ``default``, ``legacy`` or ``modern``). Diagnostics only - nothing decides on them.
    generation: int = 0
    source: str = "default"

    def as_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius_meters": self.radius_meters,
            "updated_at": self.updated_at.isoformat(sep=" ") if self.updated_at else None,
            "generation": self.generation,
            "source": self.source,
        }

    def distance_to(self, latitude: float, longitude: float) -> float:
        return haversine_meters(self.latitude, self.longitude, latitude, longitude)


#: The cold-boot fence. ``source`` says so, because "the fence this deployment is using" and
#: "a fence somebody configured" are different answers and an operator should not have to
#: read the database to tell them apart.
DEFAULT_GEOFENCE = Geofence(
    latitude=DEFAULT_LATITUDE,
    longitude=DEFAULT_LONGITUDE,
    radius_meters=DEFAULT_RADIUS_METERS,
    updated_at=None,
    generation=0,
    source="default",
)


class GeofenceCache:
    """The process-local fence the punch reads, with a lock held only by writers.

    The read path is one attribute load of an immutable dataclass - there is nothing to
    tear, so taking a lock on the hot path would buy nothing and cost the latency this
    class exists to avoid. Writers (an administrator's edit, the startup load) take the
    lock, so two concurrent edits cannot interleave into a fence neither of them wrote.
    """

    def __init__(self, initial: Geofence | None = None) -> None:
        self._lock = threading.RLock()
        self._fence = initial or DEFAULT_GEOFENCE
        self._generation = 0

    # -- readers -----------------------------------------------------------
    @property
    def fence(self) -> Geofence:
        """The fence in force, right now. One attribute load; no lock, no I/O."""
        return self._fence

    @property
    def generation(self) -> int:
        return self._generation

    # -- writers -----------------------------------------------------------
    def load(self) -> Geofence:
        """Read the active fence out of SQLite. Cold boot falls back to ``DEFAULT_GEOFENCE``.

        Called at startup (see ``main.lifespan``) and by the tests' reset. Not called on
        the punch path: that is the whole point of the cache.
        """
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT latitude, longitude, radius_meters, updated_at "
                    "FROM geofence_settings ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            # A database that has not been migrated yet, or one being restored. The gate
            # reports that state and refuses to serve; this must not be the thing that
            # crashes the boot, so the default fence is the answer and ``source`` says so.
            row = None
        if row is None:
            return self.store(DEFAULT_GEOFENCE)
        try:
            fence = Geofence(
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                radius_meters=float(row["radius_meters"]),
                updated_at=_parse_stamp(row["updated_at"]),
                generation=self._generation + 1,
                source="database",
            )
        except (TypeError, ValueError, KeyError, IndexError):
            return self.store(DEFAULT_GEOFENCE)
        return self.store(fence)

    def store(self, fence: Geofence) -> Geofence:
        """Put ``fence`` in force for this process, and say what is now in force."""
        with self._lock:
            self._generation += 1
            self._fence = replace(
                fence,
                generation=self._generation,
                updated_at=fence.updated_at or clock.now(),
            )
            return self._fence


def _parse_stamp(value: Any) -> datetime | None:
    """A stored ``updated_at``, or ``None`` when it is not one this build can read."""
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except (TypeError, ValueError):
            continue
    return None


#: The one cache this process has. ``main.lifespan`` publishes it on
#: ``app.state.geofence_cache`` as well, because that is where a handler (and an operator
#: reading the app object) looks for it.
CACHE = GeofenceCache()


# ---------------------------------------------------------------------------
# the sites, cached the same way
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SiteFence:
    """One construction site's boundary, as the punch path sees it.

    A *site* fence is not the deployment fence (``Geofence``) and the two are deliberately
    separate objects: the deployment fence is one boundary an administrator edits, and this
    is a row per site, resolved by name. They are cached by the same rule - read once, held
    in memory, written through on edit - because the punch path must not care which of them
    is answering.

    ``site_id`` is the rowid, which is what a console sends; ``site_name`` is the primary
    key of ``construction_sites``, which is what a client with a site list sends. Both are
    carried so an answer can say which fence decided, either way it was named.
    """

    site_id: int
    site_name: str
    latitude: float
    longitude: float
    radius_meters: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius_meters": self.radius_meters,
        }

    def distance_to(self, latitude: float, longitude: float) -> float:
        return haversine_meters(self.latitude, self.longitude, latitude, longitude)


class SiteCache:
    """Every site's fence, in memory, keyed by rowid and by name.

    WHY THIS EXISTS
    ---------------
    ``main.site_at`` answers "which site contains this fix" with a query - it has to, because
    that question is about *every* site and is asked from paths that already hold a
    connection. The punch endpoint's requirement is the opposite: name the site and decide
    with **zero database queries**. So the same rows are held here, read once at startup and
    refreshed on every write, and the punch reads this instead.

    The two indexes are the point: an id from a console and a name from a mobile client are
    the same question, and a caller that has one should never have to scan for the other.

    Reads take no lock (the maps are replaced wholesale by writers, so a reader sees either
    the old dict or the new one - never a half-built one), and writers hold one, exactly as
    ``GeofenceCache`` does. ``main.site_at`` remains the authority for "where am I" on the
    paths that already have a connection; this is the authority for "is this fix inside the
    site it named" on the path that must not open one.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[int, SiteFence] = {}
        self._by_name: dict[str, SiteFence] = {}
        self._generation = 0

    # -- readers -----------------------------------------------------------
    @property
    def by_id(self) -> dict[int, SiteFence]:
        return self._by_id

    @property
    def by_name(self) -> dict[str, SiteFence]:
        return self._by_name

    @property
    def generation(self) -> int:
        return self._generation

    def resolve(self, site_id: int | None, site_name: str | None) -> SiteFence | None:
        """The fence the punch named, or ``None`` when this deployment has no such site.

        The id is asked about first and the name only if it did not match, so a caller that
        sent both gets the row the *id* names - which is the stronger identifier, and the
        one a console has. A name is compared exactly: a site name is a primary key here,
        and a case-insensitive match would let "tower b" reach "Tower B" while the database
        would have refused to create it.
        """
        if site_id is not None:
            found = self._by_id.get(int(site_id))
            if found is not None:
                return found
        if site_name:
            return self._by_name.get(str(site_name))
        return None

    # -- writers -----------------------------------------------------------
    def load(self) -> "SiteCache":
        """Read every site out of SQLite. Startup, and after a write that changed rows.

        A database that is missing the table (not migrated yet, being restored) leaves the
        cache *empty* rather than fatal: an empty cache refuses a punch that names a site
        with a 404, which is the honest answer, and the deployment fence still answers
        every punch that names none.
        """
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT rowid AS site_id, site_name, lat, lon, radius "
                    "FROM construction_sites"
                ).fetchall()
        except sqlite3.Error:
            rows = []
        fences: list[SiteFence] = []
        for row in rows:
            try:
                fences.append(
                    SiteFence(
                        site_id=int(row["site_id"]),
                        site_name=str(row["site_name"]),
                        latitude=float(row["lat"]),
                        longitude=float(row["lon"]),
                        radius_meters=float(row["radius"]),
                    )
                )
            except (TypeError, ValueError, KeyError, IndexError):
                # A row this build cannot read is skipped rather than fatal, for the same
                # reason a broken fence row is: the other sites are still answerable.
                continue
        return self.replace_all(fences)

    def replace_all(self, fences: list[SiteFence]) -> "SiteCache":
        """Swap the whole cache for ``fences``. Writers only; readers take no lock."""
        with self._lock:
            self._by_id = {fence.site_id: fence for fence in fences}
            self._by_name = {fence.site_name: fence for fence in fences}
            self._generation += 1
            return self

    def store(self, fence: SiteFence) -> SiteFence:
        """Add or replace one site's fence, without a query.

        A name that changed is removed from the name index: a site renamed from "Tower B"
        to "Tower B - phase 2" must stop answering to the old name, or a punch would be
        measured against a fence whose name the site list no longer carries.
        """
        with self._lock:
            stale = self._by_id.get(fence.site_id)
            if stale is not None and stale.site_name != fence.site_name:
                self._by_name.pop(stale.site_name, None)
            self._by_id[fence.site_id] = fence
            self._by_name[fence.site_name] = fence
            self._generation += 1
            return fence

    def forget(self, site_id: int | None = None, site_name: str | None = None) -> None:
        """Drop a deleted site, by id or by name. Both indexes, always."""
        with self._lock:
            found = self._by_id.get(int(site_id)) if site_id is not None else None
            if found is None and site_name:
                found = self._by_name.get(str(site_name))
            if found is None:
                return
            self._by_id.pop(found.site_id, None)
            self._by_name.pop(found.site_name, None)
            self._generation += 1


#: Every site fence this process holds. ``main.lifespan`` publishes it on
#: ``app.state.sites_cache``, which is the name the requirement uses.
SITE_CACHE = SiteCache()


def refresh_cache() -> Geofence:
    """Re-read the fence from SQLite into the cache. Startup, edits, and the test reset."""
    return CACHE.load()


# ---------------------------------------------------------------------------
# validation schemas
# ---------------------------------------------------------------------------
class GeofenceConfig(BaseModel):
    """The modern payload: numbers, and only numbers.

    ``radius_meters`` must be strictly positive - a fence of radius 0 admits nobody, which
    is a site that cannot be clocked into (the same refusal ``main._validate_site_radius``
    makes for a construction site).
    """

    model_config = ConfigDict(extra="ignore")

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    radius_meters: float = Field(..., gt=0.0, le=MAX_RADIUS_METERS)

    @field_validator("latitude", "longitude", "radius_meters")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value

    def to_fence(self, *, source: str) -> Geofence:
        return Geofence(
            latitude=float(self.latitude),
            longitude=float(self.longitude),
            radius_meters=float(self.radius_meters),
            updated_at=clock.now(),
            source=source,
        )


class LegacyGeofenceConfig(BaseModel):
    """The legacy payload: ``lat`` / ``lng`` / ``rad``, as strings, integers or floats.

    The old microservice sent form-encoded numbers, so ``"30.05"``, ``30.05`` and ``30`` are
    all values this has to accept; ``"  31.23  "`` arrives with the whitespace a hand-typed
    or template-rendered request leaves in it. Each is normalised into the modern schema's
    shape (``normalized()``), which is what keeps one row, one cache and one code path for
    both callers.

    Deliberately *not* permissive about meaning: a latitude of 999 is refused rather than
    clamped, because clamping it would put a fence somewhere nobody surveyed while
    answering 200. Numeric *bounds* are clamped where the clamp is a real policy - the
    radius, into (0, ``MAX_RADIUS_METERS``] - and the response says which value was stored.
    """

    model_config = ConfigDict(extra="ignore")

    lat: str | float | int = Field(
        ...,
        validation_alias=AliasChoices("lat", "latitude", "Lat", "LAT"),
    )
    lng: str | float | int = Field(
        ...,
        validation_alias=AliasChoices("lng", "lon", "long", "longitude", "Lng", "LNG"),
    )
    rad: str | float | int | None = Field(
        default=None,
        validation_alias=AliasChoices("rad", "radius", "radius_meters", "Rad", "RAD"),
    )

    @field_validator("lat", "lng", "rad", mode="before")
    @classmethod
    def _number(cls, value: Any, info) -> Any:
        """Accept the string shapes a legacy caller sends; refuse anything else by name."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ValueError("must be a finite number")
            return float(value)
        text = str(value).strip().replace(",", ".")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"must be a number; got {str(value)!r}") from None
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        return number

    def normalized(self) -> GeofenceConfig:
        """The legacy triple, as the modern schema. Raises ``HTTPException(422)`` if unusable."""
        latitude = self.lat
        longitude = self.lng
        if latitude is None or longitude is None:
            raise HTTPException(
                status_code=422,
                detail="Legacy geofence requires lat and lng; neither may be empty.",
            )
        if not within_wgs84(float(latitude), float(longitude)):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Legacy geofence coordinates are outside WGS84 (latitude -90..90, "
                    "longitude -180..180) or are the (0,0) mock-location reading."
                ),
            )
        radius = DEFAULT_RADIUS_METERS if self.rad is None else float(self.rad)
        if radius <= 0:
            raise HTTPException(
                status_code=422,
                detail="Legacy geofence radius must be greater than 0 metres.",
            )
        # The one place a bound is *clamped* rather than refused: a legacy caller sending a
        # radius of 50000 meant "very large", and the largest fence this API will store is a
        # real one - so it is clamped, and the stored value is what the answer reports.
        radius = min(radius, MAX_RADIUS_METERS)
        return GeofenceConfig(
            latitude=float(latitude), longitude=float(longitude), radius_meters=radius
        )


class PunchRequest(BaseModel):
    """A punch as the client sends it.

    ``user_id`` is part of the documented contract and is *checked against the session* by
    the handler, never trusted: identity in a payload is the vulnerability this codebase
    spent a release removing (see the module docstring of ``main``), and the field stays
    only so the shipped mobile client keeps working.

    ``site_id`` / ``site_name`` are the newer half of the same contract: a client that
    knows which site it is standing at names it, and the check is made against *that*
    site's cached fence rather than against the deployment's. They are optional and
    mutually independent - the id is what a console sends, the name is what an older
    client can produce - and a punch that names neither keeps the behaviour it has always
    had, which is the deployment fence this module was built around.
    """

    model_config = ConfigDict(extra="ignore")

    user_id: str
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    accuracy_meters: float | None = Field(default=None, ge=0.0, le=100_000.0)
    #: The site the client believes it is at. Naming a site that does not exist is a 404,
    #: not a silent fall back to the deployment fence: a client with a stale site list
    #: would otherwise be measured against a boundary it did not ask for and told nothing.
    site_id: int | None = Field(default=None, ge=1)
    site_name: str | None = None

    @field_validator("latitude", "longitude")
    @classmethod
    def _usable_fix(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value


class PunchResponse(BaseModel):
    """The answer a punch gets.

    The first three fields are the contract the client reads. ``accuracy_degraded`` is the
    GPS-drift signal: the fix was inside the fence, and its own uncertainty circle still
    overlaps the boundary - so the punch is recorded *and* flagged, because a reading that
    might be on the wrong side of the line is a thing an administrator should be able to
    find later rather than a thing to refuse now.
    """

    status: Literal["approved", "rejected"]
    distance_meters: float
    timestamp: datetime
    radius_meters: float
    accuracy_meters: float | None = None
    accuracy_degraded: bool = False
    verification_ms: float = 0.0
    reason: str | None = None
    #: Which fence answered, when the punch named a site. ``None`` means the deployment
    #: fence, which is what a punch that names no site has always been judged by.
    site_name: str | None = None
    site_id: int | None = None


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    """What the in-memory check decided, and how long it took. No I/O happened here."""

    approved: bool
    distance_meters: float
    radius_meters: float
    reason: str | None
    accuracy_degraded: bool
    verification_ms: float

    @property
    def status(self) -> str:
        return "approved" if self.approved else "rejected"


def evaluate(
    latitude: float,
    longitude: float,
    accuracy_meters: float | None = None,
    site: "SiteFence | None" = None,
) -> Verdict:
    """Decide a punch against a cached fence, in memory, and time itself.

    This is the function the sub-millisecond claim is about: one attribute load off the
    cache, one Haversine, three comparisons. No database, no disk, no model, no network -
    the caller does all of that, and only after this says yes.

    ``site`` is the site the client named, already resolved out of ``SITE_CACHE`` by the
    caller. When it is ``None`` the deployment fence answers - which is what every punch
    that names no site has always been judged by, so the two paths differ in *which cached
    boundary* is read and in nothing else. Both are an attribute load off an immutable
    object, so the latency the requirement is about does not depend on which one it is.

    ``accuracy_degraded`` is set when the fix is inside the fence but its uncertainty
    radius reaches the boundary: ``distance + accuracy > radius``. The punch is approved -
    the fix itself is inside - and flagged, which is the "GPS drift / mock location" case
    the requirements ask to be visible rather than silently accepted.
    """
    started = time.perf_counter()
    fence = site if site is not None else CACHE.fence

    if accuracy_meters is not None and accuracy_meters > MAX_ACCURACY_METERS:
        return Verdict(
            approved=False,
            distance_meters=0.0,
            radius_meters=fence.radius_meters,
            reason="accuracy_degraded_beyond_usable",
            accuracy_degraded=True,
            verification_ms=(time.perf_counter() - started) * 1000.0,
        )

    distance = fence.distance_to(latitude, longitude)
    if distance > fence.radius_meters:
        return Verdict(
            approved=False,
            distance_meters=distance,
            radius_meters=fence.radius_meters,
            reason="outside_geofence",
            accuracy_degraded=False,
            verification_ms=(time.perf_counter() - started) * 1000.0,
        )

    overlap = bool(
        accuracy_meters is not None
        and accuracy_meters > 0.0
        and distance + accuracy_meters > fence.radius_meters
    )
    return Verdict(
        approved=True,
        distance_meters=distance,
        radius_meters=fence.radius_meters,
        reason=None,
        accuracy_degraded=overlap,
        verification_ms=(time.perf_counter() - started) * 1000.0,
    )


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: CurrentUser | None,
    before: Any,
    after: Any,
    request: Request | None,
) -> None:
    """Append the edit to ``audit_log``. Never raises - a fence change is not worth a 500.

    One row per save against the single settings row, with both geometries: the fence is not
    versioned, so the two together are the only record of what moved. The editor is a browser
    console, so the caller's user agent is recorded with the change.
    """
    audit.record(
        conn,
        action=action,
        actor=actor,
        entity="geofence_settings",
        entity_id="1",
        before=before,
        after=after,
        request=request,
        user_agent=True,
        created_at=clock.now().strftime(clock.TS_FORMAT),
    )


def store_site_fence(
    *,
    site_id: int,
    site_name: str,
    latitude: float,
    longitude: float,
    radius_meters: float,
) -> "SiteFence":
    """Put one site's boundary into the cache, from the row that was just written.

    Called by ``sites.store_site`` inside the request that wrote SQLite, so the punch that
    follows reads the new fence rather than the one this process loaded at startup - the
    same write-through rule ``store_geofence`` follows for the deployment fence. The window
    columns are *not* re-read here: the caller has just written them, and a second query
    inside the write lock would be a query for a value already in hand.
    """
    return SITE_CACHE.store(
        SiteFence(
            site_id=int(site_id),
            site_name=str(site_name),
            latitude=float(latitude),
            longitude=float(longitude),
            radius_meters=float(radius_meters),
        )
    )


def refresh_site_cache() -> "SiteCache":
    """Re-read every site fence from SQLite into the cache.

    Startup, the sites API's writes, and the console's own three site routes (add, edit,
    delete) - which write ``construction_sites`` directly and must therefore refresh this
    too, or a fence an administrator moved on the console's Sites screen would keep
    answering punches with the boundary it had before.
    """
    return SITE_CACHE.load()


def store_geofence(
    config: GeofenceConfig,
    *,
    source: str,
    actor: CurrentUser | None = None,
    request: Request | None = None,
) -> Geofence:
    """Write the fence to SQLite, put it in force, and return what is now in force.

    Both writes happen inside this call, in this order, so a request that returns 200 has a
    fence in the database *and* in the cache the next punch will read. The insert is a new
    row rather than an update of the old one: the table keeps the history of a site's
    boundary, which is the thing an administrator asking "when did this fence move?" is
    looking for, and ``ORDER BY id DESC LIMIT 1`` is the active row.
    """
    before = CACHE.fence.as_dict()
    stamp = clock.now()
    with db(write=True) as conn:
        conn.execute(
            "INSERT INTO geofence_settings (latitude, longitude, radius_meters, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                float(config.latitude),
                float(config.longitude),
                float(config.radius_meters),
                stamp.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        _audit(
            conn,
            action="geofence_update",
            actor=actor,
            before=before,
            after={
                "latitude": float(config.latitude),
                "longitude": float(config.longitude),
                "radius_meters": float(config.radius_meters),
                "source": source,
            },
            request=request,
        )
    return CACHE.store(
        Geofence(
            latitude=float(config.latitude),
            longitude=float(config.longitude),
            radius_meters=float(config.radius_meters),
            updated_at=stamp,
            source=source,
        )
    )


def record_punch(
    *,
    user_id: str,
    latitude: float,
    longitude: float,
    accuracy_meters: float | None,
    verdict: Verdict,
    stamp: datetime,
) -> int | None:
    """Log a verified punch. Returns the new row's id, or ``None`` if the write failed.

    Only approved punches reach here: a refusal is answered before any write (that is the
    fail-fast rule), so the ledger is "the punches that were verified", which is what the
    ``status`` column records. A write that fails is logged and survived - the worker is
    standing at a gate and has already been verified, so losing the row must not turn their
    clock-in into an error; the trace is in the log line.
    """
    try:
        with db(write=True) as conn:
            cursor = conn.execute(
                "INSERT INTO attendance_punches "
                "(user_id, latitude, longitude, distance_meters, accuracy_meters, status, "
                "accuracy_degraded, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(user_id),
                    float(latitude),
                    float(longitude),
                    float(verdict.distance_meters),
                    None if accuracy_meters is None else float(accuracy_meters),
                    verdict.status,
                    1 if verdict.accuracy_degraded else 0,
                    stamp.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except sqlite3.Error as exc:  # pragma: no cover - a broken database, not a broken punch
        logging.getLogger("attendance.api").warning("geofence punch not recorded: %s", exc)
        return None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/geofence")
async def read_geofence(current: CurrentUser = Depends(admin_only)):
    """The active fence, straight off the cache - no database read.

    Administrator-only, on purpose. The audience matrix (``tests/test_role_audience.py``)
    derives a path's audience from its *shape* and is deliberately method-blind, so a path
    that is readable by everybody and writable by one role cannot be expressed there
    without weakening the check that catches ``any_authenticated`` standing in for
    ``admin_only``. Rather than widen that matrix, the whole ``/geofence`` path is the
    administrator's - which is also the honest description: this endpoint exists for the
    editor, and a worker's phone learns its own standing from the *punch* answer
    (``distance_meters`` and ``radius_meters``), not from reading the deployment's fence.
    """
    return {"geofence": CACHE.fence.as_dict(), "slider": _slider_bounds()}


@router.post("/geofence")
async def update_geofence(
    payload: GeofenceConfig,
    request: Request,
    current: CurrentUser = Depends(admin_only),
):
    """Replace the fence: SQLite first, then the cache the punch reads. Administrator only.

    The write is the same call the legacy endpoint makes (``store_geofence``), so a fence set
    here and one set through ``/api/legacy/set-geofence`` are the same row, the same cache
    entry and the same distance for the next punch.
    """
    if not within_wgs84(payload.latitude, payload.longitude):
        raise HTTPException(
            status_code=422,
            detail="Coordinates must be a real WGS84 position, not the (0,0) mock reading.",
        )
    fence = store_geofence(payload, source="modern", actor=current, request=request)
    return {"geofence": fence.as_dict(), "slider": _slider_bounds()}


@router.post("/legacy/set-geofence")
async def set_geofence_legacy(
    payload: LegacyGeofenceConfig,
    request: Request,
    current: CurrentUser = Depends(admin_only),
):
    """The old microservice's endpoint, kept working: ``lat`` / ``lng`` / ``rad`` as strings.

    It normalises into the modern schema and then takes the *same* path as
    ``POST /api/v1/geofence`` - one row, one cache, one code path - so a legacy caller and
    the console cannot end up holding two different fences. The answer carries both shapes
    (``geofence`` and ``legacy``) because a legacy caller is reading it, and because the
    normalised values are what actually got stored.
    """
    config = payload.normalized()
    fence = store_geofence(config, source="legacy", actor=current, request=request)
    return {
        "geofence": fence.as_dict(),
        "legacy": {
            "lat": str(fence.latitude),
            "lng": str(fence.longitude),
            "rad": str(fence.radius_meters),
        },
        "normalized": True,
        "slider": _slider_bounds(),
    }


@router.post("/attendance/punch")
async def punch(
    payload: PunchRequest,
    request: Request,
    current: CurrentUser = Depends(any_authenticated),
):
    """Verify a fix against the cached fence, then (and only then) record the punch.

    The order is the requirement: **cache, then arithmetic, then refusal** - and only a fix
    that passes all three reaches SQLite. ``verification_ms`` in the answer is the measured
    cost of that first half, so the claim is checkable from the client rather than asserted
    in a docstring.

    The boundary is the one the punch *names*: ``site_id`` (or ``site_name``) selects that
    site's cached fence, and a punch that names neither is judged by the deployment fence
    as before. Both are looked up in memory, so the site lookup is part of the same
    zero-query half as the arithmetic - see ``SITE_CACHE``.

    Refusals are answered with the same body shape as an approval (``PunchResponse`` with
    ``status: "rejected"``) and a 4xx status, so the phone has one parser: 400 for a fix
    whose own accuracy makes it unusable, 403 for one outside the fence, 404 for a site
    this deployment does not have.
    """
    # Identity is the session's, and the body's ``user_id`` may only agree with it. This is
    # the same rule ``main.verify_worker`` applies to its form field: the field survives so
    # the shipped client keeps working, and it cannot name anybody else.
    if str(payload.user_id) != current.id:
        raise HTTPException(
            status_code=403, detail="You may only record attendance for your own account."
        )
    if current.is_developer:
        # The root tier owns the deployment, not a rota (see ``main.verify_worker``).
        raise HTTPException(status_code=403, detail="The developer account does not check in.")
    if not within_wgs84(payload.latitude, payload.longitude):
        raise HTTPException(
            status_code=400,
            detail=(
                "Location coordinates are not a usable WGS84 fix (out of range, not finite, "
                "or the (0,0) mock-location reading)."
            ),
        )

    # The site the client named, out of the cache. No query: this is the same "read the
    # boundary the process already holds" rule the deployment fence follows.
    site: SiteFence | None = None
    if payload.site_id is not None or payload.site_name:
        site = SITE_CACHE.resolve(payload.site_id, payload.site_name)
        if site is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No site with that id or name. A punch names the site it is standing "
                    "at, and a fence this deployment does not have cannot be measured "
                    "against."
                ),
            )

    # ---- the zero-latency half: cache + arithmetic, nothing else --------------------
    verdict = evaluate(payload.latitude, payload.longitude, payload.accuracy_meters, site)
    stamp = clock.now()

    if not verdict.approved:
        status_code = 403 if verdict.reason == "outside_geofence" else 400
        body = PunchResponse(
            status="rejected",
            distance_meters=round(verdict.distance_meters, 3),
            timestamp=stamp,
            radius_meters=verdict.radius_meters,
            accuracy_meters=payload.accuracy_meters,
            accuracy_degraded=verdict.accuracy_degraded,
            verification_ms=round(verdict.verification_ms, 4),
            reason=verdict.reason,
            site_name=site.site_name if site else None,
            site_id=site.site_id if site else None,
        )
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    # ---- the verified half: the write, and the drift alert ---------------------------
    row_id = record_punch(
        user_id=current.id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy_meters=payload.accuracy_meters,
        verdict=verdict,
        stamp=stamp,
    )
    if verdict.accuracy_degraded:
        _raise_drift_alert(
            current=current,
            payload=payload,
            verdict=verdict,
            stamp=stamp,
        )
    body = PunchResponse(
        status="approved",
        distance_meters=round(verdict.distance_meters, 3),
        timestamp=stamp,
        radius_meters=verdict.radius_meters,
        accuracy_meters=payload.accuracy_meters,
        accuracy_degraded=verdict.accuracy_degraded,
        verification_ms=round(verdict.verification_ms, 4),
        site_name=site.site_name if site else None,
        site_id=site.site_id if site else None,
    )
    answer = JSONResponse(status_code=200, content=body.model_dump(mode="json"))
    if row_id is not None:
        answer.headers["X-Punch-Id"] = str(row_id)
    return answer


def _raise_drift_alert(
    *,
    current: CurrentUser,
    payload: PunchRequest,
    verdict: Verdict,
    stamp: datetime,
) -> None:
    """File the GPS-drift notice an administrator reads. Never raises, never blocks.

    A dedupe key of (account, day) so a worker standing at the boundary all morning leaves
    one row rather than forty - the same exactly-once shape ``notifications.notify`` gives
    the overtime watcher, used here because a drift flag is an observation about a person,
    not forty separate events.
    """
    with db(write=True) as conn:
        notifications.notify(
            conn,
            kind="geofence_accuracy_drift",
            severity=notifications.SEVERITY_WARNING,
            worker_id=current.id,
            site_name="Geofence",
            title="GPS accuracy overlaps the geofence boundary",
            body=(
                f"{current.name or current.id} punched in at {round(verdict.distance_meters, 1)} m "
                f"from the fence centre with a ±{payload.accuracy_meters} m fix, so the reading "
                "may be on either side of the boundary. Verify against the punch photograph "
                "before treating the location as settled."
            ),
            payload={
                "user_id": current.id,
                "latitude": payload.latitude,
                "longitude": payload.longitude,
                "distance_meters": round(verdict.distance_meters, 3),
                "accuracy_meters": payload.accuracy_meters,
                "radius_meters": verdict.radius_meters,
                "at": stamp.isoformat(sep=" "),
            },
            dedupe_key=f"geofence_drift:{current.id}:{stamp.strftime('%Y-%m-%d')}",
        )


def _slider_bounds() -> dict[str, float]:
    """The console's slider range, so the page and the API cannot disagree about it."""
    return {
        "min_meters": SLIDER_MIN_RADIUS_METERS,
        "max_meters": SLIDER_MAX_RADIUS_METERS,
        "step_meters": SLIDER_STEP_METERS,
    }


#: The pages that draw a map, and the only ones allowed to have the map origins. Named here
#: rather than in ``netguard`` because this module owns the feature: an operator reading
#: ``netguard.CSP_HTML_MAPS`` follows this function back to the pages it exists for.
#:
#: Two pages, and they are the same exception twice for the same reason. ``admin_geofence``
#: draws the deployment's fence; ``admin_add_site`` draws a site being created. Both are
#: administrator screens reached from the console, both draw a draggable pin and a radius
#: circle on OpenStreetMap tiles, and neither is a page a worker's phone loads - so the
#: strict baseline still covers the punch screen, the link pages and the enrollment page.
MAP_PAGES: frozenset[str] = frozenset({"admin_geofence.html", "admin_add_site.html"})

#: The one page, kept as a name because ``tests/test_geofence.py`` and any reader looking
#: for "where does the map policy come from" both want the original.
MAP_PAGE = "admin_geofence.html"


def map_policy_for(page: str) -> str | None:
    """The relaxed CSP for a page that draws a map, or ``None`` for every other page."""
    import netguard

    return netguard.CSP_HTML_MAPS if page in MAP_PAGES else None


def map_sources() -> dict[str, Any]:
    """The three origins the editor may load from, for the page to check itself against."""
    import netguard

    policy = netguard.CSP_HTML_MAPS
    return {
        "leaflet": "https://unpkg.com",
        "tiles": "https://tile.openstreetmap.org",
        "google": "https://maps.googleapis.com",
        "policy": policy,
    }
