# Simulation Pool & Quota Engine — Design Spec

Status: **implemented** (the model below is live in `lm-spoke` SimQuotaEngine +
`clients/*` scripts + hub config/UI; see §15 for the running-state tracker).
Scope: the client-sim fleet. This is the authoritative model; it supersedes
ad-hoc bucket behavior where the two conflict.

> **Model update (2026-07):** SSID placement is no longer "hold N per cell." The
> **SSID matrix is definitions only** (site / SSID / password). Clients reach a
> cell two ways: an **accounted cell harvest quota** (exact/adaptive count,
> §5/§8) or a **weighted random rule** (the stateless ~99%, §5). Sharing and
> exclusivity are keyed **per SSID cell**, not per site (§8). See §15 for the
> full list of what changed.

---

## 0. Multi-spoke tenants (tenant-wide coordination)

A tenant may bind **several Client-Sim spokes** (e.g. `cs-svr-02/03/04`), each an
independent spoke with its own client registry + SimQuotaEngine + pxmx agent.
The pool/quota model is **tenant-wide across all of them**:

- **Config writes** fan out to every spoke (`get_client_sim_spokes`).
- **Reads aggregate** across spokes: `_cs_forward_all` merges the pxmx-site-map +
  agents (so all servers show/are assignable) and sums the Quota-State ledgers
  into tenant totals.
- **Target splitting:** on push, each quota/placement target `N` is apportioned
  across the spokes **proportional to each spoke's pool size** (largest-remainder,
  sum == `N`). So "hold 10" is 10 tenant-wide, not 10 per spoke; each spoke fills
  its share from its own clients and Quota State sums the ledgers back to `N`.

**Site Links** (Config → PXMX Sites): tie a sim `wsite` (SSID prefix, `MIA`) to a
Central site name (`Miami`) with a friendly Name. The Name shows in the pxmx
assignment dropdown (value = `wsite`), and `_alert_firing` maps a quota's `wsite`
to its Central site so alert-driven quotas match where the alert fires.

---

## 1. Purpose

Run a large client-sim fleet (~5,000 boxes) where:

- **Most clients just generate realistic, randomized background behavior** — no
  central bookkeeping, no per-client config.
- **On demand, a small number are "harvested"** to perform a specific task
  (fire a specific alert/insight at a specific site), with exact/adaptive
  control.
- The operator **declares intent, not placement** — you say "hold 20 on this
  SSID," "keep `dns_fail` firing at MIA," and the engine figures out *which*
  boxes and self-heals as boxes come and go.

Design rule of thumb: **anything you constrain is a tracked quota; everything
else is a stateless default (a dice roll).** State only ever grows to the
numbers you actually asked for — never to the fleet size.

---

## 2. Deployment topologies — per-PXMX-server binding

The site model is set **per PXMX server** (`pxmx_site_map`, Config → PXMX Sites),
not tenant-wide — so a single deployment can **mix** both. Every server is
assigned to exactly one of:

| | **Site Pool** (RF chamber) | **Tenant-Wide Pool** (site-based SSID) |
|---|---|---|
| `pxmx_site_map[server]` | a real site name (`MIA`) | `"Tenant-Wide Pool"` (or unmapped) |
| its clients' `wsite` | **physical** — the server's site, frozen | **assignable** — placement gives them a site/SSID |
| pool size | hardware-bounded (servers × clients) | the tenant's assignable clients |
| isolation | physical / RF | SSID name |
| `site_based_ssid` | off — plain `PSK`/`1X` on the chamber AP | on — `{wsite}-{ssid}` → `MIA-PSK` |

Pools are **always per-tenant** — each tenant has its own spoke/registry; a pool
or simulation client is never shared across tenants.

**`wsite` is the universal pool key.** Everything downstream (placement, harvest,
adaptive control, ambient random) operates *per-`wsite`* identically. A
Site-Pool client is pinned to its server's site; a Tenant-Wide-Pool client is a
candidate for any site's cells, and placement/harvest set its `wsite` when they
assign it. `_physical_site_of` / `_is_tenant_pool_client` (engine) decide which.

---

## 3. The model at a glance — the resolution stack

A client's full identity resolves top-down, each layer either a **tracked
quota** or a **stateless default**:

```
site   →  physical PXMX pool (chamber)  |  weighted assignment (site-based SSID)
SSID   →  cell harvest quota (accounted) |  weighted random rule (unaccounted)
sims   →  harvest quota (task)          |  random from the randomizable set
```

- **site**: which pool the box belongs to (§4).
- **SSID**: which network inside that site (§5).
- **sims**: what it runs — a deliberate task (§8–9) or ambient random (§6–7).

Precedence when layers set the same key: **human override > engine quota >
random/default** (§10). A human `[username]` pin is always honored.

---

## 4. Site pools

- A site's pool = **every client whose `wsite` is that site**.
- **RF chamber mode:** `wsite` is derived from the client's **hosting PXMX
  server** (`pxmx_site_map` + `_name_to_host`), injected into the served config.
  The pool is **hardware-bounded** and known. You cannot harvest more than the
  chamber physically holds.
- **Site-based-SSID mode:** `wsite` is **assigned** by a stateless weighted roll
  across the configured sites (stable per hostname → no jumping), and
  `site_based_ssid=on` so the client joins `{wsite}-{ssid}`.
- Everything else in this doc is **scoped to a single site pool** and is mode-agnostic.

---

## 5. SSID cells — definitions, cell quotas, and weighted rules

The **SSID matrix** (Pool & SSID card) is **definitions only**: each cell is
`{ name = "<site>-<ssid>", site, ssid (auth: PSK/1X/…), password, enabled }`.
It carries **no** counts — no Hold N, no per-cell weight. A cell name like
`MIA-PSK` encodes its physical site (`MIA`) and its auth (`PSK`).

Clients land on a cell in exactly **two ways**, configured in one place
(Config → Sim Quotas):

1. **Accounted cell harvest quota** (the ~1%). A sim/presence quota whose **site
   is a cell** (`MIA-PSK`) homes an **exact/adaptive** count of clients to that
   cell — it scopes to the cell's physical site AND pins the client's
   `ssid`/`ssidpw` to the cell. `MIA-PSK` and `MIA-ACD` are **distinct quotas
   that coexist** at the same site. Tracked in the ledger (§8).

2. **Weighted random rules** (the ~99%, stateless — no accounting). A rule is
   `{ site, ssid (cell), weight, all }`. After the harvest quotas claim their
   clients, the engine (`_reconcile_weighted`) spreads every **spare** client at
   a site across that site's cells **proportional to weight**:
   - `weight 5` vs `weight 1` → 5× the clients;
   - `weight 0` → that cell takes none;
   - **`all`** → that cell soaks the **balance** after the weighted split.

   No ledger, no per-client tracking — each sweep just re-sets the spare client's
   `ssid`/`wsite`.

Both write `wsite` / `ssid` / `ssidpw` into the served `[username]` (frozen
connectivity — see §12), so bucket randomization can never move a box's SSID.

> **Deprecated:** the old per-cell **Hold N** + **remainder** placement model
> (`_reconcile_placement`, `ssid_placement`) is gone. Hold N → a cell harvest
> quota; the remainder/weight → weighted rules. Legacy `placement:*` ledger
> entries are dropped on reconcile.

---

## 6. Sim universe: randomizable vs harvest-only

Split the sim primitives into two classes — **defined in config**:

- **Randomizable (ambient) sims** — benign traffic: `ping_test`, `download`,
  `www_traffic`, `iperf`. The ambient pool rolls random combos of *these only*.
- **Harvest-only sims** — alert generators: `dns_fail`, `assoc_fail`,
  `dhcp_fail`, `port_flap`, `ssidpw_fail`, `auth_fail`. These **never** run
  randomly; they turn on only when the engine harvests a box for a task.

Rationale: if ambient boxes randomly ran a failure sim they'd throw **spurious
alerts** and corrupt the adaptive harvest's firing signal. Keeping failures
harvest-only means ambient noise is pure traffic and **every alert that fires is
one the engine asked for.**

Add a per-sim **`randomizable`** flag (the ambient set). Buckets (`s0`–`s9`)
become **curated random combos drawn from the randomizable set** — pure
behavior, no connectivity.

---

## 7. The random (ambient) pool — fully stateless

Everything not harvested and not on a targeted SSID:

- **Site** is pinned (from §4) and **frozen** — no jumping.
- **Sims** are a **live client-side roll** each iteration, from the randomizable
  set (via the curated buckets). Cadence = the client's natural config-reload on
  sim completion (approximate/organic, deliberately not exact).
- **No tracking. No per-client config. No engine ledger entry.** The ~4,900
  ambient boxes are config-only (site source + randomizable set), applied
  statelessly.

Buckets rotate **sim flags only**; `wsite`/`ssid`/`ssidpw`/`sim_phy` come from
the stable site/SSID layer and win via `[username]` (§12).

---

## 8. Sim harvest quotas

Pull a specific number of boxes from a site's pool to run a specific sim
(usually to fire the linked alert/insight).

- **Fixed:** `count = N` (or `min == max`). Runs exactly N.
- **Adaptive:** `min = X`, `max = Y`, `step`, `settle`, `buffer` — see §9.
- **Cell- or site-scoped:** a quota's *site* field is either a whole site (`MIA`)
  or an **SSID cell** (`MIA-PSK`, §5). A cell quota scopes to the cell's physical
  site AND sets the assigned client's SSID.
- Harvest draws from the site pool, **capped by pool size** (chamber ceiling).
- Requires the quota be **tied to an alert/insight** to be adaptive. Untethered
  sim quotas are fixed-count and keyed `sim:{sim_id}:{site}`.

**Sharing & exclusivity — keyed per SSID cell** (`_claimed_cell` / `_cell_ok_for`):

- Two quotas on the **same cell share** clients — a sim quota stacks onto that
  cell's "Clients Associated" (presence) clients via `homed_here`. e.g. `dns_fail`
  on `MIA-PSK` runs on the same boxes as `MIA-PSK`'s presence.
- Two quotas on **different cells never share** a client — `MIA-PSK` and
  `MIA-ACD` are disjoint (a client has one SSID).

**Shareable vs exclusive sims** (`SIM_META.multi_capable` / the Simulation
Sharing tile, `_sim_multi`):

- A **shareable** sim (traffic, or a failure explicitly marked shareable like
  `dns_fail`) may stack onto presence / traffic / other shareables — but **never
  onto a client an EXCLUSIVE sim monopolizes**.
- An **exclusive** sim (`ssidpw_fail`, `dhcp_fail`, …) **monopolizes** its client:
  it only takes a client running **no other sim** (a presence-homed client
  qualifies), and nothing else stacks onto it. Exclusive quotas are **processed
  first** so they claim bare clients before shareables spread onto the rest; a
  shareable quota **yields** a client the moment an exclusive claims it.

Counting is **ledger-based** (boxes the engine assigned), never raw telemetry.
**Ignored hosts** (`hub_config.ignored_hostnames`) are filtered out of the pool,
counts, eligibility, and ledger entirely — a box being spun up/decommissioned is
never assigned or shown.

---

## 9. Adaptive harvest controller

Runs **hub-side** (the hub knows alert-firing status from the Central poller);
it only modulates the `count` pushed to the spoke engine. The engine is
unchanged — it fills to whatever count it's told.

Per `(alert, site-or-cell)`, respecting a **settle window** between changes:

```
if alert NOT firing:   target = min(target + step, max)     # ramp up
if alert firing:       decay slowly toward the floor         # probe the minimum
```

> **30-min settle floor (Central latency).** Central reports alerts with 30+ min
> latency, so the controller must **never change the target — up OR down — faster
> than that**, or it ramps to `max` long before Central can confirm firing and
> then falsely flags "at max, not firing." `settle` is floored at **1800s**
> regardless of the configured value, and the settle clock starts at cold-start
> so even the first step waits a full window.

> **Cell → site firing resolution.** `_alert_firing` resolves the quota's site
> to the Central site in two hops: if the site names an **SSID cell** (`MIA-ACD`)
> → its physical `wsite` (`MIA`) via `ssid_matrix`, then → the Central site
> (`Miami`) via `site_links`. Without hop 1 a cell-scoped adaptive quota never
> matched a firing site and ramped to max forever.

> **Anti-affinity (multi-spoke).** An **alert-tied** quota's target is split
> **evenly (round-robin)** across the tenant's spokes — not pool-proportional —
> so losing one server still leaves the alert firing from the others (§0).

- **Learned floor:** when it fires at N and clears just below, the minimum that
  holds the alert is recorded as a smoothed value (EMA / high-water) per
  `(alert, site)`, and **persisted**.
- **Operating point = `clamp(ceil(floor × 1.20), min, max)`** — hold **20% above
  the floor** (configurable) as recovery headroom, so a dying runner doesn't
  drop the alert during the substitute cycle. If `floor × 1.2 > max` (or `>` pool
  size), clamp and warn.
- **Warm-start** future ramps from the learned value, not from `min` — faster to
  fire, far less churn.
- **Anti-flap:** ramp up fast, decay slow, hysteresis band around the learned
  floor; re-probe occasionally, not every tick.

**Learning indicator** (per quota, surfaced in Quota State):

| State | Meaning |
|---|---|
| 🔄 **Learning** | hasn't converged — ramping to first fire, or probing, low confidence |
| ✅ **Stable** | converged — holding `floor + buffer` |
| ⚠️ **At max** | pinned at `Y`, still not firing (warning) |

Example rows:

```
dns_fail @ MIA    ✅ Stable   · floor 18 · running 22 (+20%)
assoc_fail @ DFW  🔄 Learning · probing (running 14)
port_flap @ MIA   ⚠️ At max   · 200, not firing
```

The learned floor ("MIA needs ~18 to hold `dns_fail`") is operator-visible
intelligence nothing else in the stack surfaces today.

---

## 10. Precedence & the human-override invariant

Highest wins. **A human `[username]` override in `user-overrides.conf` is always
honored** — above the random pool *and* the engine.

1. **Human `user-overrides.conf [username]`** — per-flag, always. `kbell →
   dns_fail=on` means on, full stop. The box still randomizes its *other* sims
   (per-flag lock, stays in the pool).
2. **Engine quota** (placement or harvest) — via `[username]`, skips any key the
   human already set. Full task takeover for harvested boxes (leaves the pool).
3. **Random pool** — the `s0`–`s9` behavior roll.
4. **Home bucket / global defaults** — baseline.

Enforcement points:

- **Serve time (`/api/config`)** — engine injections into `[username]` skip
  human-set keys. *(Shipped — see §15.)*
- **Client (`apply_override`)** — `[username]` applied last, wins over the
  rotated bucket. *(Inherent.)*
- **Engine eligibility** — must also read the human `[username]` overrides and
  treat a human-pinned flag as a manual pin, so it never harvests/counts against
  a flag the human controls. *(To build.)*

---

## 11. State model & scale

| Thing | Tracked? | Size |
|---|---|---|
| Ambient pool (site-pinned, random sims) | **No** | 0 |
| Weighted site assignment (site-based-SSID mode) | **No** (deterministic) | 0 |
| SSID placement quotas | Yes — targeted counts only | Σ targets per site |
| Sim harvest quotas | Yes — ledger of assigned boxes | tens–hundreds |
| Learned floors, operating targets | Yes — per `(alert, site)` | tens |
| Human pins | Yes — authored | a few |

**Nothing scales with the fleet.** Tracked state grows only to the numbers you
constrain; the ~4,900 ambient boxes cost nothing.

---

## 12. Delivery to the client

Clients are **pull-based**: `update.sh` fetches `GET /api/config?hostname=<host>`
into local `simulation.conf`, and resolves each sim flag as
`get_value $simulation_id` (bucket) then `apply_override` = `get_value $username`
(wins).

The spoke (`client_api.py:api_config`) renders per request:

1. base buckets + global sections,
2. human `user-overrides.conf [username]`,
3. **engine/registry overrides injected into `[username]`** (skipping human
   keys) — placement (`wsite`/`ssid`/`ssidpw`) and harvest (sim flag) — plus
   the site source (`wsite` from `pxmx_site_map` in chamber mode),
4. `random_pool` policy + randomizable-set signal for the ambient roll.

Because `[username]` wins in `apply_override`, injected placement/harvest values
**override whatever the random bucket rolled** — connectivity stays frozen,
tasks stay pinned. This is **delivery-only**: nothing is written to the on-disk
`user-overrides.conf`, so engine assignments are transient (no GitHub sync) and
self-clean on release.

Client `username = $HOSTNAME | cut -d- -f1` matches server `username_for`.

> **GOTCHA — the `web_server` gate.** A transient engine assignment reaches a
> client **only** if `update.sh` re-fetches `/api/config`, and that fetch lives
> **inside `if [[ "$web_server" == "on" ]]`**. If `web_server` is off (or the
> client is on the GitHub update tier), the `[username]` injection never lands on
> disk and the box **won't run its assigned sim even though the ledger says it
> is**. First thing to check when "engine says running, client isn't": compare
> what the spoke *serves* (`curl /api/config?hostname=$H`) vs the on-disk
> `simulation.conf`.

> **Harvest window.** A client is "in the pool" (`_is_harvestable`) if it beaconed
> within **`HARVEST_WINDOW_S = 1800s` (30 min)** — real-ish clients flap in/out, so
> a tight "online now" window would collapse the pool to a handful.

---

## 13. Config surfaces (all fleet-size-independent)

- **SSID matrix** — the cells that exist: `{ site, ssid/auth (PSK/1X/…),
  password, enabled }`. **Definitions only** — no counts.
- **Site source / mode** — PXMX topology vs weighted-assignment + site-based SSID.
- **Weighted random rules** (`ssid_weights`) — `{ site, ssid(cell), weight, all }`
  for the spare pool (replaces the old Hold-N/remainder placement).
- **Randomizable sim set** — the `randomizable` flag per sim.
- **Simulation Sharing** — per-sim shareable vs exclusive (`sim_shareable`).
- **Harvest quotas** — per site/cell/alert: `min` / `max` / `step` / `settle` /
  `buffer` (or a fixed `count`); `multi_capable`, `rehome`; target = site **or**
  SSID cell.
- **Ignored hostnames** (`hub_config.ignored_hostnames`) — excluded everywhere.

---

## 14. Warnings & edge cases

- **Infeasible placement mins:** floors summing above the online pool → fill by
  priority, surface `"MIA-1X under min (60/75) — not enough online clients."`
- **Floor + buffer > pool:** adaptive operating point exceeds the site's
  physical capacity → clamp + warn (physically impossible).
- **At max, not firing:** pin at `Y`, raise `quota_at_max_not_firing`.
- **Soft ceilings:** SSID/site placement ceilings are **soft** — going a little
  over is fine; weights prevent gross overshoot (you won't get 500 where you
  asked 75, because 75 was the weight). Hard floors/adaptive targets are the only
  strictly enforced numbers.

All warnings surface as **status/badges in Quota State** now, with a clean hook
(`quota_at_max_not_firing`, `placement_under_min`) to emit into the **internal
alert feature when it ships** (planned, not built).

---

## 15. Build status

**Core (shipped):**

- **Config plumbing** — spoke `local_store` get/set + `cs_spoke._apply_hub_config`
  for `site_source`, `randomizable_sims`, `random_pool`, `ssid_matrix`,
  `ssid_weights`; hub `_pool_config` flattens them into CS_CONFIG_UPDATE.
- **Serve-time injection** (`client_api.py`) — `wsite`, `[username]` layer with
  human-key preservation, random-pool + randomizable-set globals (§10, §12).
- **Client ambient rotation** — randomizable sim flags only, failures off,
  connectivity frozen, `[username]` pins win.
- **Adaptive controller** (hub, 45s loop) — ramp/decay/learn + floor×(1+buffer);
  `_alert_firing` mode-aware + cached; learning indicator + Adaptive Controllers
  panel in Quota State.
- **Human-override invariant** — `_pool_eligible` skips human-pinned sims;
  `effective_client_fields` lets reported toggles win in the Clients view.

**Unified placement model (2026-07, shipped):**

- **SSID matrix = definitions only** — Weight/Hold N removed (WebUI + `_reconcile_
  placement` deleted). `ssid_placement` deprecated; legacy `placement:*` ledger
  entries dropped on reconcile.
- **Cell harvest quotas** — a quota's site may be an SSID cell (`MIA-PSK`);
  `_quota_cell` → `scope_site` (physical) for eligibility, `_assign(cell=)` pins
  the SSID. Quota target dropdown shows combined `site/ssid` (`MIA/PSK`) items.
- **Weighted random rules** (`_reconcile_weighted`, runs after harvest) — spread
  the spare pool by `{site, ssid, weight, all}`; stateless, no accounting.
- **Per-cell sharing + exclusivity** (`_claimed_cell` / `_cell_ok_for`) — same
  cell shares clients, different cells don't (§8).
- **Shareable vs exclusive sims** — exclusive sims monopolize their client and
  run first; a shareable never stacks onto an exclusive, and yields when one
  claims the client (§8).
- **Anti-affinity** — alert-tied quota targets split **evenly** across spokes; other
  quotas split pool-proportional (§0/§9).

**Adaptive & operational (shipped):**

- **30-min settle floor** — `settle` floored at 1800s (Central alert latency), clock
  starts at cold-start (§9).
- **Cell → site firing resolution** in `_alert_firing` (§9).
- **Harvest window** — `HARVEST_WINDOW_S = 1800s` (§12).
- **Ignored hostnames** — filtered from pool/ledger/counts/eligibility (§8).
- **Pool count** — cheap `pool_counts()` (online / assignable / per-site) in Quota
  State, no accounting.
- **Reset & Reshuffle** — `engine.reset()` clears ledger + engine overrides and
  reconciles fresh; `CS_RESET_SIM_QUOTA` → hub `POST /sim-quota-reset` → button.
- **UI display key fix** — Quota State `keyOf` handles the untethered
  `sim:{sim}:{site}` case (was showing 0/N + phantom "RELEASING").

**Client-script sims (shipped, `clients/linux/*`, VERSION-gated):**

- **`dns_fail`** — fire-and-forget rapid DNS failures (readable rewrite).
- **`dhcp_fail`** — MAC spoof to `00:01:00:00:<real last 2 octets>` while on,
  restore real (permanent) MAC when off.
- **`ssidpw_fail`** — wrong password = **effective** ssidpw (`[username]`/cell
  wins over bucket) + suffix; restore reconnects with the correct cell password.
- **802.1X** — `connect_1x()` (PEAP/MSCHAPv2, identity = short username, no cert
  validation); `connect_wifi()` dispatches to it when the cell auth is `1X`;
  `ssidpw_fail` flows through it automatically.
- Dead `mac_id` (unused crc32 MAC) removed.

**Remaining / future:**

- `_alert_firing` — validate end-to-end against the live poller (centralized vs
  distributed) once exercised at scale.
- `quota_at_max_not_firing` / `placement_under_min` — surface as UI badges today;
  wire into the internal **alert feature** when it ships (§14).
- Weighted-rule **learning indicator** parity for non-adaptive cells (nice-to-have).

---

*This doc is the authoritative model. Keep §15 current as changes land.*
