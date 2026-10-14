"""World-entry template data recovered from the Sandstorm capture shipped in ``captures/``.

Full world entry needs a MemoryDump (opcode 0x000D) payload for the character.
The server only knows how to build its leading "core" fields; the remainder
(stats, inventory, spells, quests, ...) is taken from a captured dump. When the
operator-local ``phase3_data`` bundles are missing, this module reassembles the
segmented MemoryDump the Sandstorm server sent in ``from_sandstorm.pcapng``.
"""
from __future__ import annotations

import re
import struct
from functools import lru_cache
from pathlib import Path

from .protocol import MessageType, read_varuint

SANDSTORM_CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "from_sandstorm.pcapng"
GAME_PORT = 10070
PATCH_PORT = 7000
MEMORY_DUMP_OPCODE = 0x000D
MEMORY_DUMP_FIRST_MESSAGE = 5

_PCAPNG_SECTION_HEADER = 0x0A0D0D0A
_PCAPNG_ENHANCED_PACKET = 0x00000006


def _server_udp_payloads(path: Path) -> list[bytes]:
    data = path.read_bytes()
    endian = "<"
    offset = 0
    payloads: list[bytes] = []
    while offset + 12 <= len(data):
        block_type, block_len = struct.unpack_from(endian + "II", data, offset)
        if block_len < 12 or offset + block_len > len(data):
            break
        body = data[offset + 8 : offset + block_len - 4]
        offset += block_len
        if block_type == _PCAPNG_SECTION_HEADER:
            endian = "<" if struct.unpack_from("<I", body, 0)[0] == 0x1A2B3C4D else ">"
            continue
        if block_type != _PCAPNG_ENHANCED_PACKET or len(body) < 20:
            continue
        captured_len = struct.unpack_from(endian + "I", body, 12)[0]
        frame = body[20 : 20 + captured_len]
        if len(frame) < 14 + 20 + 8 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        if ip[9] != 17:
            continue
        ihl = (ip[0] & 0x0F) * 4
        udp = ip[ihl:]
        sport, _dport, length = struct.unpack_from("!HHH", udp, 0)
        if sport == GAME_PORT:
            payloads.append(udp[8:length])
    return payloads


def _tcp_segments(path: Path):
    """(source port, destination port, sequence number, payload) for every IPv4 TCP segment with data."""
    data = path.read_bytes()
    endian = "<"
    offset = 0
    while offset + 12 <= len(data):
        block_type, block_len = struct.unpack_from(endian + "II", data, offset)
        if block_len < 12 or offset + block_len > len(data):
            break
        body = data[offset + 8 : offset + block_len - 4]
        offset += block_len
        if block_type == _PCAPNG_SECTION_HEADER:
            endian = "<" if struct.unpack_from("<I", body, 0)[0] == 0x1A2B3C4D else ">"
            continue
        if block_type != _PCAPNG_ENHANCED_PACKET or len(body) < 20:
            continue
        captured_len = struct.unpack_from(endian + "I", body, 12)[0]
        frame = body[20 : 20 + captured_len]
        if len(frame) < 14 + 20 + 20 or frame[12:14] != b"\x08\x00" or frame[14 + 9] != 6:
            continue
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        total = struct.unpack_from("!H", ip, 2)[0]
        tcp = ip[ihl:total]
        sport, dport, seq = struct.unpack_from("!HHI", tcp, 0)
        payload = tcp[(tcp[12] >> 4) * 4 :]
        if payload:
            yield sport, dport, seq, payload


@lru_cache(maxsize=1)
def sandstorm_patch_files(path: Path = SANDSTORM_CAPTURE, port: int = PATCH_PORT) -> dict[str, bytes]:
    """Files the Sandstorm patch server sent in the capture, by request name (e.g. "ss-patch-6.dat.gz")."""
    if not path.is_file():
        return {}
    requests: dict[int, bytes] = {}
    responses: dict[int, dict[int, bytes]] = {}
    for sport, dport, seq, payload in _tcp_segments(path):
        if dport == port:
            requests[sport] = requests.get(sport, b"") + payload
        elif sport == port:
            responses.setdefault(dport, {})[seq] = payload
    files: dict[str, bytes] = {}
    for client_port, request in requests.items():
        line = request.split(b"\r\n", 1)[0].split()
        if len(line) < 2 or client_port not in responses:
            continue
        stream = b"".join(segment for _, segment in sorted(responses[client_port].items()))
        head, _, body = stream.partition(b"\r\n\r\n")
        length = re.search(rb"(?im)^content-length:\s*(\d+)", head)
        if not head.startswith(b"HTTP/1.1 200") or not length or len(body) < int(length.group(1)):
            continue
        name = line[1].decode("ascii", "replace").rsplit("/", 1)[-1]
        files[name] = body[: int(length.group(1))]
    return files


def _phase3_inner(packet: bytes) -> bytes | None:
    """Bundle body (flags, arrival, messages) of a server phase3 packet."""
    if len(packet) < 4 + 1 + 4 + 5 + 4:
        return None
    _flags, offset = read_varuint(packet, 4)
    return packet[offset + 4 + 5 : -4]


def _memory_dump_segments(inner: bytes) -> list[tuple[int, int, bytes]]:
    """(message type, message number, data) for the leading FA/FB messages of a bundle."""
    segments = []
    offset = 3
    while offset + 4 <= len(inner):
        message_type = inner[offset]
        if message_type not in (MessageType.SEGMENT_RELIABLE, MessageType.RELIABLE):
            break
        size = inner[offset + 1]
        offset += 2
        if size == 0xFF:
            size = struct.unpack_from("<H", inner, offset)[0]
            offset += 2
        (message,) = struct.unpack_from("<H", inner, offset)
        offset += 2
        segments.append((message_type, message, inner[offset : offset + size]))
        offset += size
    return segments


@lru_cache(maxsize=1)
def sandstorm_memory_dump_payload(path: Path = SANDSTORM_CAPTURE) -> bytes | None:
    """Reassembled MemoryDump payload (without the opcode), or None if unavailable."""
    if not path.is_file():
        return None
    pieces: dict[int, bytes] = {}
    last_message: int | None = None
    for packet in _server_udp_payloads(path):
        inner = _phase3_inner(packet)
        if not inner:
            continue
        for message_type, message, data in _memory_dump_segments(inner):
            if message == MEMORY_DUMP_FIRST_MESSAGE:
                if data[:2] != struct.pack("<H", MEMORY_DUMP_OPCODE):
                    continue
                data = data[2:]
            elif not pieces or message < MEMORY_DUMP_FIRST_MESSAGE:
                continue
            pieces.setdefault(message, data)
            if message_type == MessageType.RELIABLE and message > MEMORY_DUMP_FIRST_MESSAGE:
                last_message = message if last_message is None else min(last_message, message)
        if last_message is not None and all(
            m in pieces for m in range(MEMORY_DUMP_FIRST_MESSAGE, last_message + 1)
        ):
            return b"".join(pieces[m] for m in range(MEMORY_DUMP_FIRST_MESSAGE, last_message + 1))
    return None


def memory_dump_core_length(payload: bytes) -> int:
    """Length of the fields build_memory_dump_core writes, parsed from a captured dump."""
    offset = 1
    offset += 4 + struct.unpack_from("<i", payload, offset)[0]  # zone file
    _, offset = read_varuint(payload, offset)  # server id
    offset += 4 + struct.unpack_from("<i", payload, offset)[0]  # name
    for _ in range(5):
        _, offset = read_varuint(payload, offset)
    offset += 1
    for _ in range(5):
        _, offset = read_varuint(payload, offset)
    return offset + 6 * 4
