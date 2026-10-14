"""phase3 bare-control (0x80) packet — the 19-byte endpoint-ack / phase-transition.

Fixes the "buffer of at least 20 bytes ... actual 19" parse failure: the packet
is complete and carries NO message bundle, so the generic bundle parser must not
run on it.

Captured (client 0x0AD0; discovery 0x73B0; world 0xB991):
  C->S 19B  d00a b073 80 e0 14  d00a0200 d00a0200  e2ca4c98   (-> discovery)
  C->S 19B  d00a 91b9 80 e0 14  d10a0200 d10a0200  180f2138   (-> world)

Layout (little-endian):
  0  2  src_endpoint
  2  2  dst_endpoint
  4  1  flag = 0x80          bare control, no bundle
  5  1  dir  = 0xE0          (bit 0x80 => to-server)
  6  1  subtype = 0x14
  7  4  word1 (session/instance, e.g. 0x00020AD0)
  11 4  word2 (same; ack/seq pair)
  15 4  checksum             EQOA custom checksum over bytes[0:15]
"""

import struct
from dataclasses import dataclass

FLAG_BARE = 0x80
SHORT_CONTROL_LEN = 19


@dataclass
class ShortControl:
    src_endpoint: int
    dst_endpoint: int
    subtype: int
    word1: int
    word2: int
    checksum: int
    raw: bytes


def try_parse_short_control(data: bytes) -> ShortControl | None:
    """Return a ShortControl if `data` is a 0x80 bare-control packet, else None.
    Call this BEFORE the general phase3 bundle parser; a non-None result means
    the datagram is fully handled."""
    if len(data) != SHORT_CONTROL_LEN or data[4] != FLAG_BARE:
        return None
    src, dst = struct.unpack_from("<HH", data, 0)
    subtype = data[6]
    word1, word2 = struct.unpack_from("<II", data, 7)
    (checksum,) = struct.unpack_from("<I", data, 15)
    return ShortControl(src, dst, subtype, word1, word2, checksum, data)
