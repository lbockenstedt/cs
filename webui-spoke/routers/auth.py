"""Auth API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
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

router = APIRouter()




@router.get("/api/auth/check")
async def spoke_auth_check(request: Request):
    auth_required = _spoke_auth_required()
    if not auth_required:
        return {
            "auth_required": False,
            "authenticated": True,
            "username": "admin",
            "role": "admin",
            "auth_provider": "local",
        }
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
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    provider = _normalize_spoke_auth_provider(settings.get("auth_provider", "local"))
    user: SpokeUser | None = None

    if not _spoke_auth_required():
        user = SpokeUser(username=username or "admin", role="admin", auth_provider="local")
    elif provider == "ldap" and username and password:
        user = await _ldap_authenticate(username, password)
    elif provider == "radius" and username and password:
        user = await _radius_authenticate(username, password)
    elif provider == "tacacs" and username and password:
        user = await _tacacs_authenticate(username, password)

    if user is None:
        user = _check_credentials(username, password)

    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_spoke_session(user)
    resp = JSONResponse({"ok": True, "role": user.role, "username": user.username})
    resp.set_cookie(_SPOKE_SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=_get_session_ttl())
    return resp




@router.post("/api/auth/logout")
async def spoke_auth_logout(request: Request):
    token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
    if token:
        _spoke_sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SPOKE_SESSION_COOKIE)
    return resp




@router.post("/api/auth/change-password")
async def change_password(payload: ChangePasswordPayload, user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    current_password = str(payload.current_password or "")
    new_password = str(payload.new_password or "").strip()
    stored_password = _admin_password()
    if stored_password:
        # A password is already set — require the current one to change it.
        if not current_password:
            raise HTTPException(status_code=401, detail="Current password is required.")
        if not secrets.compare_digest(current_password.strip(), stored_password):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
    if not new_password:
        raise HTTPException(status_code=422, detail="New password is required")
    settings["admin_password"] = new_password
    _save_settings()
    _spoke_sessions.clear()
    return {"ok": True}




@router.get("/api/auth/local-users")
async def list_local_users(user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    users = [{"username": "admin", "role": "admin"}]
    users.extend({"username": entry["username"], "role": entry["role"]} for entry in _get_local_users())
    return users




@router.post("/api/auth/local-users")
async def create_local_user(payload: LocalUserCreatePayload, user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    role_raw = str(payload.role or "admin").strip().lower()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="The primary admin account already exists")
    if not password:
        raise HTTPException(status_code=422, detail="Password is required")
    if role_raw not in _LOCAL_USER_ROLES:
        raise HTTPException(status_code=422, detail="Role must be admin, viewer, or demo")

    users = _get_local_users()
    if any(str(entry.get("username", "")).strip().lower() == username.lower() for entry in users):
        raise HTTPException(status_code=409, detail="User already exists")

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
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    username = str(username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="The primary admin account cannot be deleted")
    if username.lower() == user.username.lower():
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")

    users = _get_local_users()
    remaining = [entry for entry in users if str(entry.get("username", "")).strip().lower() != username.lower()]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail="User not found")

    settings["local_users"] = remaining
    _save_settings()
    _spoke_sessions.clear()
    return {"ok": True}




@router.post("/api/auth/test")
async def test_auth_provider(payload: dict, request: Request):
    """Test auth provider connectivity (admin only)."""
    user = _validate_spoke_session(request.cookies.get(_SPOKE_SESSION_COOKIE, ""))
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin required")

    provider = str(payload.get("provider", settings.get("auth_provider", "local")) or "local").strip().lower()
    if provider == "ldap":
        try:
            from ldap3 import ALL, Connection, Server

            def _ldap_probe() -> None:
                srv = Server(settings["auth_ldap_url"], get_info=ALL)
                with Connection(srv, user=settings["auth_ldap_bind_dn"], password=settings["auth_ldap_bind_password"], auto_bind=True):
                    pass
            await asyncio.to_thread(_ldap_probe)
            return {"ok": True, "detail": f"Connected to {settings['auth_ldap_url']}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
    if provider == "radius":
        return {"ok": True, "detail": "RADIUS: send a test login to verify"}
    if provider == "tacacs":
        try:
            def _tacacs_probe() -> None:
                s = socket.create_connection(
                    (settings["auth_tacacs_host"], int(settings.get("auth_tacacs_port", 49))),
                    timeout=5,
                )
                s.close()
            await asyncio.to_thread(_tacacs_probe)
            return {"ok": True, "detail": f"TCP connection to {settings['auth_tacacs_host']}:{settings.get('auth_tacacs_port', 49)} OK"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": "Local auth — no external connectivity needed"}
