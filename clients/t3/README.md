# T3 — Virtual Wireless Interface Simulator

T3 is the third-tier simulation platform. Where T1 and T2 simulate network traffic inside VMs, T3 runs on a **Raspberry Pi** (or any Linux host with `mac80211_hwsim`) and creates up to **25 virtual WiFi interfaces** (`vwlan1`–`vwlan25`), each programmed with a realistic vendor OUI-based MAC address and DHCP fingerprint.

T3 devices are managed through the hub/spoke platform:
- The spoke is the T3 device's local API endpoint
- The hub manages MAC profiles and the OUI pool fleet-wide
- Operators push new MAC profiles from the hub UI; T3 devices pick them up on the next update cycle

---

## File overview

| File | Purpose |
|------|---------|
| `wireless.sh` | Core simulation engine — creates virtual interfaces, assigns MACs, runs DHCP |
| `gen_macs.sh` | Regenerates MAC addresses and udev rules from `mac_config.json` |
| `update_script.sh` | Self-update script with three-tier fallback (API → GitHub → SMB) |
| `install.sh` | Installer — sets up systemd service, udev rules, mac_config.json |
| `mac_config.json` | Default OUI profile (25 interfaces, one per vendor) |
| `t3-simulation.service` | systemd unit file for headless operation |
| `ini-parser.sh` | INI config file parser for `simulation.conf` |

---

## How MAC addresses are generated

Every virtual interface gets a MAC address in the format:

```
OUI(3 octets) : 07 : HOST_SEED : IFACE_NUM
```

| Octet | Value | Source |
|-------|-------|--------|
| 1–3 | Vendor OUI | `mac_config.json` entry |
| 4 | `07` | Hardcoded |
| 5 | `md5sum(hostname)[:2]` | Deterministic host seed (same hostname → same seed) |
| 6 | Interface number `01`–`25` | Interface index |

**Example** — host `client-sim-01`, first Apple interface:
```
3c:15:c2:07:<md5-seed>:01
```

If the hostname does not end in a number, the last two octets are derived from a hash of the full hostname. A warning is printed during install/generate but the device still works.

---

## mac_config.json format

```json
[
  { "vendor": "Apple",     "oui": "3c:15:c2", "count": 5 },
  { "vendor": "Samsung",   "oui": "a4:50:46", "count": 4 },
  { "vendor": "Microsoft", "oui": "60:45:bd", "count": 3 }
]
```

- `oui` — first three octets in lowercase colon notation
- `count` — number of interfaces to create for this vendor
- Total `count` across all entries must be **≤ 25**

This file is managed by the hub. The spoke caches the latest version pushed by the hub, and T3 devices pull it via:

```
GET <spoke-url>/api/scripts/t3/mac_config.json
```

---

## Hub/spoke integration

T3 devices integrate with the hub/spoke platform as first-class managed nodes.

### Device registration

T3 devices POST a heartbeat to the spoke on startup and at regular intervals:

```bash
curl -s -X POST http://<spoke>:8000/api/status \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"client-sim-01","hw_type":"t3","simulation_id":"t3-lab"}'
```

The `hw_type=t3` field causes the spoke and hub to track these devices separately from standard Linux/Windows simulation clients.

### Pulling config from spoke

`update_script.sh` downloads scripts and config from the spoke on a regular cadence:

```
GET <spoke-url>/api/scripts/t3/wireless.sh
GET <spoke-url>/api/scripts/t3/gen_macs.sh
GET <spoke-url>/api/scripts/t3/mac_config.json
GET <spoke-url>/api/scripts/t3/oui_pool.json   ← optional
```

### MAC profile update flow

1. Admin builds a MAC profile in the Hub UI (Spoke → T3 → MAC Profile Builder)
2. Hub calls `PUT /api/{tenant_id}/spokes/{spoke_id}/t3/mac-profile`
3. Hub queues a `t3_mac_update` command for the spoke
4. Spoke picks up the command on the next inbox poll and writes `mac_config.json` locally
5. `wireless.sh` detects the MD5 hash change on the next cycle and calls `gen_macs.sh`
6. `gen_macs.sh` live-swaps virtual interface MACs and rewrites udev rules

---

## Installation

```bash
sudo bash install.sh
```

The installer:
1. Loads `mac80211_hwsim` with `radios=25`
2. Generates udev rules based on the hostname seed
3. Copies `gen_macs.sh` and `mac_config.json` to `/opt/t3-sim/`
4. Installs the systemd service (`t3-simulation.service`)
5. Enables headless auto-start

### Headless operation

The service runs without any display. To enable on boot:

```bash
sudo systemctl enable t3-simulation.service
sudo systemctl start t3-simulation.service
```

Check status:

```bash
sudo systemctl status t3-simulation.service
journalctl -u t3-simulation.service -f
```

---

## simulation.conf — T3 section

T3 reads its config from the `[t3]` section of `simulation.conf`:

```ini
[t3]
spoke_url=http://169.253.1.1:8000
hostname=client-sim-01
simulation_id=t3-lab
update_interval=300
```

| Key | Default | Description |
|-----|---------|-------------|
| `spoke_url` | `http://169.253.1.1:8000` | Spoke API base URL |
| `hostname` | system hostname | Hostname reported to spoke |
| `simulation_id` | `t3-lab` | Simulation bucket shown in hub/spoke UI |
| `update_interval` | `300` | Seconds between `update_script.sh` runs |

---

## Updating scripts

`update_script.sh` uses a three-tier fallback to pull the latest scripts:

1. **API** (spoke) — `GET <spoke_url>/api/scripts/t3/<file>`
2. **GitHub** — raw content from the configured branch
3. **SMB share** — fallback for air-gapped environments

Run manually:

```bash
bash /opt/t3-sim/update_script.sh
```

---

## Regenerating MAC addresses manually

```bash
sudo bash /opt/t3-sim/gen_macs.sh
```

This reads `/opt/t3-sim/mac_config.json`, live-swaps each `vwlan` interface MAC using `ip link`, and rewrites `/etc/udev/rules.d/70-t3-wifi.rules` so the assignment persists across reboots.
