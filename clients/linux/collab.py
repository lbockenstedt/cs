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
# argparse parses the flags simulation.sh passes in (server, app, bandwidth, ...).
import argparse
# os provides random payload bytes and checks whether a replay capture file exists.
import os
# random is imported but not currently used by this script.
import random
# signal lets SIGTERM/SIGINT stop the send loop cleanly instead of killing it mid-send.
import signal
# socket creates the plain UDP sockets that carry the fake media.
import socket
# sys gives stderr for errors and the process exit code.
import sys
# time paces the datagrams and enforces the --time limit.
import time
# urllib.request downloads the optional replay capture from the hub.
import urllib.request

# App -> (ports, payload bytes, default bandwidth bps)
# Each profile uses the platform's real ports, a typical 1280-byte RTP-sized payload and a
# realistic per-call bitrate so DPI/NetFlow tools classify the flow like real traffic.
APP_PROFILES = {
    "teams": ([3478, 3481, 3479], 1280, 1_200_000),
    "zoom":  ([8801, 8802, 8803], 1280, 1_500_000),
    "webex": ([9000, 5004, 5006], 1280, 1_000_000),
}

# Global "keep going" flag; the signal handler flips it to False to end the send loops.
_RUNNING = True


def _handle_sig(_signum, _frame):
    """Signal handler: ask the send loops to stop at their next iteration."""
    # Must be declared global so the assignment changes the module flag, not a local copy.
    global _RUNNING
    _RUNNING = False


def parse_bw(s):
    """'1.5M' / '500k' / '2000000' -> bits per second."""
    # Normalise the text; an empty value means "not given", so the caller uses the app default.
    s = (s or "").strip().lower()
    if not s:
        return None
    # A trailing k/m/g suffix multiplies the number by a thousand, million or billion.
    mult = 1
    if s[-1] in ("k", "m", "g"):
        mult = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[s[-1]]
        s = s[:-1]
    # float() allows decimals like "1.5" before converting to whole bits per second.
    return int(float(s) * mult)


def _fetch_pcap(url, dest, timeout=20):
    """Download the replay capture from the hub to ``dest``. Returns dest on
    success, None on any failure (caller falls back to synthetic)."""
    # Any network or file error is caught below so the caller can fall back to synthetic traffic.
    try:
        # Fetch the whole capture into memory with an identifying User-Agent.
        req = urllib.request.Request(url, headers={"User-Agent": "collab-sim"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = r.read()
        # An empty response is useless, so treat it like a failure.
        if not data:
            return None
        # Write the bytes to the destination path and return that path.
        with open(dest, "wb") as f:
            f.write(data)
        return dest
    # Broad catch on purpose: whatever went wrong, synthetic traffic is still a usable fallback.
    except Exception as e:  # noqa: BLE001
        print(f"collab: pcap download failed ({e}) — falling back to synthetic",
              file=sys.stderr, flush=True)
        return None


def _replay_c2s(server, pcap_path, run_time):
    """Replay the capture's client->server datagrams at the hub sink, looped,
    preserving inter-packet timing. The hub sink replays the matching
    server->client frames back. Returns True if it ran a real replay, False if
    the capture was unusable (caller falls back to synthetic)."""
    # Load the pcap helper lazily and split the capture into client->server and
    # server->client packets; any parse error means we cannot replay.
    try:
        import collab_pcap  # noqa: PLC0415
        packets = collab_pcap.parse_udp_packets(pcap_path)
        _client, _server, c2s, _s2c = collab_pcap.split_directions(packets)
    # Unreadable capture: log it and tell the caller to use synthetic mode instead.
    except Exception as e:  # noqa: BLE001
        print(f"collab: pcap parse failed ({e}) — falling back to synthetic",
              file=sys.stderr, flush=True)
        return False
    # The capture parsed but has no client-side UDP media to send, so replay is pointless.
    if not c2s:
        print("collab: capture has no client->server UDP media — falling back to synthetic",
              file=sys.stderr, flush=True)
        return False

    # Collect the distinct destination ports and open one UDP socket per port so each flow is
    # a separate 5-tuple; total bytes is only reported for diagnostics.
    ports = sorted({port for _, port, _ in c2s})
    socks = {p: socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for p in ports}
    total_bytes = sum(len(pl) for _, _, pl in c2s)
    # Announce the replay so the sim log shows what is being sent.
    print(f"collab: REPLAY server={server} pkts={len(c2s)} ports={ports} "
          f"bytes={total_bytes} time={run_time or 'until-killed'} (looped)", flush=True)

    # Track start time, packets sent and full passes over the capture.
    start = time.monotonic()
    sent = 0
    loops = 0
    # Loop over the capture repeatedly until stopped by a signal or the --time limit.
    try:
        while _RUNNING and (run_time == 0 or time.monotonic() - start < run_time):
            # Send each packet after its recorded delay so the timing matches the original call.
            for delta, port, payload in c2s:
                # Re-check the stop conditions per packet so a long capture stops promptly.
                if not (_RUNNING and (run_time == 0 or time.monotonic() - start < run_time)):
                    break
                # Sleep the recorded gap, capped at 1 s so a long pause in the capture cannot stall
                # us.
                if delta > 0:
                    time.sleep(min(delta, 1.0))
                # Send the payload to the hub sink on the same port the original packet used.
                try:
                    socks[port].sendto(payload, (server, port))
                    sent += 1
                except OSError:
                    # Transient network errors (route not up yet) should not end the replay.
                    pass
            # One full pass of the capture is done; count it and start again.
            loops += 1
    finally:
        # Always close sockets and print a summary, even if interrupted.
        for s in socks.values():
            s.close()
        dur = time.monotonic() - start
        print(f"collab: replay sent {sent} datagrams in {dur:.1f}s ({loops} loop(s))",
              flush=True)
    # A real replay ran, so the caller should not fall back to synthetic mode.
    return True


def main():
    """Parse flags, then either replay a capture or blast synthetic UDP at the hub sink.

    Returns: process exit code (0 ok, 1 bad port list).
    """
    # Command-line flags; simulation.sh fills these from the collab ini settings.
    ap = argparse.ArgumentParser(description="Collab UDP traffic generator")
    ap.add_argument("--server", required=True, help="hub collab sink IP")
    ap.add_argument("--app", default="teams", choices=sorted(APP_PROFILES))
    ap.add_argument("--bw", default="", help="bandwidth e.g. 1.5M, 500k (overrides app default)")
    ap.add_argument("--time", type=int, default=0, help="seconds to run (0 = until killed)")
    ap.add_argument("--ports", default="", help="comma-sep port override, e.g. 3478,3481")
    ap.add_argument("--pcap", default="", help="local replay capture path")
    ap.add_argument("--pcap-url", default="", help="hub URL to download the replay capture from")
    args = ap.parse_args()

    # Stop gracefully when the sim kills us (SIGTERM) or an operator presses Ctrl-C (SIGINT).
    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    # Prefer high-fidelity pcap replay when a capture is available; fall back to
    # synthetic random-payload blasting so the sim always runs.
    pcap_path = args.pcap or ""
    # No local capture but a hub URL was given: download it to a scratch path first.
    if not pcap_path and args.pcap_url:
        pcap_path = _fetch_pcap(args.pcap_url, "/tmp/collab_replay.pcap") or ""
    # If we have a capture file, try replaying it; success means we are finished.
    if pcap_path and os.path.isfile(pcap_path):
        if _replay_c2s(args.server, pcap_path, args.time):
            return 0

    # Synthetic mode: start from the chosen app profile's ports, payload size and bandwidth.
    ports, payload_size, default_bw = APP_PROFILES[args.app]
    # An explicit --ports list replaces the profile's ports.
    if args.ports:
        ports = [int(p) for p in args.ports.split(",") if p.strip()]
    # Use --bw if given, otherwise the app's default bitrate.
    bw = parse_bw(args.bw) or default_bw
    # An empty port list would mean nothing to send, so report it and exit with code 1.
    if not ports:
        print("collab: no ports", file=sys.stderr)
        return 1

    # One random payload reused for every datagram; its content does not matter, only size.
    payload = os.urandom(payload_size)
    # datagrams/sec needed to hit bw with this payload size
    dgram_hz = max(1.0, bw / (payload_size * 8))
    # Seconds to wait between datagrams.
    interval = 1.0 / dgram_hz

    # One UDP socket per port so each 5-tuple is distinct on the wire
    # (monitoring keys on src/dst ip+port + proto; distinct dst ports => distinct flows).
    socks = {p: socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for p in ports}
    # Log the effective settings so the sim log shows what traffic is running.
    print(f"collab: app={args.app} server={args.server} ports={ports} "
          f"bw={bw}bps payload={payload_size}B interval={interval*1000:.1f}ms "
          f"time={args.time or 'until-killed'}", flush=True)

    # Track start time, which port is next and how many datagrams were sent.
    start = time.monotonic()
    port_idx = 0
    sent = 0
    # Send until stopped by a signal or the --time limit.
    try:
        while _RUNNING and (args.time == 0 or time.monotonic() - start < args.time):
            # Round-robin over the ports so bandwidth is shared evenly across them.
            port = ports[port_idx % len(ports)]
            try:
                socks[port].sendto(payload, (args.server, port))
                sent += 1
            except OSError:
                # transient (route not up yet, etc.) — keep going, don't kill the run
                pass
            # Move to the next port and wait to keep the target bitrate.
            port_idx += 1
            time.sleep(interval)
    finally:
        # Always close the sockets and log achieved throughput, even when interrupted.
        for s in socks.values():
            s.close()
        dur = time.monotonic() - start
        print(f"collab: sent {sent} datagrams in {dur:.1f}s "
              f"(~{sent * payload_size * 8 / max(dur, 1e-6) / 1e6:.2f} Mbps across {len(ports)} ports)",
              flush=True)
    return 0


# Run main() only when executed as a script and exit with its return code.
if __name__ == "__main__":
    sys.exit(main())