"""Rules behind CS_GET_SIM_TAG_HEALTH (see the commit history of sim_tag_health)."""
import time

import sim_tag_health as sth


def _clients(**ages):
    now = time.time()
    return sth.client_index({i: {"hostname": h, "last_seen": None if a is None else now - a,
                                 "active_simulations": ["Wifi Auth"]}
                             for i, (h, a) in enumerate(ages.items())})


def test_vm_row_reasons():
    clients = _clients(live=10, stale=sth.TAG_LIVE_WINDOW_S + 1, never=None)
    assert sth.vm_row({"name": "nobody"}, clients)["excluded"] == "no_client_matches_vm_name"
    assert sth.vm_row({"name": "never"}, clients)["excluded"] == "client_never_reported"
    assert sth.vm_row({"name": "stale"}, clients)["excluded"] == "client_stale_tags_cleared"
    live = sth.vm_row({"name": "LIVE", "tags": ["sim-wifi-auth"]}, clients)
    assert "excluded" not in live and live["in_sync"]


def test_unlabeled_counts_only_sim_vms_without_tags():
    clients = _clients()
    untagged_sim = sth.vm_row({"vmid": 90001, "name": "x"}, clients)
    infra = sth.vm_row({"vmid": 105, "name": "infra"}, clients)
    tagged_sim = sth.vm_row({"vmid": 90002, "name": "y", "tags": ["sim-a"]}, clients)
    assert untagged_sim["in_sync"]  # why it used to vanish from drift_count
    assert sth.is_unlabeled(untagged_sim)
    assert not sth.is_unlabeled(infra)
    assert not sth.is_unlabeled(tagged_sim)


def test_host_report_flags_ghost_hosts_and_skips_templates():
    now = time.time()
    vms = [{"vmid": 90001, "name": "x"}, {"vmid": 9000, "is_template": True}, {"type": "lxc"}]
    ghost = sth.host_report("pve1", {"vms": vms, "last_seen": now - sth.STALE_HOST_S - 1},
                            {}, agent_connected=False, skip_reason=None, points_at=None)
    assert ghost["stale"] and ghost["vm_count"] == 1
    assert ghost["skip_reason"] == "agent_not_connected"


def test_pointing_from_identity():
    assert sth.pointing_from_identity({"payload": {"data": {"spoke_url": " wss://a "}}}) == "wss://a"
    assert sth.pointing_from_identity({"spoke_ip": "10.0.0.1"}) == "10.0.0.1"
    assert sth.pointing_from_identity({"payload": {"data": {}}}) == "(auto-discovery)"
    assert sth.pointing_from_identity({"payload": {"data": ["x"]}}) is None
