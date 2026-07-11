"""Override pruning — ``ClientRegistry.set_overrides`` drops override entries
that match the pure ``simulation.conf`` bucket default, so toggling a sim OFF
that's already off by default reverts to the bucket instead of leaving a
redundant ``flag:"off"`` entry (overrides stay a true diff over the bucket).

The bucket default is supplied by an injected ``bucket_resolver`` (the spoke
wires ``sim_config.pure_bucket_profile``). Tests use a fake resolver for
unit-level control + one integration test against the real
``pure_bucket_profile`` to pin that it EXCLUDES the ``[username]`` overlay (the
WebUI mirror pollutes user-overrides.conf, so including it would defeat the
prune).
"""
import asyncio
from pathlib import Path

import pytest

from client_registry import ClientRegistry
import sim_config


def _run(loop, coro):
    return loop.run_until_complete(coro)


def _reg(tmp_path, resolver=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    reg = ClientRegistry(tmp_path / "data", bucket_resolver=resolver)
    return reg, loop


def _close(loop):
    loop.close()
    asyncio.set_event_loop(None)


def test_override_matching_bucket_off_is_pruned(tmp_path):
    # Bucket default dns_fail = off; toggling OFF reverts to the bucket → the
    # override entry MUST be dropped (the user's core request).
    reg, loop = _reg(tmp_path, resolver=lambda hn: {"dns_fail": ""})
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "off"}))
    assert entry["overrides"] == {}
    _close(loop)


def test_override_matching_bucket_on_is_pruned(tmp_path):
    # Bucket default dns_fail = on; toggling ON matches the bucket → dropped.
    reg, loop = _reg(tmp_path, resolver=lambda hn: {"dns_fail": "on"})
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "on"}))
    assert entry["overrides"] == {}
    _close(loop)


def test_override_deviating_from_bucket_is_kept(tmp_path):
    # Bucket off, user turns ON → a real deviation → KEEP the override.
    reg, loop = _reg(tmp_path, resolver=lambda hn: {"dns_fail": ""})
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "on"}))
    assert entry["overrides"] == {"dns_fail": "on"}
    _close(loop)


def test_override_off_when_bucket_on_is_kept(tmp_path):
    # Bucket ON, user turns OFF → a real override (turning a bucket-on sim off)
    # → KEEP it. This is the case where "off" is NOT a revert-to-bucket.
    reg, loop = _reg(tmp_path, resolver=lambda hn: {"dns_fail": "on"})
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "off"}))
    assert entry["overrides"] == {"dns_fail": "off"}
    _close(loop)


def test_only_redundant_keys_are_pruned(tmp_path):
    # Mixed: dns_fail matches bucket (pruned); kill_switch deviates (kept).
    reg, loop = _reg(tmp_path,
                     resolver=lambda hn: {"dns_fail": "", "kill_switch": "on"})
    entry = _run(loop, reg.set_overrides(
        "host-a", {"dns_fail": "off", "kill_switch": "off"}))
    assert entry["overrides"] == {"kill_switch": "off"}
    _close(loop)


def test_no_resolver_no_pruning_backward_compat(tmp_path):
    # A registry constructed without a resolver (tests / standalone) never
    # prunes — set_overrides preserves the merged overrides verbatim.
    reg, loop = _reg(tmp_path, resolver=None)
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "off"}))
    assert entry["overrides"] == {"dns_fail": "off"}
    _close(loop)


def test_resolver_failure_keeps_overrides(tmp_path):
    # A resolver error must NEVER block a toggle — the merged overrides stay.
    def _boom(hn):
        raise RuntimeError("boom")
    reg, loop = _reg(tmp_path, resolver=_boom)
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "off"}))
    assert entry["overrides"] == {"dns_fail": "off"}
    _close(loop)


def test_existing_redundant_override_pruned_on_next_set(tmp_path):
    # A prior real override (kill_switch off, bucket on) becomes redundant if
    # the bucket later flips to off — the next set_overrides call (even for a
    # different flag) re-prunes the whole dict against the current bucket.
    reg, loop = _reg(tmp_path, resolver=lambda hn: {"kill_switch": ""})
    _run(loop, reg.set_overrides("host-a", {"kill_switch": "off"}))  # off==bucket off → pruned
    entry = _run(loop, reg.set_overrides("host-a", {"dns_fail": "on"}))
    # kill_switch was never stored (pruned first call); dns_fail kept.
    assert entry["overrides"] == {"dns_fail": "on"}
    _close(loop)


def test_pure_bucket_profile_excludes_username_overlay(tmp_path):
    # The pure bucket MUST be simulation.conf only — the [username] overlay from
    # user-overrides.conf is excluded so the WebUI mirror's own writes can't
    # make the bucket default reflect them (which would defeat the prune).
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    # [simulation] global dns_fail applies to EVERY bucket, so regardless of
    # which sX the hostname hashes to the pure bucket has dns_fail=off.
    (cfg / "simulation.conf").write_text(
        "[simulation]\ndns_fail=off\n", encoding="utf-8")
    # The [username] overlay would force dns_fail=on IF it were included.
    (cfg / "user-overrides.conf").write_text(
        "[someuser]\ndns_fail=on\n", encoding="utf-8")
    prof = sim_config.pure_bucket_profile("someuser-1", cfg)
    assert prof.get("dns_fail") == "off"   # NOT "on" — [username] excluded