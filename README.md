# client-sim

![Version](https://img.shields.io/badge/version-autobump%20.NN-blue)

> **Current architecture — read this first.** The active LM spoke is **`lm-spoke/`** (`CSSpoke`, `module_type="simulation"`), **relay-only** for Proxmox. It runs mainly as the **`simulation`** role hosted by the generic agent (unit `lm-agent`, sub-spoke `{agent}-simulation`); the dedicated `lm-cs.service` / `lm-spoke/install_cs.sh` path is the legacy/standalone alternative. The installer is **`install_cs.sh`** (top-level wrapper → `lm-spoke/install_cs.sh`), which provisions a **cs-owned Kea** DHCP4 instance (`kea-dhcp4-sim`) on the 2nd NIC — not dnsmasq. The auto-provisioning **brain** lives in the **pxmx agent** (`pxmx/agent/src/usb_provision.py`), not in this repo's `proxmox/`. The full, canonical feature reference (ports, env vars, handlers, the two-flag auto-provisioning trap, troubleshooting) lives in **[`docs/cs.md`](docs/cs.md)** and the canonical **[`lm/docs/cs.md`](../lm/docs/cs.md)**. The rest of this README documents the **legacy `webui-spoke`** path and is preserved for operators of that standalone deployment.

`client-sim` contains the spoke-side runtime for the HPE Client-Sim platform. The repo version is the autobumped `VERSION` file (`.NN` scheme — a version-bump bot handles the last segment; do not bump manually). The runtime surfaces:

- the **active LM-native spoke** (`lm-spoke/` — `CSSpoke`, `CSControlPlane`, the client API, sim engine, registry, command queue, relay-only Proxmox deploy, cs-owned Kea DHCP)
- the **legacy/standalone spoke backend** (`webui-spoke/`) and its `installers/install-lxc.sh`
- the **legacy Proxmox host agent** (`proxmox/`) — the auto-provisioning brain has moved to the pxmx repo; this bundle remains for the legacy/Azure-backup deployment
- the **Linux simulation scripts** (`clients/linux/`), Windows equivalents (`clients/windows/`), T3 (`clients/t3/`), and installer assets

This repo is the local execution plane. It can run standalone, or relay telemetry and commands to Hub. The legacy `proxmox/` bundle also carries the Azure-backed VM backup/reseed path used by hub-managed Proxmox environments.

---

<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### CS spoke — `install_cs.sh`

The canonical entrypoint. It is a thin wrapper: the real logic lives in
`lm-spoke/install_cs.sh` (single source of truth) and the wrapper `exec`s it,
fetching it from GitHub when there is no checkout beside it.

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/install_cs.sh \
  | sudo bash -s -- --hub lm-hub.lrbtechnologies.com
```

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | Hub WebSocket URL. A bare host is fine — `lm-hub.example.com` becomes `wss://lm-hub.example.com:443`, `host:port` gets a `wss://` prefix, `ws://`/`wss://` and the `auto` sentinel are left alone. Omit it to auto-discover (DNS `lm-hub.<suffix>`, then mDNS). |
| `--id`, `--name` | Pin the spoke id. Omitted, the id derives from the hostname at startup, so a cloned and renamed container reconnects under its new name. |
| `--secret` | Pre-shared spoke secret. |
| `--hub-secret` | Hub PSK for auto-approval. Without it every clone waits for manual approval in the WebUI. |
| `--dhcp-iface IFACE` | NIC for the sim-client DHCP scope. Auto-detected otherwise. |
| `--no-dhcp` | Skip DHCP setup entirely. |
| `--tls-verify` | Verify the hub certificate. **Requires `--tls-ca-cert`** — the installer refuses `--tls-verify` on its own, even against a publicly-trusted certificate. |
| `--tls-ca-cert PATH` | CA certificate for that verification. |
| `--agent-listener` | Enable the client API listener. Already the default; a harmless no-op. |
| `--no-agent-listener` | Disable the client API listener. |
| `--infra-only` | Host-level infrastructure only — no spoke runtime. Used when the box hosts the `simulation` role via the generic agent. |
| `--purge-env`, `--reset-identity` | Delete the existing `.env` first, regenerating the secret and `INSTALL_UUID`. The spoke then re-registers and needs approval. |
| `--clone`, `--prep-clone` | Golden-image prep: full install, no identity minted, unit enabled but not started. Ignores `--id` — each clone derives its own from its hostname. |
| `--admin-token`, `--all-prereqs` | Deprecated, accepted and ignored. |

**Environment overrides:** `HUB_URL`, `SPOKE_ID`, `DHCP_IFACE`, `DHCP_SUBNET`,
`DHCP_PREFIX`, `DHCP_GATEWAY`, `DHCP_RANGE_START`, `DHCP_RANGE_END`,
`DHCP_LEASE_TIME`, `DHCP_SKIP` (`1` to skip DHCP).

Re-running preserves the spoke secret and `INSTALL_UUID`, so the spoke keeps its
identity. DHCP is a **cs-owned Kea instance** (`kea-dhcp4-sim`) on a second NIC,
separate from the `lm/dhcp` module's Kea so both coexist on an all-in-one box; it
serves `169.253.1.0/24` with no router option. The client API listens on
**8080** — the hub already owns `:8000`.

### Proxmox host agent — `proxmox/install-proxmox-agent.sh`

Runs **on the Proxmox host**. Omit `--server` and it finds LXC **1001**, resolves
its IP and points itself at `http://<spoke-ip>:8000`.

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/proxmox/install-proxmox-agent.sh | bash -s --
```

| Flag | Purpose |
| :--- | :--- |
| `--server URL` | Spoke API URL. Given explicitly, LXC-1001 auto-detection is never attempted, and it wins over the stored value on reinstall. |
| `--key` | Agent API key. |
| `--interval N` | Poll interval in seconds. Default `60`. |
| `--branch` | Repo branch to self-update from. Default `main`. |
| `--hub-url URL` | Hub URL, used to fetch the Azure SAS token for VM image pulls. |
| `--installer-key` | Paired with `--hub-url`. Without it the SAS fetch is skipped and it falls back to direct Azure URLs. |
| `--tenant-id UUID` | Tenant id. |
| `--unattended` | Non-interactive. |

**Environment overrides:** `CLIENT_SIM_AGENT_PORT` (listener, default `9105`),
`CLIENT_SIM_INSTALLER_KEY`, `REPO_BRANCH`.

Re-running reads `/etc/client-sim-proxmox-agent.env` and keeps the existing key,
interval, branch and port unless you override them.

> The auto-provisioning **brain** lives in the pxmx agent
> (`pxmx/agent/install_agent.sh`), not here. This bundle is the legacy/Azure-backup
> deployment.

### Linux simulation client — `installers/install.sh`

Run on the client VM. It reaches the spoke over the sim DHCP network
(`169.253.1.1:8080`), so it needs no server argument.

```bash
sudo bash installers/install.sh
```

| Flag | Purpose |
| :--- | :--- |
| `--debug`, `-d` | Verbose install logging. |

### Windows simulation client — `clients/windows/install.ps1`

```powershell
.\install.ps1 -SpokeIp 169.253.1.1
```

| Parameter | Default | Purpose |
| :--- | :--- | :--- |
| `-SpokeIp` | `169.253.1.1` | Spoke address. |
| `-InstallDir` | `C:\Scripts` | Install location. |
| `-Branch` | `main` | Repo branch. |
| `-RepoUrl` | `https://github.com/lbockenstedt/cs.git` | Source repo. |
| `-AutoLogonUser` | *(empty)* | Enable auto-logon as this user. |
| `-AutoLogonPassword` | *(empty)* | Password for auto-logon. |
| `-SkipDeps` | *(off)* | Skip dependency installation. |

### T3 wireless client — `clients/t3/install.sh`

Run as root on a fresh headless Debian/Ubuntu box. Installs into `/usr/scripts`,
writes `/etc/udev/rules.d/90-Wireless.rules`, and optionally enables the
`t3-simulation` service for headless autostart. No flags.

```bash
sudo bash clients/t3/install.sh
```

### Legacy standalone spoke — `installers/install-lxc.sh`

The pre-LM `webui-spoke` deployment. Prefer `install_cs.sh` for anything new.

| Flag | Purpose |
| :--- | :--- |
| `--reinstall`, `-r` | Full wipe and reinstall. |
| `--force`, `-f` | Implies `--reinstall`. |
| `--unattended` | Non-interactive. |
| `--branch`, `-b` | Repo branch. |
| `--port`, `-p` | Listener port. |
| `--admin-password` | Local admin password. |
| `--hub-url` | Legacy hub URL. |
| `--hub-tenant` | Tenant name or UUID. A name is resolved via the hub API, which needs `--hub-url`, `--hub-user` and `--hub-password`. |
| `--hub-user` | Hub username, for that lookup. |
| `--hub-password` | Hub password. |
| `--hub-psk` | Hub pre-shared key. |
| `--lm-hub-url` | LM hub URL. |
| `--lm-hub-secret` | LM hub secret. |
| `--lm-core-path` | Path to the LM core checkout. |
| `--help`, `-h` | Usage. |

Every flag also accepts `--flag=value`.

**Environment overrides:** `REPO_URL`, `REPO_BRANCH`, `INSTALL_DIR`, `REPO_CACHE`,
`SERVICE_USER`, `PORT`, `OFFLINE_TIMEOUT`, `DHCP_IFACE`, `DHCP_SUBNET`,
`DHCP_GATEWAY`, `DHCP_RANGE_START`, `DHCP_RANGE_END`, `DHCP_LEASE_TIME`.

### Local WebUI hub — `webui-local/install.sh`

```bash
sudo bash webui-local/install.sh
```

No flags. **Environment overrides:** `INSTALL_DIR` (default `/opt/hub`),
`DATA_DIR` (`/var/lib/hub`), `HUB_PORT` (`8443`), `ADMIN_PASSWORD` (`changeme`),
`SECRET_KEY`, `WEBUI_SECRET_KEY`, `PYTHON`.

### Uninstallers

```bash
sudo bash uninstall_cs.sh --yes
sudo bash proxmox/uninstall-proxmox-agent.sh --yes
```

`uninstall_cs.sh` — `--yes`, `--purge-logs`, `--remove-dhcp`, `--hub-api URL`,
`--spoke-id ID`. It never removes assets shared with other LM services: the
`svc_lm` user, `/opt/lm/core`, `/var/log/lm`, or the `dhcp` module's own Kea.

`proxmox/uninstall-proxmox-agent.sh` — `--yes`, `--purge-logs`, `--purge-pve-scripts`.
<!-- INSTALLERS:END -->

## Operators

### What this repo provides

At an operational level, this repo gives you:

- a FastAPI spoke dashboard/API inside a Proxmox LXC
- a Proxmox host agent for VM, USB, USB quarantine, reclone, post-provision retry, and guest-agent watchdog operations
- Linux VM scripts that fetch config, run simulations, and report status
- multi-Proxmox spoke support, with each connected host tracked independently by canonical hostname
- direct Proxmox VNC console workflows exposed through the spoke VM Server
- accurate per-agent Proxmox CPU/memory telemetry with 1-hour rolling averages, warmup estimates, provision-halt state, and hub relay support
- backpressure throttling so sim clients slow down under heavy API/WebSocket load instead of overwhelming the spoke
- watchdogs for both the spoke service and Proxmox agent
- configuration-driven simulation behavior using INI files
- shared hub-style spoke config editors for `simulation.conf` and `user-overrides.conf`
- 7-day client-count baseline monitoring persisted in `client_count_7day.json`
- local spoke auth management with password rotation and extra admin/viewer users

### Spoke installation (`install-lxc.sh`)

#### What the installer does

`install-lxc.sh` is the spoke installer and updater. In the current repo it:

1. validates OS and flags
2. installs system packages
3. auto-detects a second NIC and configures `dnsmasq` DHCP when present
4. clones or updates the `client-sim` repo cache
5. deploys the spoke app files into `/opt/client-sim-dashboard`
6. fetches shared frontend assets from `cs-webui`
7. creates a Python virtual environment and installs `requirements.txt`
8. writes or updates `.env`
9. installs the `client-sim-dashboard` systemd service and web UI watchdog
10. starts or restarts the service

#### Prerequisites

Before you run it, have:

- a Debian or Ubuntu LXC
- root or `sudo` access
- outbound access to GitHub
- a Proxmox-attached second NIC if you want the isolated DHCP network

#### Install commands

The production installer lives at `installers/install-lxc.sh` and should be fetched from:

```text
https://raw.githubusercontent.com/lbockenstedt/cs/main/installers/install-lxc.sh
```

Standard install/update:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/lbockenstedt/cs/main/installers/install-lxc.sh) --branch main
```

Full wipe/reinstall:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/lbockenstedt/cs/main/installers/install-lxc.sh) --reinstall --branch main
```

Custom port example:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/lbockenstedt/cs/main/installers/install-lxc.sh) --branch main --port 9000
```

Hub onboarding with tenant PSK auto-approval:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/lbockenstedt/cs/main/installers/install-lxc.sh) \
  --hub-url https://hub.example.com:8443 \
  --hub-tenant <tenant-id> \
  --hub-psk <psk>
```

Local checkout usage still works too:

```bash
cd /opt/client-sim-repo
sudo bash install-lxc.sh --branch main
```

#### What to verify after install

> **Port note:** `install-lxc.sh` defaults to **port 8000** (override with `--port`). The `:8080` examples below assume you passed `--port 8080` (which is the canonical `lm-spoke` `CS_API_PORT` default). Adjust the port to match your install.

```bash
systemctl status client-sim-dashboard
curl http://localhost:8080/api/health
cat /var/log/client-sim-dashboard-install.log
```

### Proxmox agent installation (`proxmox/install-proxmox-agent.sh`)

#### What the installer does

The Proxmox agent installer:

1. downloads the latest agent, watchdog, and installer scripts from GitHub
2. installs the systemd service and timer units
3. writes `/etc/client-sim-proxmox-agent.env`
4. installs the VirtualHere USB client, first reusing any existing binary found under root's `~/.local`, `/opt`, or `/home`, symlinking it to `/usr/sbin/vhclient`, rebuilding `virtualhereclient.service` with the real binary path, and clearing any stale `override.conf` on reinstall
5. prepares watchdog state
6. enables and starts the service and watchdog timer
7. checks spoke API reachability

#### Step-by-step install

Run this on the **Proxmox host**, not inside the LXC.

1. Install from GitHub (recommended for production `main`) or from a local checkout.

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/proxmox/install-proxmox-agent.sh | sudo bash -s -- \
  --server http://169.253.1.1:8080 \
  --branch main \
  --hub-url https://<hub-host>:8443 \
  --tenant-id <tenant-id> \
  --installer-key <installer-api-key>
```

```bash
cd /opt/client-sim-repo/proxmox
sudo bash install-proxmox-agent.sh \
  --server http://169.253.1.1:8080 \
  --branch main \
  --hub-url https://<hub-host>:8443 \
  --tenant-id <tenant-id> \
  --installer-key <installer-api-key>
```

`--hub-url` and `--tenant-id` let the installer seed spoke-to-hub settings through the spoke's localhost-only `/api/bootstrap` endpoint. `--installer-key` is the shared secret Hub expects on `X-Installer-Key` when the installer requests an Azure SAS URL for private blob downloads.

2. Check that the service started.

```bash
systemctl status client-sim-proxmox-agent
systemctl status proxmox-watchdog.timer
journalctl -u client-sim-proxmox-agent -f
```

3. Approve the agent from the spoke UI, or by API:

```bash
curl -X POST http://169.253.1.1:8080/api/proxmox/approve/<hostname>
```

4. Confirm the agent received a key and is posting telemetry.

```bash
cat /etc/client-sim-proxmox-agent.env
curl http://169.253.1.1:8080/api/proxmox/status
```

#### Common installer flags

| Flag | Meaning |
|---|---|
| `--server <url>` | Required spoke URL used by the Proxmox agent for local API access; supplying it skips the legacy LXC `1001` lookup during install |
| `--hub-url <url>` | Optional Hub URL used for one-time spoke bootstrap and installer SAS requests |
| `--tenant-id <id>` | Optional tenant identifier passed to the spoke bootstrap flow |
| `--installer-key <key>` | Optional shared secret sent as `X-Installer-Key` when requesting the installer SAS token from Hub |
| `--key <api_key>` | Pre-seed an API key instead of waiting for approval |
| `--interval <seconds>` | Override poll interval |
| `--branch <name>` | Branch to pull scripts from (default: `main`) |
| `--unattended` | Automation-friendly mode |
| `--skip-vh` | Skip VirtualHere client installation |

#### Azure VM backup and reseed

In v1.0, the Proxmox agent can receive `backup` and `reseed` commands over its WebSocket client and execute them asynchronously. Operationally:

- `run_backup_command()` creates a `vzdump` snapshot, then uploads the artifact to private Azure Blob Storage in `csvmstorage/vms` with `azcopy`.
- `run_reseed_command()` downloads the selected blob URL, restores the VM with `qmrestore`, converts the template when needed, and reclones VMs.
- Progress is reported back to Hub with `backup_progress` and `reseed_progress` messages so Hub users can watch long-running jobs.
- Installers and restore flows request a short-lived, read-only SAS URL from Hub instead of embedding the raw Azure account key on the Proxmox host.

#### Branch override config

At startup, `install-proxmox-agent.sh` tries to download `proxmox/installer-override.conf` from the selected branch and silently skips it on `404`. When present, that file can override values such as `AZURE_ACCOUNT`, `OVERRIDE_HUB_URL`, `OVERRIDE_TENANT_ID`, and `OVERRIDE_SERVER_URL`.

On production `main`, `installer-override.conf` is intentionally **not** shipped. Production installs will silently skip the override download with no effect.

### Proxmox telemetry, warmup, and recovery

Recent Proxmox-side changes add these operator-visible behaviors:

- **Multi-Proxmox host tracking** — multiple agents can connect to one spoke at the same time, and `webui-spoke/server.py` keeps each host in `proxmox_states` keyed by canonical hostname so cards, drill-down pages, and commands stay scoped to the correct node.
- **Accurate host CPU** — `proxmox-agent.sh` samples `/proc/stat` twice, 1 second apart, and reports `(1 − Δidle / Δtotal) × 100`. This captures total host CPU load (user, nice, system, iowait, irq, softirq), not just top's user-space `%us` value.
- **Per-agent 1-hour CPU and memory averages** — `webui-spoke/server.py` records rolling CPU and memory samples per host and exposes both confirmed 1-hour averages and warmup estimates.
- **Three warmup UI states** — the spoke Details tab and hub Details view show: `📊 warming up… <N> min remaining` when no samples exist yet, `📊 CPU avg: ~3.2% (<N> min remaining)` / `Mem avg: ~…` while the 1-hour window is still filling, and `📊 CPU avg: 3.2%` / `Mem avg: 41.7%` once the full 60-minute window is available.
- **Persisted warmup cache** — `webui-spoke/resource_cache.json` stores `cpu_samples`, `mem_samples`, `started`, `agent_version`, and `pve_version` so a spoke restart does not reset the warmup countdown or blank the Details version rows.
- **Provision halt telemetry + dual CPU gate** — `check_resource_halt()` runs on every main-loop pass, writes a cache entry with `{halted, reason, cpu_pct, cpu_threshold, mem_pct, mem_threshold, ts}`, and feeds the UI halt/throttle badges. Provision pacing now uses both an instantaneous ramp ceiling and an inter-clone pacing ceiling.
- **Delete gate hardening (v1.26–v1.28)** — automatic deletes tear down only the highest VMID when the 1-hour average CPU is at or above the delete threshold (default `90%`), require warmed CPU/memory telemetry before any allow/deny decision, and enforce a 5-minute cooldown after any delete that blocks both further deletes and re-provisioning. Done/failed `prov_run` items also clear stale `provisioning` state automatically.
- **VMID gap audit (v1.28)** — every 5 minutes per host, the agent checks the reported `vmid_range` for gaps in sequential VMID assignment and queues deletion of the highest VMID above the gap to restore order. This repair path intentionally bypasses the normal delete cooldown.
- **Recovery queues and watchdogs** — if a VM misses the post-reboot `update.sh` step because the guest agent is still unavailable, the agent retries every 10 minutes and destroys/reclones the VM after 1 hour. Separately, the VM guest-agent watchdog monitors `qm guest ping`, soft-reboots unresponsive guests, and reclones them if they stay down beyond the configured escalation thresholds.

When relay is enabled, Hub receives per-host `agent_version`, `pve_version`, `cpu_1h_avg`, `mem_1h_avg`, `cpu_est_avg`, `mem_est_avg`, `resource_samples_started`, `vmid_range`, `provision_halt`, and per-VM `cpu` / `mem` / `maxmem` fields through `_build_relay_telemetry_payload()`. The raw sample history itself stays local on the spoke in `resource_cache.json`.

### USB allocation policies

Spoke USB auto-provisioning is configured in **Setup → Proxmox**.

- `sim_phy` now accepts `wireless`, `ethernet`, or `any`.
- `sim_phy=any` allows any certified dongle type to be provisioned, and the Proxmox workflow writes the VM's effective `sim_phy` to match the actual dongle type attached.
- **Use All Available Dongles** lets the spoke or hub overflow to the other certified dongle type when the preferred type is exhausted.
- `protected_vmids` accepts comma-separated VMIDs and inclusive ranges such as `100-90000`; those guests are excluded from UI-driven delete/reclone/start/stop actions, including **Delete All Sim VMs**.
- USB bus quarantine state is carried through spoke and hub telemetry so the bash agent can suppress unstable or unwanted buses without losing visibility.
- The hub tenant setting lives in **Setup → Tenant Setup** and the local spoke override lives in **Setup → Proxmox**.

### VirtualHere auto-use sync

Once the Proxmox agent is approved and polling, it keeps the local VirtualHere client aligned by sending `AUTO USE ALL` over the VirtualHere IPC interface.

**How it works now:**

1. `_find_vhclient()` searches root's `~/.local`, `/opt`, and `/home` recursively before falling back to canonical locations such as `/usr/sbin/vhclient`.
2. `_apply_vh_auto_use()` makes sure `virtualhereclient.service` is pointed at the discovered binary, starts the service if needed, then sends `AUTO USE ALL`.
3. `_sync_vh_auto_use()` runs every agent cycle and only re-applies when the VirtualHere service is not active.
4. There is no client-side VID:PID filtering anymore; device filtering is done on the VirtualHere server side (for example on the QNAP that is sharing devices).

Operationally, this means the spoke-side agent claims every device the VH server exposes, while the VH server remains the source of truth for which devices are actually shared.

To skip VH installation entirely, pass `--skip-vh` to the installer. The auto-use sync is a no-op if `virtualhereclient.service` is not installed.

### Local spoke auth

The spoke now includes **Setup → Account** for local authentication management.

- **Change Password** rotates the primary local `admin` account password.
- **Local Users** lets spoke admins add or remove extra local users with `admin` or `viewer` roles.
- Hub-driven config sync never overwrites spoke auth settings: `admin_password`, `auth_provider`, and all LDAP/RADIUS/TACACS fields remain local-only on the spoke.

### Standalone spoke configuration

When a spoke is running without Hub relay, it manages its own config files locally.

- **Config** tab: edit `simulation.conf` using the shared hub-style renderer. `[simulation]`, `[server]`, `[address]`, and each `s0`–`s9` slot render as collapsible cards with text/select fields in a responsive grid and boolean flags inline.
- **Setup → Simulation**: opens the same `simulation.conf` editor from the Setup workflow.
- **Config → User Overrides**: manage `user-overrides.conf` locally from the spoke UI.

When the spoke is hub-connected, tenant pushes from Hub take precedence and are written locally as `configs/hub-sim-overrides.conf` and `configs/hub-user-overrides.conf`.

### Configuration files

The two main operator-edited files are:

- `configs/simulation.conf`
- `configs/user-overrides.conf`

Operators can edit these directly in GitHub, through Hub when relay-managed, or from the spoke UI when running standalone.

Resolution order on a client VM is:

```text
[simulation] globals
  -> [s0]-[s9] bucket profile selected by zlib.crc32(hostname) % 10
  -> [username] override from user-overrides.conf
  -> usb-phy-override.conf for sim_phy when provisioned by Proxmox agent
```

#### `simulation.conf` global keys

| Section | Key | Default in repo | Description |
|---|---|---:|---|
| `[simulation]` | `kill_switch` | `off` | Local emergency stop for the client loop |
| `[simulation]` | `rapid_update` | `on` | Run `update.sh` every loop instead of only at exec-restart checkpoints |
| `[simulation]` | `sim_load` | `100` | Probability/CPU-style load gate for enabled simulations |
| `[simulation]` | `github_repo` | `on` | Allow GitHub as an update source |
| `[simulation]` | `repo_location` | `https://github.com/lbockenstedt/cs/` | Git repo used by update logic |
| `[simulation]` | `repo_branch` | `main` | Branch used by client update logic |
| `[simulation]` | `smb_repo` | `off` | Enable SMB as a fallback update source |
| `[simulation]` | `vh_server` | `off` | Start/use VirtualHere workflow |
| `[simulation]` | `site_based_ssid` | `on` | Prefix `wsite-` to the SSID when connecting |
| `[simulation]` | `reboot_schedule` | `300` | Base reboot schedule in minutes |
| `[simulation]` | `allow_offline` | `no` | Take interfaces down for a random offline period between 100-iteration cycles |
| `[simulation]` | `ssidpw_fail` | `off` | Global default for wrong-PSK simulation |
| `[simulation]` | `auth_fail` | `off` | Global default for 802.1X auth-failure simulation |
| `[simulation]` | `dot1x_password` | `password` | Base 802.1X password |
| `[simulation]` | `dot1x_eap` | `peap` | 802.1X EAP method used by the DOT1X helper |
| `[simulation]` | `iperf_bw` | `1k` | iPerf target bandwidth |
| `[simulation]` | `syslog` | `on` | Forward logs to the configured syslog target |
| `[simulation]` | `web_server` | `on` | Use the spoke API as the primary source for config/scripts |

#### `simulation.conf` server and address keys

| Section | Key | Default in repo | Description |
|---|---|---:|---|
| `[server]` | `server_url` | `http://169.253.1.1:8080` | Spoke URL used by clients for health/config/scripts/status/inbox |
| `[address]` | `smb_address` | `//nas/scripts` | SMB fallback path for updates |
| `[address]` | `ping_address` | `172.31.201.3` | Ping target for traffic testing |
| `[address]` | `dns_latency_1` | `13.239.88.95` | DNS latency target 1 |
| `[address]` | `dns_latency_2` | `27.110.152.250` | DNS latency target 2 |
| `[address]` | `dns_latency_3` | `165.246.10.2` | DNS latency target 3 |
| `[address]` | `dns_bad_ip_1` | `10.0.0.1` | Bad DNS response IP 1 |
| `[address]` | `dns_bad_ip_2` | `172.16.0.1` | Bad DNS response IP 2 |
| `[address]` | `dns_bad_ip_3` | `192.168.0.1` | Bad DNS response IP 3 |
| `[address]` | `dns_bad_record_1` | `172.31.201.1` | Wrong-record target 1 |
| `[address]` | `dns_bad_record_2` | `172.31.202.2` | Wrong-record target 2 |
| `[address]` | `dns_bad_record_3` | `100.100.0.1` | Wrong-record target 3 |
| `[address]` | `iperf_server` | `172.31.201.135` | iPerf target |
| `[address]` | `syslog_server` | `169.253.1.5` | Remote syslog target |

#### Bucket-profile and user-override keys

These keys are valid in `[s0]`-`[s9]` bucket sections and in `[username]` sections inside `user-overrides.conf`.

| Key | Shipped example/default | Description |
|---|---|---|
| `central_check` | blank | Aruba Central alert/check id expected for this simulation |
| `wsite` | `DFW` or `MIA` in shipped sample | Site name used for SSID prefixing and Central correlation |
| `ssid` | `PSK` | Base SSID name |
| `ssidpw` | `PassW0rd!` | WPA PSK |
| `dhcp_fail` | `off` | DHCP-failure simulation |
| `dns_fail` | `on` in most shipped buckets | DNS-failure simulation |
| `ssidpw_fail` | `on` in the shipped user examples | Wrong-PSK authentication failure simulation |
| `auth_fail` | `off` in the shipped user examples | 802.1X authentication-failure simulation |
| `assoc_fail` | `off` | Association-failure simulation |
| `port_flap` | `off` | Wired port flap simulation |
| `ping_test` | `on` | ICMP traffic generation |
| `download` | `on` | HTTP download traffic generation |
| `www_traffic` | `on` | Browser/web traffic generation |
| `iperf` | `off` except `s2=on` in shipped sample | iPerf bandwidth generation |
| `sim_phy` | `wireless` | Expected physical medium: `wireless`, `ethernet`, or `any`; when `any` is used for USB provisioning, the guest override is rewritten to the actual certified dongle type |
| `l1` | `no` | If `yes`, Proxmox agent adds an L1 VLAN NIC for that bucket |
| `kill_switch` | `off` in user override examples | User-specific local kill switch override |
| `sim_load` | `100` in user override examples | User-specific load override |
| `github_repo` | `on` in user override examples | User-specific GitHub-source override |
| `repo_location` | repo URL in examples | User-specific update source override |
| `repo_branch` | `main` in examples | User-specific branch override |
| `vh_server` | `off` in examples | User-specific VirtualHere override |
| `site_based_ssid` | `on` in examples | User-specific SSID prefix override |
| `simulation_id` | unset | Pin to a specific bucket (`s0`–`s9`), overriding the hostname hash |
| `reboot_schedule` | `300` in examples | User-specific reboot timing override |
| `iperf_bw` | `1k` in examples | User-specific iPerf target |
| `smb_address` | `//nas/scripts` in examples | User-specific SMB fallback path override |
| `ping_address` | `172.31.201.1` in examples | User-specific ping target override |
| `dns_latency_1` | `13.239.88.95` in examples | User-specific DNS latency target 1 override |
| `dns_latency_2` | `27.110.152.250` in examples | User-specific DNS latency target 2 override |
| `dns_latency_3` | `165.246.10.2` in examples | User-specific DNS latency target 3 override |
| `dns_bad_ip_1` | `10.0.0.2` in examples | User-specific bad DNS IP 1 override |
| `dns_bad_ip_2` | `172.16.0.2` in examples | User-specific bad DNS IP 2 override |
| `dns_bad_ip_3` | `192.168.0.2` in examples | User-specific bad DNS IP 3 override |
| `dns_bad_record_1` | `172.31.201.1` in examples | User-specific wrong-record target 1 override |
| `dns_bad_record_2` | `172.31.202.2` in examples | User-specific wrong-record target 2 override |
| `dns_bad_record_3` | `100.100.0.1` in examples | User-specific wrong-record target 3 override |
| `iperf_server` | `172.31.201.135` in examples | User-specific iPerf target override |
| `dot1x_password` | commented example | User-specific 802.1X password |
| `dot1x_eap` | commented example | User-specific 802.1X EAP method |

#### Shipped bucket summary

| Bucket | wsite | Key behavior in shipped repo |
|---|---|---|
| `s0` | `DFW` | DNS fail + ping/download/web |
| `s1` | `DFW` | Normal traffic profile |
| `s2` | `DFW` | DNS fail + iPerf + traffic |
| `s3` | `DFW` | DNS fail + traffic |
| `s4` | `MIA` | DNS fail + traffic |
| `s5` | `DFW` | DNS fail + traffic |
| `s6` | `MIA` | DNS fail + traffic |
| `s7` | `DFW` | DNS fail + traffic |
| `s8` | `MIA` | DNS fail + traffic |
| `s9` | `DFW` | Normal traffic profile |

#### `user-overrides.conf`

`user-overrides.conf` is optional and loaded after `simulation.conf`. Only set keys you want to override.

Example:

```ini
[slynch]
dns_fail=off
ssidpw_fail=on
wsite=DFW
```

### Watchdogs

#### Spoke watchdog

- timer unit: `webui-watchdog.timer`
- service unit: `webui-watchdog.service`
- log file: `/var/log/webui-watchdog.log`
- state counter: `/var/lib/webui-watchdog/state`

Checks:

```bash
systemctl status webui-watchdog.timer
systemctl status webui-watchdog.service
cat /var/log/webui-watchdog.log
```

Behavior:

- failure 1: log only
- failure 2: restart `client-sim-dashboard`
- failure 5+: rerun `install-lxc.sh --unattended`

#### Proxmox watchdog

- timer unit: `proxmox-watchdog.timer`
- service unit: `proxmox-watchdog.service`
- log file: `/var/log/proxmox-watchdog.log`
- state counter: `/var/lib/proxmox-watchdog/state`

Checks:

```bash
systemctl status proxmox-watchdog.timer
systemctl status proxmox-watchdog.service
cat /var/log/proxmox-watchdog.log
```

Behavior:

- every 5 minutes, independently checks `virtualhereclient.service`; if it is not active, restarts it and reports a `vh_restart` event to the spoke API
- failure 1: log/report failure
- failure 2: restart `client-sim-proxmox-agent`
- failure 5+: rerun the Proxmox agent installer

The VirtualHere restart path is separate from the Proxmox agent failure counter.

### Spoke WebUI and operator-useful API endpoints

#### Health and status

```bash
curl http://localhost:8080/api/health
curl http://localhost:8080/api/services/status
curl http://localhost:8080/api/system/health
curl http://localhost:8080/api/version
```

#### Client and config views

```bash
curl http://localhost:8080/api/clients
curl "http://localhost:8080/api/config?hostname=<client-hostname>"
curl http://localhost:8080/api/config/overrides
curl http://localhost:8080/api/config/parsed
curl "http://localhost:8080/api/scripts/list?platform=linux"
```

#### Proxmox views

```bash
curl http://localhost:8080/api/proxmox/status
curl http://localhost:8080/api/proxmox/pending
curl http://localhost:8080/api/proxmox/approved
curl http://localhost:8080/api/proxmox/reclone-status
curl http://localhost:8080/api/proxmox/usb-config
curl -X POST http://localhost:8080/api/proxmox/console/<vmid>
```

`POST /api/proxmox/console/{vmid}` creates a direct Proxmox VNC console session for the spoke VM Server view; the browser then opens `/console?session_id=<id>` and bridges over `WS /ws/console/{session_id}`.

#### Relay and repo views

```bash
curl http://localhost:8080/api/repo/status
curl http://localhost:8080/api/relay/status
curl http://localhost:8080/api/relay/diag
```

#### Logs and maintenance

```bash
curl "http://localhost:8080/api/logs/history?lines=200&source=service"
curl -X POST http://localhost:8080/api/sync-now
curl -X POST http://localhost:8080/api/self-update
curl -X POST http://localhost:8080/api/update-all
```

### Troubleshooting

| Problem | What to check | Typical fix |
|---|---|---|
| Spoke UI is down | `systemctl status client-sim-dashboard` and `/api/health` | rerun `install-lxc.sh`, inspect `/var/log/client-sim-dashboard-install.log`, check watchdog log |
| Clients are not appearing | `curl /api/health`, `curl /api/clients`, client `server_url` | verify `server_url`, `web_server=on`, and client reachability to `169.253.1.1:8080` |
| Proxmox agent never connects | `systemctl status client-sim-proxmox-agent`, `curl /api/proxmox/pending` | approve the pending host, verify `/etc/client-sim-proxmox-agent.env`, then restart the agent |
| VM Server tab is empty | `curl /api/proxmox/status` | make sure the Proxmox agent is approved and posting telemetry |
| Commands stay queued | `curl /api/commands`, agent/client logs | confirm agent or client can poll `/api/inbox` and POST `/api/inbox/ack` |
| Central panel shows stale/empty data | `curl /api/central/status` | recheck Central credentials, site mappings, and monitored checks in Setup |
| Kill switch will not clear on a client | client `simulation.conf`, `kill_switch.txt`, client logs | turn off local kill switch, rerun update, or send restart; note global kill switch can override local state |
| USB device will not provision | `curl /api/proxmox/usb-config`, agent log, `/etc/client-sim-usb-state.conf` | add VID:PID to certified devices, verify expected `sim_phy`, and confirm the USB device is physically present |

---

## Developers

### Repository structure

```text
cs/
├── lm-spoke/        # ACTIVE LM-native spoke: CSSpoke/CSControlPlane, client API,
│                    # sim engine, registry, command queue, relay-only Proxmox
│                    # deploy, cs-owned Kea DHCP, install_cs.sh, tests, static/
├── clients/         # platform clients: linux/, windows/, t3/
├── configs/         # simulation.conf, user-overrides.conf, kill_switch.txt, etc.
├── proxmox/         # LEGACY Proxmox host agent, watchdog, units, installers
│                    # (auto-provisioning brain now lives in the pxmx repo)
├── webui-spoke/     # LEGACY/standalone FastAPI spoke backend, installer, watchdog
├── webui-local/     # local standalone dashboard assets
├── svr-mgmt/        # server-management helpers
├── installers/      # legacy install-lxc.sh + proxmox LXC helpers
├── docs/            # cs.md + architecture-topology.md + legacy notes
├── logs/            # local log capture
├── install_cs.sh    # ACTIVE installer wrapper (execs lm-spoke/install_cs.sh)
├── uninstall_cs.sh  # cs uninstaller
└── VERSION          # autobumped .NN version (do not bump manually)
```

### `webui-spoke/server.py` architecture

#### Core responsibilities

`server.py` is the spoke control plane. It handles:

- serving the shared UI in `WEBUI_MODE=spoke`
- receiving VM status beacons
- exposing config and script download endpoints
- managing client and Proxmox command queues
- polling Aruba Central
- relaying telemetry to Hub when enabled
- tracking updates, reclones, VM watchdog state, and service logs
- serving `VERSION`/`INSTALLER_VERSION` data to the UI and cache-busting `app.js`/`style.css` with `?v=<app_version>`
- broadcasting real-time state over `/ws`

#### Background tasks

The FastAPI lifespan boot starts these tasks:

- `sync_repo`
- `heartbeat_check`
- `central_token_manager`
- `central_poller`
- `check_for_update`
- `relay_loop`
- `client_history_saver`
- `expire_commands`
- `auto_recovery_check`
- `vm_watchdog_loop`
- `schedule_check`
- `gkill_switch_poller`
- `hourly_baseline_saver`
- `acme_renewal_loop`

#### Important persisted state

| File | Why it exists |
|---|---|
| `settings.json` | durable settings and approved keys |
| `state_cache.json` | restart recovery for last-known proxmox/central state |
| `command_queue.json` | local queued commands, persisted asynchronously for restart recovery |
| `reclone_state.json` | long-running VM operation state, persisted asynchronously |
| `relay_state.json` | hub relay connection state |
| `update_state.json` | installer/app update status |
| `vm_watchdog.json` | VM guest-agent watchdog / auto-recovery state, persisted asynchronously |
| `resource_cache.json` | rolling CPU/memory samples plus cached `agent_version` / `pve_version` for 1-hour warmup recovery |
| `central_history.jsonl` | Central alert history |
| `client_history.json` | client history persistence |
| `client_count_7day.json` | persisted 7-day hourly client-count history used for the baseline alarm |

#### Main in-memory state families

- `clients`
- `proxmox_states` (one entry per connected Proxmox host, keyed by canonical hostname)
- `central_status`
- `central_wireless_clients`
- `relay_state`
- `update_state`
- approved/pending Proxmox agent maps
- WebSocket connection list

### Proxmox agent architecture

#### What the agent owns

`proxmox/proxmox-agent.sh` runs on the Proxmox host and owns:

- host registration and API key persistence
- USB certification, quarantine, and assignment tracking
- VM inventory, node telemetry collection, and host `agent_version` / `pve_version` / `vmid_range` reporting
- VM provisioning, recloning, deletion, post-provision retry handling, and updates
- provision-halt caching, delete-gate enforcement, and VMID gap-audit repair logic
- VM guest-agent watchdog health / reboot / reclone escalation
- command polling/ACK handling, including non-blocking `delete_vm` ACKs
- self-update scheduling

#### State file format

The agent writes `/etc/client-sim-usb-state.conf` as tab-delimited lines:

```text
<vmid>	<bus_path>	<missing_since>	<image_num>	<vidpid>
```

That file is reloaded on every agent restart to reconstruct USB-to-VM mappings.

#### Runtime loops

- background telemetry sender every `TELEMETRY_INTERVAL`
- background inbox processor every `INBOX_INTERVAL`
- USB scan/provision loop in the main process
- unconditional `check_resource_halt()` evaluation on every loop iteration
- post-provision retry queue sweep for VMs that missed the post-reboot step
- VMID gap audit every 5 minutes per host
- VM guest-agent watchdog sweep using the configured grace/check/reboot/reclone timers
- periodic self-update check

#### USB tracking model

- certified devices come from spoke `/api/proxmox/usb-config`
- unknown devices are surfaced separately for operator review
- missing USB devices retain state until timeout
- `usb-phy-override.conf` is written inside guests so `sim_phy` matches the assigned USB class

The agent also keeps three durable per-VM marker files in its provision state directory:

- `.post_prov_retry` — VM completed the clone but missed the post-reboot `update.sh` step; retry every 10 minutes, reclone after 1 hour
- `.provision_done` — anchor timestamp for the guest-agent watchdog grace period
- `.agent_unresponsive` — watchdog escalation state (`first_fail`, `last_check`, `rebooted_at`)

### Simulation script architecture

#### Startup chain

```text
startup.sh
  -> sources ini-parser.sh
  -> loads simulation.conf and user-overrides.conf
  -> runs update.sh
  -> starts helper services
  -> launches simulation.sh
```

#### Main loop

`simulation.sh`:

- re-parses config on each exec restart
- writes its local runtime log to `/tmp/sim.log` so the LXDE autologin user always has a writable log target
- fetches global kill switch live
- runs up to 100 iterations per cycle
- posts status/errors to `/api/status`
- triggers `update.sh` in rapid-update mode or at restart boundaries
- optionally goes offline between cycles when `allow_offline=on`
- finishes by `exec bash /usr/local/scripts/simulation.sh`

#### Kill switch behavior

- local `kill_switch` comes from `simulation.conf`
- global kill switch is fetched live from spoke `/api/kill-switch`, then upstream GitHub fallback
- if either is `on`, the script parks and waits for restart/reload

#### `SIGUSR1` restart model

`simulation.sh` installs a `USR1` trap that sets a restart flag.

`agent.sh` uses that for:

- `restart_sim`
- `kill_switch`

That avoids hard-killing the process and lets the VM loop exit cleanly into its normal exec-restart path.

On Linux clients, the websocket agent is intended to run under `client-sim-agent.service` and publish heartbeat data to `/var/lib/client-sim/agent-health.json`. `client-sim-watchdog.timer` runs `sys_mon.sh --check-once` every minute so a crashed, zombie, or stale agent is restarted even if the websocket process stops updating its health file.

### Adding a new inbox/agent command

#### VM-side command (`clients/linux/agent.sh`)

1. Add the command producer in the spoke or hub.
2. Ensure it is returned by `/api/inbox`.
3. Add a new `case` branch in `clients/linux/agent.sh`.
4. Perform the local action.
5. POST the result to `/api/inbox/ack`.

#### Proxmox-side command (`proxmox/proxmox-agent.sh`)

1. Add the command producer in the spoke or hub.
2. Teach `process_inbox()` / `execute_vm_command()` how to recognize it.
3. Implement the host action with `qm`, `pct`, or helper functions.
4. ACK success/failure back to the spoke.
5. Refresh telemetry if the command changes host or VM state.

### API reference (key endpoints)

#### Client-facing spoke endpoints

| Endpoint | Use |
|---|---|
| `GET /api/health` | basic reachability check |
| `POST /api/status` | VM heartbeat and error upload |
| `GET /api/config` | hostname-aware effective config download |
| `GET /api/config/overrides` | effective `user-overrides.conf` text after any local hub override merge |
| `GET/PUT /api/config/user-overrides-conf` | full-file user-overrides editor for standalone spoke mode |
| `POST /api/config/overrides/save` | legacy per-user override save helper |
| `GET /api/scripts/list` | list platform files |
| `GET /api/scripts/{platform}/{filename}` | download one script |
| `GET /api/inbox` | VM command polling |
| `POST /api/inbox/ack` | VM command acknowledgement |
| `GET /api/kill-switch` | global/local kill switch value |

#### Operator/spoke UI endpoints

| Endpoint | Use |
|---|---|
| `GET /api/init` | one-shot UI bootstrap payload, including `app_version` and `installer_version` |
| `GET /api/clients` | live client list |
| `POST /api/commands` / `GET /api/commands` | local command queue |
| `GET/POST /api/settings` | spoke settings |
| `POST /api/auth/change-password` | rotate the primary local admin password |
| `GET/POST/DELETE /api/auth/local-users` | list, add, and remove spoke-local users |
| `GET /api/proxmox/status` | host, VM, USB, and reclone summary |
| `POST /api/proxmox/telemetry` | host telemetry ingest |
| `GET /api/central/status` | Central state summary |
| `GET /api/simulations` | simulation/bucket summary |
| `GET /api/logs/history` | service/install log history |
| `GET /ws` | browser real-time updates |

#### Relay-related spoke endpoints

| Endpoint | Use |
|---|---|
| `POST /api/relay/trigger` | force a relay sync |
| `GET /api/relay/status` | current hub relay state |
| `GET /api/relay/diag` | relay diagnostics |

---

## Summary

`client-sim` is the local execution engine of the platform: it installs and runs the spoke, manages Proxmox automation, distributes configs/scripts to clients, and drives the simulations themselves.
