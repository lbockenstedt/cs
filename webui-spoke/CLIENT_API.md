# webui-spoke — Client Simulation Client API

This document describes how simulation clients communicate with the local **webui-spoke** server. Clients use this API to:

1. **Report status** (heartbeat/beacon) so the spoke knows they are alive
2. **Pull configuration** to receive their `simulation.conf` (including any in-spoke overrides)
3. **Pull scripts** to stay up to date with the latest simulation scripts

> Note: the client VM API remains local to the spoke. Even when hub relay is enabled, clients still talk to `webui-spoke`; the spoke separately relays telemetry and command state to `webui-hub`.

---

## Quick Start

### 1. Point clients at the spoke server

Set `server_url` in `simulation.conf` on each client:

```ini
[server]
server_url=http://169.253.1.1:8000
```

Replace `169.253.1.1` with the webui-spoke server's IP address. For the standard Proxmox deployment this is the `eth1` address of the webui-spoke LXC on `vmbr255`. For a development/test server use the host IP and port `8000`.

If `server_url` is blank or unreachable, all API calls are skipped and the client runs in standalone mode — it continues to use whatever scripts and config it last downloaded.

### 2. Verify the server is reachable

```bash
curl http://169.253.1.1:8000/api/health
```

Expected response:

```json
{
  "status": "ok",
  "version": "1.00",
  "clients": 0,
  "repo_synced": true,
  "repo_error": null,
  "installer_version": "1.00"
}
```

`repo_synced: true` confirms the server has successfully cloned the git repo and can serve scripts and config. If `repo_synced` is `false`, check the **Setup** tab in the spoke UI for the sync error — clients cannot pull scripts until the repo is ready.

### 3. Confirm config is being served

```bash
curl "http://169.253.1.1:8000/api/config?hostname=$(hostname)"
```

This returns the INI-format `simulation.conf` with any per-client overrides already merged in. If the output looks correct, the client is ready to sync automatically.

---

## Endpoints Used by Clients

### 1. Health Check

**Before doing anything**, check the server is up:

```
GET /api/health
```

**Response:**

```json
{
  "status": "ok",
  "version": "1.00",
  "clients": 4,
  "repo_synced": true,
  "repo_error": null,
  "installer_version": "1.00"
}
```

A `200 OK` with `"status": "ok"` means the server is ready. If this fails, skip all further API calls for this cycle.

**Shell example:**

```bash
curl -sf "${server_url}/api/health" > /dev/null || exit 0
```

---

### 2. Report Status (Heartbeat)

Clients POST a JSON beacon on every simulation cycle so the spoke can track their state.

```
POST /api/status
Content-Type: application/json
```

**Request body:**

```json
{
  "hostname":           "jsmith",
  "simulation_id":      "s4",
  "platform":           "linux",
  "iteration":          42,
  "connected_ssid":     "HPE-Corp",
  "gateway_reachable":  true,
  "vh_connected":       false,
  "active_simulations": ["dns_fail", "www_traffic"],
  "errors":             ["SSID not found after 30s scan", "Gateway unreachable"],
  "config": {
    "sim_phy":     "on",
    "kill_switch": "off",
    "dns_fail":    "on",
    "iperf":       "off",
    "www_traffic": "on",
    "download":    "off",
    "ping_test":   "off",
    "ssidpw_fail": "off",
    "auth_fail":   "off",
    "dhcp_fail":   "off"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `hostname` | string | Device hostname (person name only, e.g. `jsmith`) — used as the unique client key |
| `simulation_id` | string | Bucket section from `simulation.conf` (e.g. `s0`–`s9`) — assigned by hashing the hostname; can be pinned via `user-overrides.conf` |
| `platform` | string | `linux` or `windows` |
| `iteration` | int | Simulation loop counter |
| `connected_ssid` | string \| null | Currently associated SSID (blank if disconnected) |
| `gateway_reachable` | bool | Whether the default gateway responded to ping |
| `vh_connected` | bool | Whether VH (VirtualHub) connection is active |
| `active_simulations` | array of strings | Which simulations are currently running |
| `errors` | array of strings | *(optional)* Error messages accumulated since the last beacon. The server stores the last 50 per client (circular buffer) and displays them in the spoke error log. Cleared from the client buffer only after a successful POST. |
| `config` | object | Key/value pairs from the client's active `simulation.conf` section |

**Response:**

```json
{
  "status": "ok",
  "client": { ... }
}
```

The `client` object is the full serialized client record as stored in the spoke.

**Shell example (from `simulation.sh`):**

```bash
curl -m 5 -s -o /dev/null \
  -H "Content-Type: application/json" \
  -X POST \
  --data "$payload" \
  "${server_url}/api/status"
```

The response is ignored — if the POST fails the simulation continues normally.

---

### 3. Pull Configuration

Clients fetch their effective `simulation.conf` from the server. The server returns the file from the synced git repo, with any active in-spoke overrides applied for this hostname.

```
GET /api/config?hostname=<hostname>
```

| Parameter | Required | Description |
|---|---|---|
| `hostname` | No | If provided, any overrides set via the spoke for this client are merged in. If omitted, the raw file is returned. |

**Response:** Plain text — the full contents of `configs/simulation.conf` (INI format), with overrides applied as key=value replacements in the client's section.

**Shell example (from `update.sh`):**

```bash
curl -sf \
  "${server_url}/api/config?hostname=${HOSTNAME}" \
  -o /tmp/simulation.conf

# If successful, replace the local config
if [[ -s /tmp/simulation.conf ]]; then
  cp /tmp/simulation.conf /path/to/configs/simulation.conf
fi
```

If the server is unreachable or returns an error, keep the existing local `simulation.conf`.

---

### 4. Pull Script List

Clients can check what scripts are available on the server for their platform:

```
GET /api/scripts/list?platform=linux
GET /api/scripts/list?platform=windows
```

**Response:** JSON array of filenames:

```json
["simulation.sh", "startup.sh", "update.sh", "dashboard.sh"]
```

---

### 5. Download a Script

Download an individual script file:

```
GET /api/scripts/{platform}/{filename}
```

| Parameter | Description |
|---|---|
| `platform` | `linux` or `windows` |
| `filename` | Script filename (e.g. `simulation.sh`) |

**Response:** Raw file content (`application/octet-stream`).

**Shell example (from `update.sh`):**

```bash
# Get list of scripts
scripts=$(curl -sf "${server_url}/api/scripts/list?platform=linux" || echo "")

# Download each one
for filename in $scripts; do
  curl -sf "${server_url}/api/scripts/linux/${filename}" \
    -o "/path/to/scripts/${filename}"
done
```

---

## Typical Client Cycle

Every simulation loop, a client should:

```
1.  Read local simulation.conf
2.  Check /api/health  →  server up?
3.    Yes → GET /api/config?hostname=<h>  →  apply updated config
4.         GET /api/scripts/list          →  download any new/changed scripts
5.  Run simulations based on active config
6.  POST /api/status                      →  report current state to spoke
7.  Sleep, repeat
```

Steps 2–4 are performed by `update.sh`. Step 6 is performed by `simulation.sh` on every iteration.

---

## Dashboard-Side Controls (Optional)

The spoke can push overrides to individual clients or all clients at once. Clients receive these overrides automatically when they next call `GET /api/config?hostname=<h>` — no polling required.

| Endpoint | Description |
|---|---|
| `POST /api/clients/{hostname}/control` | Push key/value overrides to a specific client |
| `DELETE /api/clients/{hostname}/control` | Clear all overrides for a specific client |
| `POST /api/clients/all/control` | Push overrides to every connected client |

**Override request body:**

```json
{
  "dns_fail": "on",
  "www_traffic": "off"
}
```

Overrides are in-memory only — they are cleared when the spoke restarts, or when explicitly deleted.

---

## Browser Bootstrap and Local Auth Endpoints

These endpoints are used by the spoke WebUI, not by simulation clients.

### Initial page bootstrap

```
GET /api/init
```

**Response (excerpt):**

```json
{
  "mode": "spoke",
  "installer_version": "1.00",
  "app_version": "1.00",
  "settings": {
    "relay_enabled": "off",
    "relay_server_url": "",
    "hub_tls_verify": "off",
    "hub_managed": false
  }
}
```

`app_version` is the current frontend build version (`VERSION`), and `installer_version` is the installed spoke package version (`INSTALLER_VERSION`).

### Change local admin password

```
POST /api/auth/change-password
Content-Type: application/json
```

**Request body:**

```json
{
  "current_password": "old-password",
  "new_password": "new-password"
}
```

Requires an authenticated local `admin` session.

### Manage local users

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/auth/local-users` | List the built-in `admin` user plus any extra local users |
| `POST` | `/api/auth/local-users` | Create a local user with role `admin` or `viewer` |
| `DELETE` | `/api/auth/local-users/{username}` | Remove a local user (the primary `admin` account cannot be deleted) |

**Create user request body:**

```json
{
  "username": "viewer1",
  "password": "ChangeMeNow!",
  "role": "viewer"
}
```

Hub-driven config sync never overwrites these local auth settings.

### Config editor endpoints

These endpoints are used by the spoke Config UI and standalone editors.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config/overrides` | Returns plain-text `user-overrides.conf` (effective content after any local hub override is merged in) |
| `POST` | `/api/config/overrides/save` | Legacy per-user section save used by the inline override editor; request body `{username, flags}` and response `{status: "ok", pushed: bool}` |
| `GET` | `/api/config/user-overrides-conf` | Returns the full `user-overrides.conf` file as `{content, mode, fetched_at}` |
| `PUT` | `/api/config/user-overrides-conf` | Replaces the full file from `{content: str}` and returns `{ok: bool, pushed: bool}` |

**`GET /api/config/user-overrides-conf` response:**

```json
{
  "content": "[jsmith]\nsimulation_id=s7\n",
  "mode": "local",
  "fetched_at": "2026-05-30T12:34:56+00:00"
}
```

`mode` identifies where the file came from. On a standalone spoke this is currently `local`.

**`PUT /api/config/user-overrides-conf` request body:**

```json
{
  "content": "[jsmith]\nsimulation_id=s7\n"
}
```

**Response:**

```json
{
  "ok": true,
  "pushed": true
}
```

---

## Proxmox Agent Endpoints

These endpoints are used by `proxmox-agent.sh` running on the Proxmox host. They are not called by simulation clients.

### USB Config

```
GET /api/proxmox/usb-config
X-API-Key: <agent_key>
```

**Response:**

```json
{
  "approved_devices": [
    { "vid": "0451", "pid": "16b6", "label": "USB Hub" }
  ],
  "missing_timeout": 60,
  "vh_auto_use_vidpids": ["0451:16b6", "0451:16b7"]
}
```

| Field | Description |
|---|---|
| `approved_devices` | List of USB devices the agent may provision to VMs |
| `missing_timeout` | Seconds before a missing USB device triggers an alert |
| `vh_auto_use_vidpids` | Approved VirtualHere `vid:pid` inventory exposed by the spoke for policy/visibility; the current agent no longer filters locally and instead uses `AUTO USE ALL` |

The current VirtualHere flow relies on server-side sharing/filtering and the agent's `AUTO USE ALL` IPC call, not client-side VID:PID filtering. See the main README for the operational details.

### Proxmox Telemetry

```
POST /api/proxmox/telemetry
X-API-Key: <agent_key>
Content-Type: application/json
```

Along with node, VM, and USB telemetry, the agent now includes a `vh_devices` object generated from `vhclient -t list`.

**`vh_devices` example:**

```json
{
  "vh_devices": {
    "vh_service_active": true,
    "vh_connected": true,
    "auto_use_all": true,
    "count": 2,
    "devices": [
      {
        "name": "802.11ac NIC",
        "address": "QNAP.5134",
        "server": "QNAP:7575",
        "auto_use": true
      }
    ]
  }
}
```

| Field | Description |
|---|---|
| `vh_service_active` | Whether `virtualhereclient.service` is currently active on the Proxmox host |
| `vh_connected` | Whether `vhclient -t list` returned one or more VirtualHere devices |
| `auto_use_all` | Whether the VirtualHere client reports global auto-use mode is on |
| `count` | Number of devices returned in the VirtualHere device list |
| `devices[]` | Per-device details used by the spoke UI tile: hub/server name, device name/address, and `auto_use` state |

---

## WebSocket (Real-Time)

The spoke streams live updates over WebSocket at `ws://<host>:<port>/ws`. Clients do not need to use this — it is intended for the browser UI.

Message types broadcast by the server:
- `full_state` — snapshot of all clients (sent on connect)
- `status_update` — one client's state changed
- `overrides_update` — overrides were applied to a client
- `overrides_cleared` — overrides were removed from a client
- `repo_status` — git sync status changed
- `settings_update` — spoke settings changed
- `central_update` — Aruba Central poll completed; payload now also includes `hardware_alerts` and `client_count_status`
- `version_status` — installer version check result (`current_version`, `available_version`, `last_checked`, `update_available`, `update_in_progress`, `update_error`)
- `relay_status` — relay connection status
