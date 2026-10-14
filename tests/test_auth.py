import struct
import unittest
from pathlib import Path
from shutil import rmtree

from eqoa_server.auth import (
    LOGIN_REQUEST,
    LOGIN_RESPONSE,
    SUBMIT_ACCT_CREATE,
    build_login_response,
    parse_auth_request,
)
from eqoa_server.storage import JsonStore


def fixed(value: str, size: int) -> bytes:
    return value.encode("ascii").ljust(size, b"\x00")


class AuthTests(unittest.TestCase):
    def test_parse_login_request(self):
        body = (
            struct.pack(">II", 0, LOGIN_REQUEST)
            + bytes(8)
            + fixed("tester", 32)
            + fixed("encrypted-token", 32)
            + bytes(64)
        )
        packet = struct.pack(">I", len(body) + 4) + body[4:]
        parsed = parse_auth_request(packet)
        self.assertEqual(parsed.message_type, LOGIN_REQUEST)
        self.assertEqual(parsed.username, "tester")
        self.assertEqual(parsed.password_token, "encrypted-token")

    def test_login_response_shape(self):
        response = build_login_response(7)
        self.assertEqual(struct.unpack_from(">I", response, 0)[0], 168)
        self.assertEqual(struct.unpack_from(">I", response, 4)[0], LOGIN_RESPONSE)
        self.assertEqual(struct.unpack_from(">I", response, 80)[0], 7)
        self.assertEqual(struct.unpack_from(">I", response, 84)[0], 3)
        self.assertEqual(response[160:168], b"\xff" * 8)

    def test_store_creates_and_authenticates(self):
        tmp = Path(".testdata")
        rmtree(tmp, ignore_errors=True)
        try:
            store = JsonStore(tmp / "state.json")
            account = store.create_account("Tester", "token")
            self.assertIsNotNone(account)
            self.assertIsNotNone(store.authenticate("tester", "token"))
            self.assertIsNone(store.authenticate("tester", "wrong"))
        finally:
            rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
