# EQOA ReturnHome

A private **EverQuest Online Adventures (EQOA)** server plus the reverse-engineered
**phase3 network protocol** it speaks — in one repo you can clone and run.

> For lawful preservation / interoperability research on software you own. No Sony,
> SOE, Daybreak, or EQOA assets or client code are included — only an independent
> server and a description of the wire protocol. Keep any deployment non-commercial.

## Layout

```
eqoa_server/      the server (auth TCP, MOTD TCP, game UDP, patch HTTP)
eqoa_protocol/    reverse-engineered phase3 protocol library (pcap-derived, tested)
docs/             PROTOCOL.md (wire spec), disc_analysis.md, research notes
tools/            decode_pcap.py, analyze_pcap.py, pnach helpers
tests/            server + protocol test suites
captures/         from_sandstorm.pcapng — the reference capture the protocol is built from
```

## Quick start

```powershell
git clone https://github.com/Guinnessstache/eqoa-returnhome.git
cd eqoa-returnhome
python -m pip install -e ".[dev]"        # installs scapy + pytest too
python -m eqoa_server --host 0.0.0.0 --public-host 127.0.0.1
```

Ports: Auth TCP `9735`, MOTD TCP `7000`, Game UDP `10070`, Patch HTTP `8080`,
Admin panel `8090`.

## Admin panel

Open <http://localhost:8090/> while the server runs. On first start the console
prints a generated admin password (stored hashed in `data/admin.json`; delete it
to get a new one), or pass `--admin-password` / set `EQOA_ADMIN_PASSWORD`.

- **Dashboard** - start / stop / restart each service (auth, patch/MOTD, game UDP,
  status HTTP) independently, request counts, uptime, CPU and memory
  (`pip install psutil` or `pip install -e ".[admin]"`), data backup download.
- **Players** - live game sessions with stage, idle time and reliable-message
  progress; kick a session.
- **Accounts** - accounts and characters; reset password, delete, reset a stuck
  character's position.
- **Settings** - shard name, message of the day (400-byte limit shown), password
  enforcement. Saved to `data/settings.json`.
- **Logs** - live tail of the server log with a filter.

The panel listens on `127.0.0.1` only. Use `--admin-host 0.0.0.0` to reach it from
other machines on your LAN; never expose it to the internet.

Passwords: the client sends an encoded token, not the password. Each account
remembers the token from its first login (or first login after a reset); with
enforcement on, other tokens are refused. Enforcement is off by default until it
is confirmed the client sends the same token for the same password every time.
Run `python -m eqoa_server --help` for options. Point PCSX2 (DNAS-patched ISO) at
the host; see `docs/research-notes.md`.

## What's wired in (pcap-verified)

The game UDP handler uses `eqoa_protocol` for the two control-packet shapes that
were breaking world entry — both confirmed against `captures/from_sandstorm.pcapng`
and reproduced byte-for-byte with the server's own `rdp_crc`:

- **19-byte `0x80` bare-control** — the post-world-list endpoint-ack. It carries no
  message bundle and is addressed to the discovery/world endpoint, so the old
  parser crashed ("needs 20 bytes, have 19"). Now absorbed, no reply (matches the
  capture).
- **22-byte `0x87` reliable-control** — the server-list-hover ack. It targets the
  discovery endpoint `0x73B0`; the server now replies **from** that endpoint
  instead of `0xFFFE`, which was the server-select / list timeout.

See `docs/PROTOCOL.md` for the full phase3 spec (endpoints, framing, opcode
families, entity records).

## External data (operator-local, not in git)

Full world entry replays captured phase3 bundles from
`external/EQOA_Server_Portable_137/base137/portable/phase3_data/`. That captured
data is **not redistributable**, so it is git-ignored and must stay on your machine.
Without it the server still boots and handles auth, world-list, and control packets.
For world entry it falls back to the MemoryDump template reassembled from
`captures/from_sandstorm.pcapng` (see `eqoa_server/capture_template.py`).

## Test

```powershell
python -m pytest -q
```

Covers auth, patch TCP, the phase3 builders, and the control-packet decoders
(keyed to the reference capture).

## Analyze a capture

```powershell
python tools\decode_pcap.py captures\from_sandstorm.pcapng     # classified phase3 view
python tools\analyze_pcap.py <your.pcapng> --out analysis\summary.json
```

## Public sources

Sandstorm (https://eqoa.live/), EQOAGameServer
(https://github.com/devin103/EQOAGameServer), ESF notes
(https://github.com/joukop/ESF-file-format/), EQOAEmu (https://eqoaemu.com/).

MIT licensed (your code). EverQuest / EQOA are trademarks of their respective owners.
