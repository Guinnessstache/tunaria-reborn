from __future__ import annotations

import struct

from . import items
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


# Own-character object update (channel 0) as the Sandstorm server sent it
# (captures/from_sandstorm.pcapng). Frontiers expects this 162-byte layout.
# Model hash, appearance and equipment bytes are kept from the captured character.
SELF_OBJECT_UPDATE_TEMPLATE = bytes.fromhex(
    "01d00ac7188220e14a0061e91de76b970000000000000000000001ffc698d870"
    "000000000000803f0000000000000000000000000000b4306d40000000000000"
    "000000d669472100000000686552d30000000000000000000000000100040404"
    "0604040808080d080802020002ff0006000000004b6572627a00000000000000"
    "0000000000000000000000001b00020100030000ffffffff0018000001007473"
    "7271"
)
OBJECT_ID_OFFSET = 1
MODEL_OFFSET = 28
WEAPONS_OFFSET = 67  # primary, secondary, shield models (int32 each)
ARMOR_OFFSET = 93  # chest, bracer, gloves, legs, boots, helm models, then their 6 colors
HAIR_OFFSET = 105  # hair color, hair length, hair style, face
ROBE_OFFSET = 109
POSITION_OFFSET = 5
HEADING_BYTE_OFFSET = 15
HEADING_FLOAT_OFFSET = 54
NAME_OFFSET = 116
NAME_LENGTH = 24
LEVEL_OFFSET = 140
RACE_OFFSET = 144
CLASS_OFFSET = 145


def build_object_update(character: Character, object_id: int, preview: dict[str, int] | None = None) -> bytes:
    update = bytearray(SELF_OBJECT_UPDATE_TEMPLATE)
    x, y, z, heading = _spawn_position(character)
    _write_u32(update, OBJECT_ID_OFFSET, object_id)
    update[POSITION_OFFSET] = 0x82
    for index, value in enumerate((x, y, z)):
        offset = POSITION_OFFSET + 1 + index * 3
        update[offset : offset + 3] = struct.pack(">i", round(value * 128.0))[1:]
    update[HEADING_BYTE_OFFSET] = _heading_byte(heading)
    _write_f32(update, HEADING_FLOAT_OFFSET, heading)
    name = character.name.encode("ascii", "replace")[: NAME_LENGTH - 1]
    update[NAME_OFFSET : NAME_OFFSET + NAME_LENGTH] = name.ljust(NAME_LENGTH, b"\x00")
    update[LEVEL_OFFSET] = max(1, min(character.level, 60))
    update[RACE_OFFSET] = character.race & 0xFF
    update[CLASS_OFFSET] = character.klass & 0xFF
    # Appearance: this character's model, hair / face and equipped gear (the template's
    # are the captured character's).
    _write_u32(update, MODEL_OFFSET, character.model_id)
    worn = items.gear(character.inventory)
    weapons = {"primary": worn.primary, "secondary": worn.secondary, "shield": worn.shield}
    weapons.update({slot: model for slot, model in (preview or {}).items() if slot in weapons})
    update[WEAPONS_OFFSET : WEAPONS_OFFSET + 12] = struct.pack(
        "<iii", *(items._signed(weapons[slot]) for slot in ("primary", "secondary", "shield"))
    )
    armor, colors = bytearray(worn.armor), bytearray(worn.colors)
    for index, part in enumerate(items.ARMOR_PARTS):
        if part in (preview or {}):
            armor[index] = preview[part] & 0xFF
        if f"{part}_color" in (preview or {}):
            colors[index] = preview[f"{part}_color"] & 0xFF
    update[ARMOR_OFFSET : ARMOR_OFFSET + 12] = bytes(armor) + bytes(colors)
    update[HAIR_OFFSET : HAIR_OFFSET + 4] = bytes(
        value & 0xFF for value in (character.hair_color, character.hair_length, character.hair_style, character.face)
    )
    update[ROBE_OFFSET] = worn.robe & 0xFF
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
