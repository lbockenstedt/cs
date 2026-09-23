# cs — Client Simulator & Test Automation Spoke (Lab Manager Module)

![Version](https://img.shields.io/badge/version-autobump%20.NN-blue)

The `cs` spoke module drives synthetic network client simulation, automated test scenarios, telemetry ingestion, and hardware device auto-clearing for the Lab Manager (LM) platform. Operating under `module_type = "simulation"`, `cs` manages simulated client workloads, ephemeral fault injection scenarios, tenant simulation quotas, isolated Kea DHCP scopes, and cloud network telemetry integrations (Aruba Central, Juniper Mist).

---

## Architecture

`cs` operates as the control, orchestration, and telemetry aggregation layer for client simulations:

```
┌─────────────────┐             WebSocket / TLS (:443)             ┌───────────────────────┐
│     LM Hub      │ ◄────────────────────────────────────────────► │       cs Spoke        │
│  Control Plane  │                                                │   (CSControlPlane)    │
└─────────────────┘                                                └───────────┬───────────┘
                                                                               │
                                                   ┌───────────────────────────┴───────────────────────────┐
                                                   │                                                       │
                                   HTTP / WS (:8080) / Kea DHCP (:8102)                 WebSocket / TLS (:443 / :8443)
                                                   │                                                       │
                                                   ▼                                                       ▼
                                      ┌────────────────────────┐                             ┌────────────────────────┐
                                      │  Simulated Clients     │                             │    pxmx Host Agent     │
                                      │ (Linux / Win / T3 VMs) │                             │   (Auto-Prov Engine)   │
                                      └────────────────────────┘                             └────────────────────────┘
```

1. **Active Spoke Coordinator (`lm-spoke/src/cs_spoke.py`):**
   - Dials outbound to the LM Hub control plane (`/ws/spoke` on port 443).
   - Serves the client API (`:8080`) over an isolated network bridge for client beaconing and profile resolution.
   - Hosts the split-topology agent listener (`/ws/agent`), enabling physical hypervisor agents to report directly to `cs`.
   - Dispatches incoming commands across specialized domain mixins: `AgentCommandsMixin`, `ClientCommandsMixin`, `ConfigCommandsMixin`, `IngestCommandsMixin`, and `SimCommandsMixin`.

2. **Simulation Engine (`lm-spoke/src/simulation_engine.py`):**
   - Deterministically calculates workload profiles (`s0` through `s9`) by hashing client hostnames (`crc32(hostname) % 10`).
   - Blends global simulation parameters, subnet targets, workload buckets, and user overrides.
   - Manages global emergency kill switches and iteration triggers.

3. **Isolated Sim DHCP Instance (`kea-dhcp4-sim`):**
   - Owns a dedicated Kea DHCP4 service running on an auto-detected secondary NIC (`169.253.1.1/24`).
   - Leases addresses in the `169.253.1.11`–`169.253.1.254` range with no default router option, keeping test traffic safely isolated.
   - Reports live lease metrics (`dhcp_status.py`) directly to the LM Hub dashboard.

---

## Features

- **Client Simulation Runner:** Executes realistic client workloads across Linux (`clients/linux/agent.sh`), Windows (`clients/windows/`), and T3 platforms, generating web browsing, continuous ping, file downloads, and iperf throughput traffic.
- **Automated Test Scenarios:** Injects ephemeral network and authentication faults with configurable time-to-live (120-minute auto-expiry): DNS failure, DHCP denial, port flapping, association drop, authentication failure, and SSID password failure.
- **Dedicated Kea DHCP Server:** Manages an isolated `kea-dhcp4-sim` instance, providing deterministic IP assignments and real-time lease tracking for simulated fleets without conflicting with infrastructure DHCP.
- **USB Auto-Clearing & Quarantine Automation:** Monitors physical USB dongle health, tracks failure strikes, isolates faulty radios into quarantine, and coordinates with Proxmox host agents for automatic USB re-initialization and host exclusion clearing.
- **Simulation Build Assistant Integration:** Full parity with the Lab Manager AI Build Assistant, enabling natural language scenario generation, dynamic quota enforcement, and intelligent failure remediation.
- **Tenant Quota Engine:** Enforces tenant-level client quotas (`sim_quota_engine.py`) across physical hypervisor pools, ensuring balanced hardware resource allocation.
- **Multi-Cloud AP Telemetry:** Actively polls and correlates telemetry from Aruba Central (Classic and New Central OAuth APIs) and Juniper Mist clouds to validate simulated client connectivity against physical AP infrastructure.

---

## Spoke Commands Reference Table

Commands routed and executed by `lm-spoke/src/cs_spoke.py` across its domain handlers:

| Command | Category | Description |
| :--- | :--- | :--- |
| `GET_VERSION` / `CS_GET_VERSION` | Lifecycle | Returns spoke module version and git commit SHA. |
| `UPDATE_CONFIG` / `CS_UPDATE_CONFIG` | Config | Updates spoke settings, credentials, and tenant parameters. |
| `CS_CONFIG_UPDATE` | Config | Ingests hub-pushed auto-provisioning parameters into local store. |
| `CS_GET_SIMULATION_STATE` | Simulation | Returns active simulation state, client counts, and current profile. |
| `CS_SET_SIMULATION_PROFILE` | Simulation | Adjusts global simulation workload profile (`s0`–`s9`). |
| `CS_TRIGGER_ITERATION` | Simulation | Forces an immediate simulation iteration cycle across all clients. |
| `CS_KILL_SWITCH` | Simulation | Activates or releases the global emergency stop kill switch. |
| `CS_GET_KILL_SWITCH` | Simulation | Queries the current operational state of the global kill switch. |
| `CS_DEMO_SCENARIO` | Test Scenarios | Applies an ephemeral, auto-expiring fault injection scenario to a client. |
| `CS_DEMO_CLEAR` | Test Scenarios | Clears active demo scenarios early and restores normal client profile. |
| `CS_GET_DEMO_ACTIVE` | Test Scenarios | Lists all active demo scenarios, affected hosts, and remaining TTLs. |
| `CS_GET_DEMO_SCENARIOS` | Test Scenarios | Queries the catalog of available demo scenario definitions. |
| `CS_GET_CLIENT_OVERRIDES` | Client Overrides | Retrieves persistent live override flags for a designated client. |
| `CS_SET_CLIENT_OVERRIDES` | Client Overrides | Sets persistent live override flags (e.g. `dns_fail`, `iperf`) for a client. |
| `CS_CLEAR_CLIENT_OVERRIDES` | Client Overrides | Clears persistent override flags for a specific client. |
| `CS_SET_ALL_CLIENT_OVERRIDES` | Client Overrides | Pushes override flags across all registered simulation clients. |
| `CS_CLEAR_ALL_CLIENT_OVERRIDES` | Client Overrides | Clears override flags across the entire simulation fleet. |
| `CS_DELETE_CLIENT` | Client Registry | Removes an individual client record from the simulation registry. |
| `CS_PURGE_CLIENTS` | Client Registry | Wipes all registered clients and resets the persistent client database. |
| `CS_GET_HOST_USB_OVERRIDES` | USB Automation | Retrieves host-specific USB provisioning overrides. |
| `CS_SET_HOST_USB_OVERRIDE` | USB Automation | Configures host-specific USB provisioning parameters. |
| `CS_CLEAR_HOST_USB_OVERRIDE` | USB Automation | Clears host-specific USB overrides. |
| `CS_PURGE_HOST` | Host Telemetry | Purges host telemetry state and clears USB exclusion tracking. |
| `CS_INGEST_TELEMETRY` | Telemetry Ingest | Ingests telemetry frames from Proxmox host agents. |
| `CS_INGEST_LOG` | Telemetry Ingest | Ingests execution and diagnostic logs from agents. |
| `CS_INGEST_PROGRESS` | Telemetry Ingest | Updates long-running operation progress in the local tracker. |
| `CS_INGEST_WATCHDOG_EVENT` | Telemetry Ingest | Logs hardware watchdog timeout events from nodes. |
| `CS_INGEST_HW_RESET` | Telemetry Ingest | Records hardware radio reset actions triggered by agents. |
| `CS_INGEST_COMMAND_RESULT` | Telemetry Ingest | Ingests terminal results from asynchronous background tasks. |
| `CS_POLL_AGENT_INBOX` | Command Queue | Delivers pending command queue actions to a polling agent. |
| `CS_ACK_COMMAND` | Command Queue | Acknowledges receipt and delivery of a dispatched command. |
| `CS_QUEUE_COMMAND` | Command Queue | Enqueues a command for host agent delivery. |
| `CS_REQUEUE_COMMAND` | Command Queue | Requeues a timed-out command with incremented retry count. |
| `CS_TOUCH_COMMAND` | Command Queue | Updates heartbeat timestamp on an in-progress queued command. |
| `CS_GET_COMMANDS` | Command Queue | Returns all commands currently residing in the command queue. |
| `CS_CLEAR_COMMANDS` | Command Queue | Clears completed, expired, or all items from the command queue. |
| `CS_DELETE_COMMAND` | Command Queue | Deletes a specific command from the queue by ID. |
| `CS_GET_HUB_CONFIG` | Local Store | Fetches locally persisted auto-provisioning parameters. |
| `CS_SET_HUB_CONFIG` | Local Store | Persists auto-provisioning knobs into `data/local_store.json`. |
| `CS_RESET_HUB_CONFIG` | Local Store | Resets auto-provisioning knobs to system defaults. |
| `CS_GET_CENTRAL_CONFIG` | Cloud Poller | Queries configured Aruba Central API credentials. |
| `CS_SET_CENTRAL_CONFIG` | Cloud Poller | Saves Aruba Central API credentials and initializes poller. |
| `CS_TEST_CENTRAL` | Cloud Poller | Executes validation test probe against Aruba Central API. |
| `CS_GET_MIST_CONFIG` | Cloud Poller | Queries configured Juniper Mist cloud API credentials. |
| `CS_SET_MIST_CONFIG` | Cloud Poller | Saves Juniper Mist credentials and starts tracker. |
| `CS_TEST_MIST` | Cloud Poller | Tests connectivity against Juniper Mist cloud endpoint. |
| `CS_GET_SIM_QUOTA_CATALOG` | Quota Engine | Returns catalog of simulation hardware templates and costs. |
| `CS_GET_SIM_QUOTA_STATE` | Quota Engine | Returns current quota allocations and tenant limits. |
| `CS_RESET_SIM_QUOTA` | Quota Engine | Resets tenant simulation quota ledger. |
| `CS_GET_DHCP_HEALTH` | DHCP Monitoring | Returns operational status and active lease counts for Kea DHCP. |
| `GET_AGENTS` | Agent Hosting | Returns connected Proxmox host agents dialed into this spoke. |
| `SET_AGENT_CONFIG` | Agent Hosting | Updates and pushes configuration to a connected host agent. |
| `SPOKE_RELAY` | Agent Hosting | Relays commands (`APPROVAL_SUCCESS`, `REVOKE_AGENT`, etc.) to agents. |
| `VNC_START` / `VNC_DISCONNECT` | Console Relay | Manages noVNC graphical console sessions for hypervisor VMs. |
| `SHELL_START` / `SHELL_DISCONNECT` | Console Relay | Manages interactive PTY terminal shell sessions on host nodes. |

---

## Installation & Configuration

<!-- INSTALLERS:START -->
Installers are idempotent — re-running updates code while preserving configuration and credentials.

### CS Spoke Installation (`install_cs.sh`)

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/install_cs.sh \
  | sudo bash -s -- --hub wss://lm-hub.example.com:443
```

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | LM Hub WebSocket URL (`wss://<host>:443`). Bare hostnames are automatically normalized. |
| `--id`, `--name` | Unique spoke identifier (defaults to `<hostname>-cs`). |
| `--secret` | Pre-shared key for authenticating with the hub. |
| `--hub-secret` | Hub PSK enabling automatic spoke approval. |
| `--dhcp-iface IFACE` | Secondary network interface for the isolated simulation DHCP scope. |
| `--no-dhcp` | Skips Kea DHCP setup. |
| `--tls-verify` | Enables strict TLS certificate verification against `--tls-ca-cert`. |
| `--tls-ca-cert PATH` | Custom CA certificate path for TLS validation. |
| `--infra-only` | Host preparation only (cs Kea + 2nd NIC + agent-listener cert). Used when loaded as an agent role. |
| `--purge-env`, `--reset-identity` | Deletes existing identity credentials and regenerates `/etc/machine-id`. |

### Environment Variables (`/opt/lm/cs/.env`)

- `HUB_URL`: LM Hub WebSocket endpoint.
- `SPOKE_ID`: Unique spoke identifier.
- `SPOKE_SECRET`: Pre-shared authentication secret.
- `CS_API_PORT`: Client API listening port (default `8080`).
- `CS_API_HOST`: Client API binding address (default `0.0.0.0`).
- `LM_CS_AGENT_LISTENER`: Enables local `/ws/agent` listener for split-topology agent hosting (`1`).
- `CS_TELEMETRY_INTERVAL_S`: Telemetry push interval in seconds (default `10`).
<!-- INSTALLERS:END -->

---

## Testing & Verification

Execute the comprehensive test suite from the repository root:

```bash
pytest tests
```
