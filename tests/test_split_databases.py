"""World content and per-character state must live in separate SQLite files."""
from __future__ import annotations

import os
import sqlite3

import pytest

from src.common.config import Config, DBConfig
from src.world import character_db, creature, databases, quests


@pytest.fixture(autouse=True)
def _reset_database_routing():
    yield
    databases.reset()


def _fresh_world(path: str) -> None:
    creature.init_creature_db(path)
    character_db.init_world_db(path)
    quests.init_world_quest_db(path)


def test_character_rows_are_written_to_the_separate_store(tmp_path):
    world = str(tmp_path / "world.db")
    characters = str(tmp_path / "characters.db")
    _fresh_world(world)

    con = sqlite3.connect(world)
    con.execute(
        "INSERT OR REPLACE INTO item_templates(entry, name1, stackable,"
        " display_id) VALUES(159, 'Ice Cold Milk', 20, 100)")
    con.commit()
    con.close()

    character_db.configure(world, characters)
    character_db.init_split_databases(world, characters)
    assert character_db.client_cache_version(world) > 0

    guid = character_db.create_character(
        world, 1, "Splitguy", 1, 1, 0, 0, 0, 0, 0, 0)
    assert character_db.list_characters(world, 1)[0]["name"] == "Splitguy"

    changes = character_db.add_item_to_inventory(world, guid, 159, 3)
    assert changes and changes[0]["created"]
    assert character_db.get_character_inventory(world, guid)[256]["item_entry"] == 159

    quests.save_quest(world, guid, 783,
                      {"slot": 1, "status": 3, "explored": 0,
                       "counts": [0, 0, 0, 0]})
    assert 783 in quests.load_quest_log(world, guid)

    store = sqlite3.connect(characters)
    try:
        assert store.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1
        assert store.execute(
            "SELECT COUNT(*) FROM character_items").fetchone()[0] == 1
        assert store.execute(
            "SELECT COUNT(*) FROM character_quests").fetchone()[0] == 1
    finally:
        store.close()

    world_con = sqlite3.connect(world)
    try:
        world_tables = {row[0] for row in world_con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        world_con.close()
    assert "characters" not in world_tables
    assert "character_quests" not in world_tables
    assert "item_templates" in world_tables


def test_legacy_combined_database_is_migrated_once(tmp_path):
    world = str(tmp_path / "world.db")
    characters = str(tmp_path / "characters.db")

    # A pre-split database: character tables and world tables in one file.
    creature.init_creature_db(world)
    character_db.init_character_db(world)
    quests.init_quest_db(world)
    con = sqlite3.connect(world)
    con.execute(
        "INSERT INTO characters(guid, account_id, name, race, class, level)"
        " VALUES(9, 1, 'Peerchar', 1, 1, 5)")
    con.execute(
        "INSERT INTO character_items(character_guid, item_entry, slot,"
        " stack_count) VALUES(9, 25, 0, 1)")
    con.execute(
        "INSERT INTO character_quests(character_guid, quest_id, slot)"
        " VALUES(9, 783, 1)")
    con.commit()
    con.close()

    character_db.configure(world, characters)
    character_db.init_split_databases(world, characters)

    store = sqlite3.connect(characters)
    try:
        assert store.execute(
            "SELECT name, level FROM characters WHERE guid = 9").fetchone() == (
                "Peerchar", 5)
        assert store.execute(
            "SELECT COUNT(*) FROM character_items WHERE character_guid = 9"
        ).fetchone()[0] == 1
        assert store.execute(
            "SELECT COUNT(*) FROM character_quests WHERE character_guid = 9"
        ).fetchone()[0] == 1
    finally:
        store.close()

    # Running the migration again must not duplicate or overwrite rows.
    character_db.init_split_databases(world, characters)
    store = sqlite3.connect(characters)
    try:
        assert store.execute(
            "SELECT COUNT(*) FROM characters").fetchone()[0] == 1
    finally:
        store.close()


def test_config_derives_a_sibling_character_database(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "database:\n"
        "  world_path: \"./data/world.db\"\n",
        encoding="utf-8")
    cfg = Config.load(config_path)
    assert os.path.normpath(cfg.database.character_path) == os.path.normpath(
        os.path.join(".", "data", "characters.db"))

    # Explicitly empty keeps the combined layout used by tests and tools.
    combined_path = tmp_path / "combined.yaml"
    combined_path.write_text(
        "database:\n"
        "  world_path: \"./data/world.db\"\n"
        "  character_path: \"\"\n",
        encoding="utf-8")
    assert Config.load(combined_path).database.character_path == ""
    assert DBConfig().character_path == ""


def test_configure_with_same_path_is_combined(tmp_path):
    path = str(tmp_path / "combined.db")
    character_db.configure(path, path)
    assert not databases.is_split()
    assert databases.character_path(path) == path
