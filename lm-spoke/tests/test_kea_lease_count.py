"""Kea memfile lease counting.

Regression: the panel reported 688 leases for a pool of 244 addresses. Kea's
memfile is APPEND-ONLY — every renew/release appends a row for the same address,
and the file only shrinks when kea-lfc compacts it. Counting lines therefore
reports the file's HISTORY, not current leases. In the field LFC was failing
(AppArmor denying kea-lfc dac_override / the rename / the pid file), so the file
grew without bound and the number was nonsense.
"""
import time


def count(lines, now=None):
    """Mirror of the collector: distinct addresses, last row wins, live only."""
    now = now or int(time.time())
    state = {}
    rows = 0
    for i, line in enumerate(lines):
        if i == 0 or not line.strip():
            continue
        rows += 1
        c = line.rstrip("\n").split(",")
        if len(c) < 5 or not c[0].strip():
            continue
        try:
            state[c[0].strip()] = (int(float(c[3] or 0)), int(float(c[4] or 0)))
        except (TypeError, ValueError):
            continue
    live = [a for a, (lt, exp) in state.items() if lt > 0 and exp > now]
    return len(live), len(state), rows


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
