# simulation.conf — Configuration Guide

The `configs/` folder contains two plain-text INI files that control every aspect
of how client-sim runs. They remain easy to edit directly in GitHub, and the same
files are surfaced through the Hub Config UI and the spoke standalone Config UI.

```
configs/
  simulation.conf      ← simulation profiles and global settings
  user-overrides.conf  ← per-username profile overrides (optional, ships with examples)
```

---

## How it works — the big picture

Each client VM is named after a person (e.g. `jsmith`). On startup, the client
hashes its own hostname to deterministically assign itself to one of 10 simulation
buckets — no VMID, no database, no API call.

```
Hostname:  jsmith
    │
    └─ zlib.crc32("jsmith") % 10  →  bucket index 0–9
                                   →  simulation bucket = s4
                                   →  client runs the [s4] profile from simulation.conf
```

The hash distributes names evenly across all 10 buckets. The first 10 names in
the fleet, for example, land in 8 different buckets — far better than the old
VMID-digit approach, which put all VMs on a single host into only 2–3 buckets.

To override the bucket for a specific user, add `simulation_id=sX` to their
section in `user-overrides.conf`. This takes precedence over the hash.

---

## simulation.conf

`simulation.conf` can be edited directly in GitHub, through the Hub **Config** view,
or from a standalone spoke in **Config** or **Setup → Simulation**. In the UI, all
section types now use the same collapsible card layout, and the `s0`–`s9` slots
always show the full standard key set.

### [simulation] — Global settings

These apply to every client unless overridden in a bucket or user section.

```ini
[simulation]
kill_switch=off          # on = stop all simulations immediately (emergency stop)
rapid_update=off         # on  = run update.sh every iteration (dev/testing mode — frequent checks,
                         #       version check keeps it lightweight when nothing has changed).
                         # off = run update.sh only at exec-restart (every 100 iterations) — prevents
                         #       hammering update services in production deployments.
sim_load=100             # CPU throttle target for cpulimit (percentage)
github_repo=on           # on = repo cloned without auth
repo_location=https://github.com/solutions-hpe/client-sim/
server_url=http://169.253.1.1:8000   # webUI heartbeat endpoint (set in [server] section)
repo_branch=main          # which branch clients pull from
smb_repo=off             # on = enable Tier 2 SMB share as an update source (see [address] smb_address)
                         # Update priority: WebUI → SMB → GitHub (each tier only tried if previous fails)
vh_server=off            # on = start VirtualHere USB server daemon
site_based_ssid=on       # on  = prepend wsite to the SSID name when connecting.
                         #       e.g. wsite=MIA + ssid=PSK → connects to "MIA-PSK"
                         # off = connect to ssid exactly as written (e.g. "PSK")
reboot_schedule=300      # minutes until client schedules a reboot (+ up to 600s jitter)
allow_offline=on         # on = after each 100-iteration cycle, bring all network interfaces
                         #      down for a random 1 second to 4 hours before restarting.
                         #      WHY: clients that are always connected look like IoT devices.
                         #      Going offline periodically makes them look like real user laptops
                         #      that leave the office, sleep, or roam off the network.
                         # off = client stays connected continuously between cycles.
ssidpw_fail=off          # global default — can be overridden per bucket or user
auth_fail=off
iperf_bw=1k              # iPerf bandwidth target
syslog=on                # on = forward all client logs to syslog_server via rsyslog.
                         #      The syslog_server address is set in the [address] section.
web_server=on            # on = sync scripts/config from WebUI server (preferred over GitHub)
```

### [server] — Alternate server block

Used internally when the client resolves the dashboard by hostname instead of IP.

```ini
[server]
server_url=http://sim-dashboard:8000
```

### [address] — Network targets

Addresses used by simulation scripts for DNS, ping, SMB, and iPerf tests.
Change these to match your lab network.

```ini
[address]
smb_address=//nas/scripts
ping_address=172.31.201.3
dns_latency_1=13.239.88.95    # External DNS servers used to generate latency
dns_latency_2=27.110.152.250
dns_latency_3=165.246.10.2
dns_bad_ip_1=10.0.0.1         # IPs that return bad DNS responses
dns_bad_ip_2=172.16.0.1
dns_bad_ip_3=192.168.0.1
dns_bad_record_1=172.31.201.1 # DNS records that resolve to wrong addresses
dns_bad_record_2=172.31.202.2
dns_bad_record_3=100.100.0.1
iperf_server=172.31.201.135
syslog_server=169.253.1.5
```

### [s0]–[s9] — Simulation bucket profiles

There are exactly 10 buckets (`s0` through `s9`). Each bucket defines the full
simulation behaviour for the clients whose hostname hash maps to it.

```ini
[s0]
name=DNS Fail — MIA
# central_check: the Aruba Central alert_type or insight category ID that should
# be firing when this simulation runs. Used by the dashboard for PASS/FAIL status.
# Leave blank if you are not mapping this simulation to a Central alert.
# Example: central_check=DNS_FAILURE
central_check=
wsite=MIA               # site label — must match a site_mappings entry in the webUI
ssid=PSK                # SSID to connect to
ssidpw=PassW0rd!        # WPA passphrase
dhcp_fail=off           # simulate DHCP failure
dns_fail=on             # simulate DNS failure (generates DNS alert in Central)
assoc_fail=off          # simulate 802.11 association failure
port_flap=off           # simulate wired port link flap
ping_test=on            # run continuous ICMP ping test
download=on             # run HTTP download traffic
www_traffic=on          # run web browsing simulation traffic
iperf=off               # run iPerf throughput test
sim_phy=wireless        # wireless or wired
```

**Simulation flags reference:**

| Flag          | When `on`                                          | Central alert generated      |
|---------------|----------------------------------------------------|------------------------------|
| `dns_fail`    | Resolves DNS to bad IPs / bad records              | DNS failure insight/alert    |
| `dhcp_fail`   | Releases and does not renew DHCP lease             | DHCP failure alert           |
| `ssidpw_fail` | Connects with wrong WPA passphrase                 | Auth failure alert           |
| `auth_fail`   | Sends bad 802.1X credentials                      | Auth failure alert           |
| `assoc_fail`  | Sends malformed association requests               | Assoc failure alert          |
| `port_flap`   | Bounces the wired interface repeatedly             | Port flap alert              |
| `ping_test`   | Sends ICMP to `ping_address` (traffic generation)  | —                            |
| `download`    | Downloads a file repeatedly (traffic generation)   | —                            |
| `www_traffic` | Fetches web pages (traffic generation)             | —                            |
| `iperf`       | Runs iPerf to `iperf_server` (bandwidth test)      | —                            |

#### How many clients does a simulation need to fire an alert?

Each alert or insight in Aruba Central has a minimum number of clients that must
be exhibiting the behaviour before the alert fires. The bucket system handles
this automatically — assign enough buckets so the total client count meets or
exceeds the threshold.

**Example:** DNS failure requires 10 clients → assign 1 bucket. If it required
25 clients, assign 3 buckets (s0, s1, s2) all with `dns_fail=on`.
Over-provisioning is fine — more clients = stronger signal.

#### Linking a simulation to a Central alert (PASS/FAIL)

Set `central_check` to the exact alert type or insight category string from
Aruba Central. The webUI Simulations tab will show **PASS** when that alert is
actively firing in Central, and **FAIL** when it is not.

```ini
[s0]
name=DNS Fail — MIA
central_check=DNS_FAILURE   ← exact string from Central API
wsite=MIA
dns_fail=on
…
```

To find the correct string: go to the webUI **Setup → Monitored Checks → Load
Available Checks**. The check IDs listed there are the strings to use here.

---

## user-overrides.conf

This file stores one INI section per username. It is loaded **after**
`simulation.conf`, so any key defined here wins over the bucket profile.

It can be edited directly in GitHub, from Hub **Config → User Overrides**, or
from a standalone spoke in **Config → User Overrides**.

```ini
# Pin slynch to run ssidpw_fail instead of his bucket profile
[slynch]
dns_fail=off
ssidpw_fail=on
ping_test=off
download=off
www_traffic=off
```

You do not need to repeat every key — only specify the keys you want to override.
Keys not listed here fall through to the bucket (`[sX]`) value or the global
`[simulation]` default. Common uses are pinning `simulation_id=sX`, changing
`wsite`/`ssid`, or toggling individual simulation flags for one user.

### When to use user overrides

- **Targeted failure testing** — pin one or two specific users to run a specific
  failure scenario for reproducibility.
- **Exclusion** — set `kill_switch=on` for a single user to stop their simulation
  without affecting others.
- **Alternate site** — change `wsite` for a user to point them at a different
  Aruba Central site than their bucket.

### Config resolution order (last wins)

```
[simulation] globals
      ↓
[sX] bucket profile  (s0–s9, determined by zlib.crc32(hostname) % 10)
      ↓
[username] override  (from user-overrides.conf — can also pin simulation_id)
```

---

## Pinning a user to a specific bucket

Add `simulation_id=sX` to the user's section in `user-overrides.conf`:

```ini
[jsmith]
simulation_id=s7   # force jsmith into bucket s7 regardless of name hash
```

This is useful when you need a specific user to always run a particular simulation,
or when you want to balance client counts across buckets precisely.

---

## Contributing a new simulation

1. Fork the repo and create your branch.
2. Edit `configs/simulation.conf` — modify an existing `[sX]` profile or add
   settings to an unused bucket.
3. If you need a user-specific override, add it to `configs/user-overrides.conf`.
4. Test on your own hardware.
5. Submit a pull request. The PR diff will show exactly which simulation flags
   changed — easy to review with no code changes required.

> **Tip:** INI format is intentional — it renders clearly in GitHub, diffs are
> readable, and anyone can edit with any text editor. Keep it that way.
> Avoid JSON, YAML, or XML in any config file.
