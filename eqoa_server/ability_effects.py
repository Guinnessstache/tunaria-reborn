"""What an ability does to the character who uses it: heals, stat buffs, heal and power over
time. The numbers are the Prima guide's (reference_data/class_abilities.json): each ability
has effects with a type, a scope (Self, Target, Group, Pet), an attribute, a change and a
duration in seconds.

Only effects that land on the caster are applied: scope Self or Group, and Target when the
caster has nobody else targeted. Damage, debuffs and effects on others need combat and NPC
state, which don't exist yet.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import items, player_stats
from .storage import Character

STAT_ATTRIBUTES = ("Strength", "Stamina", "Agility", "Dexterity", "Wisdom", "Intelligence", "Charisma")
MAX_ATTRIBUTES = ("HP Max", "Power Max")
TICK = 6.0  # seconds between heal/power-over-time ticks (Sandstorm's stat updates came every 6 s)
MODIFIERS = {"Str": 0, "Sta": 1, "Agi": 2, "Dex": 3, "Wis": 4, "Int": 5, "Cha": 6}


@dataclass
class Buff:
    spell: str
    attribute: str
    change: int
    expires: float
    next_tick: float = 0.0


def guide_row(character: Character, spell_name: str) -> dict | None:
    """The guide's row for one of the character's abilities, by name."""
    if not 0 <= character.klass < len(items.CLASS_NAMES):
        return None
    lowered = spell_name.lower()
    return next((row for row in items.class_abilities().get(items.CLASS_NAMES[character.klass], ())
                 if row["name"].lower() == lowered), None)


def bonus(buffs: list[Buff], attribute: str) -> int:
    return sum(buff.change for buff in buffs if buff.attribute == attribute)


def max_hp(character: Character, buffs: list[Buff]) -> int:
    return max(1, character.max_hp + bonus(buffs, "HP Max"))


def max_power(character: Character, buffs: list[Buff]) -> int:
    return max(0, character.max_mana + bonus(buffs, "Power Max"))


def stat_block(character: Character, buffs: list[Buff]) -> bytes:
    stats = stat_values(character, buffs)
    return player_stats.stat_block(character, stats=stats, max_hp=max_hp(character, buffs),
                                   max_power=max_power(character, buffs))


def _lands_on_caster(effect: dict, targeting_other: bool) -> bool:
    scope = effect.get("scope")
    return scope in ("Self", "Group") or (scope == "Target" and not targeting_other)


def handled(effect: dict) -> bool:
    """Whether the server does anything with this effect (on the caster in ``apply``, on a
    creature in GameUDPServer.ability_on_creature, or as a pet). For the self-test's list."""
    kind, attribute = effect.get("type"), effect.get("attribute")
    change, duration = int(effect.get("change", 0)), int(effect.get("duration", 0))
    if effect.get("scope") == "Pet" and kind != "Summon Pet":
        return False  # nothing is done to pets yet
    if kind == "Summon Pet" or kind == "Modify Hate" or kind == "Backstab":
        return True
    if kind == "Dmg/Heal" and attribute == "HP":
        return change > 0 or effect.get("scope") == "Target"
    if kind == "Statistic Change":
        if attribute == "DoT":
            return change < 0 and duration > 0 and effect.get("scope") == "Target"
        if attribute in STAT_ATTRIBUTES + MAX_ATTRIBUTES:
            return duration > 0 and change > 0 and effect.get("scope") != "Pet"  # debuffs: not yet
        if attribute in ("HoT", "PoT"):
            return duration > 0 and change > 0 and effect.get("scope") != "Pet"
        if attribute == "Power":
            return duration == 0 and effect.get("scope") != "Pet"
    return False


def stat_values(character: Character, buffs: list[Buff]) -> list[int]:
    return [value + bonus(buffs, name) for value, name in zip(player_stats.base_stats(character), STAT_ATTRIBUTES)]


def amount(character: Character, effect: dict) -> int:
    """The guide's change plus its stat modifier share (e.g. 40% of Str), same sign."""
    change = int(effect.get("change", 0))
    percent = int(effect.get("percent", 0))
    if not percent or not change:
        return change
    stat = player_stats.base_stats(character)[MODIFIERS.get(effect.get("modifier", ""), 0)]
    extra = stat * percent // 100
    return change + (extra if change > 0 else -extra)


def apply(character: Character, row: dict, buffs: list[Buff], now: float, targeting_other: bool) -> list[str]:
    """Apply the ability's effects that land on the caster. Changes ``character`` and
    ``buffs``; returns what happened, for the chat window."""
    said = []
    for effect in row.get("effects", ()):
        if not _lands_on_caster(effect, targeting_other):
            continue
        kind, attribute = effect.get("type"), effect.get("attribute")
        value, duration = amount(character, effect), int(effect.get("duration", 0))
        if kind == "Dmg/Heal" and attribute == "HP" and value > 0:
            before = character.hp
            character.hp = min(max_hp(character, buffs), character.hp + value)
            said.append(f"You are healed for {character.hp - before}.")
        elif kind == "Statistic Change" and attribute in STAT_ATTRIBUTES + MAX_ATTRIBUTES and duration > 0 and value > 0:
            buffs[:] = [b for b in buffs if not (b.spell == row["name"] and b.attribute == attribute)]
            buffs.append(Buff(row["name"], attribute, value, now + duration))
            said.append(f"{row['name']}: {value:+d} {attribute} for {_duration(duration)}.")
        elif kind == "Statistic Change" and attribute in ("HoT", "PoT") and duration > 0 and value > 0:
            buffs[:] = [b for b in buffs if not (b.spell == row["name"] and b.attribute == attribute)]
            buffs.append(Buff(row["name"], attribute, value, now + duration, next_tick=now + TICK))
            what = "HP" if attribute == "HoT" else "power"
            said.append(f"{row['name']}: {value} {what} every {TICK:.0f} s for {_duration(duration)}.")
        elif kind == "Statistic Change" and attribute == "Power" and duration == 0:
            character.mana = max(0, min(max_power(character, buffs), character.mana + value))
    character.hp = min(character.hp, max_hp(character, buffs))
    character.mana = min(character.mana, max_power(character, buffs))
    return said


def tick(character: Character, buffs: list[Buff], now: float) -> tuple[bool, list[str]]:
    """Run heal/power-over-time ticks and drop expired buffs. Returns (changed, messages)."""
    changed, said = False, []
    for buff in buffs:
        while buff.attribute in ("HoT", "PoT") and buff.next_tick and buff.next_tick <= min(now, buff.expires):
            buff.next_tick += TICK
            if buff.attribute == "HoT":
                character.hp = min(max_hp(character, buffs), character.hp + buff.change)
            else:
                character.mana = min(max_power(character, buffs), character.mana + buff.change)
            changed = True
    expired = [buff for buff in buffs if buff.expires <= now]
    if expired:
        buffs[:] = [buff for buff in buffs if buff.expires > now]
        character.hp = min(character.hp, max_hp(character, buffs))
        character.mana = min(character.mana, max_power(character, buffs))
        for name in sorted({buff.spell for buff in expired}):
            said.append(f"{name} has worn off.")
        changed = True
    return changed, said


def _duration(seconds: int) -> str:
    if seconds >= 60:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds} s"
