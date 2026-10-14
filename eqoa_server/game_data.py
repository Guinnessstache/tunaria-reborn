"""Static game data: races, classes, character models and starting characters.

The model IDs and starting characters (zone, position, facing, money, training
points and base stats for every valid race / class / home city) come from
Project ReturnHome's EQOAGameServer database (EQOA_Master.sql,
https://github.com/devin103/EQOAGameServer, AGPL-3.0).
"""
from __future__ import annotations

from dataclasses import dataclass

RACES = ("Human", "Elf", "Dark Elf", "Gnome", "Dwarf", "Troll", "Barbarian", "Halfling", "Erudite", "Ogre")
CLASSES = (
    "Warrior", "Ranger", "Paladin", "Shadowknight", "Monk", "Bard", "Rogue", "Druid",
    "Shaman", "Cleric", "Magician", "Necromancer", "Enchanter", "Wizard", "Alchemist",
)
GENDERS = ("Male", "Female")
# Humans choose a home city at creation; other races always use OTHER.
HUMAN_TYPES = ("Other", "Freeport", "Qeynos")
STATS = ("strength", "stamina", "agility", "dexterity", "wisdom", "intelligence", "charisma")

# (race, gender) -> model ID used in the character list and object updates.
CHARACTER_MODELS: dict[tuple[int, int], int] = {
    (0, 0): 0x70D898C6,  # Human male
    (0, 1): 0x0D537335,  # Human female
    (1, 0): 0xD8D56FF8,  # Elf male
    (1, 1): 0xE18919F7,  # Elf female
    (2, 0): 0x7ED89DAA,  # Dark Elf male
    (2, 1): 0x8C9B4B39,  # Dark Elf female
    (3, 0): 0xA99C6B15,  # Gnome male
    (3, 1): 0x4C4084FC,  # Gnome female
    (4, 0): 0xAA5B0447,  # Dwarf male
    (4, 1): 0x350BFDBE,  # Dwarf female
    (5, 0): 0x4C6FA532,  # Troll male
    (5, 1): 0x4DE32701,  # Troll female
    (6, 0): 0x61CA3EDF,  # Barbarian male
    (6, 1): 0xC44AD516,  # Barbarian female
    (7, 0): 0xA3DB3FE2,  # Halfling male
    (7, 1): 0x8A27A731,  # Halfling female
    (8, 0): 0x320C0B47,  # Erudite male
    (8, 1): 0x4F1A3CBE,  # Erudite female
    (9, 0): 0x84807490,  # Ogre male
    (9, 1): 0xACC00B4F,  # Ogre female
}


@dataclass(frozen=True, slots=True)
class StartingCharacter:
    world: int
    x: float
    y: float
    z: float
    facing: float
    tunar: int
    unused_training_points: int
    total_training_points: int
    stats: tuple[int, int, int, int, int, int, int]  # in STATS order


# (class, race, human type) -> where and how a new character starts.
STARTING_CHARACTERS: dict[tuple[int, int, int], StartingCharacter] = {
    (0, 0, 1): StartingCharacter(0, 25486.5, 61.3651, 15547.0, -1.61104, 5000, 20, 475, (75, 80, 65, 65, 60, 55, 55)),
    (0, 0, 2): StartingCharacter(0, 4243.7, 58.1338, 17169.0, -1.55227, 5000, 20, 475, (75, 80, 65, 65, 60, 55, 55)),
    (0, 2, 0): StartingCharacter(0, 25220.1, 7.35246, 8626.18, -0.00439516, 5000, 20, 475, (70, 80, 60, 70, 60, 65, 50)),
    (0, 3, 0): StartingCharacter(0, 23478.1, 37.2401, 6209.86, 0.722441, 5000, 20, 475, (70, 75, 70, 70, 55, 65, 50)),
    (0, 4, 0): StartingCharacter(0, 15190.8, 99.9204, 8767.33, -1.53629, 5000, 20, 475, (80, 85, 60, 65, 65, 50, 50)),
    (0, 5, 0): StartingCharacter(0, 25407.7, 70.0135, 31410.3, -1.93956, 5000, 20, 475, (90, 90, 60, 60, 55, 50, 50)),
    (0, 6, 0): StartingCharacter(0, 13298.0, 65.1573, 4310.94, -0.750984, 5000, 20, 475, (65, 80, 60, 60, 75, 55, 60)),
    (0, 7, 0): StartingCharacter(0, 18776.3, 62.376, 10959.6, -3.06876, 5000, 20, 475, (70, 85, 70, 75, 55, 50, 50)),
    (0, 9, 0): StartingCharacter(1, 9377.67, 53.1885, 7385.81, -1.53102, 5000, 20, 475, (90, 90, 60, 60, 55, 50, 50)),
    (1, 0, 2): StartingCharacter(0, 9005.05, 56.8153, 12749.2, 3.50485, 5000, 20, 475, (65, 65, 75, 80, 60, 55, 55)),
    (1, 1, 0): StartingCharacter(0, 18380.2, 75.5557, 7378.39, -2.40724, 5000, 20, 475, (60, 60, 70, 85, 65, 55, 60)),
    (2, 0, 2): StartingCharacter(0, 4225.26, 58.4463, 17325.0, -0.611862, 5000, 20, 475, (75, 80, 65, 65, 60, 55, 55)),
    (2, 1, 0): StartingCharacter(0, 18985.5, 63.3994, 6356.56, -2.35888, 5000, 20, 475, (70, 85, 60, 70, 65, 55, 60)),
    (2, 4, 0): StartingCharacter(0, 15437.3, 82.9634, 8727.31, 2.32993, 5000, 20, 475, (80, 85, 60, 65, 65, 50, 50)),
    (2, 8, 0): StartingCharacter(0, 4727.37, 54.126, 21654.9, 1.11078, 5000, 20, 475, (70, 75, 60, 65, 65, 70, 50)),
    (3, 0, 1): StartingCharacter(0, 25478.1, 53.501, 15607.3, 1.27313, 5000, 20, 475, (75, 80, 65, 65, 55, 60, 55)),
    (3, 2, 0): StartingCharacter(0, 24659.8, 5.76662, 8836.48, -5.12263, 5000, 20, 475, (70, 80, 60, 70, 55, 70, 50)),
    (3, 5, 0): StartingCharacter(0, 25628.0, 75.001, 31591.0, 5.2606, 5000, 20, 475, (90, 90, 60, 60, 50, 55, 50)),
    (3, 8, 0): StartingCharacter(0, 4683.29, 57.0635, 21726.0, 0.0455922, 5000, 20, 475, (70, 75, 60, 65, 60, 75, 50)),
    (3, 9, 0): StartingCharacter(1, 9379.49, 30.3822, 7134.56, 1.79931, 5000, 20, 475, (90, 90, 60, 60, 50, 55, 50)),
    (4, 0, 2): StartingCharacter(0, 5593.73, 58.5088, 17627.0, -1.53023, 5000, 20, 475, (65, 75, 65, 80, 60, 55, 55)),
    (5, 0, 1): StartingCharacter(0, 25416.2, 65.9091, 15606.2, 3.22916, 5000, 20, 475, (60, 75, 80, 65, 55, 55, 65)),
    (5, 0, 2): StartingCharacter(0, 4555.0, 58.2126, 17108.0, 1.23694, 5000, 20, 475, (60, 75, 80, 65, 55, 55, 65)),
    (5, 1, 0): StartingCharacter(0, 18126.4, 75.5557, 7246.11, 1.24227, 5000, 20, 475, (55, 70, 75, 70, 60, 55, 70)),
    (6, 0, 1): StartingCharacter(0, 25474.4, 53.501, 15612.9, 4.31982, 5000, 20, 475, (65, 65, 75, 80, 55, 55, 60)),
    (6, 0, 2): StartingCharacter(0, 4499.56, 57.8586, 17115.0, 1.58916, 5000, 20, 475, (65, 65, 75, 80, 55, 55, 60)),
    (6, 1, 0): StartingCharacter(0, 18216.6, 75.5557, 7294.82, -2.91637, 5000, 20, 475, (60, 60, 70, 85, 60, 55, 65)),
    (6, 2, 0): StartingCharacter(0, 25316.7, 1.20725, 8766.33, -1.52219, 5000, 20, 475, (60, 65, 70, 85, 55, 65, 55)),
    (6, 3, 0): StartingCharacter(0, 23677.9, 25.3447, 6390.02, 1.93538, 5000, 20, 475, (60, 60, 80, 85, 50, 65, 55)),
    (6, 4, 0): StartingCharacter(0, 15382.3, 88.5664, 8687.14, -2.35616, 5000, 20, 475, (70, 70, 70, 80, 60, 50, 55)),
    (6, 6, 0): StartingCharacter(0, 13115.0, 54.3994, 4316.31, 2.92491, 5000, 20, 475, (75, 70, 70, 80, 50, 55, 55)),
    (6, 7, 0): StartingCharacter(0, 18918.9, 58.5947, 11002.5, 0.892183, 5000, 20, 475, (60, 70, 80, 90, 50, 50, 55)),
    (7, 0, 2): StartingCharacter(0, 9386.06, 61.1802, 12557.0, 2.33274, 5000, 20, 475, (65, 55, 55, 75, 80, 60, 65)),
    (7, 1, 0): StartingCharacter(0, 18344.5, 75.5557, 7318.33, -1.70491, 5000, 20, 475, (60, 50, 50, 80, 85, 60, 70)),
    (7, 7, 0): StartingCharacter(0, 18885.9, 58.5947, 10860.8, 2.5023, 5000, 20, 475, (60, 60, 60, 85, 75, 55, 60)),
    (8, 5, 0): StartingCharacter(0, 25266.9, 63.751, 31424.6, -1.72244, 5000, 20, 475, (70, 85, 60, 55, 5, 50, 60)),
    (8, 6, 0): StartingCharacter(0, 13298.0, 65.1573, 4310.94, -0.750984, 5000, 20, 475, (65, 80, 60, 60, 75, 55, 60)),
    (8, 9, 0): StartingCharacter(1, 8739.3, 79.126, 7345.37, -1.54821, 5000, 20, 475, (70, 85, 60, 55, 75, 50, 60)),
    (9, 0, 1): StartingCharacter(0, 25477.1, 53.501, 15601.4, 3.15526, 5000, 20, 475, (65, 65, 60, 55, 80, 55, 75)),
    (9, 0, 2): StartingCharacter(0, 4287.73, 57.8967, 17287.0, -2.75868, 5000, 20, 475, (65, 65, 60, 55, 80, 55, 75)),
    (9, 1, 0): StartingCharacter(0, 19173.3, 63.3994, 6306.74, 0.475139, 5000, 20, 475, (60, 60, 55, 60, 85, 55, 80)),
    (9, 2, 0): StartingCharacter(0, 24775.6, 7.29475, 8814.61, -1.59202, 5000, 20, 475, (60, 65, 55, 60, 80, 65, 70)),
    (9, 3, 0): StartingCharacter(0, 23587.8, 32.626, 6107.76, 2.31424, 5000, 20, 475, (60, 60, 65, 60, 75, 65, 70)),
    (9, 4, 0): StartingCharacter(0, 15445.9, 82.9672, 8743.75, 1.50538, 5000, 20, 475, (70, 70, 55, 55, 85, 50, 70)),
    (9, 7, 0): StartingCharacter(0, 18921.1, 58.5947, 11246.8, 3.17584, 5000, 20, 475, (60, 70, 65, 65, 75, 50, 70)),
    (9, 8, 0): StartingCharacter(0, 4735.44, 66.1885, 21599.5, 3.43546, 5000, 20, 475, (60, 60, 55, 55, 85, 70, 70)),
    (10, 0, 1): StartingCharacter(0, 25289.7, 63.7276, 15552.5, 5.49659, 5000, 20, 475, (55, 65, 75, 60, 65, 80, 55)),
    (10, 0, 2): StartingCharacter(0, 4224.5, 62.0449, 17085.0, 5.11527, 5000, 20, 475, (55, 65, 75, 60, 65, 80, 55)),
    (10, 1, 0): StartingCharacter(0, 18892.0, 63.3994, 6568.56, 0.0108012, 5000, 20, 475, (50, 60, 70, 65, 70, 80, 60)),
    (10, 2, 0): StartingCharacter(0, 25002.1, 7.51975, 8745.99, 0.635421, 5000, 20, 475, (50, 65, 70, 65, 65, 90, 50)),
    (10, 3, 0): StartingCharacter(0, 23778.7, 32.626, 6101.91, -3.0061, 5000, 20, 475, (50, 60, 80, 65, 60, 90, 50)),
    (10, 8, 0): StartingCharacter(0, 4440.21, 169.564, 21840.8, 3.13725, 5000, 20, 475, (50, 60, 70, 60, 70, 95, 50)),
    (11, 0, 1): StartingCharacter(0, 25466.1, 60.2276, 15710.8, 5.36427, 5000, 20, 475, (65, 75, 55, 65, 60, 80, 55)),
    (11, 2, 0): StartingCharacter(0, 24630.1, 5.76662, 8837.69, -1.8864, 5000, 20, 475, (60, 75, 50, 70, 60, 90, 50)),
    (11, 3, 0): StartingCharacter(0, 23695.5, 36.0213, 6541.96, 2.41319, 5000, 20, 475, (60, 70, 60, 70, 55, 90, 50)),
    (11, 8, 0): StartingCharacter(0, 4685.45, 57.0635, 21693.0, 1.55153, 5000, 20, 475, (60, 70, 50, 65, 65, 95, 50)),
    (11, 9, 0): StartingCharacter(1, 9378.09, 30.3822, 7123.08, -1.15251, 5000, 20, 475, (80, 85, 50, 60, 55, 75, 50)),
    (12, 0, 1): StartingCharacter(0, 25299.8, 63.7276, 15559.0, -0.610595, 5000, 20, 475, (55, 55, 65, 65, 60, 80, 75)),
    (12, 0, 2): StartingCharacter(0, 4224.5, 62.0449, 17085.0, 5.11527, 5000, 20, 475, (55, 55, 65, 65, 60, 80, 75)),
    (12, 1, 0): StartingCharacter(0, 18888.0, 63.3994, 6544.83, -3.1319, 5000, 20, 475, (50, 50, 60, 70, 65, 80, 80)),
    (12, 2, 0): StartingCharacter(0, 25006.8, 7.51975, 8756.37, -2.32213, 5000, 20, 475, (50, 55, 60, 70, 60, 90, 70)),
    (12, 3, 0): StartingCharacter(0, 23757.3, 32.626, 6123.12, -1.86151, 5000, 20, 475, (50, 50, 70, 70, 55, 90, 70)),
    (12, 8, 0): StartingCharacter(0, 4561.02, 120.376, 21707.4, 1.60734, 5000, 20, 475, (50, 50, 60, 65, 65, 95, 70)),
    (13, 0, 1): StartingCharacter(0, 25300.8, 54.27, 15558.6, 2.38991, 5000, 20, 475, (65, 60, 65, 75, 55, 80, 55)),
    (13, 0, 2): StartingCharacter(0, 4224.5, 62.0449, 17085.0, 5.11527, 5000, 20, 475, (65, 60, 65, 75, 55, 80, 55)),
    (13, 1, 0): StartingCharacter(0, 18892.5, 63.3994, 6559.51, 2.19348, 5000, 20, 475, (60, 55, 60, 80, 60, 80, 60)),
    (13, 2, 0): StartingCharacter(0, 24971.0, 7.51975, 8746.44, -0.892634, 5000, 20, 475, (60, 60, 60, 80, 55, 90, 50)),
    (13, 3, 0): StartingCharacter(0, 23808.8, 32.7401, 6305.64, -5.47559, 5000, 20, 475, (60, 55, 70, 80, 50, 90, 50)),
    (13, 8, 0): StartingCharacter(0, 4191.0, 108.813, 21517.4, 3.09498, 5000, 20, 475, (60, 55, 60, 75, 60, 95, 50)),
    (14, 0, 1): StartingCharacter(0, 25296.8, 70.9619, 15551.2, 3.49787, 5000, 20, 475, (55, 65, 60, 65, 75, 80, 55)),
    (14, 0, 2): StartingCharacter(0, 4453.6, 58.2187, 17295.0, 2.47965, 5000, 20, 475, (55, 65, 60, 65, 75, 80, 55)),
    (14, 1, 0): StartingCharacter(0, 18909.0, 53.876, 6567.84, 0.0276634, 5000, 20, 475, (50, 60, 55, 70, 80, 80, 60)),
    (14, 2, 0): StartingCharacter(0, 25195.1, 6.59631, 8845.75, -0.42117, 5000, 20, 475, (50, 65, 55, 70, 75, 90, 50)),
    (14, 3, 0): StartingCharacter(0, 23864.6, 32.7401, 6243.07, 0.361819, 5000, 20, 475, (50, 60, 65, 70, 70, 90, 50)),
    (14, 8, 0): StartingCharacter(0, 4809.2, 54.8135, 21427.0, -2.21056, 5000, 20, 475, (50, 60, 55, 65, 80, 95, 50)),
}


def model_id(race: int, gender: int) -> int:
    return CHARACTER_MODELS.get((race, gender), CHARACTER_MODELS[(0, 0)])


def starting_character(klass: int, race: int, human_type: int) -> StartingCharacter | None:
    return STARTING_CHARACTERS.get((klass, race, human_type if race == 0 else 0))


def name_of(table: tuple[str, ...], index: int) -> str:
    return table[index] if 0 <= index < len(table) else f"#{index}"
