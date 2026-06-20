import json
import subprocess
from pathlib import Path
from typing import Dict, Any, List

try:
    import httpx
    _HTTPX = True
except ImportError:
    _HTTPX = False

try:
    from agent_role import AgentRole
except ImportError:
    from src.agent_role import AgentRole


class DhcpRole(AgentRole):
    """
    Role: dhcp

    Manages DHCP leases and subnet configuration via the KEA Control Agent
    HTTP API (http://127.0.0.1:8000 by default) or falls back to dnsmasq
    lease file inspection.

    Commands:
        GET_DHCP_STATUS     — daemon status
        GET_LEASES          — all active leases (MAC → IP → hostname)
        GET_SUBNETS         — configured subnets
        ADD_SUBNET          — add a subnet to KEA config
        ADD_RESERVATION     — add static host reservation (MAC → IP)
        DELETE_RESERVATION  — remove a reservation
    """

    role_name = "dhcp"
    module_type = "agent"

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.kea_url = config.get("kea_url", "http://127.0.0.1:8000")
        self.lease_file = Path(config.get("lease_file", "/var/lib/misc/dnsmasq.leases"))

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if command_type == "GET_DHCP_STATUS":
            return await self._status()
        if command_type == "GET_LEASES":
            return await self._leases()
        if command_type == "GET_SUBNETS":
            return await self._subnets()
        if command_type == "ADD_SUBNET":
            return await self._add_subnet(data)
        if command_type == "ADD_RESERVATION":
            return await self._add_reservation(data)
        if command_type == "DELETE_RESERVATION":
            return await self._del_reservation(data)
        return await super().handle_command(command_type, data)

    async def _kea(self, service: str, command: str, args: dict = None) -> dict:
        if not _HTTPX:
            return {"result": 1, "text": "httpx not installed"}
        payload = {"command": command, "service": [service]}
        if args:
            payload["arguments"] = args
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.post(self.kea_url, json=payload)
                return r.json()[0] if r.json() else {}
        except Exception as e:
            return {"result": 1, "text": str(e)}

    async def _status(self) -> Dict[str, Any]:
        for svc in ("kea-dhcp4-server", "dnsmasq", "isc-dhcp-server"):
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True)
            if r.stdout.strip() == "active":
                return {"status": "SUCCESS", "backend": svc, "state": "active"}
        return {"status": "ERROR", "message": "No DHCP daemon found"}

    async def _leases(self) -> Dict[str, Any]:
        # Try KEA first
        res = await self._kea("dhcp4", "lease4-get-all")
        if res.get("result") == 0:
            leases = res.get("arguments", {}).get("leases", [])
            return {"status": "SUCCESS", "backend": "kea", "leases": leases}
        # Fallback: dnsmasq lease file
        if self.lease_file.exists():
            leases: List[dict] = []
            for line in self.lease_file.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    leases.append({"expiry": parts[0], "mac": parts[1],
                                   "ip": parts[2], "hostname": parts[3]})
            return {"status": "SUCCESS", "backend": "dnsmasq", "leases": leases}
        return {"status": "ERROR", "message": "No DHCP lease source available"}

    async def _subnets(self) -> Dict[str, Any]:
        res = await self._kea("dhcp4", "config-get")
        if res.get("result") == 0:
            subnets = res.get("arguments", {}).get("Dhcp4", {}).get("subnet4", [])
            return {"status": "SUCCESS", "subnets": subnets}
        return {"status": "ERROR", "message": "Could not retrieve subnets from KEA"}

    async def _add_subnet(self, data: Dict[str, Any]) -> Dict[str, Any]:
        subnet = data.get("subnet")
        pools = data.get("pools", [])
        if not subnet:
            return {"status": "ERROR", "message": "subnet is required (e.g. '192.168.10.0/24')"}
        args = {"subnet4": [{"subnet": subnet, "pools": [{"pool": p} for p in pools]}]}
        res = await self._kea("dhcp4", "subnet4-add", args)
        if res.get("result") == 0:
            return {"status": "SUCCESS", "message": f"Subnet {subnet} added"}
        return {"status": "ERROR", "message": res.get("text", "KEA error")}

    async def _add_reservation(self, data: Dict[str, Any]) -> Dict[str, Any]:
        mac = data.get("mac")
        ip = data.get("ip")
        hostname = data.get("hostname", "")
        subnet_id = data.get("subnet_id", 1)
        if not mac or not ip:
            return {"status": "ERROR", "message": "mac and ip are required"}
        args = {"reservation": {"hw-address": mac, "ip-address": ip,
                                "hostname": hostname, "subnet-id": subnet_id}}
        res = await self._kea("dhcp4", "reservation-add", args)
        if res.get("result") == 0:
            return {"status": "SUCCESS", "message": f"Reservation {mac} → {ip} added"}
        return {"status": "ERROR", "message": res.get("text", "KEA error")}

    async def _del_reservation(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ip = data.get("ip")
        subnet_id = data.get("subnet_id", 1)
        if not ip:
            return {"status": "ERROR", "message": "ip is required"}
        args = {"ip-address": ip, "subnet-id": subnet_id}
        res = await self._kea("dhcp4", "reservation-del", args)
        if res.get("result") == 0:
            return {"status": "SUCCESS", "message": f"Reservation for {ip} removed"}
        return {"status": "ERROR", "message": res.get("text", "KEA error")}
