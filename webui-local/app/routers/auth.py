"""Login, token refresh, current-user and password-change endpoints (mounted at /api/auth)."""
# timedelta sets the lifetime of issued tokens.
from datetime import timedelta

# FastAPI router, dependency injection, HTTP errors and status codes.
from fastapi import APIRouter, Depends, HTTPException, status
# BaseModel validates request bodies and shapes responses.
from pydantic import BaseModel

# auth issues/validates tokens and passwords; store reads users and tenants; settings give
# lifetimes.
from .. import auth, store
from ..config import get_settings

# Every login method the UI knows about, and the subset that actually works today (OIDC is a stub).
ALL_PROVIDERS = ["password", "oidc", "ldap", "radius", "tacacs"]
IMPLEMENTED_PROVIDERS = {"password", "ldap", "radius", "tacacs"}

# Router mounted by app.main under /api/auth.
router = APIRouter()


class LoginRequest(BaseModel):
    """Login form body: username and password."""
    username: str
    password: str


class TokenResponse(BaseModel):
    """Response holding a new bearer token."""
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """Public view of the logged-in user (never includes the password hash)."""
    id: str
    username: str
    is_superadmin: bool
    tenant_roles: list[dict[str, str]]


class ChangePasswordRequest(BaseModel):
    """Change-password body: the current password (re-checked) and the new one."""
    current_password: str
    new_password: str


def _tenant_role_response(role: dict[str, str]) -> dict[str, str]:
    """Return a tenant role entry with the tenant's display name added for the UI."""
    # Look up the tenant so its human-readable name can be shown.
    tenant_id = role.get("tenant_id", "")
    tenant = store.get_tenant(tenant_id) if tenant_id else None
    # Prefer the live tenant name, then any stored name, then the raw id.
    return {
        **role,
        "tenant_name": tenant.name if tenant else role.get("tenant_name") or tenant_id,
    }


@router.get("/providers")
def list_auth_providers():
    """Return all login providers and those currently active (unauthenticated, used by login page).
    """
    # Imported here to avoid a circular import at module load time.
    from ..auth_providers import get_enabled_providers

    # Only providers that are both enabled and implemented are reported as active.
    enabled = get_enabled_providers()
    active = [provider for provider in enabled if provider in IMPLEMENTED_PROVIDERS]
    return {"providers": ALL_PROVIDERS, "active": active}


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    """Authenticate a user (local first, then the configured external provider) and return a JWT.

    Responds 401 with the list of active providers when the credentials are rejected.
    """
    # Imported lazily to avoid circular imports with the provider module.
    from .. import store as _store
    from ..auth_providers import get_enabled_providers, ldap_authenticate, radius_authenticate, tacacs_authenticate

    # Settings give the token lifetime; try the local user database first.
    settings = get_settings()
    user = auth.authenticate_user(payload.username, payload.password)

    # Local login failed: try the configured external provider, if any.
    if not user:
        config = _store.load_auth_config()
        provider = str(config.auth_provider or "local").strip().lower()
        try:
            if provider == "ldap":
                user = await ldap_authenticate(payload.username, payload.password)
            elif provider == "radius":
                user = await radius_authenticate(payload.username, payload.password)
            elif provider == "tacacs":
                user = await tacacs_authenticate(payload.username, payload.password)
        # Provider errors are treated the same as bad credentials so details do not leak.
        except Exception:
            pass

    # Nothing accepted the credentials: reply 401 and tell the UI which providers exist.
    if not user:
        enabled = get_enabled_providers()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Invalid username or password",
                "providers": ALL_PROVIDERS,
                "active": [provider for provider in enabled if provider in IMPLEMENTED_PROVIDERS],
            },
        )

    # Credentials accepted: issue a token for this username with the standard lifetime.
    token = auth.create_access_token(
        {"sub": user.username},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )
    return TokenResponse(access_token=token)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(current_user=Depends(auth.get_current_user)):
    """Silently renew the caller's access token.

    The existing token must be fully valid — including boot-ID check — so this
    endpoint will return 401 if the server has been redeployed since the token
    was issued.  The frontend uses that 401 to clear sessionStorage and show the
    login screen, giving the user a clean re-authentication prompt.
    """
    # Issue a fresh token with a new expiry for the same user.
    settings = get_settings()
    token = auth.create_access_token(
        {"sub": current_user.username},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )
    return TokenResponse(access_token=token)



@router.get("/me", response_model=UserResponse)
def me(current_user=Depends(auth.get_current_user)):
    """Return the logged-in user's id, name, superadmin flag and tenant roles (with tenant names).
    """
    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        is_superadmin=current_user.is_superadmin,
        tenant_roles=[_tenant_role_response(role) for role in current_user.tenant_roles],
    )


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, current_user=Depends(auth.get_current_user)):
    """Change the logged-in user's local password after re-checking the current one."""
    # Re-verify the old password so a stolen token alone cannot change it.
    if not auth.verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    # Store the new bcrypt hash and save the user.
    current_user.hashed_password = auth.hash_password(payload.new_password)
    store.save_user(current_user)
    return {"status": "ok", "message": "Password updated"}
