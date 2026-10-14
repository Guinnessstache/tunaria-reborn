# Project Tunaria Reborn

A community **EverQuest Online Adventures (EQOA): Frontiers** server plus the reverse-engineered
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
git clone https://github.com/Guinnessstache/tunaria-reborn.git
cd tunaria-reborn
python -m pip install -e ".[dev]"        # installs scapy + pytest too
python -m eqoa_server --host 0.0.0.0 --public-host 127.0.0.1
```

To let the admin panel restart and update the server, start it with the
launcher instead (same options):

```powershell
.\start_server.bat --host 0.0.0.0 --public-host 127.0.0.1    # Windows PowerShell
./start_server.sh --host 0.0.0.0 --public-host 127.0.0.1      # Linux / macOS
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
- **Accounts** - accounts and characters; delete, reset a stuck character's
  position.
- **Settings** - shard name and message of the day (400-byte limit shown). Saved
  to `data/settings.json`.
- **Logs** - live tail of the server log with a filter.

The panel listens on `127.0.0.1` only. Use `--admin-host 0.0.0.0` to reach it from
other machines on your LAN; never expose it to the internet.

Passwords are not checked yet: the client sends a token that appears to be
derived from the server's per-connection challenge, so it differs on every login.
Each login logs one-way fingerprints of the challenge and token to confirm this.
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

## Acknowledgements

Tunaria Reborn is an independent project. It builds on the work of others in the
EQOA preservation community:

- **Project ReturnHome** and its C# server, **EQOAGameServer**
  (https://github.com/devin103/EQOAGameServer, AGPL-3.0). Auth message IDs,
  packet header/bundle flags, message types, opcode IDs and parts of the
  protocol handling were learned from or modelled on it.
- **Sandstorm** (https://eqoa.live/), whose live server traffic in
  `captures/from_sandstorm.pcapng` is the reference the protocol work is checked
  against.
- ESF file format notes (https://github.com/joukop/ESF-file-format/) and EQOAEmu
  (https://eqoaemu.com/).

## License

Copyright (c) 2026 GuinnessStache and contributors.

Licensed under the **GNU Affero General Public License v3.0** (`AGPL-3.0-only`,
see `LICENSE`), the same license as EQOAGameServer, which parts of this project
are modelled on. In short: you may use, change and share it, but changed
versions you distribute **or run as a server that other people connect to** must
make their source available under the same license. If you run a modified
server for other players, give them a way to get the source, for example a link
in the message of the day.

EverQuest and EverQuest Online Adventures are trademarks of their respective
owners. This project is not affiliated with or endorsed by them.
