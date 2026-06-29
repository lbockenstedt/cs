# webui-spoke — Client-Sim Spoke Server

`webui-spoke` is the local Client-Sim **spoke** server used in the HPE hub/spoke architecture.

It runs close to the simulation environment — typically in a **Proxmox LXC container** — and provides the local control plane for up to **24 client simulation VMs**. The spoke:

- monitors client VM heartbeats, errors, and simulation state
- serves config and scripts to simulation clients
- tracks multiple connected Proxmox agents independently by canonical hostname
- exposes direct Proxmox VNC console sessions for the local VM Server view
- connects to **Aruba Central** for site, alert, and telemetry context
- relays telemetry and receives commands from **webui-hub** using the hub registration + inbox/ack workflow

`webui-hub` is the central management platform. A superadmin approves each spoke, after which the hub can manage it through tenant-scoped relay endpoints.

---

## Architecture

```text
webui-hub (central)
      |
 HTTPS relay (registration -> approval -> inbox/ack)
      |
webui-spoke (this server — Proxmox LXC)
      |
Proxmox VMs (24x client simulation VMs)
      |
Aruba Central (AP/switch telemetry)
```

### Local responsibilities

- Host the browser UI and local API for the site/lab
- Track simulation client health, overrides, logs, and command state
- Track Proxmox node telemetry, per-agent 1-hour warmup samples, `provision_halt` state, and guest-agent watchdog recovery for the VM Server UI
- Manage `simulation.conf` and `user-overrides.conf` locally when running standalone
- Poll Aruba Central and correlate Central status with local client/site mappings
- Act as the tenant-approved relay endpoint consumer for hub-issued commands

### Hub/spoke responsibilities

- **webui-hub**: multi-tenant central management, approval workflow, fleet view, tenant-scoped relay API
- **webui-spoke**: local execution, local monitoring, local client API, Aruba Central polling, command acknowledgements

> User-facing docs should call this component **webui-spoke** or **spoke**. Current settings and API payloads use `relay_spoke_id` naming.

---

## Quick Start

### Recommended deployment: Proxmox LXC

1. **Create or prepare a Debian 12 LXC**
   - `eth0`: management network / internet access
   - `eth1`: isolated client network for the simulation VMs
2. **Enter the container and run the installer**:

   ```bash
   sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --branch main --port 8000
   ```

   Common alternatives:

   ```bash
   sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --branch main --port 9000
   sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --reinstall
   ```

   The production installer moved from `webui-spoke/install-lxc.sh` to the repo root. The current raw URL is:

   ```text
   https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh
   ```

3. **Attach client VMs to the isolated bridge** used by the spoke.
4. **Point clients at the spoke**:

   ```ini
   [server]
   server_url=http://169.253.1.1:8000
   ```

5. Open the UI in a browser and complete:
   - Aruba Central configuration
   - relay configuration to `webui-hub`
   - notification settings

### What `install-lxc.sh` does

The LXC installer:

1. installs Python, git, and dnsmasq
2. assigns `169.253.1.1/24` to the client-side NIC by default
3. configures dnsmasq to serve DHCP on the client-side NIC only
4. downloads the shared `cs-webui` frontend on the selected branch and injects `WEBUI_MODE=spoke`
5. deploys the FastAPI app and systemd service
6. prints health information and the `server_url` clients should use

By default, DHCP serves `169.253.1.11`–`169.253.1.254` on the isolated client network.

### Unified frontend (`cs-webui`)

`webui-spoke` serves the shared frontend from the `cs-webui` repo rather than maintaining a separate spoke-only UI.

- `install-lxc.sh` fetches `static/js/*`, the legacy `static/app.js` compatibility bundle, `static/style.css`, and `templates/index.html` from `cs-webui` on the same branch selected for the spoke install.
- `server.py` serves the shared HTML template and injects `WEBUI_MODE=spoke` at runtime.
- Use `--branch <name>` to keep the spoke backend and shared frontend aligned (`main` for production).

### Standalone config editors

When a spoke is not hub-managed, it owns both config files directly.

- **Config** edits `simulation.conf` with the same unified collapsible-card renderer used by Hub.
- **Setup → Simulation** opens the same `simulation.conf` editor from the Setup workflow.
- **Config → User Overrides** manages `user-overrides.conf` locally.

If Hub is connected, tenant pushes still win and are written locally as `hub-sim-overrides.conf` and `hub-user-overrides.conf`.

## Proxmox telemetry, warmup, and recovery

The spoke VM Server view now stays aligned with the shared frontend used by Hub:

- a two-level **server list → agent detail** navigation model when more than one Proxmox host is connected
- **VMs**, **USB (T2)**, **IoT (T3)**, **Other**, **VirtualHere**, **Command Queue**, and **Details** sub-tabs per selected host
- per-server CPU/memory average pills plus throttle / `provision_halt` badges on cards and in the detail breadcrumb
- direct VNC launch from the spoke-side VM Server for supported guests
- **IoT (T3)** shows VMs with T3 PCI passthrough rather than raw PCI devices
- **Other** shows non-sim, non-IoT VMs plus containers

### Resource metrics and warmup states

- Host CPU uses a two-sample `/proc/stat` diff taken 1 second apart, so the reported percentage reflects total host load instead of only top's user-space `%us`.
- Memory usage is calculated from `MemTotal` and `MemAvailable` on the Proxmox host.
- The Details view exposes `cpu_1h_avg`, `mem_1h_avg`, `cpu_est_avg`, `mem_est_avg`, `resource_samples_started`, `vmid_range`, and `provision_halt` per agent.
- The shared UI shows three states for the 1-hour averages:
  1. `📊 warming up… <N> min remaining` — the 60-minute sample window has started but no confirmed average exists yet
  2. `📊 CPU avg: ~3.2% (<N> min remaining)` / `Mem avg: ~…` — estimated average from samples collected so far
  3. `📊 CPU avg: 3.2%` / `Mem avg: 41.7%` — confirmed 1-hour rolling average once the full window is available
- `resource_cache.json` persists `cpu_samples`, `mem_samples`, `started`, `agent_version`, and `pve_version` so a restart does not reset the warmup countdown or blank version metadata.
- `check_resource_halt()` runs every loop iteration and caches `{halted, reason, cpu_pct, cpu_threshold, mem_pct, mem_threshold, ts}` so the UI can show current throttle/halt status even between telemetry refreshes.

### Recovery flows

- If a VM misses the post-reboot `update.sh` step because the guest agent is still unavailable, the Proxmox agent puts it into a post-provision retry queue, retries every 10 minutes, and deletes/reclones it after 1 hour if the guest agent never responds.
- Automatic delete decisions require warmed CPU/memory telemetry, remove only the highest VMID when the 1-hour average CPU reaches the delete threshold (default `90%`), and enforce a 5-minute delete cooldown that blocks both further deletes and re-provisioning.
- Every 5 minutes per host, the VMID gap audit checks sequential VMID allocation and queues deletion of the highest VMID above a gap to restore order; this repair path bypasses the normal delete cooldown.
- The VM guest-agent watchdog tracks `qm guest ping` health per VM, marks guests as `agent down`, soft-reboots them after the configured threshold, and reclones them if they remain unresponsive.
- Watchdog state, command queue state, and reclone state are persisted asynchronously to disk so a spoke restart does not lose in-flight recovery context.

---

## Hub Relay Integration

This is the key spoke-to-hub integration that connects a local `webui-spoke` instance to `webui-hub`.

### Enable relay

Configure the spoke in the UI or through `POST /api/settings`:

- set `relay_server_url` to the base URL of `webui-hub`
- set `relay_enabled` to `on`
- optionally set `relay_poll_interval` (default: `60` seconds)
- optionally set `relay_onboarding_psk` when you want the hub to auto-approve the spoke during registration

Example settings payload:

```json
{
  "relay_enabled": "on",
  "relay_server_url": "https://hub.example.com",
  "relay_poll_interval": 60
}
```

### Registration and approval flow

1. On the first relay cycle, the spoke calls:

   ```text
   POST {relay_server_url}/api/spokes/register
   ```

2. The spoke sends its hostname/label plus seed configuration (repo branch, site mappings, monitored checks, hardware checks, USB/reclone settings, etc.).
3. The hub returns an initial registration record.
4. The spoke stores `relay_spoke_id` automatically when the hub returns it.
5. In the standard path, a **superadmin approves the spoke** in `webui-hub`.
6. After approval, the hub returns:
   - `relay_tenant_id`
   - `relay_api_key`
7. The spoke saves both values automatically and switches into approved relay mode.

### PSK-assisted onboarding

Tenant admins can generate an onboarding PSK in **Hub → Setup → Onboarding**. When the spoke installs or registers with a matching tenant ID and PSK, the hub skips the pending-approval queue and returns approved relay credentials immediately.

Recommended install command:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) \
  --hub-url https://hub.example.com:8443 \
  --hub-tenant <tenant-id> \
  --hub-psk <psk>
```

### Relay settings

| Key | Purpose | How it is set |
|---|---|---|
| `relay_server_url` | Base URL for `webui-hub` | Set by operator |
| `relay_enabled` | Turns relay on/off | Set by operator |
| `relay_poll_interval` | Relay loop interval in seconds | Set by operator |
| `relay_spoke_id` | Spoke ID assigned by hub | Auto-set after registration |
| `relay_tenant_id` | Tenant scope assigned by hub | Auto-set after approval |
| `relay_api_key` | API key used for tenant relay calls | Auto-set after approval |

### Approved relay mode

After approval, all hub relay traffic uses tenant-scoped URLs:

```text
/api/{tenant_id}/spokes/{spoke_id}/telemetry
/api/{tenant_id}/spokes/{spoke_id}/inbox
/api/{tenant_id}/spokes/{spoke_id}/ack
```

The spoke relay cycle then:

1. sends telemetry for the current local client state
2. fetches hub inbox commands
3. applies or queues each command locally
4. sends an acknowledgement with status/result payload

### Relayed Proxmox telemetry

The relay payload sent by `_build_relay_telemetry_payload()` includes a `proxmox` object that Hub uses for the shared VM Server views. The relayed fields include:

- node connectivity and timestamps (`connected`, `last_seen`, `node`)
- VM inventory plus per-VM `cpu`, `mem`, `maxmem`, `prov_status`, template flags, USB config state, and T3 PCI passthrough addresses
- USB summary (`usb_state`, `present_usb`, `unknown_usb`, `usb_count`)
- cached version metadata (`agent_version`, `pve_version`)
- resource warmup/average fields (`cpu_1h_avg`, `mem_1h_avg`, `cpu_est_avg`, `mem_est_avg`, `resource_samples_started`)
- template lock, reseed state, hardware-fault summary, and T3 counts

Local-only data stays on the spoke. That includes the raw sample arrays in `resource_cache.json`, local log buffers, and the on-disk retry/watchdog marker files the Proxmox agent uses for recovery.

### Hub-pushed commands

The hub can push these command types through the inbox:

- `config_update` — updates supported spoke settings such as relay config, repo branch, mappings, monitored checks, and selected provisioning settings
- `gkill_switch` — updates the global kill switch state on the spoke
- regular device/proxmox commands — queued into the local command system for clients or the Proxmox agent

Auth settings are intentionally excluded from hub-driven config updates. The hub never stores or pushes `admin_password`, `auth_provider`, or any LDAP, RADIUS, or TACACS fields to the spoke.

Every inbox command receives an ack. For example:

- `config_update` → ack with `status: executed` and a result payload describing the changes applied
- `gkill_switch` → ack with `status: executed` and the resulting switch value
- queued client/proxmox command → ack with `status: queued`

### Local relay endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/relay/trigger` | Manually trigger an immediate relay sync |
| `GET` | `/api/relay/status` | Return current relay state |
| `GET` | `/api/settings` | View current relay-related settings |
| `POST` | `/api/settings` | Update relay configuration |

---

## Configuration

The spoke persists its configuration in `settings.json` and exposes it through `GET /api/settings` and `POST /api/settings`.

Standalone config-file editing is separate from `settings.json`: `simulation.conf` and `user-overrides.conf` live under `configs/`, while hub-managed overrides are written locally as `hub-sim-overrides.conf` and `hub-user-overrides.conf`.

### Common top-level settings

| Key | Description |
|---|---|
| `repo_branch` | Git branch used for the synced Client-Sim repo |
| `repo_sync_interval` | Repo pull interval in seconds |
| `relay_enabled` | `on` or `off` |
| `relay_server_url` | Hub URL |
| `relay_spoke_id` | Hub-assigned spoke ID |
| `relay_tenant_id` | Hub-assigned tenant ID |
| `relay_poll_interval` | Relay polling interval in seconds |
| `relay_onboarding_psk` | Optional tenant PSK used for hub auto-approval |
| `use_all_dongles` | Allow overflow to the other certified dongle type when the preferred type is exhausted |
| `cpu_provision_threshold` / `mem_provision_threshold` | 1-hour average thresholds that block new provisioning while a host is too busy |
| `cpu_delete_threshold` / `mem_delete_threshold` | 1-hour average thresholds used by the delete gate before the highest VMID is torn down |
| `protected_vmids` | Comma-separated VMIDs or ranges (for example `101, 200-90000`) excluded from destructive VM actions |
| `watchdog_reboot_enabled` | `on` or `off`; when `off`, watchdog faults are reported but automatic reboot actions are skipped |
| `site_mappings` | Local `wsite` to Aruba Central site name mapping |
| `monitored_checks` | Central checks to watch per site |
| `hardware_checks` | Hardware alert checks to watch |

### Aruba Central settings (`central_config`)

| Key | Description |
|---|---|
| `api_version` | `classic` or `new_central` |
| `cluster_url` | Central API base URL |
| `access_token` | Classic Central access token |
| `refresh_token` | Optional Classic Central refresh token |
| `client_id` | OAuth client ID / GreenLake API client ID |
| `client_secret` | OAuth client secret |
| `customer_id` | Optional Classic Central customer/tenant ID |

### Notification settings (`notifications`)

| Key | Description |
|---|---|
| `email_enabled` | Enable SMTP email notifications |
| `smtp_host` | SMTP server hostname |
| `smtp_port` | SMTP server port (default `587`) |
| `smtp_user` | SMTP username |
| `smtp_password` | SMTP password |
| `smtp_from` | Sender address |
| `smtp_to` | Recipient list |
| `teams_enabled` | Enable Microsoft Teams notifications |
| `teams_webhook_url` | Teams incoming webhook URL |

### Local auth settings

Spoke-local auth is managed in **Setup → Account**.

- `admin_password` stays local to the spoke and is changed through `POST /api/auth/change-password`.
- Extra local users are stored in `local_users` and managed through `GET/POST/DELETE /api/auth/local-users`.
- Supported local roles are `admin` and `viewer`.
- These auth fields are never overwritten by hub sync.

### Installer and deployment variables

| Variable | Default | Description |
|---|---|---|
| `REPO_URL` | `https://github.com/solutions-hpe/client-sim.git` | Git repo to sync |
| `REPO_BRANCH` | `main` | Branch to sync |
| `INSTALL_DIR` | `/opt/client-sim-dashboard` | App install directory |
| `REPO_CACHE` | `/opt/client-sim-repo` | Local git checkout |
| `SERVICE_USER` | `dashboard` | Service account |
| `PORT` | `8000` | Web server port |
| `OFFLINE_TIMEOUT` | `60` | Seconds before a client is considered offline |
| `DHCP_IFACE` | auto-detect second NIC | Interface used for DHCP |
| `DHCP_GATEWAY` | `169.253.1.1` | Spoke IP on the client network |
| `DHCP_SUBNET` | `169.253.1.0` | Client network subnet |
| `DHCP_PREFIX` | `24` | Client network prefix |
| `DHCP_RANGE_START` | `169.253.1.11` | First DHCP address |
| `DHCP_RANGE_END` | `169.253.1.254` | Last DHCP address |
| `DHCP_LEASE_TIME` | `1h` | DHCP lease duration |

If another device provides DHCP, install without local DHCP:

```bash
DHCP_IFACE="" sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh)
```

---

## API Reference

These are the main local endpoints exposed by `webui-spoke`.

### Core spoke endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Basic health check, including installer version |
| `GET` | `/api/init` | Initial UI bootstrap payload, including `app_version` and `installer_version` |
| `GET` | `/api/settings` | Current spoke settings |
| `POST` | `/api/settings` | Update spoke settings |
| `POST` | `/api/auth/change-password` | Change the local `admin` password |
| `GET` | `/api/auth/local-users` | List local spoke users (`admin` only) |
| `POST` | `/api/auth/local-users` | Add a local spoke user with `admin` or `viewer` role |
| `DELETE` | `/api/auth/local-users/{username}` | Remove a local spoke user |
| `GET` | `/api/clients` | Current client inventory/state. Each client includes `has_usb: bool` for T1/T2 classification |
| `GET` | `/api/simulations` | Grouped simulation view |
| `GET` | `/api/simulations/{sim_id}/clients` | Client list for one simulation/site bucket |
| `GET` | `/api/proxmox/status` | Current multi-host Proxmox state, including live CPU/RAM, per-agent 1-hour avg warmup fields, `vmid_range`, `provision_halt`, agent/pve versions, and VM recovery status |
| `POST` | `/api/proxmox/console/{vmid}` | Create a direct Proxmox VNC console session for the local VM Server UI |
| `GET` | `/api/hardware-alerts` | Current hardware alert summary |
| `POST` | `/api/status` | Client heartbeat/beacon endpoint |
| `GET` | `/api/config?hostname=<h>` | Render effective `simulation.conf` |
| `GET` | `/api/config/overrides` | Return effective plain-text `user-overrides.conf` |
| `GET` | `/api/config/user-overrides-conf` | Return full-file `user-overrides.conf` as `{content, mode, fetched_at}` |
| `PUT` | `/api/config/user-overrides-conf` | Replace the full `user-overrides.conf` file |
| `POST` | `/api/config/overrides/save` | Save one user override section from the editor |
| `GET` | `/api/scripts/list?platform=linux|windows` | List available scripts |
| `GET` | `/api/scripts/{platform}/{filename}` | Download a script |

### Command and relay-adjacent endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/commands` | Queue a command for one client, all clients, or the Proxmox agent |
| `GET` | `/api/commands` | Full local command history |
| `POST` | `/api/commands/cancel-all` | Clear the pending/delivered local command queue used by the Troubleshooting page's **Clear Message Queue** action |
| `GET` | `/api/inbox?hostname=<h>` | Device/agent poll endpoint for pending commands |
| `POST` | `/api/inbox/ack` | Device/agent ack endpoint for command results |
| `POST` | `/api/relay/trigger` | Trigger immediate spoke↔hub relay sync |
| `GET` | `/api/relay/status` | Current relay registration/connection state |
| `GET` | `/api/kill-switch` | Current global kill switch value for clients |
| `GET` | `/api/kill-switch/status` | Detailed kill switch state for UI/debugging |
| `WS` | `/ws` | Real-time browser updates |

For the client-specific request/response details used by simulation VMs, see [`CLIENT_API.md`](CLIENT_API.md).

---

## Deployment

### Proxmox LXC (primary)

This is the intended production deployment model.

- Run the spoke in an LXC container on a Proxmox host
- Use one interface for management and one for the isolated simulation network
- Attach up to 24 simulation VMs to the client-side bridge/network
- Let the spoke provide DHCP on the isolated client segment unless your environment already provides it

#### Common installer commands

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh)
sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --branch main --port 8000
sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --branch main --port 9000
sudo bash <(curl -fsSL https://raw.githubusercontent.com/solutions-hpe/client-sim/main/install-lxc.sh) --reinstall
```

#### CLI flags

| Flag | Description |
|---|---|
| `--branch <name>` | Override `REPO_BRANCH` |
| `--port <number>` | Override `PORT` |
| `--admin-password <value>` | Seed the local spoke `admin` password |
| `--hub-url <url>` | Seed hub relay URL during install |
| `--hub-tenant <id>` | Seed tenant ID/name for onboarding |
| `--hub-psk <token>` | Supply the onboarding PSK for hub auto-approval |
| `--reinstall` | Full wipe and fresh install while preserving backed-up settings |
| `--force` | Reapply hub bootstrap settings during reinstall |

#### Self-update

The installer version is written to `INSTALLER_VERSION` and shown in the UI. The frontend build version is written to `VERSION`. The spoke can check for newer installer versions and re-run the installer in place during self-update, and `server.py` cache-busts `app.js` and `style.css` with `?v=<app_version>` so browsers pick up the refreshed UI immediately after deploy/update.

### Docker (development / lab use)

`docker-compose.yml` builds the local image and exposes port `8000`:

```bash
docker compose up --build
```

Default Docker behavior in this repo:

- builds from the included `Dockerfile`
- exposes `8000:8000`
- sets `REPO_URL`, `REPO_BRANCH`, `REPO_DIR`, and `OFFLINE_TIMEOUT`
- mounts a persistent `repo-cache` volume at `/app/client-sim`

### Python (development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

---

## Aruba Central Integration

`webui-spoke` can correlate local simulation state with Aruba Central telemetry.

### Configure Aruba Central

Use **Setup → Aruba Central Connection** and choose one of the supported modes.

#### Classic Central API

Use for standard Aruba Central deployments.

Required/optional fields:

- `api_version=classic`
- `cluster_url`
- `access_token`
- `refresh_token` *(optional but recommended if available)*
- `customer_id` *(optional)*
- `client_id` / `client_secret` *(optional, depending on token refresh model)*

Example cluster URLs:

- US: `https://apigw-uswest4.central.arubanetworks.com`
- EU: `https://apigw-eucentral3.central.arubanetworks.com`
- APAC: `https://apigw-apacsoutheast1.central.arubanetworks.com`
- Internal: `https://internal-apigw.central.arubanetworks.com`

#### New Central (CNX) API

Use for the HPE GreenLake-based Central platform.

Required fields:

- `api_version=new_central`
- `cluster_url` — e.g. `https://us1.api.central.arubanetworks.com`
- `client_id` / `client_secret` — GreenLake service account credentials
- `customer_id` — the **GreenLake workspace ID** (used in the auth URL, not the Classic Central customer ID)

Authentication uses the HPE GreenLake token service with a `client_credentials` grant:

```
POST https://global.api.greenlake.hpe.com/authorization/v2/oauth2/{customer_id}/token
grant_type=client_credentials
```

Tokens are short-lived (~15 minutes) and refreshed automatically. Do **not** send a `scope` parameter — this causes an `unauthorized_request` error.

Example cluster URLs:

- US: `https://us1.api.central.arubanetworks.com`
- EU: `https://eu1.api.central.arubanetworks.com`

**Browse endpoints used by the spoke in distributed mode:**

| Method | Path | Filter |
|---|---|---|
| `GET` | `/network-notifications/v1/alerts` | `$filter=status eq 'Active' and siteName eq '<site>'` |
| `GET` | `/network-notifications/v1/insights` | paginated, filtered client-side |
| `GET` | `/network-monitoring/v1/devices` | `$filter=siteName eq '<site>'` |
| `GET` | `/network-monitoring/v1/clients` | `$filter=siteName eq '<site>'` |
| `GET` | `/network-monitoring/v1alpha1/sites-health` | no filter — all sites |

#### Distributed mode — browse data collection

In **distributed mode** (spoke assigned to specific site(s) via `site_mappings`), each spoke runs `_fetch_nc_browse_for_spoke()` after every Central poll cycle. This function:

1. Iterates over its assigned Central site names (`site_mappings.values()`)
2. Fetches alerts, insights, devices, and clients filtered to those sites only
3. Stores results in module-level variables (`central_browse_alerts`, `central_browse_insights`, `central_browse_devices_by_site`, `central_browse_clients_by_site`)
4. Includes all four datasets in the telemetry payload under the `central` key

The hub collects browse data from every spoke's telemetry and merges them into a single multi-site view — the DFW spoke feeds DFW data, the MIA spoke feeds MIA data, and so on. This avoids hub-side API fan-out and prevents hitting the Central rate limit (10 calls/second globally across all tokens).

### Site mapping and checks

After Central connectivity works:

1. load local `wsite` values from `simulation.conf`
2. load Central sites
3. map local `wsite` values to Central site names (the UI now auto-adds rows for unmapped local `wsite` values right after **Load Sites**)
4. select monitored checks and hardware checks
5. save the configuration so the spoke can poll and display results locally

Useful Central endpoints:

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/central/test-connection` | Validate configured Central credentials |
| `GET` | `/api/central/available` | Available Central checks/alert types |
| `GET` | `/api/central/sites` | Site list from Central |
| `GET` | `/api/central/status` | Current status for monitored sites/checks |
| `GET` | `/api/central/history` | Historical Central check data |
| `GET` | `/api/central/site-alerts?site=<name>` | Active alerts for a site |

---

## Notification Setup

Notifications are configured in **Setup** and stored under `notifications` in `settings.json`.

The Troubleshooting page also includes a **Message Statistics** card with a **Clear Message Queue** action, and the agent / journal / install logs render newest-first so recent failures stay at the top.

### Microsoft Teams

Configure:

- `teams_enabled=true`
- `teams_webhook_url=<incoming webhook URL>`

### Email / SMTP

Configure:

- `email_enabled=true`
- `smtp_host`
- `smtp_port`
- `smtp_user`
- `smtp_password`
- `smtp_from`
- `smtp_to`

### Test notifications

Use the local API to send a validation message:

```text
POST /api/notifications/test
```

Request field `channel` must be either `email` or `teams`.

---

## Client Connectivity

Simulation clients continue to talk to the **local spoke**, not directly to the hub.

Typical client cycle:

1. `GET /api/health`
2. `GET /api/config?hostname=<hostname>`
3. `GET /api/scripts/list`
4. `GET /api/scripts/{platform}/{filename}` as needed
5. run simulations locally
6. `POST /api/status`
7. optionally poll `/api/kill-switch`

That local-first design keeps the simulation environment working even if the hub is unavailable. Relay sync to `webui-hub` is additive and separate from the client VM API.

