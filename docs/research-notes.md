# Research Notes

## Public Projects

Sandstorm is the only currently public playable EQOA server according to its project page. Its public server repository is useful as documentation but says most implementation code is not included. Sandstorm's history page mentions the older EQOA Revival scripts, `EQOAemu_2015`, OpenEQOA, and the C# EQOAGameServer release.

EQOAGameServer is the most useful public codebase for protocol constants. The pieces mirrored into this scaffold are:

- Auth TCP message IDs and response packet shapes.
- UDP packet header flags.
- UDP bundle flags.
- UDP message type constants.
- Known game opcode IDs.
- Little-endian UDP primitive handling and 7-bit encoded integers.

The code in this project is a fresh Python implementation, not a vendored copy of the C# server. The cloned repository under `public-research/` is for local study only.

## Reference Material

- **Prima Official Game Guide — EverQuest Online Adventures: Frontiers**
  <https://www.projectreturnhome.com/DownloadFiles/EverQuest-Online-Adventures-Frontiers-Prima.pdf>
  (hosted by Project ReturnHome). Reference for populating game data: races,
  classes and starting cities, spells and abilities by class, zones and maps,
  NPCs, quests, items and crafting. Use it to look facts up; don't copy its text
  or maps into the repo.

## Current Protocol Understanding

Auth TCP:

- Requests start with big-endian `uint32 length` and `uint32 messageType`.
- Login request `0x24` contains two unknown `uint32`s, a 32-byte username, a 32-byte encrypted/tokenized password, another unknown `uint32`, and game-id style trailing data.
- Create account request `0x38` contains a message header, a 32-byte username, and a 32-byte password token.
- The public auth prototype treats the password as already encrypted/encoded by the client and compares/stores that token.

Game UDP:

- First two fields are little-endian `ushort clientEndpoint` and `ushort targetEndpoint`.
- Header data is a 7-bit encoded integer. Lower 11 bits are bundle size; remaining bits are packet header flags.
- Optional fields appear based on header and bundle flags.
- Reliable messages may include a little-endian `ushort` message number followed by a compact size and payload.
- Payload opcodes are little-endian `ushort`s.

## Next Implementation Milestones

1. Implement the game server list opcode `0x07B3` and character list opcode `0x002C`.
2. Implement session state, reliable resend queues, and object update channels.
3. Add character create/delete opcode handlers backed by JSON persistence.
4. Add a minimal empty-world entry path with player speed/time/memory-dump messages.
5. Import community-safe NPC/quest metadata only when its license permits redistribution.
