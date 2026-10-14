from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path


SANDSTORM_HIGH = "3c0305a1"
SANDSTORM_LOW = "3463d9bc"


def mips_lui_ori_for_ip(ip: str) -> tuple[str, str]:
    value = int(ipaddress.IPv4Address(ip))
    high = (value >> 16) & 0xFFFF
    low = value & 0xFFFF
    return f"3c03{high:04x}", f"3463{low:04x}"


def make_local_pnach(source: Path, output: Path, ip: str) -> None:
    high, low = mips_lui_ori_for_ip(ip)
    text = source.read_text(encoding="utf-8")
    if SANDSTORM_HIGH not in text or SANDSTORM_LOW not in text:
        raise ValueError("source pnach did not contain the expected Sandstorm IP patch words")
    text = text.replace("[Sandstorm DNAS/DNS/code patch]", "[EQOA Local DNAS/DNS/code patch]")
    text = text.replace(
        "description=DNAS bypass, Sandstorm DNS fix/USA server, Sandstorm code patcher",
        f"description=DNAS bypass, local DNS/server patch for {ip}, Sandstorm code patcher base",
    )
    text = text.replace(SANDSTORM_HIGH, high).replace(SANDSTORM_LOW, low)
    output.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a local-server EQOA Frontiers pnach.")
    parser.add_argument("--source", type=Path, default=Path("tools/eeee1fcc.sandstorm.pnach"))
    parser.add_argument("--output", type=Path, default=Path("tools/eeee1fcc.local.pnach"))
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    make_local_pnach(args.source, args.output, args.ip)
    print(args.output)


if __name__ == "__main__":
    main()
