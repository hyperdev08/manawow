"""Talent data imported from ``Talent.dbc``/``TalentTab.dbc``."""
from __future__ import annotations

from functools import lru_cache
import sqlite3
from typing import Dict, List, Optional

MAX_TALENT_RANK = 5


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


def talent_points_for_level(level: int) -> int:
    """One point per level from 10 to 80 (``level - 9``)."""
    return max(0, int(level) - 9)


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


def build_talents_info_payload(character, talents: Dict[int, int]) -> bytes:
    """``SMSG_TALENTS_INFO`` for the player's single talent group."""
    import struct

    level = int(character.get("level", 1) or 1)
    free = max(0, talent_points_for_level(level)
               - used_talent_points(talents))
    out = bytearray()
    out += struct.pack("<B", 0)          # 0 = player talents
    out += struct.pack("<I", free)
    out += struct.pack("<BB", 1, 0)      # one group, active group 0
    out += struct.pack("<B", len(talents))
    for talent_id, rank in sorted(talents.items()):
        out += struct.pack("<I", int(talent_id))
        out += struct.pack("<b", min(MAX_TALENT_RANK - 1, int(rank)))
    glyphs = character.get("glyphs") or {}
    glyph_ids = [int(glyph) for glyph in glyphs] if isinstance(
        glyphs, dict) else []
    glyph_ids = (glyph_ids + [0] * 6)[:6]
    out += struct.pack("<B", 6)
    for glyph_id in glyph_ids:
        out += struct.pack("<H", glyph_id & 0xFFFF)
    return bytes(out)


def clear_caches() -> None:
    for cached in (_tab_cached, _talent_cached, _tab_talents_cached,
                   _class_tabs_cached):
        cached.cache_clear()
