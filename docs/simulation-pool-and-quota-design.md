# Simulation Pool & Quota Engine — Design Spec

Status: **design / build reference** (not yet implemented except where noted).
Scope: the client-sim fleet (`lm-spoke` SimQuotaEngine + `clients/*` scripts +
hub config/UI). This captures the target model agreed in design; it supersedes
ad-hoc bucket behavior where the two conflict.

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
SSID   →  placement quota (hold N)      |  balance / random default
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

## 5. SSID placement quotas (within a site)

Inside a site's pool you can run **many SSIDs** and pin how many clients sit on
each. This is the *same quota machinery as harvest, applied to placement.*

```
MIA-PSK:   hold 20
MIA-1X:    hold 50
remainder: → balance to MIA-1X        (or: → random spread across SSIDs)
```

- The engine **harvests from the site pool onto each SSID** to hit its target.
- **Self-healing rebalance:** if `MIA-PSK` drops 20 → 10, the engine picks 10
  boxes currently on another SSID (sourced from **telemetry** — clients report
  their SSID) and reassigns them to `MIA-PSK`. Otherwise **sticky** — a box only
  moves when the engine is correcting a target, never per iteration.
- **Remainder policy** (operator choice per site):
  - `balance → <SSID>`: everything not held elsewhere lands on one designated SSID.
  - `random`: the remainder is spread across the site's SSIDs by a stateless,
    stable weighted roll.

**State:** only the **targeted placements** are tracked (20 + 50 = 70 in MIA),
never the balance. Backfill is telemetry-sourced, so no prior per-client
tracking of the remainder is needed.

Placement writes `wsite` / `ssid` / `ssidpw` into the served `[username]`
(frozen connectivity — see §12), so bucket randomization can never move a box's
SSID.

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

- **Fixed:** `count = N` (or `min == max`). Runs exactly N. *(Exists today.)*
- **Adaptive:** `min = X`, `max = Y`, `step`, `settle`, `buffer` — see §9.
- Harvest draws from the site pool, **capped by pool size** (chamber ceiling).
- A harvested box leaves the ambient pool (runs *only* its task, deterministic)
  and rejoins on release.
- Requires the quota be **tied to an alert/insight** to be adaptive (that's the
  feedback signal). Untethered quotas are fixed-count only.

Counting is **ledger-based** (the boxes the engine assigned), never raw
telemetry — so ambient traffic running the same *class* of sim can't confuse the
count.

---

## 9. Adaptive harvest controller

Runs **hub-side** (the hub knows alert-firing status from the Central poller);
it only modulates the `count` pushed to the spoke engine. The engine is
unchanged — it fills to whatever count it's told.

Per `(alert, site)`, respecting a **settle window** between changes (matched to
the alert's detection latency, or you overshoot straight to `max`):

```
if alert NOT firing:   target = min(target + step, max)     # ramp up (fast)
if alert firing:       decay slowly toward the floor         # probe the minimum
```

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
self-clean on release. *(The `[username]` injection is shipped — §15.)*

Client `username = $HOSTNAME | cut -d- -f1` matches server `username_for`.

---

## 13. Config surfaces (all fleet-size-independent)

- **SSID matrix** — the cells that exist: `{ site, auth (PSK/1X/…), password,
  enabled }`.
- **Site source / mode** — PXMX topology vs weighted-assignment + site-based SSID.
- **Per-site SSID placement** — `hold N` per SSID + remainder policy (`balance →
  SSID` | `random`).
- **Randomizable sim set** — the `randomizable` flag per sim.
- **Harvest quotas** — per site/alert: `min` / `max` / `step` / `settle` /
  `buffer` (or a fixed `count`).
- **Weights** (site-based-SSID mode) — target proportions per site.

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

**Shipped (slices 1–6, this codebase):**

- **Config plumbing** — spoke `local_store` get/set + `cs_spoke._apply_hub_config`
  for `site_source`, `randomizable_sims`, `random_pool`, `ssid_matrix`,
  `ssid_placement`; hub `_pool_config` flattens them into the CS_CONFIG_UPDATE push.
- **Serve-time injection** (`client_api.py`) — `wsite` from `pxmx_site_map` in
  chamber mode; `random_pool` + `randomizable_sims` delivered as `[simulation]`
  globals; `[username]`-layer delivery with human-key preservation (§10, §12).
- **Client ambient rotation** (`simulation.sh` + `simulation.ps1`) — random
  bucket for randomizable sim flags only, failures forced off, connectivity
  frozen, `[username]` pins win.
- **SSID placement quotas** (`_reconcile_placement`) — per-site `hold N`, sticky,
  telemetry-sourced rebalance, remainder policy.
- **Adaptive controller** (hub) — `normalize_quota` carries min/max/step/settle/
  buffer; `_adaptive_step` ramp/decay/learn + floor×(1+buffer); `_alert_firing`
  (mode-aware, cached, holds when unknown); 45s loop from `main.py`.
- **Reconcile** processes presence first; sim quotas count ledger-homed clients
  as in-site; untethered quotas keyed `sim:{sim_id}:{site}`.
- **WebUI** — adaptive Min/Max on quota rows; Pool & SSID config card; adaptive
  learning indicator (🔄/✅/⚠️) in Quota State.

**Polish (shipped):**

1. ✅ **Engine respects human `user-overrides.conf [username]` pins** — reconcile
   loads user-overrides per sweep; `_pool_eligible` skips a client whose human
   override sets the target sim, so the ledger never over-counts (§10).
2. ✅ **Reported sim flags win in the Clients view** — `effective_client_fields`
   lets a rotating client's reported toggles win over its home-bucket profile
   (connectivity still from the profile).
3. ✅ **Placement under-min + at-max warnings** — `_reconcile_placement` records
   shortfalls (`placement_warnings()`); Quota State shows a warnings banner plus
   the per-quota ⚠️ At-max badge.

**Remaining (needs live validation / future feature):**

4. **`_alert_firing` signal** reads active Central alerts best-effort (mode-aware,
   cached, holds when unknown) — validate end-to-end against the live poller for
   centralized vs distributed tenants once exercised.
5. **`quota_at_max_not_firing` / `placement_under_min`** currently surface as UI
   badges/banner; wire them into the internal **alert feature** as emitters when
   it ships (§14).

---

## 16. Suggested build order (low → high client risk)

1. **Hub + spoke, no client change:** SSID matrix + placement quotas + site
   source (PXMX injection) + adaptive controller + warnings + reported-bucket
   trust. Fully testable server-side; old clients ignore what they don't read.
2. **Client scripts (VERSION-gated):** ambient bucket rotation + `random_pool`
   honoring + frozen connectivity. Rolls out gradually via `update.sh`;
   backward-safe (old clients keep their crc32 bucket).
3. **Learning + indicators + operator visibility** polish.

---

*Design agreed iteratively; this doc is the build reference. Update it as the
implementation lands (keep §15 current).*
