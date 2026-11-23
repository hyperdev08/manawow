"""Loot template rolling rules and corpse money."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.world import creature
from src.world.worldsocket.loot import LootMixin


class FakeLoot(LootMixin):
    def __init__(self, path: Path, player_guid: int = 100):
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=str(path)))
        self.player_guid = player_guid
        self.in_world = True
        self.character = {"map": 0}
        self.spawn = None
        self.responses = []
        self.combat = {"loot_target": 0, "loot_items": [], "loot_taken": [],
                       "loot_money": 0}

    def _get_combat_state(self):
        return self.combat

    def _get_creature_spawn(self, low_guid):
        return self.spawn

    async def _send_loot_response(self, guid, items, money):
        self.responses.append((guid, list(items), money))

    async def _send_creature_fields(self, low_guid, fields):
        pass

    def _set_loot_json(self, low_guid, payload):
        pass


def _make_loot_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript("""
        CREATE TABLE item_templates(
            entry INTEGER PRIMARY KEY, display_id INTEGER, quality INTEGER);
        CREATE TABLE creature_loot_template(
            entry INTEGER, item INTEGER, chance_or_quest_chance REAL,
            group_id INTEGER, mincount_or_ref INTEGER, maxcount INTEGER,
            condition_id INTEGER, comments TEXT);
        CREATE TABLE reference_loot_template(
            entry INTEGER, item INTEGER, chance_or_quest_chance REAL,
            group_id INTEGER, mincount_or_ref INTEGER, maxcount INTEGER,
            condition_id INTEGER, comments TEXT);
        """)
        con.executemany(
            "INSERT INTO item_templates VALUES(?,?,?)",
            [(value, value, 2) for value in
             (100, 101, 102, 103, 104, 200, *range(300, 320))])
        rows = [
            # Normal guaranteed drop.
            (1, 100, 100.0, 0, 1, 1, 0, ""),
            # Quest-only row: no quest engine, so it must not drop.
            (1, 101, -100.0, 0, 1, 1, 0, ""),
            # Conditional row: no condition engine, so it must not drop.
            (1, 102, 100.0, 0, 1, 1, 1, ""),
            # Explicit group winner.
            (1, 103, 100.0, 10, 1, 1, 0, ""),
            (1, 104, 0.0, 10, 1, 1, 0, ""),
            # Reference processed twice via maxcount.
            (1, 0, 100.0, 0, -900, 2, 0, ""),
        ]
        con.executemany(
            "INSERT INTO creature_loot_template VALUES(?,?,?,?,?,?,?,?)",
            rows)
        con.execute(
            "INSERT INTO reference_loot_template VALUES(?,?,?,?,?,?,?,?)",
            (900, 200, 100.0, 0, 1, 1, 0, ""))
        # A template with more guaranteed rows than the client can hold.
        con.executemany(
            "INSERT INTO creature_loot_template VALUES(?,?,?,?,?,?,?,?)",
            [(999, value, 100.0, 0, 1, 1, 0, "")
             for value in range(300, 320)])
        con.commit()
    finally:
        con.close()


def test_loot_rolls_skip_quest_conditions_and_expand_references(tmp_path):
    path = tmp_path / "loot.db"
    _make_loot_db(path)
    loot = FakeLoot(path)
    items = loot._generate_loot(1, 1)
    assert (100, 1) in items
    assert (200, 1) in items
    assert items.count((200, 1)) == 2
    assert (103, 1) in items
    assert all(entry not in (101, 102, 104) for entry, _count in items)


def test_loot_is_capped_at_client_slot_limit(tmp_path):
    path = tmp_path / "loot.db"
    _make_loot_db(path)
    loot = FakeLoot(path)
    items = loot._generate_loot(999, 999)
    assert len(items) == LootMixin.MAX_LOOT_ITEMS
    assert len({entry for entry, _count in items}) == LootMixin.MAX_LOOT_ITEMS


def test_loot_money_uses_creature_template_range():
    assert LootMixin._loot_money(
        {"min_loot_gold": 100, "max_loot_gold": 200}) in range(100, 201)
    assert LootMixin._loot_money(
        {"min_loot_gold": 70, "max_loot_gold": 70}) == 70
    assert LootMixin._loot_money(
        {"min_loot_gold": 0, "max_loot_gold": 0}) == 0
    # Missing columns are treated as "no money", not a synthetic level drop.
    assert LootMixin._loot_money({}) == 0


def test_real_northshire_loot_uses_known_items_and_gold():
    path = Path(ROOT) / "data" / "world.db"
    if not path.exists():
        return
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT entry, loot_id, min_loot_gold, max_loot_gold "
            "FROM creature_templates WHERE entry = 6").fetchone()
        if row is None:
            return
        known = {int(item[0]) for item in con.execute(
            "SELECT entry FROM item_templates")}
    finally:
        con.close()
    loot = FakeLoot(path)
    for _ in range(25):
        items = loot._generate_loot(row[0], row[1] or row[0])
        assert len(items) <= LootMixin.MAX_LOOT_ITEMS
        assert all(entry in known and count > 0
                   for entry, count in items)
        amount = loot._loot_money(
            {"min_loot_gold": row[2], "max_loot_gold": row[3]})
        assert row[2] <= amount <= row[3]


def test_loot_owner_can_open_corpse_only_as_tagger(tmp_path):
    path = tmp_path / "loot.db"
    _make_loot_db(path)
    low = 42
    object_guid = creature.creature_guid(low, 1234)
    loot_data = json.dumps({"items": [[100, 1]], "money": 5})

    owner = FakeLoot(path, player_guid=100)
    owner.spawn = {"dead_since": 1, "loot_json": loot_data,
                   "loot_owner": 100}
    asyncio.run(owner.handle_loot(object_guid.to_bytes(8, "little")))
    assert owner.responses[-1][1] == [(100, 1)]
    assert owner.responses[-1][2] == 5

    stranger = FakeLoot(path, player_guid=200)
    stranger.spawn = owner.spawn
    asyncio.run(stranger.handle_loot(object_guid.to_bytes(8, "little")))
    assert stranger.responses[-1][1] == []
    assert stranger.responses[-1][2] == 0
