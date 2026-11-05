"""Linux host-monitor role for the generic LM spoke agent.

Loaded at runtime by the spoke as its active "role". It answers commands that
report CPU, memory, disk, process and network information about the machine the
agent runs on, and can run a small allowlist of shell commands for diagnostics.
"""
# Standard-library module used to read the hostname, OS and Python version.
import platform
# Standard-library module used to run allowlisted shell commands (RUN_CMD).
import subprocess
# Type hints only; they document the shape of command payloads and replies.
from typing import Dict, Any

# psutil is an optional third-party library that gives detailed host metrics.
try:
    # Try to import it; if it is present we can report full stats.
    import psutil
    # Remember that psutil loaded so every method can check before using it.
    _PSUTIL = True
# psutil is not installed: keep working but return partial data instead of crashing.
except ImportError:
    _PSUTIL = False

# The base role class lives next to this file when run from the agent directory...
try:
    from agent_role import AgentRole
# ...but under the "src" package when run from the repository root, so try both.
except ImportError:
    from src.agent_role import AgentRole


class LinuxMonitorRole(AgentRole):
    """
    Role: linux_monitor

    Exposes host OS stats (CPU, memory, disk, network, processes) as LM
    spoke commands.  Works without psutil but will return partial data —
    install psutil for full metrics.

    Commands:
        GET_HOST_STATS    — cpu/mem/disk/platform summary
        GET_PROCESSES     — top-50 processes by CPU
        GET_NETWORK       — interface addresses and link state
        RUN_CMD           — run an allowlisted shell command (see config.allowlist)
    """

    # Identifier the spoke uses to pick this role from its configuration.
    role_name = "linux_monitor"
    # This role is loaded by agent spokes (not by the hub).
    module_type = "agent"

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        """Store the spoke identity, config and the RUN_CMD allowlist."""
        # Let the base class store spoke_id and config for shared use.
        super().__init__(spoke_id, config)
        # Commands RUN_CMD may execute; an empty list means "no restriction" (see _run_cmd).
        self.allowlist: list = config.get("allowlist", [])

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Route a hub command to the matching helper and return its result dict."""
        # Host-wide CPU/memory/disk summary.
        if command_type == "GET_HOST_STATS":
            return self._host_stats()
        # Top processes by CPU usage.
        if command_type == "GET_PROCESSES":
            return self._processes()
        # Network interfaces and their addresses.
        if command_type == "GET_NETWORK":
            return self._network()
        # Run an allowlisted command with the arguments supplied by the hub.
        if command_type == "RUN_CMD":
            return self._run_cmd(data.get("cmd", ""), data.get("args", []))
        # Anything else is not ours: let the base class return its "not handled" error.
        return await super().handle_command(command_type, data)

    async def get_status(self) -> Dict[str, Any]:
        """Return the base role status plus hostname and psutil availability."""
        # Start from the base role status (role name and ACTIVE flag).
        base = await super().get_status()
        # Add the hostname so the hub can tell agents apart.
        base["hostname"] = platform.node()
        # Tell the hub whether full metrics are available on this host.
        base["psutil_available"] = _PSUTIL
        return base

    # ── Internals ─────────────────────────────────────────────────────────────

    def _host_stats(self) -> Dict[str, Any]:
        """Build a CPU, memory, disk and platform summary for this host."""
        # Basic platform facts are always available, even without psutil.
        result: Dict[str, Any] = {
            "status": "SUCCESS",
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        }
        # psutil is available: add live CPU, memory and disk usage.
        if _PSUTIL:
            # Sample CPU usage over half a second; a zero interval would return a meaningless 0.0.
            result["cpu_percent"] = psutil.cpu_percent(interval=0.5)
            # Read system memory and convert bytes to gigabytes for readability.
            mem = psutil.virtual_memory()
            result["memory"] = {
                "total_gb": round(mem.total / 1e9, 2),
                "used_gb": round(mem.used / 1e9, 2),
                "percent": mem.percent,
            }
            # Collect per-mount disk usage keyed by mount point.
            disk = {}
            # Walk every mounted partition reported by the OS.
            for part in psutil.disk_partitions():
                try:
                    # Read usage for this mount and record it in gigabytes.
                    u = psutil.disk_usage(part.mountpoint)
                    disk[part.mountpoint] = {
                        "total_gb": round(u.total / 1e9, 2),
                        "used_gb": round(u.used / 1e9, 2),
                        "percent": u.percent,
                    }
                # Some mounts (e.g. system or removable volumes) deny access; skip them quietly.
                except PermissionError:
                    pass
            # Attach the disk map to the result.
            result["disk"] = disk
        # Without psutil we can only return the platform facts, plus a hint to install it.
        else:
            result["warning"] = "psutil not installed — install it for full metrics"
        # Hand the assembled stats back to the caller.
        return result

    def _processes(self) -> Dict[str, Any]:
        """Return the top 50 running processes sorted by CPU usage."""
        # Process listing needs psutil; report an error rather than guess.
        if not _PSUTIL:
            return {"status": "ERROR", "message": "psutil not installed"}
        # Gather a lightweight info dict for every running process.
        procs = []
        # Ask psutil only for the fields we report, which is faster than full objects.
        for p in psutil.process_iter(["pid", "name", "status", "cpu_percent", "memory_percent"]):
            try:
                # Save the pre-fetched info for this process.
                procs.append(p.info)
            # The process exited while we were iterating; ignore it.
            except psutil.NoSuchProcess:
                pass
        # Busiest processes first; a missing CPU value counts as 0.
        procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
        # Return only the top 50 to keep the reply small.
        return {"status": "SUCCESS", "processes": procs[:50]}

    def _network(self) -> Dict[str, Any]:
        """Return each network interface with its link state, speed and addresses."""
        # Interface details need psutil; report an error rather than guess.
        if not _PSUTIL:
            return {"status": "ERROR", "message": "psutil not installed"}
        # Link state/speed per interface, and addresses per interface.
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        # Build one entry per interface name.
        interfaces = {}
        # Union of names from both sources, since an interface may appear in only one.
        for iface in set(list(stats) + list(addrs)):
            # Link stats may be missing for this interface; handle that below.
            s = stats.get(iface)
            # Record up/down state, speed and every address bound to the interface.
            interfaces[iface] = {
                "is_up": s.isup if s else False,
                "speed_mbps": s.speed if s else 0,
                "addresses": [{"family": str(a.family), "address": a.address}
                               for a in addrs.get(iface, [])],
            }
        # Return all interfaces to the caller.
        return {"status": "SUCCESS", "interfaces": interfaces}

    def _run_cmd(self, cmd: str, args: list) -> Dict[str, Any]:
        """Run an allowlisted command with arguments and return its exit code and output."""
        # Refuse an empty request; there is nothing to run.
        if not cmd:
            return {"status": "ERROR", "message": "No command specified"}
        # When an allowlist is configured, only commands on it may run (safety guard).
        if self.allowlist and cmd not in self.allowlist:
            return {"status": "ERROR", "message": f"'{cmd}' is not in the allowlist"}
        # Run the command directly (no shell) so arguments cannot inject extra commands.
        try:
            # Capture text output and stop after 10 seconds so a hung command cannot block the
            # agent.
            result = subprocess.run(
                [cmd] + [str(a) for a in args],
                capture_output=True, text=True, timeout=10
            )
            # Return the exit code plus truncated output to keep the reply a manageable size.
            return {
                "status": "SUCCESS",
                "returncode": result.returncode,
                "stdout": result.stdout[:4096],
                "stderr": result.stderr[:1024],
            }
        # The command ran longer than the 10-second limit.
        except subprocess.TimeoutExpired:
            return {"status": "ERROR", "message": "Command timed out"}
        # Any other failure (e.g. command not found) is reported back as an error.
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}
