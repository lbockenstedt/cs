"""SimulationEngine — the cs spoke's own client simulator.

Two roles, one engine:

1. **Config/bucket authority** — :meth:`resolve` / :meth:`get_current_state`
   expose the canonical profile for a hostname (used by the mgmt API's
   ``/api/config`` and ``CS_GET_SIMULATION_STATE``).
2. **Runnable simulator** — :meth:`run_iteration` / :meth:`run_loop` execute the
   enabled simulation primitives in-process (the cs node acting as a client, or a
   standalone test). The same primitive code is the reference behaviour the
   distributed ``clients/linux/*.sh`` scripts replicate inside VMs.

The 100-iteration loop mirrors ``clients/linux/simulation.sh``: resolve profile →
kill-switch check → ``sim_phy`` adapter setup → ``sim_load`` skip gate → dispatch
enabled sims concurrently (bounded, per-sim timeout) → status beacon → sleep.
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Async runtime for the loop, subprocesses and timeouts.
import asyncio
# The status beacon is written as JSON.
import json
# Standard-library logging.
import logging
# Random sim-load threshold and offline duration.
import random
# Checks whether host tools (ip, nmcli, ping, apt-get) are installed.
import shutil
# Timestamp for the status beacon.
import time
# Filesystem path handling.
from pathlib import Path
# Type hints only.
from typing import Any, Dict, List, Optional

# Config parsing and profile resolution for simulation.conf.
import sim_config
# Tells whether a profile flag is "on".
from sim_config import flag_on
# The registry of simulation primitives and their shared context.
from sim_primitives import PRIMITIVES, SimCtx

# Library module — do NOT call basicConfig here (the process entrypoint
# cs/lm-spoke/src/control_plane.py owns logging config via configure_logging()).
logger = logging.getLogger("SimulationEngine")

# Subset of the profile emitted in the status beacon (CLIENT_API.md "config" obj).
# wsite (the Site) + sim_phy (PHY) are surfaced so the hub Clients view can show
# Site / PHY per client — the bash client's status write omits both, so the
# resolved profile is the only source (mirrors the original hub effective_config).
_BEACON_CONFIG_KEYS = (
    "kill_switch", "dns_fail", "iperf", "www_traffic", "download", "ping_test",
    "ssidpw_fail", "auth_fail", "mac_auth_fail", "dhcp_fail", "sim_phy", "wsite",
)


class SimulationEngine:
    """Owns the sim engine loop + the effective-profile resolver
    (``sim_config.resolve``: ``[simulation]`` → ``[address/server]`` → the
    ``[sX]`` bucket chosen by ``crc32(hostname) % 10`` → per-``[username]``
    override). Drives ``sim_primitives`` per active flag. See the module
    docstring."""

    def __init__(
        self,
        hostname: str,
        config_dir: Path | str = Path("../configs"),
        data_dir: Path | str = Path("../data"),
    ) -> None:
        """Set up the engine for ``hostname`` with its config and data folders."""
        # Remember the hostname and folders; make sure the data folder exists.
        self.hostname = hostname
        self.config_dir = Path(config_dir)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Runtime state.
        self.iteration = 0
        self.active_simulations: List[str] = []
        self.status = "IDLE"
        self._kill_switch = False  # in-memory override (CS_KILL_SWITCH)
        self._profile_patch: Dict[str, str] = {}  # SET_SIMULATION_PROFILE overlay
        self._loop_task: Optional[asyncio.Task] = None
        self._stop = False

        # Shared context passed to every primitive, then load the config files.
        self.ctx = SimCtx(config_dir=self.config_dir, work_dir=self.data_dir)
        self.reload_config()

    # ── config ──────────────────────────────────────────────────────────────
    def reload_config(self) -> None:
        """Re-parse ``simulation.conf`` + ``user-overrides.conf`` from disk."""
        self.sim_conf, self.user_conf = sim_config.load_configs(self.config_dir)

    def resolve(self) -> Dict[str, Any]:
        """Resolve this engine's hostname → effective profile (patched)."""
        resolved = sim_config.resolve_profile(self.hostname, self.sim_conf, self.user_conf)
        if self._profile_patch:
            resolved["profile"].update(self._profile_patch)
        return resolved

    def update_config(self, patch: Dict[str, Any]) -> None:
        """Apply an in-memory profile patch (``SET_SIMULATION_PROFILE``).

        Keys in *patch* override the resolved bucket/user values until cleared.
        """
        if isinstance(patch, dict):
            self._profile_patch.update({k: str(v) for k, v in patch.items()})

    def clear_patch(self) -> None:
        """Remove any in-memory profile patch."""
        # Drop all patched keys.
        self._profile_patch.clear()

    # ── kill switch ─────────────────────────────────────────────────────────
    def kill_switch_active(self) -> bool:
        """Return True if the kill switch is on (in memory or in kill_switch.txt)."""
        # The in-memory switch wins.
        if self._kill_switch:
            return True
        # mtime-keyed cache. run_iteration calls this every ~5 s and /api/kill-switch
        # calls it on the cs spoke's shared event loop; reading kill_switch.txt
        # each time is a sync open/read/close that stalls the loop (contributes
        # to the hub's "Request Timeout"). Re-read only when the file's mtime
        # changes (set_kill_switch writes the file → mtime changes → miss →
        # re-read); otherwise one inode-cached stat returns the cached bool.
        # Stat the switch file to build a cache key.
        ks = self.config_dir / "kill_switch.txt"
        try:
            st = ks.stat()
            # Size + inode too: two writes inside one coarse mtime tick
            # (on → off) would otherwise share a key and return stale state.
            mtime = (st.st_mtime_ns, st.st_size, st.st_ino)
            exists = True
        # Missing or unreadable file.
        except OSError:
            mtime = -1
            exists = False
        # Reuse the cached answer if the file has not changed.
        cache = getattr(self, "_ks_cache", None)
        if cache is not None and cache[0] == mtime:
            return cache[1]
        # Read the file: the switch is on only if it says "on".
        active = False
        if exists:
            try:
                active = ks.read_text(encoding="utf-8").strip().lower() == "on"
            # Unreadable file counts as off.
            except Exception:  # noqa: BLE001
                active = False
        # Cache the answer with its key and return it.
        self._ks_cache = (mtime, active)
        return active

    def set_kill_switch(self, on: bool) -> None:
        """Turn the kill switch on or off, in memory and in kill_switch.txt."""
        # Set the in-memory flag and write the file so it survives restarts.
        self._kill_switch = bool(on)
        ks = self.config_dir / "kill_switch.txt"
        ks.write_text("on\n" if on else "off\n", encoding="utf-8")

    # ── state ──────────────────────────────────────────────────────────────
    def get_current_state(self) -> Dict[str, Any]:
        """Return this host's current simulation state (profile, status, iteration)."""
        # Resolve the profile and build the state snapshot.
        resolved = self.resolve()
        profile = resolved["profile"]
        return {
            "username": resolved["username"],
            "hostname": self.hostname,
            "simulation_id": resolved["simulation_id"],
            "config": {k: profile.get(k, "off") for k in _BEACON_CONFIG_KEYS},
            "active_simulations": self.active_simulations,
            "status": self.status,
            "iteration": self.iteration,
        }

    # ── one iteration ──────────────────────────────────────────────────────
    async def run_iteration(self, iter_sleep: float = 5.0) -> Dict[str, Any]:
        """Run a single outer iteration of the simulation loop."""
        # Reload the config files and resolve this host's profile.
        self.reload_config()
        resolved = self.resolve()
        profile = resolved["profile"]
        sim_id = resolved["simulation_id"]

        # Kill switch — sleep and report KILLED (matches simulation.sh's 5-min nap).
        if self.kill_switch_active():
            self.status = "KILLED"
            self.active_simulations = []
            logger.info("Kill switch active — skipping simulation iteration")
            return {"hostname": self.hostname, "bucket": sim_id,
                    "active_sims": [], "status": "KILLED", "iteration": self.iteration}

        # sim_phy adapter setup (disable the interface not in use). Best-effort.
        await self._setup_sim_phy(profile)

        # sim_load skip gate: if sim_load < a random 1..99 threshold, skip the
        # primitives this iteration but stay "associated" (no sim work done).
        sim_load = int(profile.get("sim_load", "100") or "100")
        rn_sim_load = random.randint(1, 99)
        if sim_load < rn_sim_load:
            self.status = "SKIPPED"
            self.active_simulations = []
            logger.info("Simulation load under threshold (%d < %d) — skipping sims", sim_load, rn_sim_load)
            self.iteration += 1
            await self._beacon(resolved, ran=[], errors=[])
            if iter_sleep:
                await asyncio.sleep(iter_sleep)
            return {"hostname": self.hostname, "bucket": sim_id, "active_sims": [],
                    "status": "SKIPPED", "iteration": self.iteration}

        # Dispatch every enabled primitive concurrently (bounded, per-sim timeout).
        # Primitives whose flag is on in the profile.
        enabled = [name for name in PRIMITIVES if flag_on(profile, name)]
        self.active_simulations = list(enabled)
        self.status = "RUNNING"
        results, errors = await self._dispatch(enabled)

        # Count the iteration and write the status beacon.
        self.iteration += 1
        await self._beacon(resolved, ran=enabled, errors=errors)

        # Pause between iterations.
        if iter_sleep:
            await asyncio.sleep(iter_sleep)

        # Report the iteration result.
        return {
            "hostname": self.hostname,
            "bucket": sim_id,
            "active_sims": enabled,
            "status": "SUCCESS",
            "iteration": self.iteration,
            "results": results,
        }

    async def _dispatch(self, enabled: List[str]) -> tuple[List[Dict[str, Any]], List[str]]:
        """Run enabled primitives concurrently with a per-sim timeout."""
        # Nothing enabled: nothing to run.
        if not enabled:
            return [], []

        async def _one(name: str) -> Dict[str, Any]:
            """Run one primitive with a 45-second timeout; failures become degraded results."""
            # Run the primitive with the current profile.
            try:
                return await asyncio.wait_for(
                    PRIMITIVES[name](self.resolve()["profile"], self.ctx), timeout=45.0)
            # Too slow: report a timeout.
            except asyncio.TimeoutError:
                return {"name": name, "ok": False, "detail": "timeout", "degraded": True}
            # Any other error is reported, not raised.
            except Exception as exc:  # noqa: BLE001
                return {"name": name, "ok": False, "detail": str(exc), "degraded": True}

        # Run all primitives at once.
        results = await asyncio.gather(*(_one(n) for n in enabled))
        # Describe every failed or degraded result as an error string.
        errors = [
            f"{r.get('name')}: {r.get('detail', 'degraded')} (missing {r.get('missing', '')})".strip()
            for r in results
            if isinstance(r, dict) and (r.get("degraded") or not r.get("ok"))
        ]
        # Return the results and the error list.
        return list(results), errors

    async def _setup_sim_phy(self, profile: Dict[str, str]) -> None:
        """Disable the interface not in use so traffic egresses the simulated PHY.

        Best-effort: silently degrades if ``ip``/``nmcli`` or the adapters are
        absent (dev/headless hosts).
        """
        # No ``ip`` tool: nothing can be changed.
        if not shutil.which("ip"):
            return
        # Which PHY to simulate (wireless by default).
        phy = str(profile.get("sim_phy", "wireless")).lower()
        try:
            # Ethernet mode: take the wireless interface down.
            if phy == "ethernet":
                wl = await _find_iface(("wlx", "wlan"))
                if wl:
                    await _ip_link(wl, "down")
            # Wireless mode: take the wired interface down unless it is the management link.
            else:  # wireless → disable wired (mgmt-guarded)
                ea = await _find_iface(("enp", "eno", "eth"))
                if ea and not await _is_mgmt_iface(ea):
                    await _ip_link(ea, "down")
        # Failures only log at debug level.
        except Exception as exc:  # noqa: BLE001
            logger.debug("sim_phy setup failed: %s", exc)

    # ── status beacon ──────────────────────────────────────────────────────
    async def _beacon(self, resolved: Dict[str, Any], ran: List[str], errors: List[str]) -> None:
        """Write a CLIENT_API.md-shaped status snapshot to data/client-status.json."""
        # Build the snapshot: identity, connectivity, sims run, errors and config flags.
        profile = resolved["profile"]
        payload = {
            "hostname": self.hostname,
            "simulation_id": resolved["simulation_id"],
            "platform": "linux",
            "iteration": self.iteration,
            "connected_ssid": await _connected_ssid(),
            "gateway_reachable": await _gateway_reachable(),
            "active_simulations": ran,
            "errors": errors,
            "config": {k: profile.get(k, "off") for k in _BEACON_CONFIG_KEYS},
            "status": self.status,
            "timestamp": time.time(),
        }
        # Offload the json.dumps + write off the loop (mirrors client_registry
        # _apersist) so neither the O(N) serialization nor the file I/O can
        # stall the hub WS receive on a contended disk.
        try:
            await asyncio.to_thread(
                lambda: (self.data_dir / "client-status.json").write_text(
                    json.dumps(payload, indent=2), encoding="utf-8"))
        # A write failure only logs at debug level.
        except Exception as exc:  # noqa: BLE001
            logger.debug("beacon write failed: %s", exc)

    # ── loop control (CS_START/STOP_SIMULATION) ─────────────────────────────
    async def run_loop(self, iterations: int = 100, iter_sleep: float = 5.0) -> Dict[str, Any]:
        """Run *iterations* iterations (the outer cycle), then return a summary."""
        # Run until the iteration count is reached or stop() is called.
        self._stop = False
        ran = 0
        for _ in range(iterations):
            if self._stop:
                break
            await self.run_iteration(iter_sleep=iter_sleep)
            ran += 1
        # Post-cycle: apt update (best-effort) + optional offline window.
        await self._post_cycle()
        return {"iterations": ran, "status": "STOPPED" if self._stop else "DONE"}

    def start(self, iterations: int = 100, iter_sleep: float = 5.0) -> asyncio.Task:
        """Launch run_loop as a background task (CS_START_SIMULATION)."""
        self.stop()
        # Stop any running loop, then start a new one in the background.
        self._loop_task = asyncio.create_task(self.run_loop(iterations, iter_sleep))
        logger.info("Started simulation loop (%d iterations)", iterations)
        return self._loop_task

    def stop(self) -> None:
        """Cancel the running loop and arm the kill switch (CS_STOP_SIMULATION)."""
        # Ask the loop to stop and cancel its task.
        self._stop = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        self._loop_task = None

    async def _post_cycle(self) -> None:
        """apt update/upgrade + allow_offline window — best-effort, degrades silently."""
        # Resolve the current profile.
        resolved = self.resolve()
        profile = resolved["profile"]
        # Unless rapid_update is on, run apt-get update (best-effort, 2-minute limit).
        if str(profile.get("rapid_update", "")).lower() != "on" and shutil.which("apt-get"):
            try:
                p = await asyncio.create_subprocess_exec(
                    "sudo", "apt-get", "update",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(p.wait(), timeout=120.0)
            except Exception:  # noqa: BLE001
                pass
        # allow_offline: take the interfaces down for a random time, capped at 30 s here.
        if str(profile.get("allow_offline", "")).lower() == "yes":
            offline = random.randint(1, 14400)
            logger.info("allow_offline: bringing interfaces down for %ds", offline)
            await _bounce_all_ifaces(down=True)
            await asyncio.sleep(min(offline, 30))  # bounded in self-sim mode
            await _bounce_all_ifaces(down=False)


# ── host helpers (best-effort, degrade when tools/adapters absent) ───────────
#
# ALL of these run via ``asyncio.create_subprocess_exec`` + ``communicate`` so a
# hung ``ip``/``nmcli``/``ping`` (or just a slow one under load) CANNOT stall the
# cs spoke's shared event loop. The prior synchronous ``subprocess.run`` here
# was the root cause of the hub's "Request Timeout from cs-svr-02-spoke after
# 30.0s": ``_beacon`` runs every iteration (including the SKIPPED sim-load
# path), and ``_connected_ssid`` + ``_gateway_reachable`` together blocked the
# loop for up to ~9s per iteration, starving the in-flight hub WS receive.
async def _run_capture(argv, timeout: float = 3.0):
    """Async capture-stdout helper. Returns ``(stdout_text, returncode)``;
    ``("", None)`` on any failure or timeout (process is killed + reaped)."""
    # Start the process; a missing tool or exec error gives an empty result.
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 — tool missing / exec error → degrade
        return "", None
    # Wait for output within the timeout and decode it.
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        return text, proc.returncode
    # Timeout or read error: kill the process and reap it.
    except Exception:  # noqa: BLE001 — timeout or read error; kill + reap
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return "", None


async def _find_iface(prefixes) -> Optional[str]:
    """Return the first interface whose name starts with one of ``prefixes``, or None."""
    # No ``ip`` tool: cannot look.
    if not shutil.which("ip"):
        return None
    # List interfaces briefly and return the first matching name.
    out, _ = await _run_capture(["ip", "-br", "a"], timeout=3.0)
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith(prefixes):
            return name
    return None


async def _is_mgmt_iface(iface: str) -> bool:
    """Return True if ``iface`` has a 169.253.x management address."""
    # Without an interface or ``ip`` tool it cannot be the management link.
    if not iface or not shutil.which("ip"):
        return False
    # Read the interface's IPv4 addresses and look for the management subnet.
    out, _ = await _run_capture(["ip", "-4", "addr", "show", "dev", iface], timeout=3.0)
    return "169.253." in out


async def _ip_link(iface: str, state: str) -> None:
    """Set ``iface`` up or down with ``sudo ip link`` (5-second limit)."""
    # Run the command quietly and wait for it.
    p = await asyncio.create_subprocess_exec(
        "sudo", "ip", "link", "set", "dev", iface, state,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await asyncio.wait_for(p.wait(), timeout=5.0)


async def _bounce_all_ifaces(down: bool) -> None:
    """Take the wireless and wired interfaces down or up, never downing the management link."""
    # Find the wireless and wired interfaces.
    wl = await _find_iface(("wlx", "wlan"))
    ea = await _find_iface(("enp", "eno", "eth"))
    # Change each one found, skipping the management link when going down.
    for iface in (a for a in [wl, ea] if a):
        if down and await _is_mgmt_iface(iface):
            continue
        # Failures are ignored.
        try:
            await _ip_link(iface, "down" if down else "up")
        except Exception:  # noqa: BLE001
            pass


async def _connected_ssid() -> str:
    """Return the SSID the Wi-Fi adapter is connected to, or "" if unknown."""
    # No nmcli: SSID unknown.
    if not shutil.which("nmcli"):
        return ""
    # List Wi-Fi networks and return the active one's SSID.
    out, _ = await _run_capture(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                                timeout=3.0)
    for line in out.splitlines():
        if line.startswith("yes"):
            return line.split(":", 1)[1] if ":" in line else ""
    return ""


async def _gateway_reachable() -> Optional[bool]:
    """Ping of the default gateway; None if undeterminable.

    Sends 2 packets (-c 2), 2s timeout per packet — ping's own exit code is 0
    if AT LEAST ONE reply came back, so a single lost ICMP reply under host
    load (CPU contention, virtual-switch jitter — routine on a multi-VM fleet
    host) no longer flips a genuinely-connected client to "unreachable" the
    way a single -c 1 -W 1 probe did. A truly unreachable gateway still fails
    both packets and correctly reports unreachable; this only removes the
    false-negative window, not the check's ability to catch a real outage."""
    # Without ip and ping the answer is unknown.
    if not shutil.which("ip") or not shutil.which("ping"):
        return None
    # Find the default gateway in the routing table.
    out, _ = await _run_capture(["ip", "route"], timeout=3.0)
    gw = None
    for line in out.splitlines():
        if line.startswith("default via "):
            gw = line.split()[2]
            break
    # No default route: unknown.
    if not gw:
        return None
    # Ping the gateway; success means reachable.
    _, rc = await _run_capture(["ping", "-c", "2", "-W", "2", gw], timeout=6.0)
    return rc == 0