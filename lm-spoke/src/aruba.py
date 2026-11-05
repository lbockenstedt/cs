"""Aruba Central integration helpers for tenant-aware polling and discovery.

``ArubaClient`` wraps the token and request logic needed by Hub background
workers. It supports the legacy/classic Aruba Central token flow, the newer
``new_central`` client-credentials flow, alert and insight polling across the
supported monitoring endpoints, and MSP customer discovery for converting Aruba
Central tenants into Hub tenants.
"""
# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Hashing and JSON for config hashes and payloads.
import hashlib
# Address checks for cluster URL validation.
import ipaddress
import json
# Logging, DNS lookups and timing.
import logging
import socket
import time
# Dataclass for findings.
from dataclasses import dataclass, field
# Type hints only.
from typing import Any, Optional
# URL parsing.
from urllib.parse import urlparse

# Async HTTP client.
import httpx

# Logger for this module.
logger = logging.getLogger(__name__)

# Default token endpoint for new Central.
_NEW_CENTRAL_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

# Module-level caches for new_central global endpoints.
# Caching means 9 sites share 1 API call instead of 27 calls per poll cycle.
# Key: config_hash, Value: (timestamp_float, payload_dict_or_list)
_insights_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_INSIGHTS_CACHE_TTL = 900  # 15 minutes

_alerts_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_ALERTS_CACHE_TTL = 300   # 5 minutes

_sites_health_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_devices_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_nc_clients_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_NC_GLOBAL_CACHE_TTL = 270  # 4.5 min — just under the 5-min poll interval

# Module-level OAuth token cache keyed by config hash (like the data caches
# above). The *_from_config webui helpers build a FRESH ArubaClient per call; an
# instance-scoped token cache meant each one did a new token POST. Sharing the
# cache across instances with the same config reuses the still-valid token.
# Value: {access_token, expires_at, [refresh_token]} — expiry handling unchanged.
_token_cache: dict[str, dict[str, Any]] = {}
# GreenLake token URL and known Central API gateway host suffixes.
_GLP_TOKEN_URL_TEMPLATE = "https://global.api.greenlake.hpe.com/authorization/v2/oauth2/{workspace_id}/token"
_KNOWN_CENTRAL_GATEWAY_SUFFIXES = (".api.central.arubanetworks.com", ".api.central.arubanetworks.com.cn")

# Default checks offered for new Central tenants.
DEFAULT_NEW_CENTRAL_MONITORED_CHECKS: tuple[dict[str, str], ...] = (
    {"type": "alert", "id": "SITE_HEALTH", "name": "Site Health Score (0–100)"},
    {"type": "alert", "id": "AP_DOWN", "name": "APs Down / Offline"},
    {"type": "alert", "id": "SWITCH_DOWN", "name": "Switches Down / Offline"},
    {"type": "alert", "id": "GATEWAY_DOWN", "name": "Gateways Down / Offline"},
    {"type": "alert", "id": "CLIENT_COUNT", "name": "Connected Client Count"},
)
# Default hardware checks and the device type each one covers.
DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS: tuple[dict[str, str], ...] = (
    {"id": "AP_DOWN", "name": "APs Down / Offline", "device_type": "ap"},
    {"id": "SWITCH_DOWN", "name": "Switches Down / Offline", "device_type": "switch"},
    {"id": "GATEWAY_DOWN", "name": "Gateways Down / Offline", "device_type": "gateway"},
)
# Friendly names for classic Central alert types.
KNOWN_CLASSIC_ALERT_TYPES: dict[str, str] = {
    "AP_DOWN": "AP Down",
    "AP_UP": "AP Up",
    "ACCESS_POINT_DOWN": "Access Point Down",
    "CLIENT_ASSOCIATION_FAILURE": "Client Association Failure",
    "CLIENT_DHCP_FAILURE": "Client DHCP Failure",
    "CLIENT_DISCONNECTED": "Client Disconnected",
    "DHCP_POOL_EXHAUSTED": "DHCP Pool Exhausted",
    "IDS_AP_SPOOFED": "IDS AP Spoofed",
    "PORTAL_DOWN": "Portal Down",
    "RADIO_INTERFERENCE": "Radio Interference",
    "ROGUE_AP_DETECTED": "Rogue AP Detected",
    "SWITCH_DOWN": "Switch Down",
    "SWITCH_PORT_DOWN": "Switch Port Down",
    "TUNNEL_DOWN": "Tunnel Down",
    "UPLINK_FAILURE": "Uplink Failure",
    "VPN_TUNNEL_DOWN": "VPN Tunnel Down",
    "WIRELESS_CLIENT_ROAM": "Wireless Client Roam",
    "WIRELESS_INTERFERENCE": "Wireless Interference",
}
# Friendly names for classic insight categories.
KNOWN_CLASSIC_INSIGHT_CATEGORIES: dict[str, str] = {
    "CONNECTIVITY": "Connectivity",
    "PERFORMANCE": "Performance",
    "RELIABILITY": "Reliability",
    "SECURITY": "Security",
}


def validate_cluster_url(cluster_url: str) -> str:
    """Validate and normalise an Aruba Central cluster URL.
    Requires https and a hostname that resolves to a public address; logs a
    warning if the host is not a known Central API gateway.
    """
    # Normalise and require a value.
    normalized = str(cluster_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("cluster_url is required")

    # Require https and a hostname.
    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise ValueError("cluster_url must use https")
    if not parsed.hostname:
        raise ValueError("cluster_url must include a hostname")

    # Resolve the hostname.
    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"cluster_url hostname could not be resolved: {exc}") from exc

    # Reject private, loopback and link-local addresses.
    for _, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(
                f"cluster_url resolves to disallowed address {ip}"
            )

    # Warn about unknown gateway hosts.
    hostname = parsed.hostname.casefold()
    if not hostname.endswith(_KNOWN_CENTRAL_GATEWAY_SUFFIXES):
        logger.warning(
            "Aruba cluster_url host %s does not match known Central API gateway patterns",
            parsed.hostname,
        )

    return normalized


@dataclass
class ArubaFinding:
    """One Aruba alert or insight result for a site."""
    site_name: str
    check_name: str
    status: str  # "red" | "yellow" | "green"
    source: str  # "alert" | "insight"
    raw: dict[str, Any] = field(default_factory=dict)


# Ordered candidate keys for a client's reported OS. Central spells this
# differently across API generations and Mist differs again, and the WebUI showed
# "Unknown" for every client because only two spellings were tried while the
# Central UI clearly had the value. Model/manufacturer are LAST-RESORT: the UI
# column reads "OS/Model", so a device that reports no OS but does report a model
# is still more useful than "Unknown".
_CLIENT_OS_KEYS = (
    "osType", "os_type", "os",
    "operatingSystem", "operating_system",
    "osInfo", "os_info", "osName", "os_name",
    "clientOs", "client_os",
    "deviceType", "device_type",
    "deviceModel", "device_model", "model",
    "deviceFamily", "device_family",
)


# Sources whose missing-OS warning has already been logged.
_os_miss_logged: set = set()


def _log_os_miss(tag: str, rows: list, raw: list) -> None:
    """If EVERY client resolved to "—", log the raw field names once per source.

    The first cut of this feature tried two spellings and every client rendered
    "Unknown" while the Central UI plainly showed an OS — with nothing in the log
    to say which key it should have read. This makes the next spelling mismatch
    self-diagnosing instead of a guessing game."""
    # Only log once per source, and only when there are rows.
    try:
        if not rows or tag in _os_miss_logged:
            return
        # Stop if any client has a real OS.
        if any(str(r.get("os") or "").strip() not in ("", "\u2014") for r in rows):
            return
        # Log the field names from a sample record.
        _os_miss_logged.add(tag)
        sample = raw[0] if raw else {}
        logger.warning(
            "client OS unresolved for ALL %d client(s) [%s] — none of %s present. "
            "Available fields on a sample record: %s",
            len(rows), tag, list(_CLIENT_OS_KEYS),
            sorted(sample.keys()) if isinstance(sample, dict) else type(sample).__name__)
    # Never fail the pull.
    except Exception:  # noqa: BLE001 — diagnostics must never break the pull
        pass


def _client_os(item: dict[str, Any]) -> str:
    """First non-empty OS-ish field on a Central/Mist client record, else "—".

    Values like "unknown"/"n/a" that a controller uses to mean "not reported" are
    treated as absent so they don't outrank a later, real field."""
    # Non-dict records have no OS.
    if not isinstance(item, dict):
        return "\u2014"
    # Try each candidate key in order.
    for k in _CLIENT_OS_KEYS:
        v = item.get(k)
        # Unwrap nested values.
        if isinstance(v, dict):                       # e.g. {"name": "Windows"}
            v = v.get("name") or v.get("type") or v.get("value")
        if v is None:
            continue
        # Skip empty or "not reported" values.
        text = str(v).strip()
        if not text or text.lower() in ("unknown", "n/a", "na", "none", "-", "\u2014"):
            continue
        return text
    return "\u2014"


class ArubaClient:
    """Aruba Central API client. One instance per tenant config."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Store the config and work out the cluster URL, API version and config hash."""
        # Copy the config and read the main settings.
        self.config = dict(config)
        self.cluster_url = (self.config.get("cluster_url") or "").rstrip("/")
        self.api_version = (self.config.get("api_version") or "classic").strip()
        # Short hash used for shared caches and log tags.
        self._config_hash = hashlib.md5(
            json.dumps(self.config, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]

    def is_configured(self) -> bool:
        """Return ``True`` when the minimum Aruba endpoint configuration is present."""
        return bool(self.cluster_url)

    def _token_state(self) -> dict[str, Any]:
        """Return the shared token state for this config."""
        return _token_cache.setdefault(self._config_hash, {})

    def _new_central_token_url(self) -> str:
        """Return the new Central token URL (GreenLake when a workspace id is set)."""
        # Use the GreenLake URL when a workspace id is set.
        workspace_id = str(self.config.get("workspace_id") or "").strip()
        if workspace_id:
            return _GLP_TOKEN_URL_TEMPLATE.format(workspace_id=workspace_id)
        return _NEW_CENTRAL_TOKEN_URL

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, fetching or refreshing one if needed."""
        # Reuse a cached token that is still valid for at least a minute.
        now = time.time()
        token_state = self._token_state()
        if token_state.get("access_token") and token_state.get("expires_at", 0) > now + 60:
            return token_state["access_token"]

        # New Central flow.
        if self.api_version == "new_central":
            # Client credentials win: they mint a fresh token on every expiry.
            # A pasted static access token expires (~2h) and can't be renewed,
            # so it's only used when no client_id/client_secret are configured.
            has_client_creds = bool(self.config.get("client_id") and self.config.get("client_secret"))
            static_token = str(self.config.get("access_token") or "").strip()
            if static_token and not has_client_creds:
                token_state.clear()
                token_state.update({"access_token": static_token, "expires_at": now + 7200})
                return static_token

            # Request a token with client credentials.
            workspace_id = str(self.config.get("workspace_id") or "").strip()
            resp = await client.post(
                self._new_central_token_url(),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.get("client_id", ""),
                    "client_secret": self.config.get("client_secret", ""),
                },
                timeout=15,
            )
            # Check the reply and cache the token.
            resp.raise_for_status()
            payload = resp.json()
            if "access_token" not in payload:
                raise ValueError(f"Token endpoint returned no access_token. Response: {json.dumps(payload)[:300]}")
            token_state.clear()
            token_state.update(
                {
                    "access_token": payload["access_token"],
                    "expires_at": now + int(payload.get("expires_in", 900 if workspace_id else 7200)),
                }
            )
            return token_state["access_token"]

        # Classic flow: read the stored tokens.
        access_token = self.config.get("access_token")
        refresh_token = self.config.get("refresh_token")

        # A static token without a refresh token is used as-is.
        if access_token and not refresh_token:
            token_state.clear()
            token_state.update({"access_token": access_token, "expires_at": now + 3600})
            return access_token

        # Need a refresh token from here on.
        if not refresh_token:
            raise ValueError("Aruba Central credentials incomplete — need access_token or refresh_token")

        # Build the refresh request.
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.config.get("client_id", ""),
            "client_secret": self.config.get("client_secret", ""),
        }
        if self.config.get("customer_id"):
            data["customer_id"] = self.config["customer_id"]

        # Refresh the token and cache it.
        resp = await client.post(f"{self.cluster_url}/oauth2/token", data=data, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        token_state.clear()
        token_state.update(
            {
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token", refresh_token),
                "expires_at": now + int(payload.get("expires_in", 3600)),
            }
        )
        return token_state["access_token"]

    def _headers(self, token: str) -> dict[str, str]:
        """Build request headers with the bearer token and optional customer id."""
        # Bearer token, plus customer id when set.
        headers = {"Authorization": f"Bearer {token}"}
        if self.config.get("customer_id"):
            headers["X-Customer-ID"] = str(self.config["customer_id"])
        return headers

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` on the cluster and return the JSON body."""
        # Get a token and send the request.
        token = await self._ensure_token(client)
        resp = await client.get(
            f"{self.cluster_url}{path}",
            headers=self._headers(token),
            params=params or {},
            timeout=30,
        )
        # Raise on HTTP errors and return the JSON.
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _finding_status(value: Any) -> str:
        """Map an alert severity or status to red, yellow or green."""
        # Normalise the value, then map known words.
        sev = str(value or "").strip().lower()
        if sev in {"critical", "major", "red", "open", "error"}:
            return "red"
        if sev in {"clear", "closed", "normal", "ok", "green", "resolved"}:
            return "green"
        return "yellow"

    async def poll_alerts_and_insights(self, site_filter: Optional[str] = None) -> list[ArubaFinding]:
        """Collect Aruba alerts and insights across the supported API variants for a tenant."""
        # Nothing to poll without a config.
        if not self.is_configured():
            return []

        # Collect findings.
        findings: list[ArubaFinding] = []
        async with httpx.AsyncClient(timeout=30) as client:
            # New Central: report site health only.
            if self.api_version == "new_central":
                try:
                    # Turn each site's health into a finding.
                    data = await self._get(client, "/network-monitoring/v1alpha1/sites-health")
                    for item in data.get("items") or []:
                        site = (item.get("name") or item.get("siteName") or item.get("site_name") or "unknown").strip() or "unknown"
                        # Apply the site filter.
                        if site_filter and site.lower() != site_filter.lower():
                            continue
                        # Use the "Good" health group value, else the health score.
                        good_pct = next(
                            (g.get("value", 0) for g in (item.get("health") or {}).get("groups", []) if g.get("name") == "Good"),
                            item.get("healthScore", item.get("health_score", 100)),
                        )
                        findings.append(
                            ArubaFinding(
                                site_name=site,
                                check_name="SITE_HEALTH",
                                status="green" if int(good_pct or 0) >= 80 else "yellow",
                                source="alert",
                                raw=item,
                            )
                        )
                # Log failures.
                except Exception as exc:
                    logger.warning("Aruba sites-health fetch failed [%s]: %s", self._config_hash, exc)
                # For new_central: insights are fetched on-demand via _new_central_insights()
                # (which has a 15-min cache). Skip the aiops endpoints here to avoid 429s
                # from background polling burning the rate-limit quota.
                return findings

            # Classic Central: build request parameters.
            params: dict[str, Any] = {"limit": 1000}
            if site_filter:
                params["site"] = site_filter

            # Fetch alerts from the first endpoint that exists.
            try:
                alerts_payload = None
                for alerts_path in ("/monitoring/v1/alerts", "/monitoring/v2/alerts", "/aiops/v2/alerts"):
                    try:
                        alerts_payload = await self._get(client, alerts_path, params=params)
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                # Turn each alert into a finding.
                for alert in (alerts_payload or {}).get("alerts") or (alerts_payload or {}).get("items") or []:
                    site = (alert.get("site_name") or alert.get("site") or alert.get("group") or "unknown").strip() or "unknown"
                    name = (
                        alert.get("name")
                        or alert.get("alert_name")
                        or alert.get("rule")
                        or alert.get("alert_type")
                        or "alert"
                    )
                    status = self._finding_status(alert.get("severity") or alert.get("status"))
                    findings.append(ArubaFinding(site_name=site, check_name=str(name), status=status, source="alert", raw=alert))
            # Log failures.
            except Exception as exc:
                logger.warning("Aruba alerts fetch failed [%s]: %s", self._config_hash, exc)

            # Insight parameters use site_name instead of site.
            insight_params = dict(params)
            if site_filter:
                insight_params["site_name"] = site_filter
                insight_params.pop("site", None)
            # Fetch insights from the first endpoint that exists.
            try:
                insights_payload = None
                for insights_path in ("/aiops/v1/insights", "/aiops/v2/insights"):
                    try:
                        insights_payload = await self._get(client, insights_path, params=insight_params)
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                # Turn each insight into a finding.
                for insight in (insights_payload or {}).get("insights") or (insights_payload or {}).get("items") or []:
                    site = (insight.get("site_name") or insight.get("site") or insight.get("group") or "unknown").strip() or "unknown"
                    name = (
                        insight.get("name")
                        or insight.get("insight_name")
                        or insight.get("rule")
                        or insight.get("category")
                        or "insight"
                    )
                    status = self._finding_status(insight.get("severity") or insight.get("status"))
                    findings.append(ArubaFinding(site_name=site, check_name=str(name), status=status, source="insight", raw=insight))
            # Log failures.
            except Exception as exc:
                logger.warning("Aruba insights fetch failed [%s]: %s", self._config_hash, exc)

        return findings

    async def poll_site_data(
        self,
        site: str,
        hw_check_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Collect per-site Aruba Central health, counts, and hardware device names."""
        # Empty data without a config.
        if not self.is_configured():
            return {
                "site_health": None,
                "wireless_clients": 0,
                "client_count": 0,
                "alert_type_counts": {},
                "insight_cat_counts": {},
                "hw_devices": {},
            }

        # Clean the hardware check ids and set up counters.
        hw_check_ids = {str(check_id).strip() for check_id in (hw_check_ids or set()) if str(check_id).strip()}
        site_health: int | None = None
        wireless_clients = 0
        wired_clients = 0
        alert_type_counts: dict[str, int] = {}
        insight_cat_counts: dict[str, int] = {}
        hw_devices: dict[str, dict[str, int]] = {}

        if self.api_version == "new_central":
            # Use cached global fetchers — 9 sites share 1 API call per endpoint
            site_id: str | None = None
            # Find this site's health and id.
            for item in await self._nc_sites_health():
                site_name = (item.get("name") or item.get("siteName") or item.get("site_name") or "").strip()
                if site_name.lower() != site.lower():
                    continue
                site_id = str(item.get("id") or item.get("siteId") or item.get("site_id") or "").strip() or None
                good_pct = next(
                    (g.get("value", 0) for g in (item.get("health") or {}).get("groups", []) if g.get("name") == "Good"),
                    item.get("healthScore", item.get("health_score", 0)),
                )
                site_health = int(good_pct or 0)
                break

            # Per-cycle site index: O(1) lookup instead of rescanning each full
            # list per site. Bucketing mirrors the original per-site filters.
            idx = await self._nc_site_index()

            # Count devices that are down, by alert type.
            DEVICE_ALERT = {"ACCESS_POINT": "AP_DOWN", "SWITCH": "SWITCH_DOWN", "GATEWAY": "GATEWAY_DOWN"}
            devices_for_site = (idx["dev_by_site"].get(site_id, []) + idx["dev_global"]
                                if site_id else idx["devices"])
            for device in devices_for_site:
                device_type = str(device.get("deviceType") or "").upper()
                status = str(device.get("status") or "").upper()
                # Skip devices that are up.
                if status in {"UP", "ONLINE"}:
                    continue
                # Skip unknown device types.
                alert_id = DEVICE_ALERT.get(device_type)
                if not alert_id:
                    continue
                # Count it, and record the device name for hardware checks.
                alert_type_counts[alert_id] = alert_type_counts.get(alert_id, 0) + 1
                if not hw_check_ids or alert_id in hw_check_ids:
                    device_name = (
                        device.get("deviceName") or device.get("name")
                        or device.get("serialNumber") or device.get("serial") or ""
                    ).strip()
                    if device_name:
                        hw_devices.setdefault(alert_id, {})[device_name] = hw_devices.setdefault(alert_id, {}).get(device_name, 0) + 1

            # Count all clients (wired + wireless) for the site from the indexed
            # clients list (all clients when the site has no resolved id).
            clients_for_site = idx["cli_by_site"].get(site_id, []) if site_id else idx["clients"]
            # Split clients into wired and wireless.
            for cl in clients_for_site:
                conn_type = str(cl.get("clientConnectionType") or cl.get("connection_type") or "").lower()
                if conn_type == "wired":
                    wired_clients += 1
                else:
                    wireless_clients += 1

            # Count insights per site so MONITORED insight checks (enrolled from
            # the Central -> Insights tab) evaluate on the dashboard. Keyed
            # name||category to match the WebUI monitored-check id. Insights carry
            # a site NAME (or "All Sites" for global); _new_central_insights is
            # cached so this adds no extra API call within the TTL.
            for ins in (idx["ins_by_site"].get(site.lower(), []) + idx["ins_global"]):
                cat = str(ins.get("name") or ins.get("category") or "").strip()
                if cat:
                    insight_cat_counts[cat] = insight_cat_counts.get(cat, 0) + 1

            # Count network-notifications alerts by name so MONITORED alert checks
            # (Central -> Alerts Monitor button) evaluate on the dashboard. Alerts
            # aren't reliably site-scoped, so an active alert counts for EVERY
            # monitored site; key name||category = the WebUI monitored-check id.
            # Count an alert for THIS site when it is pinned here (name match) or
            # has no site ("-" = global -> every site). Enables per-site alert
            # monitoring; the monitored check id is name||category.
            for al in (idx["al_by_site"].get(site.lower(), []) + idx["al_global"]):
                nm = str(al.get("name") or al.get("category") or "").strip()
                if nm:
                    alert_type_counts[nm] = alert_type_counts.get(nm, 0) + 1

            # Return the new Central results.
            return {
                "site_health": site_health,
                "wireless_clients": wireless_clients,
                "wired_clients": wired_clients,
                "client_count": wireless_clients + wired_clients,
                "alert_type_counts": alert_type_counts,
                "insight_cat_counts": insight_cat_counts,
                "hw_devices": hw_devices,
            }

        # Classic Central.
        async with httpx.AsyncClient(timeout=30) as client:

            # Fetch alerts from the first endpoint that exists.
            params: dict[str, Any] = {"site": site, "limit": 1000}
            try:
                alerts_payload = None
                for alerts_path in ("/monitoring/v1/alerts", "/monitoring/v2/alerts"):
                    try:
                        alerts_payload = await self._get(client, alerts_path, params=params)
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                # Count alerts by type and record device names for hardware checks.
                for alert in (alerts_payload or {}).get("alerts") or (alerts_payload or {}).get("items") or []:
                    alert_type = str(alert.get("alert_type") or alert.get("type") or "").strip()
                    if not alert_type:
                        continue
                    alert_type_counts[alert_type] = alert_type_counts.get(alert_type, 0) + 1
                    if hw_check_ids and alert_type in hw_check_ids:
                        device_name = (
                            alert.get("device_name")
                            or alert.get("hostname")
                            or alert.get("name")
                            or ""
                        ).strip()
                        if device_name:
                            hw_devices.setdefault(alert_type, {})[device_name] = hw_devices.setdefault(alert_type, {}).get(device_name, 0) + 1
            # Log failures.
            except Exception as exc:
                logger.warning("Aruba alerts fetch failed [%s:%s]: %s", self._config_hash, site, exc)

            # Fetch insights from the first endpoint that exists.
            insight_params = {"site_name": site, "limit": 1000}
            try:
                insights_payload = None
                for insights_path in ("/aiops/v1/insights", "/aiops/v2/insights"):
                    try:
                        insights_payload = await self._get(client, insights_path, params=insight_params)
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                # Count insights by category.
                for insight in (insights_payload or {}).get("insights") or (insights_payload or {}).get("items") or []:
                    category = str(insight.get("category") or insight.get("type") or "").strip()
                    if category:
                        insight_cat_counts[category] = insight_cat_counts.get(category, 0) + 1
            # Log failures.
            except Exception as exc:
                logger.warning("Aruba insights fetch failed [%s:%s]: %s", self._config_hash, site, exc)

            # Get the wireless client total, trying each endpoint and site parameter name.
            fetched_wireless = False
            for clients_path in ("/monitoring/v2/clients/wireless", "/monitoring/v1/clients/wireless"):
                for site_param in ("site", "site_name"):
                    try:
                        payload = await self._get(client, clients_path, params={site_param: site, "limit": 1})
                        wireless_clients = int(payload.get("total") or payload.get("count") or 0)
                        fetched_wireless = True
                        break
                    # A 404 means try the next option.
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                    # Other errors: log and stop trying.
                    except Exception as exc:
                        logger.warning("Aruba wireless clients fetch failed [%s:%s]: %s", self._config_hash, site, exc)
                        break
                # Stop once we have a total.
                if fetched_wireless:
                    break

            # Same for wired clients.
            fetched_wired = False
            for clients_path in ("/monitoring/v2/clients/wired", "/monitoring/v1/clients/wired"):
                for site_param in ("site", "site_name"):
                    try:
                        payload = await self._get(client, clients_path, params={site_param: site, "limit": 1})
                        wired_clients = int(payload.get("total") or payload.get("count") or 0)
                        fetched_wired = True
                        break
                    # A 404 means try the next option.
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue
                        raise
                    # Other errors: log and stop trying.
                    except Exception as exc:
                        logger.warning("Aruba wired clients fetch failed [%s:%s]: %s", self._config_hash, site, exc)
                        break
                # Stop once we have a total.
                if fetched_wired:
                    break

        # Return the classic results.
        return {
            "site_health": site_health,
            "wireless_clients": wireless_clients,
            "wired_clients": wired_clients,
            "client_count": wireless_clients + wired_clients,
            "alert_type_counts": alert_type_counts,
            "insight_cat_counts": insight_cat_counts,
            "hw_devices": hw_devices,
        }

    async def list_sites(self) -> list[dict[str, Any]]:
        """Return normalized Aruba Central sites for hub auto-discovery."""
        # Nothing to list without a config.
        if not self.is_configured():
            return []

        # Sites keyed by lower-case name.
        sites: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=30) as client:
            # New Central: read sites from the health endpoint.
            if self.api_version == "new_central":
                data = await self._get(client, "/network-monitoring/v1alpha1/sites-health")
                for item in data.get("items") or []:
                    site_name = (item.get("name") or item.get("siteName") or item.get("site_name") or "").strip()
                    # Skip sites with no name.
                    if not site_name:
                        continue
                    key = site_name.casefold()
                    sites[key] = {
                        "name": site_name,
                        "site_id": item.get("id") or item.get("siteId") or item.get("site_id") or "",
                        "health_score": next(
                            (g.get("value", 0) for g in (item.get("health") or {}).get("groups", []) if g.get("name") == "Good"),
                            item.get("healthScore", item.get("health_score")),
                        ),
                        "wireless_clients": (item.get("clients") or {}).get("count") or item.get("clientCount") or item.get("client_count"),
                    }
                # Sorted by name.
                return sorted(sites.values(), key=lambda item: item["name"].casefold())

        # Classic Central: find site names from alerts and insights.
        try:
            findings = await self.poll_alerts_and_insights()
        # Log failures.
        except Exception as exc:
            logger.warning("Aruba classic site discovery failed [%s]: %s", self._config_hash, exc)
            findings = []
        # Collect unique site names.
        for finding in findings:
            site_name = str(finding.site_name or "").strip()
            if not site_name:
                continue
            sites.setdefault(site_name.casefold(), {"name": site_name})
        return sorted(sites.values(), key=lambda item: item["name"].casefold())

    async def list_clients(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return normalized wireless clients from Central API."""
        # Nothing to list without a config.
        if not self.is_configured():
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            # New Central: read and normalise clients.
            if self.api_version == "new_central":
                try:
                    data = await self._get(client, "/network-monitoring/v1alpha1/clients", params={"limit": limit})
                    _items = data.get("items") or []
                    _rows = [
                        {
                            "mac": item.get("macAddress") or item.get("mac_address") or item.get("mac") or "—",
                            "ip": item.get("ipv4") or item.get("ipv4Address") or item.get("ip_address") or item.get("ip") or "—",
                            "hostname": item.get("name") or item.get("clientName") or item.get("deviceName") or item.get("hostname") or "—",
                            "username": item.get("userName") or item.get("username") or item.get("associatedUser") or item.get("auth_username") or "",
                            "site": item.get("siteName") or item.get("site_name") or "—",
                            "ap": item.get("associatedDeviceName") or item.get("associatedDevice") or item.get("ap_name") or "—",
                            "ssid": item.get("ssid") or item.get("essid") or "—",
                            "status": item.get("status") or "—",
                            "os": _client_os(item),
                            "vlan": str(item.get("vlan") or "—"),
                            "connection_type": item.get("clientConnectionType") or "",
                        }
                        for item in _items
                    ]
                    # Log once if no OS was found, then return.
                    _log_os_miss("new_central", _rows, _items)
                    return _rows
                # Log failures.
                except Exception as exc:
                    logger.warning("list_clients new_central failed [%s]: %s", self._config_hash, exc)
                    return []
            # Classic Central: try each clients endpoint.
            for path in ("/monitoring/v2/clients/wireless", "/monitoring/v1/clients/wireless"):
                try:
                    data = await self._get(client, path, params={"limit": limit})
                    _items = data.get("clients") or data.get("items") or []
                    _rows = [
                        {
                            "mac": item.get("macaddr") or item.get("mac_address") or item.get("mac") or "—",
                            "ip": item.get("ip_address") or item.get("ip") or "—",
                            "hostname": item.get("name") or item.get("hostname") or "—",
                            "username": item.get("username") or item.get("userName") or "",
                            "site": item.get("site") or item.get("site_name") or "—",
                            "ap": item.get("associated_device_name") or item.get("ap_name") or "—",
                            "ssid": item.get("ssid") or "—",
                            "status": item.get("status") or "connected",
                            "os": _client_os(item),
                            "vlan": str(item.get("vlan_id") or item.get("vlan") or "—"),
                        }
                        for item in _items
                    ]
                    # Log once if no OS was found, then return.
                    _log_os_miss("classic:" + path, _rows, _items)
                    return _rows
                # A 404 means try the next endpoint.
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        continue
                    logger.warning("list_clients classic failed [%s] %s: %s", self._config_hash, path, exc)
                    return []
                # Other errors: log and give up.
                except Exception as exc:
                    logger.warning("list_clients classic failed [%s] %s: %s", self._config_hash, path, exc)
                    return []
            # No endpoint worked.
            return []

    async def _new_central_device_alerts(self) -> list[dict[str, Any]]:
        """Fetch device-level alerts (down APs/switches/gateways) for new_central browse view."""
        # Alert name for each device type.
        DEVICE_TYPE_ALERT = {"ACCESS_POINT": "AP Down", "SWITCH": "Switch Down", "GATEWAY": "Gateway Down"}
        alerts: list[dict[str, Any]] = []
        # Turn each device that is not up into an alert.
        try:
            devices = await self._nc_devices()
            for device in devices:
                status = str(device.get("status") or "").upper()
                # Skip devices that are up or have no status.
                if status in {"UP", "ONLINE", ""}:
                    continue
                # Work out the alert name, site and device name.
                device_type = str(device.get("deviceType") or device.get("type") or "").upper()
                alert_name = DEVICE_TYPE_ALERT.get(device_type) or f"{device_type} Down"
                site = (
                    device.get("siteName") or device.get("site_name") or device.get("site") or "—"
                ).strip() or "—"
                device_name = (
                    device.get("deviceName") or device.get("name") or device.get("serialNumber") or device.get("id") or "—"
                ).strip()
                alerts.append({
                    "name": alert_name,
                    "site": site,
                    "severity": "error",
                    "detail": device_name,
                    "device_name": device_name,
                    "status": status,
                    "ts": None,
                })
        # Log failures.
        except Exception as exc:
            logger.warning("new_central device alerts fetch failed [%s]: %s", self._config_hash, exc)
        return alerts

    # ── new_central cached global fetchers ────────────────────────────────────

    async def _nc_site_index(self) -> dict[str, Any]:
        """Bucket the cached global device/client/insight/alert lists by site so
        ``poll_site_data`` looks each site up in O(1) instead of rescanning every
        full list per site (was O(sites×items)).

        Memoized on the instance and keyed by the identity of the four cached
        source lists (held alive by the module-level caches). Within a poll cycle
        the cached fetchers return the SAME list objects, so every site reuses one
        index; when a module cache expires and refetches, a new list object is
        returned → the key changes → the index is rebuilt. This ties freshness to
        the existing cache TTLs and introduces no extra staleness.

        The bucketing mirrors ``poll_site_data``'s original filters exactly:
          * devices — matched by siteId; a device with NO site id counted for
            every site (``dev_global``); when a site has no resolved id, ALL
            devices are used (``devices``).
          * clients — matched by siteId; when a site has no id, ALL clients used.
          * insights — matched by site NAME; empty or "all sites" → every site.
          * alerts — matched by site NAME; empty or "-"/"—" → every site.
        """
        # Load the cached global lists.
        devices = await self._nc_devices()
        clients = await self._nc_clients()
        insights = await self._new_central_insights()
        alerts = await self._new_central_alerts()
        # Reuse the index if the lists have not changed.
        key = (id(devices), id(clients), id(insights), id(alerts))
        memo = getattr(self, "_site_index_memo", None)
        if memo is not None and memo["key"] == key:
            return memo["idx"]

        # Devices by site id; devices with no site id apply to all sites.
        dev_by_site: dict[str, list[dict[str, Any]]] = {}
        dev_global: list[dict[str, Any]] = []
        for device in devices:
            sid = str(device.get("siteId") or device.get("site_id") or "").strip()
            (dev_by_site.setdefault(sid, []) if sid else dev_global).append(device)

        # Clients by site id.
        cli_by_site: dict[str, list[dict[str, Any]]] = {}
        for cl in clients:
            sid = str(cl.get("siteId") or cl.get("site_id") or "").strip()
            if sid:
                cli_by_site.setdefault(sid, []).append(cl)

        # Insights by site name; empty or "all sites" applies to every site.
        ins_by_site: dict[str, list[dict[str, Any]]] = {}
        ins_global: list[dict[str, Any]] = []
        for ins in insights:
            s = str(ins.get("site") or "").strip().lower()
            (ins_global if (not s or s == "all sites")
             else ins_by_site.setdefault(s, [])).append(ins)

        # Alerts by site name; empty or a dash applies to every site.
        al_by_site: dict[str, list[dict[str, Any]]] = {}
        al_global: list[dict[str, Any]] = []
        for al in alerts:
            s = str(al.get("site") or "").strip()
            (al_global if (not s or s in ("-", "—"))
             else al_by_site.setdefault(s.lower(), [])).append(al)

        # Build the index.
        idx = {
            "devices": devices, "clients": clients,
            "dev_by_site": dev_by_site, "dev_global": dev_global,
            "cli_by_site": cli_by_site,
            "ins_by_site": ins_by_site, "ins_global": ins_global,
            "al_by_site": al_by_site, "al_global": al_global,
        }
        # Hold refs to the source lists so their ids can't be reused while memoized.
        self._site_index_memo = {"key": key, "idx": idx,
                                 "_refs": (devices, clients, insights, alerts)}
        return idx

    async def _nc_sites_health(self) -> list[dict[str, Any]]:
        """Return cached sites-health list; one API call shared across all site queries."""
        # Return the cached list if still fresh.
        cached = _sites_health_cache.get(self._config_hash)
        if cached and time.time() - cached[0] < _NC_GLOBAL_CACHE_TTL:
            return cached[1]
        # Fetch sites health.
        result: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                data = await self._get(http, "/network-monitoring/v1alpha1/sites-health")
                result = data.get("items") or []
        # Log failures.
        except Exception as exc:
            logger.warning("new_central sites-health cache fetch [%s]: %s", self._config_hash, exc)
        # Cache the result, even if empty.
        _sites_health_cache[self._config_hash] = (time.time(), result)
        return result

    async def _nc_devices(self) -> list[dict[str, Any]]:
        """Return cached devices list from /network-monitoring/v1/devices (paginated)."""
        # Return the cached list if still fresh.
        cached = _devices_cache.get(self._config_hash)
        if cached and time.time() - cached[0] < _NC_GLOBAL_CACHE_TTL:
            return cached[1]
        # Fetch every page of devices.
        result: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                params: dict[str, Any] = {"limit": 1000}
                while True:
                    data = await self._get(http, "/network-monitoring/v1/devices", params=params)
                    items = data.get("items") or []
                    result.extend(items)
                    nxt = data.get("next")
                    if not nxt or len(items) < 1000:
                        break
                    params["next"] = nxt
        # Log failures.
        except Exception as exc:
            logger.warning("new_central devices cache fetch [%s]: %s", self._config_hash, exc)
        # Cache the result, even if empty.
        _devices_cache[self._config_hash] = (time.time(), result)
        return result

    async def _nc_clients(self) -> list[dict[str, Any]]:
        """Return cached clients list from /network-monitoring/v1/clients (paginated)."""
        # Return the cached list if still fresh.
        cached = _nc_clients_cache.get(self._config_hash)
        if cached and time.time() - cached[0] < _NC_GLOBAL_CACHE_TTL:
            return cached[1]
        # Fetch every page of clients.
        result: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                params: dict[str, Any] = {"limit": 1000}
                while True:
                    data = await self._get(http, "/network-monitoring/v1/clients", params=params)
                    items = data.get("items") or []
                    result.extend(items)
                    nxt = data.get("next")
                    if not nxt or len(items) < 1000:
                        break
                    params["next"] = nxt
        # Log failures.
        except Exception as exc:
            logger.warning("new_central clients cache fetch [%s]: %s", self._config_hash, exc)
        # Log the client field names once for debugging.
        if result:
            logger.info("new_central client fields sample [%s]: %s", self._config_hash, list(result[0].keys()))
        # Cache the result, even if empty.
        _nc_clients_cache[self._config_hash] = (time.time(), result)
        return result

    # Reason-code → human-readable alert name mapping for new_central sites-health.
    # Codes follow the pattern: <device>_<metric>_<severity> e.g. AP_CHANNEL_UTILIZATION_5GHZ_FAIR
    _REASON_NAMES: dict[str, str] = {
        "DEVICE_OFFLINE": "Device Offline",
        "AP_OFFLINE": "AP Offline",
        "SWITCH_OFFLINE": "Switch Offline",
        "GW_OFFLINE": "Gateway Offline",
        "AP_CHANNEL_UTILIZATION_5GHZ_FAIR": "AP 5GHz Channel Utilization",
        "AP_CHANNEL_UTILIZATION_5GHZ_POOR": "AP 5GHz Channel Utilization",
        "AP_CHANNEL_UTILIZATION_24GHZ_FAIR": "AP 2.4GHz Channel Utilization",
        "AP_CHANNEL_UTILIZATION_24GHZ_POOR": "AP 2.4GHz Channel Utilization",
        "AP_CHANNEL_UTILIZATION_FAIR": "AP Channel Utilization",
        "AP_CHANNEL_UTILIZATION_POOR": "AP Channel Utilization",
        "GW_TUNNEL_FLAP_FAIR": "Gateway Tunnel Flap",
        "GW_TUNNEL_FLAP_POOR": "Gateway Tunnel Flap",
        "GW_UPLINK_UTIL_FAIR": "Gateway Uplink Utilization",
        "GW_UPLINK_UTIL_POOR": "Gateway Uplink Utilization",
        "AP_UPLINK_UTIL_FAIR": "AP Uplink Utilization",
        "AP_UPLINK_UTIL_POOR": "AP Uplink Utilization",
        "AP_DOWNLINK_PKT_DROP_FAIR": "Downlink Packet Drops",
        "AP_DOWNLINK_PKT_DROP_POOR": "Downlink Packet Drops",
        "AP_DOWNLINK_PACKET_DROPS_FAIR": "Downlink Packet Drops",
        "AP_DOWNLINK_PACKET_DROPS_POOR": "Downlink Packet Drops",
        "AP_UPLINK_PKT_DROP_FAIR": "Uplink Packet Drops",
        "AP_UPLINK_PKT_DROP_POOR": "Uplink Packet Drops",
        "CLIENT_COUNT_FAIR": "High Client Count",
        "CLIENT_COUNT_POOR": "High Client Count",
        "AP_CLIENT_COUNT_FAIR": "AP High Client Count",
        "AP_CLIENT_COUNT_POOR": "AP High Client Count",
        "MEMORY_USAGE_FAIR": "Memory Usage",
        "MEMORY_USAGE_POOR": "Memory Usage",
        "CPU_USAGE_FAIR": "CPU Usage",
        "CPU_USAGE_POOR": "CPU Usage",
        "AP_NOISE_5GHZ_FAIR": "AP 5GHz Noise Floor",
        "AP_NOISE_5GHZ_POOR": "AP 5GHz Noise Floor",
        "AP_NOISE_24GHZ_FAIR": "AP 2.4GHz Noise Floor",
        "AP_NOISE_24GHZ_POOR": "AP 2.4GHz Noise Floor",
        "EMPTY_SITE": "Empty Site",
    }

    @classmethod
    def _reason_to_alert_name(cls, reason_code: str) -> str:
        """Convert a sites-health reason code to a human-readable alert name."""
        # Use a known name if there is one.
        name = cls._REASON_NAMES.get(reason_code)
        if name:
            return name
        # Fallback: clean up the code e.g. AP_DOWNLINK_PKT_DROP_FAIR → "AP Downlink Pkt Drop"
        clean = reason_code.replace("_FAIR", "").replace("_POOR", "")
        return " ".join(w.capitalize() for w in clean.split("_"))

    @classmethod
    def _reason_severity(cls, health: str) -> str:
        """Map a sites-health reason health value to a severity string."""
        # Poor is red, fair is yellow, anything else green.
        h = str(health or "").lower()
        if h == "poor":
            return "red"
        if h == "fair":
            return "yellow"
        return "green"

    @classmethod
    def _reason_category(cls, reason_code: str) -> str:
        """Infer alert category from the reason code prefix."""
        code = reason_code.upper()
        # Check specific metrics before generic prefixes
        if "CHANNEL" in code or "NOISE" in code or "PKT_DROP" in code or "PACKET_DROP" in code:
            return "LAN"
        if "TUNNEL" in code or "UPLINK_UTIL" in code:
            return "WAN"
        if "CLIENT" in code:
            return "Client"
        if "OFFLINE" in code:
            return "Device"
        if code.startswith("AP_") or code.startswith("SWITCH_") or code.startswith("GW_"):
            return "Device"
        return ""

    @staticmethod
    def _nc_alert_severity(severity: str) -> str:
        """Map new_central alert severity (Critical/Major/Minor/Info) to UI colour."""
        # Map each severity to a colour.
        s = str(severity or "").lower()
        if s == "critical":
            return "red"
        if s == "major":
            return "orange"
        if s == "minor":
            return "yellow"
        return "info"

    async def _new_central_alerts(self, include_cleared: bool = False) -> list[dict[str, Any]]:
        """Fetch alerts from /network-notifications/v1/alerts.

        This is the real individual-alert endpoint (returned 404 on old paths like
        /monitoring/v1/alerts).  Each alert has name, site, severity, category,
        deviceType, summary and createdAt.  Results are grouped by (name, site) so
        the same alert firing multiple times shows as one row with a count.
        Cache TTL: 5 minutes.

        include_cleared=False (default) returns ONLY currently-active alerts — the
        set the dashboard counts as present problems. include_cleared=True ALSO
        returns cleared/closed alerts (each tagged ``status`` = "active"|"cleared")
        so the browse/Monitor view and the Sim-Quota ID picker can offer the full
        universe of alert names, not just the ones firing right now.
        """
        # Separate caches for active-only and all alerts.
        cache_key = f"{self._config_hash}:{'all' if include_cleared else 'active'}"
        cached = _alerts_cache.get(cache_key)
        if cached and time.time() - cached[0] < _ALERTS_CACHE_TTL:
            return cached[1]
        # Fetch and group alerts.
        alerts: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                # Fetch all active alerts (max 100 per page, follow pagination).
                # NOTE: OData filter parameter is "$filter" (with $), not "filter".
                # "sort" is not a supported query param on this endpoint.
                # network-notifications/v1/alerts — VERIFIED against Aruba's own
                # SDK reference (aruba/central-python-workflows msp-tenant-monitoring
                # + pycentral): NUMERIC "next" page starting at 1, NO OData $filter
                # (the old $filter=status eq 'Active' returned nothing), items under
                # "items", ~10-page safety cap. Alert fields per Aruba map_alert:
                # name/summary/severity/status/category/deviceType/createdAt/clearedReason.
                raw_alerts: list[dict[str, Any]] = []
                next_page: Any = 1
                pages = 0
                seen: set = set()
                # Bounded: <=5 pages, stop on an empty page or a non-advancing
                # "next" — so slow/looping alert pagination can NEVER stall
                # browse_all (which awaits alerts + insights + sites in parallel).
                while next_page is not None and pages < 5 and next_page not in seen:
                    seen.add(next_page)
                    payload = await self._get(http, "/network-notifications/v1/alerts",
                                              params={"limit": 100, "next": next_page})
                    if isinstance(payload, dict) and isinstance(payload.get("msg"), dict):
                        payload = payload["msg"]  # some responses wrap the body under "msg"
                    # Collect this page's items.
                    items = (payload or {}).get("items") or []
                    raw_alerts.extend(items)
                    pages += 1
                    next_page = (payload or {}).get("next")
                    if not items:
                        break

                # Diagnostic: log the first alert's field names once (values NOT logged).
                if raw_alerts:
                    logger.info("new_central alerts sample keys [%s]: %s",
                                self._config_hash, sorted(raw_alerts[0].keys()))

                # Resolve a site for each alert. The payload may carry a site by
                # NAME (siteName/site/groupName) or only a siteId — build an
                # id->name map from the cached sites-health so alerts key to the
                # right site instead of falling to the global "-" bucket.
                site_id_to_name: dict[str, str] = {}
                try:
                    for s in await self._nc_sites_health():
                        _sid = str(s.get("id") or s.get("siteId") or s.get("site_id") or "").strip()
                        _nm = str(s.get("name") or s.get("siteName") or s.get("site_name") or "").strip()
                        if _sid and _nm:
                            site_id_to_name[_sid] = _nm
                except Exception:  # noqa: BLE001
                    pass

                # Group by (name, site). By default keep only non-cleared alerts
                # (the demo's expected errors that should be PRESENT); with
                # include_cleared, keep cleared ones too and tag each group's
                # status "active" if ANY occurrence is still active else "cleared".
                groups: dict[tuple[str, str], dict[str, Any]] = {}
                for item in raw_alerts:
                    status = str(item.get("status") or "").strip().lower()
                    is_cleared = status in ("cleared", "closed", "resolved") or bool(item.get("clearedReason"))
                    if is_cleared and not include_cleared:
                        continue
                    name = str(item.get("name") or item.get("summary") or "Alert").strip()
                    site = (str(item.get("siteName") or item.get("site_name")
                                or item.get("site") or item.get("groupName") or "").strip()
                            or site_id_to_name.get(str(item.get("siteId")
                                or item.get("site_id") or "").strip(), "")
                            or "—")
                    # Start a new group for each new name and site pair.
                    key = (name.lower(), site.lower())
                    if key not in groups:
                        groups[key] = {
                            "name": name,
                            "site": site,
                            "severity": self._nc_alert_severity(item.get("severity", "")),
                            "category": str(item.get("category") or "").strip(),
                            "device_type": str(item.get("deviceType") or "").strip(),
                            "detail": str(item.get("summary") or item.get("description") or "").strip(),
                            "ts": item.get("createdAt") or item.get("updatedAt") or None,
                            "status": "cleared",
                            "count": 0,
                        }
                    # Count it and mark the group active if any occurrence is.
                    groups[key]["count"] += 1
                    if not is_cleared:
                        groups[key]["status"] = "active"

                # Fold the count into the detail text.
                for entry in groups.values():
                    cnt = entry.pop("count")
                    if cnt > 1:
                        entry["detail"] = f"{cnt} occurrences" + (f" — {entry['detail']}" if entry["detail"] else "")
                    alerts.append(entry)

        # Log failures with the response body.
        except Exception as exc:
            body = getattr(getattr(exc, "response", None), "text", None)
            logger.warning("new_central alerts fetch [%s]: %s%s", self._config_hash, exc, f" — {body}" if body else "")
        # Log the count.
        logger.info("new_central alerts fetched [%s]: %d alert groups (include_cleared=%s) from /network-notifications/v1/alerts",
                    self._config_hash, len(alerts), include_cleared)
        # Cache empty results for only a minute so they are retried soon.
        ttl_offset = 0 if alerts else (_ALERTS_CACHE_TTL - 60)
        _alerts_cache[cache_key] = (time.time() - ttl_offset, alerts)
        return alerts

    async def _new_central_insights(self) -> list[dict[str, Any]]:
        """Fetch AI-powered insights from /network-notifications/v1/insights.

        Each insight is global (siteId="-1") or site-specific and contains an
        impactedSites list.  Results are cached for 15 minutes.
        """
        # Return cached insights if still fresh.
        cached = _insights_cache.get(self._config_hash)
        if cached and time.time() - cached[0] < _INSIGHTS_CACHE_TTL:
            return cached[1]

        # Fetch insights.
        insights: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                payload = await self._get(http, "/network-notifications/v1/insights", params={"limit": 100})
                raw_list = payload.get("items") or []
                # Normalise each insight.
                for item in raw_list:
                    title = str(item.get("title") or item.get("name") or item.get("category") or "Insight").strip()
                    category = str(item.get("category") or "").strip()
                    description = str(item.get("description") or "").strip()
                    ts = item.get("timestamp") or item.get("createdAt") or None
                    # Convert epoch-ms timestamp to ISO string if needed
                    if ts and str(ts).isdigit() and len(str(ts)) == 13:
                        import datetime
                        ts = datetime.datetime.utcfromtimestamp(int(ts) / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")

                    # Add one row per impacted site.
                    impacted = item.get("impactedSites") or []
                    if impacted:
                        for s in impacted:
                            site_name = str(s.get("siteName") or s.get("name") or "").strip() or "All Sites"
                            insights.append({
                                "name": title,
                                "site": site_name,
                                "category": category,
                                "severity": "info",
                                "description": description,
                                "device_count": s.get("impactedDeviceCount") or 0,
                                "client_count": s.get("impactedClientCount") or 0,
                                "ts": ts,
                            })
                    # Otherwise one global row.
                    else:
                        insights.append({
                            "name": title,
                            "site": "All Sites",
                            "category": category,
                            "severity": "info",
                            "description": description,
                            "device_count": 0,
                            "client_count": 0,
                            "ts": ts,
                        })
        # Log failures.
        except Exception as exc:
            logger.warning("new_central insights fetch [%s]: %s", self._config_hash, exc)
        # Log the count.
        logger.info("new_central insights fetched [%s]: %d insights from /network-notifications/v1/insights", self._config_hash, len(insights))
        # Cache empty results for only two minutes.
        ttl = _INSIGHTS_CACHE_TTL if insights else 120
        _insights_cache[self._config_hash] = (time.time() - (_INSIGHTS_CACHE_TTL - ttl), insights)
        return insights

    async def browse_all(self) -> dict[str, Any]:
        """Fetch all Central sites, alerts, insights, clients, and devices for the browse view."""
        # Run the fetches in parallel.
        import asyncio

        # New Central: fetch everything at once.
        if self.api_version == "new_central":
            sites, all_devices, all_clients, nc_insights, nc_alerts = await asyncio.gather(
                self.list_sites(),
                self._nc_devices(),
                self._nc_clients(),
                self._new_central_insights(),
                self._new_central_alerts(include_cleared=True),
                return_exceptions=True,
            )
            if isinstance(sites, Exception):
                # Re-raise so _refresh_central_browse preserves the existing cache
                raise sites
            # Other failures fall back to empty lists.
            if isinstance(all_devices, Exception):
                all_devices = []
            if isinstance(all_clients, Exception):
                all_clients = []
            if isinstance(nc_insights, Exception):
                nc_insights = []
            if isinstance(nc_alerts, Exception):
                nc_alerts = []

            # Build devices_by_site: {siteName: [device, ...]}
            devices_by_site: dict[str, list[dict[str, Any]]] = {}
            for dev in all_devices:
                sn = (dev.get("siteName") or dev.get("site_name") or "—").strip() or "—"
                devices_by_site.setdefault(sn, []).append({
                    "name": dev.get("deviceName") or dev.get("name") or dev.get("serialNumber") or "—",
                    "type": dev.get("deviceType") or "",
                    "model": dev.get("model") or "",
                    "status": dev.get("status") or "",
                    "serial": dev.get("serialNumber") or dev.get("id") or "",
                    "ip": dev.get("ipv4") or dev.get("ipv6") or "",
                    "firmware": dev.get("firmwareVersion") or "",
                    "last_seen": dev.get("lastSeenAt") or "",
                })

            # Build clients_by_site: {siteName: {total, wired, wireless}}
            clients_by_site: dict[str, dict[str, Any]] = {}
            normalized_clients: list[dict[str, Any]] = []
            for cli in all_clients:
                sn = (cli.get("siteName") or cli.get("site_name") or "—").strip() or "—"
                entry = clients_by_site.setdefault(sn, {"total": 0, "wired": 0, "wireless": 0})
                entry["total"] += 1
                conn_type = str(cli.get("clientConnectionType") or "").lower()
                if conn_type == "wired":
                    entry["wired"] += 1
                elif conn_type == "wireless":
                    entry["wireless"] += 1
                normalized_clients.append({
                    "mac": cli.get("macAddress") or cli.get("mac_address") or cli.get("mac") or "—",
                    "ip": cli.get("ipv4") or cli.get("ipv4Address") or cli.get("ip_address") or cli.get("ip") or "—",
                    "hostname": cli.get("name") or cli.get("clientName") or cli.get("deviceName") or cli.get("hostname") or "—",
                    "username": cli.get("userName") or cli.get("username") or cli.get("associatedUser") or cli.get("auth_username") or "",
                    "site": sn,
                    "ap": cli.get("associatedDeviceName") or cli.get("associatedDevice") or cli.get("ap_name") or "—",
                    "ssid": cli.get("ssid") or cli.get("essid") or "—",
                    "status": cli.get("status") or "—",
                    "os": _client_os(cli),
                    "vlan": str(cli.get("vlan") or "—"),
                    "connection_type": cli.get("clientConnectionType") or "",
                })

            # Return the new Central browse data.
            return {
                "sites": sites,
                "alerts": list(nc_alerts),
                "insights": list(nc_insights),
                "clients": normalized_clients,
                "devices_by_site": devices_by_site,
                "clients_by_site": clients_by_site,
            }

        # Classic Central: fetch sites, findings and clients at once.
        sites, findings, clients = await asyncio.gather(
            self.list_sites(),
            self.poll_alerts_and_insights(),
            self.list_clients(),
            return_exceptions=True,
        )
        # Sites must succeed; others fall back to empty lists.
        if isinstance(sites, Exception):
            raise sites
        if isinstance(findings, Exception):
            findings = []
        if isinstance(clients, Exception):
            clients = []
        # Split findings into alerts and insights.
        alerts = [
            {"name": f.check_name, "site": f.site_name, "severity": f.status, "detail": "", "ts": None}
            for f in findings
            if isinstance(f, ArubaFinding) and f.source == "alert"
        ]
        insights = [
            {"name": f.check_name, "site": f.site_name, "severity": f.status, "category": "", "ts": None}
            for f in findings
            if isinstance(f, ArubaFinding) and f.source == "insight"
        ]
        return {"sites": sites, "alerts": alerts, "insights": insights, "clients": clients}

    async def register_webhook(self, name: str, endpoint_url: str, api_key: str) -> dict[str, Any]:
        """Register a webhook with Central. Returns the created webhook object."""
        # Get a token and create the webhook.
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._ensure_token(client)
            resp = await client.post(
                f"{self.cluster_url}/network-services/v1/webhooks",
                headers=self._headers(token),
                json={
                    "name": name,
                    "endpointURL": endpoint_url,
                    "authMechanism": {"type": "API_KEY", "apiKey": api_key},
                },
                timeout=15,
            )
            # Raise on HTTP errors and return the result.
            resp.raise_for_status()
            return resp.json()

    async def list_webhooks(self) -> list[dict[str, Any]]:
        """List all registered webhooks."""
        # Fetch the list, which may be wrapped in "items".
        async with httpx.AsyncClient(timeout=15) as client:
            data = await self._get(client, "/network-services/v1/webhooks")
            if isinstance(data, dict):
                items = data.get("items")
                return items if isinstance(items, list) else []
            return data if isinstance(data, list) else []

    async def delete_webhook(self, webhook_id: str) -> None:
        """Delete a webhook by ID."""
        # Get a token and delete the webhook.
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._ensure_token(client)
            resp = await client.delete(
                f"{self.cluster_url}/network-services/v1/webhooks/{webhook_id}",
                headers=self._headers(token),
                timeout=15,
            )
            # A missing webhook is fine.
            if resp.status_code != 404:
                resp.raise_for_status()

    async def available_checks(self) -> dict[str, Any]:
        """Return Aruba Central alert, insight, and hardware catalogs for the UI."""
        # Nothing to show without a config.
        if not self.is_configured():
            return {"alerts": [], "insights": [], "hardware": [], "warning": "Central not configured."}

        # New Central uses the default catalogs.
        if self.api_version == "new_central":
            return {
                "alerts": [dict(item) for item in DEFAULT_NEW_CENTRAL_MONITORED_CHECKS],
                "insights": [],
                "hardware": [dict(item) for item in DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS],
                "warning": None,
            }

        # Classic Central: collect alert types and insight categories from the last 30 days.
        alert_types: dict[str, str] = {}
        insight_categories: dict[str, str] = {}
        warnings: list[str] = []
        thirty_days_ago = int(time.time()) - 30 * 86400

        # Try each alerts endpoint until one returns types.
        async with httpx.AsyncClient(timeout=30) as client:
            for alerts_path in ("/monitoring/v1/alerts", "/monitoring/v2/alerts", "/aiops/v2/alerts"):
                try:
                    payload = await self._get(client, alerts_path, params={"limit": 1000, "from_timestamp": thirty_days_ago})
                    for alert in payload.get("alerts") or payload.get("items") or []:
                        alert_id = str(alert.get("alert_type") or alert.get("type") or "").strip()
                        if not alert_id:
                            continue
                        alert_types[alert_id] = str(
                            alert.get("alert_type_name")
                            or alert.get("name")
                            or alert_id.replace("_", " ").title()
                        )
                    if alert_types:
                        break
                # A 404 means try the next endpoint; other errors are warnings.
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        continue
                    warnings.append(f"Alerts endpoint returned HTTP {exc.response.status_code}.")
                    break
                except Exception as exc:
                    warnings.append(f"Network error fetching alerts: {exc}")
                    break

            # Try each insights endpoint until one returns categories.
            for insights_path in ("/aiops/v1/insights", "/aiops/v2/insights"):
                try:
                    payload = await self._get(client, insights_path, params={"limit": 1000, "from_timestamp": thirty_days_ago})
                    for insight in payload.get("insights") or payload.get("items") or []:
                        category = str(insight.get("category") or insight.get("type") or "").strip()
                        if not category:
                            continue
                        insight_categories[category] = str(
                            insight.get("category_name")
                            or insight.get("name")
                            or category.replace("_", " ").title()
                        )
                    if insight_categories:
                        break
                # A 404 means try the next endpoint; other errors are warnings.
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        continue
                    warnings.append(f"Insights endpoint returned HTTP {exc.response.status_code}.")
                    break
                except Exception as exc:
                    warnings.append(f"Network error fetching insights: {exc}")
                    break

        # Fall back to the standard lists when nothing was found.
        using_fallback = False
        if not alert_types:
            alert_types = dict(KNOWN_CLASSIC_ALERT_TYPES)
            using_fallback = True
        if not insight_categories:
            insight_categories = dict(KNOWN_CLASSIC_INSIGHT_CATEGORIES)
            using_fallback = True
        if using_fallback:
            warnings.append("No live checks returned by Central — showing standard Aruba Central check types.")

        # Hardware checks.
        hardware_catalog = [
            dict(item)
            for item in DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS
            if item.get("id") in alert_types or item.get("id") in {"AP_DOWN", "SWITCH_DOWN", "GATEWAY_DOWN"}
        ]
        # Build the catalog.
        return {
            "alerts": [{"id": key, "name": value} for key, value in sorted(alert_types.items())],
            "insights": [{"id": key, "name": value} for key, value in sorted(insight_categories.items())],
            "hardware": hardware_catalog,
            "warning": "; ".join(dict.fromkeys(warnings)) if warnings else None,
        }

    async def discover_tenants(self) -> list[dict[str, Any]]:
        """Discover Aruba MSP customer tenants and normalize them to ``{cid, name}`` pairs."""
        # Nothing to discover without a config.
        if not self.is_configured():
            return []

        # Possible MSP customer list endpoints.
        endpoints = (
            "/msptopo/v1/customer_list",
            "/msp_api/v1/customer_list",
            "/platform/msp/customer_list",
        )
        # Try each endpoint until one returns customers.
        async with httpx.AsyncClient(timeout=30) as client:
            for path in endpoints:
                try:
                    data = await self._get(client, path)
                    customers = data.get("customers") or data.get("items") or data.get("data") or []
                    results = [
                        {
                            "cid": str(c.get("customer_id") or c.get("cid") or c.get("id") or ""),
                            "name": c.get("customer_name") or c.get("name") or c.get("cid") or c.get("id") or "",
                        }
                        for c in customers
                        if c.get("customer_id") or c.get("cid") or c.get("id")
                    ]
                    if results:
                        return results
                # A 404 means try the next endpoint; other errors give up.
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        continue
                    logger.warning("Aruba MSP tenant discovery failed [%s]: %s", self._config_hash, exc)
                    return []
                except Exception as exc:
                    logger.warning("Aruba MSP tenant discovery failed [%s]: %s", self._config_hash, exc)
                    return []
        # No endpoint worked.
        return []
