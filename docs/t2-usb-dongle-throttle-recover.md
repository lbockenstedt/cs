# T2 USB-Dongle Fleet — Throttle-and-Recover Design

**Read this before touching any high-traffic client sim (DNS flood, iperf,
download, collab) or reasoning about why sim clients "go offline."** It explains
the hardware reality the client simulators run on, why they drop, why that is
partly *desirable*, and the throttle-and-recover pattern every heavy sim must
follow.

---

## The hardware: why T2 clients are flaky by design

Sim clients get their "real" wireless NIC from a **USB WiFi dongle**. On a
Proxmox sim host, that dongle hangs off a **USB PCI card (sometimes via a USB
hub) that is passed through to the guest VM**.

The critical consequence of PCI passthrough: **the guest does not own the USB PCI
device at the bus level, so it cannot reset the USB bus.** When too much traffic
contends the bus, the adapter wedges — the dongle stops passing packets and the
client's default gateway becomes unreachable. Because neither the guest (doesn't
own the PCI device) nor cleanly the host (it's assigned to the guest) can issue a
bus reset, **the only recovery is to remove the load and let the bus clear on its
own.**

Diagnostic signature (well-established through testing):

- Stop the simulation → you can **always** ping the gateway again.
- The "gateway offline" happens even at a trivial DNS rate (e.g. the 15/min
  floor — one query every 4 seconds). A gateway/AP does not die from that volume;
  the **dongle's USB bus** does. So the limit is *total traffic over the
  passthrough USB bus/hub*, **not** DNS query volume or RF.

## T1 vs T2 — the tiers, and why we lean on T2

| Tier | What it is | Reliability | Cost |
|---|---|---|---|
| **T1** | Physical machine, or a *dedicated* PCI card in a Proxmox host | High — a real, resettable adapter | Expensive **per port** |
| **T2** | A fleet of VMs with **passed-through USB dongles** that **self-heal** | Individually flaky (see above) | Cheap per port |

T1 is the "correct" hardware but doesn't scale economically — each reliable port
costs. **The T2 strategy is to reach the same aggregate with a large fleet of
cheap USB-dongle clients that individually drop and recover gracefully.** Graceful
drop handling — *throttle-and-recover* — is not a workaround; **it is the entire
value proposition that lets a T2 fleet substitute for expensive T1 hardware.**

## The ~20% rolling kill rate (a baked-in assumption)

At any given moment **roughly 20% of the dongle fleet is not working** — wedged
buses, mid-recovery, USB glitches, driver hiccups, reassociation, etc. This is
**not a fixed dead set**: it is a **dynamic equilibrium** — dropped dongles heal
back at about the same rate that fresh ones drop off, so the fleet holds a steady
**~80% availability with constant churn**.

Two implications:

1. **Fleet sizing:** overprovision. To net *N* working clients, deploy ~*N* × 1.25.
   Never assume 100% of the fleet is contributing at once.
2. **This is partly GOOD — it makes the simulation look real.** A real production
   network is never sterile: clients roam, reboot, drop, and reconnect
   constantly. A fleet that is perpetually self-healing produces exactly that
   texture — reconnects, transient failures, a moving population — which is what
   an authentic simulation environment should look like. The learner/quota engine
   is built to absorb this noise (feedback against the *observed* alert, not a
   precomputed rate) precisely because the environment is non-sterile by design.

## USB bus density — the 7-adapter limit (hard provisioning rule)

The ~20% kill rate is only achievable at the **right density**. Packing too many
adapters onto one USB bus makes contention — and therefore the kill rate — climb
**exponentially, not linearly**:

> **Do not put more than ~7 WiFi/Ethernet adapters on a single USB bus.** Past
> ~7-per-bus, the kill rate is *much* higher than 20% and the fleet stops
> self-healing (drops outpace recoveries).

The reference topology that stays under that limit:

- A **4-channel PCI USB card** — 4 independent USB buses on one card.
- A **USB hub on each channel**, with up to **7 adapters per hub** → **~28
  dongles per 4-channel card**, each bus at/under the 7-adapter limit.

The *same* 4-channel PCI card is also how **T1** clients are built: instead of
hanging dongles off it and passing the USB through, you **pass the entire PCI card
through to one guest with a single network adapter on it**. One adapter, no bus
contention, and the guest effectively owns a resettable device → reliable. The
tradeoff is cost: a whole 4-channel card dedicated to **one** T1 client is
expensive per port — which is exactly why the T2 (many-dongles-per-card, self-
healing) model exists to scale cheaply.

**Diagnostic tie-in:** if a host's fleet availability sits chronically *below* the
~80% floor, the first thing to check is **density** — a bus with more than ~7
adapters will run a permanently elevated kill rate no matter how well the
per-client throttle behaves.

## The pattern every heavy sim must follow: throttle-and-recover

Because the bus can't be reset, a heavy sim must (a) stay **under** the per-client
bus-contention threshold, and (b) when it does trip it, **back off and let the bus
clear** before resuming. The DNS circuit-breaker (`clients/linux/dns_fail.sh` /
`dns_latency.sh`, helpers in `clients/lib/common.sh`) is the reference
implementation:

**Throttle (find the sustainable rate):**
- Watch the client's *own* default gateway during the flood (a quick ping ~every
  2 s).
- On a miss, **confirm** with 5 pings — declare the gateway **OFFLINE only if all
  5 fail** (`dns_gw_confirmed_down`), so a single WiFi/busy-VM blip can't false-trip.
- Offline → **bail** (kill in-flight work, exit the burst) and **drop the
  operating rate 20%** (`rate × 0.8`, `dns_ceiling_penalize`), persisted to
  `/usr/local/scripts/dns_ceiling.state`. Re-trip → another 20% down; it converges
  to the dongle's sustainable rate in a few steps. Floored (`_DNS_RATE_FLOOR`, 15
  during testing) — below the floor the DNS rate can't be the cause anyway.

**Recover (let the bus clear before re-loading it):**
- **Recovery hold** at the start of every burst: don't (re)start the flood until
  the gateway is **STABLY up** — several pings in a row (`dns_gw_stable`), not just
  one. Resuming the instant the adapter blips back re-contends a bus that hasn't
  finished clearing, so it never recovers. Hold the flood OFF until it's solidly
  back, then resume at the (already-throttled) rate.

**Per-client, self-tuning:** every dongle finds its own ceiling — they differ, and
they drift, so the throttle is per-client and re-measured, never a fleet-wide
fixed number.

## How the alert still fires despite low per-client rates

If a dongle only sustains, say, 15–150 failures/min before its bus wedges, no
single client can hit Central's threshold (~200 failed lookups / 5-min window).
That is by design — it is a **two-dimensional** problem:

- **Dim 1 — per-client intensity:** the dongle's sustainable rate. The breaker
  measures it; in learning mode the client reports it up.
- **Dim 2 — fleet count:** how many clients (each at their safe rate) sum to the
  threshold: `clients_needed ≈ alert_threshold ÷ per_client_rate`, then padded for
  the ~20% that are down at any moment.

The quota engine solves Dim 2 (count) using the Dim-1 rates the clients report,
rather than trying to push any one flaky dongle harder. See
[`simulation-pool-and-quota-design.md`](simulation-pool-and-quota-design.md) and
`lm/docs/alert-generation.md`.

## Fleet-health monitoring (the 20% floor as an operational metric)

Because ~80% availability is *normal*, the fleet-health indicator must be judged
against that floor — not against 100%:

- **Metric:** `working ÷ total`, where `total` = sim VMs deployed and `working` =
  clients currently reporting into the API AND successfully simulating
  (online within the last-seen window, gateway reachable, sims active).
- **Bands (defaults, tunable):**
  - **≥ ~75% working → OK.** This is the expected steady state — ~20% churn is
    normal, not a fault. Do not warn here.
  - **~50–75% → WARNING** (amber / blinking on the dashboard): more of the fleet
    is down than the baked-in churn accounts for.
  - **< ~50% → CRITICAL + ALERT:** a systemic problem (host down, AP down, USB
    hub failure, a bad push, mass reclone) — not routine dongle churn.
- Example: fleet of 100 VMs → **80 reporting = normal**; **40 reporting = blinking
  warning + alert.**

The point of pinning the bands to the ~80% equilibrium is to avoid alert fatigue
(don't cry every time a dongle blips) while still catching a real collapse.

## Guardrails

- **Never "fix" the drops by pushing harder.** A wedged USB bus does not recover
  faster under more load — it recovers only under *less*. More load = it never
  clears.
- **Throttle on the client's own gateway, not a remote target.** The signal that
  matters is *this dongle's* bus health.
- **Confirm before the sticky throttle-down**, and **hold off until stably
  recovered** — the two halves are equally important. Throttling without the
  recovery hold just re-contends the bus; recovering without throttling walks
  straight back into the wall.
- **Don't chase the floor.** If a client is pinned at the floor and still
  dropping, the DNS rate is no longer the cause — it's the aggregate fleet load or
  a genuinely dead dongle (part of the 20%). Lower per-client rate won't help;
  that's a fleet-count / fewer-clients question.
