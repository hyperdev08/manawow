"""The level-1 spellbook and action bar must match 3.3.5a exactly.

The authoritative data is the CMaNGOS ``playercreateinfo_spell`` /
``playercreateinfo_action`` pair bundled in ``src/world/playercreateinfo.sql``
and imported into the world database.  These tests build a fresh database and
create one character of every legal race/class combination.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from src.world import character_db, creature

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Abilities that must be in every character's spellbook, per class.  These are
# the class-defining level-1 abilities from playercreateinfo_spell.
CLASS_ABILITIES = {
    1: {78, 2457, 6603},
    2: {635, 21084, 6603},
    3: {75, 2973, 6603},
    4: {674, 1752, 2098, 6603},
    5: {585, 2050, 6603},
    6: {3127, 674, 750, 45462, 45477, 45902, 47541, 48266, 49576, 56816,
        59879, 59921, 6603},
    7: {331, 403, 6603},
    8: {133, 168, 5009, 5019, 6603},
    9: {686, 687, 688, 6603},
    11: {5176, 5185, 6603},
}


@pytest.fixture()
def world_db():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    os.unlink(path)
    creature.init_creature_db(path)
    character_db.init_character_db(path)
    try:
        yield path
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def _legal_pairs(con) -> list[tuple[int, int]]:
    return [
        (int(row[0]), int(row[1]))
        for row in con.execute(
            "SELECT DISTINCT race, class FROM playercreateinfo_spell"
            " ORDER BY race, class"
        )
    ]


def test_bundled_playercreateinfo_rows(world_db):
    con = sqlite3.connect(world_db)
    try:
        spells = con.execute(
            "SELECT COUNT(*) FROM playercreateinfo_spell").fetchone()[0]
        actions = con.execute(
            "SELECT COUNT(*) FROM playercreateinfo_action").fetchone()[0]
        pairs = _legal_pairs(con)
    finally:
        con.close()
    assert (spells, actions) == (2919, 351)
    assert len(pairs) == 62
    assert set(pairs) == {
        (race, cls)
        for race, classes in character_db.RACE_CLASSES.items()
        for cls in classes
    }


def test_every_race_class_gets_the_authentic_spellbook(world_db):
    con = sqlite3.connect(world_db)
    con.row_factory = sqlite3.Row
    try:
        pairs = _legal_pairs(con)
        for race, cls in pairs:
            name = f"Starter{race}_{cls}"
            guid = character_db.create_character(
                world_db, 1, name, race, cls, 0, 0, 0, 0, 0, 0)
            granted = {
                int(row[0]) for row in con.execute(
                    "SELECT spell FROM character_spells"
                    " WHERE character_guid = ?", (guid,))
            }
            expected = {
                int(row[0]) for row in con.execute(
                    "SELECT spell FROM playercreateinfo_spell"
                    " WHERE race = ? AND class = ?", (race, cls))
            }
            if cls == 9:
                expected.add(688)   # deliberate no-trainer Summon Imp
            assert granted == expected, (race, cls,
                                         sorted(expected - granted),
                                         sorted(granted - expected))
            assert CLASS_ABILITIES[cls] <= granted, (race, cls)
    finally:
        con.close()


def test_action_bar_only_uses_known_spells(world_db):
    con = sqlite3.connect(world_db)
    con.row_factory = sqlite3.Row
    try:
        for race, cls in _legal_pairs(con):
            guid = character_db.create_character(
                world_db, 1, f"Bar{race}_{cls}", race, cls, 0, 0, 0, 0, 0, 0)
            known = {
                int(row[0]) for row in con.execute(
                    "SELECT spell FROM character_spells"
                    " WHERE character_guid = ?", (guid,))
            }
            buttons = character_db.get_character_action_buttons(world_db, guid)
            assert 6603 in known
            for slot, (action, action_type) in buttons.items():
                assert 0 <= slot < 144
                if action_type == 0:
                    assert action in known, (race, cls, slot, action)
                else:
                    # The only macros in playercreateinfo_action are the
                    # default Attack macros, rewritten to Attack by the server.
                    assert (action, action_type) == (6603, 0), (race, cls, slot)
    finally:
        con.close()


def test_no_bogus_hunter_starter(world_db):
    # 261 is Summon Skeleton, not Arcane Shot (3044 is learned later).
    con = sqlite3.connect(world_db)
    try:
        pairs = _legal_pairs(con)
    finally:
        con.close()
    for race, cls in pairs:
        if cls != 3:
            continue
        spells = character_db.get_starter_spells(world_db, race, cls)
        assert 261 not in spells
        assert 3044 not in spells
