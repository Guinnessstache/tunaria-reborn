from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag


class PacketHeaderFlags(IntFlag):
    NEW_INSTANCE = 0x00080000
    HAS_SESSION_ID = 0x00004000
    HAS_INSTANCE = 0x00002000
    IS_REMOTE = 0x00000800
    RESET_CONNECTION = 0x00010000


class PacketBundleFlags(IntFlag):
    NEW_PROCESS_MESSAGES = 0x00
    RDP_REPORT = 0x03
    UNKNOWN_SINGLE_BYTE = 0x08
    PROCESS_UNRELIABLE_ACKS = 0x10
    PROCESS_MESSAGES = 0x20
    PROCESS_ACK = 0x40


class MessageType(IntEnum):
    PING = 0xF9
    SEGMENT_RELIABLE = 0xFA
    RELIABLE = 0xFB
    UNRELIABLE = 0xFC
    CLIENT_UPDATE = 0x40
    STAT_UPDATE = 0x42
    BUFF_UPDATE = 0x43


GAME_OPCODES: dict[int, str] = {
    0x0000: "DiscVersion",
    0x0001: "Authenticate2",
    0x0004: "Interact",
    0x000B: "ClientSayChat",
    0x000D: "MemoryDump",
    0x000E: "ChangeChatMode",
    0x000F: "Attack",
    0x0012: "RandomName",
    0x0013: "Time",
    0x002A: "SelectedCharacter",
    0x002B: "CreateCharacter",
    0x002C: "CharacterSelect",
    0x002D: "DeleteCharacter",
    0x002F: "NameTaken",
    0x0034: "OptionBox",
    0x0035: "DialogueBoxOption",
    0x003B: "AddInventoryItem",
    0x003C: "RemoveInventoryItem",
    0x003D: "ArrangeItem",
    0x003E: "InteractItem",
    0x003F: "EquipItem",
    0x0040: "UnequipItem",
    0x0046: "DialogueBox",
    0x0049: "EnableChannel",
    0x004A: "MerchantBuy",
    0x004B: "MerchantSell",
    0x004C: "MerchantDialog",
    0x0052: "PlayerTunar",
    0x007C: "AddQuestLog",
    0x007D: "DeleteQuest",
    0x00B7: "MerchantBox",
    0x00F8: "PlayerSpeed",
    0x0760: "Target",
    0x0761: "TargetInformation",
    0x07B3: "GameServers",
    0x07D0: "BestEffortPing",
    0x07D1: "Camera1",
    0x07D2: "CharacterModifiedDisconnect",
    0x07F5: "Camera2",
    0x07F6: "Teleport",
    0x0904: "Authenticate",
    0x09B0: "DisconnectClient",
    0x0A7A: "ClientMessage",
    0x0A7B: "ColoredChat",
    0x0AC4: "ClientFaction",
    0x0C01: "ClientShout",
    0x1005: "IgnoreList",
    0x124D: "BankUI",
    0x1250: "RemoveBankItem",
    0x1251: "AddBankItem",
    0x1253: "ConfirmBankTunar",
    0x1254: "BankItem",
    0x1255: "DepositBankTunar",
    0x1401: "ClassMastery",
    0x1402: "ClassMasteryServer",
}

OLD_WORLD_SERVER_ENDPOINT = 0xB991
OLD_WORLD_ENTRY_COUNT = 6
OLD_WORLD_VISIBLE_COUNT = 4
OLD_WORLD_NAME_CHARS = 9
PHASE3_MAGIC = bytes([0xD0, 0xCF, 0xAD, 0x01])


def rdp_crc(data: bytes) -> int:
    """EQOA/ReturnHome RDP CRC variant used by old world-list packets."""
    return (zlib.crc32(data) ^ 0xFFFFFFFF ^ 0x11F19ED3) & 0xFFFFFFFF


def read_varuint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if offset >= len(data):
            raise ValueError("unterminated varuint")
        value = data[offset]
        offset += 1
        result |= (value & 0x7F) << shift
        if value & 0x80 == 0:
            return result, offset
        shift += 7
        if shift >= 64:
            raise ValueError("varuint too large")


def write_varuint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


@dataclass(slots=True)
class UdpMessage:
    message_type: int
    message_number: int | None = None
    opcode: int | None = None
    payload: bytes = b""

    @property
    def opcode_name(self) -> str:
        if self.opcode is None:
            return ""
        return GAME_OPCODES.get(self.opcode, f"Unknown_0x{self.opcode:04X}")


@dataclass(slots=True)
class UdpPacket:
    client_endpoint: int
    target_endpoint: int
    bundle_size: int
    header_flags: PacketHeaderFlags
    bundle_flags: PacketBundleFlags
    instance_id: int | None = None
    session_id: int = 0
    session_ack_id: int | None = None
    client_bundle_number: int = 0
    client_bundle_ack: int | None = None
    client_message_ack: int | None = None
    channel_acks: dict[int, int] = field(default_factory=dict)
    messages: list[UdpMessage] = field(default_factory=list)


@dataclass(slots=True)
class Phase3ClientPacket:
    client_endpoint: int
    instance_id: int | None
    arrival: int
    last_bundle: int | None = None
    guaranteed_ack: int | None = None
    max_reliable_sequence: int = 0
    has_state_update: bool = False
    has_ping: bool = False
    client_update_sequence: int | None = None


def parse_udp_packet(data: bytes) -> UdpPacket:
    if len(data) < 8:
        raise ValueError("udp packet too short")
    offset = 0
    client_endpoint, target_endpoint = struct.unpack_from("<HH", data, offset)
    offset += 4
    header_data, offset = read_varuint(data, offset)
    bundle_size = header_data & 0x7FF
    header_flags = PacketHeaderFlags(header_data - bundle_size)
    instance_id = None
    session_id = 0
    if header_flags & PacketHeaderFlags.HAS_INSTANCE:
        instance_id = struct.unpack_from("<I", data, offset)[0]
        offset += 4
    if header_flags & PacketHeaderFlags.RESET_CONNECTION:
        offset += 4
    same_endpoint_session = (
        header_flags & PacketHeaderFlags.HAS_SESSION_ID
        and client_endpoint == target_endpoint
    )
    if header_flags & PacketHeaderFlags.IS_REMOTE or same_endpoint_session:
        session_id, offset = read_varuint(data, offset)
    bundle_flags = PacketBundleFlags(data[offset])
    offset += 1
    session_ack_id = None
    if bundle_flags & PacketBundleFlags.PROCESS_ACK:
        session_ack_id = struct.unpack_from("<I", data, offset)[0]
        offset += 4
    client_bundle_number = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    client_bundle_ack = None
    client_message_ack = None
    if bundle_flags & PacketBundleFlags.RDP_REPORT:
        client_bundle_ack, client_message_ack = struct.unpack_from("<HH", data, offset)
        offset += 4
    if bundle_flags & PacketBundleFlags.UNKNOWN_SINGLE_BYTE and offset < len(data):
        marker = data[offset]
        offset += 1
        if marker == 0xFF and offset < len(data):
            offset += 1
    channel_acks: dict[int, int] = {}
    if bundle_flags & PacketBundleFlags.PROCESS_UNRELIABLE_ACKS:
        while offset < len(data):
            channel = data[offset]
            offset += 1
            if channel == 0xF8:
                break
            if 0 <= channel <= 0x17 or 0x40 <= channel <= 0x43:
                channel_acks[channel] = struct.unpack_from("<H", data, offset)[0]
                offset += 2
            else:
                raise ValueError(f"invalid unreliable ack channel 0x{channel:02X}")

    packet = UdpPacket(
        client_endpoint=client_endpoint,
        target_endpoint=target_endpoint,
        bundle_size=bundle_size,
        header_flags=header_flags,
        bundle_flags=bundle_flags,
        instance_id=instance_id,
        session_id=session_id,
        session_ack_id=session_ack_id,
        client_bundle_number=client_bundle_number,
        client_bundle_ack=client_bundle_ack,
        client_message_ack=client_message_ack,
        channel_acks=channel_acks,
    )
    message_end = len(data) if target_endpoint == 0xFFFF else max(offset, len(data) - 4)
    packet.messages = parse_messages(data[offset:message_end])
    return packet


def parse_phase3_client_packet(data: bytes) -> Phase3ClientPacket:
    if len(data) < 12:
        raise ValueError("phase3 packet too short")
    client_endpoint, target_endpoint = struct.unpack_from("<HH", data, 0)
    if client_endpoint != target_endpoint:
        raise ValueError("not a phase3 same-endpoint packet")
    body_marker = b"\xB8" + PHASE3_MAGIC
    body_offset = data.find(body_marker)
    if body_offset < 0:
        raise ValueError("phase3 marker not found")

    header_offset = 4
    header_data, header_offset = read_varuint(data, header_offset)
    bundle_size = header_data & 0x7FF
    header_flags = PacketHeaderFlags(header_data - bundle_size)
    instance_id = None
    if header_flags & PacketHeaderFlags.HAS_INSTANCE and header_offset + 4 <= body_offset:
        instance_id = struct.unpack_from("<I", data, header_offset)[0]

    inner = data[body_offset + len(body_marker) :]
    if len(inner) < 3:
        raise ValueError("phase3 inner body too short")
    inner_flags = inner[0]
    offset = 1
    if inner_flags & 0x40:
        offset += 4
    if offset + 2 > len(inner):
        raise ValueError("phase3 arrival missing")
    arrival = struct.unpack_from("<H", inner, offset)[0]
    offset += 2

    last_bundle = None
    guaranteed_ack = None
    if inner_flags & 0x02 and offset + 4 <= len(inner):
        last_bundle, guaranteed_ack = struct.unpack_from("<HH", inner, offset)
        offset += 4

    max_reliable_sequence = 0
    has_state_update = False
    has_ping = False
    client_update_sequence = None
    while offset < len(inner):
        message_type = inner[offset]
        offset += 1
        if message_type in (MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE):
            size, offset = read_varuint(inner, offset)
            if size < 2 or offset + 2 + size > len(inner):
                break
            sequence = struct.unpack_from("<H", inner, offset)[0]
            offset += 2
            max_reliable_sequence = max(max_reliable_sequence, sequence)
            offset += size
        elif message_type == MessageType.PING:
            has_ping = True
            size, offset = read_varuint(inner, offset)
            offset += min(size, len(inner) - offset)
        elif message_type == MessageType.UNRELIABLE:
            size, offset = read_varuint(inner, offset)
            offset += min(size, len(inner) - offset)
        elif message_type == MessageType.CLIENT_UPDATE:
            has_state_update = True
            size, offset = read_varuint(inner, offset)
            if size >= 3 and offset + 3 <= len(inner):
                client_update_sequence = struct.unpack_from("<H", inner, offset)[0]
            break
        elif 0x00 <= message_type <= 0x17:
            has_state_update = True
            break
        elif message_type == 0xFF:
            continue
        else:
            break

    max_reliable_sequence = max(max_reliable_sequence, _scan_phase3_reliable_sequences(inner))

    return Phase3ClientPacket(
        client_endpoint=client_endpoint,
        instance_id=instance_id,
        arrival=arrival,
        last_bundle=last_bundle,
        guaranteed_ack=guaranteed_ack,
        max_reliable_sequence=max_reliable_sequence,
        has_state_update=has_state_update,
        has_ping=has_ping,
        client_update_sequence=client_update_sequence,
    )


def _scan_phase3_reliable_sequences(inner: bytes) -> int:
    max_sequence = 0
    for marker in (MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE):
        start = 0
        while True:
            index = inner.find(bytes([marker]), start)
            if index < 0:
                break
            try:
                size, offset = read_varuint(inner, index + 1)
            except ValueError:
                start = index + 1
                continue
            if 2 <= size <= 0x2000 and offset + 2 <= len(inner):
                sequence = struct.unpack_from("<H", inner, offset)[0]
                payload_end = offset + 2 + size
                if payload_end <= len(inner) and sequence < 0x4000:
                    max_sequence = max(max_sequence, sequence)
            start = index + 1
    return max_sequence


def parse_messages(data: bytes) -> list[UdpMessage]:
    messages: list[UdpMessage] = []
    offset = 0
    while offset < len(data):
        message_type = data[offset]
        offset += 1
        if offset >= len(data):
            break
        size = data[offset]
        offset += 1
        if size == 0xFF:
            if offset + 2 > len(data):
                break
            size = struct.unpack_from("<H", data, offset)[0]
            offset += 2
        message_number = None
        if message_type != MessageType.UNRELIABLE:
            if offset + 2 > len(data):
                break
            message_number = struct.unpack_from("<H", data, offset)[0]
            offset += 2
        if message_type == MessageType.PING:
            messages.append(UdpMessage(message_type, message_number, payload=data[offset : offset + size]))
            offset += size
            continue
        opcode = None
        if message_type in (MessageType.UNRELIABLE, MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE):
            if offset + 2 > len(data):
                break
            opcode = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            size = max(0, size - 2)
        elif message_type == MessageType.CLIENT_UPDATE:
            if offset < len(data):
                offset += 1
                size = max(0, size - 1)
        else:
            break
        payload = data[offset : offset + size]
        offset += size
        messages.append(UdpMessage(message_type, message_number, opcode, payload))
    return messages


def build_minimal_ack(packet: UdpPacket, server_endpoint: int = 0xFFFE) -> bytes:
    header_data = 0
    body = bytearray()
    body += struct.pack("<HH", server_endpoint, packet.client_endpoint)
    body += write_varuint(header_data)
    body += bytes([PacketBundleFlags.RDP_REPORT])
    body += struct.pack("<HHH", packet.client_bundle_number, packet.client_bundle_number, 0)
    return bytes(body)


def build_old_world_list(
    client_endpoint: int,
    public_host: str,
    game_port: int,
    shard_name: str,
) -> bytes:
    ip_bytes = bytes(int(part) for part in public_host.split(".")[:4])
    if len(ip_bytes) != 4:
        raise ValueError(f"public host must be an IPv4 address, got {public_host!r}")

    raw_name = (shard_name or "VoxOnline")[:OLD_WORLD_NAME_CHARS]
    name_bytes = raw_name.encode("utf-16-le").ljust(OLD_WORLD_NAME_CHARS * 2, b"\x00")
    name_char_count = len(raw_name)

    entries = bytearray()
    for language in range(OLD_WORLD_ENTRY_COUNT):
        entry = bytearray()
        entry += struct.pack("<I", name_char_count)
        entry += name_bytes
        entry += b"\x00"
        entry += struct.pack("<H", OLD_WORLD_SERVER_ENDPOINT)
        entry += struct.pack("<H", game_port)
        entry += bytes(reversed(ip_bytes))
        entry += bytes([language])
        if len(entry) != 32:
            raise ValueError(f"world-list entry must be 32 bytes, got {len(entry)}")
        entries += entry

    ps2_endpoint = struct.pack("<H", client_endpoint)
    header = bytearray(32)
    header[0:2] = b"\xb0\x73"
    header[2:4] = ps2_endpoint
    header[4:7] = bytes([0xDA, 0x21, 0x63])
    header[7:9] = ps2_endpoint
    header[9:12] = bytes([0x02, 0x00, 0x01])
    header[12:16] = bytes([0x00, 0x01, 0x00, 0x02])
    header[16:20] = bytes([0x00, 0xFB, 0x06, 0x01])
    header[20:24] = bytes([0x00, 0x00, 0x00, 0x12])
    header[24:28] = bytes([0x00, 0x00, 0x00, 0xFC])
    header[28:32] = bytes([0xC3, 0xB3, 0x07, OLD_WORLD_VISIBLE_COUNT])

    body = bytes(header) + bytes(entries)
    return body + struct.pack("<I", rdp_crc(body))


def build_old_world_ack(client_endpoint: int, auth_byte: int = 0x01) -> bytes:
    ps2_endpoint = struct.pack("<H", client_endpoint)
    ack = bytearray(27)
    ack[0:2] = b"\xb0\x73"
    ack[2:4] = ps2_endpoint
    ack[4:6] = bytes([0x8D, 0x60])
    ack[6:8] = ps2_endpoint
    ack[8] = auth_byte
    ack[9:11] = bytes([0x00, 0x20])
    ack[11] = 0x02
    ack[12:23] = bytes([0x00, 0xFB, 0x06, 0x01, 0x00, 0x00, 0x00, 0x12, 0x00, 0x00, 0x00])
    ack[23:27] = struct.pack("<I", rdp_crc(bytes(ack[:23])))
    return bytes(ack)


def build_phase2_character_select_ack(client_endpoint: int, instance_id: int) -> bytes:
    messages = _phase2_character_select_messages()
    body = bytearray()
    body.append(0x63)
    body += struct.pack("<I", instance_id)
    body += struct.pack("<H", 1)
    body += struct.pack("<H", 1)
    body += struct.pack("<H", 2)
    body += messages

    header = struct.pack("<HH", OLD_WORLD_SERVER_ENDPOINT, client_endpoint)
    flags = write_varuint(0x1000 + len(body))
    packet = header + flags + bytes(body)
    return packet + struct.pack("<I", rdp_crc(packet))


def build_phase2_character_select_resend(
    client_endpoint: int,
    instance_id: int,
    arrival: int = 1,
) -> bytes:
    body = bytearray()
    body.append(PacketBundleFlags.PROCESS_MESSAGES)
    body += struct.pack("<H", arrival)
    body += _phase2_character_select_messages()

    header = struct.pack("<HH", OLD_WORLD_SERVER_ENDPOINT, client_endpoint)
    flags = write_varuint(0x3000 + len(body))
    packet = header + flags + struct.pack("<I", instance_id) + bytes(body)
    return packet + struct.pack("<I", rdp_crc(packet))


def build_phase2_guaranteed_ack(
    client_endpoint: int,
    instance_id: int,
    arrival: int,
    client_bundle_number: int,
    client_message_number: int,
) -> bytes:
    body = bytearray()
    body.append(0x23)
    body += struct.pack("<H", arrival)
    body += struct.pack("<H", client_bundle_number)
    body += struct.pack("<H", client_message_number)

    header = struct.pack("<HH", OLD_WORLD_SERVER_ENDPOINT, client_endpoint)
    flags = write_varuint(0x3000 + len(body))
    packet = header + flags + struct.pack("<I", instance_id) + bytes(body)
    return packet + struct.pack("<I", rdp_crc(packet))


def build_create_character_response(
    client_endpoint: int,
    instance_id: int,
    arrival: int,
    message_number: int,
    create_payload: bytes,
    character_id: int | None = None,
) -> bytes:
    name, appearance = parse_create_character_payload(create_payload)
    entity_id = _entity_id(character_id, name)
    response_payload = (
        bytes([0x02])
        + struct.pack("<I", len(name))
        + name
        + entity_id
        + appearance
        + bytes([0xFF, 0xFF, 0xFF, 0xFF])
        + bytes(16)
        + bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        + bytes(4)
    )
    message_data = struct.pack("<HH", message_number, 0x002C) + response_payload
    message = bytes([MessageType.RELIABLE]) + write_varuint(len(message_data)) + message_data

    body = bytearray()
    body.append(PacketBundleFlags.PROCESS_MESSAGES)
    body += struct.pack("<H", arrival)
    body += message
    body += bytes([0x00, 0x00])

    header = struct.pack("<HH", OLD_WORLD_SERVER_ENDPOINT, client_endpoint)
    flags = write_varuint(0x3000 + len(body))
    packet = header + flags + struct.pack("<I", instance_id) + bytes(body)
    return packet + struct.pack("<I", rdp_crc(packet))


def build_phase3_world_entry(
    client_endpoint: int,
    instance_id: int,
    arrival: int = 1,
    now_us: int = 0,
) -> bytes:
    if now_us <= 0:
        import time

        now_us = int(time.time() * 1_000_000)
    timestamp_a = struct.pack("<Q", now_us)
    timestamp_b_partial = struct.pack("<Q", now_us)[:6]

    camera_msg = bytes([0xFB, 0x06, 0x01, 0x00, 0xD1, 0x07, 0xFF, 0xFF])
    time_data = struct.pack("<HH", 2, 0x0013) + timestamp_a + timestamp_b_partial
    time_msg = bytes([0xFB]) + write_varuint(len(time_data)) + time_data
    unknown_20 = bytes([0xFB, 0x04, 0x03, 0x00, 0x20, 0x00])
    unknown_d9 = bytes([0xFB, 0x06, 0x04, 0x00, 0xD9, 0x00, 0x00, 0x00, 0x00, 0x00])
    messages = (
        camera_msg
        + bytes([0xFF, 0xFF])
        + time_msg
        + bytes([0x00, 0x00])
        + unknown_20
        + bytes([0x02, 0x00])
        + unknown_d9
    )
    inner = bytes([PacketBundleFlags.PROCESS_MESSAGES]) + struct.pack("<H", arrival) + messages
    return build_phase3_packet(client_endpoint, instance_id, inner, new_instance=True)


def build_phase3_guaranteed_ack(
    client_endpoint: int,
    instance_id: int,
    arrival: int,
    client_bundle_number: int,
    client_message_number: int,
) -> bytes:
    inner = (
        bytes([0x23])
        + struct.pack("<H", arrival)
        + struct.pack("<H", client_bundle_number)
        + struct.pack("<H", client_message_number)
    )
    return build_phase3_packet(client_endpoint, instance_id, inner, new_instance=False)


def build_phase3_server_ping(client_endpoint: int, instance_id: int, arrival: int) -> bytes:
    ping_byte = (arrival * 2) & 0xFF
    inner = (
        bytes([PacketBundleFlags.PROCESS_MESSAGES])
        + struct.pack("<H", arrival)
        + bytes([MessageType.PING, 0x01, ping_byte, 0x00, 0x12])
    )
    return build_phase3_packet(client_endpoint, instance_id, inner, new_instance=False)


def build_phase3_client_update_ack(
    client_endpoint: int,
    instance_id: int,
    arrival: int,
    client_update_sequence: int,
) -> bytes:
    inner = (
        bytes([PacketBundleFlags.PROCESS_MESSAGES | PacketBundleFlags.PROCESS_UNRELIABLE_ACKS])
        + struct.pack("<H", arrival)
        + bytes([MessageType.CLIENT_UPDATE])
        + struct.pack("<H", client_update_sequence & 0xFFFF)
        + bytes([0xF8])
    )
    return build_phase3_packet(client_endpoint, instance_id, inner, new_instance=False)


def build_phase3_object_update_packet(
    client_endpoint: int,
    instance_id: int,
    arrival: int,
    channel: int,
    sequence: int,
    payload: bytes,
) -> bytes:
    message_data = (
        bytes([channel & 0xFF])
        + _write_size(len(payload))
        + struct.pack("<H", sequence & 0xFFFF)
        + b"\x00"
        + _rle_zero_encode(payload)
    )
    inner = bytes([PacketBundleFlags.PROCESS_MESSAGES]) + struct.pack("<H", arrival) + message_data
    return build_phase3_packet(client_endpoint, instance_id, inner, new_instance=False)


def build_phase3_memory_dump_packets(
    client_endpoint: int,
    instance_id: int,
    start_arrival: int,
    start_message: int,
    payload: bytes,
) -> tuple[list[bytes], int, int]:
    packets: list[bytes] = []
    segment_size = 0x0484
    total_size = 2 + len(payload)
    arrival = start_arrival
    message = start_message
    if total_size < segment_size:
        message_data = (
            bytes([MessageType.RELIABLE])
            + _write_size(total_size)
            + struct.pack("<HH", message, 0x000D)
            + payload
        )
        inner = bytes([PacketBundleFlags.PROCESS_MESSAGES]) + struct.pack("<H", arrival) + message_data
        packets.append(build_phase3_packet(client_endpoint, instance_id, inner, new_instance=True))
        return packets, arrival + 1, message + 1

    first_chunk = payload[: segment_size - 2]
    remaining = payload[segment_size - 2 :]
    while True:
        is_first = message == start_message
        chunk = first_chunk if is_first else remaining[:segment_size]
        if not is_first:
            remaining = remaining[segment_size:]
        is_last = not remaining
        message_type = MessageType.RELIABLE if is_last else MessageType.SEGMENT_RELIABLE
        if is_first:
            data = struct.pack("<HH", message, 0x000D) + chunk
            size = len(data) - 2
        else:
            data = struct.pack("<H", message) + chunk
            size = len(chunk)
        message_data = bytes([message_type]) + _write_size(size) + data
        inner = bytes([PacketBundleFlags.PROCESS_MESSAGES]) + struct.pack("<H", arrival) + message_data
        packets.append(build_phase3_packet(client_endpoint, instance_id, inner, new_instance=True))
        arrival += 1
        message += 1
        if is_last:
            return packets, arrival, message


def _write_size(size: int) -> bytes:
    if size >= 255:
        return bytes([0xFF]) + struct.pack("<H", size & 0xFFFF)
    return bytes([size & 0xFF])


def _rle_zero_encode(data: bytes) -> bytes:
    out = bytearray()
    real_start = 0
    real_count = 0
    zero_count = 0
    for index, value in enumerate(data):
        if value == 0:
            if real_count:
                out.append(real_count | 0x80)
                out.append(zero_count & 0xFF)
                out += data[real_start : real_start + real_count]
                real_count = 0
                zero_count = 0
            zero_count += 1
        else:
            if real_count == 0:
                real_start = index
            real_count += 1
    out.append(real_count | 0x80)
    out.append(zero_count & 0xFF)
    if real_count:
        out += data[real_start : real_start + real_count]
    out.append(0)
    return bytes(out)


def build_phase3_packet(
    client_endpoint: int,
    instance_id: int,
    inner_body: bytes,
    *,
    new_instance: bool = True,
) -> bytes:
    header = struct.pack("<HH", client_endpoint, client_endpoint)
    body = bytes([0xB8]) + PHASE3_MAGIC + inner_body
    flag_base = 0x86000 if new_instance else 0x6000
    flags = write_varuint(flag_base + len(inner_body))
    packet = header + flags + struct.pack("<I", instance_id) + body
    return packet + struct.pack("<I", rdp_crc(packet))


def parse_create_character_payload(payload: bytes) -> tuple[bytes, bytes]:
    if len(payload) < 4:
        raise ValueError("create-character payload missing name length")
    name_len = struct.unpack_from("<I", payload, 0)[0]
    if name_len > 32 or len(payload) < 4 + name_len:
        raise ValueError(f"invalid create-character name length {name_len}")
    name = payload[4 : 4 + name_len]
    appearance_offset = 4 + name_len
    appearance = payload[appearance_offset : appearance_offset + 8].ljust(8, b"\x00")
    return name, appearance


def _entity_id(character_id: int | None, name: bytes) -> bytes:
    if character_id is not None:
        return struct.pack("<I", character_id) + b"\x01\x00\x00"
    crc = zlib.crc32(name) & 0xFFFFFFFF
    return struct.pack("<I", crc) + b"\x01\x00\x00"


def _phase2_character_select_messages() -> bytes:
    disc_msg = bytes([0xFB, 0x06, 0x01, 0x00, 0x00, 0x00, 0x12, 0x00])
    null_sep = bytes([0x00, 0x00])
    camera_msg = bytes([0xFB, 0x06, 0x02, 0x00, 0xD1, 0x07, 0xFF, 0xFF])
    ff_pad = bytes([0xFF, 0xFF])
    char_list_msg = bytes([0xFB, 0x03, 0x03, 0x00, 0x2C, 0x00, 0x00])
    return disc_msg + null_sep + camera_msg + ff_pad + char_list_msg
