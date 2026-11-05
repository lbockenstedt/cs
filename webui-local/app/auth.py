"""Authentication helpers: password hashing, JWT creation/validation and FastAPI access guards.

Routers use the get_current_user / require_* functions as dependencies to enforce who may
call an endpoint.
"""
# Postpone evaluation of type hints so annotations can reference names defined later.
from __future__ import annotations

# logging records security-relevant warnings (e.g. superadmin remote-control).
import logging
# datetime/timedelta compute token expiry times.
from datetime import datetime, timedelta
# Type hints: Annotated attaches FastAPI dependencies to parameters; Optional marks maybe-None.
from typing import Annotated, Optional
# uuid4 provides a random boot ID when no deploy version is available.
from uuid import uuid4

# FastAPI dependency injection, HTTP error responses and status-code constants.
from fastapi import Depends, HTTPException, status
# OAuth2PasswordBearer pulls the "Authorization: Bearer <token>" header out of requests.
from fastapi.security import OAuth2PasswordBearer
# PyJWT encodes and decodes the signed login tokens.
import jwt  # PyJWT, already used elsewhere in the fleet (replaces python-jose)
# passlib's CryptContext hashes and verifies passwords with bcrypt.
from passlib.context import CryptContext

# Module logger so auth messages can be filtered by name.
logger = logging.getLogger(__name__)

# store reads users; get_settings supplies secrets; User is the authenticated-user model.
from . import store
from .config import get_settings
from .data_models import User

# Password hasher configured for bcrypt; "deprecated=auto" re-hashes old schemes on upgrade.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# Dependency that requires a bearer token (401 if absent); tokenUrl tells OpenAPI where to log in.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
# Same as above but returns None instead of raising when no token is supplied.
oauth2_optional = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# ── Server boot nonce ──────────────────────────────────────────────────────────
# All gunicorn workers must agree on the same boot ID.  Using a per-process
# uuid4() causes cross-worker 401 failures: login hits worker A, /me hits
# worker B, the IDs differ, the token is rejected, and the user is immediately
# logged out again.
#
# Solution: derive the ID from APP_VERSION (the git SHA injected at build time
# via the Docker ARG / ENV pipeline).  All workers in the same container share
# the same env, so they all compute the same value.  When the container is
# rebuilt and redeployed the SHA changes → all previously issued tokens become
# invalid and users are prompted to re-authenticate.  If APP_VERSION is not set
# (local dev), fall back to a uuid4 so behaviour is still correct for that
# single-process case.
# os reads env vars; Path locates the VERSION file. Aliased to keep them private to this module.
import os as _os
from pathlib import Path as _Path

def _get_boot_id() -> str:
    """Return an ID that is identical for every worker of the same deployment.
    Order of preference: the VERSION file baked into the image, the APP_VERSION env var,
    then a random uuid4 (local single-process dev only).
    """
    # All gunicorn workers must use the same boot ID or cross-worker 401s occur.
    # The VERSION file is baked into the image by redeploy-prod.sh (contains the
    # git SHA at deploy time) — identical for every worker process in the same
    # container.  A redeploy writes a new SHA → old tokens are invalidated.
    # Falls back to APP_VERSION env var, then uuid4 for local single-process dev.
    # VERSION lives in the webui-local folder, two levels above this file.
    version_file = _Path(__file__).resolve().parent.parent / "VERSION"
    # Use the file's content if it exists and is a real version (not the "dev" placeholder).
    try:
        content = version_file.read_text().strip()
        if content and content not in ("dev", ""):
            return content
    # Missing/unreadable VERSION file is normal in dev, so silently fall through to the next option.
    except Exception:
        pass
    # Second choice: version injected through the environment at build time.
    env_ver = _os.getenv("APP_VERSION", "").strip()
    if env_ver:
        return env_ver
    # Local dev only — single process so no cross-worker issue.
    return str(uuid4())

# Computed once at import; every issued token embeds it and every decoded token must match it.
SERVER_BOOT_ID: str = _get_boot_id()


def hash_password(password: str) -> str:
    """Return a bcrypt hash of the password, suitable for storing in the database."""
    # bcrypt truncates at 72 bytes — enforce explicitly to avoid ValueError
    return pwd_context.hash(password.encode()[:72])


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if the plain password matches the stored bcrypt hash."""
    # Accounts without a password (e.g. external-login users) can never match.
    if not hashed:
        return False
    # Truncate to 72 bytes exactly as hash_password did so the comparison is consistent.
    return pwd_context.verify(plain.encode()[:72], hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a signed JWT carrying ``data`` plus an expiry and the server boot ID.

    Args:
        data: Claims to embed (normally {"sub": username}).
        expires_delta: Custom lifetime; defaults to the configured access-token lifetime.
    """
    # Load the signing key, algorithm and default lifetime.
    settings = get_settings()
    # Copy so the caller's dict is not modified when we add claims.
    to_encode = data.copy()
    # Absolute expiry time; PyJWT stores it in the standard "exp" claim and enforces it on decode.
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode["exp"] = expire
    # Embed the server boot nonce so this token is only valid for the current
    # server instance.  Tokens from before a redeploy will fail validation.
    to_encode["bid"] = SERVER_BOOT_ID
    # Sign the claims with the server secret and return the compact token string.
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def authenticate_user(username: str, password: str) -> Optional[User]:
    """Return the User if username/password are valid, otherwise None."""
    # Look the user up by name; unknown users simply fail authentication.
    user = store.get_user(username)
    # Fail if the user is missing, has no local password, or the password does not match.
    if not user or not user.hashed_password or not verify_password(password, user.hashed_password):
        return None
    return user


# ── QA runner token ────────────────────────────────────────────────────────────
# QA API keys are tenant-scoped and superadmin-generated.  Exchanging a valid
# key via POST /api/qa/auth returns a short-lived JWT that grants read-only
# access to exactly one tenant.  The JWT sub is "__qa_runner__" so _decode_token
# constructs a synthetic User without a database lookup.

# Special "sub" value that marks a token as belonging to the QA runner instead of a real user.
_QA_RUNNER_SUB = "__qa_runner__"
# QA tokens live 2 hours, long enough to finish a full QA run.
_QA_TOKEN_EXPIRE_MINUTES = 120  # 2 hours — long enough for a full QA run


def create_qa_runner_token(tenant_id: str, key_id: str) -> str:
    """Issue a short-lived JWT scoped to a single tenant for the QA runner."""
    # Reuse the normal token builder, adding the tenant and key id as extra claims.
    return create_access_token(
        {"sub": _QA_RUNNER_SUB, "qa_tenant": tenant_id, "qa_key_id": key_id},
        expires_delta=timedelta(minutes=_QA_TOKEN_EXPIRE_MINUTES),
    )


def _decode_token(token: str) -> User:
    """Validate a JWT and return the User it represents.
    Raises HTTPException 401 when the token is invalid, expired, from an older deployment,
    or refers to a user that no longer exists.
    """
    # Signing key and algorithm are needed to verify the token.
    settings = get_settings()
    # Generic 401 reused for every failure so attackers learn nothing about why validation failed.
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # Decoding can fail in several ways (bad signature, expired); all are handled below.
    try:
        # Verify signature and expiry and return the claims dict.
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        # "sub" holds the username; a token without one is not usable.
        username = payload.get("sub")
        if not username:
            raise exc
        # Validate boot nonce — reject tokens issued by a previous server instance
        # (e.g. before a redeploy).  Missing "bid" is also treated as invalid so
        # that old tokens issued before this feature was deployed are rejected cleanly.
        if payload.get("bid") != SERVER_BOOT_ID:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired after server restart — please log in again",
                headers={"WWW-Authenticate": "Bearer"},
            )
    # Any PyJWT validation failure (bad signature, expired, malformed) becomes a plain 401.
    except jwt.PyJWTError:
        raise exc

    # Synthetic QA runner user — tenant-scoped admin, no DB lookup needed.
    # Role is "admin" (not superadmin) so QA can perform destructive tenant operations
    # (teardown VMs, toggle auto-prov, clear command queue) without global access.
    if username == _QA_RUNNER_SUB:
        # A QA token must name its tenant; otherwise reject it.
        qa_tenant = payload.get("qa_tenant", "")
        if not qa_tenant:
            raise exc
        # Build an in-memory user with admin rights on that single tenant only.
        return User(
            id="__qa_runner__",
            username="__qa_runner__",
            is_superadmin=False,
            hashed_password="",
            tenant_roles=[{"tenant_id": qa_tenant, "role": "admin"}],
        )

    # Normal tokens: load the real user so role changes/deletions take effect immediately.
    user = store.get_user(username)
    if not user:
        raise exc
    return user


def decode_access_token(token: str) -> User:
    """Public wrapper around _decode_token used by routers and WebSocket handlers."""
    return _decode_token(token)


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    """FastAPI dependency: return the logged-in user or respond 401."""
    return decode_access_token(token)


def get_optional_current_user(
    token: Annotated[Optional[str], Depends(oauth2_optional)],
) -> Optional[User]:
    """FastAPI dependency: return the logged-in user, or None for anonymous/invalid requests."""
    # No token supplied means an anonymous request.
    if not token:
        return None
    # An invalid token is treated like no token rather than an error for optional-auth endpoints.
    try:
        return decode_access_token(token)
    except HTTPException:
        return None


def require_superadmin(user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency that only lets superadmins through (403 for everyone else)."""
    if not user.is_superadmin:
        raise HTTPException(status_code=403, detail="Superadmin required")
    return user


def require_tenant_access(tenant_id: str, user: User = Depends(get_current_user)) -> User:
    """Allow superadmins or any member (any role) of the given tenant; otherwise respond 403."""
    # Superadmins can see every tenant.
    if user.is_superadmin:
        return user
    # Regular users must have a role in this tenant.
    if tenant_id not in user.tenant_ids():
        raise HTTPException(status_code=403, detail="Access denied to this tenant")
    return user


def require_tenant_admin(tenant_id: str, user: User = Depends(get_current_user)) -> User:
    """Require tenant admin (or superadmin) role — viewers are rejected."""
    # Superadmins always qualify.
    if user.is_superadmin:
        return user
    # Look up the user's role in this tenant; only "admin" may perform changes.
    role = user.get_role(tenant_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Tenant admin role required")
    return user


def require_tenant_member(tenant_id: str, user: User) -> User:
    """Like require_tenant_access but requires explicit tenant membership.

    Use this for remote-control endpoints (proxmox-command, console, spoke shell)
    where only an explicit tenant member should normally be allowed.  Superadmins
    are permitted but their actions are audit-logged as a warning.
    """
    # Allow superadmins but leave an audit trail warning since they are not tenant members.
    if user.is_superadmin:
        logger.warning(
            "Superadmin '%s' remote-controlling tenant %s infrastructure "
            "(not an explicit tenant member — add them as a tenant member to suppress this)",
            user.username,
            tenant_id,
        )
        return user
    # Non-superadmins must be members of the tenant.
    if tenant_id not in user.tenant_ids():
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote control is restricted to explicit tenant members. "
                "Ask a superadmin to add your account to this tenant with admin role."
            ),
        )
    return user


def ensure_admin() -> None:
    """Bootstrap superadmin user on first start."""
    # Imported locally because only this function reads raw env variables.
    import os
    # Settings supply the initial admin username and password.
    settings = get_settings()
    # force_password=True when ADMIN_PASSWORD was explicitly set in the environment
    # so operators can reset credentials by changing the env var and restarting.
    force_password = bool(os.environ.get("ADMIN_PASSWORD") or os.environ.get("FIRST_ADMIN_PASSWORD"))
    # Create the admin if missing (or reset its password when forced) using a bcrypt hash.
    store.ensure_admin(
        username=settings.first_admin_username,
        hashed_password=hash_password(settings.first_admin_password),
        force_password=force_password,
    )
