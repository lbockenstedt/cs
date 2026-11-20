"""Who may change or push a spoke's T3 MAC profile (webui-local/app/routers/t3.py).

The routes that change a spoke's profile or queue a command for the spoke
(PUT and DELETE mac-profile, POST push-mac, POST push-oui-pool) need the admin
role in the spoke's own tenant. A superadmin who is not a member gets 403 and
nothing is saved or queued: a superadmin never remote-controls a tenant's
spoke (fleet rule 5.5). The read routes still let a superadmin in. A user
holds one role per tenant, the latest one assigned, so a downgrade or a
promotion takes effect.

webui-local has no test job of its own (CI runs only
``pytest lm-spoke/tests webui-spoke/tests``), so this file imports
webui-local's ``app`` package directly, with the store faked.
"""

import contextlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Appended, not prepended, so webui-local's top-level folders never shadow the spoke's own modules.
WEBUI_LOCAL = Path(__file__).resolve().parents[2] / "webui-local"
if str(WEBUI_LOCAL) not in sys.path:
    sys.path.append(str(WEBUI_LOCAL))

# Importing app.store reads webui-local's Settings, which loads .env from the working
# directory and refuses unknown keys. The repo root can hold another tool's .env (the
# spoke tests leave INSTALL_UUID there), so import from an empty directory; get_settings()
# caches what it read then.
with tempfile.TemporaryDirectory() as _empty, contextlib.chdir(_empty):
    from app import auth, store
    from app.data_models import MacProfile, User
    from app.routers import superadmin, t3

TENANT = "acme"
BASE = f"/api/{TENANT}/spokes/spoke-1/t3"
ENTRY = {"vendor": "Acme", "oui": "3c:15:c2", "count": 2}

# Every route that changes the spoke: method, path, JSON body, and the store changes it makes.
WRITES = [
    ("PUT", f"{BASE}/mac-profile", {"entries": [ENTRY]}, ["save", "t3_mac_update"]),
    ("DELETE", f"{BASE}/mac-profile", None, ["delete"]),
    ("POST", f"{BASE}/push-mac", None, ["t3_mac_update"]),
    ("POST", f"{BASE}/push-oui-pool", None, ["t3_oui_pool_update"]),
]
WRITE_IDS = ["put-profile", "delete-profile", "push-mac", "push-oui-pool"]


def _user(superadmin=False, role=None, tenant=TENANT):
    """Return a user with the superadmin flag given and, when role is set, that role in tenant."""
    roles = [{"tenant_id": tenant, "role": role}] if role else []
    return User(username="u", hashed_password="", is_superadmin=superadmin, tenant_roles=roles)


@pytest.fixture
def call(monkeypatch):
    """Return call(user, method, path, body) -> HTTP status, against t3's router with the store faked.

    call.changes lists what the routes did to the store: "save", "delete", or
    the type of each queued command.
    """
    # Fake every store read and write the T3 routes make.
    changes = []
    spoke = SimpleNamespace(status="approved", telemetry={})
    profile = MacProfile(spoke_id="spoke-1", tenant_id=TENANT, entries=[ENTRY])
    monkeypatch.setattr(store, "get_spoke", lambda tenant_id, spoke_id: spoke)
    monkeypatch.setattr(store, "get_mac_profile", lambda tenant_id, spoke_id: profile)
    monkeypatch.setattr(store, "save_mac_profile", lambda tenant_id, spoke_id, p: changes.append("save"))
    monkeypatch.setattr(store, "delete_mac_profile", lambda tenant_id, spoke_id: changes.append("delete"))
    monkeypatch.setattr(store, "get_oui_pool_raw", lambda: [{"vendor": "Acme", "oui": "3c:15:c2"}])
    monkeypatch.setattr(store, "enqueue_command", lambda command: changes.append(command.type))

    async def no_broadcast(message):
        """Stand in for ws_broadcast; no browser is connected."""

    monkeypatch.setattr(t3, "ws_broadcast", no_broadcast)

    # Mount the router alone and log each request in as the user given.
    app = FastAPI()
    app.include_router(t3.router, prefix="/api")
    client = TestClient(app)

    def _call(user, method, path, body=None):
        """Send one request as user and return its status code."""
        app.dependency_overrides[auth.get_current_user] = lambda: user
        return client.request(method, path, json=body).status_code

    _call.changes = changes
    return _call


@pytest.mark.parametrize("method,path,body,made", WRITES, ids=WRITE_IDS)
def test_superadmin_outside_the_tenant_cannot_change_its_spoke(call, method, path, body, made):
    """A superadmin with no role in the tenant gets 403, and nothing is saved, deleted or queued."""
    assert call(_user(superadmin=True), method, path, body) == 403
    assert call.changes == []


@pytest.mark.parametrize("method,path,body,made", WRITES, ids=WRITE_IDS)
@pytest.mark.parametrize(
    "user",
    [_user(role="admin"), _user(superadmin=True, role="admin")],
    ids=["tenant-admin", "superadmin-who-is-tenant-admin"],
)
def test_the_tenants_own_admins_can_change_its_spoke(call, user, method, path, body, made):
    """An admin of the tenant passes, superadmin or not, and the route does its work."""
    assert call(user, method, path, body) == 200
    assert call.changes == made


@pytest.mark.parametrize("method,path,body,made", WRITES, ids=WRITE_IDS)
@pytest.mark.parametrize(
    "user",
    [_user(role="viewer"), _user(role="admin", tenant="other")],
    ids=["tenant-viewer", "other-tenants-admin"],
)
def test_viewers_and_other_tenants_cannot_change_the_spoke(call, user, method, path, body, made):
    """A viewer of the tenant, or an admin of another tenant, gets 403 and changes nothing."""
    assert call(user, method, path, body) == 403
    assert call.changes == []


@pytest.mark.parametrize("path", [f"{BASE}/mac-profile", f"{BASE}/devices"], ids=["mac-profile", "devices"])
def test_reads_stay_open_to_superadmins_and_members(call, path):
    """The GET routes keep require_tenant_access: superadmins and any member read, outsiders get 403."""
    assert call(_user(superadmin=True), "GET", path) == 200
    assert call(_user(role="viewer"), "GET", path) == 200
    assert call(_user(role="admin", tenant="other"), "GET", path) == 403


@pytest.mark.parametrize(
    "roles,status", [(["admin", "viewer"], 403), (["viewer", "admin"], 200)],
    ids=["downgraded-to-viewer", "promoted-to-admin"],
)
def test_the_latest_role_assigned_in_the_tenant_is_the_one_that_counts(call, roles, status):
    """A user saved while role assignment still appended can hold two roles for
    one tenant: the last one listed, the latest assigned, decides the guard."""
    user = User(username="u", hashed_password="",
                tenant_roles=[{"tenant_id": TENANT, "role": r} for r in roles])
    method, path, body, made = WRITES[0]
    assert call(user, method, path, body) == status
    assert call.changes == (made if status == 200 else [])


def test_assigning_a_role_replaces_the_users_role_in_that_tenant(monkeypatch):
    """POST /superadmin/users/{id}/roles replaces the user's role in the tenant
    instead of adding a second one, and keeps their roles in other tenants."""
    # A tenant admin who also views another tenant, with the store faked.
    user = User(username="u", hashed_password="", tenant_roles=[
        {"tenant_id": TENANT, "role": "admin"}, {"tenant_id": "other", "role": "viewer"}])
    saved = []
    monkeypatch.setattr(store, "get_user_by_id", lambda user_id: user)
    monkeypatch.setattr(store, "get_tenant", lambda tenant_id: SimpleNamespace(id=tenant_id))
    monkeypatch.setattr(store, "save_user", saved.append)
    # Downgrade them to viewer in TENANT as a superadmin.
    app = FastAPI()
    app.include_router(superadmin.router, prefix="/api")
    app.dependency_overrides[auth.require_superadmin] = lambda: _user(superadmin=True)
    reply = TestClient(app).post(f"/api/superadmin/users/{user.id}/roles",
                                 json={"tenant_id": TENANT, "role": "viewer"})
    assert reply.status_code == 200
    assert saved[-1].tenant_roles == [{"tenant_id": "other", "role": "viewer"},
                                      {"tenant_id": TENANT, "role": "viewer"}]
