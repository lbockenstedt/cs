"""Tests for the Sim-Quota config foundation (cs twin).

Schema/validation/resolution + the simulation.conf-derived catalog. The
engine-free config layer: declaring a quota stores config only.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
for p in (str(SRC),):
    if p not in sys.path:
        sys.path.insert(0, p)

import sim_quota  # noqa: E402

_SIM_CONF = """\
[address]
iperf_server=10.0.0.1
[s0]
wsite=MIA
dhcp_fail=off
dns_fail=off
ping_test=on
www_traffic=on
[s1]
wsite=MIA
assoc_fail=on
www_traffic=on
[s8]
wsite=DFW
dns_fail=on
"""


def _write_conf(tmp_path):
    (tmp_path / "simulation.conf").write_text(_SIM_CONF, encoding="utf-8")
    return tmp_path


# ── normalize_quota ────────────────────────────────────────────────────────
def test_normalize_quota_defaults_multi_capable_from_sim_meta():
    q = sim_quota.normalize_quota({"alert_id": "CLIENT_DHCP_FAILURE", "sim_id": "dhcp_fail", "count": "10", "site": "MIA"})
    assert q["sim_id"] == "dhcp_fail"
    assert q["count"] == 10
    assert q["multi_capable"] is False  # SIM_META default for dhcp_fail
    assert q["enabled"] is False
    assert q["alert_type"] == "alert"


def test_normalize_quota_traffic_sim_defaults_multi_capable_true():
    q = sim_quota.normalize_quota({"alert_id": "X", "sim_id": "ping_test", "count": 5})
    assert q["multi_capable"] is True


def test_normalize_quota_explicit_multi_capable_overrides_default():
    q = sim_quota.normalize_quota({"alert_id": "X", "sim_id": "dhcp_fail", "multi_capable": True})
    assert q["multi_capable"] is True


def test_normalize_quota_alert_type_insights():
    q = sim_quota.normalize_quota({"alert_id": "X", "sim_id": "dns_fail", "alert_type": "insight"})
    assert q["alert_type"] == "insight"
    bad = sim_quota.normalize_quota({"alert_id": "X", "sim_id": "dns_fail", "alert_type": "weird"})
    assert bad["alert_type"] == "alert"


def test_normalize_quota_count_floor():
    assert sim_quota.normalize_quota({"alert_id": "X", "sim_id": "dns_fail", "count": 0})["count"] == 1
    assert sim_quota.normalize_quota({"alert_id": "X", "sim_id": "dns_fail", "count": "7"})["count"] == 7
    assert sim_quota.normalize_quota({"alert_id": "X", "sim_id": "dns_fail", "count": "garbage"})["count"] == 1


# ── validate_sim_quotas ────────────────────────────────────────────────────
def test_validate_drops_missing_fields():
    clean, errs = sim_quota.validate_sim_quotas(
        [{"sim_id": "dns_fail"}, {"alert_id": "X"}], ["dns_fail"])
    assert clean == []
    assert len(errs) == 2


def test_validate_drops_unknown_sim_when_set_provided():
    clean, errs = sim_quota.validate_sim_quotas(
        [{"alert_id": "A", "sim_id": "nope", "count": 3}], ["dns_fail", "dhcp_fail"])
    assert clean == []
    assert any("nope" in e for e in errs)


def test_validate_no_sim_set_skips_filter():
    # available_sims=None → shape-only validation, unknown sim retained.
    clean, _ = sim_quota.validate_sim_quotas([{"alert_id": "A", "sim_id": "nope", "count": 3}], None)
    assert len(clean) == 1 and clean[0]["sim_id"] == "nope"


def test_validate_dedupe_last_wins():
    clean, _ = sim_quota.validate_sim_quotas(
        [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA"},
         {"alert_id": "A", "sim_id": "dhcp_fail", "count": 7, "site": "MIA"}],
        ["dns_fail", "dhcp_fail"])
    assert len(clean) == 1
    assert clean[0]["count"] == 7  # last wins


def test_resolve_effective_quotas_only_enabled():
    eff = sim_quota.resolve_effective_quotas(
        [{"alert_id": "A", "sim_id": "dns_fail", "enabled": True, "count": 10, "site": "MIA"},
         {"alert_id": "B", "sim_id": "dhcp_fail", "enabled": False, "count": 5, "site": "MIA"}],
        ["dns_fail", "dhcp_fail"])
    assert len(eff) == 1 and eff[0]["alert_id"] == "A"


# ── catalog from simulation.conf ───────────────────────────────────────────
def test_available_sims_from_conf_buckets(tmp_path):
    sims = sim_quota.available_sims(_write_conf(tmp_path))
    ids = [s["sim_id"] for s in sims]
    # Bucket sims appear first (in discovery order): ping_test, www_traffic, assoc_fail, dns_fail
    assert "ping_test" in ids and "assoc_fail" in ids and "dns_fail" in ids
    # Every returned sim is enriched with category + multi_capable.
    for s in sims:
        assert "category" in s and "multi_capable" in s
    # dhcp_fail isn't in any bucket but is a runnable PRIMITIVE → still offered.
    assert "dhcp_fail" in ids


def test_available_sites_merges_conf_wsite_and_central_mappings(tmp_path):
    sites = sim_quota.available_sites(_write_conf(tmp_path), {"MIA": "MIA-CENTRAL", "ATL": "ATL-CENTRAL"})
    assert "MIA" in sites and "DFW" in sites and "ATL" in sites and "ATL-CENTRAL" in sites
    assert sites == sorted(sites)


def test_sim_quota_catalog_shape(tmp_path):
    cat = sim_quota.sim_quota_catalog(_write_conf(tmp_path), {"MIA": "MIA"})
    assert set(cat.keys()) == {"sims", "sites", "suggested", "meta"}
    assert cat["suggested"]["CLIENT_DHCP_FAILURE"] == "dhcp_fail"
    assert "dns_fail" in cat["meta"]
    assert all(s["sim_id"] for s in cat["sims"])


def test_suggested_excludes_hardware_alerts():
    # Hardware alerts are monitoring-only — no sim linkage suggestion.
    for hw in ("AP_DOWN", "SWITCH_DOWN", "GATEWAY_DOWN"):
        assert hw not in sim_quota.SUGGESTED_ALERT_SIM

# ── presence quotas (Clients Associated — sim_id empty) ────────────────────
def test_normalize_presence_quota_forces_multi_capable():
    q = sim_quota.normalize_quota({"sim_id": "", "count": 17, "site": "MIA"})
    assert q["sim_id"] == "" and q["site"] == "MIA" and q["count"] == 17
    # Presence always packs (a homed-but-sim-less client is still a free runner
    # other sims may stack onto); an operator multi_capable=False is ignored.
    assert q["multi_capable"] is True
    assert sim_quota.normalize_quota(
        {"sim_id": "", "count": 17, "site": "MIA", "multi_capable": False}
    )["multi_capable"] is True


def test_validate_presence_quota_needs_site_not_alert_id():
    clean, errs = sim_quota.validate_sim_quotas(
        [{"sim_id": "", "count": 17, "site": "MIA", "enabled": True}],
        ["dns_fail"])
    assert errs == [] and len(clean) == 1
    assert clean[0]["sim_id"] == "" and clean[0]["site"] == "MIA"
    # A presence quota with no site is dropped.
    clean, errs = sim_quota.validate_sim_quotas(
        [{"sim_id": "", "count": 17, "enabled": True}], ["dns_fail"])
    assert clean == [] and any("requires a site" in e for e in errs)


def test_validate_presence_quota_ignored_alert_id_and_sim_set():
    # Presence quotas carry no alert_id and aren't filtered by available_sims
    # (they run no sim) — an empty/odd alert_id is accepted, not an error.
    clean, errs = sim_quota.validate_sim_quotas(
        [{"sim_id": "", "alert_id": "whatever", "count": 5, "site": "DFW",
          "enabled": True}], ["dns_fail"])
    assert errs == [] and len(clean) == 1
    # available_sims filter never applies to presence.
    clean, _ = sim_quota.validate_sim_quotas(
        [{"sim_id": "", "count": 5, "site": "DFW", "enabled": True}], [])
    assert len(clean) == 1


def test_validate_presence_dedup_by_site_last_wins():
    # Two presence rows at MIA collapse to one (last-wins) keyed by site; a DFW
    # presence row survives separately.
    clean, _ = sim_quota.validate_sim_quotas(
        [{"sim_id": "", "count": 17, "site": "MIA", "enabled": True},
         {"sim_id": "", "count": 10, "site": "MIA", "enabled": True},
         {"sim_id": "", "count": 5, "site": "DFW", "enabled": True}])
    assert len(clean) == 2
    mia = [c for c in clean if c["site"] == "MIA"][0]
    assert mia["count"] == 10  # last-wins


def test_validate_presence_does_not_collide_with_sim_quota_namespace():
    # A presence quota at MIA (key presence::MIA) and a sim quota for an alert
    # at MIA (key alert:CLIENT_DNS_FAILURE:MIA) coexist — different namespaces.
    clean, _ = sim_quota.validate_sim_quotas(
        [{"sim_id": "", "count": 17, "site": "MIA", "enabled": True},
         {"alert_id": "CLIENT_DNS_FAILURE", "sim_id": "dns_fail", "count": 8,
          "site": "MIA", "enabled": True}], ["dns_fail"])
    assert len(clean) == 2
    assert any(c["sim_id"] == "" for c in clean)
    assert any(c["sim_id"] == "dns_fail" for c in clean)


def test_quota_dedup_key_presence_vs_sim():
    assert sim_quota.quota_dedup_key({"sim_id": "", "site": "MIA"}) == "presence::MIA"
    assert sim_quota.quota_dedup_key(
        {"alert_type": "alert", "alert_id": "X", "sim_id": "dns_fail", "site": "MIA"}
    ) == "alert:X:MIA"
