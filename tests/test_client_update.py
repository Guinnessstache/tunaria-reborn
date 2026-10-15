import math
import struct
import unittest

from eqoa_server.capture_template import SANDSTORM_CAPTURE, memory_dump_core_length, sandstorm_memory_dump_payload
from eqoa_server.protocol import (
    MessageType,
    apply_client_update_delta,
    decode_client_update,
    parse_udp_packet,
    rle_zero_decode,
)


def client_updates():
    data = SANDSTORM_CAPTURE.read_bytes()
    offset = 0
    while offset + 12 <= len(data):
        block_type, block_len = struct.unpack_from("<II", data, offset)
        body = data[offset + 8 : offset + block_len - 4]
        offset += block_len
        if block_type != 6:
            continue
        frame = body[20 : 20 + struct.unpack_from("<I", body, 12)[0]]
        if frame[12:14] != b"\x08\x00" or frame[23] != 17:
            continue
        ip = frame[14:]
        udp = ip[(ip[0] & 15) * 4 :]
        if struct.unpack_from("!H", udp, 2)[0] != 10070:
            continue
        try:
            packet = parse_udp_packet(udp[8 : struct.unpack_from("!H", udp, 4)[0]])
        except Exception:
            continue
        yield from (m for m in packet.messages if m.message_type == MessageType.CLIENT_UPDATE)


@unittest.skipUnless(SANDSTORM_CAPTURE.is_file(), "Sandstorm capture not present")
class ClientUpdateTests(unittest.TestCase):
    def test_updates_decode_to_the_spawn_point(self):
        payload = sandstorm_memory_dump_payload()
        core = memory_dump_core_length(payload)
        spawn_x, spawn_y, spawn_z, spawn_heading = struct.unpack_from("<4f", payload, core - 24)
        decoded = {}
        for message in client_updates():
            update = rle_zero_decode(message.payload)
            if message.xor:
                update = apply_client_update_delta(decoded[message.message_number - message.xor], update)
            decoded[message.message_number] = update
            position = decode_client_update(update)
            self.assertEqual(len(update), 41)
            self.assertEqual(position.world, 0)
            self.assertAlmostEqual(position.x, spawn_x, delta=0.01)
            self.assertAlmostEqual(position.y, spawn_y, delta=0.01)
            self.assertAlmostEqual(position.z, spawn_z, delta=0.01)
            turn = (position.heading - spawn_heading) % (2 * math.pi)
            self.assertLess(min(turn, 2 * math.pi - turn), 2 * math.pi / 256 * 1.5)
        self.assertGreaterEqual(len(decoded), 2)

    def test_rle_decode_undoes_the_server_encoder(self):
        from eqoa_server.protocol import _rle_zero_encode

        sample = bytes([0, 0, 5, 6, 0, 0, 0, 7] + [0] * 300 + [9])
        self.assertEqual(rle_zero_decode(_rle_zero_encode(sample)), sample)


if __name__ == "__main__":
    unittest.main()
