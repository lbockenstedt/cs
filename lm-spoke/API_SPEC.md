# Client Simulator (CS) Spoke API Specification

The Client Simulator Spoke generates synthetic client network traffic (DNS
failure, assoc/auth failure, ping/download/iperf/www traffic, port flap, etc.)
to test infrastructure resilience and security policies. It is an **LM spoke**
(`module_type = "simulation"`) that runs standalone locally and reports to the LM
hub, and is drivable from **two sides identically**: LM hub commands (the `CS_*`
contract below) and, in Phase 2+, a local FastAPI mgmt API (`/api/...`).

All business logic lives in plain modules (`sim_config`, `sim_primitives`,
`simulation_engine`, and — Phases 2/3 — `client_registry`, `command_queue`,
`proxmox_deploy`). `CSSpoke.handle_command` and the HTTP routes are thin
dispatchers over the same modules.

## Config model

Two INI files under `configs/`:

- `simulation.conf` — `[simulation]` globals, `[server]`, `[address]`, and ten
  bucket profiles `[s0]`–`[s9]`.
- `user-overrides.conf` — per-username overrides; may pin `simulation_id=sX`.

**Bucket assignment:** `zlib.crc32(hostname) % 10` → `s0`–`s9`
(`username = hostname.split('-')[0]`).

**Resolution order (last wins):**
`[simulation]` → `[address]`/`[server]` → `[sX]` bucket → `[username]` override.

The single resolver is `sim_config.resolve_profile(hostname, sim_conf, user_conf)`,
called by the engine, the `GET /api/config` route (Phase 2), and `CS_GET_SIMULATION_STATE`.

## LM-spoke command set (`CS_*`)

Dispatched in `CSSpoke.handle_command`. `*_GET_STATUS` falls back to
`get_status()`. Legacy non-prefixed aliases kept for back-compat.

### Identity & status
| Command | Purpose | Response |
|---|---|---|
| `GET_VERSION` / `CS_GET_VERSION` | Spoke version | `{"status","version"}` |
| `CS_GET_STATUS` | Composite status | `get_status()` fallback |
| `CS_GET_TELEMETRY` / `CS_GET_CLIENTS` | Client list (Phase 2 fills `clients`) | `{"self", "clients", "kill_switch"}` |

### Simulation execution
| Command | Payload | Response |
|---|---|---|
| `CS_TRIGGER_ITERATION` (`TRIGGER_ITERATION`) | `{}` | iteration result `{hostname,bucket,active_sims,status,iteration,results}` |
| `CS_GET_SIMULATION_STATE` (`GET_SIMULATION_STATE`) | `{}` | resolved profile + active sims |
| `CS_SET_SIMULATION_PROFILE` (`SET_SIMULATION_PROFILE`) | `{"profile": {..}}` | `{"status","message"}` |
| `CS_START_SIMULATION` | `{"iterations":100,"iter_sleep":5}` | `{"status","message"}` |
| `CS_STOP_SIMULATION` | `{}` | `{"status","message"}` |

### Config
| Command | Payload | Response |
|---|---|---|
| `CS_GET_CONFIG` | `{}` | `{"simulation_conf","user_overrides","mode"}` |
| `CS_UPDATE_CONFIG` (`UPDATE_CONFIG`) | `{"content":"<ini>"}` | `{"status","message"}` (validates INI) |
| `CS_UPDATE_USER_OVERRIDES` | `{"content":"<ini>"}` | `{"status","message"}` (validates INI) |

Central config commands and `CS_CONFIG_UPDATE` accept `central_config_clear_secrets`
and `central_on_prem_config_clear_secrets` lists of `client_secret`, `access_token`, or
`refresh_token`, or nested `<field>_clear: true` flags. Clears remove local credentials
and reload the poller; blank fields preserve them. Updated hubs persist nested flags
for reconnect replay. Standalone Central status returns only public settings and `*_set` flags.

### Kill switch
| Command | Payload | Response |
|---|---|---|
| `CS_KILL_SWITCH` | `{"on": true}` | `{"status","kill_switch"}` — stops all sims; written to `configs/kill_switch.txt` |

### Phase 2 (client registry / command queue)
| Command | Purpose |
|---|---|
| `CS_QUEUE_COMMAND` | Enqueue a control command for a client |
| `CS_GET_COMMANDS` / `CS_CLEAR_COMMANDS` | List / purge the queue |

### Phase 3 (Proxmox deploy / lifecycle)
| Command | Purpose |
|---|---|
| `CS_GET_PROXMOX_STATUS` / `CS_GET_PROXMOX_LOGS` | Node + VM status / reclone log |
| `CS_DEPLOY_CLIENTS` / `CS_RECLONE_ALL` | Reclone clients from a template |
| `CS_VM_ACTION` | start/stop/delete/config a VM |
| `CS_APPROVE_AGENT` / `CS_REJECT_AGENT` | Approve/reject a pending client |
| `CS_UPDATE_AGENT` | qm guest exec install/update |
| `CS_SELF_UPDATE` | `perform_self_update_check()` |

## Client API (port 8080, both hub and standalone mode)

The spoke that owns the isolated sim-client DHCP scope (`169.253.1.1/24` on the
2nd NIC) is also the client API gateway on `169.253.1.1:8080`. The full
client-facing surface is implemented (`lm-spoke/src/client_api.py`,
`build_client_api_app`) and served on `0.0.0.0:8080` — configurable via
`CS_API_PORT` / `CS_API_HOST` (env or `--port` / `--host`). The same app runs in
hub mode (a `uvicorn.Server.serve()` task started in `CSControlPlane.run()`,
surviving hub reconnects) and standalone mode (`run_standalone_mode()`), so both
serve identical routes. Auth is a shared `client_api_key` (`CSSettings` generates
one on first start, never left empty). Callers on the isolated sim segment
(`169.253.1.0/24`) are trusted by network position (the t3 agent sends no key);
everyone else must present it on `/ws/client` and the mutating/inbox HTTP routes
(`X-Client-Key` header or `?api_key=`, `secrets.compare_digest`).

The operator surface on the same port (the local dashboard `/`, every
`/sim/api/*` route, and `GET /status`, `POST /simulate/trigger`,
`GET|POST /config`) answers loopback callers only: a process on the spoke, or
an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 <spoke>`). The `Host` header must
name loopback (`127.0.0.1`, `localhost`, `::1`), which stops DNS rebinding, and
an API request a browser marks cross-site (`Sec-Fetch-Site`) or whose `Origin`
is not that host is refused, so another web page open in the operator's
browser can't drive the API through the tunnel. Everyone else gets 403, and
`/` shows a short page saying how to reach it. Sim VMs are not trusted
there, even on the sim segment, and `client_api_key` is not accepted, because
every sim VM can fetch it from `/api/client/key`. There is no remote operator
key: 8080 is plaintext by default, so a key sent to it could be read off the
wire. The hub never calls these routes; it drives the spoke over its
WebSocket command channel, so the Lab Manager WebUI is unaffected. The
`UPDATE_SETTINGS` reply replaces `client_api_key` with a `client_api_key_set`
flag.

| Route | Purpose |
|---|---|
| `GET /api/health` | `{status, version, clients, repo_synced, repo_error}` (public) |
| `GET /api/kill-switch` | `on`/`off` plaintext (`engine.kill_switch_active()`) |
| `POST /api/status` | client → spoke status beacon → `ClientRegistry.apply_status` (public) |
| `GET /api/client/key` | `{"client_api_key": ...}` — sim-segment callers only (403 off-segment) |
| `GET /api/config?hostname=` | rendered `simulation.conf` (host bucket + registry overrides baked in) |
| `GET /api/config/overrides` | `user-overrides.conf` plaintext |
| `GET /api/config/parsed` | `{section: {key: value}}` |
| `GET /api/scripts/list?platform=linux\|windows\|t3` | filenames in `clients/{platform}/` |
| `GET /api/scripts/{platform}/{filename}` | `FileResponse` (path-traversal guarded) |
| `GET /api/clients` | registry snapshot |
| `POST /api/clients/{hostname}/control` | `{overrides}` → `registry.set_overrides` (operator surface: loopback only, like `/status`; anyone else → 403) |
| `DELETE /api/clients/{hostname}/control` | clear overrides (operator surface: loopback only; anyone else → 403) |
| `POST /api/commands` | `{target, action, args, type}` → `queue.enqueue` (operator surface: loopback only, anyone else → 403, because a `"proxmox"` command reaches the Proxmox hosts; safeguard refuse → 400: a `vmid`/`vm_id`, or any `delete_vms` id, that is not a whole number, is below 90000 or is protected, for any target) |
| `GET /api/commands` | `queue.list_commands()` (operator surface: loopback only, like `/status`; anyone else → 403) |
| `GET /api/inbox?hostname=` | `queue.poll_agent_inbox()` (key-gated). Only the loopback operator gets a fleet poll. Any other caller, keyed or not, on the sim segment or off it, gets only that hostname's `SIM_CLIENT_ACTIONS` commands, never `target:"proxmox"` or VM actions: every sim VM can fetch the key. Proxmox agents poll through the hub (`CS_POLL_AGENT_INBOX`) |
| `POST /api/inbox/ack` | `{id, status, message, result}` → `queue.ack_command` (operator surface: loopback only; anyone else → 403). Sim clients ack over `/ws/client`, Proxmox agents through the hub (`CS_ACK_COMMAND`); unknown id → 404 |
| `WS /ws/client?hostname=&platform=&api_key=` | primary agent channel (see below) |
| `GET /status` · `POST /simulate/trigger` · `GET\|POST /config` | mgmt (same as the old standalone surface; loopback only) |
| `GET /version` | spoke version (public) |
| `GET /` · `/sim/api/*` | local dashboard and its API (loopback only) |

### `/ws/client` protocol
- Auth: sim-segment callers connect open (the t3 agent sends no key); else `secrets.compare_digest(api_key, key)`, close `4403` on mismatch. The linux agent fetches `/api/client/key` first (sim-segment only) when it wants to pass a key along.
- On accept: server sends `{type:"hello"}`, then pushes pending commands (`{type:"commands", commands:[{id,action,args,type}]}`, marks them `delivered`). Only commands addressed to this hostname whose action is a sim-client one (`restart_sim`, `reboot`, `update_now`, `kill_switch`, `debug_mode`) are pushed; `target:"proxmox"` and VM actions wait for the Proxmox agent's hub poll (`CS_POLL_AGENT_INBOX`).
- Client → server: `{type:"status", payload}` → `{type:"status_ack"}`; `{type:"ack", payload:{id,status,message,...}}` → `queue.ack_command` → `{type:"ack_ok", ok}` (`ok:false` for a command this socket could not have been sent); `{type:"sync"}` → re-push pending; `{type:"ping"}` → `{type:"pong"}`.
- Live push: `CS_QUEUE_COMMAND` (hub) calls `client_api.push_pending(spoke, target)` so a queued command reaches a connected agent immediately, without waiting for its next `sync`.

## Architecture
- **Bucket assignment** — deterministic `s0`–`s9` via CRC32 of the hostname.
- **Execution model** — `run_iteration` resolves the profile, checks the kill
  switch, applies `sim_phy` adapter setup, gates on `sim_load`, and dispatches
  every enabled primitive concurrently (bounded, per-sim timeout). `run_loop`
  repeats for 100 iterations, then a post-cycle (apt update + optional offline
  window). Each iteration writes a status beacon to `data/client-status.json`.
- **Graceful degradation** — primitives detect their tool (dnspython/icmplib/
  httpx/nmcli/ip/iperf3/dig/dhclient) and return `{"degraded": true, "missing": ...}`
  rather than raising, so the spoke runs on any host.
- **Integration flow** — Hub sends a signed WebSocket `CS_*` command →
  `CSSpoke.handle_command` dispatches to a module → module result returned as
  `COMMAND_RESULT`. (Phase 2+: client VMs POST `/api/status` beacons and pull
  `/api/config` + `/api/scripts/*`.)

## Deferred (stubbed, not implemented in Phase 1)
Aruba Central polling, ACME TLS, Azure backups, T3 wireless MAC profiling,
enterprise auth (LDAP/RADIUS/TACACS).