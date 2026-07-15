#!/usr/bin/env python3
"""Collaboration-app UDP traffic generator (Teams / Zoom / WebEx).

Launched via `nohup collab.sh` from simulation.sh, exactly like iperf.sh /
dns_fail.sh / dhcp_fail.sh. The hub runs a matching UDP sink
(lm-collab-sink) bound to its LAN IP on these same ports; this sender blasts
raw UDP at it over the wired/USB network path — the actual media NEVER rides
the WebSocket/API control plane (only the on/off + config does, via the
usual ini knobs pushed through the hub).

WHY raw UDP and not `iperf3 -u`: iperf3 negotiates its UDP data port over a
TCP control channel, so the media never lands on the port you ask for (e.g.
3478). That defeats the purpose here — we want DPI/NetFlow to see a flow on
the *real* platform ports. A plain UDP sender puts datagrams on exactly the
ports listed below, at the requested bandwidth, with no handshake.

Per-app flow profiles (port set + payload size + default bw). The ports are
the real media/control ports each platform uses, so a monitor classifying by
5-tuple sees the right signature:

    teams  : 3478, 3481, 3479  (STUN + media)   default 1.2M
    zoom   : 8801, 8802, 8803   (media)         default 1.5M
    webex  : 9000, 5004, 5006  (SRTP media)    default 1.0M

Bandwidth is shared across the port set (round-robin per datagram). No root
required — sending TO any port is unprivileged; only binding <1024 or raw
sockets need root, and we do neither (mirror of dhcp_fire.py).
"""
import argparse
import os
import random
import signal
import socket
import sys
import time

# App -> (ports, payload bytes, default bandwidth bps)
APP_PROFILES = {
    "teams": ([3478, 3481, 3479], 1280, 1_200_000),
    "zoom":  ([8801, 8802, 8803], 1280, 1_500_000),
    "webex": ([9000, 5004, 5006], 1280, 1_000_000),
}

_RUNNING = True


def _handle_sig(_signum, _frame):
    global _RUNNING
    _RUNNING = False


def parse_bw(s):
    """'1.5M' / '500k' / '2000000' -> bits per second."""
    s = (s or "").strip().lower()
    if not s:
        return None
    mult = 1
    if s[-1] in ("k", "m", "g"):
        mult = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[s[-1]]
        s = s[:-1]
    return int(float(s) * mult)


def main():
    ap = argparse.ArgumentParser(description="Collab UDP traffic generator")
    ap.add_argument("--server", required=True, help="hub collab sink IP")
    ap.add_argument("--app", default="teams", choices=sorted(APP_PROFILES))
    ap.add_argument("--bw", default="", help="bandwidth e.g. 1.5M, 500k (overrides app default)")
    ap.add_argument("--time", type=int, default=0, help="seconds to run (0 = until killed)")
    ap.add_argument("--ports", default="", help="comma-sep port override, e.g. 3478,3481")
    args = ap.parse_args()

    ports, payload_size, default_bw = APP_PROFILES[args.app]
    if args.ports:
        ports = [int(p) for p in args.ports.split(",") if p.strip()]
    bw = parse_bw(args.bw) or default_bw
    if not ports:
        print("collab: no ports", file=sys.stderr)
        return 1

    payload = os.urandom(payload_size)
    # datagrams/sec needed to hit bw with this payload size
    dgram_hz = max(1.0, bw / (payload_size * 8))
    interval = 1.0 / dgram_hz

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    # One UDP socket per port so each 5-tuple is distinct on the wire
    # (monitoring keys on src/dst ip+port + proto; distinct dst ports => distinct flows).
    socks = {p: socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for p in ports}
    print(f"collab: app={args.app} server={args.server} ports={ports} "
          f"bw={bw}bps payload={payload_size}B interval={interval*1000:.1f}ms "
          f"time={args.time or 'until-killed'}", flush=True)

    start = time.monotonic()
    port_idx = 0
    sent = 0
    try:
        while _RUNNING and (args.time == 0 or time.monotonic() - start < args.time):
            port = ports[port_idx % len(ports)]
            try:
                socks[port].sendto(payload, (args.server, port))
                sent += 1
            except OSError:
                # transient (route not up yet, etc.) — keep going, don't kill the run
                pass
            port_idx += 1
            time.sleep(interval)
    finally:
        for s in socks.values():
            s.close()
        dur = time.monotonic() - start
        print(f"collab: sent {sent} datagrams in {dur:.1f}s "
              f"(~{sent * payload_size * 8 / max(dur, 1e-6) / 1e6:.2f} Mbps across {len(ports)} ports)",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())