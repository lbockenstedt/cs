# HPE Client-Sim Platform Architecture

This document describes the current Client-Sim platform as implemented across `webui-hub`, `client-sim`, and `cs-webui`.

---

## Audience

### Operators
Use this document to understand what runs where, what data is persisted, and how commands and telemetry move through the system.

### Developers
Use this document to understand component boundaries, relay flows, persistence files, and where to extend the platform.

---

## System Overview

Client-Sim is a hub-and-spoke simulation platform for running large sets of client devices, collecting their status, and optionally correlating those local simulations with Aruba Central data.

At a high level:

- **Hub (`webui-hub`)** is the multi-tenant central control plane.
- **Spoke (`client-sim/webui-spoke`)** is the local FastAPI control plane inside each site/lab.
- **`cs-webui`** is the shared browser UI served by both Hub and Spoke.
- **Proxmox agent (`client-sim/proxmox`)** runs on the Proxmox host and manages local VM/USB work.
- **Simulation scripts (`client-sim/linux`)** run inside simulation VMs and generate traffic/failure conditions.

---

## Operator View

### Deployment Topology

```text
                           +----------------------------------+
                           | Hub: webui-hub                   |
                           | Azure Container Instance         |
                           | HTTPS :8443                      |
                           | /data JSON store                 |
                           +----------------+-----------------+
                                            ^
                                            | tenant-scoped relay
                                            | JWT UI auth + X-API-Key
                                            v
+-----------------------------------------------------------------------------------+
| Spoke site                                                                          |
|                                                                                     |
|  +----------------------------------+        local API        +------------------+ |
|  | Spoke: webui-spoke               | <---------------------> | Proxmox Agent    | |
|  | Proxmox LXC                      |                         | Proxmox host      | |
|  | HTTP/HTTPS local UI/API          |                         | qm / pct / USB    | |
|  | settings.json + state_cache.json |                         | state file        | |
|  +----------------+-----------------+                         +---------+--------+ |
|                   ^                                                     |          |
|                   | config, scripts, inbox, status                      | provision |
|                   |                                                     | telemetry |
|                   v                                                     v          |
|          +-----------------------+                             +-----------------+ |
|          | Simulation VMs        | --------------------------> | Aruba Central   | |
|          | Linux client scripts  |   simulated traffic/errors  | API / alerts    | |
|          +-----------------------+ <-------------------------- +-----------------+ |
|                                      spoke or hub polls Central                   |
+-----------------------------------------------------------------------------------+
```

### What each component does

| Component | Runs where | Main job |
|---|---|---|
| Hub | Azure Container Instance | Central multi-tenant management, spoke approval, aggregate telemetry, command relay, user/tenant administration |
| Spoke | Proxmox LXC | Local dashboard/API, config/script distribution to clients, Central polling, command queue, relay to Hub |
| `cs-webui` | Served by Hub and Spoke | Shared single-page UI; backend injects `WEBUI_MODE=hub` or `WEBUI_MODE=spoke` |
| Proxmox agent | Proxmox host | Reports node/VM/USB state, polls local inbox, provisions and reclones VMs, tracks USB assignments |
| Simulation scripts | Linux VMs | Pull config/scripts, run traffic/failure simulations, post heartbeat/errors, poll client inbox |

### Command flow

#### Hub to spoke/proxmox/VM path

1. Operator issues a command in Hub.
2. Hub stores it in the tenant/spoke queue and exposes it on the spoke inbox endpoint.
3. Spoke relay loop polls Hub and converts relay commands into local spoke state/queues.
4. If the command targets Proxmox work, the Proxmox agent polls the spoke inbox and executes it on the host.
5. If the command targets a client VM, the client polls the spoke inbox and executes it locally.
6. Agent or client ACKs completion back to the spoke; the spoke can relay result state back to Hub.

Typical examples:

- Hub `reclone` -> spoke inbox -> Proxmox agent -> `qm clone/start/guest exec`
- Hub `kill_switch` -> spoke queue -> client `agent.sh` -> edit `simulation.conf` -> send `SIGUSR1`
- Hub `repo_sync` / `update_now` -> spoke background task or spoke/agent command execution

### Telemetry flow

#### VM and host telemetry back to Hub

1. Simulation VMs POST heartbeat and error data to `webui-spoke` via `/api/status`.
2. Proxmox agent POSTs node, VM, USB, and reclone state to `/api/proxmox/telemetry`.
3. Spoke merges that into in-memory state and persists key snapshots to disk.
4. If relay is enabled, spoke sends tenant-scoped telemetry to Hub.
5. Hub stores the latest telemetry under the approved spoke record and exposes aggregate views to the shared UI.

### State persistence

#### Spoke persistence

| File | Purpose |
|---|---|
| `settings.json` | Persisted runtime settings, including relay settings, notifications, Central config, and approved Proxmox agent keys |
| `.secret_key` | Fernet key used to encrypt sensitive spoke settings at rest |
| `state_cache.json` | Cached snapshot of Proxmox/Central state used for restart recovery |
| `command_queue.json` | Local client/proxmox command queue |
| `reclone_state.json` | Rolling reclone job state for VM operations |
| `relay_state.json` | Hub relay status and state bookkeeping |
| `update_state.json` | Self-update/version check state |
| `vm_watchdog.json` | VM watchdog/autorecovery state |
| `central_history.jsonl` | Aruba Central history log |
| `client_history.json` | Retained client history on disk |
| `client_count_baseline.json` | Baseline data for client-count monitoring |

#### Proxmox host persistence

| File | Purpose |
|---|---|
| `/etc/client-sim-proxmox-agent.env` | Agent server URL, API key, branch, intervals, and port |
| `/etc/client-sim-usb-state.conf` | Tab-delimited USB-to-VM assignment state used by the agent |
| `/var/lib/client-sim/reclone-state.json` | Current reclone status cache |
| `/var/lib/proxmox-watchdog/state` | Watchdog failure counter |
| `/var/log/client-sim-proxmox-agent.log` | Agent runtime log |
| `/var/log/proxmox-watchdog.log` | Proxmox watchdog log |

#### Simulation VM persistence

| File | Purpose |
|---|---|
| `/usr/local/scripts/simulation.conf` | Active simulation config |
| `/usr/local/scripts/user-overrides.conf` | Per-user overrides |
| `/usr/local/scripts/usb-phy-override.conf` | Agent-written `sim_phy` override for USB-backed provisioning |
| `/usr/local/scripts/sim.log` and `debug-*.log` | Local runtime and debug logs |

#### Hub persistence

| Path under `DATA_DIR` (`/data` in container) | Purpose |
|---|---|
| `users.json` | Hub users, bcrypt hashes, superadmin flag, tenant roles |
| `tenants.json` | Tenant metadata and encrypted tenant settings |
| `pending/*.json` | Spokes waiting for approval |
| `{tenant_id}/islands.json` | Approved spoke records, config, telemetry, last-seen state |
| `{tenant_id}/queue/{spoke_id}.json` | Tenant-scoped command queue |
| `{tenant_id}/audit/{spoke_id}.json` | Rolling audit/task history |
| `tls/*` | Generated or mounted TLS cert/key |

### Security model

#### Operator-facing summary

- **Hub login uses JWT access tokens** and bcrypt password hashes.
- **Tenant isolation is explicit**: non-superadmin users can only access tenants in their `tenant_roles` list.
- **Spokes do not self-attach to tenants automatically**. A spoke registers first, then waits for approval.
- **Hub-to-spoke relay uses per-spoke API keys** stored encrypted at rest in Hub.
- **Spoke-to-Proxmox communication also uses an approval step**. The Proxmox agent registers, waits in pending state, then receives a generated key after approval.
- **Spoke secrets are encrypted at rest** with `.secret_key` when cryptography support is available.
- **Global kill switch is polled from the upstream HPE-controlled location**, not trusted from a forked repo copy.

### Approval flow

#### Hub approval

1. A spoke calls `POST /api/spokes/register` on Hub.
2. Hub stores a pending record under `/data/pending/<spoke_id>.json`.
3. Superadmin or tenant admin approves the pending spoke.
4. Hub moves it into the tenant's approved spoke list, generates a relay API key, and returns it once.
5. Future telemetry/inbox/ack calls must include `X-API-Key` for that tenant/spoke.

#### Proxmox agent approval on a spoke

1. Agent installer starts the host service.
2. Agent calls `/api/proxmox/register` with its hostname.
3. Spoke places it in a pending list.
4. Operator approves it from the spoke UI or `POST /api/proxmox/approve/{hostname}`.
5. Agent polls `/api/proxmox/key` until approved, saves the returned key, and uses it for future authenticated calls.

### Version numbering scheme

Client-Sim is versioned per component, not as one monolithic platform version.

| Component | Current scheme in repo | Notes |
|---|---|---|
| `cs-webui` | Repo `VERSION` file (for example `2.75`) | Pre-commit bumps the repo version on non-`main` commits |
| `webui-hub` | Repo `VERSION` file (currently `1.20`) | Used by `/api/health` and deployment flow |
| Spoke installer / Proxmox agent | Embedded script versions (currently `2.39`) | `install-lxc.sh` and `proxmox-agent.sh` track their own release number |
| Client scripts | Per-script `version=.NN` headers | Example: `simulation.sh`, `startup.sh`, `update.sh` each advance independently |

Branch convention in active docs and installers:

- `main` = production branch

---

## Developer View

### Component boundaries

#### `cs-webui`

- `templates/index.html` holds the shared DOM skeleton for both modes.
- `static/app.js` contains one shared preamble plus two large mode-specific IIFEs.
- `shared/shared_utils.py` keeps shared telemetry serialization logic aligned between Hub and Spoke.

#### `client-sim/webui-spoke`

- FastAPI app in `server.py`
- Serves local UI/API
- Owns client inbox, Proxmox inbox, Central polling, repo sync, relay loop, WebSocket broadcast, and local persistence

#### `client-sim/proxmox`

- `proxmox-agent.sh` is a host-side daemon
- Maintains USB certification/assignment state
- Runs VM lifecycle operations with `qm`/`pct`
- Pushes telemetry and consumes host-targeted commands

#### `client-sim/linux`

- `startup.sh` bootstraps one VM session
- `simulation.sh` owns the long-running 100-iteration exec-restart loop
- `update.sh` refreshes config/scripts from spoke, SMB, or GitHub
- `agent.sh` polls `/api/inbox` and executes VM-side commands

#### `webui-hub`

- FastAPI app with JSON-backed persistence in `app/store.py`
- Multi-tenant routing, pending-spoke approval, per-tenant settings, command queue, audit history, and aggregate views
- Background workers in `app/tasks.py` handle gkill polling, heartbeat checks, schedule processing, Aruba polling, and maintenance

### Command path in code terms

```text
Hub UI
  -> Hub API command queue (`/data/{tenant}/queue/{spoke}.json`)
  -> Spoke relay loop (`server.py`)
  -> local spoke queue or proxmox/client inbox
  -> Proxmox agent `process_inbox()` or VM `agent.sh`
  -> ACK to spoke
  -> optional relay/result state back to Hub
```

### Telemetry path in code terms

```text
VM `simulation.sh`
  -> POST /api/status on spoke
  -> spoke in-memory `clients` state + persistence
  -> optional relay to Hub

Proxmox agent `collect_telemetry()`
  -> POST /api/proxmox/telemetry on spoke
  -> spoke `proxmox_state`
  -> optional relay summary to Hub
```

### Spoke runtime model

`server.py` starts these long-running background tasks during FastAPI lifespan:

- `sync_repo`
- `heartbeat`
- `central_token`
- `central_poller`
- `update_checker`
- `relay`
- `client_history_saver`
- `command_expiry`
- `auto_recovery`
- `vm_watchdog`
- `schedule_check`
- `gkill_switch`
- `baseline_saver`
- `acme_renewal`

The spoke keeps most state in memory and snapshots key parts to JSON for restart recovery.

### Hub runtime model

`webui-hub` starts these main background workers in `app/tasks.py`:

- global kill switch poller
- heartbeat monitor
- auto-recovery checker
- schedule checker
- Aruba poller
- state engine / maintenance loops
- ACME renewal check

Hub persistence is intentionally file-backed JSON rather than a database.

### Notable implementation details

- **Atomic writes** are used for key JSON files on both Hub and Spoke.
- **Hub approval is identity-first**: a spoke is pending until explicitly approved and assigned.
- **Relay auth is tenant-scoped**: telemetry/inbox/ack routes require `tenant_id`, `spoke_id`, and `X-API-Key`.
- **Simulation restarts are graceful**: VM-side `restart_sim` and `kill_switch` use `SIGUSR1`, not process-kill shortcuts.
- **USB provisioning is stateful on the Proxmox host**: the agent stores bus-path mappings in `/etc/client-sim-usb-state.conf` and reconstructs them on restart.
- **Mode injection happens at render time**: Hub replaces `{{WEBUI_MODE}}` with `hub`; Spoke replaces it with `spoke`.

### Extension points

#### Adding a new hub-distributed feature

1. Add or update the backend endpoint and persistence model in `webui-hub`.
2. Decide whether the feature is centralized or distributed via processing mode.
3. If distributed, define the queued command type and ACK/result handling.
4. Update `client-sim/webui-spoke` relay or local execution logic as needed.
5. Add UI in `cs-webui` under the correct mode-specific IIFE.

#### Adding a new local spoke/VM command

1. Add the command producer in spoke or hub UI/backend.
2. Add queue/inbox handling in `client-sim/webui-spoke/server.py` or relay mapping.
3. Implement execution in either `client-sim/proxmox/proxmox-agent.sh` or `client-sim/linux/agent.sh`.
4. Return an ACK/result so UI state clears correctly.

---

## Current Architectural Summary

- **Hub is optional for local operation**: a spoke can run standalone.
- **Spoke is the local source of truth for clients and Proxmox state**.
- **Hub is the multi-tenant aggregation and approval layer**.
- **`cs-webui` is a single frontend served in two modes**.
- **Persistence is file-based JSON throughout the control plane**.
- **Command execution is pull-based at every boundary**: hub->spoke, spoke->agent, spoke->VM.

