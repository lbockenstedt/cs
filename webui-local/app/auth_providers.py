"""External login providers (LDAP, RADIUS, TACACS+, OIDC stub) for hub users.

Each provider validates a username/password against an enterprise server, maps the
result to superadmin or tenant-admin access, and creates/updates a matching local User
record with no local password.
"""
# Postpone annotation evaluation so ``str | None`` hints work on older Python.
from __future__ import annotations

# asyncio runs blocking network clients in threads; io feeds an in-memory RADIUS dictionary.
import asyncio
import io
# logging reports provider errors; socket is used for the TACACS+ reachability probe.
import logging
import socket
# Optional marks functions that may return None (login rejected).
from typing import Optional

# Settings hold OIDC flags; HubAuthConfig/User are the config and user models.
from .config import get_settings
from .data_models import HubAuthConfig, User

# Module logger for authentication provider messages.
logger = logging.getLogger(__name__)


class AuthProviderError(Exception):
    """Raised when a provider is misconfigured or cannot be used."""
    pass


def _normalize_provider(value: str) -> str:
    """Return a supported provider name in lower case, defaulting to "local" for unknown values."""
    # Normalise empty/None to "local" and ignore surrounding whitespace and case.
    provider = str(value or "local").strip().lower()
    return provider if provider in {"local", "ldap", "radius", "tacacs"} else "local"


def _normalize_default_role(value: str) -> str:
    """Return "superadmin" or "tenant_admin", defaulting to "tenant_admin" for anything else."""
    # Same normalisation as above, restricted to the two valid default roles.
    role = str(value or "tenant_admin").strip().lower()
    return role if role in {"superadmin", "tenant_admin"} else "tenant_admin"


def _tenant_roles(tenant_id: str | None) -> list[dict[str, str]]:
    """Return a tenant_roles list granting admin on tenant_id, or an empty list if no tenant."""
    return [{"tenant_id": tenant_id, "role": "admin"}] if tenant_id else []


def _build_enterprise_user(username: str, is_superadmin: bool, tenant_id: str | None) -> User:
    """Build an in-memory User for an externally authenticated login (no local password)."""
    # Superadmins need no tenant roles; everyone else gets admin on the given tenant.
    return User(
        username=username,
        hashed_password="",
        is_superadmin=is_superadmin,
        tenant_roles=[] if is_superadmin else _tenant_roles(tenant_id),
    )


def _resolve_default_access(config: HubAuthConfig) -> tuple[bool, str | None]:
    """Return (is_superadmin, tenant_id) for a user who matched no explicit group/role mapping.

    Raises AuthProviderError if the default is tenant_admin but no tenant is configured.
    """
    # Default tenant_admin requires knowing which tenant to grant.
    if _normalize_default_role(config.auth_default_role) == "tenant_admin":
        tenant_id = (config.auth_ldap_tenant_id or "").strip()
        if not tenant_id:
            raise AuthProviderError("auth_ldap_tenant_id is required when auth_default_role is tenant_admin")
        return False, tenant_id
    # Otherwise the default role is superadmin.
    return True, None


def _decrypt_secret(value: str) -> str:
    """Decrypt a stored provider secret; empty input returns an empty string."""
    # Nothing stored means no secret configured.
    if not value:
        return ""
    # Imported lazily to avoid loading crypto until a secret is needed.
    from .crypto import decrypt_str

    # Wrong key or corrupt data becomes an AuthProviderError with a readable message.
    try:
        return decrypt_str(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise AuthProviderError(f"Unable to decrypt stored secret: {exc}") from exc


def _provision_enterprise_user(username: str, is_superadmin: bool, tenant_id: str | None) -> User:
    """Create or update the local User record for an external login and save it."""
    # Imported lazily to avoid a circular import with the store module.
    from . import store

    # Look for an existing account with the same username.
    user = store.get_user(username)
    # First login: build a fresh user record.
    if user is None:
        user = _build_enterprise_user(username, is_superadmin, tenant_id)
    # Returning user: refresh their access from the external server's answer on every login.
    else:
        user.is_superadmin = is_superadmin
        user.tenant_roles = [] if is_superadmin else _tenant_roles(tenant_id)
        user.hashed_password = user.hashed_password or ""
    # Persist the record so the user appears in the user list and tokens can resolve it.
    store.save_user(user)
    return user


async def oidc_authenticate(token: str) -> Optional[User]:
    """OpenID Connect login; not implemented yet (returns None when disabled, raises otherwise)."""
    # Skip entirely when OIDC is turned off in settings.
    settings = get_settings()
    if not settings.oidc_enabled:
        return None
    logger.warning("OIDC auth attempted but not yet implemented")
    raise AuthProviderError("OIDC authentication not yet implemented")


def _ldap_authenticate_sync(
    config: HubAuthConfig,
    username: str,
    password: str,
    *,
    provision: bool,
) -> Optional[User]:
    """Authenticate against LDAP (blocking). Returns the User, or None on any failure.

    Binds with the service account, finds the user, re-binds as the user to check the password,
    then maps group membership to a role. ``provision`` saves the user to the store.
    """
    # ldap3 is optional; without it LDAP login is simply unavailable.
    try:
        from ldap3 import ALL, Connection, Server
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        logger.warning("ldap3 not installed — LDAP auth unavailable")
        return None

    # Every error (network, bad config, bad password) is logged and treated as a failed login.
    try:
        # Service-account password plus required connection settings; missing any means "not set
        # up".
        bind_password = _decrypt_secret(config.auth_ldap_bind_password_enc)
        if not config.auth_ldap_url or not config.auth_ldap_bind_dn or not bind_password or not config.auth_ldap_user_base:
            return None

        # LDAP server object; get_info=ALL reads the server schema/info on connect.
        server = Server(config.auth_ldap_url, get_info=ALL)
        # Escape the unauthenticated username before interpolating it into the
        # LDAP search filter — otherwise a login body like "x)(sAMAccountName=*)"
        # broadens the filter into unauthenticated directory enumeration /
        # attribute disclosure (LDAP filter injection).
        safe_user = escape_filter_chars(str(username or ""))
        # Insert the escaped username into the configured search filter (default: AD
        # sAMAccountName).
        search_filter = str(config.auth_ldap_user_filter or "(&(objectClass=user)(sAMAccountName={username}))").format(username=safe_user)
        # Bind as the service account to look the user up.
        with Connection(server, user=config.auth_ldap_bind_dn, password=bind_password, auto_bind=True) as conn:
            # Search below the user base for the login name and fetch a few useful attributes.
            conn.search(
                search_base=config.auth_ldap_user_base,
                search_filter=search_filter,
                attributes=["cn", "mail", "memberOf", "displayName"],
            )
            # Unknown user: login fails.
            if not conn.entries:
                return None
            # Use the first match; keep its DN for the password bind and its group list for role
            # mapping.
            entry = conn.entries[0]
            user_dn = entry.entry_dn
            member_of = [str(group) for group in list(entry.memberOf)] if hasattr(entry, "memberOf") and entry.memberOf else []

        # Bind as the user with the supplied password; failure means a wrong password.
        with Connection(server, user=user_dn, password=password, auto_bind=True) as user_conn:
            if not user_conn.bound:
                return None

        # Lower-case groups and configured group names so matching is case-insensitive.
        member_of_lower = [group.lower() for group in member_of]
        superadmin_group = (config.auth_ldap_group_superadmin or "").strip().lower()
        tenant_admin_group = (config.auth_ldap_group_tenant_admin or "").strip().lower()
        # Tenant granted to members of the tenant-admin group.
        tenant_id = (config.auth_ldap_tenant_id or "").strip()

        # Superadmin if any group contains the configured superadmin group name.
        is_superadmin = bool(superadmin_group and any(superadmin_group in group for group in member_of_lower))
        # Otherwise grant tenant admin when the user is in the tenant-admin group...
        assigned_tenant_id: str | None = None
        if not is_superadmin and tenant_admin_group and tenant_id and any(tenant_admin_group in group for group in member_of_lower):
            assigned_tenant_id = tenant_id
        # ...or fall back to the configured default role.
        elif not is_superadmin:
            is_superadmin, assigned_tenant_id = _resolve_default_access(config)

        # Either save the user (real login) or just build it in memory (connection test).
        return _provision_enterprise_user(username, is_superadmin, assigned_tenant_id) if provision else _build_enterprise_user(username, is_superadmin, assigned_tenant_id)
    # Any failure is logged without the password and treated as a rejected login.
    except Exception as exc:
        logger.warning("LDAP auth error for %s: %s", username, exc)
        return None


async def ldap_authenticate(username: str, password: str) -> Optional[User]:
    """Async LDAP login used by the login endpoint; returns None unless LDAP is the active provider.
    """
    # Imported lazily to avoid a circular import.
    from . import store

    # Load the saved provider settings and only proceed if LDAP is selected.
    config = store.load_auth_config()
    if _normalize_provider(config.auth_provider) != "ldap":
        return None
    # Run the blocking LDAP client in a worker thread and save the user on success.
    return await asyncio.to_thread(_ldap_authenticate_sync, config, username, password, provision=True)


def _radius_authenticate_sync(
    config: HubAuthConfig,
    username: str,
    password: str,
    *,
    provision: bool,
) -> Optional[User]:
    """Authenticate against RADIUS (blocking). Returns the User, or None on any failure.

    A configured reply attribute (default Filter-Id) equal to the superadmin value grants
    superadmin; otherwise the default role applies.
    """
    # pyrad is optional; without it RADIUS login is unavailable.
    try:
        import pyrad.client
        import pyrad.dictionary
        import pyrad.packet
    except ImportError:
        logger.warning("pyrad not installed — RADIUS auth unavailable")
        return None

    # Every error is logged and treated as a failed login.
    try:
        # Shared secret and host are required.
        secret = _decrypt_secret(config.auth_radius_secret_enc)
        if not config.auth_radius_host or not secret:
            return None

        # Minimal attribute dictionary (standard RADIUS attribute numbers) so no dictionary file is
        # needed.
        dictionary = pyrad.dictionary.Dictionary(io.StringIO(
            "ATTRIBUTE User-Name 1 string\n"
            "ATTRIBUTE User-Password 2 string\n"
            "ATTRIBUTE Filter-Id 11 string\n"
            "ATTRIBUTE Class 25 string\n"
        ))
        # RADIUS client for the configured server; 1812 is the standard auth port.
        client = pyrad.client.Client(
            server=config.auth_radius_host,
            authport=int(config.auth_radius_port or 1812),
            secret=secret.encode(),
            dict=dictionary,
        )
        # Give up after 10 seconds if the server does not answer.
        client.timeout = 10
        # Build an Access-Request with the username and the password obfuscated with the shared
        # secret.
        req = client.CreateAuthPacket(code=pyrad.packet.AccessRequest, User_Name=username)
        req["User-Password"] = req.PwCrypt(password)
        # Send it; anything other than Access-Accept means the login failed.
        reply = client.SendPacket(req)
        if reply.code != pyrad.packet.AccessAccept:
            return None

        # Which reply attribute carries the role, and the value that means superadmin.
        role_attr = str(config.auth_radius_role_attr or "Filter-Id")
        superadmin_val = str(config.auth_radius_superadmin_val or "superadmin").strip().lower()
        # Check each returned attribute value (bytes or str) for the superadmin marker.
        is_superadmin = False
        values = reply.get(role_attr, []) if hasattr(reply, "get") else []
        for raw_value in values:
            attr_value = raw_value.decode(errors="ignore") if isinstance(raw_value, bytes) else str(raw_value)
            if attr_value.strip().lower() == superadmin_val:
                is_superadmin = True
                break

        # Non-superadmins get the configured default access.
        assigned_tenant_id: str | None = None
        if not is_superadmin:
            is_superadmin, assigned_tenant_id = _resolve_default_access(config)

        # Save the user for a real login, or just build it for a connection test.
        return _provision_enterprise_user(username, is_superadmin, assigned_tenant_id) if provision else _build_enterprise_user(username, is_superadmin, assigned_tenant_id)
    # Failures are logged and treated as a rejected login.
    except Exception as exc:
        logger.warning("RADIUS auth error for %s: %s", username, exc)
        return None


async def radius_authenticate(username: str, password: str) -> Optional[User]:
    """Async RADIUS login; returns None unless RADIUS is the active provider."""
    # Imported lazily to avoid a circular import.
    from . import store

    # Only proceed if RADIUS is the selected provider.
    config = store.load_auth_config()
    if _normalize_provider(config.auth_provider) != "radius":
        return None
    # Run the blocking client in a worker thread and save the user on success.
    return await asyncio.to_thread(_radius_authenticate_sync, config, username, password, provision=True)


def _tacacs_authenticate_sync(
    config: HubAuthConfig,
    username: str,
    password: str,
    *,
    provision: bool,
) -> Optional[User]:
    """Authenticate against TACACS+ (blocking). Returns the User, or None on any failure.

    The user's shell privilege level (priv-lvl) at or above the configured threshold
    grants superadmin.
    """
    # tacacs_plus is optional; without it TACACS+ login is unavailable.
    try:
        import tacacs_plus.client as tacacs
    except ImportError:
        logger.warning("tacacs-plus not installed — TACACS+ auth unavailable")
        return None

    # Every error is logged and treated as a failed login.
    try:
        # Shared secret and host are required.
        secret = _decrypt_secret(config.auth_tacacs_secret_enc)
        if not config.auth_tacacs_host or not secret:
            return None

        # TACACS+ client; 49 is the standard TACACS+ port.
        client = tacacs.TACACSClient(
            host=config.auth_tacacs_host,
            port=int(config.auth_tacacs_port or 49),
            secret=secret.encode(),
            timeout=10,
        )
        # Authentication step: verify username/password.
        authen = client.authenticate(username, password)
        if not getattr(authen, "valid", False):
            return None

        # Authorization step: ask for shell service attributes to learn the privilege level.
        author = client.authorize(username, arguments=[b"service=shell", b"cmd="])
        # Find the "priv-lvl=N" attribute; unparsable values count as 0 (no privileges).
        priv_lvl = 0
        for arg in getattr(author, "arguments", []) or []:
            arg_str = arg.decode() if isinstance(arg, bytes) else str(arg)
            if arg_str.startswith("priv-lvl="):
                try:
                    priv_lvl = int(arg_str.split("=", 1)[1])
                except Exception:
                    priv_lvl = 0
                break

        # Privilege at or above the threshold (default 15, the Cisco maximum) means superadmin.
        is_superadmin = priv_lvl >= int(config.auth_tacacs_superadmin_priv or 15)
        assigned_tenant_id: str | None = None
        if not is_superadmin:
            is_superadmin, assigned_tenant_id = _resolve_default_access(config)

        # Save the user for a real login, or just build it for a connection test.
        return _provision_enterprise_user(username, is_superadmin, assigned_tenant_id) if provision else _build_enterprise_user(username, is_superadmin, assigned_tenant_id)
    # Failures are logged and treated as a rejected login.
    except Exception as exc:
        logger.warning("TACACS+ auth error for %s: %s", username, exc)
        return None


async def tacacs_authenticate(username: str, password: str) -> Optional[User]:
    """Async TACACS+ login; returns None unless TACACS+ is the active provider."""
    # Imported lazily to avoid a circular import.
    from . import store

    # Only proceed if TACACS+ is the selected provider.
    config = store.load_auth_config()
    if _normalize_provider(config.auth_provider) != "tacacs":
        return None
    # Run the blocking client in a worker thread and save the user on success.
    return await asyncio.to_thread(_tacacs_authenticate_sync, config, username, password, provision=True)


async def test_auth_provider(
    provider: str,
    config: HubAuthConfig,
    test_username: str = "",
    test_password: str = "",
) -> dict:
    """Check that a provider's settings work, optionally trying a test login.

    Returns {"ok": bool, "message": str} for display in the settings page; never saves users.
    """
    # Local auth needs no external server, so it always passes.
    provider_name = _normalize_provider(provider)
    if provider_name == "local":
        return {"ok": True, "message": "Local auth does not require external connectivity."}

    # LDAP: bind with the service account, then optionally try the test credentials.
    if provider_name == "ldap":
        try:
            from ldap3 import ALL, Connection, Server

            # Required LDAP settings must be filled in before testing.
            bind_password = _decrypt_secret(config.auth_ldap_bind_password_enc)
            if not config.auth_ldap_url or not config.auth_ldap_bind_dn or not bind_password:
                return {"ok": False, "message": "LDAP server URL, bind DN, and bind password are required."}

            def _ldap_bind_probe() -> None:
                """Open and close a service-account bind to prove connectivity and credentials."""
                server = Server(config.auth_ldap_url, get_info=ALL)
                with Connection(server, user=config.auth_ldap_bind_dn, password=bind_password, auto_bind=True):
                    pass

            # Run the bind probe off the event loop.
            await asyncio.to_thread(_ldap_bind_probe)
            # With test credentials, run the full login flow without saving the user.
            if test_username and test_password:
                user = await asyncio.to_thread(_ldap_authenticate_sync, config, test_username, test_password, provision=False)
                if not user:
                    return {"ok": False, "message": "LDAP test login failed."}
                # Report which access level the test user would receive.
                role = "superadmin" if user.is_superadmin else ("tenant-admin" if user.tenant_roles else "user")
                return {"ok": True, "message": f"LDAP bind and test login succeeded ({role})."}
            return {"ok": True, "message": f"Connected to {config.auth_ldap_url}."}
        # Show any error text to the admin performing the test.
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # RADIUS has no separate connectivity check, so a test username/password is mandatory.
    if provider_name == "radius":
        try:
            if not test_username or not test_password:
                return {"ok": False, "message": "RADIUS testing requires a username and password."}
            user = await asyncio.to_thread(_radius_authenticate_sync, config, test_username, test_password, provision=False)
            if not user:
                return {"ok": False, "message": "RADIUS authentication failed."}
            role = "superadmin" if user.is_superadmin else ("tenant-admin" if user.tenant_roles else "user")
            return {"ok": True, "message": f"RADIUS authentication succeeded ({role})."}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # TACACS+: full login test if credentials are given, otherwise just a TCP reachability check.
    if provider_name == "tacacs":
        try:
            if test_username and test_password:
                user = await asyncio.to_thread(_tacacs_authenticate_sync, config, test_username, test_password, provision=False)
                if not user:
                    return {"ok": False, "message": "TACACS+ authentication failed."}
                role = "superadmin" if user.is_superadmin else ("tenant-admin" if user.tenant_roles else "user")
                return {"ok": True, "message": f"TACACS+ authentication succeeded ({role})."}

            # A host is needed for the TCP probe.
            if not config.auth_tacacs_host:
                return {"ok": False, "message": "TACACS+ host is required."}

            def _tacacs_tcp_probe() -> None:
                """Open and close a TCP connection to the TACACS+ server (5 second timeout)."""
                s = socket.create_connection(
                    (config.auth_tacacs_host, int(config.auth_tacacs_port or 49)),
                    timeout=5,
                )
                s.close()

            # Run the probe off the event loop and report success.
            await asyncio.to_thread(_tacacs_tcp_probe)
            return {"ok": True, "message": f"TCP connection to {config.auth_tacacs_host}:{config.auth_tacacs_port or 49} OK."}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # Provider name not recognised (normally unreachable thanks to _normalize_provider).
    return {"ok": False, "message": f"Unsupported provider: {provider_name}"}


def get_enabled_providers() -> list[str]:
    """Return the login methods the login page should offer."""
    # Imported lazily to avoid a circular import.
    from . import store

    # Read OIDC flags from settings and the selected provider from the stored auth config.
    settings = get_settings()
    config = store.load_auth_config()
    # Username/password is always offered.
    providers = ["password"]
    # Add OIDC when enabled in settings.
    if settings.oidc_enabled:
        providers.append("oidc")
    # Add the selected external provider (local login is already covered by "password").
    selected = _normalize_provider(config.auth_provider)
    if selected != "local":
        providers.append(selected)
    return providers
