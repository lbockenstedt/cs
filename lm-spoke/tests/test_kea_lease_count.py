"""Kea memfile lease counting.

Regression: the panel reported 688 leases for a pool of 244 addresses. Kea's
memfile is APPEND-ONLY — every renew/release appends a row for the same address,
and the file only shrinks when kea-lfc compacts it. Counting lines therefore
reports the file's HISTORY, not current leases. In the field LFC was failing
(AppArmor denying kea-lfc dac_override / the rename / the pid file), so the file
grew without bound and the number was nonsense.
"""
import time

from dhcp_health import count_leases


def count(lines, now=None):
    return count_leases(lines, now or int(time.time()))


HDR = "address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state"


def _row(addr, lifetime=3600, expire_in=3600, now=None):
    exp = (now or int(time.time())) + expire_in
    return f"{addr},aa:bb,01:aa:bb,{lifetime},{exp},1,0,0,,0"


def test_renewals_of_one_address_count_once():
    now = int(time.time())
    lines = [HDR] + [_row("169.253.1.11", now=now) for _ in range(50)]
    live, addrs, rows = count(lines, now)
    assert (live, addrs, rows) == (1, 1, 50)


def test_released_lease_is_not_live():
    # valid_lifetime 0 == release. Last row wins, so a re-lease then release
    # leaves the address NOT live.
    now = int(time.time())
    lines = [HDR, _row("169.253.1.11", now=now), _row("169.253.1.11", lifetime=0, now=now)]
    assert count(lines, now)[0] == 0


def test_expired_lease_is_not_live():
    now = int(time.time())
    lines = [HDR, _row("169.253.1.12", expire_in=-60, now=now)]
    assert count(lines, now)[0] == 0


def test_count_cannot_exceed_the_pool():
    # THE field symptom: 244 usable addresses, each renewed repeatedly.
    now = int(time.time())
    lines = [HDR]
    for n in range(11, 255):
        for _ in range(3):
            lines.append(_row(f"169.253.1.{n}", now=now))
    live, addrs, rows = count(lines, now)
    assert live == 244 and addrs == 244 and rows == 732
    assert live <= 244, "a lease count above the pool size is impossible"


def test_malformed_and_blank_rows_are_skipped():
    now = int(time.time())
    lines = [HDR, "", "garbage", ",,,,", _row("169.253.1.20", now=now)]
    assert count(lines, now)[0] == 1


def test_denial_age_from_dmesg_timestamp():
    from dhcp_health import denial_age_s
    assert denial_age_s("[ 1105.38] apparmor=\"DENIED\" kea", 2000.0) == 895
    assert denial_age_s("[ 1105.38] x", 0.0) is None          # uptime unknown
    assert denial_age_s("no timestamp", 2000.0) is None
    assert denial_age_s("[ 3000.1] x", 2000.0) == 0           # clock skew clamps to 0


# ── One rule for the panel AND the hub's DHCP Server card ──────────────────
# dhcp_status._read_leases (card) and count_leases (panel) used to disagree:
# the card counted releases (valid_lifetime 0, state 0) as used, capped the
# count at its 50-row payload, and showed "0 used" when the file was
# unreadable. Both now go through dhcp_health.live_leases.

import json  # noqa: E402

import dhcp_status  # noqa: E402
from dhcp_health import live_leases  # noqa: E402


def _row_state(addr, state, now, lifetime=3600, expire_in=3600, host=""):
    return f"{addr},aa:bb,01:aa:bb,{lifetime},{now + expire_in},1,0,0,{host},{state}"


def test_declined_or_reclaimed_lease_is_not_live():
    now = int(time.time())
    lines = [HDR, _row_state("169.253.1.30", 1, now), _row_state("169.253.1.31", 2, now),
             _row_state("169.253.1.32", 0, now)]
    assert count(lines, now) == (1, 3, 3)


def test_missing_state_column_counts_as_active():
    now = int(time.time())
    assert count([HDR, f"169.253.1.40,aa,01,3600,{now + 60}"], now)[0] == 1


def test_lease_expiring_exactly_now_is_not_live():
    now = int(time.time())
    assert count([HDR, _row("169.253.1.41", expire_in=0, now=now)], now)[0] == 0


def test_live_leases_keeps_first_seen_order_and_last_row_wins():
    now = int(time.time())
    lines = [HDR, _row_state("10.0.0.2", 0, now, host="a"), _row_state("10.0.0.1", 0, now),
             _row_state("10.0.0.2", 0, now, host="b")]
    live, addrs, rows = live_leases(lines, now)
    assert list(live) == ["10.0.0.2", "10.0.0.1"] and live["10.0.0.2"][8] == "b"
    assert (addrs, rows) == (2, 3)


def _write(tmp_path, lines):
    p = tmp_path / "leases.csv"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_card_and_panel_count_the_same_file_the_same(tmp_path):
    now = int(time.time())
    lines = [HDR]
    for n in range(1, 120):
        st = 1 if n % 7 == 0 else 0
        lt = 0 if n % 5 == 0 else 3600
        exp = -10 if n % 11 == 0 else 3600
        lines.append(_row_state(f"10.1.0.{n}", st, now, lifetime=lt, expire_in=exp))
        lines.append(_row_state(f"10.1.0.{n}", st, now, lifetime=lt, expire_in=exp))
    path = _write(tmp_path, lines)
    card_count, card_rows = dhcp_status._read_leases(path, now)
    panel_count = count(lines, now)[0]
    assert card_count == panel_count
    assert card_count > dhcp_status._MAX_LEASE_ROWS, "count must not be capped"
    assert len(card_rows) == dhcp_status._MAX_LEASE_ROWS


def test_card_does_not_count_a_released_lease(tmp_path):
    now = int(time.time())
    path = _write(tmp_path, [HDR, _row_state("10.2.0.1", 0, now),
                             _row_state("10.2.0.1", 0, now, lifetime=0)])
    assert dhcp_status._read_leases(path, now) == (0, [])


def test_card_rows_keep_the_dnsmasq_shape(tmp_path):
    now = int(time.time())
    path = _write(tmp_path, [HDR, _row_state("10.3.0.1", 0, now, host="pc1")])
    n, rows = dhcp_status._read_leases(path, now)
    assert n == 1 and rows == [{"mac": "aa:bb", "ip": "10.3.0.1", "hostname": "pc1",
                                "expiry": now + 3600}]


def test_card_missing_file_is_zero_unreadable_is_unknown(tmp_path):
    assert dhcp_status._read_leases(str(tmp_path / "nope.csv"), time.time()) == (0, [])
    # A directory raises IsADirectoryError on open — stands in for EACCES
    # (tests may run as root, where chmod 000 would still be readable).
    assert dhcp_status._read_leases(str(tmp_path), time.time()) == (None, [])


def _collect_with(tmp_path, monkeypatch, lease_path):
    conf = tmp_path / "kea.conf"
    conf.write_text(json.dumps({"Dhcp4": {
        "interfaces-config": {"interfaces": ["eth1"]},
        "lease-database": {"name": lease_path},
        "subnet4": [{"pools": [{"pool": "10.4.0.1 - 10.4.0.10"}]}]}}))
    monkeypatch.setattr(dhcp_status, "KEA_DHCP4_CONF", str(conf))
    monkeypatch.setattr(dhcp_status, "_is_running", lambda: True)
    monkeypatch.setattr(dhcp_status, "_iface_subnet", lambda iface: (None, None))
    return dhcp_status.collect_dhcp_status()


def test_card_payload_counts(tmp_path, monkeypatch):
    now = int(time.time())
    path = _write(tmp_path, [HDR] + [_row_state(f"10.4.0.{n}", 0, now) for n in (1, 2, 3)])
    d = _collect_with(tmp_path, monkeypatch, path)
    assert (d["leases_used"], d["leases_free"], d["utilization_pct"], d["leases_readable"]) \
        == (3, 7, 30.0, True)


def test_card_payload_unreadable_is_unknown_not_zero(tmp_path, monkeypatch):
    d = _collect_with(tmp_path, monkeypatch, str(tmp_path))
    assert d["installed"] is True and "error" not in d
    assert (d["leases_used"], d["leases_free"], d["utilization_pct"], d["leases_readable"]) \
        == (None, None, None, False)
