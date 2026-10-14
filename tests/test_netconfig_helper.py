"""Tests for deploy/tunaria-netconfig against a fake root with stub system commands."""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HELPER = Path(__file__).resolve().parents[1] / "deploy" / "tunaria-netconfig"

ORIGINAL_NETPLAN = """network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
"""

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{log}"
case "$(basename "$0") $*" in
  "netplan get ethernets") cat "{root}/stub/ethernets" ;;
  "netplan get ethernets."*".dhcp4") echo true ;;
  "netplan get "*) echo null ;;
  "netplan generate") [ -e "{root}/stub/fail-generate" ] && {{ echo "bad yaml" >&2; exit 1; }} ;;
  "ip -j -4 addr show") echo '[{{"ifname":"lo","addr_info":[{{"local":"127.0.0.1","prefixlen":8}}]}},{{"ifname":"eth0","flags":["UP"],"addr_info":[{{"local":"192.168.1.65","prefixlen":24}}]}}]' ;;
  "ip -j -4 route show default") echo '[{{"gateway":"192.168.1.1"}}]' ;;
esac
exit 0
"""


@unittest.skipIf(os.name == "nt", "the helper runs on Linux servers")
class NetconfigHelperTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.log = self.root / "calls.log"
        (self.root / "stub").mkdir()
        (self.root / "stub" / "ethernets").write_text("eth0:\n  dhcp4: true\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        env = {"TUNARIA_NETCONFIG_ROOT": str(self.root), "TUNARIA_NETCONFIG_REVERT_AFTER": "120"}
        for name in ("netplan", "ip", "systemctl", "systemd-run"):
            path = bin_dir / name
            path.write_text(STUB.format(log=self.log, root=self.root))
            path.chmod(0o755)
            env[f"TUNARIA_CMD_{name.upper().replace('-', '_')}"] = str(path)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for directory in ("etc/netplan", "etc/cloud/cloud.cfg.d", "opt/tunaria-reborn/netconfig",
                          "run/tunaria-netconfig", "var/lib/tunaria-netconfig", "run/systemd/resolve"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "etc/netplan/50-cloud-init.yaml").write_text(ORIGINAL_NETPLAN)
        (self.root / "run/systemd/resolve/resolv.conf").write_text("nameserver 192.168.1.1\n")
        loader = importlib.machinery.SourceFileLoader("tunaria_netconfig", str(HELPER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.helper = importlib.util.module_from_spec(spec)
        loader.exec_module(self.helper)

    def request(self, payload):
        (self.root / "opt/tunaria-reborn/netconfig/request.json").write_text(json.dumps(payload))
        self.helper.process()
        self.assertFalse((self.root / "opt/tunaria-reborn/netconfig/request.json").exists())
        return json.loads((self.root / "run/tunaria-netconfig/status.json").read_text())

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    STATIC = {"interface": "eth0", "method": "static", "address": "192.168.1.70", "prefix": 24,
              "gateway": "192.168.1.1", "dns": ["1.1.1.1", "8.8.8.8"]}

    def test_status_reports_current_network(self):
        status = self.request({"id": "s1", "action": "status"})
        self.assertTrue(status["ok"])
        state = status["state"]
        self.assertTrue(state["supported"], state["reason"])
        self.assertEqual(state["configured"], ["eth0"])
        self.assertEqual(state["method"], "dhcp")
        self.assertEqual(state["gateway"], "192.168.1.1")
        self.assertEqual(state["interfaces"], [{"name": "eth0", "addresses": ["192.168.1.65/24"], "up": True}])
        self.assertIsNone(status["pending"])

    def test_apply_static_writes_netplan_and_schedules_revert(self):
        status = self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        self.assertTrue(status["ok"], status["message"])
        ours = (self.root / "etc/netplan/90-tunaria.yaml").read_text()
        self.assertIn("addresses: [192.168.1.70/24]", ours)
        self.assertIn("via: 192.168.1.1", ours)
        self.assertIn("addresses: [1.1.1.1, 8.8.8.8]", ours)
        self.assertEqual((self.root / "etc/netplan/90-tunaria.yaml").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "etc/netplan/50-cloud-init.yaml").exists())
        self.assertTrue((self.root / "etc/cloud/cloud.cfg.d/99-tunaria-disable-network-config.cfg").exists())
        self.assertEqual(status["pending"]["id"], "a1")
        calls = self.calls()
        self.assertIn("netplan generate", calls)
        timer = next(i for i, c in enumerate(calls) if c.startswith("systemd-run --unit=tunaria-netconfig-revert --on-active=120"))
        # The undo timer must be running before the network changes.
        self.assertGreater(calls.index("netplan apply"), timer)

    def test_confirm_keeps_change(self):
        self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        status = self.request({"id": "c1", "action": "confirm"})
        self.assertTrue(status["ok"], status["message"])
        self.assertIsNone(status["pending"])
        self.assertTrue((self.root / "etc/netplan/90-tunaria.yaml").exists())
        self.assertIn("systemctl stop tunaria-netconfig-revert.timer", self.calls())

    def test_revert_restores_previous_files(self):
        self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        status = self.request({"id": "r1", "action": "revert"})
        self.assertTrue(status["ok"], status["message"])
        self.assertFalse((self.root / "etc/netplan/90-tunaria.yaml").exists())
        self.assertEqual((self.root / "etc/netplan/50-cloud-init.yaml").read_text(), ORIGINAL_NETPLAN)
        self.assertFalse((self.root / "etc/cloud/cloud.cfg.d/99-tunaria-disable-network-config.cfg").exists())
        self.assertIsNone(status["pending"])
        self.assertEqual(self.calls().count("netplan apply"), 2)

    def test_timer_revert_when_not_confirmed(self):
        self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        self.helper.revert_pending()
        status = json.loads((self.root / "run/tunaria-netconfig/status.json").read_text())
        self.assertIn("No confirmation", status["message"])
        self.assertEqual((self.root / "etc/netplan/50-cloud-init.yaml").read_text(), ORIGINAL_NETPLAN)

    def test_second_apply_refused_while_pending(self):
        self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        status = self.request({"id": "a2", "action": "apply", "config": {**self.STATIC, "address": "192.168.1.71"}})
        self.assertFalse(status["ok"])
        self.assertIn("already waiting", status["message"])

    def test_invalid_configs_are_refused_without_changes(self):
        bad = [
            {**self.STATIC, "interface": "eth9"},
            {**self.STATIC, "address": "192.168.1.255"},
            {**self.STATIC, "gateway": "10.0.0.1"},
            {**self.STATIC, "gateway": "192.168.1.70"},
            {**self.STATIC, "prefix": 4},
            {**self.STATIC, "dns": []},
            {**self.STATIC, "address": "not-an-ip"},
            {**self.STATIC, "interface": "eth0\nevil: 1"},
            {**self.STATIC, "method": "pppoe"},
        ]
        for config in bad:
            status = self.request({"id": "x", "action": "apply", "config": config})
            self.assertFalse(status["ok"], config)
            self.assertTrue((self.root / "etc/netplan/50-cloud-init.yaml").exists())
            self.assertFalse((self.root / "etc/netplan/90-tunaria.yaml").exists())

    def test_failed_generate_restores_files(self):
        (self.root / "stub" / "fail-generate").touch()
        status = self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        self.assertFalse(status["ok"])
        self.assertIn("bad yaml", status["message"])
        self.assertEqual((self.root / "etc/netplan/50-cloud-init.yaml").read_text(), ORIGINAL_NETPLAN)
        self.assertFalse((self.root / "etc/netplan/90-tunaria.yaml").exists())
        self.assertNotIn("netplan apply", self.calls())

    def test_multiple_interfaces_unsupported(self):
        (self.root / "stub" / "ethernets").write_text("eth0:\n  dhcp4: true\neth1:\n  dhcp4: true\n")
        status = self.request({"id": "a1", "action": "apply", "config": self.STATIC})
        self.assertFalse(status["ok"])
        self.assertIn("2 wired interfaces", status["message"])

    def test_dhcp(self):
        status = self.request({"id": "a1", "action": "apply", "config": {"interface": "eth0", "method": "dhcp"}})
        self.assertTrue(status["ok"], status["message"])
        self.assertIn("dhcp4: true", (self.root / "etc/netplan/90-tunaria.yaml").read_text())

    def test_symlinked_request_is_refused(self):
        target = self.root / "secret"
        target.write_text('{"action": "status"}')
        (self.root / "opt/tunaria-reborn/netconfig/request.json").symlink_to(target)
        self.helper.process()
        status = json.loads((self.root / "run/tunaria-netconfig/status.json").read_text())
        self.assertFalse(status["ok"])
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
