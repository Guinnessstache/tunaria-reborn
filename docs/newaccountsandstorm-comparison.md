# Sandstorm Account/Create/Login Capture Comparison

Capture analyzed: `newaccountsandstorm.pcapng` (operator-local, not in the repo)

Sensitive request fields were not exported. The analyzer only records packet direction, lengths, flags, endpoint ids, message numbers, and opcode names.

## Flow Summary

| Stage | Direction | Transport | Port | Payloads | Scaffold status |
| --- | --- | --- | --- | --- | --- |
| Account create | client -> server | TCP | 9735 | `CreateAccount` length 476 | Request parser matches public prototype shape. |
| Account create response | server -> client | TCP | 9735 | `CreateAccountResponse` length 276 | Scaffold response length/opcode matches. |
| Login | client -> server | TCP | 9735 | `LoginRequest` length 148 | Request parser matches public prototype shape. |
| Login response | server -> client | TCP | 9735 | `LoginResponse` length 168 | Scaffold response length/opcode matches. |
| Patch/update probe 1 | both | TCP | 7000 | client 64, server 707 | Implemented as local MOTD HTTP responder. |
| Patch/update probe 2 | both | TCP | 7000 | client 64, server 707 | Implemented as local MOTD HTTP responder. |
| Server list session | both | UDP | 10070 | `DiscVersion`, `Authenticate`, `GameServers` | Parser now decodes; scaffold does not yet emit `GameServers`. |
| Character-select session | both | UDP | 10070 | `Authenticate2`, `Camera1`, `CharacterSelect` | Parser now decodes; scaffold does not yet emit character list. |
| Character create/select | both | UDP | 10070 | `CreateCharacter`, `CharacterSelect`, `SelectedCharacter` | Parser now decodes; persistence exists but opcode handlers are not wired. |
| World entry | both | UDP | 10070 | `MemoryDump`, `DialogueBox`, `Time`, `ColoredChat`, client update channel `0x40` | Parser partially decodes; world-entry writers are not implemented. |

## Packet Timeline

| Packet | Direction | Length | Messages |
| --- | --- | ---: | --- |
| 1 | client -> auth | 476 | `CreateAccount` |
| 3 | auth -> client | 276 | `CreateAccountResponse` |
| 5 | client -> auth | 148 | `LoginRequest` |
| 7 | auth -> client | 168 | `LoginResponse` |
| 12 | client -> patch | 64 | binary patch/update request |
| 14 | patch -> client | 707 | binary patch/update response |
| 21 | client -> patch | 64 | binary patch/update request |
| 23 | patch -> client | 707 | binary patch/update response |
| 30 | client -> game | 91 | `DiscVersion`, `Authenticate` |
| 31 | game -> client | 228 | `DiscVersion`, `GameServers` |
| 36 | client -> game | 91 | `DiscVersion`, `Authenticate2` |
| 38 | game -> client | 48 | `DiscVersion`, `Camera1`, `CharacterSelect` |
| 47 | client -> game | 74 | `CreateCharacter` |
| 49 | game -> client | 84 | `CharacterSelect` |
| 51 | client -> game | 44 | `SelectedCharacter` |
| 52 | game -> client | 73 | `Camera1`, `Time`, unknown `0x0020`, unknown `0x00D9` |
| 54-62 | game -> client | 681-1235 | segmented `MemoryDump` and follow-up world-entry data |
| 69 | client -> game | 97 | unknown `0x0014`, `ChangeChatMode`, unknown `0x00C9`, unknown `0x00CA`, channel `0x40` |
| 79-80, 89 | game -> client | 78-333 | `ColoredChat` |
| 83, 86 | game -> client | 30 | `IgnoreList` |
| 84 | client -> game | 536 | unknown `0x07E1` diagnostics/state payload |

## Prototype Alignment

Confirmed from the public C# prototype:

- Auth message IDs and response sizes match the capture.
- UDP message header order is `type`, `size`, optional `messageNumber`, optional `opcode`.
- Reliable `0xFB`, unreliable `0xFC`, segmented reliable `0xFA`, ping `0xF9`, and client update channel `0x40` are visible in the capture.
- `0x07B3` is the game-server list.
- `0x002C` is character select/list.
- `0x002B` is create character.
- `0x002A` is selected character.
- `0x000D` is segmented memory dump.

New or still unclear from this Sandstorm capture:

- Header flag `0x00004000` appears to carry a session id only in same-endpoint world traffic.
- TCP `7000` binary patch/update exchange is required before UDP game sessions.
- Unknown opcodes seen during world entry: `0x0020`, `0x00D9`, `0x00C9`, `0x00CA`, `0x07E1`.
- Two very short UDP control packets remain undecoded; they are likely session transition/cancel packets.

## Implementation Implications

The scaffold should next implement, in order:

1. UDP session state with correct bundle/message ack progression.
2. `GameServers` writer for the first UDP session.
3. `DiscVersion`, `Camera1`, and empty `CharacterSelect` writers for the second UDP session.
4. `CreateCharacter` and `SelectedCharacter` handlers backed by JSON persistence.
5. Minimal segmented `MemoryDump` writer, then `Time`, `DialogueBox`, `ColoredChat`, and `IgnoreList`.
