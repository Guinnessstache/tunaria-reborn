import json
import struct
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from eqoa_server.admin import AdminContext, AdminCredentials, AdminServer
from eqoa_server.auth import LOGIN_REQUEST, AuthRequest
from eqoa_server.config import ServerConfig, load_settings
from eqoa_server.netconfig import NetworkHelper
from eqoa_server.patch_tcp import MOTD_BODY_SIZE, fixed_motd_body
from eqoa_server.services import LogBuffer, ServiceManager
from eqoa_server.storage import JsonStore

PASSWORD = "test-admin-password"


def isolated_network() -> NetworkHelper:
    """Never let tests reach a real network helper installed on the machine running them."""
    missing = Path(tempfile.mkdtemp()) / "no-helper"
    return NetworkHelper(missing / "requests", missing / "status.json")


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
            AdminCredentials.load(self.config), self.stop_event, network=isolated_network(),
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
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def login(self):
        status, _ = self.request("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)

    def test_requires_login_and_rejects_wrong_password(self):
        self.assertEqual(self.request("/api/status")[0], 401)
        self.assertEqual(self.request("/api/login", {"password": "nope"})[0], 401)
        self.login()
        self.assertEqual(self.request("/api/status")[0], 200)

    def test_self_test_runs_and_takes_captures(self):
        self.login()
        capture = Path(__file__).resolve().parents[1] / "captures" / "from_sandstorm.pcapng"
        req = urllib.request.Request(self.base + "/api/selftest/capture?name=sandstorm.pcapng", data=capture.read_bytes(),
                                     headers={"Cookie": self.cookie, "X-EQOA-Admin": "1",
                                              "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(json.loads(response.read())["captures"][0]["name"], "sandstorm.pcapng")
        self.game_server().ignored_opcodes[0x10] = {"count": 2, "sample": ""}
        status, body = self.request("/api/selftest/run", {"quick": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["running"])
        deadline = time.time() + 120
        while body["running"] and time.time() < deadline:
            time.sleep(0.5)
            body = self.request("/api/selftest")[1]
        self.assertIsNone(body["error"])
        self.assertIn("1. [", body["report"]["text"])
        self.assertTrue(any(f["area"] == "live play" for f in body["report"]["findings"]))

    def game_server(self):
        return self.services.game_server

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
            "/api/settings", {"shard_name": "Test Shard", "motd": "Hello\r\nworld"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(settings["shard_name"], "Test Shard")
        body = fixed_motd_body(self.config)
        self.assertEqual(len(body), MOTD_BODY_SIZE)
        self.assertTrue(body.startswith(b"Hello\nworld"))

        reloaded = ServerConfig(data_path=self.config.data_path)
        load_settings(reloaded)
        self.assertEqual((reloaded.shard_name, reloaded.motd), ("Test Shard", "Hello\nworld"))

    def test_reset_password_and_toggle_checking(self):
        auth = self.services.services["auth"].server
        login = lambda token: auth.process_auth_request(AuthRequest(148, LOGIN_REQUEST, "tester", token), b"c" * 32)
        good = lambda response: struct.unpack_from(">I", response, 4)[0] == 0x25
        self.assertTrue(good(login("aa")))
        self.assertFalse(good(login("bb")))
        self.login()
        account = self.store.accounts["tester"]
        _, accounts = self.request("/api/accounts")
        self.assertTrue(accounts["accounts"][0]["password_set"])
        self.assertEqual(self.request(f"/api/accounts/{account.account_id}/reset-password", {})[0], 200)
        self.assertTrue(good(login("bb")))  # sets the new password
        self.assertFalse(good(login("aa")))

        self.request("/api/settings", {"check_passwords": False})
        self.assertTrue(good(login("aa")))
        reloaded = ServerConfig(data_path=self.config.data_path)
        load_settings(reloaded)
        self.assertFalse(reloaded.check_passwords)

    def test_give_equip_and_remove_items(self):
        from eqoa_server import items

        account = self.store.create_account("giver", "x")
        character = self.store.create_character(account.account_id, "Chanty", klass=12)
        self.login()
        status, found = self.request("/api/items?q=apprentice%20staff")
        self.assertEqual(status, 200)
        self.assertEqual(found["items"][0]["id"], items.APPRENTICE_STAFF)
        base = f"/api/characters/{character.character_id}"
        self.assertEqual(self.request(f"{base}/give", {"item": items.APPRENTICE_STAFF, "equip": True})[0], 200)
        self.assertEqual(self.request(f"{base}/give", {"item": items.APPRENTICE_SWORD, "equip": True})[0], 200)
        inventory = self.store.get_character(character.character_id).inventory
        # rations, then the staff (unequipped by the sword: two hands vs one) and the sword
        self.assertEqual([(e["item"], e["equip"]) for e in inventory[1:]],
                         [(items.APPRENTICE_STAFF, -1), (items.APPRENTICE_SWORD, items.EQUIP_PRIMARY)])
        self.assertEqual(self.request(f"{base}/items/0/equip", {})[0], 409)  # rations can't be worn
        self.assertEqual(self.request(f"{base}/give", {"item": 999999})[0], 409)
        _, accounts = self.request("/api/accounts")
        listed = accounts["accounts"][0]["characters"][0]["inventory"]
        self.assertEqual([i["equipped"] for i in listed], [False, False, True])
        self.assertEqual(self.request(f"{base}/items/2/remove", {})[0], 200)
        self.assertEqual(len(self.store.get_character(character.character_id).inventory), 2)

    def test_set_level_learns_abilities(self):
        from eqoa_server import items

        account = self.store.create_account("leveler", "x")
        warrior = self.store.create_character(account.account_id, "Brute", klass=0)
        self.login()
        base = f"/api/characters/{warrior.character_id}/level"
        status, result = self.request(base, {"level": 5, "learn_abilities": True})
        self.assertEqual(status, 200)
        names = [items.spell_patterns()[s["spell"]]["name"] for s in self.store.get_character(warrior.character_id).spells]
        for expected in ("Kick", "Concentration", "Tough Skin", "Taunt"):  # warrior levels 3-5 in the guide
            self.assertIn(expected, names)
        self.assertEqual(result["learned"], len(names) - 3)
        self.assertEqual(self.store.get_character(warrior.character_id).level, 5)
        self.assertEqual(self.request(base, {"level": 61})[0], 409)
        self.assertEqual(self.request(base, {"level": 30, "learn_abilities": False})[1]["learned"], 0)
        self.assertEqual(self.request(base, {"level": 40})[1]["learned"], 0)  # off unless asked

    def test_set_tunar(self):
        account = self.store.create_account("rich", "x")
        character = self.store.create_character(account.account_id, "Midas")
        self.login()
        base = f"/api/characters/{character.character_id}/tunar"
        self.assertEqual(self.request(base, {"tunar": 5000})[0], 200)
        self.assertEqual(self.store.get_character(character.character_id).tunar, 5000)
        self.assertEqual(self.request("/api/accounts")[1]["accounts"][0]["characters"][0]["tunar"], 5000)
        self.assertEqual(self.request(base, {"tunar": -1})[0], 409)

    def test_model_browser_previews_live_and_saves_item_models(self):
        from eqoa_server import items
        from eqoa_server.game_udp import GameSession

        account = self.store.create_account("browser", "x")
        character = self.store.create_character(account.account_id, "Chanty", klass=12)
        game = self.services.game_server
        session = GameSession(0x0AD0, 0x20AD1, phase3_in_world=True, selected_character=character)
        game.sessions[("127.0.0.1", 40000)] = session
        self.login()
        _, models = self.request("/api/models")
        self.assertIn("shield", models["categories"])
        self.assertEqual(models["in_world"][0]["name"], "Chanty")
        shield = models["categories"]["shield"][0]
        status, result = self.request("/api/preview", {"character": character.character_id, "slot": "shield", "model": shield})
        self.assertEqual(status, 200, result)
        self.assertEqual(struct.unpack_from("<I", session.phase3_self_update, 75)[0], shield)  # shield model offset
        self.assertFalse(session.phase3_self_update_acked)
        self.assertEqual(self.request("/api/preview", {"character": 999, "slot": "shield", "model": shield})[0], 409)

        from eqoa_server.memory_dump import ARMOR_OFFSET

        status, _ = self.request("/api/preview", {"character": character.character_id, "slot": "legs", "model": 6, "color": 13})
        self.assertEqual(status, 200)
        self.assertEqual((session.phase3_self_update[ARMOR_OFFSET + 3], session.phase3_self_update[ARMOR_OFFSET + 9]), (6, 13))
        self.addCleanup(items.set_model_override, 3701, None)  # Apprentice Cap
        self.request("/api/item-models", {"item": 3701, "model": 3, "color": 5})
        self.assertEqual((items.model_for(3701), items.color_for(3701)), (3, 5))

        self.addCleanup(items.set_model_override, items.APPRENTICE_STAFF, None)
        self.request("/api/item-models", {"item": items.APPRENTICE_STAFF, "model": shield})
        self.assertEqual(items.model_for(items.APPRENTICE_STAFF) & 0xFFFFFFFF, shield)
        self.assertEqual(items.model_source(items.APPRENTICE_STAFF), "chosen")
        self.request("/api/item-models", {"item": items.APPRENTICE_STAFF, "model": None})
        self.assertEqual(items.model_source(items.APPRENTICE_STAFF), "similar")

    def test_delete_account_removes_characters(self):
        account = self.store.create_account("deleteme", "x")
        self.store.create_character(account.account_id, "Gone")
        self.login()
        self.assertEqual(self.request(f"/api/accounts/{account.account_id}/delete", {})[0], 200)
        self.assertNotIn("deleteme", self.store.accounts)
        self.assertEqual(self.store.list_characters(account.account_id), [])

    def test_autostart_setting_is_saved_and_respected(self):
        self.login()
        status, result = self.request("/api/services/game/autostart", {"enabled": False})
        self.assertEqual(status, 200)
        game = next(s for s in result["services"] if s["name"] == "game")
        self.assertFalse(game["autostart"])
        self.assertEqual(self.request("/api/services/nope/autostart", {"enabled": False})[0], 400)

        reloaded = ServerConfig(data_path=self.config.data_path)
        load_settings(reloaded)
        self.assertEqual(reloaded.autostart, {"game": False})

        self.services.stop_all()
        self.services.start_enabled()
        running = {s["name"]: s["running"] for s in self.services.status()}
        self.assertEqual(running, {"auth": True, "motd": True, "game": False, "status": True})

    def test_boot_unavailable_without_helper(self):
        self.login()
        _, status = self.request("/api/status")
        self.assertFalse(status["boot"]["available"])

    def test_logs_and_backup(self):
        self.logs.append("first line\nsecond ")
        self.logs.append("line\n")
        self.login()
        _, logs = self.request("/api/logs?after=0")
        self.assertEqual([line["text"] for line in logs["lines"]], ["first line", "second line"])
        self.assertEqual(logs["started"], self.admin.context.started_at)
        _, logs = self.request(f"/api/logs?after={logs['lines'][0]['id']}")
        self.assertEqual([line["text"] for line in logs["lines"]], ["second line"])
        self.store.create_account("backup", "x")
        status, raw = self.request("/api/backup")
        self.assertEqual(status, 200)
        self.assertEqual(raw["accounts"][0]["username"], "backup")

    @mock.patch.dict(os.environ, {"EQOA_LAUNCHER": ""})
    def test_update_status_without_updater(self):
        # The updater runs this suite from a launcher-started server; behave as if started directly.
        self.login()
        status, update = self.request("/api/update")
        self.assertEqual(status, 200)
        self.assertFalse(update["available"])
        self.assertFalse(update["launcher"])
        status, result = self.request("/api/restart", {})
        self.assertEqual(status, 409)

    def test_explicit_password_cannot_be_changed_from_panel(self):
        self.login()
        status, result = self.request("/api/admin-password", {"current": PASSWORD, "new": "another-password"})
        self.assertEqual(status, 409)
        self.assertIn("--admin-password", result["error"])


class GeneratedPasswordTests(unittest.TestCase):
    """A generated admin password must be changed at first sign-in."""

    def setUp(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("EQOA_ADMIN_PASSWORD", None)
        self.config = ServerConfig(data_path=Path(tempfile.mkdtemp()) / "server_state.json", log_packets=False)
        self.credentials = AdminCredentials.load(self.config)
        self.generated = self.credentials.generated_password
        self.start_admin(self.credentials)

    def start_admin(self, credentials):
        store = JsonStore(self.config.data_path)
        services = ServiceManager(self.config, store)
        context = AdminContext(
            self.config, store, services, LogBuffer(), credentials, threading.Event(), network=isolated_network()
        )
        self.admin = AdminServer(("127.0.0.1", 0), context)
        threading.Thread(target=self.admin.serve_forever, daemon=True).start()
        self.addCleanup(self.admin.server_close)
        self.addCleanup(self.admin.shutdown)
        self.base = f"http://127.0.0.1:{self.admin.server_address[1]}"

    request = AdminPanelTests.request

    def test_forced_change_then_full_access_and_restart_keeps_session(self):
        self.cookie = None
        status, login = self.request("/api/login", {"password": self.generated})
        self.assertEqual(status, 200)
        self.assertTrue(login["must_change_password"])
        self.assertEqual(self.request("/api/status")[0], 403)
        self.assertEqual(self.request("/api/services/auth/stop", {})[0], 403)

        status, result = self.request("/api/admin-password", {"current": self.generated, "new": "short"})
        self.assertEqual(status, 409)
        status, _ = self.request("/api/admin-password", {"current": self.generated, "new": "my-new-password"})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/status")[0], 200)

        # A restart reloads credentials from disk; the signed session stays valid.
        self.admin.shutdown()
        self.admin.server_close()
        reloaded = AdminCredentials.load(self.config)
        self.assertFalse(reloaded.must_change)
        self.assertIsNone(reloaded.generated_password)
        self.start_admin(reloaded)
        self.assertEqual(self.request("/api/status")[0], 200)
        self.assertTrue(reloaded.check("my-new-password"))
        self.assertFalse(reloaded.check(self.generated))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_admin_secrets_are_private(self):
        data_dir = self.config.data_path.parent
        for name in ("admin.json", "admin_session.key"):
            self.assertEqual((data_dir / name).stat().st_mode & 0o777, 0o600, name)

    def test_password_change_invalidates_other_sessions(self):
        self.cookie = None
        self.request("/api/login", {"password": self.generated})
        old_cookie = self.cookie
        self.request("/api/admin-password", {"current": self.generated, "new": "my-new-password"})
        self.cookie = old_cookie
        self.assertEqual(self.request("/api/status")[0], 401)


if __name__ == "__main__":
    unittest.main()
