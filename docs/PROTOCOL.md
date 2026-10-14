# EQOA phase3 protocol notes

Reverse-engineered from packet captures of EverQuest Online Adventures: Frontiers
(PS2). Transport is **UDP on port 10070**, alongside auth (TCP 9735) and MOTD
(TCP 7000). All multi-byte fields are **little-endian**. Every datagram ends with
a 4-byte **EQOA custom checksum** (not CRC32).

Reference capture: `tests/data/from_sandstorm.pcapng`
(client `192.168.1.57` ↔ server `5.161.217.188`, 101 s, 136 UDP datagrams).

## Endpoints

Datagrams are addressed between logical *endpoints* (LE u16 at offsets 0 and 2):

| id | name | phase |
|----|------|-------|
| `0xFFFE` | none / broadcast | initial client hello |
| `0x73B0` | discovery | auth / server-list / select |
| `0xB991` | world | in-world game session |
| `0x0AD0`+ | client session | the client's own id; low half of the 32-bit session id, **increments by phase** (`0x0AD0` discovery → `0x0AD1` world) |

## Common front-matter

```
0  2  src_endpoint
2  2  dst_endpoint
4  1  flag        0x80 bare control | 0x87 reliable control | other = data bundle
5  1  dir         high bit set (0xE0) = to-server; clear (0x60) = to-client
...                body depends on flag
-4 4  checksum    EQOA custom, over all preceding bytes
```

The **direction bit** (bit `0x80` of byte 5) is a reliable tell of who sent a
frame, independent of IP.

## Bare control — flag `0x80` (19 bytes)

Endpoint-ack / phase-transition. Carries **no message bundle** — parsing it as a
bundle walks off the end (the "needs 20 bytes, have 19" bug).

```
0  2  src_endpoint
2  2  dst_endpoint
4  1  0x80
5  1  0xE0
6  1  subtype = 0x14
7  4  word1   (session/instance, e.g. 0x00020AD0)
11 4  word2   (== word1)
15 4  checksum
```

Example: `d00a b073 80 e0 14  d00a0200 d00a0200  e2ca4c98`

**Server behavior:** accept, refresh session liveness, send nothing. After the
client sends this to `0x73B0` it re-binds to the world endpoint on its own; after
it sends one to `0xB991` the server simply keeps streaming world data.

## Reliable control — flag `0x87` (22 B client / 21 B server)

A sliding-window seq/ack. Client frames carry an extra `0x01` bundle-count byte
after the dir byte; server frames omit it (hence 22 vs 21 bytes).

```
0  2  src_endpoint
2  2  dst_endpoint
4  1  0x87
5  1  dir (0xE0 to-server / 0x60 to-client)
6  1  0x01            [client frames only]
7  4  session_id
11 1  op = 0x23
12 2  seq             sender's own message number
14 2  ack             highest seq received from peer
16 2  field           window / last-reliable id
-4 4  checksum
```

Each side keeps its own `seq`; `ack` = highest peer `seq` seen.

```
C->S disc   seq=2 ack=1      (acking the world-list)
C->S disc   seq=3 ack=2
S->C world  seq=5 ack=4      (server's reliable ack)
```

**Server-select timeout fix:** during list-hover the client sends these to the
**discovery** endpoint `0x73B0`. The server must reply **FROM `0x73B0` → client**
with a `0x87` ack (`dir=0x60`, `ack = client.seq`). Replying to `0xFFFE` (or not
at all) starves the client's reliable layer and the list times out in seconds.

## Session establishment sequence (from the capture)

| # | dir | src→dst | flag | len | meaning |
|---|-----|---------|------|-----|---------|
| 0 | C→S | 0x0AD0→0xFFFE | data | 90 | initial hello / discovery |
| 1 | S→C | 0x73B0→0x0AD0 | data | 228 | **world/server list** (shard "Local EQOA") |
| 2 | S→C | 0x73B0→0x0AD0 | data | 27 | list follow-up |
| 3,4 | C→S | 0x0AD0→0x73B0 | 0x87 | 22 | reliable acks of the list |
| 5 | C→S | 0x0AD0→0x73B0 | 0x80 | 19 | phase-close (discovery done) |
| 7 | S→C | 0xB991→0x0AD0 | data | 41 | world endpoint hello (fields `ffffffff` = no char yet) |
| 10,12 | C→S | 0x0AD0→0xB991 | 0x87 | 22 | reliable acks vs world |
| 15 | S→C | 0xB991→0x0AD0 | 0x87 | 21 | server reliable ack |
| 16–31 | S→C | 0x0AD0→0x0AD0 | data | 1185 | bulk world/zone stream (`faff 8404…`, UI/zone text) |
| 32 | C→S | 0x0AD0→0xB991 | 0x80 | 19 | world phase-close/ack |
| 52+ | both | 0x0AD0↔0x0AD0 | data | var | in-world: object updates, movement, `0x13`-op reliable msgs |

## In-world data bundles (partial)

Once the world session is established the datagrams gain a second 4-byte
**session tag** and carry gameplay messages. Header (confirmed over 84 in-world
datagrams in the capture):

```
0  2  src_endpoint        (0x0AD0 both ways in-world)
2  2  dst_endpoint        (0x0AD0)
4  2  ctl                 per-packet control word (0xXXc0 S->C data; low byte varies)
6  1  (varies)
7  4  session_a           0x00020AD1 (world session id)
11 4  session_tag         0xC69C95D0  <- constant for the whole world session
15 1  0x01                bundle marker (always 0x01 in-world)
16 2  msg_seq             reliable message number; S->C runs 0x2001,0x2002,0x2003…
18 1  channel = 0x00
19 1  opcode
...   message payload
-4 4  checksum
```

The `session_tag` (`0xC69C95D0`) is the in-world correlation id — track it per
session. `msg_seq` is the server's reliable counter; the client acks it with the
`0x87` reliable-control (see above) and the `0x80` bare-control.

### Opcode families observed (S->C)

| op | count | role |
|----|-------|------|
| `0x17` | 30 | **entity/object update** — carries `a2` records (below) |
| `0xFA` | 24 | **reliable fragmented bulk** (`FA FF 8404…`) — zone/asset stream; fragments carry `data\tunaria\…` asset paths |
| `0xFB` | 9 | control/status messages (also used in the list phase) |
| `0x0C,0x07,0x08,0x0A,0x10,0x14,0x15` | — | large entity/zone batch updates (also `a2`-record bearing) |
| `0xF9` | 3 | reliable-message container (wraps `fb12…` sub-messages) |
| `0x18` | 1 | short ack/status |

### Entity record — `0x17` → `a2`

```
a2                      record marker
<index> (2)             entity/object index (increments: a205, a206, a207…)
then typed fields, each introduced by a 0x8N tag byte:
   0x81 <v>             1-byte field
   0x82 <v v>           2-byte field
   0x83 <v v>           field
   0x84 <v v v v>       4-byte field (e.g. 84 06 3598ed01)
   0x88 / 0x80|id       packed position / state
```

Example (`#65`, 52 B): `… 202a 00 17  a2 0500  8108 40  8205 e604  81 18 1e
8304 0a00  7c  8406 3598ed01  8068 00  25e8  <crc>`. The `0x8N` tags are a
packed/delta encoding (the same family as the server's `_rle_zero_encode`): type
in the low nibble, value follows. Fields look like position + heading + state,
but **exact coordinate decoding is not yet pinned down from a single capture.**

### Open (needs more data)

- Exact `a2` field semantics (which tag = X/Y/Z, heading, hp, model id).
- The non-`0x2000` seq variants (`0x1Fxx`, `0x33xx`) seen on the big batch ops.
- C->S movement payload (the periodic 30-byte `… 63 <sess> 0100 0700 0a00`).

The leverage to finish this cleanly is the **decrypted client** (a PCSX2 EE-RAM
dump, or a WSL+Ghidra decompile) — the `a2` field layout is defined there; see
`docs/disc_analysis.md`.

## Known checksum

The 4-byte trailer is an EQOA-specific checksum (not zlib/bzip2 CRC32). The
server implementation already computes it for outbound packets; `build_*`
helpers take it as an injected `checksum_fn`.
