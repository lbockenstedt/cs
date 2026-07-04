# cs — Client Simulation

Client Simulation spoke. Repo: `cs`. `module_type = "simulation"`, label "Generic Agent"/"Client Simulator". See [architecture-topology.md](architecture-topology.md).

## Role & module_type

The active LM spoke is `lm-spoke/` (`CSSpoke`), **relay-only** for Proxmox/USB auto-provisioning (the gate/VMID audit runs in the pxmx agent). It owns: the sim engine, client registry, per-client override control panel, hub-config store, command queue, token store, demo scenarios, and the DHCP/client API for the isolated sim-client network. `webui-spoke/` is the **legacy/standalone** combined spoke+UI server (FastAPI :8000, Aruba Central, older relay) — a parallel path, not the LM-native active one. `clients/` holds sim-agent scripts that run on sim VMs (Linux/Windows/T3).

## Entrypoints

- **lm-spoke (native):** `python3 -m src.control_plane` (`CSControlPlane`), systemd `lm-cs.service`, `User=svc_lm`, `--port $CS_API_PORT --host $CS_API_HOST`. Installer `lm-spoke/install_cs.sh` (clones lm core `core/` to `/opt/lm/core`, cs to `/opt/lm/cs`, dnsmasq DHCP on 2nd NIC, `lm-cs.service`, rollback watchdog + sudoers). `--standalone` opts out of hub mode.
- **webui-spoke (legacy):** `uvicorn server:app` :8000. Installer `installers/install-lxc.sh`.
- **Sim agents:** `clients/linux/agent.sh` (systemd `client-sim-agent.service`), `clients/windows/*.ps1`, `clients/t3/*`.

## Ports

- lm-spoke client API: `CS_API_PORT` (default **8080**, not 8000 — the legacy webui-spoke used :8000; the unified LM hub owns :443). Bound `0.0.0.0`/`CS_API_HOST` so it also lands on the DHCP NIC `169.253.1.1`. Clients reach `169.253.1.1:8080`.
- Spoke dials hub on **443** (`/ws/spoke`, wss — verify-off same-box).
- webui-spoke legacy: **8000** HTTP + WS `/ws`.
- DHCP: dnsmasq on the auto-detected 2nd NIC, scope `169.253.1.11`–`169.253.1.254`, no default gateway, `port=0`.

## Environment variables

- `.env`: `HUB_URL`, `SPOKE_ID`, `SPOKE_SECRET`, `HUB_SECRET`, `CS_API_PORT`, `CS_API_HOST`, `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`.
- Process: `LM_ONBOARDING_PSK`, `LM_TENANT_ID_HINT`, `CS_TELEMETRY_INTERVAL_S` (10), `LM_DEP_GUARD_DISABLE`.
- DHCP (installer): `DHCP_IFACE`, `DHCP_SUBNET`, `DHCP_PREFIX`, `DHCP_GATEWAY`, `DHCP_RANGE_START`, `DHCP_RANGE_END`, `DHCP_LEASE_TIME`, `DHCP_SKIP`.

## Install flags

`lm-spoke/install_cs.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--dhcp-iface`, `--no-dhcp`, `--tls-verify` (+ `--tls-ca-cert`, **required**), `--no-agent-listener` (opt OUT of the split-topology `/ws/agent` listener, which is ON by default — see below), `--admin-token` (deprecated no-op), `--all-prereqs` (no-op). A stale `CS_API_PORT=8000` is auto-migrated to 8080. `control_plane.py` CLI also accepts `--port`, `--host`, `--standalone`, `--onboarding-psk`, `--tenant-id-hint`.

### Agent listener (split topology, ON by default)

`CSControlPlane` subclasses the shared `AgentHostingControlPlane` mixin (also used by pxmx), so a cs spoke hosts inbound Proxmox host agents on `/ws/agent` directly by default — most deployments route the agent relationship through cs, so `install_cs.sh` generates a self-signed cert at `$LM_DIR/cs/certs/` (preserved on re-run), writes `LM_CS_AGENT_LISTENER=1` + `LM_TLS_CERT`/`LM_TLS_KEY` to `.env`, and grants the `lm-cs.service` unit `CAP_NET_BIND_SERVICE` so `svc_lm` can bind `:443` unconditionally. Pass `--no-agent-listener` for the rare all-in-one/relay-only deployment, where this cs spoke never binds `:443` and agents go through the pxmx spoke (or the hub's `/ws/agent` byte-proxy) instead. Since a standalone cs spoke doesn't broadcast `_lm-hub` mDNS, an agent dialing it must be pinned: `agent/install_agent.sh --spoke-url wss://<cs-host>:443/ws/agent` (the installer prints this — the path suffix is required, the agent does not append it itself). `CSSpoke` answers `GET_AGENTS`/`SET_AGENT_CONFIG`/`SPOKE_RELAY` for cs-dialed agents, mirroring `ProxmoxSpoke`'s existing handlers.

## Key commands / handlers (`CSSpoke.handle_command`, `lm-spoke/src/cs_spoke.py`)

- Identity: `GET_VERSION`/`CS_GET_VERSION`.
- Simulation: `CS_TRIGGER_ITERATION` (legacy `TRIGGER_ITERATION`), `CS_GET_SIMULATION_STATE`, `CS_SET_SIMULATION_PROFILE`.
- Config: `CS_GET_CONFIG`, `CS_UPDATE_CONFIG`/`UPDATE_CONFIG`, `CS_UPDATE_USER_OVERRIDES`.
- Kill switch: `CS_KILL_SWITCH`, `CS_GET_KILL_SWITCH`.
- Demo scenarios (TTL + auto-expiry): `CS_DEMO_SCENARIO`, `CS_DEMO_CLEAR`, `CS_GET_DEMO_ACTIVE`, `CS_GET_DEMO_SCENARIOS`.
- Per-client override panel (11 toggles): `CS_GET/SET/CLEAR/SET_ALL_CLIENT_OVERRIDES`. Toggles: `kill_switch`, `dns_fail`, `iperf`, `download`, `www_traffic`, `ping_test`, `ssidpw_fail`, `auth_fail`, `dhcp_fail`, `port_flap`, `assoc_fail`.
- Per-host USB VMID overrides: `CS_GET/SET/CLEAR_HOST_USB_OVERRIDE`.
- CS ingest (unified pxmx agent → hub → here): `CS_INGEST_TELEMETRY/LOG/PROGRESS/WATCHDOG_EVENT/HW_RESET/COMMAND_RESULT`, `CS_STORE_PROXMOX_TOKEN`.
- Command queue: `CS_QUEUE_COMMAND`, `CS_POLL_AGENT_INBOX`, `CS_ACK_COMMAND`, `CS_GET_USB_CONFIG`, `CS_GET_COMMANDS`, `CS_CLEAR_COMMANDS`, `CS_DELETE_COMMAND`, `CS_UPDATE_SETTINGS`, `CS_CONFIG_UPDATE` (hub-pushed provisioning config; `_HUB_DIRECT_KEYS` + `_HUB_KEY_REMAP`; writes `hub-sim-overrides.conf`/`hub-user-overrides.conf`).
- Retired (hub no longer sends): `CS_START_SIMULATION`, `CS_STOP_SIMULATION`, `CS_GET_STATUS`, `CS_GET_TELEMETRY`, `CS_GET_CLIENTS`.

## Local standalone dashboard (`GET /`)

`build_client_api_app` serves a browsable local dashboard at `/` (Simulations + Clients tabs so far — the first slice ported; Central/VM Server/API Server/Config/Setup are follow-up work), available in **both** `--standalone` and hub-connected mode since both modes build the app from the same function. This is the equivalent of the original `solutions-hpe/client-sim` `webui-spoke`'s `http://<spoke-host>:8000` local UI — a very small deployment that just wants to run simulations can use this directly, with no LM hub required at all.

Implementation: `local_ui_routes.py` answers the same `/sim/api/*` REST contract that `lm/WebUI/sim-views.js` (the LM hub's per-spoke Simulations/Clients renderer) already speaks to the hub, but sourced from THIS spoke's own local state (`registry`/`engine`/`demo`) instead of the hub's cross-spoke aggregation cache — so `static/sim-views.js` (a vendored verbatim copy) renders identically to the hub's own per-spoke views, without needing a tenant/spoke-aggregation layer. `static/dashboard.html` is a minimal page shell (Tailwind CDN + the hub's own `.hpe-card` styling, no login/tenant-switcher chrome) providing the globals sim-views.js expects (`currentTenant`, `showToast`, `handleSessionExpired`) and two tabs wired to `loadCSData('Dashboard')`/`loadCSData('Clients')`.

**Known gap:** Aruba Central integration doesn't exist in lm-spoke yet, so `/sim/api/aggregate/central*` honestly returns an empty spoke list (renders sim-views.js's own "No spokes reporting simulation data yet" empty state) rather than fabricating data — the original webui-spoke's Simulations Checks/Hardware/Client-Count sub-tabs need real Central polling ported before they'll show anything.

## Key files

- lm-spoke: `lm-spoke/src/cs_spoke.py`, `control_plane.py` (`CSControlPlane`, `module_type="simulation"`, CS telemetry relay, standalone), `client_api.py` (FastAPI :8080 — `/api/health`, `/api/kill-switch`, `POST /api/status`, `/api/client/key`, `/api/config`(+`/overrides`/`/parsed`), `/api/scripts/{platform}/*`, `/api/clients`(+`/{h}/control`), `/api/commands`, `/api/inbox`(/ack), `ws /ws/client`, `/` local dashboard, `/static/*`), `local_ui_routes.py` (`/sim/api/*` — local dashboard backend, see "Local standalone dashboard" above), `client_registry.py`, `command_queue.py`, `proxmox_deploy.py` (`ProxmoxDeploy` — telemetry ingest, `relay_payload` with `provision` diagnostic), `sim_config.py`, `simulation_engine.py`, `demo_scenarios.py`, `token_store.py`, `data_models.py`, `dhcp_status.py`, `sim_primitives.py`, `agent_role.py`; `lm-spoke/role.py`, `lm-spoke/API_SPEC.md`, `lm-spoke/static/` (`dashboard.html`, `sim-views.js` — vendored copy of `lm/WebUI/sim-views.js`, re-sync when that changes).
- webui-spoke legacy: `webui-spoke/server.py`, `lm_relay.py` (`CSBridge`/`LMControlPlane`), `acme.py`.
- Clients: `clients/linux/agent.sh` + scripts, `clients/windows/*.ps1`, `clients/t3/*`; configs `configs/simulation.conf`, `configs/user-overrides.conf`.

## Notable behaviors & gotchas

- **lm-spoke is relay-only for Proxmox** — `proxmox_deploy.py` ingests telemetry + builds `relay_payload` (per-host `provision` diagnostic with `cs_enabled`/`loop_running`/`auto_provision_on`/`reason`/`halt`); the brain is `pxmx/agent/src/usb_provision.py`.
- **Client API port 8080** (was 8000) — at the time, the hub owned :8000 in hub mode; a second bind failed with `[Errno 98]` and crash-looped `lm-cs`. The hub has since moved to unified :443, but cs stays on 8080. Installer migrates stale `.env`.
- **Two flags trap** — tenant `usb_auto_provision` toggle ≠ per-agent `client_simulation.enabled`; the provision loop only spawns on the latter (the "enabled but nothing provisions" root cause).
- **store.set_hub_config REPLACES** — both `csSaveHubConfig` and `csSaveAutoProvConfig` must GET-merge-PUT or the two cards wipe each other.
- **CS_CONFIG_UPDATE handler** is required for hub config pushes (usb_vidpids, templates, sim/user overrides) to land — without it they silently dropped to "Unknown command" and `usb_vidpids` stayed `[]`.

## Related pages

[architecture-topology.md](architecture-topology.md), [pxmx.md](pxmx.md), [lm-hub.md](lm-hub.md), [environment-variables.md](environment-variables.md), [install-flags.md](install-flags.md).