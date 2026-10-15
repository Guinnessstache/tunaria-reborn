from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time

from . import game_data, items


TOKEN_HASH_PREFIX = "sha256:"


def _hash_token(username_key: str, token: str) -> str:
    """The client already sends a hashed password; never keep it as is."""
    digest = hashlib.sha256(f"tunaria-reborn:{username_key}:{token}".encode("utf-8")).hexdigest()
    return TOKEN_HASH_PREFIX + digest


@dataclass(slots=True)
class Account:
    account_id: int
    username: str
    # Salted hash of the client's password token ("" = not set: the next login sets it).
    password_token: str
    created_at: float = field(default_factory=time)
    last_login_at: float | None = None


@dataclass(slots=True)
class Character:
    character_id: int
    account_id: int
    name: str
    race: int = 0
    klass: int = 10
    gender: int = 0
    level: int = 1
    experience: int = 0
    hp: int = 100
    max_hp: int = 100
    mana: int = 50
    max_mana: int = 50
    zone_id: int = 1
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    heading: float = 0.0
    appearance: str = ""
    # Chosen at character creation.
    hair_color: int = 0
    hair_length: int = 0
    hair_style: int = 0
    face: int = 0
    human_type: int = 0
    training_points: list[int] = field(default_factory=lambda: [0] * 7)
    tunar: int = 0
    # [{"item": pattern id, "stack": n, "equip": equip slot or -1}] and [{"spell": id, "hotbar": slot or -1}];
    # None until the character has been given its starting kit.
    inventory: list[dict] | None = None
    spells: list[dict] | None = None
    # Version of the starting kit given (items.STARTING_KIT_VERSION); older kits are replaced once.
    kit_version: int = 0

    @property
    def model_id(self) -> int:
        return game_data.model_id(self.race, self.gender)


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.characters: dict[int, Character] = {}
        self._next_account_id = 1
        self._next_character_id = 1
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.accounts = {
                row["username"].lower(): Account(**{k: v for k, v in row.items() if k in Account.__dataclass_fields__})
                for row in payload.get("accounts", [])
            }
            for account in self.accounts.values():
                if not account.password_token.startswith(TOKEN_HASH_PREFIX):
                    # Stored by an older version as the client's raw token, which earlier
                    # clients (other memory cards) sent differently: let the next login set it.
                    account.password_token = ""
            self.characters = {
                int(row["character_id"]): Character(**{k: v for k, v in row.items() if k in Character.__dataclass_fields__})
                for row in payload.get("characters", [])
            }
            for character in self.characters.values():
                _give_starting_kit(character)
            self._next_account_id = payload.get("next_account_id", 1)
            self._next_character_id = payload.get("next_character_id", 1)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "next_account_id": self._next_account_id,
                "next_character_id": self._next_character_id,
                "accounts": [asdict(account) for account in self.accounts.values()],
                "characters": [asdict(character) for character in self.characters.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def create_account(self, username: str, password_token: str) -> Account | None:
        key = username.strip().lower()
        if not key or key in self.accounts:
            return None
        with self._lock:
            account = Account(self._next_account_id, username.strip(), _hash_token(key, password_token))
            self._next_account_id += 1
            self.accounts[key] = account
            self.save()
            return account

    def check_login(self, username: str, password_token: str) -> tuple[Account | None, str]:
        """Log in, creating the account on first use.

        Returns (account, outcome): outcome is "created", "set" (no password stored
        yet, this one is now the account's password), "ok" or "wrong" (account None).
        """
        key = username.strip().lower()
        with self._lock:
            account = self.accounts.get(key)
            if account is None:
                return self.create_account(username, password_token), "created"
            given = _hash_token(key, password_token)
            if not account.password_token:
                account.password_token = given
                outcome = "set"
            elif secrets.compare_digest(account.password_token, given):
                outcome = "ok"
            else:
                return None, "wrong"
            account.last_login_at = time()
            self.save()
            return account, outcome

    def change_password(self, username: str, old_token: str, new_token: str) -> bool:
        key = username.strip().lower()
        with self._lock:
            account = self.accounts.get(key)
            if account is None:
                return False
            if account.password_token and not secrets.compare_digest(account.password_token, _hash_token(key, old_token)):
                return False
            account.password_token = _hash_token(key, new_token)
            self.save()
            return True

    def reset_password(self, account_id: int) -> bool:
        """Forget an account's password; the next login sets a new one."""
        with self._lock:
            account = self.get_account(account_id)
            if account is None:
                return False
            account.password_token = ""
            self.save()
            return True

    def get_account(self, account_id: int) -> Account | None:
        with self._lock:
            return next((a for a in self.accounts.values() if a.account_id == account_id), None)

    def delete_account(self, account_id: int) -> bool:
        with self._lock:
            account = self.get_account(account_id)
            if account is None:
                return False
            del self.accounts[account.username.lower()]
            for character_id in [c.character_id for c in self.characters.values() if c.account_id == account_id]:
                del self.characters[character_id]
            self.save()
            return True

    def delete_character(self, character_id: int) -> bool:
        with self._lock:
            if self.characters.pop(character_id, None) is None:
                return False
            self.save()
            return True

    def reset_character_position(self, character_id: int) -> bool:
        with self._lock:
            character = self.characters.get(character_id)
            if character is None:
                return False
            start = game_data.starting_character(character.klass, character.race, character.human_type)
            if start is not None:
                character.zone_id, character.x, character.y, character.z = start.world, start.x, start.y, start.z
                character.heading = start.facing
            else:
                character.zone_id, character.x, character.y, character.z = _starting_position(character.race)
                character.heading = 0.0
            self.save()
            return True

    def latest_account(self) -> Account | None:
        with self._lock:
            if not self.accounts:
                return None
            return max(self.accounts.values(), key=lambda account: account.last_login_at or account.created_at)

    def list_characters(self, account_id: int) -> list[Character]:
        with self._lock:
            return [
                character
                for character in sorted(self.characters.values(), key=lambda row: row.character_id)
                if character.account_id == account_id
            ]

    def get_character(self, character_id: int) -> Character | None:
        with self._lock:
            return self.characters.get(character_id)

    def give_item(self, character_id: int, item_id: int, stack: int = 1, equip: bool = False) -> dict:
        with self._lock:
            character = self._character_or_error(character_id)
            if item_id not in items.item_patterns():
                raise InventoryError(f"no item with id {item_id}")
            stack = max(1, min(int(stack), items.describe_item(item_id)["max_stack"]))
            entry = {"item": item_id, "stack": stack, "equip": items.NOT_EQUIPPED,
                     "key": items.next_item_key(character.inventory)}
            character.inventory.append(entry)
            if equip:
                self._equip(character, len(character.inventory) - 1)
            self.save()
            return entry

    def set_item_equipped(self, character_id: int, index: int, equipped: bool) -> None:
        with self._lock:
            character = self._character_or_error(character_id)
            self._entry_or_error(character, index)
            if equipped:
                self._equip(character, index)
            else:
                character.inventory[index]["equip"] = items.NOT_EQUIPPED
            self.save()

    def set_level(self, character_id: int, level: int, learn_abilities: bool = True) -> list[int]:
        """Set a character's level (1-60); optionally learn the class's abilities up to it.
        Returns the newly learned ability ids."""
        if not MIN_LEVEL <= level <= MAX_LEVEL:
            raise InventoryError(f"level must be {MIN_LEVEL} to {MAX_LEVEL}")
        with self._lock:
            character = self._character_or_error(character_id)
            character.level = level
            character.max_hp, character.max_mana = level_vitals(character.klass, level)
            character.hp, character.mana = character.max_hp, character.max_mana
            learned = []
            if learn_abilities:
                known = {entry["spell"] for entry in character.spells}
                learned = [spell_id for spell_id in items.abilities_up_to(character.klass, level) if spell_id not in known]
                character.spells += [{"spell": spell_id, "hotbar": -1} for spell_id in learned]
            self.save()
            return learned

    def learn_spell(self, character_id: int, spell_id: int) -> bool:
        """Add a spell to the character's book (not on the hotbar). False if already known."""
        with self._lock:
            character = self._character_or_error(character_id)
            character.spells = character.spells or []
            if any(entry["spell"] == spell_id for entry in character.spells):
                return False
            character.spells.append({"spell": spell_id, "hotbar": -1})
            self.save()
            return True

    def set_tunar(self, character_id: int, tunar: int) -> None:
        if not 0 <= tunar <= 2_000_000_000:
            raise InventoryError("tunar must be 0 to 2000000000")
        with self._lock:
            self._character_or_error(character_id).tunar = tunar
            self.save()

    def remove_item(self, character_id: int, index: int) -> None:
        with self._lock:
            character = self._character_or_error(character_id)
            self._entry_or_error(character, index)
            del character.inventory[index]
            self.save()

    def _character_or_error(self, character_id: int) -> Character:
        character = self.characters.get(character_id)
        if character is None:
            raise InventoryError(f"no character {character_id}")
        return character

    @staticmethod
    def _entry_or_error(character: Character, index: int) -> dict:
        if not 0 <= index < len(character.inventory):
            raise InventoryError(f"no item at position {index}")
        return character.inventory[index]

    @staticmethod
    def _equip(character: Character, index: int) -> None:
        entry = character.inventory[index]
        slot = items.equip_slot_for(entry["item"])
        if slot == items.NOT_EQUIPPED:
            raise InventoryError(f"{items.describe_item(entry['item'])['name']} can't be equipped")
        # One item per slot; a two-handed weapon also takes both hands and the shield's place.
        clashes = {slot}
        if slot == items.EQUIP_TWO_HAND:
            clashes |= {items.EQUIP_PRIMARY, items.EQUIP_SECONDARY, items.EQUIP_SHIELD}
        elif slot in (items.EQUIP_PRIMARY, items.EQUIP_SECONDARY, items.EQUIP_SHIELD):
            clashes.add(items.EQUIP_TWO_HAND)
        for other in character.inventory:
            if other is not entry and other.get("equip") in clashes:
                other["equip"] = items.NOT_EQUIPPED
        entry["equip"] = slot

    def create_character(
        self,
        account_id: int,
        name: str,
        *,
        race: int = 0,
        klass: int = 10,
        gender: int = 0,
        appearance: bytes = b"",
        hair_color: int = 0,
        hair_length: int = 0,
        hair_style: int = 0,
        face: int = 0,
        human_type: int = 0,
        training_points: list[int] | None = None,
    ) -> Character:
        with self._lock:
            for character in self.characters.values():
                if character.account_id == account_id and character.name.lower() == name.lower():
                    return character
            hp, mana = _starting_vitals(race, klass)
            zone_id, x, y, z = _starting_position(race)
            heading, tunar = 0.0, 0
            start = game_data.starting_character(klass, race, human_type)
            if start is not None:
                zone_id, x, y, z, heading, tunar = start.world, start.x, start.y, start.z, start.facing, start.tunar
            character = Character(
                character_id=self._next_character_id,
                account_id=account_id,
                name=name,
                race=race,
                klass=klass,
                gender=gender,
                hp=hp,
                max_hp=hp,
                mana=mana,
                max_mana=mana,
                zone_id=zone_id,
                x=x,
                y=y,
                z=z,
                heading=heading,
                appearance=appearance.hex(),
                hair_color=hair_color,
                hair_length=hair_length,
                hair_style=hair_style,
                face=face,
                human_type=human_type,
                training_points=list(training_points or [0] * 7),
                tunar=tunar,
            )
            _give_starting_kit(character)
            self._next_character_id += 1
            self.characters[character.character_id] = character
            self.save()
            return character


class InventoryError(ValueError):
    pass


MIN_LEVEL, MAX_LEVEL = 1, 60


def _give_starting_kit(character: Character) -> None:
    """Starting food and abilities for characters that have none yet (also older saves)."""
    # Nothing can be bought or looted yet, so replacing an older kit loses nothing.
    if character.inventory is None or character.kit_version < items.STARTING_KIT_VERSION:
        character.inventory = items.starting_inventory(character.race)
        character.spells = items.starting_spells(character.klass)
        character.kit_version = items.STARTING_KIT_VERSION


def _starting_vitals(race: int, klass: int) -> tuple[int, int]:
    if klass in {7, 8, 9, 10, 11, 12, 13, 14}:
        return (100, 100)
    return (100, 50)


TANK_CLASSES = {0, 2, 3}  # Warrior, Paladin, Shadowknight
CASTER_CLASSES = {7, 8, 9, 10, 11, 12, 13, 14}


def level_vitals(klass: int, level: int) -> tuple[int, int]:
    """(max HP, max power) for a class at a level: 100 + 6 per level and a little more each
    level (more for tanks; a level 4 warrior had 124 HP on Sandstorm); power at least three
    times the dearest ability the class has by then (Sandstorm: 159 at level 4, Kick 23)."""
    level = max(MIN_LEVEL, min(level, MAX_LEVEL))
    growth = 0.5 if klass in TANK_CLASSES else 0.25 if klass in CASTER_CLASSES else 0.35
    hp = round(100 + level * (6 + growth * level))
    return hp, max(100 + 15 * level, 3 * _dearest_ability(klass, level))


def _dearest_ability(klass: int, level: int) -> int:
    if not 0 <= klass < len(items.CLASS_NAMES):
        return 0
    return max((a["power"] for a in items.class_abilities().get(items.CLASS_NAMES[klass], ()) if a["level"] <= level),
               default=0)


def bind_point(character: Character) -> tuple[int, float, float, float, float]:
    """(world, x, y, z, facing) Return Home sends the character to: where its class and
    race start (the Sandstorm capture returned a character to where it entered the world)."""
    start = game_data.starting_character(character.klass, character.race, character.human_type)
    if start is not None:
        return start.world, start.x, start.y, start.z, start.facing
    _zone, x, y, z = _starting_position(character.race)
    return 0, x, y, z, 0.0


def _starting_position(race: int) -> tuple[int, float, float, float]:
    starts = {
        0: (1, 25273.03125, 54.125, 15723.29102),  # Human -> Freeport
        1: (3, 0.0, 0.0, 0.0),  # Barbarian -> Halas
        2: (1, 0.0, 0.0, 0.0),
        3: (2, 0.0, 0.0, 0.0),
        4: (2, 0.0, 0.0, 0.0),
        5: (8, 0.0, 0.0, 0.0),
        6: (6, 0.0, 0.0, 0.0),
        7: (1, 0.0, 0.0, 0.0),
        8: (4, 0.0, 0.0, 0.0),
        9: (1, 0.0, 0.0, 0.0),
        10: (8, 0.0, 0.0, 0.0),
        11: (7, 0.0, 0.0, 0.0),
        12: (1, 0.0, 0.0, 0.0),
    }
    return starts.get(race, starts[0])
