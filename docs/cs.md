---
summary: "Client Simulation spoke. Repo: cs. moduletype = 'simulation', label 'Agent'/'Client Simulator'."
keywords: [client, collab, cs, dashboard, dhcp_fail, dns_fail, dns_latency, download, iperf, lm, sims, simulation, traffic, www_traffic]
---

# cs — Client Simulation

Client Simulation spoke. Repo: `cs`. `module_type = "simulation"`, label "Generic Agent"/"Client Simulator". See [architecture-topology.md](architecture-topology.md).

## Role & module_type

The active LM spoke is `lm-spoke/` (`CSSpoke`), **relay-only** for Proxmox/USB auto-provisioning (the gate/VMID audit runs in the pxmx agent). It owns: the sim engine, client registry, per-client override control panel, hub-config store, command queue, token store, demo scenarios, and the DHCP/client API for the isolated sim-client network. `webui-spoke/` is the **legacy/standalone** combined spoke+UI server (FastAPI :8000, Aruba Central, older relay) — a parallel path, not the LM-native active one. `clients/` holds sim-agent scripts that run on sim VMs (Linux/Windows/T3).

## Entrypoints

- **lm-spoke (native):** `python3 -m src.control_plane` (`CSControlPlane`), systemd `lm-cs.service`, `User=svc_lm`, `--port $CS_API_PORT --host $CS_API_HOST`. Installer `lm-spoke/install_cs.sh` (clones lm core `core/` to `/opt/lm/core`, cs to `/opt/lm/cs`, a cs-owned Kea DHCP instance `kea-dhcp4-sim` on the 2nd NIC, `lm-cs.service`, rollback watchdog + sudoers). `--standalone` opts out of hub mode.
- **webui-spoke (legacy):** `uvicorn server:app` :8000. Installer `installers/install-lxc.sh`.
- **Sim agents:** `clients/linux/agent.sh` (systemd `client-sim-agent.service`), `clients/windows/*.ps1`, `clients/t3/*`.

> **Primarily a role now.** cs runs mainly as the **`simulation`** role hosted by the generic agent (`agent-<hostname>`, unit `lm-agent`): the agent opens a sub-spoke `{agent}-simulation` (module_type `simulation`, parent-auto-approved) and self-installs the role via `agent/src/agent_spoke.py::_install_role` — cloning `lbockenstedt/cs.git` + deps and running `install_cs.sh --infra-only` for the idempotent host prep (cs-owned Kea + 2nd NIC). The dedicated `lm-cs.service` / `install_cs.sh` `{module}-spoke-1` path below is the **legacy/standalone** alternative. Sim/provisioning config arrives via the hub push (WebUI), not a per-module `.env`.

## Ports

- lm-spoke client API: `CS_API_PORT` (default **8080**, not 8000 — the legacy webui-spoke used :8000; the unified LM hub owns :443). Bound `0.0.0.0`/`CS_API_HOST` so it also lands on the DHCP NIC `169.253.1.1`. Clients reach `169.253.1.1:8080`.
- Spoke dials hub on **443** (`/ws/spoke`, wss — verify-off same-box).
- webui-spoke legacy: **8000** HTTP + WS `/ws`.
- DHCP: a **cs-owned Kea instance** (`kea-dhcp4-sim`, separate from the `dhcp` module's Kea so both coexist on one box) on the auto-detected 2nd NIC. Subnet `169.253.1.0/24`, pool `169.253.1.11`–`169.253.1.254`, no router option (parity with the prior dnsmasq scope). Configs `/etc/kea/kea-dhcp4-sim.conf` + `/etc/kea/kea-ctrl-agent-sim.conf`; leases `/var/lib/kea/kea-leases4-sim.csv`; control socket `/run/kea/kea4-ctrl-socket-sim`; units `kea-dhcp4-sim.service` + `kea-ctrl-agent-sim.service`. A minimal cs `kea-ctrl-agent-sim` binds `127.0.0.1:8002` (the `dhcp` module uses :8001).

## Environment variables

- `.env`: `HUB_URL`, `SPOKE_ID`, `SPOKE_SECRET`, `HUB_SECRET`, `CS_API_PORT`, `CS_API_HOST`, `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`.
- Process: `LM_ONBOARDING_PSK`, `LM_TENANT_ID_HINT`, `CS_TELEMETRY_INTERVAL_S` (10), `LM_DEP_GUARD_DISABLE`.
- DHCP (installer): `DHCP_IFACE`, `DHCP_SUBNET`, `DHCP_PREFIX`, `DHCP_GATEWAY`, `DHCP_RANGE_START`, `DHCP_RANGE_END`, `DHCP_LEASE_TIME`, `DHCP_SKIP`.

## Install flags

`lm-spoke/install_cs.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--dhcp-iface`, `--no-dhcp`, `--tls-verify` (+ `--tls-ca-cert`, **required**), `--no-agent-listener` (opt OUT of the split-topology `/ws/agent` listener, which is ON by default — see below), `--agent-listener` (harmless no-op — already the default), `--infra-only` (host prep only: cs Kea + 2nd NIC + agent-listener cert, no unit/.env — used by the generic agent's `simulation` role load), `--purge-env`/`--reset-identity` (delete the existing `.env` first so the secret + `INSTALL_UUID` regenerate → fresh identity, re-registers and needs hub approval), `--admin-token` (deprecated no-op), `--all-prereqs` (no-op). A stale `CS_API_PORT=8000` is auto-migrated to 8080. `control_plane.py` CLI also accepts `--port`, `--host`, `--standalone`, `--onboarding-psk`, `--tenant-id-hint`.

### Agent listener (split topology, ON by default)

`CSControlPlane` subclasses the shared `AgentHostingControlPlane` mixin (also used by pxmx), so a cs spoke hosts inbound Proxmox host agents on `/ws/agent` directly by default — most deployments route the agent relationship through cs, so `install_cs.sh` generates a self-signed cert at `$LM_DIR/cs/certs/` (preserved on re-run), writes `LM_CS_AGENT_LISTENER=1` + `LM_TLS_CERT`/`LM_TLS_KEY` to `.env`, and grants the `lm-cs.service` unit `CAP_NET_BIND_SERVICE` so `svc_lm` can bind `:443` unconditionally. Pass `--no-agent-listener` for the rare all-in-one/relay-only deployment, where this cs spoke never binds `:443` and agents go through the pxmx spoke (or the hub's `/ws/agent` byte-proxy) instead. Since a standalone cs spoke doesn't broadcast `_lm-hub` mDNS, an agent dialing it must be pinned: `agent/install_agent.sh --spoke-ip <cs-host>` (the installer prints this — just the IP; the agent auto-determines the scheme/port/`/ws/agent` path by probing). `CSSpoke` answers `GET_AGENTS`/`SET_AGENT_CONFIG`/`SPOKE_RELAY` for cs-dialed agents, mirroring `ProxmoxSpoke`'s existing handlers.

## Local Setup config: auto-provisioning + Aruba Central (`local_store.py`, `central_poller.py`, `aruba.py`)

The standalone/hub-connected local dashboard (see "Local standalone dashboard" above) can now own two categories of config an LM hub tenant used to hold exclusively:

- **Auto-provisioning (`hub_config`)** — the same USB/VM-provisioning knobs as the LM hub's Setup/Proxmox card (thresholds, VM templates, VMID range, watchdog group, etc.), stored locally in `data/local_store.json` (`local_store.py`, defaults mirror `lm/core/src/simulations/store.py::_DEFAULT_HUB_CONFIG` — re-sync if the hub's knob set changes). Saving calls `_apply_hub_config` directly — the SAME method `CS_CONFIG_UPDATE` already uses — so the knobs flow to `self.settings` and on to any cs-dialed pxmx agent exactly like a hub-pushed config. Dashboard tabs: **Auto-Provisioning** (`csSetupAutoProvConfigCard`, the structured 5-section card) + the remaining flat knobs (`csHubConfigCard`), both reused as-is from `sim-views.js`.
- **Aruba Central integration (`central_config`/`central_sites_config`)** — real API polling, not a stub. `aruba.py` is a vendored copy of `solutions-hpe/webui-hub`'s `app/aruba.py` (self-contained: stdlib + `httpx` only, OAuth token handling for both the classic and `new_central` API flows, site health/alerts/insights/client-count polling, available-checks catalog). `central_poller.py`'s `CentralPoller` drives it every 5 minutes against the sites in `central_sites_config`, and assembles the result into `spoke.central_status` in the exact shape `sim-views.js`'s Simulations Checks/Hardware/Client-Count tabs already expect — this closes the "Central integration doesn't exist" gap noted when those tabs first landed. Started from both `CSControlPlane.run()` (direct call — already inside a running loop) and `run_standalone_mode()` (via a FastAPI `startup` event handler, since `uvicorn.run()` doesn't have a running loop yet at construction time). When hub-connected, `_cs_telemetry_relay_loop` also overlays `central_status` onto the relayed `CS_TELEMETRY` payload's `central` field (previously always `{}`), so the LM hub's own Simulations tab gets live Central data too. Dashboard tab: **Central API Setup** (`csRenderSetupCentralApi`, reused as-is).

New CS_* commands (all local-store/aruba-backed, none require a hub): `CS_GET/SET/RESET_HUB_CONFIG`, `CS_GET/SET_CENTRAL_CONFIG`, `CS_GET/SET_CENTRAL_SITES_CONFIG`, `CS_GET_CENTRAL_AVAILABLE`, `CS_TEST_CENTRAL`.

## Key commands / handlers (`CSSpoke.handle_command`, `lm-spoke/src/cs_spoke.py`)

- Identity: `GET_VERSION`/`CS_GET_VERSION`.
- Simulation: `CS_TRIGGER_ITERATION` (legacy `TRIGGER_ITERATION`), `CS_GET_SIMULATION_STATE`, `CS_SET_SIMULATION_PROFILE`.
- Config: `CS_GET_CONFIG`, `CS_UPDATE_CONFIG`/`UPDATE_CONFIG`, `CS_UPDATE_USER_OVERRIDES`.
- Kill switch: `CS_KILL_SWITCH`, `CS_GET_KILL_SWITCH`.
- Demo scenarios (TTL + auto-expiry): `CS_DEMO_SCENARIO`, `CS_DEMO_CLEAR`, `CS_GET_DEMO_ACTIVE`, `CS_GET_DEMO_SCENARIOS`.
- Per-client override panel (11 toggles): `CS_GET/SET/CLEAR/SET_ALL_CLIENT_OVERRIDES`. Toggles: `kill_switch`, `dns_fail`, `iperf`, `download`, `www_traffic`, `ping_test`, `ssidpw_fail`, `auth_fail`, `dhcp_fail`, `port_flap`, `assoc_fail`.
- Client registry bulk: `CS_PURGE_CLIENTS` (drops every registered client + deletes `data/clients.json`).
- Per-host USB VMID overrides: `CS_GET/SET/CLEAR_HOST_USB_OVERRIDE`.
- CS ingest (unified pxmx agent → hub → here): `CS_INGEST_TELEMETRY/LOG/PROGRESS/WATCHDOG_EVENT/HW_RESET/COMMAND_RESULT`, `CS_STORE_PROXMOX_TOKEN`.
- VNC relay (cs-dialed pxmx agents, mirrors `ProxmoxSpoke`): `VNC_START`, `VNC_FRAME_DOWN`, `VNC_DISCONNECT`.
- Command queue: `CS_QUEUE_COMMAND`, `CS_POLL_AGENT_INBOX`, `CS_ACK_COMMAND`, `CS_GET_USB_CONFIG`, `CS_GET_COMMANDS`, `CS_CLEAR_COMMANDS`, `CS_DELETE_COMMAND`, `CS_UPDATE_SETTINGS`, `CS_CONFIG_UPDATE` (hub-pushed provisioning config; `_HUB_DIRECT_KEYS` + `_HUB_KEY_REMAP`; writes `hub-sim-overrides.conf`/`hub-user-overrides.conf`).
- Local Setup config (see "Local Setup config" above): `CS_GET/SET/RESET_HUB_CONFIG`, `CS_GET/SET_CENTRAL_CONFIG`, `CS_GET/SET_CENTRAL_SITES_CONFIG`, `CS_GET_CENTRAL_AVAILABLE`, `CS_TEST_CENTRAL`.
- Retired (hub no longer sends): `CS_START_SIMULATION`, `CS_STOP_SIMULATION`, `CS_GET_STATUS`, `CS_GET_TELEMETRY`, `CS_GET_CLIENTS`.

## Local standalone dashboard (`GET /`)

`build_client_api_app` serves a browsable local dashboard at `/` — Simulations, Clients, Central, API Server, Config, Auto-Provisioning, Central API Setup tabs (VM Server still to come) — available in **both** `--standalone` and hub-connected mode since both modes build the app from the same function. This is the equivalent of the original `solutions-hpe/client-sim` `webui-spoke`'s `http://<spoke-host>:8000` local UI — a very small deployment that just wants to run simulations can use this directly, with no LM hub required at all.

Implementation: `local_ui_routes.py` answers the same `/sim/api/*` REST contract that `lm/WebUI/sim-views.js` (the LM hub's per-spoke Simulations/Clients renderer) already speaks to the hub, but sourced from THIS spoke's own local state (`registry`/`engine`/`demo`/`local_store`/`central_poller`) instead of the hub's cross-spoke aggregation cache — so `static/sim-views.js` (a vendored verbatim copy) renders identically to the hub's own per-spoke views, without needing a tenant/spoke-aggregation layer. `static/dashboard.html` is a minimal page shell (Tailwind CDN + the hub's own `.hpe-card` styling, no login/tenant-switcher chrome) providing the globals sim-views.js expects (`currentTenant`, `showToast`, `handleSessionExpired`) plus a `TAB_RENDERERS` override for the two tabs that don't map onto a single top-level `loadCSData` case (Config → `csRenderConfigSimulation`; Auto-Provisioning → a composite of `csSetupAutoProvConfigCard` + `csHubConfigCard`).

**Still to come:** VM Server (real per-host VM/USB data is feasible via `proxmox_deploy.py`'s telemetry once a pxmx agent is connected — through the cs-agent-listener or hub relay — but the fleet-wide Reclone-All/Auto-Provisioning-toggle controls in `sim-views.js`'s version are tenant-hub-scoped concepts needing more thought for a single-spoke deployment); the rest of Setup (GitHub repo settings, spoke-local dashboard security, notifications, troubleshooting) — these are hub/tenant-admin concepts in `sim-views.js` with no obvious single-spoke equivalent yet, unlike Auto-Provisioning/Central which mapped over cleanly.

## Key files

- lm-spoke: `lm-spoke/src/cs_spoke.py`, `control_plane.py` (`CSControlPlane`, `module_type="simulation"`, CS telemetry relay, standalone), `client_api.py` (FastAPI :8080 — `/api/health`, `/api/kill-switch`, `POST /api/status`, `/api/client/key`, `/api/config`(+`/overrides`/`/parsed`), `/api/scripts/{platform}/*`, `/api/clients`(+`/{h}/control`), `/api/commands`, `/api/inbox`(/ack), `ws /ws/client`, `/` local dashboard, `/static/*`), `local_ui_routes.py` (`/sim/api/*` — local dashboard backend, see "Local standalone dashboard" above), `local_store.py` (JSON store — hub_config/central_config/central_sites_config), `central_poller.py` (`CentralPoller` — drives `aruba.py` on a 5-min loop), `aruba.py` (`ArubaClient` — vendored from `solutions-hpe/webui-hub`'s `app/aruba.py`), `client_registry.py`, `command_queue.py`, `proxmox_deploy.py` (`ProxmoxDeploy` — telemetry ingest, `relay_payload` with `provision` diagnostic), `sim_config.py`, `simulation_engine.py`, `demo_scenarios.py`, `token_store.py`, `data_models.py`, `dhcp_status.py`, `sim_primitives.py`, `resource_pressure.py` (shared client back-pressure throttling, vendored), `agent_role.py`; `lm-spoke/role.py`, `lm-spoke/API_SPEC.md`, `lm-spoke/static/` (`dashboard.html`, `sim-views.js` — vendored copy of `lm/WebUI/sim-views.js`, re-sync when that changes).
- webui-spoke legacy: `webui-spoke/server.py`, `lm_relay.py` (`CSBridge`/`LMControlPlane`), `acme.py`.
- Clients: `clients/linux/agent.sh` + scripts, `clients/windows/*.ps1`, `clients/t3/*`; configs `configs/simulation.conf`, `configs/user-overrides.conf`.

## Notable behaviors & gotchas

- **lm-spoke is relay-only for Proxmox** — `proxmox_deploy.py` ingests telemetry + builds `relay_payload` (per-host `provision` diagnostic with `cs_enabled`/`loop_running`/`auto_provision_on`/`reason`/`halt`); the brain is `pxmx/agent/src/usb_provision.py`.
- **Client API port 8080** (was 8000) — at the time, the hub owned :8000 in hub mode; a second bind failed with `[Errno 98]` and crash-looped `lm-cs`. The hub has since moved to unified :443, but cs stays on 8080. Installer migrates stale `.env`.
- **Two flags trap** — tenant `usb_auto_provision` toggle ≠ per-agent `client_simulation.enabled`; the provision loop only spawns on the latter (the "enabled but nothing provisions" root cause).
- **store.set_hub_config REPLACES** — both `csSaveHubConfig` and `csSaveAutoProvConfig` must GET-merge-PUT or the two cards wipe each other.
- **CS_CONFIG_UPDATE handler** is required for hub config pushes (usb_vidpids, templates, sim/user overrides) to land — without it they silently dropped to "Unknown command" and `usb_vidpids` stayed `[]`.

## How it works

**End-to-end, cs is a control + relay plane; the pxmx agent is the execution plane.**

**Proxmox is relay-only here.** The cs lm-spoke never talks to Proxmox directly. The unified **pxmx agent** (running on each Proxmox host) is where the auto-provisioning *brain* lives (`pxmx/agent/src/usb_provision.py::run_provision_loop`). That loop decides when to clone, reboot, reclone, or delete sim VMs. cs only: (a) ingests the agent's telemetry (`CS_INGEST_TELEMETRY` etc.), (b) stores per-host Proxmox state + rolling CPU/mem 1h averages (`proxmox_deploy.py`), (c) re-emits a `CS_TELEMETRY` frame to the hub every ~10s so the VM Server view has data, and (d) surfaces a per-host **`provision` diagnostic** (`cs_enabled` / `loop_running` / `auto_provision_on` / `reason` / `halt`) that reports *why* the agent's loop is or isn't provisioning. Commands the UI issues (start/stop/reclone a VM, push USB/dongle config) are queued on cs and relayed to the agent by the hub's `CSBridgePoller`.

**The sim engine + config resolver.** `simulation_engine.py` + `sim_config.py` compute each client's effective profile. A client is deterministically bucketed into one of ten profiles `s0`–`s9` by `crc32(hostname) % 10`, then layered: `[simulation]` globals → `[address]`/`[server]` targets → the `[sX]` bucket → a per-`[username]` override (username = hostname before the first `-`, e.g. `jsmith-1` → `jsmith`). All of this is edited in **Simulations → Config → Simulation** (`simulation.conf`) and user-overrides. Hub-pushed overrides are merged from `hub-sim-overrides.conf` / `hub-user-overrides.conf` on top.

**Client registry + per-client overrides.** Every sim VM that reports in is tracked in `client_registry.py` (persisted to `data/clients.json`): last-seen, SSID, gateway reachability, running sims, recent errors. The **per-client Control Panel** (11 fault toggles) writes *persisted* overrides into that registry. **Demo scenarios** (`demo_scenarios.py`) are the ephemeral counterpart: an in-memory, 120-minute-TTL override that flips one failure flag and auto-expires (or clears on reboot) back to whatever the operator had set — demos never mutate the persisted registry.

**Hub-config store + command queue.** Auto-provisioning knobs (templates, VMID range, thresholds, dongle VID:PIDs) live in a local store (`local_store.py`) and/or arrive from the hub via `CS_CONFIG_UPDATE`. The command queue (`command_queue.py`) holds VM actions (`pending → delivered → completed/failed/expired`) with an idempotent enqueue and a sim-VMID safeguard (refuses anything below VMID 90000 or in `protected_vmids`, default `{1001}`) so the UI can only ever touch sim VMs.

**The cs-owned sim DHCP.** cs owns its **own** Kea DHCP4 instance for the isolated sim-client network — this is **separate** from the `dhcp` module's Kea. `install_cs.sh` provisions it on an auto-detected **second NIC** at `169.253.1.1`, static subnet **`169.253.1.0/24`**, pool **`169.253.1.11`–`169.253.1.254`**, **no** router/gateway option (the network is deliberately isolated). Configs: `/etc/kea/kea-dhcp4-sim.conf` + `/etc/kea/kea-ctrl-agent-sim.conf`; the sim control agent listens on **127.0.0.1:8002** (the `dhcp` module's Kea control agent is on a different port); leases in `/var/lib/kea/kea-leases4-sim.csv`; units `kea-dhcp4-sim.service` + `kea-ctrl-agent-sim.service`. `dhcp_status.py` cheaply reads the lease CSV (not the ctrl-agent) and rides the 10s telemetry frame to the hub's DHCP-server card.

**How a sim client gets an address and phones home.** A sim VM boots on the isolated sim network → the cs-owned Kea leases it an address from `169.253.1.11`–`.254` → the client reaches the **client API at `169.253.1.1:8080`** (`client_api.py`, FastAPI). It fetches its profile from `GET /api/config?hostname=…` (bucket + overrides + any live demo flags baked in), POSTs status beacons to `/api/status` (upserting the registry), and opens `ws /ws/client` for live command push. When a client-api key is set, the linux agent fetches it from `/api/client/key` first; the t3 agent sends none (empty key = open).

## How to use it

**Enable client simulation / auto-provisioning (the two toggles that both must be on).** Auto-provisioning has a *tenant* switch and a *per-agent* switch, and VMs only spawn when **both** are on:

1. Turn on the **tenant** switch: Simulations → **Setup → Proxmox** (or Config → Simulation), in the **"VM Auto-Provisioning"** card, set **"Auto-Provision VMs"** on (`usb_auto_provision`). While here also confirm the card's other knobs — VM template IDs, VMID range, CPU/mem thresholds, dongle VID:PIDs — since a missing template or empty dongle list also stops provisioning.
2. Turn on the **per-agent** switch for each Proxmox host: Setup → **Spokes & Agents** → the agent's row → **Edit** → check **"Enable Client Simulation mode on this host"** (`client_simulation.enabled`) and save. This is what actually puts the pxmx agent's provision loop into CS mode.
3. Watch **Simulations → VM Server**: each host row shows the `provision` diagnostic; when both flags are on and thresholds pass, the agent begins cloning sim VMs into the 90000+ VMID range.

**Run a demo scenario (auto-expiring fault).** Simulations → **Clients** tab → the target client's **Demo** column → pick a scenario (`normal` = clear, or one of `dns_fail` / `dhcp_fail` / `assoc_fail` / `auth_fail` / `ssidpw_fail` / `port_flap`) → trigger. It shows in the **"Active Demo Scenarios"** card with minutes remaining; it auto-clears after **120 minutes** (or on client reboot), reverting to the client's persisted state. Clear early from the same card / column.

**Toggle a per-client fault (persisted override).** Simulations → **Clients** → the client row's **⚙ Control** button opens **"Live Overrides — {hostname}"** with the 11 toggles: `kill_switch`, `dns_fail`, `iperf`, `download`, `www_traffic`, `ping_test`, `ssidpw_fail`, `auth_fail`, `dhcp_fail`, `port_flap`, `assoc_fail`. Set what you want → **Apply** (persists to that client), **Clear Overrides** (removes them), or **Apply to ALL** (pushes the same set to every registered client). Unlike a demo, these persist until you clear them. The client picks them up on its next `/api/config` fetch.

**Use the kill switch (emergency stop).** Simulations → **Clients** → the banner at the top: **"⛔ Emergency Stop"** halts all sims (clients poll `/api/kill-switch` and stand down); **"▶ Resume Sims"** re-enables. This is global; the per-client `kill_switch` override above stops just one client.

## Troubleshooting / common questions

**"Auto-provisioning is enabled but nothing provisions."** This is almost always the **two-flag trap**: the tenant-level **"Auto-Provision VMs"** toggle (`usb_auto_provision`) is a *different* switch from each host's **"Enable Client Simulation mode on this host"** (`client_simulation.enabled`). The pxmx agent's loop only spawns when **both** are on. Check the host's `provision` diagnostic on **VM Server** — it reports `cs_enabled`, `loop_running`, `auto_provision_on`, a `reason` string for the current gate, and `halt`. Common gate reasons beyond the two flags: no VM template configured, an empty dongle VID:PID list, CPU/mem over the 1h-average thresholds, or `provision_halt` set. Remember the brain runs in the agent; cs only relays and displays the diagnostic.

**"Sim clients aren't getting an IP."** Their addresses come from the **cs-owned Kea** (`kea-dhcp4-sim`) on the **second NIC** at `169.253.1.1`, subnet `169.253.1.0/24`, pool `169.253.1.11`–`.254` — not from the `dhcp` module. Check the hub-level **Setup → Simulations** DHCP-server card (or the telemetry `dhcp` block) for `installed`/`running`/utilization. On the box: `systemctl is-active kea-dhcp4-sim`, confirm the second NIC is up at `169.253.1.1`, and that the sim VMs are actually on the isolated network. Because the scope serves no router option, sim clients are intentionally isolated and reach only `169.253.1.1:8080`.

**"I changed a config/provisioning setting and nothing happened."** Hub-pushed provisioning config lands via the **`CS_CONFIG_UPDATE`** handler; without it, `usb_vidpids` stays `[]`, the bridge pulls an empty list every 60s, and auto-provision never fires. Also note the hub-config store **REPLACES** on write, so the two Setup cards ("VM Auto-Provisioning" and the flat "Hub Config") must GET-merge-PUT — if a save wiped the other card's values, re-open both and re-save. For simulation-profile edits, remember they only apply to a client on its next `/api/config` fetch, and per-`[username]` overrides key off the hostname's first `-` segment.

**"The simulation spoke is offline / red."** cs runs mainly as the **`simulation`** role on the generic agent (sub-spoke `{agent}-simulation`), dialing the hub on 443. If it's red: confirm the generic agent (`lm-agent`) is up and approved, that `install_cs.sh --infra-only` host prep ran (cs Kea + second NIC), and check the spoke logs. A spoke that never provisioned its Kea/NIC still connects but the DHCP card shows "Not configured."

**"Why is the client API on 8080 and not 8000?"** The legacy `webui-spoke` used :8000, but the unified LM hub owns that box, so binding :8000 collided and crash-looped cs. The client API moved to **8080**; sim clients reach `169.253.1.1:8080`. The installer auto-migrates a stale `CS_API_PORT=8000` in `.env` to 8080.

## Related pages

[architecture-topology.md](architecture-topology.md), [pxmx.md](pxmx.md), [lm-hub.md](lm-hub.md), [environment-variables.md](environment-variables.md), [install-flags.md](install-flags.md).