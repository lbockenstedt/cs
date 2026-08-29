# AGENTS.md — `cs`

**Client-Sim** — the spoke-side runtime that simulates Linux/Windows/T3 clients.

- **Repo:** `github.com/lbockenstedt/cs`
- **Module type:** `module_type = "simulation"`
- **Canonical docs:** [`lm/docs/cs.md`](../lm/docs/cs.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Read this before editing — live vs legacy

| Path | Status |
| :--- | :--- |
| `lm-spoke/` | **LIVE.** `CSSpoke`, `CSControlPlane`, client API, sim engine, registry, command queue. Usually runs as the `simulation` role hosted by the generic agent (`lm-agent`, sub-spoke `{agent}-simulation`). |
| `clients/linux/`, `clients/windows/`, `clients/t3/` | **LIVE.** The simulation scripts themselves. |
| `webui-spoke/` | **LEGACY.** Standalone spoke backend, preserved for existing deployments. |
| `proxmox/` | **LEGACY.** The auto-provisioning brain moved to `pxmx/agent/src/usb_provision.py`. Retains the Azure-backed VM backup/reseed path. |

**Editing the legacy path is a silent no-op for the live system.**

## cs-specific gotchas

- **The spoke is relay-only for Proxmox.** It ingests telemetry and surfaces a `provision` diagnostic; the gate / VMID-gap-audit / clone logic runs in the **pxmx agent**.
- `install_cs.sh` (top level) is a wrapper around `lm-spoke/install_cs.sh`. It provisions a **cs-owned Kea** DHCP4 instance (`kea-dhcp4-sim`) on the 2nd NIC — **not dnsmasq**.
- Watch the **two-flag auto-provisioning trap** documented in `docs/cs.md`.
- `cs.log` is a committed runtime artifact, not source.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
