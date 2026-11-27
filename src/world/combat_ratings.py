"""3.3.5 combat-rating, skill and avoidance math.

The rating curves live in the world database (generated from
``gtCombatRatings.dbc`` and the crit DBCs).  If a database predates those
tables, the helpers fall back to the old flat 15-points-per-percent behaviour
so the server remains playable.
"""
from __future__ import annotations

from functools import lru_cache
import sqlite3

# CombatRating enum order in gtCombatRatings.dbc.
CR_WEAPON_SKILL = 0
CR_DEFENSE_SKILL = 1
CR_DODGE = 2
CR_PARRY = 3
CR_BLOCK = 4
CR_HIT_MELEE = 5
CR_HIT_RANGED = 6
CR_HIT_SPELL = 7
CR_CRIT_MELEE = 8
CR_CRIT_RANGED = 9
CR_CRIT_SPELL = 10
CR_HIT_TAKEN_MELEE = 11
CR_HIT_TAKEN_RANGED = 12
CR_HIT_TAKEN_SPELL = 13
CR_CRIT_TAKEN_MELEE = 14
CR_CRIT_TAKEN_RANGED = 15
CR_CRIT_TAKEN_SPELL = 16
CR_HASTE_MELEE = 17
CR_HASTE_RANGED = 18
CR_HASTE_SPELL = 19
CR_WEAPON_SKILL_MAINHAND = 20
CR_WEAPON_SKILL_OFFHAND = 21
CR_WEAPON_SKILL_RANGED = 22
CR_EXPERTISE = 23
CR_ARMOR_PENETRATION = 24

MAX_COMBAT_RATING = 25
MAX_CLASSES = 12

# ``Player::UpdateDodgePercentage`` / ``UpdateParryPercentage`` constants.
CLASS_BASE_DODGE = (
    0.0000, 3.6640, 3.4943, -4.0873, 2.0957, 3.4178,
    3.6640, 2.1080, 3.6587, 2.4211, 0.0000, 5.6097,
)
AGI_TO_CRIT_TO_DODGE = (
    0.0, 0.85 / 1.15, 1.00 / 1.15, 1.11 / 1.15, 2.00 / 1.15,
    1.00 / 1.15, 0.85 / 1.15, 1.60 / 1.15, 1.00 / 1.15,
    0.97 / 1.15, 0.0, 2.00 / 1.15,
)
AVOIDANCE_DIMINISHING_COEFFICIENT = (
    0.0, 0.9560, 0.9560, 0.9880, 0.9880, 0.9830,
    0.9560, 0.9880, 0.9830, 0.9830, 0.0, 0.9720,
)
AVOIDANCE_DIMINISHING_DODGE_CAP = (
    0.0, 88.129021, 88.129021, 145.560408, 145.560408, 150.375940,
    88.129021, 145.560408, 150.375940, 150.375940, 0.0, 116.890707,
)
AVOIDANCE_DIMINISHING_PARRY_CAP = (
    0.0, 47.003525, 47.003525, 145.560408, 145.560408, 0.0,
    47.003525, 145.560408, 0.0, 0.0, 0.0, 0.0,
)
CAN_PARRY = frozenset((1, 2, 3, 4, 6, 7))

# SkillLine ids used by weapon subclasses (3.3.5).
WEAPON_SKILL_BY_SUBCLASS = {
    0: 44,      # One-Handed Axes
    1: 172,     # Two-Handed Axes
    2: 45,      # Bows
    3: 46,      # Guns
    4: 54,      # One-Handed Maces
    5: 160,     # Two-Handed Maces
    6: 229,     # Polearms
    7: 43,      # One-Handed Swords
    8: 55,      # Two-Handed Swords
    10: 136,    # Staves
    13: 473,    # Fist Weapons
    15: 173,    # Daggers
    16: 176,    # Thrown
    17: 227,    # Spears
    18: 226,    # Crossbows
    19: 228,    # Wands
    20: 356,    # Fishing Poles
}
SKILL_UNARMED = 162
SKILL_DEFENSE = 95


@lru_cache(maxsize=4096)
def _rating_cached(path: str, level: int, rating_type: int):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT rating FROM combat_ratings"
            " WHERE level = ? AND rating_type = ?",
            (int(level), int(rating_type))).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


@lru_cache(maxsize=1024)
def _scalar_cached(path: str, char_class: int, rating_type: int):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT ratio FROM combat_rating_scalars"
            " WHERE class = ? AND rating_type = ?",
            (int(char_class), int(rating_type))).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def rating_per_point(path: str, rating_type: int, level: int) -> float:
    """Rating points needed for one percent/skill point at ``level``."""
    row = _rating_cached(path, int(level), int(rating_type))
    return float(row[0]) if row is not None and float(row[0]) > 0.0 else 15.0


def class_scalar(path: str, char_class: int, rating_type: int) -> float:
    row = _scalar_cached(path, int(char_class), int(rating_type))
    return float(row[0]) if row is not None else 1.0


def rating_bonus(path: str, rating_type: int, rating: int, level: int,
                 char_class: int) -> float:
    """Convert a combat rating to percent/skill points like CMaNGOS."""
    rating = max(0, int(rating or 0))
    if not rating:
        return 0.0
    per_point = rating_per_point(path, rating_type, level)
    if per_point <= 0.0:
        return 0.0
    return float(rating) * class_scalar(
        path, char_class, rating_type) / per_point


def rating_to_skill(path: str, rating_type: int, rating: int, level: int,
                    char_class: int) -> float:
    return rating_bonus(path, rating_type, rating, level, char_class)


@lru_cache(maxsize=2048)
def _melee_crit_cached(path: str, char_class: int, level: int):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT chance FROM melee_crit_chance"
            " WHERE class = ? AND level = ?",
            (int(char_class), int(level))).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


@lru_cache(maxsize=64)
def _melee_crit_base_cached(path: str, char_class: int):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT base FROM melee_crit_base WHERE class = ?",
            (int(char_class),)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


@lru_cache(maxsize=2048)
def _spell_crit_cached(path: str, char_class: int, level: int):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT chance FROM spell_crit_chance"
            " WHERE class = ? AND level = ?",
            (int(char_class), int(level))).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


@lru_cache(maxsize=64)
def _spell_crit_base_cached(path: str, char_class: int):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT base FROM spell_crit_base WHERE class = ?",
            (int(char_class),)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def melee_crit_per_agility(path: str, char_class: int, level: int) -> float:
    row = _melee_crit_cached(path, int(char_class), int(level))
    return float(row[0]) if row is not None else 0.0


def melee_crit_base(path: str, char_class: int) -> float:
    row = _melee_crit_base_cached(path, int(char_class))
    return float(row[0]) if row is not None else 0.0


def spell_crit_per_intellect(path: str, char_class: int, level: int) -> float:
    row = _spell_crit_cached(path, int(char_class), int(level))
    return float(row[0]) if row is not None else 0.0


def spell_crit_base(path: str, char_class: int) -> float:
    row = _spell_crit_base_cached(path, int(char_class))
    return float(row[0]) if row is not None else 0.0


def melee_crit_from_agility(path: str, char_class: int, level: int,
                            agility: float) -> float:
    """``Player::GetMeleeCritFromAgility`` in percent."""
    if not (_melee_crit_cached(path, int(char_class), int(level))):
        # Pre-rating-curve fallback: old server approximation.
        return 5.0 + float(agility) * 0.005
    return (melee_crit_base(path, char_class)
            + float(agility) * melee_crit_per_agility(
                path, char_class, level)) * 100.0


def spell_crit_from_intellect(path: str, char_class: int, level: int,
                              intellect: float) -> float:
    """``Player::GetSpellCritFromIntellect`` in percent."""
    if not (_spell_crit_cached(path, int(char_class), int(level))):
        return 5.0
    return (spell_crit_base(path, char_class)
            + float(intellect) * spell_crit_per_intellect(
                path, char_class, level)) * 100.0


def dodge_from_agility(path: str, char_class: int, level: int,
                       agility: float) -> float:
    """``Player::GetDodgeFromAgility`` in percent."""
    if not 0 <= int(char_class) < MAX_CLASSES:
        return 0.0
    if not (_melee_crit_cached(path, int(char_class), int(level))):
        return float(agility) * 0.002
    return (100.0 * float(agility)
            * melee_crit_per_agility(path, char_class, level)
            * AGI_TO_CRIT_TO_DODGE[int(char_class)])


def diminish_avoidance(chance: float, diminishing: float, cap: float,
                       coefficient: float) -> float:
    """Apply the 3.3.5 avoidance diminishing-returns curve."""
    if cap < 1.0 or coefficient < 0.001 or diminishing <= 0.0:
        return chance
    nondiminishing = chance - diminishing
    return (nondiminishing
            + (diminishing * cap) / (diminishing + cap * coefficient))


def class_can_parry(char_class: int) -> bool:
    return int(char_class) in CAN_PARRY


def weapon_skill_for_subclass(subclass: int) -> int:
    return WEAPON_SKILL_BY_SUBCLASS.get(int(subclass), SKILL_UNARMED)


def clear_caches() -> None:
    for cached in (_rating_cached, _scalar_cached, _melee_crit_cached,
                   _melee_crit_base_cached, _spell_crit_cached,
                   _spell_crit_base_cached):
        cached.cache_clear()
