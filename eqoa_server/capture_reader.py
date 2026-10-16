"""Read the game's reliable messages out of a packet capture (pcapng or pcap), both ways.

Client packets go through protocol.parse_udp_packet, as the server reads them. Server
packets are read here: header, bundle, then messages: 0xFB a whole reliable message (u16
number, then opcode and payload), 0xFA a piece of a segmented one (the pieces join up to
the next 0xFB), 0xFC unreliable, 0xF9 ping, and state channels 0x00-0x17 / 0x40-0x43
(u16 sequence, xor byte, zero-RLE data up to a 0 byte). Server packets carry a session id
varint after the header; when that reading doesn't end on the CRC, it's read without.
Retransmitted messages (same number) count once.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .protocol import PacketBundleFlags, PacketHeaderFlags, parse_udp_packet, read_varuint

GAME_PORT = 10070


@dataclass(frozen=True)
class CapturedMessage:
    time: float  # seconds from the first game packet
    from_client: bool
    number: int | None
    opcode: int
    payload: bytes


def udp_packets(path: Path) -> list[tuple[float, str, int, str, int, bytes]]:
    """(seconds, src ip, src port, dst ip, dst port, udp payload) from a pcapng or pcap file."""
    data = Path(path).read_bytes()
    frames: list[tuple[float, bytes]] = []
    if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):  # classic pcap
        endian = "<" if data[:4] == b"\xd4\xc3\xb2\xa1" else ">"
        offset = 24
        while offset + 16 <= len(data):
            seconds, micros, length, _ = struct.unpack_from(endian + "IIII", data, offset)
            frames.append((seconds + micros / 1e6, data[offset + 16:offset + 16 + length]))
            offset += 16 + length
    else:
        endian, offset, resolutions = "<", 0, []
        while offset + 12 <= len(data):
            block_type, block_length = struct.unpack_from(endian + "II", data, offset)
            if block_type == 0x0A0D0D0A:
                endian = "<" if data[offset + 8:offset + 12] == b"\x4d\x3c\x2b\x1a" else ">"
                block_type, block_length = struct.unpack_from(endian + "II", data, offset)
            if block_length < 12 or offset + block_length > len(data):
                break
            body = data[offset + 8:offset + block_length - 4]
            offset += block_length
            if block_type == 1:  # interface description: its if_tsresol option sets the clock
                resolutions.append(_timestamp_resolution(body, endian))
            elif block_type == 6 and len(body) >= 20:
                interface, high, low, captured = struct.unpack_from(endian + "IIII", body, 0)
                scale = resolutions[interface] if interface < len(resolutions) else 1e-6
                frames.append((((high << 32) | low) * scale, body[20:20 + captured]))
    packets = []
    for stamp, frame in frames:
        if len(frame) < 42 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        if ip[9] != 17:
            continue
        udp = ip[(ip[0] & 15) * 4:]
        source_port, dest_port, length = struct.unpack_from("!HHH", udp, 0)
        packets.append((stamp, ".".join(map(str, ip[12:16])), source_port,
                        ".".join(map(str, ip[16:20])), dest_port, bytes(udp[8:length])))
    return packets


def _timestamp_resolution(body: bytes, endian: str) -> float:
    """Seconds per timestamp unit from an interface block's options (default microseconds)."""
    offset = 8
    while offset + 4 <= len(body):
        code, length = struct.unpack_from(endian + "HH", body, offset)
        if code == 0:
            break
        if code == 9 and length >= 1:
            value = body[offset + 4]
            return 2.0 ** -(value & 0x7F) if value & 0x80 else 10.0 ** -value
        offset += 4 + ((length + 3) & ~3)
    return 1e-6


def _body_offset(data: bytes, with_session: bool) -> int:
    offset = 4
    header, offset = read_varuint(data, offset)
    flags = PacketHeaderFlags(header - (header & 0x7FF))
    sender, target = struct.unpack_from("<HH", data, 0)
    if flags & PacketHeaderFlags.HAS_INSTANCE:
        offset += 4
    if flags & PacketHeaderFlags.RESET_CONNECTION:
        offset += 4
    if with_session or flags & PacketHeaderFlags.IS_REMOTE or (flags & PacketHeaderFlags.HAS_SESSION_ID and sender == target):
        _, offset = read_varuint(data, offset)
    bundle = PacketBundleFlags(data[offset])
    offset += 1
    if bundle & PacketBundleFlags.PROCESS_ACK:
        offset += 4
    offset += 2
    if bundle & PacketBundleFlags.RDP_REPORT:
        offset += 4
    if bundle & PacketBundleFlags.UNKNOWN_SINGLE_BYTE and offset < len(data):
        offset += 2 if data[offset] == 0xFF else 1
    if bundle & PacketBundleFlags.PROCESS_UNRELIABLE_ACKS:
        while offset < len(data):
            channel = data[offset]
            offset += 1
            if channel == 0xF8:
                break
            offset += 2
    return offset


def _rle_end(data: bytes, offset: int) -> int:
    while offset < len(data):
        code = data[offset]
        offset += 1
        if code == 0:
            return offset
        offset += (code & 0x7F) + 1 if code & 0x80 else code >> 4
    return offset


def server_messages(data: bytes, with_session: bool = True) -> list[tuple[int, int | None, bytes]]:
    """(message type, number, body) of one server packet; raises ValueError if unreadable."""
    offset, end, out = _body_offset(data, with_session), len(data) - 4, []
    while offset < end:
        kind = data[offset]
        size = data[offset + 1]
        offset += 2
        if size == 0xFF:
            size = struct.unpack_from("<H", data, offset)[0]
            offset += 2
        if kind in (0xFA, 0xFB, 0xF9):
            number = struct.unpack_from("<H", data, offset)[0]
            out.append((kind, number, data[offset + 2:offset + 2 + size]))
            offset += 2 + size
        elif kind == 0xFC:
            out.append((kind, None, data[offset:offset + size]))
            offset += size
        elif kind <= 0x17 or 0x40 <= kind <= 0x43:
            sequence, reference = struct.unpack_from("<HB", data, offset)
            end_of_data = _rle_end(data, offset + 3)
            out.append((kind, (sequence, reference), data[offset + 3:end_of_data]))
            offset = end_of_data
        else:
            if with_session:
                return server_messages(data, False)
            raise ValueError(f"unknown message type {kind:#x}")
    if offset != end:
        if with_session:
            return server_messages(data, False)
        raise ValueError("messages don't end at the CRC")
    return out


def read_capture(path: Path, game_port: int = GAME_PORT) -> list[CapturedMessage]:
    """The capture's reliable game messages in order, from both sides."""
    packets = [p for p in udp_packets(path) if game_port in (p[2], p[4])]
    if not packets:
        return []
    start = packets[0][0]
    # Message numbers restart with each connection (character select, then the world), told
    # apart by the packets' endpoint pair.
    seen_client: dict[tuple, set[int]] = {}
    seen_server: dict[tuple, set[int]] = {}
    pieces_by: dict[tuple, dict[int, bytes]] = {}
    out: list[CapturedMessage] = []
    for stamp, _src, source_port, _dst, _dest_port, payload in packets:
        when = stamp - start
        stream = tuple(sorted((payload[0:2], payload[2:4])))
        if source_port != game_port:
            try:
                packet = parse_udp_packet(payload)
            except Exception:
                continue
            for message in packet.messages:
                seen = seen_client.setdefault(stream, set())
                if message.opcode is None or message.message_number is None or message.message_number in seen:
                    continue
                seen.add(message.message_number)
                out.append(CapturedMessage(when, True, message.message_number, message.opcode, bytes(message.payload)))
            continue
        try:
            messages = server_messages(payload)
        except Exception:
            continue
        for kind, number, body in messages:
            if kind not in (0xFA, 0xFB):
                continue
            seen = seen_server.setdefault(stream, set())
            pieces = pieces_by.setdefault(stream, {})
            if number in seen:
                continue
            seen.add(number)
            if kind == 0xFA:
                pieces[number] = body
                continue
            # A segmented message: the 0xFA pieces numbered just before its closing 0xFB, in
            # number order (they can arrive out of order or be resent).
            parts, earlier = [body], (number - 1) & 0xFFFF
            while earlier in pieces:
                parts.append(pieces.pop(earlier))
                earlier = (earlier - 1) & 0xFFFF
            full = b"".join(reversed(parts))
            if len(full) >= 2:
                out.append(CapturedMessage(when, False, number, struct.unpack_from("<H", full, 0)[0], full[2:]))
    return out


NAME, NAME_LENGTH, OBJECT_ID = 116, 24, 1  # in the 162-byte object update


def entity_names(path: Path, game_port: int = GAME_PORT) -> dict[int, str]:
    """{object id: name} of everything the server drew on its entity channels (0x01-0x17):
    each channel message is zero-RLE data, XORed with the state ``reference`` messages back
    (0: a full update)."""
    from .protocol import rle_zero_decode

    states: dict[int, dict[int, bytes]] = {}
    names: dict[int, str] = {}
    for _stamp, _src, source_port, _dst, _dest_port, payload in udp_packets(path):
        if source_port != game_port:
            continue
        try:
            messages = server_messages(payload)
        except Exception:
            continue
        for kind, header, body in messages:
            if not 0x01 <= kind <= 0x17 or not isinstance(header, tuple):
                continue
            sequence, reference = header
            data = rle_zero_decode(body)
            history = states.setdefault(kind, {})
            if reference:
                base = history.get((sequence - reference) & 0xFFFF)
                if base is None:
                    continue
                size = max(len(base), len(data))
                data = bytes(a ^ b for a, b in zip(data.ljust(size, b"\0"), base.ljust(size, b"\0")))
            history[sequence] = data
            if len(data) >= NAME + NAME_LENGTH and data[0]:
                name = data[NAME:NAME + NAME_LENGTH].split(b"\0", 1)[0].decode("ascii", "replace").strip()
                if name:
                    names[struct.unpack_from("<I", data, OBJECT_ID)[0]] = name
    return names


ITEM_FIELDS = ("unknown1", "icon", "unknown2", "slot", "unknown3", "trade", "rent", "unknown4", "attack_type",
               "damage", "unknown5", "level", "max_stack", "max_hp", "duration", "class_mask", "race_mask",
               "proc_anim", "lore", "unknown6", "craft")


def _zigzag(data: bytes, offset: int) -> tuple[int, int]:
    value, offset = read_varuint(data, offset)
    return (value >> 1) ^ -(value & 1), offset


def _utf16(data: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<i", data, offset)[0]
    start = offset + 4
    return data[start:start + 2 * length].decode("utf-16-le", "replace"), start + 2 * length


def item_record(data: bytes, offset: int) -> tuple[dict, int]:
    """One item record (inventory, MerchantBox, Loot, AddInventoryItem), in the field order
    items.item_record writes: zigzag stack, durability, charges, equip slot; byte location;
    int32 key; zigzag item id, cost, 21 pattern fields; name, description; zigzag stat count
    and (stat, value) pairs."""
    item: dict = {}
    for name in ("stack", "durability", "charges", "equip"):
        item[name], offset = _zigzag(data, offset)
    item["location"] = data[offset]
    item["key"] = struct.unpack_from("<i", data, offset + 1)[0]
    offset += 5
    item["id"], offset = _zigzag(data, offset)
    item["cost"], offset = _zigzag(data, offset)
    for name in ITEM_FIELDS:
        item[name], offset = _zigzag(data, offset)
    item["name"], offset = _utf16(data, offset)
    item["description"], offset = _utf16(data, offset)
    count, offset = _zigzag(data, offset)
    stats = []
    for _ in range(count):
        stat, offset = _zigzag(data, offset)
        value, offset = _zigzag(data, offset)
        stats.append([stat, value])
    item["stats"] = stats
    return item, offset


def item_list(data: bytes, offset: int) -> list[dict]:
    """zigzag count, int32 count, then the records (MerchantBox after its header, Loot)."""
    count, offset = _zigzag(data, offset)
    offset += 4
    found = []
    for _ in range(count):
        item, offset = item_record(data, offset)
        found.append(item)
    return found


def merchant_box(payload: bytes) -> tuple[int, int, int, list[dict]]:
    """MerchantBox 0xB7: u32 merchant, zigzag sell %, zigzag buy %, then the item list."""
    merchant = struct.unpack_from("<I", payload, 0)[0]
    sell, offset = _zigzag(payload, 4)
    buy, offset = _zigzag(payload, offset)
    return merchant, sell, buy, item_list(payload, offset)
