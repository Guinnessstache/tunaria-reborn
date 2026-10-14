from __future__ import annotations

import struct

from .storage import Character


class BufferWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def bytes(self) -> bytes:
        return bytes(self.data)

    def write_byte(self, value: int) -> None:
        self.data.append(value & 0xFF)

    def write_int32(self, value: int) -> None:
        self.data += struct.pack("<i", value)

    def write_float(self, value: float) -> None:
        self.data += struct.pack("<f", value)

    def write_bytes(self, value: bytes) -> None:
        self.data += value

    def write_string(self, value: str) -> None:
        encoded = value.encode("utf-8")
        self.data += struct.pack("<i", len(value))
        self.data += encoded

    def write_7bit_uint64(self, value: int) -> None:
        value &= 0xFFFFFFFFFFFFFFFF
        while True:
            chunk = value & 0x7F
            value >>= 7
            if value:
                self.data.append(chunk | 0x80)
            else:
                self.data.append(chunk)
                return

    def write_7bit_int64(self, value: int) -> None:
        if value < 0:
            unsigned = (~(value & 0xFFFFFFFF)) & 0xFFFFFFFF
            zigzag = ((unsigned << 1) + 1) & 0xFFFFFFFFFFFFFFFF
        else:
            zigzag = (value << 1) & 0xFFFFFFFFFFFFFFFF
        self.write_7bit_uint64(zigzag)


STAT_CM_PLACEHOLDER = bytes([
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10,
    0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18,
    0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20,
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x00, 0x00, 0x00, 0x30, 0x00, 0x00, 0x00,
    0x00, 0x80, 0x00, 0x00, 0x03, 0x80, 0x00, 0x00,
    0x50, 0x00, 0x00, 0x00, 0x51, 0x00, 0x00, 0x00,
    0x25, 0x26, 0x27, 0x28, 0x11, 0x12, 0x13, 0x14,
    0x15, 0x16, 0x17, 0x18, 0x0D, 0x0E, 0x0F, 0x10,
    0x21, 0x22, 0x23, 0x24, 0x05, 0x06, 0x07, 0x08,
    0x01, 0x02, 0x03, 0x04, 0x19, 0x1A, 0x1B, 0x1C,
    0x1D, 0x1E, 0x1F, 0x20, 0x25, 0x26, 0x27, 0x28,
])


def build_memory_dump_core(character: Character, server_id: int | None = None) -> bytes:
    writer = BufferWriter()
    x, y, z, heading = _spawn_position(character)
    writer.write_byte(0)
    writer.write_string("data\\tunaria.esf")
    writer.write_7bit_int64(character.character_id if server_id is None else server_id)
    writer.write_string(character.name)
    writer.write_7bit_int64(character.klass)
    writer.write_7bit_int64(character.race)
    writer.write_7bit_int64(character.level)
    writer.write_7bit_int64(character.experience)
    writer.write_7bit_int64(0)
    writer.write_byte(127)
    writer.write_7bit_int64(0)
    writer.write_7bit_int64(0)
    writer.write_7bit_int64(0)
    writer.write_7bit_int64(700)
    writer.write_7bit_int64(0)
    writer.write_float(x)
    writer.write_float(y)
    writer.write_float(z)
    writer.write_float(heading)
    writer.write_float(0.0)
    writer.write_float(0.0)
    return writer.bytes()


def build_memory_dump(character: Character, speed: float = 1.0, server_id: int | None = None) -> bytes:
    writer = BufferWriter()
    writer.write_bytes(build_memory_dump_core(character, server_id))
    writer.write_7bit_int64(0)
    writer.write_int32(0)
    writer.write_int32(0)
    writer.write_int32(0)
    writer.write_7bit_int64(0)
    writer.write_int32(0)
    writer.write_7bit_int64(0)
    writer.write_int32(0)
    writer.write_byte(0)
    writer.write_byte(0)
    writer.write_byte(0)
    writer.write_7bit_int64(0)
    writer.write_float(speed)
    writer.write_bytes(STAT_CM_PLACEHOLDER)
    writer.write_7bit_int64(0)
    writer.write_7bit_int64(0)
    writer.write_bytes(bytes([0xAE, 0x98, 0x4C]))
    writer.write_7bit_int64(0)
    writer.write_float(speed)
    for _ in range(7):
        writer.write_7bit_int64(0)
    writer.write_bytes(bytes([0xDE, 0x02, 0xDE, 0x02, 0x00, 0xFA, 0x01, 0x00, 0x00, 0x00]))
    writer.write_7bit_int64(0)
    writer.write_7bit_int64(0)
    writer.write_bytes(bytes([0x5A, 0x00]))
    writer.write_7bit_int64(0)
    writer.write_bytes(bytes([0x04, 0x00, 0x0C]))
    for _ in range(6):
        writer.write_7bit_int64(0)
    writer.write_bytes(bytes([0x00, 0xDE, 0x02, 0xDE, 0x02, 0x00, 0xFA, 0x01, 0x00, 0x00, 0x00]))
    return writer.bytes()


def build_object_update(character: Character, object_id: int) -> bytes:
    update = bytearray(0xC9)
    body = update[1:]
    x, y, z, heading = _spawn_position(character)

    _write_u32(body, 0, object_id)
    _write_be_i32(body, 4, int(x * 128.0))
    _write_be_i32(body, 7, int(y * 128.0))
    _write_be_i32(body, 10, int(z * 128.0))
    body[4] = 0x82
    body[14] = _heading_byte(heading)
    body[16] = character.zone_id & 0xFF
    _write_u32(body, 27, _model_id(character))
    _write_f32(body, 35, 1.0)
    _write_f32(body, 49, y)
    _write_f32(body, 53, heading)
    body[57] = 0
    _write_u32(body, 58, 0xFFFFFFFF)
    _write_u16(body, 62, 0x0105)
    for offset in range(98, 110, 2):
        _write_u16(body, offset, 0xFFFF)
    for offset in (110, 115, 120, 125, 130, 135, 140):
        _write_be_u32(body, offset, 0xFFFFFFFF)
    body[148] = 0xFF
    name = character.name.encode("ascii", "replace")[:23]
    body[154 : 154 + len(name)] = name
    body[178] = max(1, min(character.level, 61))
    body[179] = 1
    body[180] = 2
    body[182] = character.race & 0xFF
    body[183] = character.klass & 0xFF
    _write_u32(body, 186, 0xFFFFFFFF)
    body[196:200] = b"trsq"
    update[0] = 1
    return bytes(update)


def _spawn_position(character: Character) -> tuple[float, float, float, float]:
    if any((character.x, character.y, character.z)):
        return character.x, character.y, character.z, character.heading
    if character.race == 0:
        return 25273.03125, 54.125, 15723.29102, 3.138683081
    if character.race == 6:
        return 12978.21094, 53.22509384, 4678.135254, -0.903002858
    return character.x, character.y, character.z, character.heading


def _heading_byte(heading: float) -> int:
    return int(heading / 0.0245433693) & 0xFF


def _model_id(character: Character) -> int:
    if character.race == 0:
        return 3
    if character.race == 6:
        return 9
    return max(1, character.race + 3)


def _write_u16(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 2] = struct.pack("<H", value & 0xFFFF)


def _write_u32(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 4] = struct.pack("<I", value & 0xFFFFFFFF)


def _write_f32(buffer: bytearray, offset: int, value: float) -> None:
    buffer[offset : offset + 4] = struct.pack("<f", value)


def _write_be_i32(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 4] = struct.pack(">i", value)


def _write_be_u32(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 4] = struct.pack(">I", value & 0xFFFFFFFF)
