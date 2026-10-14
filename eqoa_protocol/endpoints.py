"""EQOA phase3 endpoint identifiers (little-endian u16 on the wire).

Observed in the Sandstorm capture. Endpoints identify the logical peer a
datagram is addressed to; the client re-binds across phases (discovery -> world).
"""

EP_NONE = 0xFFFE        # broadcast / "no endpoint yet" (initial client hello)
EP_DISCOVERY = 0x73B0   # auth / server-list / discovery phase
EP_WORLD = 0xB991       # in-world game session

# The client's own session endpoint is small (e.g. 0x0AD0) and is echoed as the
# low half of the 32-bit session id; it increments by phase (0x0AD0 -> 0x0AD1).

ENDPOINT_NAMES = {
    EP_NONE: "none/broadcast",
    EP_DISCOVERY: "discovery",
    EP_WORLD: "world",
}


def endpoint_name(ep: int) -> str:
    return ENDPOINT_NAMES.get(ep, f"0x{ep:04x}")
