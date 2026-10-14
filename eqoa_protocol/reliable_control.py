"""phase3 reliable-control (0x87) packet — the seq/ack used during server-list
and in-world reliable messaging.

Fixes the server-select / world-list timeout: the client's list-phase reliable
controls are addressed to the DISCOVERY endpoint (0x73B0); the server must ack
them FROM 0x73B0 back to the client. Acking to 0xFFFE leaves the client's
reliable layer unacked, so the list times out after a few seconds.

Captured (client 0x0AD0; discovery 0x73B0; world 0xB991):
  C->S 22B  d00a b073 87 e0 01  d00a0200  23  0200 0100 0100  ...   seq=2 ack=1
  C->S 22B  d00a b073 87 e0 01  d00a0200  23  0300 0200 0100  ...   seq=3 ack=2
  S->C 21B  91b9 d00a 87 60      d10a0200  23  0500 0400 0300  ...   seq=5 ack=4

Layout (little-endian):
  0  2  src_endpoint
  2  2  dst_endpoint
  4  1  flag = 0x87
  5  1  dir  : 0xE0 to-server, 0x60 to-client  (bit 0x80 = "to server")
  6  1  0x01 bundle-count            [client frames only; server frames omit it]
  7  4  session_id (e.g. 0x00020AD0)
  11 1  op = 0x23
  12 2  seq    sender's own message number
  14 2  ack    highest seq received from the peer
  16 2  field  window / last-reliable id
  +4    checksum   EQOA custom checksum over all preceding bytes
"""

import struct
from dataclasses import dataclass

FLAG_RELIABLE = 0x87
DIR_TO_SERVER = 0xE0
DIR_TO_CLIENT = 0x60
OP_RELIABLE = 0x23


@dataclass
class ReliableControl:
    src_endpoint: int
    dst_endpoint: int
    to_server: bool
    session_id: int
    seq: int
    ack: int
    field: int
    raw: bytes


def parse_reliable_control(data: bytes) -> ReliableControl | None:
    """Parse an 0x87 reliable control (either direction), length-agnostic."""
    if len(data) < 21 or data[4] != FLAG_RELIABLE:
        return None
    src, dst = struct.unpack_from("<HH", data, 0)
    to_server = bool(data[5] & 0x80)
    off = 6 + (1 if to_server else 0)   # client frames carry an extra 0x01 byte
    session_id = struct.unpack_from("<I", data, off)[0]; off += 4
    if data[off] != OP_RELIABLE:
        return None
    off += 1
    seq, ack, field = struct.unpack_from("<HHH", data, off)
    return ReliableControl(src, dst, to_server, session_id, seq, ack, field, data)


def build_reliable_ack(ctrl: ReliableControl, server_seq: int,
                       checksum_fn, field: int | None = None) -> bytes:
    """Build the server's 0x87 reliable-ack FROM the endpoint the client
    addressed (ctrl.dst_endpoint) back to the client (ctrl.src_endpoint), with
    ack = the client's seq just received.

    `checksum_fn(body: bytes) -> int` is the project's existing EQOA checksum.
    """
    fld = ctrl.field if field is None else field
    body = struct.pack(
        "<HH B B I B HHH",
        ctrl.dst_endpoint, ctrl.src_endpoint,   # reply FROM targeted ep -> client
        FLAG_RELIABLE, DIR_TO_CLIENT,           # 0x87, 0x60 (no 0x01 on server frames)
        ctrl.session_id,
        OP_RELIABLE,
        server_seq, ctrl.seq, fld,              # ack = client's seq
    )
    return body + struct.pack("<I", checksum_fn(body) & 0xFFFFFFFF)
