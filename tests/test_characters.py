import struct
import tempfile
import unittest
from pathlib import Path

from eqoa_server import game_data
from eqoa_server.capture_template import SANDSTORM_CAPTURE, _server_udp_payloads
from eqoa_server.protocol import (
    _write_zigzag,
    build_character_list_payload,
    parse_create_character,
)
from eqoa_server.storage import Character, JsonStore


def create_payload(name, level=1, race=0, klass=0, gender=0, hair=(0, 0, 0), face=0, human_type=0, points=(0,) * 7):
    out = struct.pack("<I", len(name)) + name.encode()
    for value in (level, race, klass, gender, *hair, face, human_type):
        out += _write_zigzag(value)
    return out + struct.pack("<7i", *points)


class CharacterCreationTests(unittest.TestCase):
    def test_parse_create_character(self):
        payload = create_payload("Elfy", race=1, klass=7, gender=1, hair=(3, 1, 2), face=4, points=(5, 0, 0, 10, 5, 0, 0))
        fields = parse_create_character(payload)
        self.assertEqual(fields["name"], "Elfy")
        self.assertEqual((fields["race"], fields["klass"], fields["gender"]), (1, 7, 1))
        self.assertEqual((fields["hair_color"], fields["hair_length"], fields["hair_style"], fields["face"]), (3, 1, 2, 4))
        self.assertEqual(fields["training_points"], [5, 0, 0, 10, 5, 0, 0])

    def test_new_character_gets_race_start_and_model(self):
        store = JsonStore(Path(tempfile.mkdtemp()) / "state.json")
        elf = store.create_character(1, "Elfy", race=1, klass=7, gender=1, hair_color=3)
        start = game_data.starting_character(7, 1, 0)
        self.assertEqual((elf.x, elf.y, elf.z), (start.x, start.y, start.z))
        self.assertEqual(elf.model_id, game_data.CHARACTER_MODELS[(1, 1)])
        human = store.create_character(1, "Qeyno", race=0, klass=0, human_type=2)
        self.assertEqual(human.x, game_data.starting_character(0, 0, 2).x)
        reloaded = JsonStore(store.path)
        self.assertEqual(reloaded.get_character(elf.character_id).hair_color, 3)

    def test_every_starting_character_has_a_model(self):
        for (_klass, race, _human_type) in game_data.STARTING_CHARACTERS:
            for gender in (0, 1):
                self.assertIn((race, gender), game_data.CHARACTER_MODELS)


@unittest.skipUnless(SANDSTORM_CAPTURE.is_file(), "Sandstorm capture not present")
class CharacterListTests(unittest.TestCase):
    def sandstorm_list(self) -> bytes:
        for packet in _server_udp_payloads(SANDSTORM_CAPTURE):
            index = packet.find(b"\x2c\x00\x10\x05\x00\x00\x00Kerbz")
            if index >= 0:
                return packet[index + 2 : -4]
        self.fail("character list not found in capture")

    def test_kerbz_record_matches_sandstorm_except_gear(self):
        kerbz = Character(character_id=8353, account_id=1, name="Kerbz", race=0, klass=3, level=27,
                          hair_color=2, hair_length=2, hair_style=0, face=2)
        ours = build_character_list_payload([kerbz])[1:]  # without the count
        theirs = self.sandstorm_list()[1:]
        name_and_fields = 4 + 5 + 15  # name length, name, zigzag fields
        self.assertEqual(ours[:name_and_fields], theirs[:name_and_fields])
        self.assertEqual(ours[name_and_fields : name_and_fields + 4], theirs[name_and_fields : name_and_fields + 4])  # robe -1
        self.assertEqual(len(ours), 4 + 5 + 47)  # every Sandstorm record is 47 bytes after the name
        stance = name_and_fields + 4 + 1 + 12
        self.assertEqual(ours[stance : stance + 3], theirs[stance : stance + 3])

    def test_character_select_packet_carries_the_list(self):
        from eqoa_server.protocol import build_phase2_character_select_resend, parse_udp_packet

        characters = [Character(character_id=i, account_id=1, name=f"Char{i}") for i in range(1, 9)]
        listing = build_character_list_payload(characters)
        packet = parse_udp_packet(build_phase2_character_select_resend(0x0AD0, 0x20AD1, character_list=listing))
        message = next(m for m in packet.messages if m.opcode == 0x002C)
        self.assertEqual(message.message_number, 3)
        self.assertEqual(message.payload, listing)

    def test_create_reply_is_exactly_one_list_message(self):
        from eqoa_server.protocol import build_character_list_response, rdp_crc

        characters = [Character(character_id=1, account_id=1, name="Magey"), Character(character_id=2, account_id=1, name="Elfy", race=1, gender=1)]
        packet = build_character_list_response(0x0AD0, 0x20AD1, 4, 4, characters)
        self.assertEqual(packet[-4:], struct.pack("<I", rdp_crc(packet[:-4])))
        body = packet[4 + 2 + 4 : -4]  # endpoints, flags, instance; CRC
        self.assertEqual(body[0], 0x20)
        message = body[3:]
        self.assertEqual(message[0], 0xFB)
        size = message[1]
        self.assertEqual(struct.unpack_from("<HH", message, 2), (4, 0x002C))
        # size counts the opcode and payload; nothing may follow the message
        self.assertEqual(len(message), 2 + 2 + size)
        self.assertEqual(message[6:], build_character_list_payload(characters))

    def test_count_is_zigzag(self):
        self.assertEqual(self.sandstorm_list()[0], 0x10)  # 8 characters
        two = [Character(character_id=i, account_id=1, name=f"C{i}") for i in (1, 2)]
        self.assertEqual(build_character_list_payload(two)[0], 0x04)


if __name__ == "__main__":
    unittest.main()
