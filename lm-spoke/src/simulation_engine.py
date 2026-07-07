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

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import sim_config
from sim_config import flag_on
from sim_primitives import PRIMITIVES, SimCtx

# Library module — do NOT call basicConfig here (the process entrypoint
# cs/lm-spoke/src/control_plane.py owns logging config via configure_logging()).
logger = logging.getLogger("SimulationEngine")

# Subset of the profile emitted in the status beacon (CLIENT_API.md "config" obj).
_BEACON_CONFIG_KEYS = (
    "kill_switch", "dns_fail", "iperf", "www_traffic", "download", "ping_test",
    "ssidpw_fail", "auth_fail", "dhcp_fail", "sim_phy",
)


class SimulationEngine:
    def __init__(
        self,
        hostname: str,
        config_dir: Path | str = Path("../configs"),
        data_dir: Path | str = Path("../data"),
    ) -> None:
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
        self._profile_patch.clear()

    # ── kill switch ─────────────────────────────────────────────────────────
    def kill_switch_active(self) -> bool:
        if self._kill_switch:
            return True
        # mtime-keyed cache. run_iteration calls this every ~5 s and /api/kill-switch
        # calls it on the cs spoke's shared event loop; reading kill_switch.txt
        # each time is a sync open/read/close that stalls the loop (contributes
        # to the hub's "Request Timeout"). Re-read only when the file's mtime
        # changes (set_kill_switch writes the file → mtime changes → miss →
        # re-read); otherwise one inode-cached stat returns the cached bool.
        ks = self.config_dir / "kill_switch.txt"
        try:
            mtime = ks.stat().st_mtime_ns
            exists = True
        except OSError:
            mtime = -1
            exists = False
        cache = getattr(self, "_ks_cache", None)
        if cache is not None and cache[0] == mtime:
            return cache[1]
        active = False
        if exists:
            try:
                active = ks.read_text(encoding="utf-8").strip().lower() == "on"
            except Exception:  # noqa: BLE001
                active = False
        self._ks_cache = (mtime, active)
        return active

    def set_kill_switch(self, on: bool) -> None:
        self._kill_switch = bool(on)
        ks = self.config_dir / "kill_switch.txt"
        ks.write_text("on\n" if on else "off\n", encoding="utf-8")

    # ── state ──────────────────────────────────────────────────────────────
    def get_current_state(self) -> Dict[str, Any]:
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
        enabled = [name for name in PRIMITIVES if flag_on(profile, name)]
        self.active_simulations = list(enabled)
        self.status = "RUNNING"
        results, errors = await self._dispatch(enabled)

        self.iteration += 1
        await self._beacon(resolved, ran=enabled, errors=errors)

        if iter_sleep:
            await asyncio.sleep(iter_sleep)

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
        if not enabled:
            return [], []

        async def _one(name: str) -> Dict[str, Any]:
            try:
                return await asyncio.wait_for(
                    PRIMITIVES[name](self.resolve()["profile"], self.ctx), timeout=45.0)
            except asyncio.TimeoutError:
                return {"name": name, "ok": False, "detail": "timeout", "degraded": True}
            except Exception as exc:  # noqa: BLE001
                return {"name": name, "ok": False, "detail": str(exc), "degraded": True}

        results = await asyncio.gather(*(_one(n) for n in enabled))
        errors = [
            f"{r.get('name')}: {r.get('detail', 'degraded')} (missing {r.get('missing', '')})".strip()
            for r in results
            if isinstance(r, dict) and (r.get("degraded") or not r.get("ok"))
        ]
        return list(results), errors

    async def _setup_sim_phy(self, profile: Dict[str, str]) -> None:
        """Disable the interface not in use so traffic egresses the simulated PHY.

        Best-effort: silently degrades if ``ip``/``nmcli`` or the adapters are
        absent (dev/headless hosts).
        """
        if not shutil.which("ip"):
            return
        phy = str(profile.get("sim_phy", "wireless")).lower()
        try:
            if phy == "ethernet":
                wl = _find_iface(("wlx", "wlan"))
                if wl:
                    await _ip_link(wl, "down")
            else:  # wireless → disable wired (mgmt-guarded)
                ea = _find_iface(("enp", "eno", "eth"))
                if ea and not _is_mgmt_iface(ea):
                    await _ip_link(ea, "down")
        except Exception as exc:  # noqa: BLE001
            logger.debug("sim_phy setup failed: %s", exc)

    # ── status beacon ──────────────────────────────────────────────────────
    async def _beacon(self, resolved: Dict[str, Any], ran: List[str], errors: List[str]) -> None:
        """Write a CLIENT_API.md-shaped status snapshot to data/client-status.json."""
        profile = resolved["profile"]
        payload = {
            "hostname": self.hostname,
            "simulation_id": resolved["simulation_id"],
            "platform": "linux",
            "iteration": self.iteration,
            "connected_ssid": _connected_ssid(),
            "gateway_reachable": _gateway_reachable(),
            "active_simulations": ran,
            "errors": errors,
            "config": {k: profile.get(k, "off") for k in _BEACON_CONFIG_KEYS},
            "status": self.status,
            "timestamp": time.time(),
        }
        try:
            (self.data_dir / "client-status.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("beacon write failed: %s", exc)

    # ── loop control (CS_START/STOP_SIMULATION) ─────────────────────────────
    async def run_loop(self, iterations: int = 100, iter_sleep: float = 5.0) -> Dict[str, Any]:
        """Run *iterations* iterations (the outer cycle), then return a summary."""
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
        self._loop_task = asyncio.create_task(self.run_loop(iterations, iter_sleep))
        logger.info("Started simulation loop (%d iterations)", iterations)
        return self._loop_task

    def stop(self) -> None:
        """Cancel the running loop and arm the kill switch (CS_STOP_SIMULATION)."""
        self._stop = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        self._loop_task = None

    async def _post_cycle(self) -> None:
        """apt update/upgrade + allow_offline window — best-effort, degrades silently."""
        resolved = self.resolve()
        profile = resolved["profile"]
        if str(profile.get("rapid_update", "")).lower() != "on" and shutil.which("apt-get"):
            try:
                p = await asyncio.create_subprocess_exec(
                    "sudo", "apt-get", "update",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(p.wait(), timeout=120.0)
            except Exception:  # noqa: BLE001
                pass
        if str(profile.get("allow_offline", "")).lower() == "yes":
            offline = random.randint(1, 14400)
            logger.info("allow_offline: bringing interfaces down for %ds", offline)
            await _bounce_all_ifaces(down=True)
            await asyncio.sleep(min(offline, 30))  # bounded in self-sim mode
            await _bounce_all_ifaces(down=False)


# ── host helpers (best-effort, degrade when tools/adapters absent) ───────────
def _find_iface(prefixes) -> Optional[str]:
    import subprocess
    if not shutil.which("ip"):
        return None
    try:
        out = subprocess.run(["ip", "-br", "a"], capture_output=True, text=True, timeout=3).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith(prefixes):
            return name
    return None


def _is_mgmt_iface(iface: str) -> bool:
    import subprocess
    if not iface or not shutil.which("ip"):
        return False
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", "dev", iface],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:  # noqa: BLE001
        return False
    return "169.253." in out


async def _ip_link(iface: str, state: str) -> None:
    p = await asyncio.create_subprocess_exec(
        "sudo", "ip", "link", "set", "dev", iface, state,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await asyncio.wait_for(p.wait(), timeout=5.0)


async def _bounce_all_ifaces(down: bool) -> None:
    for iface in (a for a in [_find_iface(("wlx", "wlan")), _find_iface(("enp", "eno", "eth"))] if a):
        if down and _is_mgmt_iface(iface):
            continue
        try:
            await _ip_link(iface, "down" if down else "up")
        except Exception:  # noqa: BLE001
            pass


def _connected_ssid() -> str:
    import subprocess
    if not shutil.which("nmcli"):
        return ""
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:  # noqa: BLE001
        return ""
    for line in out.splitlines():
        if line.startswith("yes"):
            return line.split(":", 1)[1] if ":" in line else ""
    return ""


def _gateway_reachable() -> Optional[bool]:
    """Quick ping of the default gateway; None if undeterminable."""
    import subprocess
    if not shutil.which("ip") or not shutil.which("ping"):
        return None
    try:
        rt = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=3).stdout
        gw = None
        for line in rt.splitlines():
            if line.startswith("default via "):
                gw = line.split()[2]
                break
        if not gw:
            return None
        p = subprocess.run(["ping", "-c", "1", "-W", "1", gw],
                           capture_output=True, text=True, timeout=3)
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return None