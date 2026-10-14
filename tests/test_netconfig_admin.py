"""Admin panel <-> network helper round trip, with the helper run in-process on a fake root."""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from eqoa_server.admin import AdminContext, AdminCredentials, AdminServer
from eqoa_server.config import ServerConfig, load_settings
from eqoa_server.netconfig import NetworkHelper
from eqoa_server.services import LogBuffer, ServiceManager
from eqoa_server.storage import JsonStore

# Import the modules, not the classes, so unittest does not collect their tests here too.
import test_admin
import test_netconfig_helper

PASSWORD = "test-admin-password"


@unittest.skipIf(os.name == "nt", "the helper runs on Linux servers")
class NetworkAdminTests(unittest.TestCase):
    request = test_admin.AdminPanelTests.request

    def setUp(self):
        # Fake root + stub commands + the helper module, as in the helper tests.
        test_netconfig_helper.NetconfigHelperTests.setUp(self)
        self.network = NetworkHelper(
            self.root / "opt/tunaria-reborn/netconfig", self.root / "run/tunaria-netconfig/status.json"
        )
        self.stop_helper = threading.Event()
        threading.Thread(target=self.fake_systemd_path_unit, daemon=True).start()
        self.addCleanup(self.stop_helper.set)

        data_dir = Path(tempfile.mkdtemp())
        self.config = ServerConfig(
            host="127.0.0.1", public_host="192.168.1.65", data_path=data_dir / "server_state.json",
            admin_password=PASSWORD, log_packets=False,
        )
        store = JsonStore(self.config.data_path)
        context = AdminContext(
            self.config, store, ServiceManager(self.config, store), LogBuffer(),
            AdminCredentials.load(self.config), threading.Event(), network=self.network,
        )
        self.admin = AdminServer(("127.0.0.1", 0), context)
        threading.Thread(target=self.admin.serve_forever, daemon=True).start()
        self.addCleanup(self.admin.server_close)
        self.addCleanup(self.admin.shutdown)
        self.base = f"http://127.0.0.1:{self.admin.server_address[1]}"
        self.cookie = None
        self.assertEqual(self.request("/api/login", {"password": PASSWORD})[0], 200)

    def fake_systemd_path_unit(self):
        request = self.root / "opt/tunaria-reborn/netconfig/request.json"
        while not self.stop_helper.wait(0.05):
            if request.exists():
                self.helper.process()

    STATIC = {"interface": "eth0", "method": "static", "address": "192.168.1.70", "prefix": "24",
              "gateway": "192.168.1.1", "dns": ["1.1.1.1", " 8.8.8.8"]}

    def test_status_shows_current_network(self):
        status, network = self.request("/api/network")
        self.assertEqual(status, 200)
        self.assertTrue(network["helper"])
        self.assertEqual(network["public_host"], "192.168.1.65")
        state = network["status"]["state"]
        self.assertTrue(state["supported"])
        self.assertEqual(state["method"], "dhcp")

    def test_apply_then_keep_moves_game_address(self):
        status, result = self.request("/api/network/apply", {"config": self.STATIC})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"]["pending"]["new"]["address"], "192.168.1.70")
        status, result = self.request("/api/network/confirm", {})
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["status"]["pending"])
        self.assertEqual(result["public_host"], "192.168.1.70")
        reloaded = ServerConfig(data_path=self.config.data_path)
        load_settings(reloaded)
        self.assertEqual(reloaded.public_host, "192.168.1.70")

    def test_apply_then_undo_keeps_game_address(self):
        self.request("/api/network/apply", {"config": self.STATIC})
        status, result = self.request("/api/network/revert", {})
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["status"]["pending"])
        self.assertEqual(result["public_host"], "192.168.1.65")
        self.assertTrue((self.root / "etc/netplan/50-cloud-init.yaml").exists())

    def test_server_side_validation(self):
        status, result = self.request("/api/network/apply", {"config": {**self.STATIC, "gateway": "10.0.0.1"}})
        self.assertEqual(status, 409)
        self.assertIn("not in 192.168.1.0/24", result["error"])
        self.assertFalse((self.root / "etc/netplan/90-tunaria.yaml").exists())

    def test_set_game_address(self):
        status, result = self.request("/api/network/public-host", {"public_host": " 10.0.0.5 "})
        self.assertEqual(status, 200)
        self.assertEqual(result["public_host"], "10.0.0.5")
        self.assertEqual(self.request("/api/network/public-host", {"public_host": "example.com"})[0], 409)

    def test_start_at_boot(self):
        self.request("/api/network")  # first status report from the helper
        _, status = self.request("/api/status")
        self.assertEqual(status["boot"], {"available": True, "enabled": True, "raw": "enabled"})
        code, result = self.request("/api/boot", {"enabled": False})
        self.assertEqual(code, 200, result)
        self.assertFalse(result["boot"]["enabled"])
        _, status = self.request("/api/status")
        self.assertFalse(status["boot"]["enabled"])

    def test_helper_missing(self):
        self.admin.context.network = NetworkHelper(Path(tempfile.mkdtemp()) / "nope", Path("/nonexistent/status.json"))
        status, network = self.request("/api/network")
        self.assertEqual(status, 200)
        self.assertFalse(network["helper"])
        status, result = self.request("/api/network/apply", {"config": self.STATIC})
        self.assertEqual(status, 409)
        self.assertIn("not installed", result["error"])


if __name__ == "__main__":
    unittest.main()
