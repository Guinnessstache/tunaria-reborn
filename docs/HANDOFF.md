# Handoff: where Tunaria Reborn stands

Give this file to a new assistant session so it can pick up the work. It sums up
the project, the setup, what has been confirmed in game, the protocol facts we
have verified, and what is still open. Last updated after merchants were confirmed in game
(`e54b177`).

## The project

Tunaria Reborn is a private server for *EverQuest Online Adventures: Frontiers*
(PS2), written in pure Python 3.10+ with no required dependencies. The repo is
`Guinnessstache/tunaria-reborn` on GitHub. The old name, `eqoa-returnhome`,
redirects to it.

- The working branch is `claude/serve-eqahosts`. Each change is pushed both to
  that branch and to `main`:

  ```
  git push origin claude/serve-eqahosts
  git push origin HEAD:main
  ```

- Run the tests with `python3 -m unittest discover -s tests`. All 154 pass, with
  1 skipped.
- Commit messages end with these two lines (no model names anywhere in the repo):

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DTxAZVte174AM8Euxi2Vj8
  ```

  A new session should use its own attribution lines instead.
- Don't rewrite the user's own commits (`21aa84e`, `ee8f377`).
- Don't commit the user's disc files (ITEM.ESF, CHAR.ESF, ISO). Commit only the
  ids and numbers derived from them.

## How the user tests

- **Server:** Ubuntu at 192.168.1.65, installed as a systemd service with
  `deploy/install.sh`.
  - The admin panel is at http://192.168.1.65:8090. The user updates the server
    from the panel: **Updates** tab, then **Update & restart**. That pulls
    `main` fast-forward, runs the tests, and rolls back if they fail.
  - The user accepts the risk of exposing the admin panel.
- **Client:** PCSX2 on a Windows PC at 192.168.1.51. It uses a DNAS pnach
  (`tools/make_local_pnach.py`) and internal DNS pointing at the server.
- **Feedback:** the user sends screenshots and log lines from the panel's
  **Logs** tab.

**Ports:**

| Port | Use |
| --- | --- |
| TCP 9735 | Auth |
| TCP 7000 | Patch and MOTD |
| UDP 10070 | Game |
| TCP 8080 | Status page |
| TCP 8090 | Admin panel |

## Modules (`eqoa_server/`)

| Module | What it does |
| --- | --- |
| `auth.py` | Login over TCP 9735. A refusal is sent as a 0x29 text message. Logs fingerprints of the token. |
| `patch_tcp.py`, `patch_http.py` | Patch check, hosts file, MOTD. |
| `protocol.py` | Bundle and message encoding, discovery and server list, client update (0x40) decode (`rle_zero_decode`, `decode_client_update`). |
| `game_udp.py` | The game session state machine, from discovery through character select into the world. Reliable send with segmentation (`send_reliable`), keepalives, entity channels (NPCs), position tracking, item interaction, merchants. |
| `memory_dump.py` | Builds the in-world MemoryDump and the object update (`build_object_update`) for the player and for previews. |
| `capture_template.py` | Parses the Sandstorm capture. Splices our inventory and spells into the captured MemoryDump template. `sandstorm_entity_updates()`. |
| `items.py` | Item and spell patterns and records. Starting kit (`STARTING_KIT_VERSION = 2`). Equip slots, armor and models (`model_for`, overrides in `data/item_models.json`), search and describe. |
| `npcs.py` | NPC placements (`reference_data/npcs.json.gz`). Nearest-23 grid lookup and `build_npc_update` from the capture's channel-8 template. |
| `merchants.py` | Merchant stock fitted from the guide's descriptions, and the price formula. |
| `storage.py` | `JsonStore`: accounts (salted sha256 game passwords) and characters. Inventory, spells, level, tunar, position. |
| `admin.py`, `admin_ui.html` | Admin panel: services, players, accounts, give item, equip, level, tunar, reset password, model browser, settings, logs, updates. |
| `config.py`, `main.py`, `services.py`, `updater.py`, `netconfig.py` | Plumbing. |

**Reference data** (`eqoa_server/reference_data/`):

| File | Contents |
| --- | --- |
| `items.json.gz`, `spells.json`, `npcs.json.gz` | From ReturnHome's `EQOA_Master.sql` |
| `class_abilities.json`, `merchants.json` | From the Prima guide |
| `wiki_items.json.gz` | wiki.eqoa.live facts |
| `sandstorm_items.json` | Items seen in the capture |
| `item_models.json` | Model ids from ITEM.ESF |
| `spell_effects.json` | The 238 spell effect ids in DATA2/SPELLFX.CSF, with particle counts and colours |

Each one is built by a script in `tools/` (`import_*.py`).

**Other docs:**
- `docs/PROTOCOL.md`
- `docs/research-notes.md`
- `docs/linux-server.md`
- `docs/disc_analysis.md`

**External sources used:**
- ReturnHome EQOAGameServer (AGPL), github.com/devin103/EQOAGameServer
- The Sandstorm capture, `captures/from_sandstorm.pcapng`
- The Prima guide PDF (via `pdftotext -layout`)
- wiki.eqoa.live through its MediaWiki API
- joukop/eqoa-server (Java) and joukop/ESF-file-format

eqoa.live has no public database. Only `/scp/status` (players online) is open to
the public.

## Verified protocol facts

### State channel messages

Channels 0x00–0x17 carry object state:

```
[channel][size][seq u16 LE][xor-ref byte][RLE data]
```

The RLE coding works like this:
- A control byte of 0x80 or more means `c & 0x7F` literal bytes, then a byte
  giving the count of zeros.
- Otherwise the high nibble is the literal count and the low nibble is the zero
  count.
- Zeros come before the literals. A 0x00 byte ends the data.

Hide an entity by resending its update with byte 0 set to 0.

### Object update (162 bytes)

| Offset | Field |
| --- | --- |
| 0 | Active flag |
| 1 | Object id |
| 5 | 0x82, then x/y/z as 3-byte big-endian ×128 |
| 15 | Facing |
| 26 | HP shown |
| 27 | HP |
| 28 | Model |
| 32 | Model 2 |
| 36 | Scale (float) |
| 67, 71, 75 | Weapons (int32 each) |
| 93–98 | Armor parts: chest, bracers, hands, legs, feet, head |
| 99–104 | Armor colors |
| 105–108 | Hair and face |
| 109 | Robe |
| 58 | Animation: 0/1 standing, 3 running, 4 creature attack, 11 Kick, 14 lying dead |
| 116 | Name (24 bytes) |
| 140 | Level |
| 144 | Race |
| 145 | Class |
| 146 | NPC type (u16): 0x81 merchant, 0x82 banker, 0x80 talkable |

Armor values, checked in game:

| Value | Look |
| --- | --- |
| 0 | None |
| 1 | Cloth |
| 2 | Leather |
| 3 | Chain |
| 4 | Plate |
| 5, 6 | Heavier plate looks |

### Client update (0x40)

The payload is RLE-compressed, then XORed against an earlier update. Decoded:

- x and z are 24-bit big-endian values, scaled as `36000·v/0xFFFFFF − 4000`.
- y is scaled as `2000·v/0xFFFFFF − 1000`.
- The facing byte is at offset 22: heading = `b·2π/256 + π`.

### MemoryDump order

core → hotkeys → 2 int32 → quests → inventory → bank → 4 bytes → spells → 191-byte stat tail

### Item record

Fields, in order:

1. zz stack, zz durability, zz charges, zz equip slot
2. byte 1, int32 key
3. zz id, zz cost (the item's value; every cost in the capture is even)
4. zz fields: unk1, icon, unk2, slot, unk3, trade, rent, unk4, atk, dmg, unk5,
   lvl, maxstack, maxhp, dur, class mask, race mask, proc, lore, unk6, craft
5. UTF-16 name and description, then the stats list

The class mask bit order is:
WAR RAN PAL SK MNK BRD RGE DRD SHA CL MAG NEC ENC WIZ ALC

### Spell record

Fields, in order:

1. 9 zz fields: id, order, onhotbar, where, 0, −1, abilitylvl, 0, 0
2. u16 range:

   | Value | Range |
   | --- | --- |
   | 0x4090 | Self |
   | 0x40A0 | Melee |
   | 0x41F0 | Target |

3. 7 zz fields: cast, power, icon_color, icon, scope, recast, −1
4. Name, description, then a 0 byte

### Item keys and client indexes

Each inventory item has two numbers, as in EQOAGameServer:

- **Key:** the int32 in its item record. The client names items by key in
  InteractItem (0x3E) and MerchantSell (0x4B). Keys don't change while in the
  world; we restart them at 0 on each world entry and give new items the next
  free key.
- **Client index:** its position in the client's list. Our replies
  (RemoveInvItem 0x3C, EquipItem 0x3F) name items by index, and indexes shift
  down when an earlier item goes.

Treating the two as one broke using or selling items after any item was used up.

### Equip slots

| Slot | Value |
| --- | --- |
| Head | 0 |
| Hands | 1 |
| Chest | 5 |
| Bracers | 8 |
| Rings | 9 / 10 |
| Legs | 12 |
| Feet | 13 |
| Primary | 14 |
| Secondary | 15 |
| Two-hand | 16 |
| Shield | 17 |
| Thrown | 18 |
| Held | 19 |
| Robe | 21 |

### Opcodes

**Inventory:**

| Opcode | Name | Payload |
| --- | --- | --- |
| 0x3E | InteractItem | u32 index, u32 target, zz action |
| 0x3F | Equip | |
| 0x40 | Unequip | byte slot |
| 0x3B | AddInvItem | item record |
| 0x3C | RemoveInvItem | byte index, byte 1, zz qty |
| 0x52 | PlayerTunar | zz |

**Merchants:**

| Opcode | Name | Payload |
| --- | --- | --- |
| 0x4C | MerchantDiag | u32 npc |
| 0xB7 | MerchantBox | u32 npc, zz sell %, zz buy %, zz count, i32 count, records (segmented). The client shows cost × % / 100, rounded. We send 25 and 100. |
| 0x4A | MerchantBuy | zz slot, zz qty, u32 npc |
| 0x4B | MerchantSell | i32 index, zz qty, u32 npc |

### Abilities (Sandstorm capture of a level 4 warrior, 2026-10-15)

The user captured Sandstorm while a level 4 warrior learned Kick from a scroll,
put it on the hotbar, used it on two mobs, then cast Return Home. Sandstorm
also says "abilities are no longer automatically learned. You need a scroll
to learn them."

| Opcode | Direction | Name | Payload |
| --- | --- | --- | --- |
| 0x10 | C>S | SelectTarget | u32 0, u32 target (sent all the time; the logs' `Unknown_0x0010`) |
| 0x0F | C>S | Attack | u32 target (auto attack) |
| 0x32 | C>S | UseAbility | u32 0, byte **hotbar slot**, u32 target |
| 0x33 | C>S | AssignHotbar | u32 0, byte spell book order, byte hotbar slot |
| 0xB4 | S>C | HotbarAssigned | the 0x33 payload echoed |
| 0xB3 | S>C | AddSpell | i32 0, i32 1, one spell record (sent after a scroll is used) |
| 0xB2 | S>C | SetPosition | byte world, float x, y, z, facing, 12 zero bytes (world entry; end of Return Home) |
| 0xCD | S>C | AbilityCooldown | byte spell book order, i32 0, zz ms: greys the ability out for cast + recast + 300 ms (Kick 6300, Return Home 65300); sent again with 0 when ready |
| 0xB1 | S>C | CastEffect | u32 caster, u32 target, byte 0, i32 cast ms, u32 effect id (Return Home 0x8FB28233 = blue glow, Kick 0x0A286E2E; mobs use it too) |
| 0x15 / 0x16 | C>S | ? | u32 target / empty (consider and close?) |
| 0x7E1 | C>S | ? | periodic client debug text ("0: discarded (old)") |

- Within 30 ms of every UseAbility Sandstorm sent the power spent (channel
  0x42), AbilityCooldown and CastEffect. Without them the ability doesn't grey
  out and Return Home's fists don't glow. SetPosition ended Return Home 61.5 s
  later, at the point where the character had entered the world. (An earlier
  read missed these: server packets always carry the session id after the
  header, whatever the header flags say.)
- Kick killed mobs. The server's replies were 0xDB (u32 attacker, byte, u32
  target; combat animations or hits), stat channel 0x42 updates, 0x20 (3
  bytes), 0xD9 (u32 0) and a ClientMessage "You receive 138 XP". Not decoded.
- **Stat channel 0x42** (unreliable, like the entity channels): 58 int32 and
  a closing 0xFF byte, 233 bytes. Fields 0-6 stats (STR STA AGI DEX WIS INT
  CHA), 7/8 HP and max HP, 9/10 power and max power, 29-35 base stats, 37 base
  max HP, 39 base max power; several fields hold their own index. Sent on world
  entry and on every HP or power change (about every 6 s while regenerating).
  Channel 0x43 got 4 zero bytes on world entry. **Without 0x42 the client has
  no power and won't cast** (Return Home didn't start; the fists glow blue
  while it casts). `player_stats.py` builds both; they're resent until acked.
- **Spell effects** are in `DATA2/SPELLFX.CSF` (compressed ESF: zlib blocks):
  238 SpellEffect objects (0xC200, id in the 0xC210 header) made of 332
  ParticleDefinitions (0xC000). Nothing says which spell uses which; the
  original server held that. `spell_effects.py` uses the two known ones and
  guesses the rest by colour (each particle has an RGBA ramp from byte 0x6C;
  Return Home's particles come out blue) against the ability's element and
  kind (name words, then the guide's effect type). All ranks of a spell share
  an effect. Not a hash of the spell name (8 hash functions tried).
- **Combat** (same capture): see the notes at the top of `combat.py`. A
  creature's HP is 0-255 at byte 27 of its object update; byte 26 is 1 alive,
  3 dead. 0xDB is a hit (attacker, damage, target). 0x15 on a corpse is
  answered 0x16 + 0x18 (`00 00000000`); the client's 0x16 closes it.
- In the spell record, the hotbar slot is the 4th field ("where"); the 3rd is
  1 when it's on the hotbar.

## Confirmed working in game

- Login, server list and character select. The session stays alive while idle
  on any screen.
- Character creation, entering the world, and walking around.
- Logging back in at the saved position (saved every 15 s and on disconnect).
- Starting kit: rations and the class's level 1 abilities plus Return Home.
- Admin panel:
  - Give items. Weapons, shields and armor show on the character, and equip and
    unequip work in game.
  - Setting a character's level.
  - The model browser changes the look of an item live.
- Merchants: the shop window opens, buying and selling move tunar, and the
  prices shown match what the server charges and pays.
- NPCs, merchants and mobs appear around the player. The "$" has been removed
  from mob names; the user has not confirmed the fix yet.

## Open issues and ideas, roughly by priority

1. **Password refusal on re-login.** With password checking on, logging in
   again was refused even with the right password. The token the client sends
   may not be stable.
   - The auth log prints the token and stored fingerprints, so compare those
     across two logins.
   - The user has turned checking off for now (Settings).
2. **Spell scrolls are untested in game.** Spells were sold as scroll items
   ("This allows you to scribe X.", icon -607649328). The item table only has
   294, so every other class ability gets a built scroll (ids 200000 + class ·
   1000 + guide position). Spell merchants sell one per spell in their range.
   Using a scroll (InteractItem) learns the spell, removes the scroll and sends
   a ClientMessage (0x0A7A, int32 length + UTF-16). The opcode that adds a spell
   to the open spell book is AddSpell (0xB3), so the spell appears at once. Scribing works in game (the chat line shows in white).
   Merchants don't sell level 1 scrolls (every character starts with those),
   scribing needs the scroll's level, and the panel's **Set level** only
   teaches abilities when its checkbox is ticked (off by default). Prices are our own formula:
   - Consumables cost 5 + 5·level.
   - Everything else, scrolls included, costs 100 + 120·level.
   - Merchants buy back at a quarter of that.
   - Only 519 of the 1051 merchants have a guide description. The rest sell food
     and drink.
3. **NPCs are static.** They don't move, talk (no dialogue), or fight. There is
   no banker window yet; the banker NPC type is 0x82. Scale and race bytes come
   from a template.
4. **Abilities on yourself work; abilities on others need combat.** Every
   ability spends power, greys out and plays an effect. `ability_effects.py`
   applies the guide's numbers to the caster: heals, stat and max HP / power
   buffs for their duration, heal and power over time (a tick every 6 s, our
   guess). That covers about 400 of 1,413 abilities. Buffs aren't saved over a
   relog and show no icon (channel 0x43 isn't decoded). AC changes aren't
   shown (no AC field found). HP and power regenerate a twentieth every 6 s.
   Only the records for Warrior, Shadowknight and Return Home are real data. The others are built from the guide.
5. **No other players are visible.** The game needs to share entity channels
   between sessions.
6. **Combat works in a first form (untested in game)**, `combat.py`. Creatures
   are the NPCs EQOAGameServer marks with "$" (4,815). Attack (0x0F) starts
   auto attack (3 s swings); damage abilities and DoTs hit the target. The
   creature turns, chases (8 units/s), swings every 3 s (CastEffect + 0xDB),
   gives up past 120 units. On death: corpse (byte 26 = 3), XP (0x20, 0xD9,
   chat line), gone after 7 s, back after 90 s. A dead player goes to the bind
   point with full HP. HP, damage, XP and speeds are our formulas, fitted to
   the one capture. Loot (0x15) answers "empty". No level-ups yet (XP only),
   no quests.
7. **Pets (untested in game)**, `pets.py`. The 78 "Summon Pet" abilities make a
   pet that looks like a creature from the NPC table named like the spell
   (fire elementalkin, skeleton, companion spirit, clay golem...), named after
   the spell. It's an ordinary object on an entity channel: follows its owner,
   fights the owner's target or whatever is after them (0xDB with the pet as
   attacker; kills give the owner XP). Creatures don't fight back at it.
   Recasting replaces it; it goes when the owner dies or logs out. How the real
   game showed pets (a pet window, ownership) isn't in any capture.
8. Armor colors always default to 0. Weapon models are exact only where known.
   Elsewhere the item gets a model of the same shape, or one set through the
   model browser.

## Useful local paths in the old session (gone now; ask the user if needed)

- The Prima guide text came from the PDF the user uploaded. The user also has
  ITEM.ESF and CHAR.ESF from their disc and can upload them again.
