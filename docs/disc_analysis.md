# EQOA Frontiers — Disc Static Analysis (ReturnHome)

**Target:** `SLUS_207.44 — EverQuest Online Adventures: Frontiers (USA)` (DNAS-bypass ISO, 2.85 GB)
**Disc version:** `SYSTEM.CNF` → `BOOT2 = cdrom0:\SLUS_207.44;1`, `VER = 1.02`, `VMODE = NTSC`
**Boot ELF SHA-256:** `40909bc57f83be237a5dbbaef1ca935c98188fddfa4ffc715039e1d3307fdcd2`
**Analyzed:** 2026-10-14, static only (strings / readelf), host = Linux sandbox.

---

## TL;DR

The three plaintext executables on the disc (`SLUS_207.44`, `NTGUI2.ELF`, `UTIL.REL`)
are the **launcher / auth / network-config layer only**. EQOA's actual game client —
and with it the RDP/world-server protocol — is **not shipped as a plaintext ELF**; the
`VILauncher` decrypts/loads it at runtime from the encrypted module/data images. The
highest-value protocol target is therefore the **decrypted client captured from PCSX2
memory**, not a disc file.

---

## Disc anatomy

| Path | What it is | RE value |
|------|-----------|----------|
| `SLUS_207.44` (391 KB) | **VILauncher** — boot, DNAS2 auth, IRX/REL loader, `.ENC` unpack | Launch flow, module-load order |
| `NTGUI2.ELF` (4 MB) | Sony **network-config GUI** (BB Navigator, DHCP/DNS, `pg_connecttest.c`) + soft keyboard | Low — stock Sony netconfig UI |
| `UTIL.REL` (1.2 MB) | PS2 relocatable module (REL), loaded via `LoadUtilRel` | **Unanalyzed — worth a pass** |
| `MODULES/*.IRX` | Stock Sony IOP drivers: `INET`, `INETCTL`, `SMAP` (ethernet), `PPP(OE)`, `DEV9`, USB, memcard, sound | Transport stack reference only |
| `MODULES/DNAS255.IMG` (259 KB) | DNAS2 auth image | The thing the "DNAS bypass" defeats |
| `DATA/TUNARIA.ESF` (997 MB) | Game world (Tunaria) in `.ESF` format | World data — separate RE effort |
| `DATA/CHAR.ESF` (148 MB) | Character data (`.ESF`) | |
| `DATA/ITEM.ESF`, `ITEMICON.ESF` | Items + icons (`.ESF`) | |
| `DATA/LOGIN.XML` (5 KB) | Station Login screen layout | **Login flow — see below** |
| `DATA/PSVRPATH.DAT` (45 B) | Server patch path | **Endpoint — see below** |
| `DATA/WORDS.TXT`, `PHRASES.TXT` | Chat profanity filter / canned phrases | |

---

## `SLUS_207.44` (VILauncher) — findings

- **ELF32 LSB MIPS-III, R5900 (Emotion Engine), EABI64**, entry `0x100008`, statically
  linked, stripped. Sections: `.text 0x100000` (size 0x47834), `.data`, `.sndata`,
  `.rodata`, `.bss`.
- **Stripped, but leaks the C++ object model** via RTTI/debug strings. The launcher is a
  `VILauncher` class (VI = Verant Interactive) with methods incl.:
  `Init`, `Setup`, `Station`, `Client`, `LoadIrx`, `LoadIrx2`, `LoadRel`, `LoadUtilRel`,
  `UnloadRel(s)`, `UnpackAndVerify`, `PreparePowerOff`, plus `VIGameOptions::Username` /
  `::Password`, `VIMemCardSetProductId/Title`, `VISetGameCode`, `VILanguage`.
- **Dev source paths leaked** (build tree `C:/code-frontiers-gm/...`):
  `eznetcnf/libezcnf.c`, `eznetcnf/libezctl.c`. Bundles **zlib** (`inflate.c`,
  `deflate.c`, `crc32.c`, …) for `.ENC`/asset decompression.
- **DNAS2 auth** present: `sceDNAS2CryptInit_nohdd`, `sceDNAS2InstExtractData_nohdd`,
  `MODULES\DNAS255.IMG`, DNAS encrypt/decrypt error paths.
- **Memory-card save path:** `mc0:/BASLUS-20744EQONLINE/%s.ENC` (saves are `.ENC`).
- Boot sequence (inferred): init EE/IOP → load network IRX (`eznetcnf`/`eznetctl`) →
  DNAS auth → `UnpackAndVerify` + `LoadRel`/`LoadUtilRel` → `Station()` / `Client()`
  launch the decrypted game.

## Login flow — `LOGIN.XML` + `SubmitStationLogin`

- Station Login screen fields: **`StationName`** and **`StationPassword`**.
- Shipping build default creds (placeholder): **`ps2test` / `test`**.
- Submit button → action **`SubmitStationLogin`** (the C++ handler to locate in the
  decrypted client).
- Other actions: `Create Station Name` → `data/ui/id_create.xml`, `ForgotPassword`,
  `Disconnect`. Auth model is Sony **"Station"** account (StationName + password).

## Server endpoint — `PSVRPATH.DAT`

```
patch/eqoa/eqoa-frontierslive-update.xml.gz
```
Relative patch path the client fetches at boot (gzip'd XML update manifest). A private
server must serve this path; the hostname is resolved elsewhere (login/redirect config,
likely in the decrypted client or a station-redirect lookup).

---

## Why REA can't help here (on Windows)

Tested against the live REA MCP server on `jayspc`. Every deep provider **and** rizin
refused the MIPS target on Windows:

- **Ghidra** (Windows P0): accepts native PE and raw 6502 only — no MIPS ELF.
- **Hopper**: not supported on win32; adapter also doesn't admit MIPS.
- **IDA**: not configured, and REA's IDA adapter has **no verified MIPS lane**.
- **rizin**: `ERR_REA_WINDOWS_NATIVE`.
- `inspect_binary_layout`: Linux-x64 + pwntools only.

**Conclusion:** REA/rea-pilot cannot analyze PS2 MIPS binaries on Windows. To use them
for EQOA, run REA under **WSL + Ghidra** (Linux x64, JDK 21, Ghidra 12.1.4 — the only
engine here with a real MIPS lane). For quick static passes, the direct sandbox route
(readelf/strings/capstone) used for this report is faster and sufficient.

---

## Recommended next steps (in priority order)

1. **Capture the decrypted client from PCSX2** — run the DNAS-bypass disc in PCSX2, dump
   EE RAM once in-game; that image contains the real client with the RDP protocol code,
   opcode tables, and `SubmitStationLogin`. This is the single highest-value artifact.
   (Your `eqoa.ps2` / `mem.ps2` / `EQOA_bootstrap.ps2` 8.25 MB dumps on the Desktop may
   already be partial captures — worth checking their headers.)
2. **Analyze `UTIL.REL`** — the one unexamined loaded module; parse the PS2 REL header +
   relocations and disassemble (capstone MIPS) to see if game/util logic lives here.
3. **Decode `.ESF`** (`CHAR`/`ITEM`/`TUNARIA`) — separate format-RE effort for world/char
   data; start with `ITEM.ESF` (4 MB) as the smallest structured sample.
4. **Stand up WSL + Ghidra REA** only if you want full MIPS *pseudocode* through
   rea-pilot; otherwise the sandbox route covers disassembly + strings + structure.
