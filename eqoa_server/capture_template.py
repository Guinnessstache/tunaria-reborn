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

from .protocol import MessageType, _read_zigzag, read_varuint

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


def _skip_string(payload: bytes, offset: int, char_size: int = 2) -> int:
    return offset + 4 + char_size * struct.unpack_from("<i", payload, offset)[0]


def _skip_item(payload: bytes, offset: int) -> int:
    for _ in range(4):  # stack, remaining HP, charges, equip location
        _, offset = _read_zigzag(payload, offset)
    offset += 1 + 4  # location, server key
    for _ in range(23):  # item id, cost, ... craft
        _, offset = read_varuint(payload, offset)
    offset = _skip_string(payload, _skip_string(payload, offset))  # name, description
    stats, offset = _read_zigzag(payload, offset)
    for _ in range(2 * stats):
        _, offset = read_varuint(payload, offset)
    return offset


def _skip_spell(payload: bytes, offset: int) -> int:
    for _ in range(9):
        _, offset = read_varuint(payload, offset)
    offset += 2  # range (half float)
    for _ in range(7):
        _, offset = read_varuint(payload, offset)
    return _skip_string(payload, _skip_string(payload, offset)) + 1


EMPTY_ITEM_LIST = b"\x00" + bytes(4)  # zigzag count 0, int32 count 0


def memory_dump_without_belongings(
    payload: bytes,
    core_length: int,
    inventory: bytes = EMPTY_ITEM_LIST,
    spells: bytes = b"\x00",
) -> bytes:
    """A captured MemoryDump after its core, with the captured character's quests,
    inventory, bank and spells replaced (by ``inventory`` and ``spells``, already
    encoded; empty by default). Hotkeys (the default emote / chat set) and the
    trailing stat block are kept. Layout as in ReturnHome's ServerMemoryDump.
    """
    offset = core_length
    hotkeys, offset = _read_zigzag(payload, offset)
    for _ in range(hotkeys):
        _, offset = read_varuint(payload, offset)  # direction
        for _ in range(4):
            offset = _skip_string(payload, _skip_string(payload, offset + 1))  # index, label, message
    hotkeys_end = offset
    offset += 4 + 4  # two unknown int32
    quests = struct.unpack_from("<i", payload, offset)[0]
    offset += 4
    for _ in range(quests):
        offset = _skip_string(payload, offset)
    for _ in range(2):  # inventory, then bank
        count, offset = _read_zigzag(payload, offset)
        offset += 4
        for _ in range(count):
            offset = _skip_item(payload, offset)
    auctions = payload[offset : offset + 4]  # end of bank, buying and selling auction counts
    offset += 4
    captured_spells, offset = _read_zigzag(payload, offset)
    for _ in range(captured_spells):
        offset = _skip_spell(payload, offset)
    if offset > len(payload):
        raise ValueError("MemoryDump template is shorter than its layout")
    return (
        payload[core_length:hotkeys_end]
        + bytes(12)  # two unknown int32, no quests
        + inventory
        + EMPTY_ITEM_LIST  # bank
        + auctions
        + spells
        + payload[offset:]
    )


@lru_cache(maxsize=1)
def sandstorm_entity_updates(path: Path = SANDSTORM_CAPTURE) -> dict[int, bytes]:
    """First full object update the Sandstorm server sent on each entity channel (1-0x17)."""
    from .protocol import rle_zero_decode

    found: dict[int, bytes] = {}
    if not path.is_file():
        return found
    for packet in _server_udp_payloads(path):
        inner = _phase3_inner(packet)
        if not inner or len(inner) < 4:
            continue
        offset = 3 + (4 if inner[0] & 0x03 else 0) + (4 if inner[0] & 0x40 else 0)
        while offset + 4 < len(inner):
            channel = inner[offset]
            if channel > 0x17:
                if channel not in (0xF9, 0xFA, 0xFB, 0xFC):
                    break
                size, start = inner[offset + 1], offset + 2
                if size == 0xFF:
                    size, start = struct.unpack_from("<H", inner, start)[0], start + 2
                offset = start + (0 if channel == 0xFC else 2) + size
                continue
            reference = inner[offset + 4 + (2 if inner[offset + 1] == 0xFF else 0)]
            start = offset + 5 + (2 if inner[offset + 1] == 0xFF else 0)
            end = start
            while end < len(inner):  # skip the run-length data
                control = inner[end]
                end += 1
                if control == 0:
                    break
                end += (control & 0x7F) + 1 if control & 0x80 else control >> 4
            if reference == 0 and channel and channel not in found:
                found[channel] = rle_zero_decode(inner[start:end])
            offset = end
    return found


def memory_dump_spells(payload: bytes) -> list[tuple[int, str]]:
    """(spell id, name) of the spells in a captured MemoryDump, in spell book order."""
    offset = memory_dump_core_length(payload)
    hotkeys, offset = _read_zigzag(payload, offset)
    for _ in range(hotkeys):
        _, offset = read_varuint(payload, offset)
        for _ in range(4):
            offset = _skip_string(payload, _skip_string(payload, offset + 1))
    offset += 8
    quests = struct.unpack_from("<i", payload, offset)[0]
    offset += 4
    for _ in range(quests):
        offset = _skip_string(payload, offset)
    for _ in range(2):
        count, offset = _read_zigzag(payload, offset)
        offset += 4
        for _ in range(count):
            offset = _skip_item(payload, offset)
    offset += 4
    count, offset = _read_zigzag(payload, offset)
    spells = []
    for _ in range(count):
        spell_id, start = _read_zigzag(payload, offset)
        position = start
        for _ in range(8):
            _, position = read_varuint(payload, position)
        position += 2
        for _ in range(7):
            _, position = read_varuint(payload, position)
        length = struct.unpack_from("<i", payload, position)[0]
        spells.append((spell_id, payload[position + 4:position + 4 + 2 * length].decode("utf-16-le", "replace")))
        offset = _skip_spell(payload, offset)
    return spells
