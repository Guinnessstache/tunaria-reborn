"""Admin panel side of the network settings feature.

The server never changes the network itself. It drops a request file for the
root helper (deploy/tunaria-netconfig, run by tunaria-netconfig.path) and reads
the helper's answer from its status file. See docs/linux-server.md.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import time
from pathlib import Path

REQUESTS_DIR = Path("/opt/tunaria-reborn/netconfig")
STATUS_PATH = Path("/run/tunaria-netconfig/status.json")


class NetworkError(Exception):
    pass


def local_ipv4_addresses() -> list[str]:
    """This machine's IPv4 addresses, the one used for outgoing traffic first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; picks the outgoing interface
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found


def validate_public_host(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise NetworkError("the game address must be an IPv4 address such as 192.168.1.65") from None
    if address.is_unspecified or address.is_multicast:
        raise NetworkError(f"{address} cannot be used as the game address")
    return str(address)


def validate_network_config(config: dict) -> dict:
    """Friendly early checks; the root helper validates again and is the real gatekeeper."""
    method = config.get("method")
    interface = str(config.get("interface", ""))
    if not interface:
        raise NetworkError("choose a network interface")
    if method == "dhcp":
        return {"interface": interface, "method": "dhcp"}
    if method != "static":
        raise NetworkError("choose DHCP or a static address")
    try:
        network = ipaddress.IPv4Interface(f"{str(config.get('address', '')).strip()}/{int(config.get('prefix') or 0)}")
        gateway = ipaddress.IPv4Address(str(config.get("gateway", "")).strip())
        dns = [ipaddress.IPv4Address(part.strip()) for part in config.get("dns", []) if str(part).strip()]
    except (ValueError, TypeError) as exc:
        raise NetworkError(f"invalid address: {exc}") from None
    if gateway not in network.network:
        raise NetworkError(f"the gateway {gateway} is not in {network.network}")
    if not dns:
        raise NetworkError("give at least one DNS server")
    return {
        "interface": interface, "method": "static", "address": str(network.ip),
        "prefix": network.network.prefixlen, "gateway": str(gateway), "dns": [str(d) for d in dns][:3],
    }


class NetworkHelper:
    def __init__(self, requests_dir: Path = REQUESTS_DIR, status_path: Path = STATUS_PATH):
        self.requests_dir = requests_dir
        self.status_path = status_path

    def available(self) -> bool:
        return (
            self.requests_dir.is_dir()
            and os.access(self.requests_dir, os.W_OK)
            and self.status_path.parent.is_dir()
        )

    def status(self) -> dict | None:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def send(self, action: str, config: dict | None = None, timeout: float = 10.0) -> dict:
        if not self.available():
            raise NetworkError("the network helper is not installed; run the install command again on the server")
        request_id = secrets.token_hex(8)
        payload = {"id": request_id, "action": action}
        if config is not None:
            payload["config"] = config
        tmp = self.requests_dir / f".request-{request_id}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.requests_dir / "request.json")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status and status.get("request_id") == request_id:
                return status
            time.sleep(0.2)
        raise NetworkError("the network helper did not answer; check: journalctl -u tunaria-netconfig")
