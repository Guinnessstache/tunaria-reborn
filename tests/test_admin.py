import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from eqoa_server.admin import AdminContext, AdminCredentials, AdminServer
from eqoa_server.auth import LOGIN_BAD_PASS, LOGIN_REQUEST, AuthRequest
from eqoa_server.config import ServerConfig, load_settings
from eqoa_server.patch_tcp import MOTD_BODY_SIZE, fixed_motd_body
from eqoa_server.services import LogBuffer, ServiceManager
from eqoa_server.storage import JsonStore

PASSWORD = "test-admin-password"


class AdminPanelTests(unittest.TestCase):
    def setUp(self):
        data_dir = Path(tempfile.mkdtemp())
        self.config = ServerConfig(
            host="127.0.0.1",
            auth_port=0,
            game_port=0,
            motd_port=0,
            patch_port=0,
            data_path=data_dir / "server_state.json",
            admin_password=PASSWORD,
            log_packets=False,
        )
        self.store = JsonStore(self.config.data_path)
        self.services = ServiceManager(self.config, self.store)
        self.services.start_all()
        self.logs = LogBuffer()
        self.stop_event = threading.Event()
        context = AdminContext(
            self.config, self.store, self.services, self.logs,
            AdminCredentials.load(self.config), self.stop_event,
        )
        self.admin = AdminServer(("127.0.0.1", 0), context)
        threading.Thread(target=self.admin.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.admin.server_address[1]}"
        self.cookie = None

    def tearDown(self):
        self.services.stop_all()
        self.admin.shutdown()
        self.admin.server_close()

    def request(self, path, body=None, *, admin_header=True):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if admin_header:
                headers["X-EQOA-Admin"] = "1"
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                cookie = response.headers.get("Set-Cookie")
                if cookie:
                    self.cookie = cookie.split(";", 1)[0]
                raw = response.read()
                return response.status, (json.loads(raw) if "json" in response.headers["Content-Type"] else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def login(self):
        status, _ = self.request("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)

    def test_requires_login_and_rejects_wrong_password(self):
        self.assertEqual(self.request("/api/status")[0], 401)
        self.assertEqual(self.request("/api/login", {"password": "nope"})[0], 401)
        self.login()
        self.assertEqual(self.request("/api/status")[0], 200)

    def test_post_without_admin_header_is_refused(self):
        self.login()
        status, _ = self.request("/api/services/auth/stop", {}, admin_header=False)
        self.assertEqual(status, 403)
        self.assertTrue(self.services.services["auth"].running)

    def test_stop_and_start_service(self):
        self.login()
        port = self.services.port("auth")
        status, result = self.request("/api/services/auth/stop", {})
        self.assertEqual(status, 200)
        auth = next(s for s in result["services"] if s["name"] == "auth")
        self.assertFalse(auth["running"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

        status, result = self.request("/api/services/auth/start", {})
        auth = next(s for s in result["services"] if s["name"] == "auth")
        self.assertTrue(auth["running"])
        socket.create_connection(("127.0.0.1", auth["port"]), timeout=1).close()

    def test_status_lists_services_and_system(self):
        self.login()
        _, status = self.request("/api/status")
        self.assertEqual({s["name"] for s in status["services"]}, {"auth", "motd", "game", "status"})
        self.assertTrue(all(s["running"] for s in status["services"]))
        self.assertIn("uptime", status["system"])

    def test_settings_update_motd_and_persist(self):
        self.login()
        status, settings = self.request(
            "/api/settings", {"shard_name": "Test Shard", "motd": "Hello\r\nworld", "enforce_passwords": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(settings["shard_name"], "Test Shard")
        body = fixed_motd_body(self.config)
        self.assertEqual(len(body), MOTD_BODY_SIZE)
        self.assertTrue(body.startswith(b"Hello\nworld"))

        reloaded = ServerConfig(data_path=self.config.data_path)
        load_settings(reloaded)
        self.assertEqual((reloaded.shard_name, reloaded.motd, reloaded.enforce_passwords), ("Test Shard", "Hello\nworld", True))

    def test_password_reset_and_enforcement(self):
        auth = self.services.services["auth"].server
        login = lambda token: auth.process_auth_request(AuthRequest(148, LOGIN_REQUEST, "tester", token))
        result_code = lambda response: int.from_bytes(response[8:12], "big")

        self.assertEqual(result_code(login("aa")), 0)
        self.config.enforce_passwords = True
        self.assertEqual(result_code(login("bb")), LOGIN_BAD_PASS)
        self.assertEqual(result_code(login("aa")), 0)

        self.login()
        account_id = self.store.accounts["tester"].account_id
        self.assertEqual(self.request(f"/api/accounts/{account_id}/reset-password", {})[0], 200)
        self.assertEqual(result_code(login("bb")), 0)
        _, accounts = self.request("/api/accounts")
        self.assertTrue(accounts["accounts"][0]["password_set"])

    def test_delete_account_removes_characters(self):
        account = self.store.create_account("deleteme", "x")
        self.store.create_character(account.account_id, "Gone")
        self.login()
        self.assertEqual(self.request(f"/api/accounts/{account.account_id}/delete", {})[0], 200)
        self.assertNotIn("deleteme", self.store.accounts)
        self.assertEqual(self.store.list_characters(account.account_id), [])

    def test_logs_and_backup(self):
        self.logs.append("first line\nsecond ")
        self.logs.append("line\n")
        self.login()
        _, logs = self.request("/api/logs?after=0")
        self.assertEqual([line["text"] for line in logs["lines"]], ["first line", "second line"])
        _, logs = self.request(f"/api/logs?after={logs['lines'][0]['id']}")
        self.assertEqual([line["text"] for line in logs["lines"]], ["second line"])
        self.store.create_account("backup", "x")
        status, raw = self.request("/api/backup")
        self.assertEqual(status, 200)
        self.assertEqual(raw["accounts"][0]["username"], "backup")


if __name__ == "__main__":
    unittest.main()
