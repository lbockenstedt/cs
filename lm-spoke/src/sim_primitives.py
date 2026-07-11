"""Simulation primitives — one async coroutine per simulation flag.

Each ``sim_<name>(profile, ctx)`` mirrors the behaviour of the corresponding
``clients/linux/<name>.sh`` leaf script, but runs in-process and is **bounded** per call
(the engine's 100-iteration loop repeats them). Every primitive detects its
required tool and **degrades gracefully** — returning
``{"ok": False, "degraded": True, "missing": "<tool>"}`` — rather than raising,
so the spoke runs anywhere (dev laptop, headless LXC, etc.) and only does the work
the host can actually do.

Returns a dict: ``{"name", "ok", "detail", "tool", "degraded?"}``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger("CSPrimitives")

# Optional, purely-Python deps — degrade if absent.
try:  # dns_fail
    import dns.resolver  # type: ignore
    _HAS_DNSPYTHON = True
except Exception:  # pragma: no cover
    _HAS_DNSPYTHON = False

try:  # ping_test
    import icmplib  # type: ignore
    _HAS_ICMPLIB = True
except Exception:  # pragma: no cover
    _HAS_ICMPLIB = False

try:  # download / www_traffic
    import httpx  # type: ignore
    _HAS_HTTPX = True
except Exception:  # pragma: no cover
    _HAS_HTTPX = False

# httpx/httpcore are chatty at INFO; the spoke log only needs warnings from them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class SimCtx:
    """Runtime context shared by all primitives for one spoke."""

    config_dir: Path
    work_dir: Path = Path("/tmp")
    # Caps keep a single run_iteration responsive; the loop repeats the work.
    dns_max_records: int = 5
    dns_passes: int = 1
    download_max_bytes: int = 8 * 1024 * 1024
    www_max_bytes: int = 2 * 1024 * 1024
    ping_max_count: int = 20
    iperf_max_time: int = 3
    wifi_iters: int = 5
    port_flap_iters: int = 5

    def data_lines(self, name: str) -> list[str]:
        """Non-blank, de-duplicated lines from ``configs/<name>`` (preserves order)."""
        p = self.config_dir / name
        if not p.exists():
            return []
        seen: set[str] = set()
        out: list[str] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                out.append(line)
        return out


PrimResult = Dict[str, object]
PrimFn = Callable[[Dict[str, str], SimCtx], Awaitable[PrimResult]]


def _degraded(name: str, missing: str, detail: str = "") -> PrimResult:
    return {"name": name, "ok": False, "degraded": True, "missing": missing, "detail": detail}


def _ok(name: str, detail: str = "", tool: str = "") -> PrimResult:
    return {"name": name, "ok": True, "detail": detail, "tool": tool}


# ── dns_fail ──────────────────────────────────────────────────────────────────
async def sim_dns_fail(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """Query each dns_fail.txt record against the bad-record/bad-ip/latency servers.

    Mirrors ``dns_fail.sh``: ``dig @<server> <record>`` over the union of
    ``dns_bad_record_*`` + ``dns_bad_ip_*`` + ``dns_latency_*``. These servers
    either don't respond or return bogus answers — the point is to generate DNS
    failure alerts, so timeouts/exceptions are expected and swallowed.
    """
    records = ctx.data_lines("dns_fail.txt")
    if not records:
        return _ok("dns_fail", "no dns_fail.txt records", "none")
    records = records[: ctx.dns_max_records]

    servers = [
        profile.get(f"dns_bad_record_{i}", "") for i in (1, 2, 3)
    ] + [
        profile.get(f"dns_bad_ip_{i}", "") for i in (1, 2, 3)
    ] + [
        profile.get(f"dns_latency_{i}", "") for i in (1, 2, 3)
    ]
    servers = [s for s in servers if s]
    if not servers:
        return _degraded("dns_fail", "dns_bad_*", "no DNS targets configured")

    digs = 0
    if _HAS_DNSPYTHON:
        def _query() -> int:
            count = 0
            for _ in range(ctx.dns_passes):
                for rec in records:
                    for srv in servers:
                        r = dns.resolver.Resolver(configure=False)
                        r.nameservers = [srv]
                        r.lifetime = 2.0
                        try:
                            r.resolve(rec, "A")
                        except Exception:
                            pass  # expected — failure IS the simulation
                        count += 1
            return count
        digs = await asyncio.to_thread(_query)
        return _ok("dns_fail", f"{digs} queries to {len(servers)} servers", "dnspython")

    # Fallback: subprocess dig if present.
    if shutil.which("dig"):
        cmds = []
        for _ in range(ctx.dns_passes):
            for rec in records:
                for srv in servers:
                    cmds.append(["dig", f"@{srv}", rec, "+time=2", "+tries=1"])
        digs = await _run_batched(cmds, per_timeout=3.0, concurrency=4)
        return _ok("dns_fail", f"{digs} dig queries", "dig")

    return _degraded("dns_fail", "dnspython|dig", "install dnspython or dig")


# ── ping_test ─────────────────────────────────────────────────────────────────
async def sim_ping_test(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """ICMP ping ``ping_address`` with a random count (1–60) and size (1–1400)."""
    host = profile.get("ping_address", "").strip()
    if not host:
        return _degraded("ping_test", "ping_address", "no ping target configured")
    count = random.randint(1, min(ctx.ping_max_count, 60))
    size = random.randint(1, 1400)

    if _HAS_ICMPLIB:
        def _ping() -> PrimResult:
            try:
                icmplib.ping(host, count=count, size=size, timeout=2, privileged=False)
                return _ok("ping_test", f"pinged {host} x{count} size={size}", "icmplib")
            except Exception as exc:  # noqa: BLE001
                return _degraded("ping_test", "icmplib", str(exc))
        return await asyncio.to_thread(_ping)

    if shutil.which("ping"):
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(count), "-s", str(size), "-W", "2", host,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=count + 5)
        except asyncio.TimeoutError:
            proc.kill()
        return _ok("ping_test", f"pinged {host} x{count} size={size}", "ping")

    return _degraded("ping_test", "icmplib|ping", "install icmplib or ping")


# ── download ─────────────────────────────────────────────────────────────────
async def sim_download(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """HTTP-download a random URL from ``downloads.txt`` to a temp file."""
    urls = ctx.data_lines("downloads.txt")
    if not urls:
        return _degraded("download", "downloads.txt", "no download URLs configured")
    url = random.choice(urls)
    if not _HAS_HTTPX:
        return _degraded("download", "httpx", "install httpx")
    dest = ctx.work_dir / "cs-download.tmp"
    try:
        written = 0
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_raw():
                        f.write(chunk)
                        written += len(chunk)
                        if written >= ctx.download_max_bytes:
                            break
        return _ok("download", f"downloaded {written} bytes from {url}", "httpx")
    except Exception as exc:  # noqa: BLE001
        return _degraded("download", "httpx", str(exc))


# ── www_traffic ──────────────────────────────────────────────────────────────
async def sim_www_traffic(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """Fetch a random URL from ``websites.txt`` with a browser UA (traffic gen)."""
    urls = ctx.data_lines("websites.txt")
    if not urls:
        return _degraded("www_traffic", "websites.txt", "no website URLs configured")
    url = random.choice(urls)
    if not _HAS_HTTPX:
        return _degraded("www_traffic", "httpx", "install httpx")
    ua = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")
    try:
        read = 0
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": ua}) as client:
            async with client.stream("GET", url) as r:
                async for chunk in r.aiter_bytes():
                    read += len(chunk)
                    if read >= ctx.www_max_bytes:
                        break
        return _ok("www_traffic", f"fetched {read} bytes from {url} ({r.status_code})", "httpx")
    except Exception as exc:  # noqa: BLE001
        return _degraded("www_traffic", "httpx", str(exc))


# ── iperf ─────────────────────────────────────────────────────────────────────
async def sim_iperf(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """``iperf3 -c <iperf_server>`` across a small port set at ``iperf_bw``."""
    server = profile.get("iperf_server", "").strip()
    if not server:
        return _degraded("iperf", "iperf_server", "no iperf server configured")
    if not shutil.which("iperf3"):
        return _degraded("iperf", "iperf3", "install iperf3")
    bw = profile.get("iperf_bw", "1k")
    t = random.randint(1, max(1, ctx.iperf_max_time))
    base = 5201 + random.randint(0, 9)
    ports = [base, 443, 3260, 2049, 1194, 3389, 445, 80, 1433]
    ran = 0
    for port in ports:
        proc = await asyncio.create_subprocess_exec(
            "iperf3", "-c", server, "-p", str(port), "-b", bw, "-t", str(t),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=t + 3)
        except asyncio.TimeoutError:
            proc.kill()
        ran += 1
        if ran >= 2:  # bounded per call
            break
    return _ok("iperf", f"iperf3 -> {server} on {ran} port(s), -b {bw} -t {t}", "iperf3")


# ── nmcli-based WiFi sims (assoc_fail / ssidpw_fail / auth_fail) ──────────────
async def _nmcli(*args: str, timeout: float = 10.0) -> bool:
    if not shutil.which("nmcli"):
        return False
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
        return rc == 0
    except asyncio.TimeoutError:
        proc.kill()
        return False


def _effective_ssid(profile: Dict[str, str]) -> str:
    wsite = profile.get("wsite", "").strip()
    ssid = profile.get("ssid", "").strip()
    if profile.get("site_based_ssid", "").lower() == "on" and wsite:
        return f"{wsite}-{ssid}"
    return ssid


async def sim_assoc_fail(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """802.11 assoc failure: cycle the WLAN interface up/down repeatedly (nmcli)."""
    if not shutil.which("nmcli"):
        return _degraded("assoc_fail", "nmcli", "install NetworkManager (nmcli)")
    ssid = _effective_ssid(profile)
    for _ in range(ctx.wifi_iters):
        await _nmcli("connection", "up", ssid, timeout=5.0)
        await asyncio.sleep(1.0)
        await _nmcli("connection", "down", ssid, timeout=5.0)
    return _ok("assoc_fail", f"cycled {ssid} x{ctx.wifi_iters}", "nmcli")


async def sim_ssidpw_fail(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """Wrong-PSK auth failure: attempt to connect with ``<ssidpw>_fail`` (nmcli)."""
    if not shutil.which("nmcli"):
        return _degraded("ssidpw_fail", "nmcli", "install NetworkManager (nmcli)")
    ssid = _effective_ssid(profile)
    badpw = f"{profile.get('ssidpw', '')}_fail"
    # Drop any stale PSK connections first, like delete_matching_connections().
    await _nmcli("connection", "delete", ssid, timeout=5.0)
    for _ in range(ctx.wifi_iters):
        await _nmcli("device", "wifi", "connect", ssid, "password", badpw, timeout=5.0)
    return _ok("ssidpw_fail", f"wrong-PSK attempts to {ssid} x{ctx.wifi_iters}", "nmcli")


async def sim_auth_fail(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """802.1X/blocked-MAC style auth failure: toggle WLAN radio + interface (nmcli)."""
    if not shutil.which("nmcli"):
        return _degraded("auth_fail", "nmcli", "install NetworkManager (nmcli)")
    for _ in range(ctx.wifi_iters):
        await _nmcli("radio", "wifi", "off", timeout=5.0)
        await asyncio.sleep(0.5)
        await _nmcli("radio", "wifi", "on", timeout=5.0)
        await asyncio.sleep(0.5)
    return _ok("auth_fail", f"toggled wifi radio x{ctx.wifi_iters}", "nmcli")


# ── port_flap (wired) ─────────────────────────────────────────────────────────
#
# These two run via ``create_subprocess_exec`` + ``communicate`` with a timeout
# so a hung ``ip`` (which previously had NO timeout) can't stall the cs spoke's
# shared event loop and trigger a hub "Request Timeout". Sync ``subprocess.run``
# without a timeout was an unbounded blocker on the loop.
async def _run_capture(argv, timeout: float = 3.0):
    """Async capture-stdout helper. Returns ``(stdout_text, returncode)``;
    ``("", None)`` on any failure or timeout (process is killed + reaped)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return "", None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        return text, proc.returncode
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return "", None


async def _find_wired_adapter() -> Optional[str]:
    """Best-effort wired interface name (``enp*/eno*/eth*``) via ``ip -br a``."""
    if not shutil.which("ip"):
        return None
    out, _ = await _run_capture(["ip", "-br", "a"], timeout=3.0)
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith(("enp", "eno", "eth")):
            return name
    return None


async def _is_mgmt_iface(iface: str) -> bool:
    """Management-IP guard: never bounce the 169.253.x mgmt interface."""
    if not iface or not shutil.which("ip"):
        return False
    out, _ = await _run_capture(["ip", "-4", "addr", "show", "dev", iface], timeout=3.0)
    return "169.253." in out


async def sim_port_flap(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """Wired port link flap: bounce the wired interface up/down (ip link), mgmt-guarded."""
    if not shutil.which("ip"):
        return _degraded("port_flap", "ip", "install iproute2 (ip)")
    iface = await _find_wired_adapter()
    if not iface:
        return _degraded("port_flap", "eth-iface", "no wired interface found")
    if await _is_mgmt_iface(iface):
        return _ok("port_flap", f"skipped — {iface} is the mgmt interface", "ip")
    for _ in range(ctx.port_flap_iters):
        p = await asyncio.create_subprocess_exec(
            "sudo", "ip", "link", "set", "dev", iface, "down",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=5.0)
        await asyncio.sleep(0.5)
        p = await asyncio.create_subprocess_exec(
            "sudo", "ip", "link", "set", "dev", iface, "up",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=5.0)
        await asyncio.sleep(0.5)
    return _ok("port_flap", f"flapped {iface} x{ctx.port_flap_iters}", "ip")


# ── dhcp_fail (light) ──────────────────────────────────────────────────────────
async def sim_dhcp_fail(profile: Dict[str, str], ctx: SimCtx) -> PrimResult:
    """Release the DHCP lease and don't renew (``dhclient -r``); best-effort."""
    if not shutil.which("dhclient"):
        return _degraded("dhcp_fail", "dhclient", "install isc-dhcp-client")
    proc = await asyncio.create_subprocess_exec(
        "sudo", "dhclient", "-r",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
    return _ok("dhcp_fail", "released DHCP lease", "dhclient")


# ── helpers ───────────────────────────────────────────────────────────────────
async def _run_batched(cmds: list[list[str]], per_timeout: float, concurrency: int) -> int:
    """Run a list of command vectors concurrently (bounded), counting completions."""
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def _one(cmd: list[str]) -> None:
        nonlocal done
        async with sem:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=per_timeout)
            except asyncio.TimeoutError:
                proc.kill()
            done += 1

    await asyncio.gather(*(_one(c) for c in cmds))
    return done


# Flag → primitive coroutine. sim_phy/dhcp_fail/kill_switch are handled by the
# engine directly; dhcp_fail is included here as a best-effort primitive.
PRIMITIVES: Dict[str, PrimFn] = {
    "dns_fail": sim_dns_fail,
    "ping_test": sim_ping_test,
    "download": sim_download,
    "www_traffic": sim_www_traffic,
    "iperf": sim_iperf,
    "assoc_fail": sim_assoc_fail,
    "ssidpw_fail": sim_ssidpw_fail,
    "auth_fail": sim_auth_fail,
    "port_flap": sim_port_flap,
    "dhcp_fail": sim_dhcp_fail,
}

# Flags the engine treats as "run" sims (dispatched to PRIMITIVES when "on").
SIM_FLAGS = tuple(PRIMITIVES.keys())