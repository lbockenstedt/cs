#!/usr/bin/env python3
"""Fire a single DHCPDISCOVER at a chosen server with a chosen identity.

Fire-and-forget: we SEND a well-formed DHCP DISCOVER and do NOT wait for a
reply — the failure is the point (mirror of dns_fail.sh's background digs).
dhcp_fail.sh calls this twice per attempt:

  * Request A -> the REAL (detected) DHCP server, carrying a FORGED identity
    (00:01:00:00:<o5>:<o6>). The good server rejects/ignores the unknown id.
  * Request B -> a dead server (default 10.10.10.10), carrying the REAL mac.
    Nothing responds (timeout).

The identity MAC is written into BOTH the BOOTP `chaddr` field AND DHCP option
61 (client-identifier), so whichever field the server/NAC keys on sees the same
identity.

No root required: this is a plain UDP datagram to <dst>:67 from an ephemeral
source port. Sending TO port 67 is allowed for unprivileged processes; only
binding to ports <1024 or raw sockets need root, and we do neither. The kernel
routes the unicast to the dst (real server on-link, or 10.10.10.10 via the
default gateway). SO_BINDTODEVICE is attempted best-effort to pin the egress
interface but is skipped silently without root (kernel then routes by dst).
"""
import argparse
import os
import socket
import struct
import sys

MAGIC = b"\x63\x82\x53\x63"  # DHCP magic cookie (RFC 1497)


def mac_bytes(mac: str) -> bytes:
    parts = mac.split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid mac: {mac!r}")
    return bytes(int(p, 16) for p in parts)


def build_discover(xid: int, mac_b: bytes) -> bytes:
    """Build a 300-byte DHCPDISCOVER (BOOTREQUEST) packet.

    chaddr and option-61 both carry mac_b so the identity is consistent
    regardless of which field the server/NAC inspects.
    """
    chaddr = mac_b + b"\x00" * 10  # 16-byte hardware address field
    # op=1 BOOTREQUEST, htype=1 ethernet, hlen=6, hops=0, xid, secs=0, flags=0
    pkt = struct.pack("!BBBBIHH", 1, 1, 6, 0, xid, 0, 0)
    pkt += b"\x00" * 4  # ciaddr
    pkt += b"\x00" * 4  # yiaddr
    pkt += b"\x00" * 4  # siaddr
    pkt += b"\x00" * 4  # giaddr
    pkt += chaddr       # 16
    pkt += b"\x00" * 64   # sname
    pkt += b"\x00" * 128  # file
    pkt += MAGIC
    # option 53: DHCP Message Type = DISCOVER (1)
    pkt += b"\x35\x01\x01"
    # option 61: client-identifier = hw-type 1 (ethernet) + mac
    pkt += b"\x3d\x07\x01" + mac_b
    # option 55: parameter request list — subnet mask, router, dns, domain
    pkt += b"\x37\x04\x01\x03\x06\x0f"
    pkt += b"\xff"  # END
    # Pad to the BOOTP minimum (300B) so relay agents / servers are happy.
    if len(pkt) < 300:
        pkt += b"\x00" * (300 - len(pkt))
    return pkt


def main() -> int:
    ap = argparse.ArgumentParser(description="Fire one DHCPDISCOVER at a server.")
    ap.add_argument("--iface", required=True, help="egress interface (best-effort pin)")
    ap.add_argument("--dst", required=True, help="DHCP server IP to send to")
    ap.add_argument("--mac", required=True, help="identity MAC (chaddr + opt-61)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        mac_b = mac_bytes(args.mac)
    except ValueError as e:
        print(f"dhcp_fire: {e}", file=sys.stderr)
        return 2

    xid = int.from_bytes(os.urandom(4), "big")
    pkt = build_discover(xid, mac_b)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Pin egress to the chosen interface so wifi traffic leaves wlan0, not
        # a wired default. Best-effort: needs root, skip silently without it.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                         (args.iface + "\0").encode())
        except (PermissionError, OSError):
            pass
        s.sendto(pkt, (args.dst, 67))
        if args.verbose:
            print(f"sent DHCPDISCOVER xid={xid:08x} -> {args.dst}:67 "
                  f"id={args.mac} iface={args.iface} ({len(pkt)}B)")
    except OSError as e:
        if args.verbose:
            print(f"dhcp_fire: send -> {args.dst} failed: {e}", file=sys.stderr)
        return 1
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())