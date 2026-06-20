import platform
import subprocess
from typing import Dict, Any

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    from agent_role import AgentRole
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

    role_name = "linux_monitor"
    module_type = "agent"

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.allowlist: list = config.get("allowlist", [])

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if command_type == "GET_HOST_STATS":
            return self._host_stats()
        if command_type == "GET_PROCESSES":
            return self._processes()
        if command_type == "GET_NETWORK":
            return self._network()
        if command_type == "RUN_CMD":
            return self._run_cmd(data.get("cmd", ""), data.get("args", []))
        return await super().handle_command(command_type, data)

    async def get_status(self) -> Dict[str, Any]:
        base = await super().get_status()
        base["hostname"] = platform.node()
        base["psutil_available"] = _PSUTIL
        return base

    # ── Internals ─────────────────────────────────────────────────────────────

    def _host_stats(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "status": "SUCCESS",
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        }
        if _PSUTIL:
            result["cpu_percent"] = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            result["memory"] = {
                "total_gb": round(mem.total / 1e9, 2),
                "used_gb": round(mem.used / 1e9, 2),
                "percent": mem.percent,
            }
            disk = {}
            for part in psutil.disk_partitions():
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    disk[part.mountpoint] = {
                        "total_gb": round(u.total / 1e9, 2),
                        "used_gb": round(u.used / 1e9, 2),
                        "percent": u.percent,
                    }
                except PermissionError:
                    pass
            result["disk"] = disk
        else:
            result["warning"] = "psutil not installed — install it for full metrics"
        return result

    def _processes(self) -> Dict[str, Any]:
        if not _PSUTIL:
            return {"status": "ERROR", "message": "psutil not installed"}
        procs = []
        for p in psutil.process_iter(["pid", "name", "status", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except psutil.NoSuchProcess:
                pass
        procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
        return {"status": "SUCCESS", "processes": procs[:50]}

    def _network(self) -> Dict[str, Any]:
        if not _PSUTIL:
            return {"status": "ERROR", "message": "psutil not installed"}
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        interfaces = {}
        for iface in set(list(stats) + list(addrs)):
            s = stats.get(iface)
            interfaces[iface] = {
                "is_up": s.isup if s else False,
                "speed_mbps": s.speed if s else 0,
                "addresses": [{"family": str(a.family), "address": a.address}
                               for a in addrs.get(iface, [])],
            }
        return {"status": "SUCCESS", "interfaces": interfaces}

    def _run_cmd(self, cmd: str, args: list) -> Dict[str, Any]:
        if not cmd:
            return {"status": "ERROR", "message": "No command specified"}
        if self.allowlist and cmd not in self.allowlist:
            return {"status": "ERROR", "message": f"'{cmd}' is not in the allowlist"}
        try:
            result = subprocess.run(
                [cmd] + [str(a) for a in args],
                capture_output=True, text=True, timeout=10
            )
            return {
                "status": "SUCCESS",
                "returncode": result.returncode,
                "stdout": result.stdout[:4096],
                "stderr": result.stderr[:1024],
            }
        except subprocess.TimeoutExpired:
            return {"status": "ERROR", "message": "Command timed out"}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}
