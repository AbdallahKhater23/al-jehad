"""Authentication and authorization primitives.

Three decisions worth stating, because each replaces a shipped vulnerability:

* **Algorithm is pinned** (``algorithms=[settings.jwt_algorithm]``). Reading the
  algorithm from the token header is what makes ``alg: none`` and key-confusion
  forgeries work.
* **Identity and role are re-read from the database on every request**, never
  trusted from the token body alone. That makes a deleted user lose access
  immediately, makes a role demotion take effect on the next call, and removes
  the per-process password cache that silently kept rotated credentials alive on
  other workers.
* **``token_version``** is compared against the value in the token, so changing a
  password revokes every outstanding token for that user.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext

from config import settings
from database import db

pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=12, deprecated="auto")

#: bcrypt hashes at most 72 bytes. The old code truncated silently, which meant
#: two different long passwords could be the same credential.
BCRYPT_MAX_BYTES = 72

#: Passwords that must never be accepted on a real deployment.
WEAK_PASSWORDS = frozenset({"password", "passw0rd", "12345678", "123456789", "password1", "admin", "testpassword", "changeme", "qwertyuiop"})

# ``auto_error=False`` because HTTPBearer's default for a *missing* header is 403,
# and an unauthenticated caller must get 401.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    name: str
    role: str
    token_version: int

    @property
    def is_admin(self) -> bool:
        return self.role in {"admin", "head_admin"}


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def _truncate_for_bcrypt(password: str) -> str:
    raw = password.encode("utf-8")
    return raw[:BCRYPT_MAX_BYTES].decode("utf-8", errors="ignore")


#: The id ranges each role occupies. Part of the data model, not a rule of any one
#: endpoint: the ranges are what let an operator tell an account's role from its id, and
#: two copies of them drift (an enrollment link that mints id 3 as a "moallem" would be
#: a worker's id in a moallem's slot).
ROLE_ID_RANGES: dict[str, tuple[int, int | None]] = {
    "worker": (1, 499),
    "moallem": (500, 999),
    "admin": (1000, 4999),
    "head_admin": (5000, None),
}

ROLE_ID_MESSAGES: dict[str, str] = {
    "worker": "Worker ID must be in range 1-499 for role 'worker'.",
    "moallem": "Lead Worker (Moallem) ID must be in range 500-999.",
    "admin": "Admin ID must be in range 1000-4999.",
    "head_admin": "Head Admin ID must be 5000 or greater.",
}


def validate_user_id_for_role(user_id: str, role: str) -> None:
    """Check an id against its role's range. Raises ``HTTPException(400)``.

    One implementation for every path that can create an account: the console, the
    bulk roster and a registration link. See ``ROLE_ID_RANGES``.
    """
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="User ID must be a numeric integer.") from None
    if role not in ROLE_ID_RANGES:
        raise HTTPException(status_code=400, detail="Invalid user role specified.")
    low, high = ROLE_ID_RANGES[role]
    if value < low or (high is not None and value > high):
        raise HTTPException(status_code=400, detail=ROLE_ID_MESSAGES[role])


def validate_password_strength(password: str) -> None:
    if not password:
        raise HTTPException(status_code=400, detail="Password must not be empty.")
    if len(password) < settings.min_password_length:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {settings.min_password_length} characters.",
        )
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at most {BCRYPT_MAX_BYTES} bytes. It is rejected rather than "
                   "silently truncated, because truncation would make two different passwords identical.",
        )
    if password.strip().lower() in WEAK_PASSWORDS:
        raise HTTPException(status_code=400, detail="Password is too common. Choose another one.")


def hash_password(password: str, *, enforce_policy: bool = True) -> str:
    """Hash a password. ``enforce_policy=False`` is only for the bootstrap admin,
    where an operator-supplied value is used once and must not crash startup."""
    if enforce_policy:
        validate_password_strength(password)
    elif len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Password must be at most 72 bytes.")
    return pwd_context.hash(_truncate_for_bcrypt(password))


def verify_password(plain: str, hashed: str | None) -> bool:
    if not plain or not hashed:
        return False
    try:
        # Truncation on verify keeps hashes written by the previous implementation
        # (which truncated before hashing) working.
        return pwd_context.verify(_truncate_for_bcrypt(plain), hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
def create_access_token(
    user_id: str,
    role: str,
    token_version: int = 0,
    *,
    ttl_hours: float | None = None,
) -> tuple[str, datetime]:
    issued = int(time.time())
    expires = issued + int((ttl_hours if ttl_hours is not None else settings.jwt_ttl_hours) * 3600)
    payload = {
        "sub": str(user_id),
        "role": role,
        "ver": int(token_version),
        "iat": issued,
        "exp": expires,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, datetime.fromtimestamp(expires, tz=timezone.utc)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "role"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401, detail="Token has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"}
        ) from None


def _load_user(user_id: str) -> sqlite3.Row | None:
    with db() as conn:
        try:
            return conn.execute(
                "SELECT id, name, role, COALESCE(token_version, 0) AS token_version "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Tolerates a database that has not been migrated yet; the startup gate
            # refuses to serve in that state, so this is belt-and-braces only.
            return conn.execute(
                "SELECT id, name, role, 0 AS token_version FROM users WHERE id = ?", (user_id,)
            ).fetchone()


async def get_current_user(
    request: Request, token: str | None = Depends(oauth2_scheme)
) -> CurrentUser:
    if not token:
        raise HTTPException(
            status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
        )
    claims = decode_access_token(token)
    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token claims")

    row = _load_user(user_id)
    if row is None:
        raise HTTPException(status_code=401, detail="User no longer exists")

    if int(claims.get("ver", 0)) != int(row["token_version"]):
        raise HTTPException(
            status_code=401,
            detail="Credentials changed. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(
        id=str(row["id"]), name=row["name"], role=row["role"], token_version=int(row["token_version"])
    )


def require_role(*allowed_roles: str) -> Callable:
    """Dependency factory: ``Depends(require_role("admin", "head_admin"))``."""
    allowed = frozenset(allowed_roles)

    def dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Requires one of these roles: {', '.join(sorted(allowed))}.",
            )
        return current

    # Consumed by readiness.router introspection so a future endpoint added
    # without a guard fails the startup gate instead of going unnoticed.
    dependency._auth_marker = "require_role"  # type: ignore[attr-defined]
    dependency._allowed_roles = tuple(sorted(allowed))  # type: ignore[attr-defined]
    return dependency


def ensure_self_or_role(target_id: str, current: CurrentUser, roles: tuple[str, ...] = ("admin", "head_admin")) -> None:
    if str(target_id) == str(current.id):
        return
    if current.role in roles:
        return
    raise HTTPException(status_code=403, detail="You may only access your own records.")


# Pre-built guards, reused so every endpoint shares one implementation.
any_authenticated = require_role("worker", "moallem", "admin", "head_admin")
admin_only = require_role("admin", "head_admin")
head_admin_only = require_role("head_admin")
