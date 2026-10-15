import struct
import tempfile
import unittest
from pathlib import Path

from eqoa_server.auth import (
    ACCT_CREATE_RESPONSE,
    AuthRequest,
    AuthServer,
    LOGIN_REQUEST,
    LOGIN_RESPONSE,
    SUBMIT_ACCT_CREATE,
    build_login_response,
    parse_auth_request,
)
from eqoa_server.config import ServerConfig
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

    def test_store_checks_passwords(self):
        tmp = Path(tempfile.mkdtemp())
        store = JsonStore(tmp / "state.json")
        self.assertEqual(store.check_login("Tester", "s3cr3t-tok")[1], "created")
        self.assertEqual(store.check_login("tester", "s3cr3t-tok")[1], "ok")
        self.assertEqual(store.check_login("TESTER", "wrong"), (None, "wrong"))
        self.assertNotIn("s3cr3t-tok", (tmp / "state.json").read_text())  # only a hash is stored

        account = store.accounts["tester"]
        self.assertTrue(store.reset_password(account.account_id))
        self.assertEqual(store.check_login("tester", "new")[1], "set")
        self.assertEqual(store.check_login("tester", "s3cr3t-tok")[1], "wrong")
        self.assertEqual(JsonStore(tmp / "state.json").check_login("tester", "new")[1], "ok")

    def test_raw_tokens_from_older_versions_are_reset(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "state.json").write_text(
            '{"accounts": [{"account_id": 1, "username": "ladmin", "password_token": "oldtoken"}]}'
        )
        store = JsonStore(tmp / "state.json")
        self.assertEqual(store.check_login("ladmin", "anything")[1], "set")

    def test_wrong_password_is_refused_unless_checking_is_off(self):
        store = JsonStore(Path(tempfile.mkdtemp()) / "state.json")
        config = ServerConfig(data_path=store.path)
        server = AuthServer(("127.0.0.1", 0), store, config)
        self.addCleanup(server.server_close)
        login = lambda token: server.process_auth_request(AuthRequest(148, LOGIN_REQUEST, "tester", token))
        opcode = lambda response: struct.unpack_from(">I", response, 4)[0]
        self.assertEqual(opcode(login("right")), LOGIN_RESPONSE)
        self.assertEqual(opcode(login("right")), LOGIN_RESPONSE)
        refused = login("wrong")
        self.assertEqual(opcode(refused), ACCT_CREATE_RESPONSE)
        self.assertIn(b"Wrong password", refused)
        config.check_passwords = False
        self.assertEqual(opcode(login("wrong")), LOGIN_RESPONSE)

if __name__ == "__main__":
    unittest.main()
