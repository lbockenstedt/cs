"""Auth API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so hints like "SpokeUser | None" are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Auth helpers, models and shared state all live in server.py; this file only defines routes.
from server import (
    ChangePasswordPayload,
    Depends,
    HTTPException,
    JSONResponse,
    LocalUserCreatePayload,
    Request,
    SpokeUser,
    _LOCAL_USER_ROLES,
    _SPOKE_SESSION_COOKIE,
    _SpokeLoginRequest,
    _admin_password,
    _check_credentials,
    _create_spoke_session,
    _get_local_users,
    _get_session_ttl,
    _hash_local_password,
    _ldap_authenticate,
    _normalize_spoke_auth_provider,
    _radius_authenticate,
    _save_settings,
    _spoke_auth_required,
    _spoke_sessions,
    _tacacs_authenticate,
    _validate_spoke_session,
    asyncio,
    require_auth,
    secrets,
    settings,
    socket,
)

# The router object every endpoint below is registered on.
router = APIRouter()




@router.get("/api/auth/check")
async def spoke_auth_check(request: Request):
    """Tell the UI whether login is required and, if so, who the current session belongs to."""
    # With auth turned off everyone is treated as the built-in admin.
    auth_required = _spoke_auth_required()
    if not auth_required:
        return {
            "auth_required": False,
            "authenticated": True,
            "username": "admin",
            "role": "admin",
            "auth_provider": "local",
        }
    # Look up the session cookie and report the logged-in user (or blanks if not logged in).
    token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
    user = _validate_spoke_session(token)
    return {
        "auth_required": True,
        "authenticated": bool(user),
        "username": user.username if user else "",
        "role": user.role if user else "",
        "auth_provider": user.auth_provider if user else _normalize_spoke_auth_provider(settings.get("auth_provider", "local")),
    }




@router.post("/api/auth/login")
async def spoke_auth_login(payload: _SpokeLoginRequest):
    """Log a user in with the configured provider (falling back to local accounts) and set the
    session cookie.
    """
    # Normalise the submitted credentials and the configured provider name.
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    provider = _normalize_spoke_auth_provider(settings.get("auth_provider", "local"))
    user: SpokeUser | None = None

    # Auth disabled: accept anyone as admin.
    if not _spoke_auth_required():
        user = SpokeUser(username=username or "admin", role="admin", auth_provider="local")
    elif provider == "ldap" and username and password:
        # Otherwise try the configured external provider (LDAP, RADIUS or TACACS+) first.
        user = await _ldap_authenticate(username, password)
    elif provider == "radius" and username and password:
        user = await _radius_authenticate(username, password)
    elif provider == "tacacs" and username and password:
        user = await _tacacs_authenticate(username, password)

    # External provider failed or not configured: check the local admin/user accounts.
    if user is None:
        user = _check_credentials(username, password)

    # Nobody accepted the credentials.
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Create a server-side session and return it as an HTTP-only, same-site cookie.
    token = _create_spoke_session(user)
    resp = JSONResponse({"ok": True, "role": user.role, "username": user.username})
    resp.set_cookie(_SPOKE_SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=_get_session_ttl())
    return resp




@router.post("/api/auth/logout")
async def spoke_auth_logout(request: Request):
    """Log out by deleting the server-side session and clearing the cookie."""
    # Forget the session if the browser sent one.
    token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
    if token:
        _spoke_sessions.pop(token, None)
    # Tell the browser to drop the cookie as well.
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SPOKE_SESSION_COOKIE)
    return resp




@router.post("/api/auth/change-password")
async def change_password(payload: ChangePasswordPayload, user: SpokeUser = Depends(require_auth)):
    """Change the built-in admin password (admin only), signing everyone out afterwards."""
    # Only admins may change the admin password.
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    # Read the current and new passwords from the request.
    current_password = str(payload.current_password or "")
    new_password = str(payload.new_password or "").strip()
    stored_password = _admin_password()
    if stored_password:
        # A password is already set — require the current one to change it.
        if not current_password:
            raise HTTPException(status_code=401, detail="Current password is required.")
        if not secrets.compare_digest(current_password.strip(), stored_password):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
    # An empty new password is not allowed.
    if not new_password:
        raise HTTPException(status_code=422, detail="New password is required")
    # Save the new password and end all sessions so everyone must log in again.
    settings["admin_password"] = new_password
    _save_settings()
    _spoke_sessions.clear()
    return {"ok": True}




@router.get("/api/auth/local-users")
async def list_local_users(user: SpokeUser = Depends(require_auth)):
    """List the built-in admin plus all local user accounts (admin only)."""
    # Only admins may see the user list.
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    # The built-in admin always comes first, followed by stored users (without password hashes).
    users = [{"username": "admin", "role": "admin"}]
    users.extend({"username": entry["username"], "role": entry["role"]} for entry in _get_local_users())
    return users




@router.post("/api/auth/local-users")
async def create_local_user(payload: LocalUserCreatePayload, user: SpokeUser = Depends(require_auth)):
    """Create a local user account with a hashed password (admin only)."""
    # Only admins may add users.
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    # Read and normalise the fields; role defaults to admin.
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    role_raw = str(payload.role or "admin").strip().lower()
    # Validate: username and password required, "admin" is reserved, role must be known.
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="The primary admin account already exists")
    if not password:
        raise HTTPException(status_code=422, detail="Password is required")
    if role_raw not in _LOCAL_USER_ROLES:
        raise HTTPException(status_code=422, detail="Role must be admin, viewer, or demo")

    # Usernames are unique, compared case-insensitively.
    users = _get_local_users()
    if any(str(entry.get("username", "")).strip().lower() == username.lower() for entry in users):
        raise HTTPException(status_code=409, detail="User already exists")

    # Store the user with a password hash (never the plain password) and save settings.
    users.append({
        "username": username,
        "password_hash": _hash_local_password(password),
        "role": role_raw,
    })
    settings["local_users"] = users
    _save_settings()
    return {"ok": True}




@router.delete("/api/auth/local-users/{username}")
async def delete_local_user(username: str, user: SpokeUser = Depends(require_auth)):
    """Delete a local user account (admin only); the primary admin and yourself cannot be deleted.
    """
    # Only admins may delete users.
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    # Validate the requested username.
    username = str(username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="The primary admin account cannot be deleted")
    if username.lower() == user.username.lower():
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")

    # Keep every user except the one being deleted; if nothing was removed it did not exist.
    users = _get_local_users()
    remaining = [entry for entry in users if str(entry.get("username", "")).strip().lower() != username.lower()]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail="User not found")

    # Save and end all sessions so the deleted user's session cannot be reused.
    settings["local_users"] = remaining
    _save_settings()
    _spoke_sessions.clear()
    return {"ok": True}




@router.post("/api/auth/test")
async def test_auth_provider(payload: dict, request: Request):
    """Test auth provider connectivity (admin only)."""
    # Only a logged-in admin may run this test.
    user = _validate_spoke_session(request.cookies.get(_SPOKE_SESSION_COOKIE, ""))
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin required")

    # Which provider to test: from the request, else the saved setting, default local.
    provider = str(payload.get("provider", settings.get("auth_provider", "local")) or "local").strip().lower()
    # LDAP: try to bind with the configured service account.
    if provider == "ldap":
        try:
            from ldap3 import ALL, Connection, Server

            def _ldap_probe() -> None:
                """Open an LDAP connection and bind; raises if the server or credentials are wrong.
                """
                srv = Server(settings["auth_ldap_url"], get_info=ALL)
                with Connection(srv, user=settings["auth_ldap_bind_dn"], password=settings["auth_ldap_bind_password"], auto_bind=True):
                    pass
            # ldap3 is blocking, so run the probe in a worker thread.
            await asyncio.to_thread(_ldap_probe)
            return {"ok": True, "detail": f"Connected to {settings['auth_ldap_url']}"}
        except Exception as exc:
            # Any failure is returned as the detail message for the UI.
            return {"ok": False, "detail": str(exc)}
    # RADIUS has no harmless probe, so ask the user to try a real login.
    if provider == "radius":
        return {"ok": True, "detail": "RADIUS: send a test login to verify"}
    # TACACS+: just check that the TCP port is reachable.
    if provider == "tacacs":
        try:
            def _tacacs_probe() -> None:
                """Open and close a TCP connection to the TACACS+ server (5 s timeout)."""
                s = socket.create_connection(
                    (settings["auth_tacacs_host"], int(settings.get("auth_tacacs_port", 49))),
                    timeout=5,
                )
                s.close()
            # Socket calls are blocking, so run the probe in a worker thread.
            await asyncio.to_thread(_tacacs_probe)
            return {"ok": True, "detail": f"TCP connection to {settings['auth_tacacs_host']}:{settings.get('auth_tacacs_port', 49)} OK"}
        except Exception as exc:
            # Connection failures are returned as the detail message.
            return {"ok": False, "detail": str(exc)}
    # Local auth needs no network test.
    return {"ok": True, "detail": "Local auth — no external connectivity needed"}
