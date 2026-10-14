"""Tests for phase3 control packets, keyed to the captured bytes in the
Sandstorm reference pcap."""

from eqoa_protocol import (
    classify, try_parse_short_control, parse_reliable_control,
    build_reliable_ack, EP_DISCOVERY, EP_WORLD,
)

# --- bare control (0x80), 19 bytes ---
SHORT = {
    "disc": "d00ab07380e014d00a0200d00a0200e2ca4c98",
    "world": "d00a91b980e014d10a0200d10a0200180f2138",
    # the field-reported failing packet (different session base, same shape)
    "failing": "4ccab07380e0144cca01004cca0100e5c3246a",
}

# --- reliable control (0x87) ---
RELIABLE = {
    "c3": "d00ab07387e001d00a020023020001000100ddd18bcc",
    "c4": "d00ab07387e001d00a02002303000200010096ad6215",
    "c10": "d00a91b987e001d10a02002302000300030028b81ecf",
    "c12": "d00a91b987e001d10a020023030004000500b2f4cfcf",
    "s15": "91b9d00a8760d10a020023050004000300c9d7b467",
}


def test_short_control_parses_all():
    for name, h in SHORT.items():
        c = try_parse_short_control(bytes.fromhex(h))
        assert c is not None, name
        assert c.word1 == c.word2
        assert classify(bytes.fromhex(h)) == "short_control"


def test_short_control_endpoints():
    assert try_parse_short_control(bytes.fromhex(SHORT["disc"])).dst_endpoint == EP_DISCOVERY
    assert try_parse_short_control(bytes.fromhex(SHORT["world"])).dst_endpoint == EP_WORLD


def test_reliable_control_seq_ack():
    c3 = parse_reliable_control(bytes.fromhex(RELIABLE["c3"]))
    assert (c3.seq, c3.ack) == (2, 1) and c3.to_server
    c4 = parse_reliable_control(bytes.fromhex(RELIABLE["c4"]))
    assert (c4.seq, c4.ack) == (3, 2)
    s15 = parse_reliable_control(bytes.fromhex(RELIABLE["s15"]))
    assert (s15.seq, s15.ack) == (5, 4) and not s15.to_server


def test_reliable_ack_addressing_is_the_fix():
    """The ack must go FROM the discovery endpoint back to the client, not 0xFFFE."""
    client = parse_reliable_control(bytes.fromhex(RELIABLE["c3"]))
    reply = build_reliable_ack(client, server_seq=1, checksum_fn=lambda b: 0)
    r = parse_reliable_control(reply)
    assert r.src_endpoint == EP_DISCOVERY
    assert r.dst_endpoint == client.src_endpoint
    assert r.ack == client.seq
    assert not r.to_server


def test_classify_distinguishes_shapes():
    assert classify(bytes.fromhex(SHORT["disc"])) == "short_control"
    assert classify(bytes.fromhex(RELIABLE["c3"])) == "reliable_control"
    # a longer data bundle (world-list #1) is neither control shape
    worldlist = "b073d00ada2163d00a0200010001000200fb060100000012000000fcc3b307"
    assert classify(bytes.fromhex(worldlist)) == "data_bundle"
