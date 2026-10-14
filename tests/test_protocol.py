import struct
import unittest

from eqoa_server.protocol import (
    MessageType,
    PacketBundleFlags,
    build_old_world_ack,
    build_old_world_list,
    build_create_character_response,
    build_phase3_guaranteed_ack,
    build_phase3_packet,
    build_phase3_server_ping,
    build_phase3_world_entry,
    build_phase2_character_select_ack,
    build_phase2_character_select_resend,
    build_phase2_guaranteed_ack,
    parse_udp_packet,
    parse_create_character_payload,
    parse_phase3_client_packet,
    rdp_crc,
    read_varuint,
    write_varuint,
)


class ProtocolTests(unittest.TestCase):
    def test_varuint_round_trip(self):
        for value in [0, 1, 127, 128, 2048, 0xABCDEF]:
            encoded = write_varuint(value)
            decoded, offset = read_varuint(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(encoded))

    def test_parse_minimal_reliable_message(self):
        payload = struct.pack("<H", 0x000B) + b"hello"
        data = bytearray()
        data += struct.pack("<HH", 0x1111, 0xFFFE)
        data += write_varuint(0)
        data += bytes([PacketBundleFlags.PROCESS_MESSAGES])
        data += struct.pack("<H", 5)
        data += bytes([MessageType.RELIABLE])
        data += struct.pack("<H", 9)
        data += bytes([len(payload)])
        data += payload
        packet = parse_udp_packet(bytes(data))
        self.assertEqual(packet.client_endpoint, 0x1111)
        self.assertEqual(packet.client_bundle_number, 5)
        self.assertEqual(packet.messages[0].opcode_name, "ClientSayChat")

    def test_old_world_list_shape_and_crc(self):
        packet = build_old_world_list(0x1234, "192.168.1.32", 10070, "Local EQOA")
        self.assertEqual(len(packet), 228)
        self.assertEqual(packet[:2], b"\xb0\x73")
        self.assertEqual(packet[2:4], b"\x34\x12")
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))
        self.assertEqual(packet[32 + 23 : 32 + 25], b"\x91\xb9")
        self.assertEqual(packet[32 + 25 : 32 + 27], struct.pack("<H", 10070))
        self.assertEqual(packet[32 + 27 : 32 + 31], bytes([32, 1, 168, 192]))

    def test_old_world_ack_shape_and_crc(self):
        packet = build_old_world_ack(0x1234)
        self.assertEqual(len(packet), 27)
        self.assertEqual(packet[:2], b"\xb0\x73")
        self.assertEqual(packet[2:4], b"\x34\x12")
        self.assertEqual(struct.unpack_from("<I", packet, 23)[0], rdp_crc(packet[:23]))

    def test_phase2_character_select_ack_shape_and_crc(self):
        packet = build_phase2_character_select_ack(0x1234, 0x55667788)
        self.assertEqual(len(packet), 48)
        self.assertEqual(packet[:4], b"\x91\xb9\x34\x12")
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))
        self.assertIn(b"\x2c\x00", packet)

    def test_phase2_character_select_resend_shape_and_crc(self):
        packet = build_phase2_character_select_resend(0x1234, 0x55667788)
        self.assertEqual(len(packet), 44)
        self.assertEqual(packet[:4], b"\x91\xb9\x34\x12")
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))
        self.assertIn(b"\x2c\x00", packet)

    def test_phase2_guaranteed_ack_shape_and_crc(self):
        packet = build_phase2_guaranteed_ack(0x1234, 0x55667788, 3, 9, 4)
        self.assertEqual(len(packet), 21)
        self.assertEqual(packet[:4], b"\x91\xb9\x34\x12")
        self.assertEqual(packet[10], 0x23)
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))

    def test_create_character_response_shape_and_crc(self):
        create_payload = struct.pack("<I", 9) + b"Sneakyguy" + bytes([2, 12, 12, 0, 4, 4, 6, 4])
        packet = build_create_character_response(0x1234, 0x55667788, 4, 7, create_payload)
        self.assertEqual(packet[:4], b"\x91\xb9\x34\x12")
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))
        self.assertIn(b"\x2c\x00\x02\x09\x00\x00\x00Sneakyguy", packet)
        name, appearance = parse_create_character_payload(create_payload)
        self.assertEqual(name, b"Sneakyguy")
        self.assertEqual(appearance, bytes([2, 12, 12, 0, 4, 4, 6, 4]))

    def test_phase3_world_entry_shape_and_crc(self):
        packet = build_phase3_world_entry(0x1234, 0x55667788, arrival=1, now_us=123456)
        self.assertEqual(packet[:4], b"\x34\x12\x34\x12")
        self.assertIn(b"\xb8\xd0\xcf\xad\x01", packet)
        self.assertIn(b"\xd1\x07\xff\xff", packet)
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))

    def test_phase3_packet_wraps_inner_body(self):
        packet = build_phase3_packet(0x1234, 0x55667788, b"\x20\x02\x00", new_instance=False)
        self.assertEqual(packet[:4], b"\x34\x12\x34\x12")
        self.assertIn(b"\xb8\xd0\xcf\xad\x01\x20\x02\x00", packet)
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))

    def test_phase3_guaranteed_ack_shape_and_crc(self):
        packet = build_phase3_guaranteed_ack(0x1234, 0x55667788, 32, 2, 4)
        self.assertEqual(packet[:4], b"\x34\x12\x34\x12")
        self.assertIn(b"\xb8\xd0\xcf\xad\x01\x23\x20\x00\x02\x00\x04\x00", packet)
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))

    def test_parse_phase3_client_update_does_not_invent_reliable_sequence(self):
        inner = b"\x20\x09\x00" + b"\x40\x75\x00\x01abcdef"
        packet = build_phase3_packet(0x1234, 0x55667788, inner, new_instance=False)
        parsed = parse_phase3_client_packet(packet)
        self.assertEqual(parsed.arrival, 9)
        self.assertTrue(parsed.has_state_update)
        self.assertFalse(parsed.has_ping)
        self.assertEqual(parsed.max_reliable_sequence, 0)

    def test_parse_phase3_reliable_messages(self):
        reliable = b"\xfb\x04\x07\x00\x0e\x00"
        inner = b"\x20\x02\x00" + reliable
        packet = build_phase3_packet(0x1234, 0x55667788, inner, new_instance=False)
        parsed = parse_phase3_client_packet(packet)
        self.assertEqual(parsed.arrival, 2)
        self.assertEqual(parsed.max_reliable_sequence, 7)

    def test_phase3_server_ping_shape_and_crc(self):
        packet = build_phase3_server_ping(0x1234, 0x55667788, 33)
        self.assertEqual(packet[:4], b"\x34\x12\x34\x12")
        self.assertIn(b"\xb8\xd0\xcf\xad\x01\x20\x21\x00\xf9\x01", packet)
        self.assertEqual(struct.unpack_from("<I", packet, len(packet) - 4)[0], rdp_crc(packet[:-4]))


if __name__ == "__main__":
    unittest.main()
