"""Common phase3 datagram front-matter and a single dispatch entry point.

Every phase3 UDP datagram begins with:
  0  2  src_endpoint (LE u16)
  2  2  dst_endpoint (LE u16)
  4  1  flag byte     0x80 bare control | 0x87 reliable control | others = data bundle
  5  1  dir byte      high bit 0x80 => to-server (0xE0), clear => to-client (0x60)
and ends with a 4-byte EQOA checksum trailer.

`classify()` tells you what you're holding without consuming it; `handle()` is a
drop-in guard for a server recv loop that deals with the two control shapes and
leaves everything else to the caller's bundle parser.
"""

import struct

from .short_control import try_parse_short_control, FLAG_BARE
from .reliable_control import parse_reliable_control, FLAG_RELIABLE


def peek_header(data: bytes):
    """Return (src_endpoint, dst_endpoint, flag, to_server) or None if too short."""
    if len(data) < 6:
        return None
    src, dst = struct.unpack_from("<HH", data, 0)
    flag = data[4]
    to_server = bool(data[5] & 0x80)
    return src, dst, flag, to_server


def classify(data: bytes) -> str:
    """One of: 'short_control', 'reliable_control', 'data_bundle', 'unknown'."""
    hdr = peek_header(data)
    if hdr is None:
        return "unknown"
    flag = hdr[2]
    if flag == FLAG_BARE and try_parse_short_control(data) is not None:
        return "short_control"
    if flag == FLAG_RELIABLE and parse_reliable_control(data) is not None:
        return "reliable_control"
    return "data_bundle"


def handle(data, addr, session, checksum_fn, logger=None):
    """Drop-in guard for a phase3 UDP recv loop.

    Handles the two control shapes and returns an optional reply (bytes or None)
    plus a 'handled' flag:
        reply, handled = handle(data, addr, session, checksum_fn)
        if handled:
            if reply: sock.sendto(reply, addr)
            continue   # do NOT run the bundle parser
    """
    kind = classify(data)

    if kind == "short_control":
        import time
        ctrl = try_parse_short_control(data)
        session.phase3_last_client_recv_monotonic = time.monotonic()
        session.last_control_word = ctrl.word2
        if logger:
            logger.info("phase3 short-control %s dst=0x%04x (ack, no reply)",
                        addr, ctrl.dst_endpoint)
        return None, True   # server sends nothing; client drives on

    if kind == "reliable_control":
        ctrl = parse_reliable_control(data)
        if not ctrl.to_server:
            return None, True   # a server->client frame echoed back; ignore
        server_seq = getattr(session, "phase3_next_message", 1)
        session.phase3_next_message = server_seq + 1
        from .reliable_control import build_reliable_ack
        reply = build_reliable_ack(ctrl, server_seq, checksum_fn)
        if logger:
            logger.info("phase3 reliable-control %s dst=0x%04x seq=%d ack=%d -> ack",
                        addr, ctrl.dst_endpoint, ctrl.seq, ctrl.ack)
        return reply, True

    return None, False
