# cs — docs

This repo carries a copy of its LM feature page plus the shared topology page:

- [cs.md](cs.md) — this module's feature reference (purpose, ports, env vars, install flags, commands, gotchas).
- [architecture-topology.md](architecture-topology.md) — the shared LM backbone (hub/spoke/agent mesh, WS+TLS scheme, discovery, signing, onboarding, self-update, state/tenancy).

The **full canonical doc set** (all modules, the env-var reference, the install-flag reference, and the hub/WebUI/generic-agent pages) lives in [`lm/docs/`](../../lm/docs/README.md) — specifically [`lm/docs/cs.md`](../../lm/docs/cs.md).

## Also in this repo

- [CLIENT_SIM_CHANGELOG.md](CLIENT_SIM_CHANGELOG.md), [CLIENT_SIM_VERSION.md](CLIENT_SIM_VERSION.md) — **historical/legacy** notes covering the pre-reset standalone `webui-spoke` + per-script `MAJOR.MINOR` versioning era; version labels here do not reflect the repo's current autobumped `.NN` `VERSION`. Each carries a top-of-file notice.
- [DEBIAN_COMPATIBILITY.md](DEBIAN_COMPATIBILITY.md) — legacy Debian-readiness analysis of the `clients/linux/` scripts (some recommended fixes have since shipped).
- [SECURITY.md](SECURITY.md) — generic security-policy template for the legacy client-sim scripts.
- [terminal-layout.md](terminal-layout.md) — GNOME-terminal autostart layout for the `clients/linux/` sim VMs.
