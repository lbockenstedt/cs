import subprocess
import re
from pathlib import Path
from typing import Dict, Any

try:
    from agent_role import AgentRole
except ImportError:
    from src.agent_role import AgentRole


class DnsRole(AgentRole):
    """
    Role: dns

    Manages a local dnsmasq or bind9 instance.  Detects which is installed.

    Commands:
        GET_DNS_STATUS      — daemon status + uptime
        GET_DNS_RECORDS     — list records from config
        ADD_DNS_RECORD      — add A/CNAME/PTR record (dnsmasq --address or zone file)
        DELETE_DNS_RECORD   — remove record by name
        RELOAD_DNS          — SIGHUP / systemctl reload
    """

    role_name = "dns"
    module_type = "agent"

    _DNSMASQ_HOSTS = Path("/etc/dnsmasq.d/lm-hosts.conf")

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.backend = self._detect_backend()

    def _detect_backend(self) -> str:
        for svc in ("dnsmasq", "named", "bind9"):
            try:
                r = subprocess.run(["systemctl", "is-active", svc],
                                   capture_output=True, text=True)
                if r.stdout.strip() == "active":
                    return svc
            except FileNotFoundError:
                pass
        return "unknown"

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if command_type == "GET_DNS_STATUS":
            return self._status()
        if command_type == "GET_DNS_RECORDS":
            return self._list_records()
        if command_type == "ADD_DNS_RECORD":
            return self._add_record(data)
        if command_type == "DELETE_DNS_RECORD":
            return self._delete_record(data.get("name", ""))
        if command_type == "RELOAD_DNS":
            return self._reload()
        return await super().handle_command(command_type, data)

    def _status(self) -> Dict[str, Any]:
        if self.backend == "unknown":
            return {"status": "ERROR", "message": "No supported DNS daemon found (dnsmasq / bind9)"}
        r = subprocess.run(["systemctl", "status", self.backend],
                            capture_output=True, text=True)
        return {"status": "SUCCESS", "backend": self.backend, "output": r.stdout[:2000]}

    def _list_records(self) -> Dict[str, Any]:
        if not self._DNSMASQ_HOSTS.exists():
            return {"status": "SUCCESS", "records": [], "note": "No LM-managed host file yet"}
        lines = self._DNSMASQ_HOSTS.read_text().splitlines()
        records = [l for l in lines if l.strip() and not l.startswith("#")]
        return {"status": "SUCCESS", "records": records}

    def _add_record(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = data.get("name", "").strip()
        ip = data.get("ip", "").strip()
        if not name or not ip:
            return {"status": "ERROR", "message": "name and ip are required"}
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            return {"status": "ERROR", "message": "Invalid IP address"}
        line = f"address=/{name}/{ip}\n"
        with self._DNSMASQ_HOSTS.open("a") as f:
            f.write(line)
        self._reload()
        return {"status": "SUCCESS", "added": line.strip()}

    def _delete_record(self, name: str) -> Dict[str, Any]:
        if not name or not self._DNSMASQ_HOSTS.exists():
            return {"status": "ERROR", "message": "Record not found"}
        lines = self._DNSMASQ_HOSTS.read_text().splitlines(keepends=True)
        new_lines = [l for l in lines if f"/{name}/" not in l]
        if len(new_lines) == len(lines):
            return {"status": "ERROR", "message": f"No record found for '{name}'"}
        self._DNSMASQ_HOSTS.write_text("".join(new_lines))
        self._reload()
        return {"status": "SUCCESS", "deleted": name}

    def _reload(self) -> Dict[str, Any]:
        if self.backend == "unknown":
            return {"status": "ERROR", "message": "No DNS daemon to reload"}
        subprocess.run(["systemctl", "reload", self.backend], capture_output=True)
        return {"status": "SUCCESS", "message": f"{self.backend} reloaded"}
