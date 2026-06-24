# cs-webui

![Release](https://img.shields.io/badge/release-v1.0.0-success)

`cs-webui` is the shared browser frontend for the HPE Client-Sim platform. Version **v1.0.0** on `main` is the production frontend consumed by both Hub and Spoke deployments. The same `index.html`, `app.js`, and `style.css` are served by both:

- `webui-hub` in **hub** mode
- `client-sim/webui-spoke` in **spoke** mode

The backend injects `window.WEBUI_MODE` at runtime, so one codebase drives both experiences.

---

## Operators

### What this frontend does

The UI is the operator-facing surface for:

- viewing simulations and client status
- approving and managing spokes
- sending commands
- managing Proxmox VM/USB operations
- configuring Aruba Central, notifications, relay, and TLS
- checking service health and troubleshooting state

### How to access it

#### Spoke mode

Access the spoke UI from the spoke server itself.

Typical URL:

```text
http://<spoke-host>:8000
```

If spoke TLS is enabled, use HTTPS instead.

#### Hub mode

Access the hub UI over HTTPS.

Typical URL:

```text
https://<hub-host>:8443
```

Hub requires login. Spoke mode does not implement the same tenant-auth flow.

### Navigation by mode

#### Spoke mode tabs

| Tab | What it is for |
|---|---|
| **Simulations** | Current simulation buckets, counts, Central correlation, hardware alert status, client-count monitoring |
| **Clients** | Live client inventory, status, SSID, active simulations, errors, and per-client control panel. Client type tabs: **All**, **T1** (no USB dongle), **T2** (has USB dongle), **IoT/T3** (placeholder) |
| **Central** | Aruba Central overview plus site alerts, wireless client counts, and 24-hour history |
| **VM Server** | Proxmox VM and USB management; appears when Proxmox integration is active. Sub-tabs: **VMs**, **USB (T2)**, **IoT (T3)**, **VirtualHere**, **Command Queue**, **Details** |
| **API Server** | Spoke service status, health, and service log views |
| **Config** | Read/edit `simulation.conf` and view bucket profiles |
| **Setup** | Repository, VM/USB, Hub relay, Central API, notifications, TLS, and troubleshooting configuration |

#### Hub mode tabs

| Tab | What it is for |
|---|---|
| **Tenants** | Superadmin landing view and tenant selection |
| **Simulations** | Cross-spoke simulation summary inside a tenant context |
| **Clients** | Aggregate client list across the selected tenant |
| **Spokes** | Approved spokes, detail modal, processing mode, spoke health, and access to VM Server workflows for backup/reseed operations |
| **Commands** | Queue commands to a spoke |
| **Setup** | Tenant settings, notifications, API info, TLS, and pending spoke approval |
| **Config** | Tenant processing-mode summary/config |
| **Superadmin** | Tenant creation, user management, global kill switch state, and pending spoke approvals |

Hub mode does not expose VM backup as a standalone top-level tab. In v1.0, those controls appear in the hub-side VM Server context for eligible spokes, including backup, reseed, and related recovery actions.

### Spoke operator workflow

#### 1. Simulations

Use **Simulations** first to answer:

- which simulation buckets exist right now
- how many clients are attached to each bucket
- whether the expected Aruba Central check is passing or failing
- whether client-count or hardware-alert monitoring is reporting issues

#### 2. Clients

Use **Clients** for day-to-day VM troubleshooting:

- online/offline state (online means the last heartbeat was within 300 seconds)
- last seen time
- platform/hardware type
- active simulations
- Aruba impact badge
- recent client-side errors posted by `simulation.sh`

#### 3. VM Server / USB (T2) / IoT (T3)

When the spoke has a connected Proxmox agent, **VM Server** exposes sub-tabs:

| Sub-tab | Content |
|---------|---------|
| **VMs** | All simulation and other VMs with VMID, name, type, status |
| **USB (T2)** | USB dongle inventory, VID:PID, assigned VMs, missing/available status |
| **IoT (T3)** | IoT client VM inventory (T3 classification in development) |
| **VirtualHere** | VH hub/server name, device names, connection state, auto-use status |
| **Command Queue** | Queued and completed commands for clients and the Proxmox host |
| **Details** | Proxmox node info, agent version, and health statistics |

**T1 / T2 client classification** is visible on the **Clients** tab using the type filter buttons at the top of the client list:
- **T1** — client VM has no USB dongle assigned
- **T2** — client VM has an active USB dongle assignment (determined by the `has_usb` field on each client)
- **IoT (T3)** — placeholder for future classification

#### 4. Setup -> Hub

If the spoke will relay to Hub, go to:

```text
Setup -> Hub
```

That section holds:

- hub URL
- relay enable/disable
- spoke name
- tenant hint
- current registration/relay diagnostics

#### 5. Setup -> Central API

Use:

```text
Setup -> Central API
```

for Aruba Central credentials, site mappings, monitored checks, and hardware-alert settings.

#### 6. Setup -> Notifications

Use:

```text
Setup -> Notifications
```

for SMTP and Teams webhook configuration.

#### 7. Setup -> TLS Certificate

Use:

```text
Setup -> TLS Certificate
```

for spoke HTTPS settings and certificate request status.

#### 8. Setup -> Troubleshooting

Use:

```text
Setup -> Troubleshooting
```

for:

- system health summary
- service control buttons
- install/service log history
- Proxmox agent setup instructions
- relay diagnostics

### Hub vs Spoke differences

| Area | Hub mode | Spoke mode |
|---|---|---|
| Auth | JWT login, users, tenant roles, superadmin | Local UI; no tenant-login flow |
| Scope | Multi-tenant, many spokes | Single site/lab |
| Command target | Spokes in a tenant | Local VMs and Proxmox host |
| Proxmox controls | View through spoke detail/aggregate context | Native local VM/USB controls |
| Central config | Tenant-level settings | Local spoke polling and site mappings |
| Top-bar status | API auth, tenant context, auto-refresh | GitHub sync, local API status, relay indicator, Central API status |

---

## Developers

### Repository layout

```text
cs-webui/
├── templates/
│   └── index.html
├── static/
│   ├── app.js
│   └── style.css
├── shared/
│   └── shared_utils.py
├── VERSION
└── .githooks/
    └── pre-commit
```

### Runtime architecture

The frontend is intentionally simple:

- no bundler
- no npm build
- no framework runtime
- one HTML template
- one JS bundle
- one CSS file

#### Mode injection

`templates/index.html` sets:

```html
<script>window.WEBUI_MODE = "{{WEBUI_MODE}}";</script>
```

At runtime:

- Hub replaces it with `hub`
- Spoke replaces it with `spoke`

#### IIFE structure in `static/app.js`

`app.js` has three layers:

1. **shared preamble**
   - reads `window.WEBUI_MODE`
   - adds `mode-hub` or `mode-spoke` to `<body>`
2. **spoke IIFE**
   - runs only when `WEBUI_MODE === 'spoke'`
   - owns local clients, Proxmox, config editing, Central, setup, and logs UI
3. **hub IIFE**
   - runs only when `WEBUI_MODE === 'hub'`
   - owns login, tenant context, spoke detail, aggregate dashboards, and hub admin flows

This keeps hub and spoke logic in one file while avoiding runtime overlap.

### How to add a new tab or feature

#### Add a new tab

1. Add the tab button in `templates/index.html` using `data-tab="..."`.
2. Add a matching panel with id `tab-<name>`.
3. Put it under the correct root:
   - `#hub-root` for hub-only pages
   - `#spoke-root` for spoke-only pages
4. Add mode classes if needed:
   - `hub-only`
   - `spoke-only`
5. In `static/app.js`, wire the new tab into the correct mode-specific IIFE.
6. Add fetch/render/init logic for the new panel.
7. Update `static/style.css` if the layout needs new classes.

#### Add a new data-driven feature

1. Decide whether the backend source is Hub, Spoke, or both.
2. Add or reuse the backend endpoint in `webui-hub` or `client-sim/webui-spoke`.
3. Add UI fetch logic in the correct IIFE.
4. Render into the existing page/card/table pattern used in `index.html`.
5. If the feature belongs in both modes, keep selectors and function names mode-scoped so the two IIFEs stay isolated.

### UI conventions

- `btn-primary` is the default action button style and uses the solid HPE green fill.
- Use a more specific variant only when semantics require it, such as `btn-danger` for destructive actions or `btn-secondary` for secondary/outline actions.
- Keep online/offline UI behavior aligned to the shared 300-second timeout used by spoke and hub backends.

### Shared utilities

`shared/shared_utils.py` is the Python-side contract between Hub and Spoke for telemetry serialization helpers. Keep it in sync when the telemetry shape changes.

### Versioning

- Repo version is stored in `VERSION`.
- `main` is the production branch. Hub and Spoke both consume this repo from `main`.

#### How the version is bumped

`.githooks/pre-commit`:

- runs Python syntax checks on staged `.py`
- runs `bash -n` on staged `.sh`
- runs a lightweight JS balance check on staged `.js`
- bumps `VERSION` by `0.01` on non-`main` commits when files other than `VERSION` are staged

### How other repos consume this frontend

#### `webui-hub`

- copies `static/` and `index.html` into its runtime image
- injects `WEBUI_MODE=hub`

#### `client-sim/webui-spoke`

- `install-lxc.sh` downloads `static/app.js`, `static/style.css`, and `templates/index.html` from `cs-webui`
- injects `WEBUI_MODE=spoke`

### Local development notes

There is no standalone frontend dev server in this repo. The normal workflow is:

1. edit `templates/`, `static/`, or `shared/`
2. commit to `main`
3. redeploy/reinstall the spoke or rebuild hub so it consumes the updated assets

---

## Summary

`cs-webui` is the single source of truth for the platform UI. Hub and Spoke stay aligned by serving the same frontend and switching behavior with runtime `WEBUI_MODE` injection.
