"""Talent data imported from ``Talent.dbc``/``TalentTab.dbc``."""
from __future__ import annotations

from functools import lru_cache
import sqlite3
import struct
from typing import Dict, List, Optional

MAX_TALENT_RANK = 5
MAX_GLYPH_SLOT_INDEX = 6
CLASS_DEATH_KNIGHT = 6


def class_mask(char_class: int) -> int:
    return 1 << max(0, min(31, int(char_class) - 1))


@lru_cache(maxsize=1024)
def _tab_cached(path: str, tab_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM talent_tabs WHERE id = ?", (int(tab_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row


def get_talent_tab(path: str, tab_id: int):
    return _tab_cached(path, int(tab_id))


@lru_cache(maxsize=2048)
def _talent_cached(path: str, talent_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM talents WHERE id = ?", (int(talent_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row


def get_talent(path: str, talent_id: int):
    return _talent_cached(path, int(talent_id))


@lru_cache(maxsize=128)
def _tab_talents_cached(path: str, tab_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM talents WHERE tab_id = ? ORDER BY tier_id,"
            " column_index, id", (int(tab_id),)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return tuple(rows)


def talents_in_tab(path: str, tab_id: int) -> List[dict]:
    return [dict(row) for row in _tab_talents_cached(path, int(tab_id))]


@lru_cache(maxsize=64)
def _class_tabs_cached(path: str, mask: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM talent_tabs WHERE (class_mask & ?) <> 0"
            " ORDER BY order_index, id", (int(mask),)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return tuple(rows)


def talent_tabs_for_class(path: str, char_class: int) -> List[dict]:
    return [dict(row) for row in _class_tabs_cached(
        path, class_mask(int(char_class)))]


@lru_cache(maxsize=128)
def _family_cached(path: str, family_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM creature_family WHERE id = ?", (int(family_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row


def get_creature_family(path: str, family_id: int):
    return _family_cached(path, int(family_id))


def pet_talent_type(path: str, family_id: int) -> int:
    """CreatureFamily.dbc ``PetTalentType`` or ``-1`` for non-hunter pets."""
    row = _family_cached(path, int(family_id))
    return int(row["pet_talent_type"]) if row is not None else -1


@lru_cache(maxsize=16)
def _pet_tabs_cached(path: str, mask: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM talent_tabs WHERE (pet_talent_mask & ?) <> 0"
            " ORDER BY order_index, id", (int(mask),)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return tuple(rows)


def pet_talent_tabs(path: str, family_id: int) -> List[dict]:
    talent_type = pet_talent_type(path, family_id)
    if talent_type < 0:
        return []
    return [dict(row) for row in _pet_tabs_cached(
        path, 1 << min(30, talent_type))]


def pet_talent_points_for_level(level: int, bonus: int = 0) -> int:
    """``Pet::GetMaxTalentPointsForLevel``: one point per four levels after 16."""
    base = ((int(level) - 16) // 4) if int(level) >= 20 else 0
    return max(0, base) + max(0, int(bonus or 0))


def talent_points_for_level(level: int, bonus: int = 0,
                            char_class: int = 0) -> int:
    """``Player::CalculateTalentsPoints`` for the 3.3.5a level ranges.

    Death knights start earning points after level 55; every other class
    starts at level 10.  Quest ``RewardBonusTalents`` are added on top.
    """
    base_level = 55 if int(char_class or 0) == CLASS_DEATH_KNIGHT else 9
    base = max(0, int(level) - base_level)
    return base + max(0, int(bonus or 0))


def talent_rank_spells(talent) -> List[int]:
    return [int(talent[f"spell_rank{i}"] or 0) for i in range(1, 6)]


def max_learned_rank(talent, known_spells) -> int:
    """Highest learned rank index for a talent, or ``-1`` when unlearned."""
    for rank in range(MAX_TALENT_RANK - 1, -1, -1):
        spell_id = int(talent[f"spell_rank{rank + 1}"] or 0)
        if spell_id and int(spell_id) in known_spells:
            return rank
    return -1


def spent_points_in_tab(path: str, tab_id: int,
                        talents: Dict[int, int]) -> int:
    total = 0
    for talent in talents_in_tab(path, tab_id):
        talent_id = int(talent["id"])
        if talent_id in talents:
            total += int(talents[talent_id]) + 1
    return total


def used_talent_points(talents: Dict[int, int]) -> int:
    return sum(int(rank) + 1 for rank in talents.values())


def build_talents_info_payload(character, groups, active_group: int = 0,
                               specs_count: int = 1) -> bytes:
    """``SMSG_TALENTS_INFO`` for the player's talent groups.

    ``groups`` is ``{spec: {talent_id: rank_index}}``.  The 3.3.5 layout is:
    uint8 isPet, uint32 free points, uint8 group count, uint8 active group,
    then for each group a talent list, glyph count and six glyph ids.
    """
    specs_count = max(1, min(2, int(specs_count)))
    active_group = max(0, min(specs_count - 1, int(active_group)))
    active = groups.get(active_group) or {}
    level = int(character.get("level", 1) or 1)
    free = max(0, talent_points_for_level(
        level, int(character.get("bonus_talents", 0) or 0),
        int(character.get("class", 0) or 0))
        - used_talent_points(active))
    out = bytearray()
    out += struct.pack("<B", 0)          # 0 = player talents
    out += struct.pack("<I", free)
    out += struct.pack("<BB", specs_count, active_group)
    glyphs = character.get("glyphs") or {}
    glyph_ids = [int(glyph) for glyph in glyphs] if isinstance(
        glyphs, dict) else []
    glyph_ids = (glyph_ids + [0] * MAX_GLYPH_SLOT_INDEX)[
        :MAX_GLYPH_SLOT_INDEX]
    for spec in range(specs_count):
        talents = groups.get(spec) or {}
        items = sorted(talents.items())
        out += struct.pack("<B", min(255, len(items)))
        for talent_id, rank in items[:255]:
            out += struct.pack("<I", int(talent_id))
            out += struct.pack("<b", min(MAX_TALENT_RANK - 1, int(rank)))
        out += struct.pack("<B", MAX_GLYPH_SLOT_INDEX)
        for glyph_id in glyph_ids:
            out += struct.pack("<H", glyph_id & 0xFFFF)
    return bytes(out)


def build_talent_wipe_confirm(guid: int, cost: int) -> bytes:
    """``MSG_TALENT_WIPE_CONFIRM``: trainer guid and respec cost in copper."""
    return struct.pack("<QI", int(guid) & 0xFFFFFFFFFFFFFFFF,
                       int(cost) & 0xFFFFFFFF)


def build_pet_talents_info_payload(free_points: int,
                                   talents: Dict[int, int]) -> bytes:
    """``SMSG_TALENT_UPDATE`` payload for the active hunter pet."""
    out = bytearray()
    out += struct.pack("<B", 1)          # 1 = pet talents
    out += struct.pack("<I", max(0, int(free_points)))
    items = sorted((int(key), int(value)) for key, value in talents.items())
    out += struct.pack("<B", min(255, len(items)))
    for talent_id, rank in items[:255]:
        out += struct.pack("<I", talent_id)
        out += struct.pack("<b", min(MAX_TALENT_RANK - 1, rank))
    return bytes(out)


def clear_caches() -> None:
    for cached in (_tab_cached, _talent_cached, _tab_talents_cached,
                   _class_tabs_cached, _family_cached, _pet_tabs_cached):
        cached.cache_clear()
