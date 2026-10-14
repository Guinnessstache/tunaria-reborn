"""eqoa-protocol — reverse-engineered EQOA (EverQuest Online Adventures)
phase3 network protocol, derived from packet captures.

Public API:
    from eqoa_protocol import (
        classify, handle, peek_header,
        try_parse_short_control, ShortControl,
        parse_reliable_control, build_reliable_ack, ReliableControl,
        EP_DISCOVERY, EP_WORLD, EP_NONE, endpoint_name,
    )
"""

from .endpoints import (
    EP_NONE, EP_DISCOVERY, EP_WORLD, endpoint_name, ENDPOINT_NAMES,
)
from .short_control import (
    try_parse_short_control, ShortControl, FLAG_BARE, SHORT_CONTROL_LEN,
)
from .reliable_control import (
    parse_reliable_control, build_reliable_ack, ReliableControl,
    FLAG_RELIABLE, DIR_TO_SERVER, DIR_TO_CLIENT, OP_RELIABLE,
)
from .packet import classify, handle, peek_header

__version__ = "0.1.0"

__all__ = [
    "EP_NONE", "EP_DISCOVERY", "EP_WORLD", "endpoint_name", "ENDPOINT_NAMES",
    "try_parse_short_control", "ShortControl", "FLAG_BARE", "SHORT_CONTROL_LEN",
    "parse_reliable_control", "build_reliable_ack", "ReliableControl",
    "FLAG_RELIABLE", "DIR_TO_SERVER", "DIR_TO_CLIENT", "OP_RELIABLE",
    "classify", "handle", "peek_header",
    "__version__",
]
