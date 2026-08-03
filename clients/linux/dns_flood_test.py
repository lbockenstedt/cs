#!/usr/bin/env python3
"""Single-process DNS query generator — a TEST TOOL, not part of the simulation.

WHY THIS EXISTS
---------------
dns_fail.sh forks a `dig` per query, plus a `/bin/sleep` per iteration, plus a
subshell + `wc` for the in-flight check — roughly four processes per query. On a
2-vCPU sim client that makes the flood FORK-BOUND long before it is network-
bound: the achieved rate tops out well below the configured one, the process
table churns, and pushing the rate harder produces the stalls that look like
"it hangs when we send a large number at once".

This sends the same queries from ONE process with no forking at all, so it can
answer a question the shell version cannot: what rate can this client actually
sustain when the process model is not the limit?

It is deliberately NOT wired into simulation.sh. Run it by hand, compare its
achieved rate against dns_fail.sh, and decide from data whether replacing the
dig-per-query model is worth it.

WHAT IT DOES
------------
Builds minimal DNS A-record queries (stdlib only, no dnspython) and fires them
UDP fire-and-forget at the configured bad servers, exactly like dns_fail.sh does
with `dig +short ... >/dev/null` — the answer is discarded either way; the point
is that the query was made and failed.

Responses are drained non-blocking so the socket buffer cannot fill and start
dropping sends silently. Against unreachable servers there should be ~none; a
non-zero count means a "bad" server is actually answering, which is worth
knowing since it would mean the sim is not generating the failure it thinks.

PACING
------
Deadline-based with an accumulator rather than sleep-per-query: at 750/min a
per-query sleep is 80ms of syscall overhead per query, and at higher rates the
sleep granularity itself becomes the limit. Here the loop wakes on a fixed tick
and sends however many queries are due, so pacing stays accurate from 1/min to
tens of thousands/min with a constant ~200 wakeups/sec.

USAGE
-----
  # match the current sim config
  python3 dns_flood_test.py --rate 750 --duration 60

  # find the ceiling: no pacing, send as fast as the socket allows
  python3 dns_flood_test.py --rate 0 --duration 10

  # explicit targets
  python3 dns_flood_test.py --servers 10.252.0.1,172.16.252.1 --rate 2000 --duration 30
"""
import argparse
import os
import random
import socket
import struct
import sys
import time

# Defaults mirror configs/simulation.conf so a bare run is comparable to the sim.
DEFAULT_SERVERS = [
    "10.252.0.1", "172.16.252.1", "192.168.252.1",
    "172.31.201.129", "172.31.201.130", "172.31.201.131",
]
NAMES_FILE = "/usr/local/scripts/dns_fail.txt"
FALLBACK_NAMES = ["nonexistent.invalid", "bogus.invalid", "missing.invalid"]


def build_query(name: str, qid: int) -> bytes:
    """Minimal DNS A/IN query. Same wire request `dig <name> @<server>` sends."""
    # ID, flags=0x0100 (standard query, recursion desired), QD=1, AN/NS/AR=0
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(lbl)]) + lbl.encode("ascii", "ignore")
        for lbl in name.rstrip(".").split(".") if lbl
    ) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN


def load_names(path: str):
    try:
        with open(path) as fh:
            names = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        if names:
            return names
    except OSError:
        pass
    return list(FALLBACK_NAMES)


def drain(sock) -> int:
    """Non-blocking read of anything that came back.

    Not cosmetic: an unread socket buffer eventually makes sendto() block or
    fail, which would silently cap the send rate and look like a client that
    "cannot go faster" when it is really just not reading.
    """
    got = 0
    while True:
        try:
            sock.recv(4096)
            got += 1
        except (BlockingIOError, InterruptedError):
            return got
        except OSError:
            # ICMP port-unreachable surfaces here on connected UDP sockets;
            # for unconnected ones it is harmless. Either way, not fatal.
            return got


def main() -> int:
    ap = argparse.ArgumentParser(description="single-process DNS query generator (test tool)")
    ap.add_argument("--servers", default=",".join(DEFAULT_SERVERS),
                    help="comma-separated DNS servers to query")
    ap.add_argument("--names-file", default=NAMES_FILE,
                    help=f"file of names to look up (default {NAMES_FILE})")
    ap.add_argument("--rate", type=int, default=750,
                    help="queries per MINUTE; 0 = unpaced (find the ceiling)")
    ap.add_argument("--duration", type=int, default=60, help="seconds to run")
    ap.add_argument("--tick", type=float, default=0.005,
                    help="pacing wakeup interval in seconds (default 5ms)")
    ap.add_argument("--quiet", action="store_true", help="summary line only")
    args = ap.parse_args()

    servers = [s.strip() for s in args.servers.split(",") if s.strip()]
    if not servers:
        print("no servers given", file=sys.stderr)
        return 2
    names = load_names(args.names_file)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    # A large send buffer keeps a burst from blocking on a slow link; we never
    # want the SOCKET to be what limits the measured rate.
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    except OSError:
        pass

    sent = errors = replies = 0
    start = time.perf_counter()
    end = start + args.duration
    per_sec = args.rate / 60.0 if args.rate > 0 else 0.0
    qid = random.randrange(1, 65535)
    si = ni = 0

    if not args.quiet:
        pace = f"{args.rate}/min" if args.rate > 0 else "UNPACED (ceiling test)"
        print(f"dns_flood_test: {pace} for {args.duration}s "
              f"across {len(servers)} servers x {len(names)} names, pid {os.getpid()}",
              flush=True)

    try:
        while True:
            now = time.perf_counter()
            if now >= end:
                break
            if args.rate > 0:
                # Derive the backlog from ELAPSED TIME, not by accumulating a
                # per-tick quota. time.sleep() reliably overshoots (a 5ms sleep
                # costs ~6ms), so a per-tick accumulator silently runs ~20% slow
                # and every measurement taken with it is wrong. Comparing total
                # sent against what elapsed time says we owe is self-correcting:
                # a late tick simply sends more.
                due = int(per_sec * (now - start)) - sent
                if due < 0:
                    due = 0
            else:
                due = 256  # unpaced: send in chunks, then drain, then repeat

            for _ in range(due):
                server = servers[si % len(servers)]
                name = names[ni % len(names)]
                si += 1
                ni += 1
                qid = (qid + 1) & 0xFFFF
                try:
                    sock.sendto(build_query(name, qid), (server, 53))
                    sent += 1
                except BlockingIOError:
                    # Send buffer full — the kernel is the limit, not us. Back
                    # off a tick rather than spinning; counted so the summary
                    # shows the client hit a real network-side ceiling.
                    errors += 1
                    break
                except OSError:
                    errors += 1

            replies += drain(sock)

            if args.rate > 0:
                # Sleep the remainder of this tick, never past the end time.
                nxt = min(now + args.tick, end)
                delay = nxt - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        replies += drain(sock)
        sock.close()

    elapsed = time.perf_counter() - start
    rate_min = (sent / elapsed) * 60.0 if elapsed > 0 else 0.0
    print(f"sent={sent} errors={errors} replies={replies} "
          f"elapsed={elapsed:.1f}s achieved={rate_min:.0f}/min ({sent/elapsed:.0f}/s)",
          flush=True)
    if replies:
        print(f"NOTE: {replies} server(s) ANSWERED — those targets are reachable, "
              f"so those queries are not generating DNS failures.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
