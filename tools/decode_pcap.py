#!/usr/bin/env python3
"""Decode an EQOA phase3 capture: list every UDP :10070 datagram with its
direction, endpoints, flag, classification, and (for controls) decoded fields.

Usage:
    python tools/decode_pcap.py tests/data/from_sandstorm.pcapng [--port 10070]

Requires scapy (pip install scapy). Pure-stdlib fallback reads a raw hex dump
file (one payload per line) if given a .hex file instead.
"""
import argparse
import sys

from eqoa_protocol import (
    classify, peek_header, endpoint_name,
    try_parse_short_control, parse_reliable_control,
)


def iter_payloads_pcap(path, port):
    from scapy.all import rdpcap, UDP, IP, Raw
    for p in rdpcap(path):
        if UDP in p and (p[UDP].sport == port or p[UDP].dport == port):
            raw = bytes(p[Raw].load) if Raw in p else b""
            yield float(p.time), p[IP].src, p[IP].dst, p[UDP].dport == port, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("--port", type=int, default=10070)
    args = ap.parse_args()

    rows = list(iter_payloads_pcap(args.pcap, args.port))
    if not rows:
        print("no UDP :%d packets found" % args.port); return 1
    t0 = rows[0][0]
    print(f"{'t':>6} {'dir':>4} {'len':>4} {'src_ep':>8} {'dst_ep':>8} {'kind':>16}  detail")
    for t, src, dst, to_server, raw in rows:
        hdr = peek_header(raw)
        if hdr is None:
            print(f"{t-t0:6.1f} {'?':>4} {len(raw):>4}  (short)"); continue
        s, d, flag, ts = hdr
        kind = classify(raw)
        detail = ""
        if kind == "short_control":
            c = try_parse_short_control(raw)
            detail = f"word=0x{c.word1:08x} sub=0x{c.subtype:02x}"
        elif kind == "reliable_control":
            c = parse_reliable_control(raw)
            detail = f"seq={c.seq} ack={c.ack} field={c.field} sess=0x{c.session_id:08x}"
        dirn = "C->S" if ts else "S->C"
        print(f"{t-t0:6.1f} {dirn:>4} {len(raw):>4} {endpoint_name(s):>8} "
              f"{endpoint_name(d):>8} {kind:>16}  {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
