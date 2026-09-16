"""Response-surface guard: an anonymous caller must never see server internals.

The suite sends **no credentials at all** to every unauthenticated API route the
app exposes, then scans the response body for two classes of leak:

* a **filesystem path** - the answer tells the caller where the server keeps its
  files (``./local_references/1.json``, ``C:\\Users\\pc\\times.db``,
  ``/home/app/backend/site-packages/...``);
* a **secret-shaped value** - an API key, bearer/basic credential, JWT, private
  key block, a ``scheme://user:password@host`` URI, or a ``password: ...`` style
  assignment.

Three design choices are load-bearing:

* The walk is driven by the app's routing table plus its OpenAPI document, so a
  route added tomorrow is covered here without editing this file. A route is
  skipped only when the app's own metadata proves it needs credentials, and the
  coverage assertion fails if any route is left unaccounted for.
* ``main.py`` builds the DeepFace model and creates ``times.db`` at *import*
  time, and resolves ``./temp`` / ``./local_references`` relative to the working
  directory at *request* time. The model is therefore replaced with a
  deterministic stub, and the working directory is pinned to a scratch dir for
  the whole walk, so the suite is offline, fast, and cannot touch real worker
  references.
* Routes that need specific values before their handler produces a body of its
  own are listed in ``DEEP_PAYLOADS``; the registry is checked against the real
  routing table so a rename cannot quietly reduce coverage.

Run it with ``python -m pytest test_api_surface_leaks.py``.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import sys
import types
from dataclasses import dataclass
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase
from fastapi.testclient import TestClient

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

#: Modules that may hold the FastAPI app, current layout first. The prototype is
#: a flat ``main.py``; the restructured backend lives in ``backend/main.py``.
APP_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("main", "app"),
    ("backend.main", "app"),
)

#: Deterministic embedding the stubbed model returns. Its value only matters in
#: that it must match ``./local_references/1.json`` so the handler's success
#: branch is reachable.
_EMBEDDING = [0.5] * 8

#: Routes whose handler only produces *its own* response body once the request
#: is semantically valid. ``/api/v1/attendance/verify`` rejects an empty request
#: at the GPS geofence, so a caller inside "Downtown Tower A" is needed to get
#: past the 400s and see what the handler actually returns.
DEEP_PAYLOADS: dict[tuple[str, str], dict[str, str]] = {
    ("POST", "/api/v1/attendance/verify"): {
        "worker_id": "1",
        "action": "Clock In",
        "latitude": "30.050010",  # inside the "Downtown Tower A" radius
        "longitude": "31.230010",
    },
}

#: Variants sent per documented route. The empty request is what a scanner or a
#: bare ``curl`` sends; the populated one fills every required parameter so the
#: handler - not just the validator - produces the body.
EMPTY_VARIANT = "unauthenticated-empty"
POPULATED_VARIANT = "unauthenticated-populated"
VARIANTS = (EMPTY_VARIANT, POPULATED_VARIANT)

# FastAPI's UI endpoints (``/docs``, ``/redoc``, ``/docs/oauth2-redirect``) and
# ``/openapi.json`` are plain Starlette ``Route``\\ s in every FastAPI version, so
# they never enter the walk: they serve framework HTML, not app data.


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #

FILESYSTEM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windows_drive_path", re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/][^\s\"'<>|,]*)")),
    ("unc_share_path", re.compile(r"\\\\[A-Za-z0-9_.\-]+\\[A-Za-z0-9_.$\-]+")),
    (
        "home_or_server_root",
        re.compile(
            r"/(?:home|Users|root|tmp|var|usr|opt|srv|mnt|etc|private|proc|sys|media|workspace)/[\w.\-]+"
        ),
    ),
    ("python_environment", re.compile(r"(?:site-packages|dist-packages)")),
    ("relative_path", re.compile(r"(?<![\w./])\.{1,2}[\\/][\w.\-]+")),
    (
        "disk_file_reference",
        re.compile(
            r"(?<![\w./:\\-])[\w\-]+[\\/][\w.\-]*\.(?:json|jsonl|jpe?g|png|gif|webp|bmp|db"
            r"|sqlite3?|pem|crt|key|p12|pfx|h5|pkl|joblib|onnx|tflite|log|env|ini|cfg|conf"
            r"|ya?ml|toml|py|pyc|so|dll|sh|bat)\b"
        ),
    ),
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|ANPA|ANVA|ASCA|AGPA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("twilio_key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("uri_with_credentials", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@\"']+:[^\s:/@\"']+@")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("basic_auth_value", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)[\"']?(?:api[_-]?key|apikey|secret|client[_-]?secret|access[_-]?key"
            r"|private[_-]?key|password|passwd|token|credential)[\"']?\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9_\-+/=.]{12,}"
        ),
    ),
)


@dataclass(frozen=True)
class Leak:
    kind: str  # "filesystem_path" | "secret"
    name: str  # which pattern fired
    sample: str  # redacted match, safe to print into CI logs


def _redact(fragment: str) -> str:
    """Keep a match recognisable without copying the whole value into the log."""

    if len(fragment) <= 12:
        return fragment
    return f"{fragment[:6]}...{fragment[-3:]}"


def scan_body(body: str) -> list[Leak]:
    """Return every path/secret-shaped fragment in a response body."""

    leaks: list[Leak] = []
    for name, pattern in FILESYSTEM_PATTERNS:
        leaks += [Leak("filesystem_path", name, _redact(m.group(0))) for m in pattern.finditer(body)]
    for name, pattern in SECRET_PATTERNS:
        leaks += [Leak("secret", name, _redact(m.group(0))) for m in pattern.finditer(body)]
    return leaks


# --------------------------------------------------------------------------- #
# App discovery and fixture (offline, isolated working directory)
# --------------------------------------------------------------------------- #


class _StubDeepFace:
    """Replaces the real model.

    ``main.py`` calls ``DeepFace.build_model("VGG-Face")`` at import time, which
    downloads several hundred megabytes of weights. This suite is about response
    bodies, not face matching, so the model is stubbed and ``represent`` returns
    one face whose embedding matches the scratch reference file.
    """

    def build_model(self, *args: Any, **kwargs: Any) -> None:
        return None

    def represent(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"embedding": list(_EMBEDDING)}]


def _install_deepface_stub() -> None:
    stub = types.ModuleType("deepface")
    stub.DeepFace = _StubDeepFace()  # type: ignore[attr-defined]
    sys.modules["deepface"] = stub


def _load_app() -> tuple[FastAPI, str]:
    for extra in (REPO_ROOT, os.path.join(REPO_ROOT, "backend")):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    failures: list[str] = []
    for module_path, attribute in APP_CANDIDATES:
        sys.modules.pop(module_path, None)
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - reported below
            failures.append(f"{module_path}: {type(exc).__name__}: {exc}")
            continue
        app = getattr(module, attribute, None)
        if isinstance(app, FastAPI):
            return app, f"{module_path}:{attribute}"
        failures.append(f"{module_path}: no FastAPI instance at '{attribute}'")

    raise RuntimeError(
        "Could not import a FastAPI app for this repo. Tried "
        + ", ".join(f"{mod}:{attr}" for mod, attr in APP_CANDIDATES)
        + ".\n"
        + "\n".join(failures)
    )


@dataclass(frozen=True)
class Session:
    app: FastAPI
    target: str
    client: TestClient


@pytest.fixture()
def api(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Session:
    """App + anonymous client, rooted in a scratch directory.

    ``chdir`` is what keeps the suite honest about the app's relative paths:
    ``times.db``, ``./temp`` and ``./local_references`` all resolve inside
    ``tmp_path``, so no real worker photo or embedding is read or deleted.
    """

    _install_deepface_stub()
    monkeypatch.chdir(tmp_path)
    app, target = _load_app()

    references = os.path.join(tmp_path, "local_references")
    os.makedirs(references, exist_ok=True)
    with open(os.path.join(references, "1.json"), "w", encoding="utf-8") as handle:
        json.dump(_EMBEDDING, handle)

    # raise_server_exceptions=False turns an unhandled exception into the 500 the
    # caller would really receive, so its body is scanned instead of aborting the
    # walk.
    client = TestClient(app, raise_server_exceptions=False)
    return Session(app=app, target=target, client=client)


# --------------------------------------------------------------------------- #
# Route discovery
# --------------------------------------------------------------------------- #


def _iter_api_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Flatten the routing table into ``(full_path, APIRoute)`` pairs.

    FastAPI versions differ: older ones copy every route into ``app.routes``,
    while 0.14x keeps an included router behind a wrapper route that only
    exposes it as ``original_router`` / ``include_context.included_router``.
    Both shapes are handled, and nested ``Mount``\\ s recurse through ``.routes``.
    """

    for route in routes or ():
        children = getattr(route, "routes", None)
        if children:
            nested = getattr(route, "prefix", "") or getattr(route, "path", "") or ""
            yield from _iter_api_routes(children, prefix + nested)
            continue

        included = getattr(route, "original_router", None)
        if included is None:
            included = getattr(getattr(route, "include_context", None), "included_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            include_prefix = getattr(context, "prefix", "") or getattr(included, "prefix", "") or ""
            yield from _iter_api_routes(getattr(included, "routes", ()), prefix + include_prefix)
            continue

        if isinstance(route, APIRoute):
            yield prefix + route.path, route


def _openapi(app: FastAPI) -> dict[str, Any]:
    return app.openapi() or {}


def _has_security_dependency(dependant: Any) -> bool:
    stack = list(getattr(dependant, "dependencies", ()) or ())
    while stack:
        dependency = stack.pop()
        if isinstance(getattr(dependency, "call", None), SecurityBase):
            return True
        stack.extend(getattr(dependency, "dependencies", ()) or ())
    return False


def _declares_auth(app: FastAPI, route: APIRoute, path: str, method: str) -> bool:
    """True only when the app's own metadata proves credentials are required.

    Deliberately structural: an OpenAPI ``security`` entry (top-level or per
    operation), or a ``fastapi.security`` primitive in the dependency tree.
    Guessing from function names would silently drop routes from the walk, and a
    route that does require credentials still returns a scannable 401 body, so
    the bias is toward walking too much rather than too little.
    """

    schema = _openapi(app)
    operation = ((schema.get("paths") or {}).get(path) or {}).get(method.lower())
    if isinstance(operation, dict) and "security" in operation:
        if operation["security"]:
            return True
    elif schema.get("security"):
        return True

    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    return bool(getattr(dependant, "security_requirements", None)) or _has_security_dependency(dependant)


@dataclass(frozen=True)
class Endpoint:
    path: str
    method: str
    operation: dict[str, Any] | None  # OpenAPI operation, None when undocumented
    authenticated: bool

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


def discover_endpoints(app: FastAPI) -> list[Endpoint]:
    """Every API route the app serves, with its OpenAPI operation if documented."""

    openapi = _openapi(app)
    endpoints: list[Endpoint] = []
    for path, route in _iter_api_routes(app.router.routes):
        for method in sorted(route.methods or ()):
            operation = ((openapi.get("paths") or {}).get(path) or {}).get(method.lower())
            endpoints.append(
                Endpoint(
                    path=path,
                    method=method,
                    operation=operation if isinstance(operation, dict) else None,
                    authenticated=_declares_auth(app, route, path, method),
                )
            )
    return sorted(endpoints, key=lambda endpoint: (endpoint.path, endpoint.method))


def _public_endpoints(app: FastAPI) -> list[Endpoint]:
    return [endpoint for endpoint in discover_endpoints(app) if not endpoint.authenticated]


def _describe(session: Session) -> str:
    endpoints = discover_endpoints(session.app)
    return (
        f"app {session.target}: {len(endpoints)} API routes, "
        f"{len(_public_endpoints(session.app))} unauthenticated, "
        f"{len(endpoints) - len(_public_endpoints(session.app))} authenticated"
    )


# --------------------------------------------------------------------------- #
# Request synthesis
# --------------------------------------------------------------------------- #

_JPEG: bytes | None = None


def _tiny_jpeg() -> bytes:
    global _JPEG
    if _JPEG is None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), (127, 127, 127)).save(buffer, format="JPEG")
        _JPEG = buffer.getvalue()
    return _JPEG


def _resolve(schema: Any, openapi: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict) or "$ref" not in schema:
        return schema if isinstance(schema, dict) else {}
    node: Any = openapi
    for part in str(schema["$ref"]).lstrip("#/").split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


#: How OpenAPI spells "this string is an uploaded file". FastAPI switched from
#: ``format: binary`` to ``contentMediaType`` at some point, so both are checked.
UPLOAD_FORMATS = ("binary", "base64")
UPLOAD_MEDIA_TYPES = (
    "application/octet-stream",
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
)


def _is_upload(schema: dict[str, Any]) -> bool:
    return schema.get("format") in UPLOAD_FORMATS or schema.get("contentMediaType") in UPLOAD_MEDIA_TYPES


def _sample(schema: Any, openapi: dict[str, Any], depth: int = 0) -> Any:
    """A plausible value for a schema fragment: enough to clear validation."""

    if depth > 8 or not isinstance(schema, dict):
        return "1"
    schema = _resolve(schema, openapi)

    for combinator in ("anyOf", "oneOf", "allOf"):
        options = [
            option
            for option in schema.get(combinator) or ()
            if _resolve(option, openapi).get("type") != "null"
        ]
        if options:
            return _sample(options[0], openapi, depth + 1)

    if schema.get("enum"):
        return schema["enum"][0]
    if "example" in schema:
        return schema["example"]

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "string")

    if schema_type == "string":
        return _tiny_jpeg() if _is_upload(schema) else "1"
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        return []
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {
            name: _sample(properties.get(name, {}), openapi, depth + 1)
            for name in schema.get("required") or ()
        }
    return "1"


def _path_and_query(
    operation: dict[str, Any], route_path: str, openapi: dict[str, Any]
) -> tuple[str, dict[str, str]]:
    path = route_path
    query: dict[str, str] = {}
    for parameter in operation.get("parameters") or ():
        if not parameter.get("required"):
            continue
        value = _sample(parameter.get("schema") or {}, openapi)
        placeholder = "{" + str(parameter.get("name")) + "}"
        if placeholder in path:
            path = path.replace(placeholder, str(value))
        else:
            query[str(parameter.get("name"))] = str(value)
    return path, query


def _body(
    operation: dict[str, Any],
    openapi: dict[str, Any],
    override: dict[str, str] | None,
    populate: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    if not populate:
        return {}, {}, None

    request_body = operation.get("requestBody") or {}
    content = request_body.get("content") or {}
    if not request_body.get("required") or not content:
        return {}, {}, None

    if "application/json" in content:
        schema = _resolve(content["application/json"].get("schema") or {}, openapi)
        payload = _sample(schema, openapi)
        if not isinstance(payload, dict):
            payload = {}
        payload.update(override or {})
        return {}, {}, payload

    # Form or multipart: files must go through ``files=`` to become an upload.
    schema = _resolve(next(iter(content.values())).get("schema") or {}, openapi)
    properties = schema.get("properties") or {}
    data: dict[str, Any] = {}
    files: dict[str, Any] = {}
    for name in schema.get("required") or ():
        value = _sample(properties.get(name, {}), openapi)
        if isinstance(value, (bytes, bytearray)):
            files[name] = ("probe.jpg", io.BytesIO(bytes(value)), "image/jpeg")
        else:
            data[name] = value
    data.update(override or {})
    return data, files, None


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Probe:
    path: str
    method: str
    variant: str
    status_code: int
    content_type: str
    body: str

    @property
    def label(self) -> str:
        return f"{self.method} {self.path} [{self.variant}]"


def walk_public_routes(session: Session) -> Iterator[Probe]:
    """Send credential-free requests to every unauthenticated API route.

    A documented route is hit twice: empty, then populated. An undocumented one
    (``include_in_schema=False``) has no schema to synthesise from, so it is hit
    empty - still enough to see whether the handler's response leaks.
    """

    openapi = _openapi(session.app)
    for endpoint in _public_endpoints(session.app):
        operation = endpoint.operation or {}
        variants = VARIANTS if endpoint.operation is not None else (EMPTY_VARIANT,)
        for variant in variants:
            populated = variant == POPULATED_VARIANT
            override = DEEP_PAYLOADS.get((endpoint.method, endpoint.path)) if populated else None
            path, query = _path_and_query(operation, endpoint.path, openapi)
            data, files, json_body = _body(operation, openapi, override, populate=populated)
            response = session.client.request(
                endpoint.method,
                path,
                params=query or None,
                data=data or None,
                files=files or None,
                json=json_body,
            )
            yield Probe(
                path=endpoint.path,
                method=endpoint.method,
                variant=variant,
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                body=response.text,
            )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

KNOWN_LEAKS: tuple[tuple[str, str], ...] = (
    ('{"detail": "Reference data for worker 1 not found at ./local_references/1.json"}', "filesystem_path"),
    (r'{"error": "open failed: C:\\Users\\pc\\real world project odoo\\times.db"}', "filesystem_path"),
    ('{"trace": "no such file: /home/app/backend/uploads/selfie.jpg"}', "filesystem_path"),
    ('{"detail": "/usr/local/lib/python3.12/site-packages/deepface/weights/vgg_face_weights.h5"}', "filesystem_path"),
    ('{"detail": "cleanup failed for temp/9f1c9a3e.jpg"}', "filesystem_path"),
    ('{"api_key": "AKIAIOSFODNN7EXAMPLE"}', "secret"),
    ('{"access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"}', "secret"),
    ('{"Authorization": "Bearer sk-proj-abcdefghijklmnopqrstuvwx"}', "secret"),
    ('{"database_url": "postgresql://odoo:hunter2@db.internal:5432/attendance"}', "secret"),
    ('{"private_key": "-----BEGIN RSA PRIVATE KEY-----"}', "secret"),
    ('{"password": "correct-horse-battery"}', "secret"),
)

BENIGN_BODIES: tuple[str, ...] = (
    '{"detail": "GPS coordinates are missing."}',
    '{"detail": "GPS coordinates must be numbers."}',
    '{"detail": "Location Rejected. You are not within the radius of any authorized construction site."}',
    '{"detail": "Face verification failed. Score: 0.61"}',
    '{"detail": "Internal processing error."}',
    '{"detail": "Reference data for worker 1 not found. HR needs to enroll them."}',
    '{"status": "success", "message": "Auto-Approved (Clocked In at Downtown Tower A)", '
    '"score": 0.0, "hours": 0.0, "site": "Downtown Tower A"}',
    '{"detail": [{"type": "missing", "loc": ["body", "selfie"], "msg": "Field required", "input": null}]}',
    '{"openapi": "3.1.0", "paths": {"/api/v1/attendance/verify": {"post": {"summary": "Verify Worker"}}}}',
    '{"detail": "See https://example.com/docs/api-keys.html"}',
    '{"detail": "worker must Clock In before Clocking Out"}',
)


@pytest.mark.parametrize(("body", "expected_kind"), KNOWN_LEAKS)
def test_detectors_flag_known_leaks(body: str, expected_kind: str) -> None:
    found = scan_body(body)
    assert found, f"scanner missed a known {expected_kind}: {body}"
    assert expected_kind in {leak.kind for leak in found}, f"wrong class for {body}: {found}"


@pytest.mark.parametrize("body", BENIGN_BODIES)
def test_detectors_ignore_benign_bodies(body: str) -> None:
    assert not scan_body(body), f"false positive on a benign body: {body}"


def test_routing_table_and_openapi_agree(api: Session) -> None:
    """Every documented operation must also be routable, so the walk can reach it."""

    documented = {
        (method.upper(), path)
        for path, operations in (_openapi(api.app).get("paths") or {}).items()
        for method in operations
    }
    discovered = {(endpoint.method, endpoint.path) for endpoint in discover_endpoints(api.app)}

    assert documented, f"app documents no operations to walk; {_describe(api)}"
    assert discovered, f"no API routes found in the routing table; {_describe(api)}"
    assert documented <= discovered, (
        f"documented but not routable: {sorted(documented - discovered)}; {_describe(api)}"
    )


def test_deep_payloads_still_match_real_routes(api: Session) -> None:
    """Guards the deep-payload registry against rot after a rename."""

    discovered = {(endpoint.method, endpoint.path) for endpoint in discover_endpoints(api.app)}
    stale = sorted(key for key in DEEP_PAYLOADS if key not in discovered)
    assert not stale, f"deep payloads no longer match a route: {stale}; {_describe(api)}"


def test_walks_every_unauthenticated_route_without_leaking_paths_or_secrets(api: Session) -> None:
    probes = list(walk_public_routes(api))

    # Coverage first: a walk that quietly skipped routes would pass vacuously.
    expected = {(endpoint.method, endpoint.path) for endpoint in _public_endpoints(api.app)}
    walked = {(probe.method, probe.path) for probe in probes}
    assert expected, f"no unauthenticated routes discovered; {_describe(api)}"
    assert walked == expected, (
        f"walk did not cover every unauthenticated route ({_describe(api)}).\n"
        f"never requested: {sorted(expected - walked)}"
    )
    assert len(probes) >= len(expected)

    findings: list[str] = []
    for probe in probes:
        for leak in sorted(set(scan_body(probe.body)), key=lambda item: (item.kind, item.name)):
            findings.append(
                f"{probe.label} -> HTTP {probe.status_code} ({probe.content_type}): "
                f"{leak.kind} matched {leak.name}: {leak.sample!r}"
            )

    assert not findings, (
        "unauthenticated responses exposed server internals "
        f"({_describe(api)}, {len(probes)} requests):\n" + "\n".join(findings)
    )
